from datetime import date, datetime

from pydantic import BaseModel

from app.db.models.enums import DocumentType
from app.schemas.common import ORMModel


class DocumentOut(ORMModel):
    id: int
    patient_id: int
    document_type: DocumentType
    original_filename: str
    document_date: date | None
    checksum: str
    classification_confidence: float
    is_duplicate_of: int | None
    summary: str | None = None
    summary_generated_at: datetime | None = None
    created_at: datetime


class DuplicateCheckResult(BaseModel):
    is_duplicate: bool
    existing_document_id: int | None = None
    # "checksum" = byte-identical re-upload; "same_type_and_date" = likely re-scan of
    # the same underlying report. The two warrant different staff handling.
    match_kind: str | None = None


class MissingDocumentsResult(BaseModel):
    department_name: str
    required: list[DocumentType]
    present: list[DocumentType]
    missing: list[DocumentType]


class DocumentCoordinationSummary(BaseModel):
    stored: list[DocumentOut]
    duplicates: list[DuplicateCheckResult]
    missing: MissingDocumentsResult | None = None
