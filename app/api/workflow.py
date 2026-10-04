"""The agent workflow: submit a request, watch it run, and (for staff) resume a run
suspended on a HITL tool-approval interrupt.

Safety-escalation resumes go through `/escalations/{id}/resolve` instead — see that
router's docstring for why the two paths differ.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, map_service_error, require_role
from app.db.models import UserRole, WorkflowStatus
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.document import DocumentOut
from app.schemas.workflow import (
    DoctorAssign,
    LineageEntry,
    SubmitRequestIn,
    WorkflowRunOut,
    WorkflowRunResult,
)
from app.services import audit_service, escalation_service, workflow_service
from app.services.errors import NotFoundError

router = APIRouter(prefix="/workflow", tags=["workflow"])


class ResumeRequest(BaseModel):
    approved: bool


@router.post("/submit", response_model=WorkflowRunResult)
def submit_request(
    payload: SubmitRequestIn,
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)),
    db: Session = Depends(get_db),
):
    from app.agents.llm import LLMNotConfiguredError
    from app.agents.runner import start_workflow

    try:
        return start_workflow(
            db,
            patient_id=current.patient_id,
            request_text=payload.request_text,
            document_ids=payload.document_ids,
            actor_id=current.user_id,
        )
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/me", response_model=list[WorkflowRunOut])
def list_my_runs(
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)), db: Session = Depends(get_db)
):
    return workflow_service.list_runs(db, patient_id=current.patient_id)


@router.get("", response_model=list[WorkflowRunOut])
def list_runs(
    status: WorkflowStatus | None = None,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return workflow_service.list_runs(db, status=status)


@router.get("/{run_id}", response_model=WorkflowRunOut)
def get_run(run_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    run = workflow_service.get_run(db, run_id)
    if not current.is_staff and run.patient_id != current.patient_id:
        raise map_service_error(NotFoundError(f"Workflow run {run_id} not found."))
    return run


@router.get("/{run_id}/lineage", response_model=list[LineageEntry])
def get_run_lineage(
    run_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Which doctor availability, documents, and department matches this run actually
    used — the patient's own request gets the same ownership check as `GET /{run_id}`."""
    run = workflow_service.get_run(db, run_id)
    if not current.is_staff and run.patient_id != current.patient_id:
        raise map_service_error(NotFoundError(f"Workflow run {run_id} not found."))
    return audit_service.list_lineage_for_workflow(db, run_id)


@router.get("/{run_id}/documents", response_model=list[DocumentOut])
def get_run_documents(
    run_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Only the documents the patient actually attached to this request — see
    `app/services/workflow_service.py:list_relevant_documents`."""
    run = workflow_service.get_run(db, run_id)
    if not current.is_staff and run.patient_id != current.patient_id:
        raise map_service_error(NotFoundError(f"Workflow run {run_id} not found."))
    return workflow_service.list_relevant_documents(db, run_id)


@router.post("/{run_id}/assign-doctor", response_model=WorkflowRunOut)
def assign_doctor(
    run_id: int,
    payload: DoctorAssign,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return workflow_service.assign_doctor(
        db,
        workflow_run_id=run_id,
        doctor_id=payload.doctor_id,
        actor_id=current.user_id,
        note=payload.note,
    )


def _has_pending_interrupt(db: Session, run_id: int) -> bool:
    """Whether the graph is actually suspended at a checkpoint for this run.

    Mirrors `app/cli.py:_has_pending_interrupt` — an escalation row and a suspended
    graph are two different things (see `app/services/escalation_service.py`), so a
    decision without a real checkpoint to resume would be a silent no-op.
    """
    from app.agents.graph import build_graph, thread_config

    try:
        snapshot = build_graph(db).get_state(thread_config(run_id))
    except Exception:
        return False
    return any(task.interrupts for task in (getattr(snapshot, "tasks", ()) or ()))


@router.post("/{run_id}/resume", response_model=WorkflowRunResult)
def resume_run(
    run_id: int,
    payload: ResumeRequest,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    from app.agents.runner import resume_workflow

    workflow_service.get_run(db, run_id)

    pending = escalation_service.get_pending_for_workflow(db, run_id)
    if pending:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} has escalation #{pending.id} pending — resolve it via "
                f"/escalations/{pending.id}/resolve instead."
            ),
        )
    if not _has_pending_interrupt(db, run_id):
        raise HTTPException(
            status_code=409, detail=f"Run {run_id} is not suspended at a checkpoint; nothing to resume."
        )

    return resume_workflow(db, workflow_run_id=run_id, approved=payload.approved, actor_id=current.user_id)
