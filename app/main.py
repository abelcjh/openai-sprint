from __future__ import annotations

import os
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db
from .agent_workflow import content_hash, ingest_invoice
from .connectors import (
    connector_status,
    fetch_email_payloads,
    payloads_from_meta_json,
    payloads_from_twilio_form,
)
from .models import SourceType, TraceStep


UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Invoice Register Agents SDK Demo", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    invoices = db.list_invoices()
    status = connector_status()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "invoices": invoices,
            "registered_count": sum(1 for item in invoices if item.status.value == "registered"),
            "review_count": sum(1 for item in invoices if item.status.value == "needs_review"),
            "rejected_count": sum(1 for item in invoices if item.status.value == "rejected"),
            "openai_enabled": bool(os.getenv("OPENAI_API_KEY")),
            "model": os.getenv("OPENAI_MODEL", "gpt-5.5"),
            "sample_text": SAMPLE_TEXT,
            "connector_status": status,
            "base_url": str(request.base_url).rstrip("/"),
            "notice": request.query_params.get("notice", ""),
        },
    )


@app.post("/ingest/text")
async def ingest_text(invoice_text: str = Form(...)) -> RedirectResponse:
    content = invoice_text.encode("utf-8")
    await _persist_ingestion(SourceType.TEXT, "whatsapp-message.txt", content, invoice_text)
    return RedirectResponse("/", status_code=303)


@app.post("/ingest/file")
async def ingest_file(file: UploadFile = File(...)) -> RedirectResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    source_type = _source_type_for_upload(file)
    await _persist_ingestion(source_type, file.filename or "invoice-upload", content, "")
    return RedirectResponse("/", status_code=303)


@app.post("/connectors/email/poll")
async def poll_email() -> RedirectResponse:
    try:
        payloads = fetch_email_payloads(limit=int(os.getenv("EMAIL_IMAP_LIMIT", "5")))
    except Exception as exc:
        return RedirectResponse(f"/?notice={quote(str(exc))}", status_code=303)

    for payload in payloads:
        await _persist_ingestion(payload.source_type, payload.filename, payload.content, payload.text_hint)
    return RedirectResponse(f"/?notice={quote(f'Email poll complete: {len(payloads)} item(s) ingested.')}", status_code=303)


@app.post("/connectors/whatsapp/simulate")
async def simulate_whatsapp(message: str = Form(...)) -> RedirectResponse:
    content = f"From: whatsapp-demo\n{message}".encode("utf-8")
    await _persist_ingestion(SourceType.TEXT, "whatsapp-simulated-message.txt", content, content.decode("utf-8"))
    return RedirectResponse("/", status_code=303)


@app.get("/webhooks/whatsapp")
def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    params = request.query_params
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if expected and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == expected:
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="WhatsApp webhook verification failed.")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payloads = await payloads_from_meta_json(await request.json())
    else:
        form = await request.form()
        payloads = await payloads_from_twilio_form({key: str(value) for key, value in form.items()})

    invoice_ids = []
    for payload in payloads:
        invoice_ids.append(
            await _persist_ingestion(payload.source_type, payload.filename, payload.content, payload.text_hint)
        )
    return JSONResponse({"ok": True, "ingested": len(invoice_ids), "invoice_ids": invoice_ids})


@app.post("/invoices/{invoice_id}/approve")
def approve(invoice_id: int) -> RedirectResponse:
    db.approve_invoice(invoice_id)
    return RedirectResponse("/", status_code=303)


@app.post("/invoices/{invoice_id}/delete")
def delete(invoice_id: int) -> RedirectResponse:
    db.delete_invoice(invoice_id)
    return RedirectResponse("/", status_code=303)


@app.get("/export.csv")
def export_csv() -> Response:
    return Response(
        content=db.export_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=invoice-register.csv"},
    )


async def _persist_ingestion(
    source_type: SourceType,
    filename: str,
    content: bytes,
    text_hint: str,
) -> int:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    digest = content_hash(content)
    storage_name = f"{digest[:12]}-{Path(filename).name}"
    storage_path = UPLOAD_DIR / storage_name
    storage_path.write_bytes(content)

    artifact_id = db.create_artifact(
        source_type=source_type,
        filename=filename,
        content_hash=digest,
        storage_path=str(storage_path),
        db_path=db.DATABASE_PATH,
    )
    draft, trace, mode = await ingest_invoice(source_type, filename, content, text_hint, db_path=db.DATABASE_PATH)
    trace.append(
        TraceStep(
            agent="RegisterStore",
            action="saved",
            status="ok",
            detail=f"Saved raw artifact and invoice draft using {mode}.",
            confidence=draft.confidence,
        )
    )
    return db.save_invoice(artifact_id, draft, trace, db_path=db.DATABASE_PATH)


def _source_type_for_upload(file: UploadFile) -> SourceType:
    filename = (file.filename or "").casefold()
    content_type = (file.content_type or "").casefold()
    if filename.endswith(".pdf") or content_type == "application/pdf":
        return SourceType.PDF
    if content_type.startswith("image/") or filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return SourceType.IMAGE
    raise HTTPException(status_code=400, detail="Upload a PDF or image invoice.")


SAMPLE_TEXT = """Supplier: Kopi Kita Sdn Bhd
Reg No: 202001234567
Invoice No: INV-2026-0512
Date: 12/05/2026
Kopi beans 5kg RM 180.00
Paper cups RM 42.00
Subtotal: RM 222.00
SST: RM 13.32
Total: RM 235.32"""
