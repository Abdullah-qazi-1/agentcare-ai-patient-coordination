from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import ReminderStatus, ReminderType

if TYPE_CHECKING:
    from app.db.models.appointment import Appointment
    from app.db.models.user import PatientProfile


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient_profiles.id"))
    appointment_id: Mapped[int | None] = mapped_column(ForeignKey("appointments.id"), nullable=True)
    reminder_type: Mapped[ReminderType] = mapped_column(
        Enum(ReminderType, native_enum=False, values_callable=lambda e: [m.value for m in e]),
    )
    # `list_due_reminders` filters on both — the dispatch sweep's hot path.
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=ReminderStatus.SCHEDULED,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    patient: Mapped["PatientProfile"] = relationship()
    appointment: Mapped["Appointment | None"] = relationship()


class Notification(Base):
    """Simulated dispatch channel — a real integration point (email/SMS provider)
    would replace `dispatch_notification` in services/reminder_service.py without
    changing this table's shape.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"))
    channel: Mapped[str] = mapped_column(default="in_app")
    message: Mapped[str] = mapped_column(default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
