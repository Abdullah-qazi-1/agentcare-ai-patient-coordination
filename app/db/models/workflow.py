from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import WorkflowStatus, WorkflowStep

if TYPE_CHECKING:
    from app.db.models.clinical import Doctor
    from app.db.models.document import PatientDocument
    from app.db.models.user import PatientProfile, User


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # `list_my_runs` / `list_runs` filter on both — the Staff Dashboard's default view.
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient_profiles.id"), index=True)
    raw_request: Mapped[str] = mapped_column(String(2000))
    current_step: Mapped[WorkflowStep] = mapped_column(
        Enum(WorkflowStep, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=WorkflowStep.REGISTRATION,
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(WorkflowStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=WorkflowStatus.IN_PROGRESS,
        index=True,
    )
    # Human-readable mirror of the LangGraph WorkflowState at the last step boundary —
    # this is what the Staff Dashboard renders, independent of LangGraph's own
    # checkpoint storage (see agents/graph.py).
    state_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # Staff/admin marking a doctor as the one who should handle this request — a manual
    # administrative decision, independent of whether a slot has actually been booked
    # (see app/services/workflow_service.py:assign_doctor).
    assigned_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("doctors.id"), nullable=True)
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assignment_note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    patient: Mapped["PatientProfile"] = relationship()
    assigned_doctor: Mapped["Doctor | None"] = relationship()
    assigned_by_user: Mapped["User | None"] = relationship()


class WorkflowRunDocument(Base):
    """Which of a patient's documents they attached to a given request.

    `SubmitRequestIn.document_ids` lets a patient pick a relevant subset of their
    documents at submission time, but that selection used to live only in transient
    LangGraph state (`WorkflowState.document_ids`) — never a real relation. This table
    makes it queryable, so staff reviewing a request see only the reports the patient
    actually attached to *this* request, not their entire document history.
    """

    __tablename__ = "workflow_run_documents"
    __table_args__ = (UniqueConstraint("workflow_run_id", "document_id", name="uq_workflow_run_document"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("patient_documents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    workflow_run: Mapped["WorkflowRun"] = relationship()
    document: Mapped["PatientDocument"] = relationship()
