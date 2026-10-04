"""Staff/admin-facing web pages."""

from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db.models import EscalationStatus, UserRole, WorkflowStatus
from app.db.session import get_db
from app.services import (
    appointment_service,
    audit_service,
    department_service,
    escalation_service,
    patient_service,
    workflow_service,
)
from app.services.errors import ServiceError
from app.web.deps import WebSession, render, require_role_web, templates
from app.web.security import verify_csrf

router = APIRouter(prefix="/staff", tags=["web-staff"])

_staff_or_admin = require_role_web(UserRole.STAFF, UserRole.ADMIN)
_admin_only = require_role_web(UserRole.ADMIN)


# ---- Escalations ------------------------------------------------------------------


@router.get("/escalations")
def escalations(
    request: Request, current: WebSession = Depends(_staff_or_admin), db: Session = Depends(get_db)
):
    pending = escalation_service.list_escalations(db, status=EscalationStatus.PENDING)
    return render(request, "staff/escalations.html", current_user=current, escalations=pending)


@router.post("/escalations/{escalation_id}/resolve", dependencies=[Depends(verify_csrf)])
def resolve_escalation(
    escalation_id: int,
    decision: str = Form(...),
    resolution_note: str = Form(""),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    from app.agents.runner import resume_workflow

    try:
        escalation = escalation_service.resolve_escalation(
            db,
            escalation_id=escalation_id,
            decision=EscalationStatus(decision),
            reviewed_by=current.user_id,
            resolution_note=resolution_note,
        )
        approved = EscalationStatus(decision) == EscalationStatus.APPROVED
        resume_workflow(
            db, workflow_run_id=escalation.workflow_run_id, approved=approved, actor_id=current.user_id
        )
        return RedirectResponse(url="/staff/escalations?notice=Escalation+resolved.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/staff/escalations?error={quote(str(exc))}", status_code=303)


# ---- Workflow Runs -----------------------------------------------------------------


def _has_pending_interrupt(db: Session, run_id: int) -> bool:
    """Mirrors `app/api/workflow.py:_has_pending_interrupt` — whether the graph is
    actually suspended at a checkpoint, as distinct from an escalation row existing."""
    from app.agents.graph import build_graph, thread_config

    try:
        snapshot = build_graph(db).get_state(thread_config(run_id))
    except Exception:
        return False
    return any(task.interrupts for task in (getattr(snapshot, "tasks", ()) or ()))


@router.get("/workflows")
def workflow_runs(
    request: Request,
    status: str = "",
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    status_enum = WorkflowStatus(status) if status else None
    runs = workflow_service.list_runs(db, status=status_enum)
    return render(
        request, "staff/workflow_runs.html", current_user=current, runs=runs, selected_status=status
    )


@router.get("/workflows/{run_id}/detail-fragment")
def workflow_run_detail(
    run_id: int,
    request: Request,
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    run = workflow_service.get_run(db, run_id)
    run_escalations = escalation_service.list_for_workflow(db, run_id)
    pending_escalation = next((e for e in run_escalations if e.status == EscalationStatus.PENDING), None)
    # The one piece of real business logic to port carefully: direct approve/reject
    # resume buttons show ONLY for a HITL tool-approval interrupt (reschedule/cancel),
    # never when a safety escalation is what's actually blocking the run — that goes
    # through /staff/escalations instead.
    needs_direct_resume = run.status == WorkflowStatus.AWAITING_REVIEW and pending_escalation is None

    documents = workflow_service.list_relevant_documents(db, run_id)
    lineage = audit_service.list_lineage_for_workflow(db, run_id)
    audit_trail = audit_service.list_for_workflow(db, run_id)
    doctors = department_service.list_doctors(db, active_only=True)

    return templates.TemplateResponse(
        request,
        "staff/_workflow_run_detail.html",
        {
            "run": run,
            "pending_escalation": pending_escalation,
            "needs_direct_resume": needs_direct_resume,
            "documents": documents,
            "lineage": lineage,
            "audit_trail": audit_trail,
            "doctors": doctors,
            "csrf_token": request.cookies.get("csrf_token", ""),
        },
    )


@router.post("/workflows/{run_id}/resume", dependencies=[Depends(verify_csrf)])
def resume_run(
    run_id: int,
    approved: str = Form(...),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    from app.agents.runner import resume_workflow

    workflow_service.get_run(db, run_id)
    pending = escalation_service.get_pending_for_workflow(db, run_id)
    if pending:
        message = quote(f"Run {run_id} has escalation #{pending.id} pending — resolve it from Escalations.")
        return RedirectResponse(url=f"/staff/workflows?error={message}", status_code=303)
    if not _has_pending_interrupt(db, run_id):
        message = quote(f"Run {run_id} is not suspended at a checkpoint; nothing to resume.")
        return RedirectResponse(url=f"/staff/workflows?error={message}", status_code=303)

    resume_workflow(db, workflow_run_id=run_id, approved=(approved == "true"), actor_id=current.user_id)
    return RedirectResponse(url="/staff/workflows?notice=Run+resumed.", status_code=303)


@router.post("/workflows/{run_id}/assign-doctor", dependencies=[Depends(verify_csrf)])
def assign_doctor(
    run_id: int,
    doctor_id: int = Form(...),
    note: str = Form(""),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    try:
        workflow_service.assign_doctor(
            db, workflow_run_id=run_id, doctor_id=doctor_id, actor_id=current.user_id, note=note
        )
        return RedirectResponse(url="/staff/workflows?notice=Doctor+assigned.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/staff/workflows?error={quote(str(exc))}", status_code=303)


# ---- Patients ------------------------------------------------------------------------


@router.get("/patients")
def patients_page(
    request: Request, current: WebSession = Depends(_staff_or_admin), db: Session = Depends(get_db)
):
    rows = patient_service.list_patients(db)
    patients = [
        {
            "id": p.id,
            "name": u.name,
            "email": u.email,
            "phone": p.phone,
            "preferred_language": p.preferred_language,
        }
        for p, u in rows
    ]
    return render(request, "staff/patients.html", current_user=current, patients=patients)


# ---- Manage Directory -----------------------------------------------------------------


@router.get("/directory")
def directory_page(
    request: Request, current: WebSession = Depends(_staff_or_admin), db: Session = Depends(get_db)
):
    departments = department_service.list_departments(db, active_only=False)
    doctors = department_service.list_doctors(db, active_only=False)
    default_start = (datetime.now() + timedelta(days=7)).replace(hour=9, minute=0, second=0, microsecond=0)
    default_end = default_start + timedelta(minutes=30)
    return render(
        request,
        "staff/manage_directory.html",
        current_user=current,
        departments=departments,
        doctors=doctors,
        default_start=default_start.strftime("%Y-%m-%dT%H:%M"),
        default_end=default_end.strftime("%Y-%m-%dT%H:%M"),
    )


@router.post("/directory/departments", dependencies=[Depends(verify_csrf)])
def create_department(
    name: str = Form(...),
    description: str = Form(""),
    routing_keywords: str = Form(""),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    keywords = [kw.strip() for kw in routing_keywords.split(",") if kw.strip()]
    try:
        department_service.create_department(
            db, name=name, description=description, routing_keywords=keywords, actor_id=current.user_id
        )
        return RedirectResponse(url="/staff/directory?notice=Department+created.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/staff/directory?error={quote(str(exc))}", status_code=303)


@router.post("/directory/departments/{department_id}/toggle-active", dependencies=[Depends(verify_csrf)])
def toggle_department_active(
    department_id: int,
    active: str = Form(...),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    department_service.set_department_active(db, department_id, active == "true", actor_id=current.user_id)
    return RedirectResponse(url="/staff/directory?notice=Department+updated.", status_code=303)


@router.post("/directory/doctors", dependencies=[Depends(verify_csrf)])
def create_doctor(
    department_id: int = Form(...),
    name: str = Form(...),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    try:
        department_service.create_doctor(db, department_id=department_id, name=name, actor_id=current.user_id)
        return RedirectResponse(url="/staff/directory?notice=Doctor+added.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/staff/directory?error={quote(str(exc))}", status_code=303)


@router.post("/directory/slots", dependencies=[Depends(verify_csrf)])
def create_slot(
    doctor_id: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    current: WebSession = Depends(_staff_or_admin),
    db: Session = Depends(get_db),
):
    try:
        appointment_service.create_slot(
            db,
            doctor_id=doctor_id,
            start_time=datetime.fromisoformat(start_time),
            end_time=datetime.fromisoformat(end_time),
            actor_id=current.user_id,
        )
        return RedirectResponse(url="/staff/directory?notice=Slot+added.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/staff/directory?error={quote(str(exc))}", status_code=303)


# ---- Create Staff (admin only) -------------------------------------------------------


@router.get("/new-staff")
def new_staff_form(request: Request, current: WebSession = Depends(_admin_only)):
    return render(request, "staff/create_staff.html", current_user=current)


@router.post("/new-staff", dependencies=[Depends(verify_csrf)])
def create_staff(
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    current: WebSession = Depends(_admin_only),
    db: Session = Depends(get_db),
):
    try:
        patient_service.create_staff_user(
            db, name=name, email=email, password=password, role=UserRole(role), actor_id=current.user_id
        )
        return RedirectResponse(url="/staff/new-staff?notice=Account+created.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/staff/new-staff?error={quote(str(exc))}", status_code=303)
