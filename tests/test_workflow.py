import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as main_module
from app.main import app
from app.agent_workflow import ingest_invoice
from app.models import InvoiceStatus, SourceType


@pytest.fixture()
def temp_db(tmp_path: Path) -> Path:
    path = tmp_path / "register.db"
    db.init_db(path)
    return path


def test_text_invoice_registers(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    text = """Supplier: Kopi Kita Sdn Bhd
Invoice No: INV-100
Date: 12/05/2026
Total: RM 235.32"""

    draft, trace, mode = asyncio.run(
        ingest_invoice(SourceType.TEXT, "message.txt", text.encode(), text, temp_db)
    )

    assert mode == "local-fallback"
    assert draft.status == InvoiceStatus.REGISTERED
    assert draft.supplier_name == "Kopi Kita Sdn Bhd"
    assert draft.invoice_no == "INV-100"
    assert draft.issue_date == "2026-05-12"
    assert draft.total_amount == 235.32
    assert any(step.agent == "MessageInvoiceAgent" for step in trace)


def test_duplicate_invoice_needs_review(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    text = """Supplier: Kopi Kita Sdn Bhd
Invoice No: INV-100
Date: 12/05/2026
Total: RM 235.32"""
    first, trace, _ = asyncio.run(
        ingest_invoice(SourceType.TEXT, "message.txt", text.encode(), text, temp_db)
    )
    artifact_id = db.create_artifact(SourceType.TEXT, "message.txt", "hash-1", "memory", temp_db)
    db.save_invoice(artifact_id, first, trace, temp_db)

    second, _, _ = asyncio.run(
        ingest_invoice(SourceType.TEXT, "message.txt", text.encode(), text, temp_db)
    )

    assert second.status == InvoiceStatus.NEEDS_REVIEW
    assert second.duplicate_candidates
    assert "Possible duplicate invoice found" in second.review_reasons


def test_missing_required_field_needs_review(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    text = """Supplier: Maju Trading
Date: 12/05/2026
Total: RM 88.00"""

    draft, _, _ = asyncio.run(
        ingest_invoice(SourceType.TEXT, "message.txt", text.encode(), text, temp_db)
    )

    assert draft.status == InvoiceStatus.NEEDS_REVIEW
    assert "invoice_no" in draft.missing_fields


def test_non_invoice_rejected(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    text = "Birthday lunch agenda and weather notes."

    draft, _, _ = asyncio.run(
        ingest_invoice(SourceType.TEXT, "note.txt", text.encode(), text, temp_db)
    )

    assert draft.status == InvoiceStatus.REJECTED


def test_pdf_and_image_fallback_need_review(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    pdf, _, _ = asyncio.run(
        ingest_invoice(SourceType.PDF, "kopi-kita-invoice-INV-200.pdf", b"%PDF-1.4", "", temp_db)
    )
    image, _, _ = asyncio.run(
        ingest_invoice(SourceType.IMAGE, "receipt-photo-300.jpg", b"\xff\xd8\xff", "", temp_db)
    )

    assert pdf.source_type == SourceType.PDF
    assert pdf.status == InvoiceStatus.NEEDS_REVIEW
    assert image.source_type == SourceType.IMAGE
    assert image.status == InvoiceStatus.NEEDS_REVIEW


def test_dashboard_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "dashboard.db")
    monkeypatch.setattr(main_module, "UPLOAD_DIR", tmp_path / "uploads")
    db.init_db(db.DATABASE_PATH)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Invoice Register" in response.text


def test_whatsapp_simulator_ingests_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "whatsapp.db")
    monkeypatch.setattr(main_module, "UPLOAD_DIR", tmp_path / "uploads")
    db.init_db(db.DATABASE_PATH)

    message = "Supplier: Restoran Seri Pagi\nInvoice No: WA-7781\nDate: 14/05/2026\nTotal: RM 129.90"
    with TestClient(app) as client:
        response = client.post("/connectors/whatsapp/simulate", data={"message": message})

    assert response.status_code == 200
    invoices = db.list_invoices(db.DATABASE_PATH)
    assert len(invoices) == 1
    assert invoices[0].invoice_no == "WA-7781"


def test_twilio_whatsapp_webhook_ingests_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "webhook.db")
    monkeypatch.setattr(main_module, "UPLOAD_DIR", tmp_path / "uploads")
    db.init_db(db.DATABASE_PATH)

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp",
            data={
                "From": "whatsapp:+60123456789",
                "Body": "Supplier: Auto Parts KL\nInvoice No: WA-900\nDate: 15/05/2026\nTotal: RM 401.20",
                "NumMedia": "0",
            },
        )

    assert response.status_code == 200
    assert response.json()["ingested"] == 1
    invoices = db.list_invoices(db.DATABASE_PATH)
    assert invoices[0].supplier_name == "Auto Parts KL"
