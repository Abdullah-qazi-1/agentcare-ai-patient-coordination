"""Slot availability, booking, rescheduling and cancellation.

Booking is the one place where two concurrent requests can genuinely corrupt state, so
claiming a slot uses a single conditional UPDATE (`... WHERE id = ? AND status =
'open'`) and checks the affected row count. That is atomic on both SQLite and
PostgreSQL, unlike `SELECT ... FOR UPDATE`, which SQLite silently ignores — a check
that appears to lock but doesn't would be worse than no check at all.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    Appointment,
    AppointmentSlot,
    AppointmentStatus,
    Department,
    Doctor,
    SlotStatus,
)
from app.schemas.appointment import AppointmentDetail
from app.schemas.clinical import SlotWithContext
from app.services import audit_service
from app.services.errors import ConflictError, NotFoundError, ValidationError


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; normalize so comparisons never raise."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def get_available_slots(
    db: Session,
    *,
    department_id: int | None = None,
    doctor_id: int | None = None,
    start_from: datetime | None = None,
    start_before: datetime | None = None,
    limit: int = 20,
) -> list[SlotWithContext]:
    """Open, future slots enriched with doctor and department names."""
    start_from = start_from or _utcnow()

    stmt = (
        select(AppointmentSlot, Doctor, Department)
        .join(Doctor, AppointmentSlot.doctor_id == Doctor.id)
        .join(Department, Doctor.department_id == Department.id)
        .where(
            AppointmentSlot.status == SlotStatus.OPEN,
            AppointmentSlot.start_time >= start_from,
            Doctor.active.is_(True),
            Department.active.is_(True),
        )
        .order_by(AppointmentSlot.start_time)
        .limit(limit)
    )
    if department_id is not None:
        stmt = stmt.where(Doctor.department_id == department_id)
    if doctor_id is not None:
        stmt = stmt.where(AppointmentSlot.doctor_id == doctor_id)
    if start_before is not None:
        stmt = stmt.where(AppointmentSlot.start_time <= start_before)

    return [
        SlotWithContext(
            slot_id=slot.id,
            doctor_id=doctor.id,
            doctor_name=doctor.name,
            department_id=dept.id,
            department_name=dept.name,
            start_time=_as_aware(slot.start_time),
            end_time=_as_aware(slot.end_time),
        )
        for slot, doctor, dept in db.execute(stmt).all()
    ]


def _patient_has_conflict(db: Session, patient_id: int, slot: AppointmentSlot) -> Appointment | None:
    """Detect an existing active appointment overlapping this slot's time window."""
    active = (AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED, AppointmentStatus.RESCHEDULED)
    stmt = (
        select(Appointment)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .where(
            Appointment.patient_id == patient_id,
            Appointment.status.in_(active),
            and_(
                AppointmentSlot.start_time < slot.end_time,
                AppointmentSlot.end_time > slot.start_time,
            ),
        )
    )
    return db.execute(stmt).scalars().first()


def _claim_slot(db: Session, slot_id: int) -> bool:
    """Atomically move a slot OPEN -> BOOKED. False means someone else claimed it first."""
    result = db.execute(
        update(AppointmentSlot)
        .where(AppointmentSlot.id == slot_id, AppointmentSlot.status == SlotStatus.OPEN)
        .values(status=SlotStatus.BOOKED)
    )
    return result.rowcount == 1


def _release_slot(db: Session, slot_id: int) -> None:
    db.execute(
        update(AppointmentSlot)
        .where(AppointmentSlot.id == slot_id)
        .values(status=SlotStatus.OPEN)
    )


def book_appointment(
    db: Session,
    *,
    patient_id: int,
    slot_id: int,
    reason: str = "",
    actor_id: int | None = None,
    actor_label: str = "appointment_agent",
    workflow_run_id: int | None = None,
) -> Appointment:
    slot = db.get(AppointmentSlot, slot_id)
    if not slot:
        raise NotFoundError(f"Slot {slot_id} not found.")
    if _as_aware(slot.start_time) < _utcnow():
        raise ValidationError("That slot is in the past and can no longer be booked.")

    conflict = _patient_has_conflict(db, patient_id, slot)
    if conflict:
        raise ConflictError(
            f"You already have an appointment (#{conflict.id}) that overlaps this time."
        )

    if not _claim_slot(db, slot_id):
        raise ConflictError("That slot was just taken. Please choose another.")

    appointment = Appointment(
        patient_id=patient_id,
        doctor_id=slot.doctor_id,
        slot_id=slot.id,
        status=AppointmentStatus.CONFIRMED,
        reason=reason[:500],
    )
    db.add(appointment)
    db.flush()

    audit_service.record(
        db,
        action="appointment_booked",
        entity_type="appointment",
        entity_id=appointment.id,
        actor_id=actor_id,
        actor_label=actor_label,
        metadata={"slot_id": slot_id, "patient_id": patient_id, "workflow_run_id": workflow_run_id},
        commit=False,
    )
    db.commit()
    db.refresh(appointment)
    return appointment


def reschedule_appointment(
    db: Session,
    *,
    appointment_id: int,
    new_slot_id: int,
    actor_id: int | None = None,
    actor_label: str = "appointment_agent",
    workflow_run_id: int | None = None,
) -> Appointment:
    appointment = get_appointment(db, appointment_id)
    if appointment.status == AppointmentStatus.CANCELLED:
        raise ValidationError("A cancelled appointment cannot be rescheduled.")

    new_slot = db.get(AppointmentSlot, new_slot_id)
    if not new_slot:
        raise NotFoundError(f"Slot {new_slot_id} not found.")
    if _as_aware(new_slot.start_time) < _utcnow():
        raise ValidationError("That slot is in the past.")

    old_slot_id = appointment.slot_id
    if old_slot_id == new_slot_id:
        raise ValidationError("The appointment is already in that slot.")

    if not _claim_slot(db, new_slot_id):
        raise ConflictError("That slot was just taken. Please choose another.")

    # Free the old slot only after the new one is secured, so a failed claim never
    # leaves the patient with no appointment at all.
    _release_slot(db, old_slot_id)

    appointment.slot_id = new_slot_id
    appointment.doctor_id = new_slot.doctor_id
    appointment.status = AppointmentStatus.RESCHEDULED
    db.flush()

    audit_service.record(
        db,
        action="appointment_rescheduled",
        entity_type="appointment",
        entity_id=appointment.id,
        actor_id=actor_id,
        actor_label=actor_label,
        metadata={"from_slot": old_slot_id, "to_slot": new_slot_id, "workflow_run_id": workflow_run_id},
        commit=False,
    )
    db.commit()
    db.refresh(appointment)
    return appointment


def cancel_appointment(
    db: Session,
    *,
    appointment_id: int,
    reason: str = "",
    actor_id: int | None = None,
    actor_label: str = "appointment_agent",
    workflow_run_id: int | None = None,
) -> Appointment:
    appointment = get_appointment(db, appointment_id)
    if appointment.status == AppointmentStatus.CANCELLED:
        raise ValidationError("That appointment is already cancelled.")

    appointment.status = AppointmentStatus.CANCELLED
    _release_slot(db, appointment.slot_id)
    db.flush()

    audit_service.record(
        db,
        action="appointment_cancelled",
        entity_type="appointment",
        entity_id=appointment.id,
        actor_id=actor_id,
        actor_label=actor_label,
        metadata={"reason": reason[:200], "workflow_run_id": workflow_run_id},
        commit=False,
    )
    db.commit()
    db.refresh(appointment)
    return appointment


def get_appointment(db: Session, appointment_id: int) -> Appointment:
    appointment = db.get(Appointment, appointment_id)
    if not appointment:
        raise NotFoundError(f"Appointment {appointment_id} not found.")
    return appointment


def get_appointment_detail(db: Session, appointment_id: int) -> AppointmentDetail:
    """Confirmation view built entirely from persisted rows.

    The patient-facing confirmation is rendered from this, never from agent prose, so a
    confirmation can never describe an appointment that wasn't actually written.
    """
    stmt = (
        select(Appointment, AppointmentSlot, Doctor, Department)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .join(Doctor, Appointment.doctor_id == Doctor.id)
        .join(Department, Doctor.department_id == Department.id)
        .where(Appointment.id == appointment_id)
    )
    row = db.execute(stmt).first()
    if not row:
        raise NotFoundError(f"Appointment {appointment_id} not found.")
    appointment, slot, doctor, dept = row
    return AppointmentDetail(
        appointment_id=appointment.id,
        status=appointment.status,
        reason=appointment.reason,
        doctor_name=doctor.name,
        department_name=dept.name,
        start_time=_as_aware(slot.start_time),
        end_time=_as_aware(slot.end_time),
    )


def list_patient_appointments(
    db: Session, patient_id: int, *, include_cancelled: bool = True
) -> list[Appointment]:
    stmt = select(Appointment).where(Appointment.patient_id == patient_id)
    if not include_cancelled:
        stmt = stmt.where(Appointment.status != AppointmentStatus.CANCELLED)
    return list(db.execute(stmt.order_by(Appointment.created_at.desc())).scalars().all())


def create_slot(
    db: Session, *, doctor_id: int, start_time: datetime, end_time: datetime, actor_id: int
) -> AppointmentSlot:
    if end_time <= start_time:
        raise ValidationError("Slot end time must be after its start time.")
    if not db.get(Doctor, doctor_id):
        raise NotFoundError(f"Doctor {doctor_id} not found.")

    overlap = db.execute(
        select(AppointmentSlot).where(
            AppointmentSlot.doctor_id == doctor_id,
            AppointmentSlot.start_time < end_time,
            AppointmentSlot.end_time > start_time,
        )
    ).scalars().first()
    if overlap:
        raise ConflictError(f"This doctor already has an overlapping slot (#{overlap.id}).")

    slot = AppointmentSlot(doctor_id=doctor_id, start_time=start_time, end_time=end_time)
    db.add(slot)
    db.flush()
    audit_service.record(
        db, action="slot_created", entity_type="appointment_slot", entity_id=slot.id,
        actor_id=actor_id, actor_label="staff", metadata={"doctor_id": doctor_id}, commit=False,
    )
    db.commit()
    db.refresh(slot)
    return slot


def parse_timing_preference(text: str | None, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Turn a phrase like 'next week' into a concrete search window.

    Deterministic on purpose: date arithmetic is something code does correctly every
    time and an LLM does *mostly* correctly, and a mis-parsed date is a wasted trip to
    the hospital. The agent extracts the phrase; this function resolves it.
    """
    now = now or _utcnow()
    lowered = (text or "").lower()

    if "tomorrow" in lowered:
        start = now + timedelta(days=1)
        return start.replace(hour=0, minute=0), start.replace(hour=23, minute=59)
    if "today" in lowered:
        return now, now.replace(hour=23, minute=59)
    if "next week" in lowered:
        return now + timedelta(days=7), now + timedelta(days=14)
    if "this week" in lowered:
        return now, now + timedelta(days=7)
    if "next month" in lowered:
        return now + timedelta(days=30), now + timedelta(days=60)
    if "month" in lowered:
        return now, now + timedelta(days=30)
    # Default: the next two weeks.
    return now, now + timedelta(days=14)
