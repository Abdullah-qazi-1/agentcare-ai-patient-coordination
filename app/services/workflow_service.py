"""Workflow run lifecycle and state persistence.

`state_snapshot` is a human-readable mirror of the LangGraph state, written at each
step boundary. The Staff Dashboard reads this rather than decoding LangGraph's internal
checkpoint blob, so the audit view never depends on framework internals.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Doctor,
    PatientDocument,
    WorkflowRun,
    WorkflowRunDocument,
    WorkflowStatus,
    WorkflowStep,
)
from app.services import audit_service
from app.services.errors import NotFoundError, ValidationError


def create_run(
    db: Session,
    *,
    patient_id: int,
    raw_request: str,
    actor_id: int | None = None,
    document_ids: list[int] = (),
) -> WorkflowRun:
    run = WorkflowRun(
        patient_id=patient_id,
        raw_request=raw_request[:2000],
        current_step=WorkflowStep.REGISTRATION,
        status=WorkflowStatus.IN_PROGRESS,
        state_snapshot={},
    )
    db.add(run)
    db.flush()

    attached_ids = _attach_relevant_documents(
        db, run_id=run.id, patient_id=patient_id, document_ids=document_ids
    )

    audit_service.record(
        db,
        action="workflow_started",
        entity_type="workflow_run",
        entity_id=run.id,
        actor_id=actor_id,
        actor_label="patient",
        metadata={
            "patient_id": patient_id,
            "request_length": len(raw_request),
            "attached_document_ids": attached_ids,
        },
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return run


def _attach_relevant_documents(
    db: Session, *, run_id: int, patient_id: int, document_ids: list[int]
) -> list[int]:
    """Persist the patient-chosen subset of their documents as relevant to this run.

    Only documents actually owned by this patient are attached — a stray or
    other-patient id is silently skipped rather than trusted, the same
    defense-in-depth ownership check already applied in
    `app/agents/tools/document_tools.py`.
    """
    if not document_ids:
        return []

    owned_ids = set(
        db.execute(
            select(PatientDocument.id).where(
                PatientDocument.id.in_(document_ids), PatientDocument.patient_id == patient_id
            )
        ).scalars().all()
    )
    attached: list[int] = []
    for document_id in dict.fromkeys(document_ids):  # de-duplicate, preserve order
        if document_id not in owned_ids:
            continue
        db.add(WorkflowRunDocument(workflow_run_id=run_id, document_id=document_id))
        attached.append(document_id)
    if attached:
        db.flush()
    return attached


def list_relevant_documents(db: Session, workflow_run_id: int) -> list[PatientDocument]:
    """The documents the patient actually attached to this request — not their whole
    document history. See `_attach_relevant_documents`."""
    get_run(db, workflow_run_id)  # 404s cleanly if the run doesn't exist
    return list(
        db.execute(
            select(PatientDocument)
            .join(WorkflowRunDocument, WorkflowRunDocument.document_id == PatientDocument.id)
            .where(WorkflowRunDocument.workflow_run_id == workflow_run_id)
            .order_by(PatientDocument.created_at.desc())
        ).scalars().all()
    )


def assign_doctor(
    db: Session,
    *,
    workflow_run_id: int,
    doctor_id: int,
    actor_id: int,
    note: str = "",
) -> WorkflowRun:
    """Staff/admin mark a doctor as the one who should handle this request.

    Marking-only: it does not book a slot. Booking stays on the existing
    patient/agent-driven path (`app/services/appointment_service.py`) — this is a
    administrative routing decision, recorded so staff and the patient can both see
    who has been assigned, independent of whether an appointment has been booked yet.
    """
    run = get_run(db, workflow_run_id)

    doctor = db.get(Doctor, doctor_id)
    if not doctor:
        raise NotFoundError(f"Doctor {doctor_id} not found.")
    if not doctor.active:
        raise ValidationError(f"Doctor {doctor_id} is not active.")

    run.assigned_doctor_id = doctor.id
    run.assigned_by = actor_id
    run.assigned_at = datetime.now(UTC)
    run.assignment_note = note[:500]
    db.flush()

    audit_service.record(
        db,
        action="doctor_assigned",
        entity_type="workflow_run",
        entity_id=run.id,
        actor_id=actor_id,
        actor_label="staff",
        metadata={"doctor_id": doctor.id, "doctor_name": doctor.name, "note": note[:500]},
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return run


def get_run(db: Session, workflow_run_id: int) -> WorkflowRun:
    run = db.get(WorkflowRun, workflow_run_id)
    if not run:
        raise NotFoundError(f"Workflow run {workflow_run_id} not found.")
    return run


def save_state(
    db: Session,
    *,
    workflow_run_id: int,
    step: WorkflowStep | None = None,
    status: WorkflowStatus | None = None,
    state_patch: dict | None = None,
    actor_label: str = "coordinator_agent",
) -> WorkflowRun:
    """Advance a run and merge new facts into its snapshot.

    The snapshot is merged rather than replaced so a later step can't erase what an
    earlier one established.
    """
    run = get_run(db, workflow_run_id)

    if step is not None:
        run.current_step = step
    if status is not None:
        run.status = status
    if state_patch:
        merged = dict(run.state_snapshot or {})
        merged.update({k: v for k, v in state_patch.items() if v is not None})
        run.state_snapshot = merged

    db.flush()
    audit_service.record(
        db,
        action="workflow_state_saved",
        entity_type="workflow_run",
        entity_id=run.id,
        actor_label=actor_label,
        metadata={
            "step": run.current_step.value,
            "status": run.status.value,
            "keys": sorted((state_patch or {}).keys()),
        },
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return run


def complete_run(db: Session, *, workflow_run_id: int, summary: str) -> WorkflowRun:
    run = get_run(db, workflow_run_id)
    run.status = WorkflowStatus.COMPLETED
    run.current_step = WorkflowStep.CONFIRMATION
    snapshot = dict(run.state_snapshot or {})
    snapshot["summary"] = summary[:2000]
    run.state_snapshot = snapshot
    db.flush()
    audit_service.record(
        db,
        action="workflow_completed",
        entity_type="workflow_run",
        entity_id=run.id,
        actor_label="coordinator_agent",
        metadata={"summary": summary[:500]},
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return run


def fail_run(db: Session, *, workflow_run_id: int, error: str) -> WorkflowRun:
    """Mark a run failed. Reached only after retries and escalation are exhausted."""
    run = get_run(db, workflow_run_id)
    run.status = WorkflowStatus.FAILED
    snapshot = dict(run.state_snapshot or {})
    snapshot["error"] = error[:1000]
    run.state_snapshot = snapshot
    db.flush()
    audit_service.record(
        db,
        action="workflow_failed",
        entity_type="workflow_run",
        entity_id=run.id,
        actor_label="system",
        metadata={"error": error[:500]},
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return run


def list_runs(
    db: Session,
    *,
    patient_id: int | None = None,
    status: WorkflowStatus | None = None,
    limit: int = 100,
) -> list[WorkflowRun]:
    stmt = select(WorkflowRun).order_by(WorkflowRun.created_at.desc()).limit(limit)
    if patient_id is not None:
        stmt = stmt.where(WorkflowRun.patient_id == patient_id)
    if status is not None:
        stmt = stmt.where(WorkflowRun.status == status)
    return list(db.execute(stmt).scalars().all())
