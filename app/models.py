from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SourceType(StrEnum):
    PDF = "pdf"
    IMAGE = "image"
    TEXT = "text"


class InvoiceStatus(StrEnum):
    REGISTERED = "registered"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class LineItem(BaseModel):
    description: str = ""
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class DuplicateCandidate(BaseModel):
    invoice_id: int
    supplier_name: str
    invoice_no: str
    issue_date: str
    total_amount: float
    reason: str


class TraceStep(BaseModel):
    agent: str
    action: str
    status: Literal["ok", "warning", "error"] = "ok"
    detail: str
    confidence: float | None = None


class InvoiceDraft(BaseModel):
    supplier_name: str = ""
    invoice_no: str = ""
    issue_date: str = ""
    currency: str = "MYR"
    total_amount: float | None = None
    source_type: SourceType = SourceType.TEXT
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: InvoiceStatus = InvoiceStatus.NEEDS_REVIEW
    supplier_registration_no: str = ""
    sst_or_tax_amount: float | None = None
    subtotal: float | None = None
    due_date: str = ""
    line_items: list[LineItem] = Field(default_factory=list)
    duplicate_candidates: list[DuplicateCandidate] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    raw_text: str = ""

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> str:
        if not value:
            return "MYR"
        value = str(value).upper().strip()
        if value in {"RM", "RINGGIT", "MALAYSIAN RINGGIT"}:
            return "MYR"
        return value

    def enforce_review_rules(self) -> "InvoiceDraft":
        missing = []
        if not self.supplier_name:
            missing.append("supplier_name")
        if not self.invoice_no:
            missing.append("invoice_no")
        if not self.issue_date:
            missing.append("issue_date")
        if self.total_amount is None:
            missing.append("total_amount")

        self.missing_fields = sorted(set([*self.missing_fields, *missing]))

        if self.status != InvoiceStatus.REJECTED:
            if self.missing_fields:
                self.status = InvoiceStatus.NEEDS_REVIEW
                reason = "Missing required fields: " + ", ".join(self.missing_fields)
                if reason not in self.review_reasons:
                    self.review_reasons.append(reason)
            elif self.duplicate_candidates:
                self.status = InvoiceStatus.NEEDS_REVIEW
                reason = "Possible duplicate invoice found"
                if reason not in self.review_reasons:
                    self.review_reasons.append(reason)
            elif self.confidence < 0.72:
                self.status = InvoiceStatus.NEEDS_REVIEW
                reason = "Extraction confidence is below the auto-register threshold"
                if reason not in self.review_reasons:
                    self.review_reasons.append(reason)
            else:
                self.status = InvoiceStatus.REGISTERED
        return self


class RawArtifact(BaseModel):
    id: int
    source_type: SourceType
    filename: str
    content_hash: str
    created_at: datetime


class InvoiceRecord(InvoiceDraft):
    id: int
    artifact_id: int
    created_at: datetime
    updated_at: datetime
    trace: list[TraceStep] = Field(default_factory=list)


class ExistingInvoice(BaseModel):
    id: int
    supplier_name: str
    invoice_no: str
    issue_date: str
    total_amount: float | None
