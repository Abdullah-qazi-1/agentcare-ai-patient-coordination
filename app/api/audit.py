"""Read-only audit trail, staff/admin only — what the Staff Dashboard renders per run."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_role
from app.db.models import UserRole
from app.db.session import get_db
from app.schemas.workflow import AuditEventOut
from app.services import audit_service

router = APIRouter(
    prefix="/audit", tags=["audit"], dependencies=[Depends(require_role(UserRole.STAFF, UserRole.ADMIN))]
)


@router.get("/workflow/{run_id}", response_model=list[AuditEventOut])
def audit_for_workflow(run_id: int, db: Session = Depends(get_db)):
    return audit_service.list_for_workflow(db, run_id)
