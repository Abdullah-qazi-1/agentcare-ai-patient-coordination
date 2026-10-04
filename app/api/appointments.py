"""Slot browsing, booking, reschedule/cancel, and reminders — all patient-self scoped
unless the caller is staff."""

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, map_service_error, require_role
from app.db.models import UserRole
from app.db.session import get_db
from app.schemas.appointment import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentDetail,
    AppointmentOut,
    AppointmentReschedule,
)
from app.schemas.auth import CurrentUser
from app.schemas.clinical import SlotCreate, SlotOut, SlotWithContext
from app.schemas.workflow import ReminderOut
from app.services import appointment_service, reminder_service
from app.services.errors import NotFoundError

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.get("/slots", response_model=list[SlotWithContext])
def list_slots(
    department_id: int | None = None,
    doctor_id: int | None = None,
    start_from: datetime | None = None,
    start_before: datetime | None = None,
    limit: int = 20,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return appointment_service.get_available_slots(
        db,
        department_id=department_id,
        doctor_id=doctor_id,
        start_from=start_from,
        start_before=start_before,
        limit=limit,
    )


@router.post("/slots", response_model=SlotOut)
def create_slot(
    payload: SlotCreate,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return appointment_service.create_slot(
        db, doctor_id=payload.doctor_id, start_time=payload.start_time, end_time=payload.end_time,
        actor_id=current.user_id,
    )


@router.get("/reminders/me", response_model=list[ReminderOut])
def list_my_reminders(
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)), db: Session = Depends(get_db)
):
    return reminder_service.list_patient_reminders(db, current.patient_id)


@router.get("/me", response_model=list[AppointmentOut])
def list_my_appointments(
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)), db: Session = Depends(get_db)
):
    return appointment_service.list_patient_appointments(db, current.patient_id)


def _get_owned_appointment(appointment_id: int, current: CurrentUser, db: Session):
    appointment = appointment_service.get_appointment(db, appointment_id)
    if not current.is_staff and appointment.patient_id != current.patient_id:
        raise map_service_error(NotFoundError(f"Appointment {appointment_id} not found."))
    return appointment


@router.get("/{appointment_id}", response_model=AppointmentDetail)
def get_appointment(
    appointment_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    _get_owned_appointment(appointment_id, current, db)
    return appointment_service.get_appointment_detail(db, appointment_id)


@router.post("", response_model=AppointmentOut)
def book_appointment(
    payload: AppointmentCreate,
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)),
    db: Session = Depends(get_db),
):
    return appointment_service.book_appointment(
        db,
        patient_id=current.patient_id,
        slot_id=payload.slot_id,
        reason=payload.reason,
        actor_id=current.user_id,
        actor_label="patient",
    )


@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut)
def reschedule_appointment(
    appointment_id: int,
    payload: AppointmentReschedule,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_owned_appointment(appointment_id, current, db)
    return appointment_service.reschedule_appointment(
        db,
        appointment_id=appointment_id,
        new_slot_id=payload.new_slot_id,
        actor_id=current.user_id,
        actor_label=current.role.value,
    )


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel_appointment(
    appointment_id: int,
    payload: AppointmentCancel,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_owned_appointment(appointment_id, current, db)
    appointment = appointment_service.cancel_appointment(
        db,
        appointment_id=appointment_id,
        reason=payload.reason,
        actor_id=current.user_id,
        actor_label=current.role.value,
    )
    reminder_service.cancel_reminders_for_appointment(db, appointment_id)
    return appointment
