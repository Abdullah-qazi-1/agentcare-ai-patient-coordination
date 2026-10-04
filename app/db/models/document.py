from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import DocumentType

if TYPE_CHECKING:
    from app.db.models.user import PatientProfile


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PatientDocument(Base):
    __tablename__ = "patient_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient_profiles.id"))
    document_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=DocumentType.OTHER,
    )
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_reference: Mapped[str] = mapped_column(String(500))
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    classification_confidence: Mapped[float] = mapped_column(default=0.0)
    is_duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("patient_documents.id"), nullable=True)
    # Administrative-only, LLM-generated on demand (see document_service.summarize_document) —
    # not backfilled at upload, since not every upload needs one and generating one always
    # costs a model call.
    summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    patient: Mapped["PatientProfile"] = relationship()
