"""User registration, authentication, and patient profile management."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import PatientProfile, User, UserRole
from app.services import audit_service
from app.services.errors import ConflictError, NotFoundError, ValidationError


def register_patient(
    db: Session,
    *,
    name: str,
    email: str,
    password: str,
    date_of_birth=None,
    phone: str | None = None,
    preferred_language: str = "en",
    emergency_contact: str | None = None,
) -> tuple[User, PatientProfile]:
    email = email.lower().strip()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise ConflictError("An account with this email already exists.")

    user = User(name=name, email=email, password_hash=hash_password(password), role=UserRole.PATIENT)
    db.add(user)
    db.flush()

    profile = PatientProfile(
        user_id=user.id,
        date_of_birth=date_of_birth,
        phone=phone,
        preferred_language=preferred_language,
        emergency_contact=emergency_contact,
    )
    db.add(profile)
    db.flush()

    audit_service.record(
        db,
        action="patient_registered",
        entity_type="patient_profile",
        entity_id=profile.id,
        actor_id=user.id,
        actor_label="patient",
        metadata={"email_domain": email.split("@")[-1]},
        commit=False,
    )
    db.commit()
    db.refresh(user)
    db.refresh(profile)
    return user, profile


def create_staff_user(
    db: Session, *, name: str, email: str, password: str, role: UserRole, actor_id: int
) -> User:
    """Create a staff/admin account. Callable only by an admin (enforced at the route)."""
    if role == UserRole.PATIENT:
        raise ValidationError("Use patient registration to create patient accounts.")
    email = email.lower().strip()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        raise ConflictError("An account with this email already exists.")

    user = User(name=name, email=email, password_hash=hash_password(password), role=role)
    db.add(user)
    db.flush()
    audit_service.record(
        db,
        action="staff_user_created",
        entity_type="user",
        entity_id=user.id,
        actor_id=actor_id,
        actor_label="admin",
        metadata={"role": role.value},
        commit=False,
    )
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, *, email: str, password: str) -> tuple[str, User, PatientProfile | None]:
    user = db.execute(select(User).where(User.email == email.lower().strip())).scalar_one_or_none()
    # Same generic message whether the email is unknown or the password is wrong —
    # a differentiated error would let an attacker enumerate registered accounts.
    if not user or not verify_password(password, user.password_hash):
        audit_service.record(
            db,
            action="login_failed",
            entity_type="user",
            entity_id=user.id if user else None,
            actor_label="anonymous",
            metadata={"email_domain": email.split("@")[-1] if "@" in email else "invalid"},
        )
        raise NotFoundError("Invalid email or password.")

    token = create_access_token(user_id=user.id, role=user.role.value)
    audit_service.record(
        db, action="login_succeeded", entity_type="user", entity_id=user.id, actor_id=user.id,
        actor_label=user.role.value,
    )
    return token, user, user.patient_profile


def get_profile(db: Session, patient_id: int) -> PatientProfile:
    profile = db.get(PatientProfile, patient_id)
    if not profile:
        raise NotFoundError(f"Patient profile {patient_id} not found.")
    return profile


def get_profile_by_user(db: Session, user_id: int) -> PatientProfile | None:
    return db.execute(select(PatientProfile).where(PatientProfile.user_id == user_id)).scalar_one_or_none()


def update_profile(db: Session, patient_id: int, *, actor_id: int, **fields) -> PatientProfile:
    profile = get_profile(db, patient_id)
    changed = {}
    for key, value in fields.items():
        if value is not None and hasattr(profile, key) and getattr(profile, key) != value:
            changed[key] = key  # record which fields changed, never the values (PII)
            setattr(profile, key, value)

    if changed:
        audit_service.record(
            db,
            action="patient_profile_updated",
            entity_type="patient_profile",
            entity_id=profile.id,
            actor_id=actor_id,
            actor_label="patient",
            metadata={"fields_changed": sorted(changed)},
            commit=False,
        )
    db.commit()
    db.refresh(profile)
    return profile


def get_or_create_by_email(db: Session, *, email: str, name: str) -> PatientProfile:
    """Identity resolution used by the Coordinator agent.

    Returns the existing patient when the email is already known, otherwise creates a
    profile. Agent-created accounts get no usable password — the patient must complete
    registration through the portal to gain login access.
    """
    email = email.lower().strip()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user:
        if not user.patient_profile:
            profile = PatientProfile(user_id=user.id)
            db.add(profile)
            db.commit()
            db.refresh(profile)
            return profile
        return user.patient_profile

    user = User(name=name, email=email, password_hash="!", role=UserRole.PATIENT)
    db.add(user)
    db.flush()
    profile = PatientProfile(user_id=user.id)
    db.add(profile)
    db.flush()
    audit_service.record(
        db,
        action="patient_record_created_by_agent",
        entity_type="patient_profile",
        entity_id=profile.id,
        actor_label="coordinator_agent",
        metadata={"email_domain": email.split("@")[-1]},
        commit=False,
    )
    db.commit()
    db.refresh(profile)
    return profile


def list_patients(db: Session, limit: int = 100) -> list[tuple[PatientProfile, User]]:
    stmt = select(PatientProfile, User).join(User, PatientProfile.user_id == User.id).limit(limit)
    return [(p, u) for p, u in db.execute(stmt).all()]
