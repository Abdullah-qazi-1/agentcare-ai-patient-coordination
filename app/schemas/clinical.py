from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models.enums import SlotStatus
from app.schemas.common import ORMModel


class DepartmentOut(ORMModel):
    id: int
    name: str
    description: str
    required_document_types: list[str]
    active: bool


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    routing_keywords: list[str] = Field(default_factory=list)
    required_document_types: list[str] = Field(default_factory=list)


class DepartmentActiveUpdate(BaseModel):
    active: bool


class DoctorOut(ORMModel):
    id: int
    department_id: int
    name: str
    active: bool


class DoctorCreate(BaseModel):
    department_id: int
    name: str = Field(min_length=1, max_length=200)


class SlotOut(ORMModel):
    id: int
    doctor_id: int
    start_time: datetime
    end_time: datetime
    status: SlotStatus


class SlotWithContext(BaseModel):
    """Slot enriched with doctor/department names — what the agent and UI actually need."""

    slot_id: int
    doctor_id: int
    doctor_name: str
    department_id: int
    department_name: str
    start_time: datetime
    end_time: datetime


class SlotCreate(BaseModel):
    doctor_id: int
    start_time: datetime
    end_time: datetime
