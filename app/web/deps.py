"""Cookie-based auth + role guards for server-rendered (Jinja2) routes.

Mirrors `app/api/deps.py`'s bearer-token dependencies exactly, just reading the session
JWT from a cookie instead of the `Authorization` header, so every existing service
function (which only ever sees a `CurrentUser`) needs no changes at all.
"""

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.models import User, UserRole
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.services import patient_service
from app.web.context import register_globals
from app.web.security import SESSION_COOKIE

templates = Jinja2Templates(directory="templates")
register_globals(templates)


class NotAuthenticatedError(Exception):
    """No valid session cookie. Handled once in `app/main.py` by redirecting to
    /login, rather than every route handling it individually."""


class ForbiddenError(Exception):
    """Authenticated, but this role can't access the route. Renders a styled 403."""


class WebSession(CurrentUser):
    """`CurrentUser` plus the display fields templates need (name/email for the nav
    chip) — a strict superset, so every service call that expects `CurrentUser` still
    works unchanged."""

    name: str
    email: str


def get_current_user_from_cookie(request: Request, db: Session = Depends(get_db)) -> WebSession:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise NotAuthenticatedError()
    try:
        payload = decode_access_token(token)
    except ValueError as exc:
        raise NotAuthenticatedError() from exc

    user_id = int(payload["sub"])
    user = db.get(User, user_id)
    if not user:
        raise NotAuthenticatedError()

    role = UserRole(payload["role"])
    patient_id = None
    if role == UserRole.PATIENT:
        profile = patient_service.get_profile_by_user(db, user_id)
        patient_id = profile.id if profile else None

    return WebSession(user_id=user_id, role=role, patient_id=patient_id, name=user.name, email=user.email)


def require_role_web(*roles: UserRole):
    def checker(current: WebSession = Depends(get_current_user_from_cookie)) -> WebSession:
        if current.role not in roles:
            raise ForbiddenError()
        return current

    return checker


PATIENT_NAV = [
    {"label": "New Request", "to": "/"},
    {"label": "My Requests", "to": "/requests"},
    {"label": "Find a Doctor", "to": "/find-a-doctor"},
    {"label": "Documents", "to": "/documents"},
    {"label": "Reminders", "to": "/reminders"},
]

STAFF_NAV = [
    {"label": "Escalations", "to": "/staff/escalations"},
    {"label": "Workflow Runs", "to": "/staff/workflows"},
    {"label": "Patients", "to": "/staff/patients"},
    {"label": "Manage Directory", "to": "/staff/directory"},
]

ADMIN_EXTRA_NAV = [{"label": "Add Staff", "to": "/staff/new-staff"}]


def nav_items_for(role: UserRole) -> list[dict]:
    if role == UserRole.PATIENT:
        return PATIENT_NAV
    items = list(STAFF_NAV)
    if role == UserRole.ADMIN:
        items = items + ADMIN_EXTRA_NAV
    return items


def render(
    request: Request,
    name: str,
    *,
    current_user: WebSession | None = None,
    status_code: int = 200,
    **context,
):
    """`TemplateResponse` wrapper that injects the context every page needs, so
    individual routes only pass what's specific to that page."""
    ctx = {
        "current_user": current_user,
        "nav_items": nav_items_for(current_user.role) if current_user else [],
        "csrf_token": request.cookies.get("csrf_token", ""),
        **context,
    }
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
