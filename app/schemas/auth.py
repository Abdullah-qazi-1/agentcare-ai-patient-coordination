from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

from app.db.models.enums import UserRole
from app.schemas.common import ORMModel


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    # Self-registration always creates a patient. Staff/admin accounts are created
    # by an existing admin via /patients/staff — never selectable by the caller here.
    date_of_birth: date | None = None
    phone: str | None = Field(default=None, max_length=30)
    preferred_language: str = "en"
    emergency_contact: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    # Deliberately a plain `str`, not `EmailStr`: this checks an *existing* account, and
    # `EmailStr`'s reserved-TLD rule would lock out real accounts on internal domains
    # like the seeded `@agentcare.local` staff/admin logins. Format validation belongs
    # at registration time, where the email is new and worth catching typos on.
    email: str
    password: str


class CreateStaffRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    role: UserRole


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: UserRole
    user_id: int
    patient_id: int | None = None


class UserOut(ORMModel):
    id: int
    name: str
    email: str
    role: UserRole
    created_at: datetime


class CurrentUser(BaseModel):
    """Authenticated principal resolved from the JWT, used by route dependencies."""

    user_id: int
    role: UserRole
    patient_id: int | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in (UserRole.STAFF, UserRole.ADMIN)
