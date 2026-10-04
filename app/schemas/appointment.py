from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models.enums import AppointmentStatus
from app.schemas.common import ORMModel


class AppointmentOut(ORMModel):
    id: int
    patient_id: int
    doctor_id: int
    slot_id: int
    status: AppointmentStatus
    reason: str
    created_at: datetime
    updated_at: datetime


class AppointmentDetail(BaseModel):
    """Confirmation-shaped view, assembled from persisted rows (never from agent prose)."""

    appointment_id: int
    status: AppointmentStatus
    reason: str
    doctor_name: str
    department_name: str
    start_time: datetime
    end_time: datetime


class AppointmentCreate(BaseModel):
    slot_id: int
    reason: str = Field(default="", max_length=500)


class AppointmentReschedule(BaseModel):
    new_slot_id: int


class AppointmentCancel(BaseModel):
    reason: str = Field(default="", max_length=500)
