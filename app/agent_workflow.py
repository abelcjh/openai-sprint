from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path

from . import db
from .heuristics import extract_invoice_locally
from .models import InvoiceDraft, SourceType, TraceStep


DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
FALLBACK_MODEL = os.getenv("OPENAI_FALLBACK_MODEL", "gpt-5.4-mini")


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def ingest_invoice(
    source_type: SourceType,
    filename: str,
    content: bytes,
    text_hint: str = "",
    db_path: Path | str = db.DATABASE_PATH,
) -> tuple[InvoiceDraft, list[TraceStep], str]:
    text = (_bytes_to_text(content) if source_type == SourceType.TEXT else text_hint) or ""
    if _should_use_agents():
        try:
            draft, trace = await _run_agents(source_type, filename, content, text, db_path)
            return draft.enforce_review_rules(), trace, "agents-sdk"
        except Exception as exc:  # pragma: no cover - exercised manually with API failures
            draft, trace = extract_invoice_locally(source_type, filename, text, db_path)
            trace.insert(
                0,
                TraceStep(
                    agent="AgentsSDK",
                    action="fallback",
                    status="warning",
                    detail=f"Live Agents SDK path failed, local fallback used: {exc}",
                ),
            )
            return draft, trace, "local-fallback"

    draft, trace = extract_invoice_locally(source_type, filename, text, db_path)
    trace.insert(
        0,
        TraceStep(
            agent="AgentsSDK",
            action="skipped",
            status="warning",
            detail="OPENAI_API_KEY or openai-agents package is unavailable; local fallback used.",
        ),
    )
    return draft, trace, "local-fallback"


def _should_use_agents() -> bool:
    if not os.getenv("OPENAI_API_KEY"):
        return False
    try:
        import agents  # noqa: F401
    except Exception:
        return False
    return True


async def _run_agents(
    source_type: SourceType,
    filename: str,
    content: bytes,
    text: str,
    db_path: Path | str,
) -> tuple[InvoiceDraft, list[TraceStep]]:
    from agents import Agent, Runner, function_tool

    trace: list[TraceStep] = [
        TraceStep(
            agent="InvoiceRegisterOrchestrator",
            action="started",
            detail=f"Live Agents SDK workflow using {DEFAULT_MODEL}.",
        )
    ]

    @function_tool
    def find_similar_invoices(
        supplier_name: str,
        invoice_no: str,
        total_amount: float | None,
        issue_date: str,
    ) -> str:
        """Return possible duplicate invoices from the register."""
        matches = db.find_similar_invoices(supplier_name, invoice_no, total_amount, issue_date, db_path)
        return json.dumps([match.model_dump() for match in matches])

    @function_tool
    def create_invoice_draft(invoice_json: str, confidence: float, review_reasons: list[str]) -> str:
        """Validate a draft invoice payload and echo the canonical JSON."""
        data = json.loads(invoice_json)
        data["confidence"] = confidence
        data["review_reasons"] = review_reasons
        draft = InvoiceDraft(**data)
        return draft.model_dump_json()

    pdf_agent = Agent(
        name="PdfInvoiceAgent",
        instructions=_specialist_instructions("PDF invoices"),
        model=DEFAULT_MODEL,
        output_type=InvoiceDraft,
    )
    image_agent = Agent(
        name="ImageInvoiceAgent",
        instructions=_specialist_instructions("receipt photos and physical invoices"),
        model=DEFAULT_MODEL,
        output_type=InvoiceDraft,
    )
    message_agent = Agent(
        name="MessageInvoiceAgent",
        instructions=_specialist_instructions("WhatsApp, SMS, and plain text invoice messages"),
        model=DEFAULT_MODEL,
        output_type=InvoiceDraft,
    )
    normalizer_agent = Agent(
        name="NormalizerAgent",
        instructions=(
            "Normalize invoice drafts into Malaysian SME register format. "
            "Use MYR for RM, ISO dates where possible, concise supplier names, and preserve uncertain fields."
        ),
        model=FALLBACK_MODEL,
        output_type=InvoiceDraft,
    )
    duplicate_agent = Agent(
        name="DuplicateRiskAgent",
        instructions=(
            "Check for duplicate invoices using find_similar_invoices. "
            "Never delete or overwrite. Add duplicate candidates and review reasons."
        ),
        model=FALLBACK_MODEL,
        tools=[find_similar_invoices],
        output_type=InvoiceDraft,
    )
    review_agent = Agent(
        name="ReviewDecisionAgent",
        instructions=(
            "Set status to registered, needs_review, or rejected. "
            "Never auto-register if supplier_name, invoice_no, issue_date, or total_amount is missing. "
            "Use needs_review for duplicate candidates or confidence under 0.72."
        ),
        model=FALLBACK_MODEL,
        output_type=InvoiceDraft,
    )

    orchestrator = Agent(
        name="InvoiceRegisterOrchestrator",
        instructions=(
            "You are the central invoice register manager for Malaysian SMEs. "
            "Call the right specialist, normalize the output, check duplicates, then decide review status. "
            "Return exactly one canonical InvoiceDraft. Do not invent unavailable values; leave missing fields blank."
        ),
        model=DEFAULT_MODEL,
        tools=[
            pdf_agent.as_tool(
                tool_name="extract_pdf_invoice",
                tool_description="Extract a canonical invoice draft from a PDF invoice.",
            ),
            image_agent.as_tool(
                tool_name="extract_image_invoice",
                tool_description="Extract a canonical invoice draft from a receipt photo or physical invoice image.",
            ),
            message_agent.as_tool(
                tool_name="extract_message_invoice",
                tool_description="Extract a canonical invoice draft from WhatsApp, SMS, or pasted text.",
            ),
            normalizer_agent.as_tool(
                tool_name="normalize_invoice",
                tool_description="Normalize an invoice draft into the register schema.",
            ),
            duplicate_agent.as_tool(
                tool_name="check_duplicate_risk",
                tool_description="Find possible duplicate invoices already in the register.",
            ),
            review_agent.as_tool(
                tool_name="decide_review_status",
                tool_description="Apply review guardrails and decide the final invoice status.",
            ),
            create_invoice_draft,
            find_similar_invoices,
        ],
        output_type=InvoiceDraft,
    )

    result = await Runner.run(
        orchestrator,
        _agent_input(source_type, filename, content, text),
        max_turns=8,
    )
    draft = result.final_output
    if not isinstance(draft, InvoiceDraft):
        draft = InvoiceDraft.model_validate(draft)

    draft.source_type = source_type
    draft.duplicate_candidates = db.find_similar_invoices(
        draft.supplier_name,
        draft.invoice_no,
        draft.total_amount,
        draft.issue_date,
        db_path=db_path,
    )
    draft.enforce_review_rules()
    trace.extend(
        [
            TraceStep(
                agent="AgentsSDK",
                action="completed",
                detail="Agent workflow completed. See OpenAI dashboard traces for the full run.",
                confidence=draft.confidence,
            ),
            TraceStep(
                agent="ReviewDecisionAgent",
                action="decided",
                detail=f"Set invoice status to {draft.status.value}.",
                confidence=draft.confidence,
            ),
        ]
    )
    return draft, trace


def _specialist_instructions(surface: str) -> str:
    return (
        f"Extract invoice data from {surface}. Return InvoiceDraft only. "
        "Capture supplier_name, invoice_no, issue_date, currency, total_amount, SST/tax, subtotal, due_date, "
        "registration number, and line items when present. Use blank strings/nulls instead of guessing. "
        "Set confidence based on extraction certainty."
    )


def _agent_input(source_type: SourceType, filename: str, content: bytes, text: str):
    prompt = (
        f"Source type: {source_type.value}\n"
        f"Filename: {filename}\n"
        "Extract and register this invoice for a Malaysian SME. "
        "If fields are uncertain, mark needs_review with reasons."
    )
    if source_type == SourceType.TEXT:
        return f"{prompt}\n\n{text}"

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    encoded = base64.b64encode(content).decode("ascii")
    if source_type == SourceType.PDF:
        payload = {
            "type": "input_file",
            "filename": filename,
            "file_data": f"data:{mime_type};base64,{encoded}",
        }
    else:
        payload = {
            "type": "input_image",
            "image_url": f"data:{mime_type};base64,{encoded}",
        }
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                payload,
            ],
        }
    ]


def _bytes_to_text(content: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            decoded = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in decoded[:100]:
            return decoded
    return ""
