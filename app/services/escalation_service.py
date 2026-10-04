"""Human escalation and approval records.

An escalation is the durable half of human-in-the-loop: the LangGraph checkpoint holds
the suspended execution, while this table holds the reviewable record staff act on. The
two are joined by `workflow_run_id`, which is also the graph's `thread_id`.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Escalation,
    EscalationReason,
    EscalationStatus,
    WorkflowRun,
    WorkflowStatus,
)
from app.services import audit_service
from app.services.errors import NotFoundError, ValidationError


def create_escalation(
    db: Session,
    *,
    workflow_run_id: int,
    reason: EscalationReason,
    detail: str = "",
    actor_label: str = "safety_agent",
) -> Escalation:
    """Raise an escalation and park the workflow awaiting human review.

    Re-raising the same reason on the same run returns the existing pending record
    rather than creating a duplicate — retries must not flood the staff queue.
    """
    run = db.get(WorkflowRun, workflow_run_id)
    if not run:
        raise NotFoundError(f"Workflow run {workflow_run_id} not found.")

    existing = db.execute(
        select(Escalation).where(
            Escalation.workflow_run_id == workflow_run_id,
            Escalation.reason == reason,
            Escalation.status == EscalationStatus.PENDING,
        )
    ).scalars().first()
    if existing:
        return existing

    escalation = Escalation(
        workflow_run_id=workflow_run_id,
        reason=reason,
        detail=detail[:1000],
        status=EscalationStatus.PENDING,
    )
    db.add(escalation)
    run.status = WorkflowStatus.AWAITING_REVIEW
    db.flush()

    audit_service.record(
        db,
        action="escalation_created",
        entity_type="escalation",
        entity_id=escalation.id,
        actor_label=actor_label,
        metadata={"workflow_run_id": workflow_run_id, "reason": reason.value, "detail": detail[:500]},
        commit=False,
    )
    db.commit()
    db.refresh(escalation)
    return escalation


def resolve_escalation(
    db: Session,
    *,
    escalation_id: int,
    decision: EscalationStatus,
    reviewed_by: int,
    resolution_note: str = "",
) -> Escalation:
    """Record a staff decision. `reviewed_by` comes from the authenticated user only."""
    if decision not in (EscalationStatus.APPROVED, EscalationStatus.REJECTED, EscalationStatus.RESOLVED):
        raise ValidationError("Decision must be approved, rejected, or resolved.")

    escalation = get_escalation(db, escalation_id)
    if escalation.status != EscalationStatus.PENDING:
        raise ValidationError(f"That escalation was already {escalation.status.value}.")

    escalation.status = decision
    escalation.reviewed_by = reviewed_by
    escalation.resolution_note = resolution_note[:1000]
    escalation.resolved_at = datetime.now(UTC)

    run = db.get(WorkflowRun, escalation.workflow_run_id)
    if run:
        # Approval hands control back to the graph, which resumes from its checkpoint.
        # Rejection ends the run here; nothing further executes.
        run.status = (
            WorkflowStatus.IN_PROGRESS if decision == EscalationStatus.APPROVED else WorkflowStatus.TERMINATED
        )
    db.flush()

    audit_service.record(
        db,
        action="escalation_resolved",
        entity_type="escalation",
        entity_id=escalation.id,
        actor_id=reviewed_by,
        actor_label="staff",
        metadata={
            "decision": decision.value,
            "workflow_run_id": escalation.workflow_run_id,
            "note": resolution_note[:500],
        },
        commit=False,
    )
    db.commit()
    db.refresh(escalation)
    return escalation


def get_escalation(db: Session, escalation_id: int) -> Escalation:
    escalation = db.get(Escalation, escalation_id)
    if not escalation:
        raise NotFoundError(f"Escalation {escalation_id} not found.")
    return escalation


def list_escalations(
    db: Session, *, status: EscalationStatus | None = None, limit: int = 100
) -> list[Escalation]:
    stmt = select(Escalation).order_by(Escalation.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Escalation.status == status)
    return list(db.execute(stmt).scalars().all())


def list_for_workflow(db: Session, workflow_run_id: int) -> list[Escalation]:
    return list(
        db.execute(
            select(Escalation)
            .where(Escalation.workflow_run_id == workflow_run_id)
            .order_by(Escalation.created_at.asc())
        ).scalars().all()
    )


def get_pending_for_workflow(db: Session, workflow_run_id: int) -> Escalation | None:
    return db.execute(
        select(Escalation).where(
            Escalation.workflow_run_id == workflow_run_id,
            Escalation.status == EscalationStatus.PENDING,
        )
    ).scalars().first()
