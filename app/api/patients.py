"""Patient profile self-service, plus staff listing and staff-account creation."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_patient_self_or_staff, require_role
from app.db.models import UserRole
from app.db.session import get_db
from app.schemas.auth import CreateStaffRequest, CurrentUser, UserOut
from app.schemas.patient import PatientProfileOut, PatientProfileUpdate, PatientSummary
from app.services import patient_service

router = APIRouter(prefix="/patients", tags=["patients"])


@router.get("/me", response_model=PatientProfileOut)
def get_my_profile(
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)), db: Session = Depends(get_db)
):
    return patient_service.get_profile(db, current.patient_id)


@router.patch("/me", response_model=PatientProfileOut)
def update_my_profile(
    payload: PatientProfileUpdate,
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)),
    db: Session = Depends(get_db),
):
    return patient_service.update_profile(
        db, current.patient_id, actor_id=current.user_id, **payload.model_dump()
    )


@router.get("", response_model=list[PatientSummary])
def list_patients(
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    rows = patient_service.list_patients(db)
    return [
        PatientSummary(id=profile.id, name=user.name, email=user.email, phone=profile.phone,
                        preferred_language=profile.preferred_language)
        for profile, user in rows
    ]


@router.get("/{patient_id}", response_model=PatientProfileOut)
def get_patient(
    patient_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    require_patient_self_or_staff(patient_id, current, db)
    return patient_service.get_profile(db, patient_id)


@router.post("/staff", response_model=UserOut)
def create_staff_account(
    payload: CreateStaffRequest,
    current: CurrentUser = Depends(require_role(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return patient_service.create_staff_user(
        db,
        name=payload.name,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        actor_id=current.user_id,
    )
