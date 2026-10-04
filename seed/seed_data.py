"""Synthetic sample data for local development, demos and manual testing.

Everything here is fake. No real patient data, no real credentials.

The script writes through the **service layer**, not directly through the ORM, for
the same reason the agent tools do: `create_department`, `create_slot` and
`register_patient` carry the validation, conflict-checking and audit writes that make
a row legitimate. Seeding through them means a broken service fails here, loudly,
before it can fail inside an agent run where the cause is much harder to see.

The one exception is the bootstrap admin. `create_staff_user` requires an `actor_id`
of the admin performing the creation, which does not exist in an empty database, so
the first account is built directly and every later write is attributed to it.

Usage
-----
    python -m seed.seed_data              # seed; skips if data already present
    python -m seed.seed_data --reset      # delete all rows first, then seed
    python -m seed.seed_data --slot-days 7
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import (
    Appointment,
    AppointmentSlot,
    AuditEvent,
    Department,
    Doctor,
    DocumentType,
    Escalation,
    Notification,
    PatientDocument,
    PatientProfile,
    Reminder,
    User,
    UserRole,
    WorkflowRun,
)
from app.db.session import SessionLocal
from app.services import appointment_service, department_service, document_service, patient_service

# Synthetic throughout. Shared by every seeded account so a demo login is one thing to
# remember; it is not a credential to anything real.
DEMO_PASSWORD = "AgentCare!2026"

ADMIN_EMAIL = "admin@agentcare.local"
STAFF_EMAIL = "staff@agentcare.local"

# Clinic hours used for generated slots. Each doctor gets the same four 30-minute
# windows per weekday — non-overlapping, so `create_slot`'s overlap check passes.
SLOT_TIMES = [time(9, 0), time(10, 0), time(14, 0), time(15, 0)]
SLOT_MINUTES = 30

# Routing keywords are deliberately disjoint across departments. `match_department_by_
# keywords` returns no match when two departments tie, so overlapping vocabularies would
# quietly push every request onto the expensive LLM path.
DEPARTMENTS: list[dict] = [
    {
        "name": "Cardiology",
        "description": "Heart and circulatory care, including ECG review and blood pressure follow-up.",
        "routing_keywords": [
            "heart", "cardiac", "cardiology", "ecg", "ekg", "palpitations",
            "blood pressure", "cholesterol", "angina",
        ],
        "required_document_types": [DocumentType.ECG_REPORT.value, DocumentType.INSURANCE_CARD.value],
        "doctors": ["Dr. Meera Iyer", "Dr. Samuel Okonkwo"],
    },
    {
        "name": "Orthopedics",
        "description": "Bones, joints, fractures and musculoskeletal injury.",
        "routing_keywords": [
            "bone", "fracture", "knee", "shoulder", "joint", "spine",
            "back pain", "sprain", "ligament", "orthopedic",
        ],
        "required_document_types": [DocumentType.IMAGING_REPORT.value, DocumentType.INSURANCE_CARD.value],
        "doctors": ["Dr. Lena Fischer", "Dr. Arjun Desai"],
    },
    {
        "name": "Dermatology",
        "description": "Skin, hair and nail conditions.",
        "routing_keywords": [
            "skin", "rash", "acne", "eczema", "psoriasis", "mole",
            "dermatitis", "hair loss", "dermatology",
        ],
        "required_document_types": [DocumentType.INSURANCE_CARD.value],
        "doctors": ["Dr. Yuki Tanaka", "Dr. Fatima Al-Rashid"],
    },
    {
        "name": "Neurology",
        "description": "Brain, spinal cord and nervous system care.",
        "routing_keywords": [
            "brain", "headache", "migraine", "seizure", "epilepsy",
            "numbness", "dizziness", "nerve", "tremor", "neurology",
        ],
        "required_document_types": [DocumentType.IMAGING_REPORT.value, DocumentType.INSURANCE_CARD.value],
        "doctors": ["Dr. Peter Grant", "Dr. Ana Sousa"],
    },
    {
        "name": "General Medicine",
        "description": "First point of contact for common illness and routine check-ups.",
        "routing_keywords": [
            "fever", "cough", "cold", "flu", "checkup", "check-up",
            "fatigue", "general", "physical", "vaccination",
        ],
        "required_document_types": [DocumentType.INSURANCE_CARD.value],
        "doctors": ["Dr. Nadia Haddad", "Dr. Tom Bennett"],
    },
    {
        "name": "Radiology",
        "description": "Diagnostic imaging: X-ray, MRI, CT and ultrasound.",
        "routing_keywords": [
            "xray", "x-ray", "mri", "ct scan", "ultrasound",
            "imaging", "sonography", "radiology", "mammogram",
        ],
        "required_document_types": [DocumentType.REFERRAL_LETTER.value],
        "doctors": ["Dr. Ravi Krishnan", "Dr. Elise Moreau"],
    },
]

PATIENTS: list[dict] = [
    {
        "name": "Asha Menon",
        "email": "asha.menon@example.com",
        "date_of_birth": date(1986, 3, 14),
        "phone": "+91-98200-11223",
        "emergency_contact": "Vikram Menon (spouse) +91-98200-11224",
    },
    {
        "name": "Rohit Sharma",
        "email": "rohit.sharma@example.com",
        "date_of_birth": date(1994, 11, 2),
        "phone": "+91-99870-55440",
        "emergency_contact": "Sunita Sharma (mother) +91-99870-55441",
    },
    {
        "name": "Priya Nair",
        "email": "priya.nair@example.com",
        "date_of_birth": date(1971, 6, 28),
        "phone": "+91-90040-77881",
        "emergency_contact": "Deepak Nair (brother) +91-90040-77882",
    },
]

# Filenames are chosen so `classify_by_filename` resolves them deterministically —
# these seed the document fast-path rather than the LLM classification path.
# Priya is left with no documents so `check_missing_documents` has a real gap to find.
DOCUMENTS: list[dict] = [
    {
        "patient_email": "asha.menon@example.com",
        "filename": "ecg_report_2026_07_10.pdf",
        "body": "SYNTHETIC SAMPLE — ECG report for demonstration only. Not a real clinical record.",
    },
    {
        "patient_email": "asha.menon@example.com",
        "filename": "insurance_card_front.pdf",
        "body": "SYNTHETIC SAMPLE — insurance card image placeholder.",
    },
    {
        "patient_email": "rohit.sharma@example.com",
        "filename": "blood_report_2026_07_01.pdf",
        "body": "SYNTHETIC SAMPLE — CBC panel placeholder. Not a real clinical record.",
    },
]

# Child-to-parent order: rows with foreign keys go before the rows they point at, so a
# reset works whether or not the backend enforces the constraints.
RESET_ORDER = [
    AuditEvent,
    Notification,
    Reminder,
    Escalation,
    Appointment,
    WorkflowRun,
    PatientDocument,
    AppointmentSlot,
    Doctor,
    Department,
    PatientProfile,
    User,
]


def _log(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(message)


def reset_data(db: Session, *, quiet: bool = False) -> None:
    """Delete every application row, leaving the schema and alembic_version intact.

    Deliberately not a table drop: the schema is Alembic's to own, and blowing it away
    here would put the database out of step with its migration history.
    """
    _log("Resetting existing data...", quiet=quiet)
    for model in RESET_ORDER:
        deleted = db.query(model).delete()
        if deleted:
            _log(f"  - {model.__tablename__:20} {deleted} row(s) deleted", quiet=quiet)
    db.commit()


def bootstrap_admin(db: Session) -> User:
    """Create (or fetch) the admin account every other seeded write is attributed to."""
    existing = db.query(User).filter(User.email == ADMIN_EMAIL).one_or_none()
    if existing:
        return existing

    admin = User(
        name="AgentCare Admin",
        email=ADMIN_EMAIL,
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.ADMIN,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def seed_staff(db: Session, *, admin_id: int) -> User:
    existing = db.query(User).filter(User.email == STAFF_EMAIL).one_or_none()
    if existing:
        return existing
    return patient_service.create_staff_user(
        db,
        name="Priya Desai (Front Desk)",
        email=STAFF_EMAIL,
        password=DEMO_PASSWORD,
        role=UserRole.STAFF,
        actor_id=admin_id,
    )


def seed_departments(db: Session, *, admin_id: int, quiet: bool = False) -> dict[str, Department]:
    departments: dict[str, Department] = {}
    for spec in DEPARTMENTS:
        existing = department_service.get_department_by_name(db, spec["name"])
        if existing:
            departments[spec["name"]] = existing
            continue

        dept = department_service.create_department(
            db,
            name=spec["name"],
            description=spec["description"],
            routing_keywords=spec["routing_keywords"],
            required_document_types=spec["required_document_types"],
            actor_id=admin_id,
        )
        departments[spec["name"]] = dept
        _log(f"  + department  {dept.name}", quiet=quiet)
    return departments


def seed_doctors(
    db: Session, departments: dict[str, Department], *, admin_id: int, quiet: bool = False
) -> list[Doctor]:
    doctors: list[Doctor] = []
    for spec in DEPARTMENTS:
        dept = departments[spec["name"]]
        existing_names = {d.name for d in department_service.list_doctors(db, department_id=dept.id)}
        for doctor_name in spec["doctors"]:
            if doctor_name in existing_names:
                doctors.extend(
                    d
                    for d in department_service.list_doctors(db, department_id=dept.id)
                    if d.name == doctor_name
                )
                continue
            doctor = department_service.create_doctor(
                db, department_id=dept.id, name=doctor_name, actor_id=admin_id
            )
            doctors.append(doctor)
            _log(f"  + doctor      {doctor.name} ({dept.name})", quiet=quiet)
    return doctors


def seed_slots(db: Session, doctors: list[Doctor], *, admin_id: int, days: int, quiet: bool = False) -> int:
    """Generate open slots on weekdays for the next `days` calendar days.

    Slots start tomorrow because `get_available_slots` only returns future ones — a slot
    generated for earlier today would be invisible and look like a bug.
    """
    today = datetime.now(UTC).date()
    created = 0

    for offset in range(1, days + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:  # Saturday/Sunday
            continue
        for doctor in doctors:
            for slot_time in SLOT_TIMES:
                start = datetime.combine(day, slot_time, tzinfo=UTC)
                try:
                    appointment_service.create_slot(
                        db,
                        doctor_id=doctor.id,
                        start_time=start,
                        end_time=start + timedelta(minutes=SLOT_MINUTES),
                        actor_id=admin_id,
                    )
                    created += 1
                except Exception:
                    # An overlap means this slot already exists from an earlier run.
                    db.rollback()

    _log(f"  + slots       {created} open slot(s) across {len(doctors)} doctor(s)", quiet=quiet)
    return created


def seed_patients(db: Session, *, quiet: bool = False) -> dict[str, PatientProfile]:
    profiles: dict[str, PatientProfile] = {}
    for spec in PATIENTS:
        existing = db.query(User).filter(User.email == spec["email"]).one_or_none()
        if existing and existing.patient_profile:
            profiles[spec["email"]] = existing.patient_profile
            continue

        _, profile = patient_service.register_patient(
            db,
            name=spec["name"],
            email=spec["email"],
            password=DEMO_PASSWORD,
            date_of_birth=spec["date_of_birth"],
            phone=spec["phone"],
            emergency_contact=spec["emergency_contact"],
        )
        profiles[spec["email"]] = profile
        _log(f"  + patient     {spec['name']} (patient_id={profile.id})", quiet=quiet)
    return profiles


def seed_documents(
    db: Session, profiles: dict[str, PatientProfile], *, admin_id: int, quiet: bool = False
) -> int:
    created = 0
    for spec in DOCUMENTS:
        profile = profiles.get(spec["patient_email"])
        if not profile:
            continue

        content = spec["body"].encode("utf-8")
        checksum = document_service.compute_checksum(content)
        if document_service.check_duplicate(db, patient_id=profile.id, checksum=checksum).is_duplicate:
            continue  # already seeded on a previous run

        doc_type, confidence = document_service.classify_by_filename(spec["filename"])
        document_service.store_document(
            db,
            patient_id=profile.id,
            filename=spec["filename"],
            content=content,
            document_type=doc_type or DocumentType.OTHER,
            confidence=confidence,
            document_date=document_service.extract_date_from_filename(spec["filename"]),
            actor_id=admin_id,
            actor_label="seed",
        )
        created += 1
        _log(f"  + document    {spec['filename']} -> {(doc_type or DocumentType.OTHER).value}", quiet=quiet)
    return created


def summarize(db: Session, profiles: dict[str, PatientProfile]) -> None:
    counts = {model.__tablename__: db.query(model).count() for model in reversed(RESET_ORDER)}
    print("\nDatabase now contains:")
    for table, count in counts.items():
        if count:
            print(f"  {table:20} {count}")

    print("\nLogins (all use the same synthetic password):")
    print(f"  password        {DEMO_PASSWORD}")
    print(f"  admin           {ADMIN_EMAIL}")
    print(f"  staff           {STAFF_EMAIL}")
    for spec in PATIENTS:
        profile = profiles.get(spec["email"])
        suffix = f"  (patient_id={profile.id})" if profile else ""
        print(f"  patient         {spec['email']}{suffix}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed AgentCare with synthetic sample data.")
    parser.add_argument(
        "--reset", action="store_true", help="Delete all existing rows before seeding."
    )
    parser.add_argument(
        "--slot-days", type=int, default=14, help="Days ahead to generate slots for (default: 14)."
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the final summary.")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        if args.reset:
            reset_data(db, quiet=args.quiet)

        _log("Seeding...", quiet=args.quiet)
        admin = bootstrap_admin(db)
        seed_staff(db, admin_id=admin.id)

        departments = seed_departments(db, admin_id=admin.id, quiet=args.quiet)
        doctors = seed_doctors(db, departments, admin_id=admin.id, quiet=args.quiet)
        seed_slots(db, doctors, admin_id=admin.id, days=args.slot_days, quiet=args.quiet)
        profiles = seed_patients(db, quiet=args.quiet)
        seed_documents(db, profiles, admin_id=admin.id, quiet=args.quiet)

        summarize(db, profiles)
        print("\nSeed complete.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
