"""Hospital directory: departments and doctors.

Reading the directory is open to any authenticated user — it's public structure, not
patient data — so a UI can build a "find a doctor" browsing experience on top of
`/appointments/slots`. Creating or deactivating entries is staff/admin only, since this
is the actual hospital configuration staff are expected to manage (requirement 4.2).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_role
from app.db.models import UserRole
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.clinical import (
    DepartmentActiveUpdate,
    DepartmentCreate,
    DepartmentOut,
    DoctorCreate,
    DoctorOut,
)
from app.services import department_service

router = APIRouter(tags=["clinical"], dependencies=[Depends(get_current_user)])


@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(active_only: bool = True, db: Session = Depends(get_db)):
    return department_service.list_departments(db, active_only=active_only)


@router.post("/departments", response_model=DepartmentOut)
def create_department(
    payload: DepartmentCreate,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return department_service.create_department(
        db,
        name=payload.name,
        description=payload.description,
        routing_keywords=payload.routing_keywords,
        required_document_types=payload.required_document_types,
        actor_id=current.user_id,
    )


@router.patch("/departments/{department_id}/active", response_model=DepartmentOut)
def set_department_active(
    department_id: int,
    payload: DepartmentActiveUpdate,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return department_service.set_department_active(
        db, department_id, payload.active, actor_id=current.user_id
    )


@router.get("/doctors", response_model=list[DoctorOut])
def list_doctors(department_id: int | None = None, db: Session = Depends(get_db)):
    return department_service.list_doctors(db, department_id=department_id)


@router.post("/doctors", response_model=DoctorOut)
def create_doctor(
    payload: DoctorCreate,
    current: CurrentUser = Depends(require_role(UserRole.STAFF, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    return department_service.create_doctor(
        db, department_id=payload.department_id, name=payload.name, actor_id=current.user_id
    )
