"""Booking through the API exercises the real appointment_service — the same code
path a staff member or an agent tool uses, per the "one code path" design."""

from datetime import UTC, datetime, timedelta

from app.db.models import SlotStatus
from tests.conftest import auth_headers


class TestBookAppointment:
    def test_book_flips_the_slot_to_booked(self, client, db, patient, slots):
        resp = client.post("/appointments", json={"slot_id": slots[0].id, "reason": "checkup"},
                            headers=auth_headers(patient.user))
        assert resp.status_code == 200
        db.refresh(slots[0])
        assert slots[0].status == SlotStatus.BOOKED

    def test_second_booking_of_the_same_slot_conflicts(self, client, db, patient, other_patient, slots):
        first = client.post(
            "/appointments", json={"slot_id": slots[0].id}, headers=auth_headers(patient.user)
        )
        assert first.status_code == 200

        second = client.post(
            "/appointments", json={"slot_id": slots[0].id}, headers=auth_headers(other_patient.user)
        )
        assert second.status_code == 409

    def test_unknown_slot_is_404(self, client, patient):
        resp = client.post("/appointments", json={"slot_id": 999999}, headers=auth_headers(patient.user))
        assert resp.status_code == 404

    def test_staff_cannot_book_for_themselves(self, client, actor, slots):
        # /appointments always books for the authenticated patient; staff have no
        # patient_id, so the route must reject rather than book against `None`.
        resp = client.post("/appointments", json={"slot_id": slots[0].id}, headers=auth_headers(actor))
        assert resp.status_code == 403


class TestListAndCancel:
    def test_booked_appointment_appears_in_my_list_and_can_be_cancelled(self, client, db, patient, slots):
        book = client.post("/appointments", json={"slot_id": slots[0].id}, headers=auth_headers(patient.user))
        appointment_id = book.json()["id"]

        listed = client.get("/appointments/me", headers=auth_headers(patient.user))
        assert any(a["id"] == appointment_id for a in listed.json())

        cancel = client.post(
            f"/appointments/{appointment_id}/cancel", json={}, headers=auth_headers(patient.user)
        )
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancelled"

    def test_patient_cannot_cancel_another_patients_appointment(self, client, patient, other_patient, slots):
        book = client.post("/appointments", json={"slot_id": slots[0].id}, headers=auth_headers(patient.user))
        appointment_id = book.json()["id"]

        resp = client.post(
            f"/appointments/{appointment_id}/cancel", json={}, headers=auth_headers(other_patient.user)
        )
        assert resp.status_code == 404


class TestCreateSlot:
    def test_patient_cannot_create_a_slot(self, client, patient, doctor):
        start = datetime.now(UTC) + timedelta(days=3)
        resp = client.post(
            "/appointments/slots",
            json={
                "doctor_id": doctor.id,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=30)).isoformat(),
            },
            headers=auth_headers(patient.user),
        )
        assert resp.status_code == 403

    def test_staff_can_create_a_slot_and_it_becomes_bookable(self, client, actor, doctor, cardiology):
        start = datetime.now(UTC) + timedelta(days=3)
        create = client.post(
            "/appointments/slots",
            json={
                "doctor_id": doctor.id,
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=30)).isoformat(),
            },
            headers=auth_headers(actor),
        )
        assert create.status_code == 200
        assert create.json()["status"] == "open"

        listed = client.get(
            "/appointments/slots", params={"doctor_id": doctor.id}, headers=auth_headers(actor)
        ).json()
        assert any(s["slot_id"] == create.json()["id"] for s in listed)
