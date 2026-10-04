from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class PatientProfileOut(ORMModel):
    id: int
    user_id: int
    date_of_birth: date | None
    phone: str | None
    preferred_language: str
    emergency_contact: str | None
    created_at: datetime
    updated_at: datetime


class PatientProfileUpdate(BaseModel):
    phone: str | None = Field(default=None, max_length=30)
    preferred_language: str | None = None
    emergency_contact: str | None = Field(default=None, max_length=255)
    date_of_birth: date | None = None


class PatientSummary(ORMModel):
    """Patient view for staff listings — name/email come from the joined User."""

    id: int
    name: str
    email: str
    phone: str | None
    preferred_language: str
