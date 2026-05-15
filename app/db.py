from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import (
    DuplicateCandidate,
    ExistingInvoice,
    InvoiceDraft,
    InvoiceRecord,
    InvoiceStatus,
    SourceType,
    TraceStep,
)


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/invoice_register.db"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: Path | str = DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | str = DATABASE_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artifact_id INTEGER NOT NULL,
                supplier_name TEXT NOT NULL,
                invoice_no TEXT NOT NULL,
                issue_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                total_amount REAL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                supplier_registration_no TEXT NOT NULL DEFAULT '',
                sst_or_tax_amount REAL,
                subtotal REAL,
                due_date TEXT NOT NULL DEFAULT '',
                line_items_json TEXT NOT NULL DEFAULT '[]',
                duplicate_candidates_json TEXT NOT NULL DEFAULT '[]',
                missing_fields_json TEXT NOT NULL DEFAULT '[]',
                review_reasons_json TEXT NOT NULL DEFAULT '[]',
                raw_text TEXT NOT NULL DEFAULT '',
                trace_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
            );
            """
        )


def create_artifact(
    source_type: SourceType,
    filename: str,
    content_hash: str,
    storage_path: str,
    db_path: Path | str = DATABASE_PATH,
) -> int:
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO artifacts (source_type, filename, content_hash, storage_path, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_type.value, filename, content_hash, storage_path, utc_now()),
        )
        return int(cursor.lastrowid)


def save_invoice(
    artifact_id: int,
    draft: InvoiceDraft,
    trace: list[TraceStep],
    db_path: Path | str = DATABASE_PATH,
) -> int:
    now = utc_now()
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO invoices (
                artifact_id, supplier_name, invoice_no, issue_date, currency, total_amount,
                status, confidence, supplier_registration_no, sst_or_tax_amount, subtotal,
                due_date, line_items_json, duplicate_candidates_json, missing_fields_json,
                review_reasons_json, raw_text, trace_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                draft.supplier_name,
                draft.invoice_no,
                draft.issue_date,
                draft.currency,
                draft.total_amount,
                draft.status.value,
                draft.confidence,
                draft.supplier_registration_no,
                draft.sst_or_tax_amount,
                draft.subtotal,
                draft.due_date,
                json.dumps([item.model_dump() for item in draft.line_items]),
                json.dumps([item.model_dump() for item in draft.duplicate_candidates]),
                json.dumps(draft.missing_fields),
                json.dumps(draft.review_reasons),
                draft.raw_text,
                json.dumps([step.model_dump() for step in trace]),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def approve_invoice(invoice_id: int, db_path: Path | str = DATABASE_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE invoices
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (InvoiceStatus.REGISTERED.value, utc_now(), invoice_id),
        )


def delete_invoice(invoice_id: int, db_path: Path | str = DATABASE_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))


def list_existing(db_path: Path | str = DATABASE_PATH) -> list[ExistingInvoice]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, supplier_name, invoice_no, issue_date, total_amount
            FROM invoices
            WHERE status != ?
            ORDER BY created_at DESC
            """,
            (InvoiceStatus.REJECTED.value,),
        ).fetchall()
    return [
        ExistingInvoice(
            id=row["id"],
            supplier_name=row["supplier_name"],
            invoice_no=row["invoice_no"],
            issue_date=row["issue_date"],
            total_amount=row["total_amount"],
        )
        for row in rows
    ]


def list_invoices(db_path: Path | str = DATABASE_PATH) -> list[InvoiceRecord]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT invoices.*, artifacts.source_type AS artifact_source_type
            FROM invoices
            LEFT JOIN artifacts ON artifacts.id = invoices.artifact_id
            ORDER BY created_at DESC
            """
        ).fetchall()
    return [_row_to_invoice(row) for row in rows]


def get_invoice(invoice_id: int, db_path: Path | str = DATABASE_PATH) -> InvoiceRecord | None:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT invoices.*, artifacts.source_type AS artifact_source_type
            FROM invoices
            LEFT JOIN artifacts ON artifacts.id = invoices.artifact_id
            WHERE invoices.id = ?
            """,
            (invoice_id,),
        ).fetchone()
    return _row_to_invoice(row) if row else None


def find_similar_invoices(
    supplier_name: str,
    invoice_no: str,
    total_amount: float | None,
    issue_date: str,
    db_path: Path | str = DATABASE_PATH,
) -> list[DuplicateCandidate]:
    candidates: list[DuplicateCandidate] = []
    supplier_norm = _norm(supplier_name)
    invoice_norm = _norm(invoice_no)

    for existing in list_existing(db_path):
        reasons = []
        if invoice_norm and _norm(existing.invoice_no) == invoice_norm:
            reasons.append("same invoice number")
        if supplier_norm and supplier_norm in _norm(existing.supplier_name):
            reasons.append("same supplier")
        if (
            total_amount is not None
            and existing.total_amount is not None
            and abs(existing.total_amount - total_amount) < 0.01
        ):
            reasons.append("same total")
        if issue_date and existing.issue_date == issue_date:
            reasons.append("same issue date")

        strong_match = "same invoice number" in reasons
        fuzzy_match = len(reasons) >= 3
        if strong_match or fuzzy_match:
            candidates.append(
                DuplicateCandidate(
                    invoice_id=existing.id,
                    supplier_name=existing.supplier_name,
                    invoice_no=existing.invoice_no,
                    issue_date=existing.issue_date,
                    total_amount=existing.total_amount or 0.0,
                    reason=", ".join(reasons),
                )
            )
    return candidates


def export_csv(db_path: Path | str = DATABASE_PATH) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "status",
            "supplier_name",
            "invoice_no",
            "issue_date",
            "currency",
            "total_amount",
            "confidence",
            "review_reasons",
        ]
    )
    for invoice in list_invoices(db_path):
        writer.writerow(
            [
                invoice.id,
                invoice.status.value,
                invoice.supplier_name,
                invoice.invoice_no,
                invoice.issue_date,
                invoice.currency,
                invoice.total_amount,
                invoice.confidence,
                "; ".join(invoice.review_reasons),
            ]
        )
    return output.getvalue()


def _row_to_invoice(row: sqlite3.Row) -> InvoiceRecord:
    return InvoiceRecord(
        id=row["id"],
        artifact_id=row["artifact_id"],
        supplier_name=row["supplier_name"],
        invoice_no=row["invoice_no"],
        issue_date=row["issue_date"],
        currency=row["currency"],
        total_amount=row["total_amount"],
        status=row["status"],
        confidence=row["confidence"],
        supplier_registration_no=row["supplier_registration_no"],
        sst_or_tax_amount=row["sst_or_tax_amount"],
        subtotal=row["subtotal"],
        due_date=row["due_date"],
        line_items=json.loads(row["line_items_json"] or "[]"),
        duplicate_candidates=json.loads(row["duplicate_candidates_json"] or "[]"),
        missing_fields=json.loads(row["missing_fields_json"] or "[]"),
        review_reasons=json.loads(row["review_reasons_json"] or "[]"),
        raw_text=row["raw_text"],
        trace=[TraceStep(**step) for step in json.loads(row["trace_json"] or "[]")],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        source_type=SourceType(row["artifact_source_type"] or SourceType.TEXT.value),
    )


def _norm(value: str) -> str:
    return " ".join((value or "").casefold().split())
