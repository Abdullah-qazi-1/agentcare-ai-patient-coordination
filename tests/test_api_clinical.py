"""Department/doctor directory: open read access, staff/admin-only management."""

from tests.conftest import auth_headers


class TestDepartments:
    def test_lists_departments(self, client, cardiology, orthopedics, patient):
        resp = client.get("/departments", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()}
        assert {"Cardiology", "Orthopedics"} <= names

    def test_requires_authentication(self, client, cardiology):
        resp = client.get("/departments")
        assert resp.status_code in (401, 403)


class TestDoctors:
    def test_lists_doctors_filtered_by_department(self, client, doctor, cardiology, orthopedics, patient):
        resp = client.get(
            "/doctors", params={"department_id": cardiology.id}, headers=auth_headers(patient.user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["id"] == doctor.id
        assert body[0]["department_id"] == cardiology.id

    def test_lists_all_doctors_without_filter(self, client, doctor, patient):
        resp = client.get("/doctors", headers=auth_headers(patient.user))
        assert resp.status_code == 200
        assert any(d["id"] == doctor.id for d in resp.json())


class TestManageDepartments:
    def test_patient_cannot_create_a_department(self, client, patient):
        resp = client.post(
            "/departments", json={"name": "Oncology"}, headers=auth_headers(patient.user)
        )
        assert resp.status_code == 403

    def test_staff_can_create_a_department(self, client, db, actor):
        resp = client.post(
            "/departments",
            json={
                "name": "Oncology",
                "description": "Cancer care.",
                "routing_keywords": ["oncology", "tumor"],
                "required_document_types": [],
            },
            headers=auth_headers(actor),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Oncology"
        assert body["active"] is True

        listed = client.get("/departments", headers=auth_headers(actor)).json()
        assert any(d["name"] == "Oncology" for d in listed)

    def test_staff_can_deactivate_a_department(self, client, db, actor, cardiology):
        resp = client.patch(
            f"/departments/{cardiology.id}/active", json={"active": False}, headers=auth_headers(actor)
        )
        assert resp.status_code == 200
        assert resp.json()["active"] is False

        active_only = client.get("/departments", headers=auth_headers(actor)).json()
        assert not any(d["id"] == cardiology.id for d in active_only)


class TestManageDoctors:
    def test_patient_cannot_create_a_doctor(self, client, patient, cardiology):
        resp = client.post(
            "/doctors",
            json={"department_id": cardiology.id, "name": "Dr. Test"},
            headers=auth_headers(patient.user),
        )
        assert resp.status_code == 403

    def test_staff_can_create_a_doctor(self, client, actor, cardiology):
        resp = client.post(
            "/doctors",
            json={"department_id": cardiology.id, "name": "Dr. New Cardiologist"},
            headers=auth_headers(actor),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Dr. New Cardiologist"
        assert body["department_id"] == cardiology.id
