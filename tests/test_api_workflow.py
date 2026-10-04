"""Workflow listing and the `/workflow/{id}/resume` guard rails.

`/workflow/submit` itself needs a live LLM API key (exercised manually, and by
`tests/test_graph.py` with scripted agents standing in for the LLM) so it isn't
re-tested here. What's covered is what the route does *before* ever touching the
graph: ownership, and the two guards that stop a resume from silently no-op'ing —
mirroring `app/cli.py`'s own `_has_pending_interrupt` check.
"""

from app.db.models import (
    Escalation,
    EscalationReason,
    EscalationStatus,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
)
from tests.conftest import auth_headers


def _make_run(db, patient, *, status=WorkflowStatus.IN_PROGRESS, step=WorkflowStep.REGISTRATION):
    run = WorkflowRun(
        patient_id=patient.id,
        raw_request="test request",
        current_step=step,
        status=status,
        state_snapshot={},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


class TestListRuns:
    def test_patient_only_sees_their_own_runs(self, client, db, patient, other_patient):
        _make_run(db, patient)
        _make_run(db, other_patient)

        resp = client.get("/workflow/me", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_patient_cannot_list_all_runs(self, client, patient):
        resp = client.get("/workflow", headers=auth_headers(patient.user))
        assert resp.status_code == 403

    def test_staff_sees_all_runs(self, client, db, patient, other_patient, actor):
        _make_run(db, patient)
        _make_run(db, other_patient)

        resp = client.get("/workflow", headers=auth_headers(actor))
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_patient_cannot_read_another_patients_run(self, client, db, patient, other_patient):
        run = _make_run(db, other_patient)
        resp = client.get(f"/workflow/{run.id}", headers=auth_headers(patient.user))
        assert resp.status_code == 404


class TestResumeGuards:
    def test_patient_cannot_resume(self, client, db, patient):
        run = _make_run(db, patient, status=WorkflowStatus.AWAITING_REVIEW)
        resp = client.post(
            f"/workflow/{run.id}/resume", json={"approved": True}, headers=auth_headers(patient.user)
        )
        assert resp.status_code == 403

    def test_resume_with_a_pending_escalation_is_refused_409(self, db, client, patient, actor):
        """A pending safety escalation has to go through /escalations/{id}/resolve —
        resuming the raw graph here would race with that and is refused instead."""
        run = _make_run(db, patient, status=WorkflowStatus.AWAITING_REVIEW)
        db.add(
            Escalation(
                workflow_run_id=run.id,
                reason=EscalationReason.EMERGENCY_LANGUAGE,
                detail="test",
                status=EscalationStatus.PENDING,
            )
        )
        db.commit()

        resp = client.post(f"/workflow/{run.id}/resume", json={"approved": True}, headers=auth_headers(actor))
        assert resp.status_code == 409
        assert "escalations" in resp.json()["detail"]

    def test_resume_with_no_suspended_checkpoint_is_refused_409(
        self, db, client, patient, actor, in_memory_checkpointer
    ):
        """No pending escalation and no real graph checkpoint for this run id — a
        decision here would be a silent no-op, so it's refused rather than pretending
        to work."""
        run = _make_run(db, patient, status=WorkflowStatus.IN_PROGRESS)
        resp = client.post(f"/workflow/{run.id}/resume", json={"approved": True}, headers=auth_headers(actor))
        assert resp.status_code == 409
        assert "not suspended" in resp.json()["detail"]

    def test_resume_of_unknown_run_is_404(self, client, actor):
        resp = client.post("/workflow/999999/resume", json={"approved": True}, headers=auth_headers(actor))
        assert resp.status_code == 404


class TestRelevantDocuments:
    def test_owner_sees_only_documents_attached_to_that_run(self, client, db, patient, storage):
        from app.db.models import DocumentType
        from app.services import document_service, workflow_service

        attached = document_service.store_document(
            db, patient_id=patient.id, filename="ecg.pdf", content=b"ecg",
            document_type=DocumentType.ECG_REPORT,
        )
        # A second document the patient has but did NOT attach to this request.
        document_service.store_document(
            db, patient_id=patient.id, filename="unrelated.pdf", content=b"other",
            document_type=DocumentType.OTHER,
        )
        run = workflow_service.create_run(
            db, patient_id=patient.id, raw_request="book cardiology", document_ids=[attached.id]
        )

        resp = client.get(f"/workflow/{run.id}/documents", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == attached.id

    def test_patient_cannot_see_another_patients_run_documents(self, client, db, patient, other_patient):
        run = _make_run(db, other_patient)
        resp = client.get(f"/workflow/{run.id}/documents", headers=auth_headers(patient.user))
        assert resp.status_code == 404

    def test_staff_can_see_any_patients_run_documents(self, client, db, patient, actor):
        run = _make_run(db, patient)
        resp = client.get(f"/workflow/{run.id}/documents", headers=auth_headers(actor))
        assert resp.status_code == 200
        assert resp.json() == []


class TestAssignDoctor:
    def test_staff_can_assign_a_doctor(self, client, db, patient, doctor, actor):
        run = _make_run(db, patient)
        resp = client.post(
            f"/workflow/{run.id}/assign-doctor",
            json={"doctor_id": doctor.id, "note": "Good fit."},
            headers=auth_headers(actor),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["assigned_doctor_id"] == doctor.id
        assert body["assignment_note"] == "Good fit."

    def test_patient_cannot_assign_a_doctor(self, client, db, patient, doctor):
        run = _make_run(db, patient)
        resp = client.post(
            f"/workflow/{run.id}/assign-doctor",
            json={"doctor_id": doctor.id},
            headers=auth_headers(patient.user),
        )
        assert resp.status_code == 403

    def test_inactive_doctor_is_rejected(self, client, db, patient, doctor, actor):
        doctor.active = False
        db.commit()
        run = _make_run(db, patient)
        resp = client.post(
            f"/workflow/{run.id}/assign-doctor",
            json={"doctor_id": doctor.id},
            headers=auth_headers(actor),
        )
        assert resp.status_code == 422


class TestLineage:
    def test_owner_can_see_their_own_lineage(self, client, db, patient):
        from app.services import audit_service

        run = _make_run(db, patient)
        audit_service.record(
            db,
            action="tool_call:find_available_slots",
            entity_type="agent_tool",
            entity_id=run.id,
            actor_label="appointment_agent",
            metadata={
                "tool": "find_available_slots",
                "args": {"department_name": "Cardiology"},
                "result": "Open slots in Cardiology: slot_id=1 with Dr. Ayesha Malik",
                "status": "succeeded",
                "workflow_run_id": run.id,
            },
        )

        resp = client.get(f"/workflow/{run.id}/lineage", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["step"] == "Checked doctor availability"
        assert "Dr. Ayesha Malik" in body[0]["detail"]

    def test_patient_cannot_see_another_patients_lineage(self, client, db, patient, other_patient):
        run = _make_run(db, other_patient)
        resp = client.get(f"/workflow/{run.id}/lineage", headers=auth_headers(patient.user))
        assert resp.status_code == 404

    def test_staff_can_see_any_patients_lineage(self, client, db, patient, actor):
        run = _make_run(db, patient)
        resp = client.get(f"/workflow/{run.id}/lineage", headers=auth_headers(actor))
        assert resp.status_code == 200

    def test_a_run_with_no_lineage_worthy_tool_calls_is_an_empty_list(self, client, db, patient):
        run = _make_run(db, patient)
        resp = client.get(f"/workflow/{run.id}/lineage", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        assert resp.json() == []
