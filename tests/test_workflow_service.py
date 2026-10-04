"""Workflow run creation, relevant-document attachment, and doctor assignment.

`create_run` used to accept no `document_ids` at all — the patient's chosen subset of
documents for a request lived only in transient LangGraph state and was never a real
relation (see app/services/workflow_service.py:_attach_relevant_documents). These tests
cover the DB-relation side of that; the graph-state side is covered by test_graph.py.
"""

import pytest
from sqlalchemy import select

from app.db.models import AuditEvent, DocumentType
from app.services import document_service, workflow_service
from app.services.errors import NotFoundError, ValidationError


@pytest.fixture
def document(db, patient, storage):
    return document_service.store_document(
        db, patient_id=patient.id, filename="ecg.pdf", content=b"ecg bytes",
        document_type=DocumentType.ECG_REPORT,
    )


@pytest.fixture
def other_document(db, other_patient, storage):
    return document_service.store_document(
        db, patient_id=other_patient.id, filename="blood.pdf", content=b"blood bytes",
        document_type=DocumentType.BLOOD_REPORT,
    )


class TestCreateRunWithDocuments:
    def test_document_ids_are_persisted_as_relevant_to_the_run(self, db, patient, document):
        run = workflow_service.create_run(
            db, patient_id=patient.id, raw_request="book cardiology", document_ids=[document.id]
        )
        relevant = workflow_service.list_relevant_documents(db, run.id)
        assert [d.id for d in relevant] == [document.id]

    def test_only_the_patients_own_documents_are_attached(self, db, patient, document, other_document):
        """A stray or other-patient document id must be silently skipped, not trusted."""
        run = workflow_service.create_run(
            db, patient_id=patient.id, raw_request="book cardiology",
            document_ids=[document.id, other_document.id, 999999],
        )
        relevant = workflow_service.list_relevant_documents(db, run.id)
        assert [d.id for d in relevant] == [document.id]

    def test_no_document_ids_means_no_relevant_documents(self, db, patient):
        run = workflow_service.create_run(db, patient_id=patient.id, raw_request="book cardiology")
        assert workflow_service.list_relevant_documents(db, run.id) == []

    def test_relevant_documents_do_not_leak_across_requests(self, db, patient, document):
        """A document attached to one request must not appear on an unrelated one."""
        run_with_doc = workflow_service.create_run(
            db, patient_id=patient.id, raw_request="request one", document_ids=[document.id]
        )
        run_without_doc = workflow_service.create_run(
            db, patient_id=patient.id, raw_request="request two"
        )
        assert len(workflow_service.list_relevant_documents(db, run_with_doc.id)) == 1
        assert workflow_service.list_relevant_documents(db, run_without_doc.id) == []


class TestAssignDoctor:
    def test_assigns_without_booking_a_slot(self, db, patient, doctor, actor):
        run = workflow_service.create_run(db, patient_id=patient.id, raw_request="book cardiology")
        updated = workflow_service.assign_doctor(
            db, workflow_run_id=run.id, doctor_id=doctor.id, actor_id=actor.id, note="Best fit for ECG."
        )
        assert updated.assigned_doctor_id == doctor.id
        assert updated.assigned_by == actor.id
        assert updated.assigned_at is not None
        assert updated.assignment_note == "Best fit for ECG."

    def test_unknown_doctor_is_404(self, db, patient, actor):
        run = workflow_service.create_run(db, patient_id=patient.id, raw_request="book cardiology")
        with pytest.raises(NotFoundError):
            workflow_service.assign_doctor(db, workflow_run_id=run.id, doctor_id=999999, actor_id=actor.id)

    def test_inactive_doctor_is_rejected(self, db, patient, doctor, actor):
        doctor.active = False
        db.commit()
        run = workflow_service.create_run(db, patient_id=patient.id, raw_request="book cardiology")
        with pytest.raises(ValidationError):
            workflow_service.assign_doctor(db, workflow_run_id=run.id, doctor_id=doctor.id, actor_id=actor.id)

    def test_assignment_is_audited(self, db, patient, doctor, actor):
        run = workflow_service.create_run(db, patient_id=patient.id, raw_request="book cardiology")
        workflow_service.assign_doctor(db, workflow_run_id=run.id, doctor_id=doctor.id, actor_id=actor.id)

        recorded = db.execute(
            select(AuditEvent).where(AuditEvent.action == "doctor_assigned")
        ).scalars().all()
        assert len(recorded) == 1
        assert recorded[0].event_metadata["doctor_id"] == doctor.id
