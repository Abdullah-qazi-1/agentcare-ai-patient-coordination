"""Registration, login, and the authenticated principal."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.rate_limit import check_rate_limit
from app.core.security import create_access_token
from app.db.models import User
from app.db.session import get_db
from app.schemas.auth import CurrentUser, LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services import patient_service
from app.services.errors import NotFoundError

router = APIRouter(prefix="/auth", tags=["auth"])

# Generous enough that a patient fumbling their password a few times never notices,
# tight enough that brute-forcing a password by request volume stops being viable.
LOGIN_RATE_LIMIT = 10
LOGIN_RATE_WINDOW_SECONDS = 60.0


@router.post("/register", response_model=TokenResponse)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    # ServiceError (e.g. duplicate email) isn't caught here — the global handler in
    # main.py maps it to the same response this route would have built by hand.
    user, profile = patient_service.register_patient(
        db,
        name=payload.name,
        email=payload.email,
        password=payload.password,
        date_of_birth=payload.date_of_birth,
        phone=payload.phone,
        preferred_language=payload.preferred_language,
        emergency_contact=payload.emergency_contact,
    )

    token = create_access_token(user_id=user.id, role=user.role.value)
    return TokenResponse(access_token=token, role=user.role, user_id=user.id, patient_id=profile.id)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    # Keyed by IP, not email — rate-limiting by account would let an attacker lock a
    # real patient out just by submitting wrong passwords for their address.
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(
        f"login:{client_ip}", limit=LOGIN_RATE_LIMIT, window_seconds=LOGIN_RATE_WINDOW_SECONDS
    ):
        raise HTTPException(status_code=429, detail="Too many login attempts. Please try again shortly.")

    try:
        token, user, profile = patient_service.authenticate(
            db, email=payload.email, password=payload.password
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=401, detail="Invalid email or password.") from exc

    return TokenResponse(
        access_token=token, role=user.role, user_id=user.id, patient_id=profile.id if profile else None
    )


@router.get("/me", response_model=UserOut)
def me(current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    return db.get(User, current.user_id)
