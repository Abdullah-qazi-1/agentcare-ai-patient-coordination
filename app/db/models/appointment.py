from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import AppointmentStatus

if TYPE_CHECKING:
    from app.db.models.clinical import AppointmentSlot, Doctor
    from app.db.models.user import PatientProfile


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Every `list_patient_appointments` / `GET /appointments/me` call filters on this.
    patient_id: Mapped[int] = mapped_column(ForeignKey("patient_profiles.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"))
    slot_id: Mapped[int] = mapped_column(ForeignKey("appointment_slots.id"))
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=AppointmentStatus.PENDING,
    )
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    patient: Mapped["PatientProfile"] = relationship()
    doctor: Mapped["Doctor"] = relationship()
    slot: Mapped["AppointmentSlot"] = relationship()
