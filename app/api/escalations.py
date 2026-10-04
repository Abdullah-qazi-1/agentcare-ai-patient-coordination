"""Safety-escalation review, staff/admin only.

A safety escalation is the one HITL mechanism that carries a persisted `Escalation`
row (see `app/agents/nodes/safety_gate.py`). Resolving one is two steps,
both required: record who decided and what (`escalation_service.resolve_escalation`,
which nothing in the codebase called before this route existed), then hand control
back to the graph (`resume_workflow`) so it actually continues from its checkpoint.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_role
from app.db.models import EscalationStatus, UserRole
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.workflow import EscalationOut, EscalationResolve, WorkflowRunResult
from app.services import escalation_service

router = APIRouter(
    prefix="/escalations",
    tags=["escalations"],
    dependencies=[Depends(require_role(UserRole.STAFF, UserRole.ADMIN))],
)


@router.get("", response_model=list[EscalationOut])
def list_escalations(status: EscalationStatus | None = None, db: Session = Depends(get_db)):
    return escalation_service.list_escalations(db, status=status)


@router.get("/workflow/{run_id}", response_model=list[EscalationOut])
def list_for_workflow(run_id: int, db: Session = Depends(get_db)):
    return escalation_service.list_for_workflow(db, run_id)


@router.get("/{escalation_id}", response_model=EscalationOut)
def get_escalation(escalation_id: int, db: Session = Depends(get_db)):
    return escalation_service.get_escalation(db, escalation_id)


@router.post("/{escalation_id}/resolve", response_model=WorkflowRunResult)
def resolve_escalation(
    escalation_id: int,
    payload: EscalationResolve,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.agents.runner import resume_workflow

    escalation = escalation_service.resolve_escalation(
        db,
        escalation_id=escalation_id,
        decision=payload.decision,
        reviewed_by=current.user_id,
        resolution_note=payload.resolution_note,
    )

    approved = payload.decision == EscalationStatus.APPROVED
    return resume_workflow(
        db, workflow_run_id=escalation.workflow_run_id, approved=approved, actor_id=current.user_id
    )
