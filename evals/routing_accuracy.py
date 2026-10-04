"""Deterministic routing accuracy evaluation.

Run standalone:
    uv run python -m evals.routing_accuracy

Run as part of the test suite:
    uv run pytest tests/test_evals_gate.py

`department_service.match_department_by_keywords` is the cheap, no-LLM-call fast path
the Routing agent tries first (`app/agents/prompts/routing.py`, `docs/ARCHITECTURE_
BLUEPRINT.md` §8) — routing hits this table before any model call, and the model is
invoked only on genuine ambiguity. This eval measures how well that fast path covers
realistic patient phrasing against the *real* seeded department/keyword data
(`seed.seed_data.DEPARTMENTS`), not a hand-copied mirror of it that could drift.

Two kinds of case are asserted:
- clear phrasings that should resolve deterministically to one department, or to an
  exact department-name mention;
- phrasings that should deliberately NOT resolve deterministically (an ambiguous
  request, or a natural inflection like a plural/verb form the keyword table doesn't
  stem for) — these document the designed hand-off point to the LLM Routing agent
  rather than being errors.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Department
from app.services import department_service


@dataclass(frozen=True)
class Case:
    text: str
    expected_department: str | None  # None means "should defer to the LLM"
    note: str = ""


DATASET: list[Case] = [
    # --- Cardiology ---
    Case("I have been having heart palpitations", "Cardiology"),
    Case("I need a cardiology appointment", "Cardiology"),
    Case("my heart has been racing lately", "Cardiology"),
    Case("follow up on my angina", "Cardiology"),
    Case("I need to see Cardiology", "Cardiology", "exact department-name mention"),
    # --- Orthopedics ---
    Case("I have a fracture in my wrist", "Orthopedics"),
    Case("my knee has been swollen for days", "Orthopedics"),
    Case("possible ligament tear in my ankle", "Orthopedics"),
    # --- Dermatology ---
    Case("I have a persistent skin rash", "Dermatology"),
    Case("I've noticed some hair loss lately", "Dermatology"),
    Case("I think I have eczema on my arms", "Dermatology"),
    # --- Neurology ---
    Case("I have been getting a severe migraine", "Neurology"),
    Case("I have numbness in my left arm", "Neurology"),
    Case("I get frequent dizziness and tremors", "Neurology"),
    # --- General Medicine ---
    Case("I have a fever and cough", "General Medicine"),
    Case("I need a routine checkup", "General Medicine"),
    Case("I need my annual vaccination", "General Medicine"),
    # --- Radiology ---
    Case("I need an MRI scan", "Radiology"),
    Case("can I get an x-ray of my ankle", "Radiology"),
    Case("I need an ultrasound", "Radiology"),
    # --- Deliberately deferred to the LLM (documents the coverage boundary) ---
    Case(
        "I'm not sure what's wrong, can someone help me",
        None,
        "no keyword present at all — genuinely ambiguous",
    ),
    Case(
        "I fractured my wrist",
        None,
        "inflected form ('fractured' vs the keyword 'fracture') — the table does not "
        "stem words, by design; the LLM Routing agent handles this",
    ),
    Case(
        "I've been getting severe migraines",
        None,
        "plural form ('migraines' vs the keyword 'migraine') — same reason as above",
    ),
]


def _seeded_session():
    from seed.seed_data import DEPARTMENTS

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for spec in DEPARTMENTS:
        db.add(
            Department(
                name=spec["name"],
                description=spec["description"],
                routing_keywords=spec["routing_keywords"],
                required_document_types=spec["required_document_types"],
            )
        )
    db.commit()
    return db


@dataclass
class EvalReport:
    total: int
    passed: int
    failures: list[str]

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0


def run() -> EvalReport:
    db = _seeded_session()
    failures = []
    for case in DATASET:
        department, _confidence = department_service.match_department_by_keywords(db, case.text)
        got = department.name if department else None
        if got != case.expected_department:
            failures.append(
                f"expected department={case.expected_department!r}, got {got!r}: "
                f"{case.text!r}" + (f" ({case.note})" if case.note else "")
            )
    return EvalReport(total=len(DATASET), passed=len(DATASET) - len(failures), failures=failures)


def main() -> int:
    report = run()
    print(f"Routing accuracy eval: {report.passed}/{report.total} passed ({report.pass_rate:.0%})")
    for failure in report.failures:
        print(f"  FAIL: {failure}")
    return 0 if not report.failures else 1


if __name__ == "__main__":
    sys.exit(main())
