"""Patient-facing web pages (and the staff/admin redirect away from `/`)."""

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.agents.llm import LLMNotConfiguredError
from app.db.models import DocumentType
from app.db.session import get_db
from app.services import (
    appointment_service,
    audit_service,
    department_service,
    document_service,
    reminder_service,
    workflow_service,
)
from app.services.errors import ServiceError
from app.web.deps import WebSession, get_current_user_from_cookie, render, templates
from app.web.security import verify_csrf

router = APIRouter(tags=["web-patient"])


def _redirect_home_if_staff(current: WebSession) -> RedirectResponse | None:
    if current.is_staff:
        return RedirectResponse(url="/staff/escalations", status_code=303)
    return None


# ---- New Request --------------------------------------------------------------


@router.get("/")
def home(request: Request, current: WebSession = Depends(get_current_user_from_cookie)):
    return _redirect_home_if_staff(current) or render(
        request, "patient/new_request.html", current_user=current
    )


@router.post("/", dependencies=[Depends(verify_csrf)])
def submit_request(
    request: Request,
    request_text: str = Form(...),
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    redirect = _redirect_home_if_staff(current)
    if redirect:
        return redirect

    from app.agents.runner import start_workflow

    try:
        result = start_workflow(
            db, patient_id=current.patient_id, request_text=request_text, actor_id=current.user_id
        )
        return render(request, "patient/new_request.html", current_user=current, result=result)
    except LLMNotConfiguredError as exc:
        return render(
            request,
            "patient/new_request.html",
            current_user=current,
            submit_error=str(exc),
            status_code=503,
        )


# ---- My Requests ---------------------------------------------------------------


@router.get("/requests")
def my_requests(
    request: Request,
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    redirect = _redirect_home_if_staff(current)
    if redirect:
        return redirect
    runs = workflow_service.list_runs(db, patient_id=current.patient_id)
    doctors = department_service.list_doctors(db, active_only=False)
    doctor_names = {d.id: d.name for d in doctors}
    return render(
        request, "patient/my_requests.html", current_user=current, runs=runs, doctor_names=doctor_names
    )


@router.get("/requests/{run_id}/detail-fragment")
def request_detail_fragment(
    run_id: int,
    request: Request,
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    """Lazy-loaded accordion content: documents attached to this request plus the
    pipeline's lineage — only fetched once, the first time a row is expanded."""
    run = workflow_service.get_run(db, run_id)
    if not current.is_staff and run.patient_id != current.patient_id:
        raise HTTPException(status_code=404, detail="Not found.")
    documents = workflow_service.list_relevant_documents(db, run_id)
    lineage = audit_service.list_lineage_for_workflow(db, run_id)
    return templates.TemplateResponse(
        request,
        "patient/_request_detail_fragment.html",
        {"documents": documents, "lineage": lineage},
    )


# ---- Find a Doctor --------------------------------------------------------------
#
# Note: the page lives at /find-a-doctor rather than /doctors — the JSON API's
# clinical router already owns the bare GET /doctors path on this same app (it has no
# prefix), so the web page needs a distinct path to avoid being shadowed by it.


@router.get("/find-a-doctor")
def find_a_doctor(
    request: Request,
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    redirect = _redirect_home_if_staff(current)
    if redirect:
        return redirect

    departments = department_service.list_departments(db)
    doctors = department_service.list_doctors(db)
    slots = appointment_service.get_available_slots(db, limit=200)

    slots_by_doctor: dict[int, list] = {}
    for slot in slots:
        slots_by_doctor.setdefault(slot.doctor_id, []).append(slot)

    department_by_id = {d.id: d for d in departments}
    appointments = appointment_service.list_patient_appointments(db, current.patient_id)
    appointment_details = [
        appointment_service.get_appointment_detail(db, a.id) for a in appointments
    ]

    return render(
        request,
        "patient/appointments.html",
        current_user=current,
        departments=departments,
        doctors=doctors,
        slots_by_doctor=slots_by_doctor,
        department_by_id=department_by_id,
        appointment_details=appointment_details,
    )


@router.post("/appointments/book", dependencies=[Depends(verify_csrf)])
def book_appointment(
    slot_id: int = Form(...),
    reason: str = Form(""),
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    try:
        appointment_service.book_appointment(
            db,
            patient_id=current.patient_id,
            slot_id=slot_id,
            reason=reason,
            actor_id=current.user_id,
            actor_label="patient",
        )
        return RedirectResponse(url="/find-a-doctor?notice=Appointment+booked.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/find-a-doctor?error={quote(str(exc))}", status_code=303)


# Cancellation takes the appointment id as a form field rather than a path segment
# (`/appointments/cancel`, not `/appointments/{id}/cancel`) — the JSON API's
# appointments router already registers that exact path+method shape, so reusing it
# here would be shadowed by that handler instead of ever reaching this one.
@router.post("/appointments/cancel", dependencies=[Depends(verify_csrf)])
def cancel_appointment(
    appointment_id: int = Form(...),
    reason: str = Form(""),
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    appointment = appointment_service.get_appointment(db, appointment_id)
    if not current.is_staff and appointment.patient_id != current.patient_id:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        appointment_service.cancel_appointment(
            db,
            appointment_id=appointment_id,
            reason=reason,
            actor_id=current.user_id,
            actor_label=current.role.value,
        )
        reminder_service.cancel_reminders_for_appointment(db, appointment_id)
        return RedirectResponse(url="/find-a-doctor?notice=Appointment+cancelled.", status_code=303)
    except ServiceError as exc:
        return RedirectResponse(url=f"/find-a-doctor?error={quote(str(exc))}", status_code=303)


# ---- Documents -------------------------------------------------------------------


@router.get("/documents")
def documents_page(
    request: Request,
    department_id: int | None = None,
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    redirect = _redirect_home_if_staff(current)
    if redirect:
        return redirect

    documents = document_service.list_patient_documents(db, current.patient_id)
    departments = department_service.list_departments(db)
    missing = None
    if department_id:
        missing = document_service.check_missing_documents(
            db, patient_id=current.patient_id, department_id=department_id
        )
    return render(
        request,
        "patient/documents.html",
        current_user=current,
        documents=documents,
        departments=departments,
        missing=missing,
        selected_department_id=department_id,
        max_bytes=document_service.max_file_bytes(),
    )


# Both routes below use a path distinct from the JSON API's `/documents/upload` and
# `/documents/{id}/summarize` (same methods, same shape) — reusing those exact paths
# would be shadowed by the JSON handlers, which expect a Bearer token and never see
# these cookie-authenticated form posts.
@router.post("/documents/upload-form", dependencies=[Depends(verify_csrf)])
async def upload_document(
    file: UploadFile = File(...),
    document_type: str = Form(""),
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    content = await file.read()
    limit = document_service.max_file_bytes()
    if len(content) > limit:
        message = quote(f"File exceeds the {limit // (1024 * 1024)} MB limit.")
        return RedirectResponse(url=f"/documents?error={message}", status_code=303)

    resolved_type = DocumentType(document_type) if document_type else None
    confidence = 1.0 if resolved_type else 0.0
    if resolved_type is None:
        resolved_type, confidence = document_service.classify_by_filename(file.filename or "")
        resolved_type = resolved_type or DocumentType.OTHER

    document_date = document_service.extract_date_from_filename(file.filename or "")
    document_service.store_document(
        db,
        patient_id=current.patient_id,
        filename=file.filename or "upload",
        content=content,
        document_type=resolved_type,
        confidence=confidence,
        document_date=document_date,
        actor_id=current.user_id,
        actor_label="patient",
    )
    return RedirectResponse(url="/documents?notice=Document+uploaded.", status_code=303)


@router.post("/documents/summarize", dependencies=[Depends(verify_csrf)])
def summarize_document(
    document_id: int = Form(...),
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    document = document_service.get_document(db, document_id)
    if not current.is_staff and document.patient_id != current.patient_id:
        raise HTTPException(status_code=404, detail="Not found.")
    try:
        document_service.summarize_document(
            db, document_id=document_id, actor_id=current.user_id, actor_label=current.role.value
        )
        return RedirectResponse(url="/documents?notice=Summary+generated.", status_code=303)
    except LLMNotConfiguredError as exc:
        return RedirectResponse(url=f"/documents?error={quote(str(exc))}", status_code=303)


# ---- Reminders --------------------------------------------------------------------


@router.get("/reminders")
def reminders_page(
    request: Request,
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    redirect = _redirect_home_if_staff(current)
    if redirect:
        return redirect
    reminders = reminder_service.list_patient_reminders(db, current.patient_id)
    return render(request, "patient/reminders.html", current_user=current, reminders=reminders)
