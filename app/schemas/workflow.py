from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models.enums import (
    EscalationReason,
    EscalationStatus,
    ReminderStatus,
    ReminderType,
    WorkflowStatus,
    WorkflowStep,
)
from app.schemas.common import ORMModel


class WorkflowRunOut(ORMModel):
    id: int
    patient_id: int
    raw_request: str
    current_step: WorkflowStep
    status: WorkflowStatus
    state_snapshot: dict
    assigned_doctor_id: int | None = None
    assigned_by: int | None = None
    assigned_at: datetime | None = None
    assignment_note: str = ""
    created_at: datetime
    updated_at: datetime


class SubmitRequestIn(BaseModel):
    """A patient's free-text administrative request — the entry point to the agent graph."""

    request_text: str = Field(min_length=3, max_length=2000)
    document_ids: list[int] = Field(default_factory=list)


class DoctorAssign(BaseModel):
    """Staff/admin marking a doctor as the one who should handle a request — see
    `app/services/workflow_service.py:assign_doctor`. Marking-only: it does not book
    a slot."""

    doctor_id: int
    note: str = Field(default="", max_length=500)


class WorkflowRunResult(BaseModel):
    workflow_run_id: int
    status: WorkflowStatus
    current_step: WorkflowStep
    # Patient-facing summary, assembled from persisted rows by the Coordinator.
    summary: str
    appointment_id: int | None = None
    escalation_id: int | None = None
    awaiting_human_review: bool = False


class EscalationOut(ORMModel):
    id: int
    workflow_run_id: int
    reason: EscalationReason
    detail: str
    status: EscalationStatus
    reviewed_by: int | None
    resolution_note: str
    created_at: datetime
    resolved_at: datetime | None


class EscalationResolve(BaseModel):
    decision: EscalationStatus = Field(
        description="APPROVED resumes the paused workflow; REJECTED terminates it."
    )
    resolution_note: str = Field(default="", max_length=1000)


class ReminderOut(ORMModel):
    id: int
    patient_id: int
    appointment_id: int | None
    reminder_type: ReminderType
    scheduled_at: datetime
    status: ReminderStatus
    created_at: datetime


class AuditEventOut(ORMModel):
    id: int
    actor_id: int | None
    actor_label: str
    action: str
    entity_type: str
    entity_id: int | None
    event_metadata: dict
    created_at: datetime


class LineageEntry(BaseModel):
    """One data source the agent pipeline actually consulted for this run — which
    doctor availability was checked, which document was matched, which department
    keyword table hit — built from the same AuditEvent rows as the raw audit trail,
    filtered and worded for a patient rather than an engineer."""

    step: str = Field(description="Human-readable stage, e.g. 'Checked doctor availability'.")
    agent: str
    tool: str
    detail: str = Field(
        description="What the tool actually reported back, or its arguments if no result was captured."
    )
    created_at: datetime
