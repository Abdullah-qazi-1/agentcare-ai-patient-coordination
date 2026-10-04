"""Shared FastAPI dependencies: JWT auth, RBAC, and domain-error mapping.

Every route gets its authenticated principal from `get_current_user`, which resolves a
`CurrentUser` straight off the JWT — never off anything the client supplies in the body
or query string. `require_role` and `require_patient_self_or_staff` are the only two
places RBAC decisions get made, so "who can call this route?" has one answer per route
rather than being re-derived ad hoc.
"""

from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.models import User, UserRole
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.services import audit_service, patient_service
from app.services.errors import ServiceError

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    user_id = int(payload["sub"])
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    role = UserRole(payload["role"])
    patient_id = None
    if role == UserRole.PATIENT:
        profile = patient_service.get_profile_by_user(db, user_id)
        patient_id = profile.id if profile else None

    return CurrentUser(user_id=user_id, role=role, patient_id=patient_id)


def require_role(*roles: UserRole):
    """Dependency factory: only the given roles may proceed.

    A rejection is itself audited (`action="access_denied"`), per the blueprint's RBAC
    section — a denial is exactly the kind of event a compliance reviewer wants on record.
    """

    def checker(
        current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
    ) -> CurrentUser:
        if current.role not in roles:
            audit_service.record(
                db,
                action="access_denied",
                entity_type="route",
                actor_id=current.user_id,
                actor_label=current.role.value,
                metadata={"required_roles": [role.value for role in roles]},
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current

    return checker


def require_patient_self_or_staff(patient_id: int, current: CurrentUser, db: Session) -> None:
    """Guard a route that takes a `patient_id`: staff/admin pass; a patient must match self.

    Patient-scoped reads always filter by `current.patient_id` from the token — this
    guard exists for the handful of routes that also accept a path/query `patient_id`
    (for staff use), so a patient can never read another patient's record by guessing an id.
    """
    if current.is_staff:
        return
    if current.patient_id == patient_id:
        return
    audit_service.record(
        db,
        action="access_denied",
        entity_type="patient_profile",
        entity_id=patient_id,
        actor_id=current.user_id,
        actor_label=current.role.value,
        metadata={"reason": "not_self"},
    )
    raise HTTPException(status_code=403, detail="Insufficient permissions")


def map_service_error(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))
