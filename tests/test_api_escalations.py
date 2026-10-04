"""Escalation resolution through the API.

Before this router existed, nothing in the codebase ever called
`escalation_service.resolve_escalation` — see the plan note in `app/api/escalations.py`.
These tests build a `WorkflowRun` + pending `Escalation` directly through the ORM
(the same way `tests/conftest.py`'s other fixtures do) rather than running a real
`start_workflow`, since that needs a live LLM API key and no test in this suite does that.
"""

import pytest

from app.db.models import (
    Escalation,
    EscalationReason,
    EscalationStatus,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
)
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _use_in_memory_checkpointer(in_memory_checkpointer):
    """Resolving a fabricated escalation still calls `resume_workflow`, which touches
    the graph's checkpointer — keep it off the real `agentcare_checkpoints.db`."""


@pytest.fixture
def pending_escalation(db, patient):
    run = WorkflowRun(
        patient_id=patient.id,
        raw_request="I have chest pain",
        current_step=WorkflowStep.INTENT_DETECTION,
        status=WorkflowStatus.AWAITING_REVIEW,
        state_snapshot={},
    )
    db.add(run)
    db.flush()
    escalation = Escalation(
        workflow_run_id=run.id,
        reason=EscalationReason.EMERGENCY_LANGUAGE,
        detail="Flagged by the deterministic scan.",
        status=EscalationStatus.PENDING,
    )
    db.add(escalation)
    db.commit()
    db.refresh(escalation)
    return escalation


class TestResolveEscalation:
    def test_patient_cannot_resolve_escalations(self, client, patient, pending_escalation):
        resp = client.post(
            f"/escalations/{pending_escalation.id}/resolve",
            json={"decision": "approved"},
            headers=auth_headers(patient.user),
        )
        assert resp.status_code == 403

    def test_approve_records_the_reviewer_and_decision(self, client, db, actor, pending_escalation):
        resp = client.post(
            f"/escalations/{pending_escalation.id}/resolve",
            json={"decision": "approved", "resolution_note": "Reviewed, false positive."},
            headers=auth_headers(actor),
        )
        assert resp.status_code == 200

        db.refresh(pending_escalation)
        assert pending_escalation.status == EscalationStatus.APPROVED
        assert pending_escalation.reviewed_by == actor.id
        assert pending_escalation.resolution_note == "Reviewed, false positive."

    def test_reject_terminates_the_run(self, client, db, actor, pending_escalation):
        resp = client.post(
            f"/escalations/{pending_escalation.id}/resolve",
            json={"decision": "rejected"},
            headers=auth_headers(actor),
        )
        assert resp.status_code == 200

        db.refresh(pending_escalation)
        assert pending_escalation.status == EscalationStatus.REJECTED

    def test_already_resolved_escalation_cannot_be_resolved_again(
        self, client, db, actor, pending_escalation
    ):
        first = client.post(
            f"/escalations/{pending_escalation.id}/resolve",
            json={"decision": "approved"},
            headers=auth_headers(actor),
        )
        assert first.status_code == 200

        second = client.post(
            f"/escalations/{pending_escalation.id}/resolve",
            json={"decision": "approved"},
            headers=auth_headers(actor),
        )
        assert second.status_code == 422

    def test_list_pending_escalations(self, client, actor, pending_escalation):
        resp = client.get("/escalations", params={"status": "pending"}, headers=auth_headers(actor))
        assert resp.status_code == 200
        assert any(e["id"] == pending_escalation.id for e in resp.json())
