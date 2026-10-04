from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import SlotStatus


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    # Keyword/specialty synonyms used by the deterministic routing fast-path
    # before any LLM classification call — see agents/tools/routing_tools.py.
    routing_keywords: Mapped[list] = mapped_column(JSON, default=list)
    required_document_types: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    doctors: Mapped[list["Doctor"]] = relationship(back_populates="department")


class Doctor(Base):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    department: Mapped["Department"] = relationship(back_populates="doctors")
    slots: Mapped[list["AppointmentSlot"]] = relationship(back_populates="doctor")


class AppointmentSlot(Base):
    __tablename__ = "appointment_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    # All three are filtered on every `get_available_slots` call (the hottest read path
    # in the app — hit on every doctor-directory page load).
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SlotStatus] = mapped_column(
        Enum(SlotStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=SlotStatus.OPEN,
        index=True,
    )

    doctor: Mapped["Doctor"] = relationship(back_populates="slots")
