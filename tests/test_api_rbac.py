"""RBAC: role gates, ownership checks, and the access_denied audit trail."""

from app.db.models import AuditEvent
from tests.conftest import auth_headers


class TestRoleGates:
    def test_patient_cannot_list_all_patients(self, client, db, patient):
        resp = client.get("/patients", headers=auth_headers(patient.user))
        assert resp.status_code == 403

    def test_staff_can_list_all_patients(self, client, db, patient, actor):
        resp = client.get("/patients", headers=auth_headers(actor))
        assert resp.status_code == 200

    def test_access_denied_writes_an_audit_event(self, client, db, patient):
        client.get("/patients", headers=auth_headers(patient.user))
        events = db.query(AuditEvent).filter(AuditEvent.action == "access_denied").all()
        assert len(events) == 1
        assert events[0].actor_id == patient.user.id


class TestOwnership:
    def test_patient_cannot_read_another_patients_profile(self, client, db, patient, other_patient):
        resp = client.get(f"/patients/{other_patient.id}", headers=auth_headers(patient.user))
        assert resp.status_code == 403

    def test_patient_can_read_their_own_profile(self, client, db, patient):
        resp = client.get(f"/patients/{patient.id}", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        assert resp.json()["id"] == patient.id

    def test_staff_can_read_any_patients_profile(self, client, db, patient, actor):
        resp = client.get(f"/patients/{patient.id}", headers=auth_headers(actor))
        assert resp.status_code == 200
