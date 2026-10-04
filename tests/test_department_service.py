"""Deterministic department routing — the fast-path that keeps routing cheap.

Most requests name their department outright, so they never need an LLM call. The
tie-breaking behaviour is the subtle part: two departments matching equally returns
no match, so the caller escalates rather than silently picking the first one. An
arbitrary choice here would be a routing error the patient never sees explained.
"""

import pytest

from app.services import department_service
from app.services.errors import ConflictError, NotFoundError


class TestKeywordMatching:
    def test_exact_department_name_scores_highest(self, db, cardiology):
        dept, confidence = department_service.match_department_by_keywords(
            db, "I need a Cardiology appointment next week"
        )
        assert dept.id == cardiology.id
        assert confidence == 1.0

    def test_keyword_hit_scores_lower_than_a_name_match(self, db, cardiology):
        dept, confidence = department_service.match_department_by_keywords(
            db, "my heart has been racing"
        )
        assert dept.id == cardiology.id
        assert confidence == 0.8

    def test_matching_is_case_insensitive(self, db, cardiology):
        dept, _ = department_service.match_department_by_keywords(db, "CARDIOLOGY please")
        assert dept.id == cardiology.id

    def test_no_match_returns_none(self, db, cardiology, orthopedics):
        dept, confidence = department_service.match_department_by_keywords(
            db, "I would like to schedule something"
        )
        assert dept is None
        assert confidence == 0.0

    def test_word_boundaries_prevent_substring_matches(self, db, cardiology):
        """'heart' must not match inside 'hearten' or 'heartland'."""
        dept, _ = department_service.match_department_by_keywords(
            db, "the heartland clinic sent a letter"
        )
        assert dept is None

    def test_more_keyword_hits_wins(self, db, cardiology, orthopedics):
        dept, confidence = department_service.match_department_by_keywords(
            db, "cardiac issues with palpitations and my ecg"
        )
        assert dept.id == cardiology.id
        assert confidence == 0.8

    def test_a_tie_escalates_instead_of_guessing(self, db, cardiology, orthopedics):
        """One hit each — returning either would be an unexplained routing error."""
        dept, confidence = department_service.match_department_by_keywords(
            db, "my knee hurts and my heart races"
        )
        assert dept is None
        assert confidence == 0.0

    def test_inactive_departments_are_never_matched(self, db, cardiology):
        cardiology.active = False
        db.commit()
        dept, _ = department_service.match_department_by_keywords(db, "cardiology please")
        assert dept is None

    def test_empty_text_returns_no_match(self, db, cardiology):
        dept, confidence = department_service.match_department_by_keywords(db, "")
        assert dept is None
        assert confidence == 0.0


class TestDepartmentCrud:
    def test_creates_with_keywords(self, db, actor):
        dept = department_service.create_department(
            db,
            name="Neurology",
            description="Nervous system.",
            routing_keywords=["migraine", "seizure"],
            actor_id=actor.id,
        )
        assert dept.id is not None
        assert dept.routing_keywords == ["migraine", "seizure"]
        assert dept.active is True

    def test_duplicate_name_raises(self, db, actor, cardiology):
        with pytest.raises(ConflictError):
            department_service.create_department(db, name="Cardiology", actor_id=actor.id)

    def test_lookup_by_name_is_case_insensitive(self, db, cardiology):
        assert department_service.get_department_by_name(db, "cardiology").id == cardiology.id

    def test_unknown_department_raises(self, db):
        with pytest.raises(NotFoundError):
            department_service.get_department(db, 99999)

    def test_listing_hides_inactive_by_default(self, db, cardiology, orthopedics):
        department_service.set_department_active(db, orthopedics.id, False, actor_id=1)
        assert [d.id for d in department_service.list_departments(db)] == [cardiology.id]
        assert len(department_service.list_departments(db, active_only=False)) == 2

    def test_listing_is_alphabetical(self, db, cardiology, orthopedics):
        names = [d.name for d in department_service.list_departments(db)]
        assert names == sorted(names)


class TestDoctors:
    def test_creates_under_a_department(self, db, actor, cardiology):
        doc = department_service.create_doctor(
            db, department_id=cardiology.id, name="Dr. New", actor_id=actor.id
        )
        assert doc.department_id == cardiology.id

    def test_rejects_an_unknown_department(self, db, actor):
        with pytest.raises(NotFoundError):
            department_service.create_doctor(
                db, department_id=99999, name="Dr. Nowhere", actor_id=actor.id
            )

    def test_lists_scoped_to_a_department(self, db, actor, cardiology, orthopedics, doctor):
        department_service.create_doctor(
            db, department_id=orthopedics.id, name="Dr. Bones", actor_id=actor.id
        )
        cardio_docs = department_service.list_doctors(db, department_id=cardiology.id)
        assert [d.id for d in cardio_docs] == [doctor.id]

    def test_inactive_doctors_are_hidden_by_default(self, db, cardiology, doctor):
        doctor.active = False
        db.commit()
        assert department_service.list_doctors(db, department_id=cardiology.id) == []
        assert len(department_service.list_doctors(db, department_id=cardiology.id, active_only=False)) == 1
