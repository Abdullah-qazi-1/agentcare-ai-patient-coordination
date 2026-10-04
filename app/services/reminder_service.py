"""Reminders, follow-up tasks and notification dispatch.

`dispatch_notification` writes a Notification row and logs it rather than calling an
email/SMS provider. That is the integration seam: swapping in a real provider means
changing this one function, and the persisted Notification record is what proves a
dispatch actually happened.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    Appointment,
    AppointmentSlot,
    Notification,
    Reminder,
    ReminderStatus,
    ReminderType,
)
from app.services import audit_service
from app.services.errors import NotFoundError, ValidationError

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def create_reminder(
    db: Session,
    *,
    patient_id: int,
    reminder_type: ReminderType,
    scheduled_at: datetime,
    appointment_id: int | None = None,
    actor_id: int | None = None,
    actor_label: str = "followup_agent",
    workflow_run_id: int | None = None,
) -> Reminder:
    if appointment_id is not None and not db.get(Appointment, appointment_id):
        raise NotFoundError(f"Appointment {appointment_id} not found.")

    reminder = Reminder(
        patient_id=patient_id,
        appointment_id=appointment_id,
        reminder_type=reminder_type,
        scheduled_at=scheduled_at,
    )
    db.add(reminder)
    db.flush()

    audit_service.record(
        db,
        action="reminder_created",
        entity_type="reminder",
        entity_id=reminder.id,
        actor_id=actor_id,
        actor_label=actor_label,
        metadata={
            "reminder_type": reminder_type.value,
            "appointment_id": appointment_id,
            "scheduled_at": scheduled_at.isoformat(),
            "workflow_run_id": workflow_run_id,
        },
        commit=False,
    )
    db.commit()
    db.refresh(reminder)
    return reminder


def create_appointment_reminder(
    db: Session,
    *,
    patient_id: int,
    appointment_id: int,
    days_before: int = 1,
    actor_label: str = "followup_agent",
    workflow_run_id: int | None = None,
) -> Reminder:
    """Schedule a reminder relative to the appointment's actual persisted start time."""
    row = db.execute(
        select(Appointment, AppointmentSlot)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .where(Appointment.id == appointment_id)
    ).first()
    if not row:
        raise NotFoundError(f"Appointment {appointment_id} not found.")
    _, slot = row

    scheduled_at = _as_aware(slot.start_time) - timedelta(days=max(0, days_before))
    # A reminder in the past would never fire; send it shortly from now instead.
    if scheduled_at < _utcnow():
        scheduled_at = _utcnow() + timedelta(minutes=5)

    return create_reminder(
        db,
        patient_id=patient_id,
        reminder_type=ReminderType.APPOINTMENT_REMINDER,
        scheduled_at=scheduled_at,
        appointment_id=appointment_id,
        actor_label=actor_label,
        workflow_run_id=workflow_run_id,
    )


def schedule_followup_task(
    db: Session,
    *,
    patient_id: int,
    days_after: int,
    appointment_id: int | None = None,
    actor_label: str = "followup_agent",
    workflow_run_id: int | None = None,
) -> Reminder:
    if days_after <= 0:
        raise ValidationError("A follow-up must be scheduled at least one day out.")
    return create_reminder(
        db,
        patient_id=patient_id,
        reminder_type=ReminderType.FOLLOW_UP_VISIT,
        scheduled_at=_utcnow() + timedelta(days=days_after),
        appointment_id=appointment_id,
        actor_label=actor_label,
        workflow_run_id=workflow_run_id,
    )


def dispatch_notification(
    db: Session, *, reminder_id: int, message: str, channel: str = "in_app"
) -> Notification:
    """Deliver a reminder and mark it sent. Idempotent per reminder."""
    reminder = db.get(Reminder, reminder_id)
    if not reminder:
        raise NotFoundError(f"Reminder {reminder_id} not found.")
    if reminder.status == ReminderStatus.SENT:
        raise ValidationError("That reminder has already been sent.")

    notification = Notification(reminder_id=reminder_id, channel=channel, message=message[:1000])
    db.add(notification)
    reminder.status = ReminderStatus.SENT
    db.flush()

    audit_service.record(
        db,
        action="notification_dispatched",
        entity_type="reminder",
        entity_id=reminder_id,
        actor_label="followup_agent",
        metadata={"channel": channel, "patient_id": reminder.patient_id},
        commit=False,
    )
    db.commit()
    db.refresh(notification)
    logger.info("notification_dispatched", reminder_id=reminder_id, channel=channel)
    return notification


def list_patient_reminders(db: Session, patient_id: int) -> list[Reminder]:
    return list(
        db.execute(
            select(Reminder)
            .where(Reminder.patient_id == patient_id)
            .order_by(Reminder.scheduled_at.asc())
        ).scalars().all()
    )


def list_due_reminders(db: Session, *, now: datetime | None = None, limit: int = 50) -> list[Reminder]:
    """Reminders whose time has arrived and which haven't been sent."""
    return list(
        db.execute(
            select(Reminder)
            .where(
                Reminder.status == ReminderStatus.SCHEDULED,
                Reminder.scheduled_at <= (now or _utcnow()),
            )
            .order_by(Reminder.scheduled_at.asc())
            .limit(limit)
        ).scalars().all()
    )


def cancel_reminders_for_appointment(db: Session, appointment_id: int) -> int:
    """Cancel outstanding reminders when their appointment is cancelled."""
    reminders = db.execute(
        select(Reminder).where(
            Reminder.appointment_id == appointment_id,
            Reminder.status == ReminderStatus.SCHEDULED,
        )
    ).scalars().all()
    for reminder in reminders:
        reminder.status = ReminderStatus.CANCELLED
    if reminders:
        db.commit()
    return len(reminders)
