"""Shared fixtures.

Each test gets a fresh in-memory database built with `Base.metadata.create_all`
rather than by running Alembic. A throwaway database has no migration history to
preserve, and `create_all` is milliseconds against a migration runner's seconds —
Alembic still owns the real schema, which the `test_schema_matches_models` check in
`test_config.py` guards against drift.

Fixtures build rows through the ORM rather than the service layer. The services are
what these tests exercise; seeding through them would mean a service bug shows up as
a fixture error in every unrelated test rather than as one clear failure.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AppointmentSlot,
    Department,
    Doctor,
    DocumentType,
    PatientProfile,
    User,
    UserRole,
)


@pytest.fixture
def db():
    """A fresh, empty database per test.

    StaticPool keeps every connection pointed at the same in-memory database; the
    default pool would hand each connection its own blank one.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def actor(db: Session) -> User:
    """A staff account to attribute audited writes to."""
    user = User(
        name="Test Staff",
        email="staff@test.local",
        password_hash="not-a-real-hash",  # auth is out of scope for these tests
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def patient(db: Session) -> PatientProfile:
    user = User(
        name="Test Patient",
        email="patient@test.local",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db.add(user)
    db.flush()
    profile = PatientProfile(user_id=user.id, phone="+91-90000-00000")
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@pytest.fixture
def other_patient(db: Session) -> PatientProfile:
    """A second patient, for tests where one patient must not affect another."""
    user = User(
        name="Other Patient",
        email="other@test.local",
        password_hash="not-a-real-hash",
        role=UserRole.PATIENT,
    )
    db.add(user)
    db.flush()
    profile = PatientProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@pytest.fixture
def cardiology(db: Session) -> Department:
    dept = Department(
        name="Cardiology",
        description="Heart care.",
        routing_keywords=["heart", "cardiac", "ecg", "palpitations"],
        required_document_types=[DocumentType.ECG_REPORT.value, DocumentType.INSURANCE_CARD.value],
    )
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


@pytest.fixture
def orthopedics(db: Session) -> Department:
    dept = Department(
        name="Orthopedics",
        description="Bones and joints.",
        routing_keywords=["bone", "fracture", "knee", "joint"],
        required_document_types=[DocumentType.IMAGING_REPORT.value],
    )
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


@pytest.fixture
def doctor(db: Session, cardiology: Department) -> Doctor:
    doc = Doctor(department_id=cardiology.id, name="Dr. Test Cardiologist")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.fixture
def slots(db: Session, doctor: Doctor) -> list[AppointmentSlot]:
    """Four open, non-overlapping slots starting tomorrow.

    Future-dated because `get_available_slots` and `book_appointment` both reject
    anything in the past — a slot generated for earlier today would be invisible and
    read as a bug in the query rather than in the fixture.
    """
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    created = []
    for hour_offset in range(4):
        start = base + timedelta(hours=hour_offset)
        slot = AppointmentSlot(
            doctor_id=doctor.id, start_time=start, end_time=start + timedelta(minutes=30)
        )
        db.add(slot)
        created.append(slot)
    db.commit()
    for slot in created:
        db.refresh(slot)
    return created


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Redirect document storage into a temp dir so tests never touch storage/."""
    from app.services import document_service

    root = tmp_path / "documents"
    monkeypatch.setattr(document_service, "STORAGE_ROOT", root)
    return root


@pytest.fixture
def client(db: Session):
    """A TestClient wired to the same in-memory `db` session every other fixture uses.

    Overriding the `get_db` dependency (rather than pointing the app at a second
    database) means a test can set up rows through the ORM/`db` fixture and then see
    them immediately through an HTTP call in the same test.
    """
    from fastapi.testclient import TestClient

    from app.core import rate_limit
    from app.db.session import get_db
    from app.main import app

    def _override_get_db():
        yield db

    # TestClient's `request.client.host` is the same constant for every test in the
    # process, so /auth/login's rate limiter would otherwise accumulate across tests
    # and start rejecting unrelated ones.
    rate_limit.reset()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def auth_headers(user: User) -> dict[str, str]:
    """A bearer-token header for a fixture-created user, bypassing password login.

    Fixture users carry a placeholder `password_hash` ("not-a-real-hash"), so they
    can't log in through `/auth/login` — tests that need an authenticated request build
    the token directly, exactly as `create_access_token` does after a real login.
    """
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id=user.id, role=user.role.value)}"}


@pytest.fixture
def in_memory_checkpointer(monkeypatch):
    """Point the graph's checkpointer at a throwaway in-memory SQLite connection.

    Any test that calls `build_graph`/`start_workflow`/`resume_workflow` needs this —
    otherwise it opens the project's real `agentcare_checkpoints.db`, which is both slow
    and pollutes real state with test-only thread ids. Opt-in (not autouse) so tests that
    never touch the graph don't pay for it.
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    import app.agents.graph as graph_module

    connection = sqlite3.connect(":memory:", check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    monkeypatch.setattr(graph_module, "get_checkpointer", lambda: saver)
    return saver
