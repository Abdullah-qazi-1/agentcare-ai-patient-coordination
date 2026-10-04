"""Department and doctor management, plus the deterministic routing fast-path.

`match_department_by_keywords` is what keeps routing cheap: most requests name their
department outright ("cardiology follow-up"), so they never need an LLM call. The
Routing agent only invokes the model when this returns no confident match.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Department, Doctor
from app.services import audit_service
from app.services.errors import ConflictError, NotFoundError


def list_departments(db: Session, *, active_only: bool = True) -> list[Department]:
    stmt = select(Department).order_by(Department.name)
    if active_only:
        stmt = stmt.where(Department.active.is_(True))
    return list(db.execute(stmt).scalars().all())


def get_department(db: Session, department_id: int) -> Department:
    dept = db.get(Department, department_id)
    if not dept:
        raise NotFoundError(f"Department {department_id} not found.")
    return dept


def get_department_by_name(db: Session, name: str) -> Department | None:
    return db.execute(
        select(Department).where(Department.name.ilike(name.strip()))
    ).scalar_one_or_none()


def match_department_by_keywords(db: Session, text: str) -> tuple[Department | None, float]:
    """Deterministic department match. Returns (department, confidence).

    Confidence is 1.0 for an exact department-name mention and 0.8 for a keyword hit.
    Ties (two departments matching equally) return None so the caller escalates to the
    LLM rather than silently picking the first — an arbitrary choice here would be a
    routing error the patient never sees explained.
    """
    lowered = text.lower()
    departments = list_departments(db)

    for dept in departments:
        if re.search(rf"\b{re.escape(dept.name.lower())}\b", lowered):
            return dept, 1.0

    scored: list[tuple[Department, int]] = []
    for dept in departments:
        hits = sum(
            1
            for kw in (dept.routing_keywords or [])
            if re.search(rf"\b{re.escape(str(kw).lower())}\b", lowered)
        )
        if hits:
            scored.append((dept, hits))

    if not scored:
        return None, 0.0

    scored.sort(key=lambda pair: pair[1], reverse=True)
    if len(scored) > 1 and scored[0][1] == scored[1][1]:
        return None, 0.0
    return scored[0][0], 0.8


def create_department(
    db: Session,
    *,
    name: str,
    description: str = "",
    routing_keywords: list[str] | None = None,
    required_document_types: list[str] | None = None,
    actor_id: int,
) -> Department:
    if get_department_by_name(db, name):
        raise ConflictError(f"Department '{name}' already exists.")
    dept = Department(
        name=name.strip(),
        description=description,
        routing_keywords=routing_keywords or [],
        required_document_types=required_document_types or [],
    )
    db.add(dept)
    db.flush()
    audit_service.record(
        db, action="department_created", entity_type="department", entity_id=dept.id,
        actor_id=actor_id, actor_label="staff", metadata={"name": dept.name}, commit=False,
    )
    db.commit()
    db.refresh(dept)
    return dept


def set_department_active(db: Session, department_id: int, active: bool, *, actor_id: int) -> Department:
    dept = get_department(db, department_id)
    dept.active = active
    audit_service.record(
        db, action="department_activated" if active else "department_deactivated",
        entity_type="department", entity_id=dept.id, actor_id=actor_id, actor_label="staff", commit=False,
    )
    db.commit()
    db.refresh(dept)
    return dept


def list_doctors(db: Session, *, department_id: int | None = None, active_only: bool = True) -> list[Doctor]:
    stmt = select(Doctor).order_by(Doctor.name)
    if department_id is not None:
        stmt = stmt.where(Doctor.department_id == department_id)
    if active_only:
        stmt = stmt.where(Doctor.active.is_(True))
    return list(db.execute(stmt).scalars().all())


def create_doctor(db: Session, *, department_id: int, name: str, actor_id: int) -> Doctor:
    get_department(db, department_id)  # validates existence
    doctor = Doctor(department_id=department_id, name=name.strip())
    db.add(doctor)
    db.flush()
    audit_service.record(
        db, action="doctor_created", entity_type="doctor", entity_id=doctor.id,
        actor_id=actor_id, actor_label="staff", metadata={"department_id": department_id}, commit=False,
    )
    db.commit()
    db.refresh(doctor)
    return doctor
