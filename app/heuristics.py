from __future__ import annotations

import re
from pathlib import Path

from . import db
from .models import InvoiceDraft, InvoiceStatus, LineItem, SourceType, TraceStep


NON_INVOICE_HINTS = {
    "birthday",
    "holiday",
    "recipe",
    "meeting agenda",
    "weather",
    "football",
}


def extract_invoice_locally(
    source_type: SourceType,
    filename: str,
    payload_text: str,
    db_path: Path | str = db.DATABASE_PATH,
) -> tuple[InvoiceDraft, list[TraceStep]]:
    trace = [
        TraceStep(
            agent="InvoiceRegisterOrchestrator",
            action="started",
            detail=f"Local fallback workflow selected for {source_type.value} input.",
            confidence=0.0,
        )
    ]

    draft = _extract_from_text(source_type, filename, payload_text)
    trace.append(
        TraceStep(
            agent=_agent_name(source_type),
            action="extracted",
            detail=_extraction_detail(source_type, draft),
            confidence=draft.confidence,
        )
    )

    _normalize(draft)
    trace.append(
        TraceStep(
            agent="NormalizerAgent",
            action="normalized",
            detail=f"Normalized currency to {draft.currency} and applied Malaysian SME register schema.",
            confidence=draft.confidence,
        )
    )

    draft.duplicate_candidates = db.find_similar_invoices(
        draft.supplier_name,
        draft.invoice_no,
        draft.total_amount,
        draft.issue_date,
        db_path=db_path,
    )
    duplicate_status = "warning" if draft.duplicate_candidates else "ok"
    duplicate_detail = (
        f"Found {len(draft.duplicate_candidates)} possible duplicate(s)."
        if draft.duplicate_candidates
        else "No duplicate candidates found."
    )
    trace.append(
        TraceStep(
            agent="DuplicateRiskAgent",
            action="checked",
            status=duplicate_status,
            detail=duplicate_detail,
            confidence=0.86,
        )
    )

    draft.enforce_review_rules()
    trace.append(
        TraceStep(
            agent="ReviewDecisionAgent",
            action="decided",
            status="warning" if draft.status == InvoiceStatus.NEEDS_REVIEW else "ok",
            detail=f"Set invoice status to {draft.status.value}.",
            confidence=draft.confidence,
        )
    )
    return draft, trace


def _extract_from_text(source_type: SourceType, filename: str, text: str) -> InvoiceDraft:
    lowered = text.casefold()
    if text and not _looks_like_invoice(lowered):
        return InvoiceDraft(
            supplier_name="",
            invoice_no="",
            issue_date="",
            source_type=source_type,
            confidence=0.21,
            status=InvoiceStatus.REJECTED,
            raw_text=text,
            review_reasons=["Input does not look like an invoice, receipt, bill, or order request."],
        )

    supplier = _match(
        [
            r"(?:supplier|vendor|merchant|company)\s*[:\-]\s*(.+)",
            r"(?:from)\s*[:\-]\s*(.+)",
            r"^([A-Z][A-Za-z0-9 &.'()/-]{2,})\s*(?:Sdn\.?\s*Bhd\.?|Enterprise|Trading)?",
        ],
        text,
    )
    invoice_no = _match(
        [
            r"(?:invoice|inv|receipt|bill|order)\s*(?:no|number|#)?\s*[:\-#]?\s*([A-Za-z0-9\-\/]+)",
            r"\b(INV[-\/]?[A-Za-z0-9\-\/]+)\b",
        ],
        text,
    )
    issue_date = _match(
        [
            r"(?:date|issued on|invoice date)\s*[:\-]\s*([0-9]{1,2}[\/\-.][0-9]{1,2}[\/\-.][0-9]{2,4})",
            r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})\b",
        ],
        text,
    )
    total_amount = _amount(
        _match(
            [
                r"(?:grand total|amount due|total|jumlah)\s*[:\-]?\s*(?:RM|MYR)?\s*([0-9,]+(?:\.[0-9]{2})?)",
                r"\b(?:RM|MYR)\s*([0-9,]+(?:\.[0-9]{2})?)\b",
            ],
            text,
        )
    )
    sst = _amount(
        _match(
            [r"(?:sst|service tax|tax)\s*[:\-]?\s*(?:RM|MYR)?\s*([0-9,]+(?:\.[0-9]{2})?)"],
            text,
        )
    )
    subtotal = _amount(
        _match([r"(?:subtotal|sub-total)\s*[:\-]?\s*(?:RM|MYR)?\s*([0-9,]+(?:\.[0-9]{2})?)"], text)
    )
    registration_no = _match(
        [
            r"(?:brn|reg(?:istration)?\s*no|company\s*no)\s*[:\-]\s*([A-Za-z0-9\-]+)",
            r"\b([0-9]{6,12}-?[A-Z]?)\b",
        ],
        text,
    )

    if source_type in {SourceType.PDF, SourceType.IMAGE} and not text.strip():
        supplier = _supplier_from_filename(filename)
        invoice_no = _invoice_from_filename(filename)

    confidence = _confidence(source_type, supplier, invoice_no, issue_date, total_amount, bool(text.strip()))
    line_items = _line_items(text)

    return InvoiceDraft(
        supplier_name=supplier,
        invoice_no=invoice_no,
        issue_date=_normalize_date(issue_date),
        currency="MYR" if re.search(r"\b(RM|MYR)\b", text, re.I) else "MYR",
        total_amount=total_amount,
        source_type=source_type,
        confidence=confidence,
        supplier_registration_no=registration_no,
        sst_or_tax_amount=sst,
        subtotal=subtotal,
        line_items=line_items,
        raw_text=text,
    )


def _normalize(draft: InvoiceDraft) -> None:
    draft.supplier_name = " ".join(draft.supplier_name.split()).strip(" :-")
    draft.invoice_no = draft.invoice_no.strip(" .,:;#")
    draft.currency = "MYR"


def _looks_like_invoice(lowered: str) -> bool:
    if any(hint in lowered for hint in NON_INVOICE_HINTS):
        return False
    return any(
        token in lowered
        for token in ["invoice", "receipt", "bill", "total", "amount", "rm", "myr", "sst", "order"]
    )


def _match(patterns: list[str], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def _amount(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _normalize_date(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.fullmatch(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})", value)
    if not match:
        return value
    day, month, year = match.groups()
    if len(year) == 2:
        year = "20" + year
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _confidence(
    source_type: SourceType,
    supplier: str,
    invoice_no: str,
    issue_date: str,
    total_amount: float | None,
    has_text: bool,
) -> float:
    score = 0.35 if has_text else 0.18
    score += 0.15 if supplier else 0
    score += 0.15 if invoice_no else 0
    score += 0.15 if issue_date else 0
    score += 0.15 if total_amount is not None else 0
    if source_type == SourceType.IMAGE and has_text:
        score -= 0.03
    return round(min(score, 0.96), 2)


def _line_items(text: str) -> list[LineItem]:
    items = []
    for raw in text.splitlines():
        match = re.search(r"^[-*]?\s*(.+?)\s+(?:RM|MYR)?\s*([0-9,]+\.[0-9]{2})$", raw.strip(), re.I)
        if match and not re.search(r"total|tax|sst|subtotal", match.group(1), re.I):
            items.append(LineItem(description=match.group(1).strip(), amount=_amount(match.group(2))))
    return items[:8]


def _supplier_from_filename(filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\b(invoice|receipt|bill|scan|photo|img|pdf)\b", "", stem, flags=re.I)
    return " ".join(stem.split()).title()


def _invoice_from_filename(filename: str) -> str:
    match = re.search(r"(INV[-_ ]?[A-Za-z0-9]+|[0-9]{4,})", filename, re.I)
    return match.group(1).replace(" ", "-").replace("_", "-").upper() if match else ""


def _agent_name(source_type: SourceType) -> str:
    return {
        SourceType.PDF: "PdfInvoiceAgent",
        SourceType.IMAGE: "ImageInvoiceAgent",
        SourceType.TEXT: "MessageInvoiceAgent",
    }[source_type]


def _extraction_detail(source_type: SourceType, draft: InvoiceDraft) -> str:
    if source_type in {SourceType.PDF, SourceType.IMAGE} and not draft.raw_text.strip():
        return "No text layer/OCR text was available locally; filename clues were used and review is required."
    return f"Extracted supplier={draft.supplier_name or 'missing'}, invoice_no={draft.invoice_no or 'missing'}."
