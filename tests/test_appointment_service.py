"""Slot search, booking, and the concurrency guard.

The double-booking tests are the important ones here. Slot claiming uses a single
conditional `UPDATE ... WHERE id = ? AND status = 'open'` and checks the affected row
count — atomic on both SQLite and PostgreSQL, unlike `SELECT ... FOR UPDATE`, which
SQLite silently ignores.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import AppointmentStatus, SlotStatus
from app.services import appointment_service
from app.services.errors import ConflictError, NotFoundError, ValidationError


class TestSlotSearch:
    def test_lists_open_future_slots(self, db, slots, cardiology):
        found = appointment_service.get_available_slots(db, department_id=cardiology.id)
        assert len(found) == len(slots)
        assert {s.slot_id for s in found} == {s.id for s in slots}

    def test_enriches_with_doctor_and_department(self, db, slots, cardiology, doctor):
        first = appointment_service.get_available_slots(db, department_id=cardiology.id)[0]
        assert first.doctor_name == doctor.name
        assert first.department_name == cardiology.name

    def test_returns_slots_in_chronological_order(self, db, slots, cardiology):
        found = appointment_service.get_available_slots(db, department_id=cardiology.id)
        assert found == sorted(found, key=lambda s: s.start_time)

    def test_filters_by_department(self, db, slots, orthopedics):
        assert appointment_service.get_available_slots(db, department_id=orthopedics.id) == []

    def test_excludes_past_slots(self, db, doctor, cardiology):
        past = datetime.now(UTC) - timedelta(days=2)
        appointment_service.create_slot(
            db, doctor_id=doctor.id, start_time=past,
            end_time=past + timedelta(minutes=30), actor_id=1,
        )
        assert appointment_service.get_available_slots(db, department_id=cardiology.id) == []

    def test_excludes_inactive_doctors(self, db, slots, cardiology, doctor):
        doctor.active = False
        db.commit()
        assert appointment_service.get_available_slots(db, department_id=cardiology.id) == []

    def test_excludes_inactive_departments(self, db, slots, cardiology):
        cardiology.active = False
        db.commit()
        assert appointment_service.get_available_slots(db, department_id=cardiology.id) == []

    def test_respects_time_window(self, db, slots, cardiology):
        window_end = slots[0].start_time + timedelta(minutes=1)
        found = appointment_service.get_available_slots(
            db, department_id=cardiology.id, start_before=window_end
        )
        assert [s.slot_id for s in found] == [slots[0].id]


class TestBooking:
    def test_books_a_slot(self, db, slots, patient):
        appt = appointment_service.book_appointment(
            db, patient_id=patient.id, slot_id=slots[0].id, reason="annual review"
        )
        assert appt.id is not None
        assert appt.status is AppointmentStatus.CONFIRMED
        assert appt.patient_id == patient.id
        assert appt.reason == "annual review"

    def test_marks_the_slot_booked(self, db, slots, patient):
        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        db.refresh(slots[0])
        assert slots[0].status is SlotStatus.BOOKED

    def test_booked_slot_leaves_the_available_list(self, db, slots, patient, cardiology):
        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        remaining = appointment_service.get_available_slots(db, department_id=cardiology.id)
        assert slots[0].id not in {s.slot_id for s in remaining}

    def test_writes_an_audit_event(self, db, slots, patient):
        from app.db.models import AuditEvent

        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        events = db.query(AuditEvent).filter(AuditEvent.action == "appointment_booked").all()
        assert len(events) == 1
        assert events[0].entity_id == appt.id

    def test_unknown_slot_raises(self, db, patient):
        with pytest.raises(NotFoundError):
            appointment_service.book_appointment(db, patient_id=patient.id, slot_id=99999)

    def test_past_slot_raises(self, db, doctor, patient):
        past = datetime.now(UTC) - timedelta(days=1)
        slot = appointment_service.create_slot(
            db, doctor_id=doctor.id, start_time=past,
            end_time=past + timedelta(minutes=30), actor_id=1,
        )
        with pytest.raises(ValidationError):
            appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slot.id)

    def test_reason_is_truncated_not_rejected(self, db, slots, patient):
        appt = appointment_service.book_appointment(
            db, patient_id=patient.id, slot_id=slots[0].id, reason="x" * 900
        )
        assert len(appt.reason) == 500


class TestDoubleBookingGuard:
    """The invariant that matters: one slot, one appointment."""

    def test_second_patient_cannot_take_a_booked_slot(self, db, slots, patient, other_patient):
        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        with pytest.raises(ConflictError):
            appointment_service.book_appointment(
                db, patient_id=other_patient.id, slot_id=slots[0].id
            )

    def test_no_second_appointment_row_is_created(self, db, slots, patient, other_patient):
        from app.db.models import Appointment

        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        with pytest.raises(ConflictError):
            appointment_service.book_appointment(
                db, patient_id=other_patient.id, slot_id=slots[0].id
            )
        assert db.query(Appointment).filter(Appointment.slot_id == slots[0].id).count() == 1

    def test_claim_is_single_shot(self, db, slots):
        """The atomic claim itself: the second attempt affects zero rows."""
        assert appointment_service._claim_slot(db, slots[0].id) is True
        assert appointment_service._claim_slot(db, slots[0].id) is False

    def test_same_patient_cannot_be_in_two_places_at_once(self, db, slots, patient, cardiology):
        """A conflict on the patient's own calendar rather than on the slot.

        Needs a second doctor: two overlapping slots under one doctor are rejected at
        slot-creation time, so the only way to reach this check is across doctors.
        """
        from app.db.models import Doctor

        other_doctor = Doctor(department_id=cardiology.id, name="Dr. Second")
        db.add(other_doctor)
        db.commit()

        parallel = appointment_service.create_slot(
            db,
            doctor_id=other_doctor.id,
            start_time=slots[0].start_time,
            end_time=slots[0].end_time,
            actor_id=1,
        )
        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)

        with pytest.raises(ConflictError):
            appointment_service.book_appointment(db, patient_id=patient.id, slot_id=parallel.id)

    def test_parallel_slot_stays_open_after_a_patient_conflict(
        self, db, slots, patient, other_patient, cardiology
    ):
        """A rejected booking must not consume the slot it was rejected for."""
        from app.db.models import Doctor

        other_doctor = Doctor(department_id=cardiology.id, name="Dr. Second")
        db.add(other_doctor)
        db.commit()
        parallel = appointment_service.create_slot(
            db, doctor_id=other_doctor.id, start_time=slots[0].start_time,
            end_time=slots[0].end_time, actor_id=1,
        )
        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        with pytest.raises(ConflictError):
            appointment_service.book_appointment(db, patient_id=patient.id, slot_id=parallel.id)

        # Someone else can still take it.
        booked = appointment_service.book_appointment(
            db, patient_id=other_patient.id, slot_id=parallel.id
        )
        assert booked.status is AppointmentStatus.CONFIRMED

    def test_cancelled_appointment_frees_the_slot_for_others(
        self, db, slots, patient, other_patient
    ):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        appointment_service.cancel_appointment(db, appointment_id=appt.id)
        second = appointment_service.book_appointment(
            db, patient_id=other_patient.id, slot_id=slots[0].id
        )
        assert second.status is AppointmentStatus.CONFIRMED


class TestReschedule:
    def test_moves_to_the_new_slot(self, db, slots, patient):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        moved = appointment_service.reschedule_appointment(
            db, appointment_id=appt.id, new_slot_id=slots[1].id
        )
        assert moved.slot_id == slots[1].id
        assert moved.status is AppointmentStatus.RESCHEDULED

    def test_releases_the_old_slot(self, db, slots, patient):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        appointment_service.reschedule_appointment(
            db, appointment_id=appt.id, new_slot_id=slots[1].id
        )
        db.refresh(slots[0])
        db.refresh(slots[1])
        assert slots[0].status is SlotStatus.OPEN
        assert slots[1].status is SlotStatus.BOOKED

    def test_taken_slot_leaves_the_original_intact(self, db, slots, patient, other_patient):
        """A failed claim must never leave the patient with no appointment at all."""
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        appointment_service.book_appointment(db, patient_id=other_patient.id, slot_id=slots[1].id)

        with pytest.raises(ConflictError):
            appointment_service.reschedule_appointment(
                db, appointment_id=appt.id, new_slot_id=slots[1].id
            )
        db.refresh(appt)
        db.refresh(slots[0])
        assert appt.slot_id == slots[0].id
        assert slots[0].status is SlotStatus.BOOKED

    def test_cannot_reschedule_into_the_same_slot(self, db, slots, patient):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        with pytest.raises(ValidationError):
            appointment_service.reschedule_appointment(
                db, appointment_id=appt.id, new_slot_id=slots[0].id
            )

    def test_cannot_reschedule_a_cancelled_appointment(self, db, slots, patient):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        appointment_service.cancel_appointment(db, appointment_id=appt.id)
        with pytest.raises(ValidationError):
            appointment_service.reschedule_appointment(
                db, appointment_id=appt.id, new_slot_id=slots[1].id
            )


class TestCancel:
    def test_marks_cancelled_and_frees_the_slot(self, db, slots, patient):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        cancelled = appointment_service.cancel_appointment(
            db, appointment_id=appt.id, reason="patient request"
        )
        db.refresh(slots[0])
        assert cancelled.status is AppointmentStatus.CANCELLED
        assert slots[0].status is SlotStatus.OPEN

    def test_double_cancel_raises(self, db, slots, patient):
        appt = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        appointment_service.cancel_appointment(db, appointment_id=appt.id)
        with pytest.raises(ValidationError):
            appointment_service.cancel_appointment(db, appointment_id=appt.id)


class TestSlotCreation:
    def test_rejects_overlapping_slots_for_one_doctor(self, db, slots, doctor):
        with pytest.raises(ConflictError):
            appointment_service.create_slot(
                db,
                doctor_id=doctor.id,
                start_time=slots[0].start_time,
                end_time=slots[0].end_time,
                actor_id=1,
            )

    def test_rejects_end_before_start(self, db, doctor):
        start = datetime.now(UTC) + timedelta(days=1)
        with pytest.raises(ValidationError):
            appointment_service.create_slot(
                db, doctor_id=doctor.id, start_time=start,
                end_time=start - timedelta(minutes=30), actor_id=1,
            )

    def test_rejects_unknown_doctor(self, db):
        start = datetime.now(UTC) + timedelta(days=1)
        with pytest.raises(NotFoundError):
            appointment_service.create_slot(
                db, doctor_id=99999, start_time=start,
                end_time=start + timedelta(minutes=30), actor_id=1,
            )


class TestConfirmationView:
    def test_detail_is_assembled_from_persisted_rows(
        self, db, slots, patient, doctor, cardiology
    ):
        """The patient-facing confirmation never comes from agent prose."""
        appt = appointment_service.book_appointment(
            db, patient_id=patient.id, slot_id=slots[0].id, reason="follow-up"
        )
        detail = appointment_service.get_appointment_detail(db, appt.id)
        assert detail.appointment_id == appt.id
        assert detail.doctor_name == doctor.name
        assert detail.department_name == cardiology.name
        assert detail.reason == "follow-up"
        assert detail.status is AppointmentStatus.CONFIRMED

    def test_detail_for_unknown_appointment_raises(self, db):
        with pytest.raises(NotFoundError):
            appointment_service.get_appointment_detail(db, 99999)

    def test_listing_can_exclude_cancelled(self, db, slots, patient):
        first = appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[0].id)
        appointment_service.book_appointment(db, patient_id=patient.id, slot_id=slots[2].id)
        appointment_service.cancel_appointment(db, appointment_id=first.id)

        assert len(appointment_service.list_patient_appointments(db, patient.id)) == 2
        active = appointment_service.list_patient_appointments(
            db, patient.id, include_cancelled=False
        )
        assert len(active) == 1


class TestTimingPreference:
    """Date phrases are resolved by code, not by the model — a mis-parsed date is a
    wasted trip to the hospital."""

    @pytest.mark.parametrize("phrase", ["next week", "tomorrow", "this week", "next month"])
    def test_known_phrases_produce_a_forward_window(self, phrase):
        start, end = appointment_service.parse_timing_preference(phrase)
        assert start < end

    @pytest.mark.parametrize("phrase", [None, "", "whenever suits"])
    def test_unparseable_input_still_returns_a_usable_window(self, phrase):
        start, end = appointment_service.parse_timing_preference(phrase)
        assert start < end

    def test_window_is_computed_from_the_supplied_now(self):
        now = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
        start, end = appointment_service.parse_timing_preference("next week", now=now)
        assert start >= now
