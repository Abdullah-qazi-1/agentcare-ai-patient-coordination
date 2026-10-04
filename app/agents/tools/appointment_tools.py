"""Appointment agent tools: availability, booking, rescheduling, cancellation.

Slot IDs are never invented by the model — it can only book a slot that
`find_available_slots` actually returned from the database, and the service layer
re-validates the slot on the way in regardless.
"""

from langchain_core.tools import tool

from app.agents.context import current_context
from app.services import appointment_service, department_service, reminder_service
from app.services.errors import ServiceError


@tool
def find_available_slots(department_name: str, timing_preference: str = "") -> str:
    """Find open appointment slots in a department.

    Args:
        department_name: Exact department name, e.g. "Cardiology".
        timing_preference: The patient's timing words, e.g. "next week", "tomorrow".
            Left blank, this searches the next two weeks.
    """
    context = current_context()
    department = department_service.get_department_by_name(context.db, department_name)
    if not department:
        available = ", ".join(d.name for d in department_service.list_departments(context.db))
        return f"Error: no department named '{department_name}'. Available: {available}"

    start_from, start_before = appointment_service.parse_timing_preference(timing_preference)
    slots = appointment_service.get_available_slots(
        context.db, department_id=department.id, start_from=start_from, start_before=start_before, limit=10
    )

    if not slots:
        # Widening the search beats reporting a dead end the patient can't act on.
        slots = appointment_service.get_available_slots(context.db, department_id=department.id, limit=10)
        if not slots:
            return f"No open slots in {department.name} at all. Offer to escalate to staff."
        prefix = (
            f"No slots matched '{timing_preference}', but these are the next available "
            f"in {department.name}:\n"
        )
    else:
        prefix = f"Open slots in {department.name}:\n"

    lines = [
        f"- slot_id={s.slot_id}: {s.start_time.strftime('%a %d %b %Y, %H:%M')} with {s.doctor_name}"
        for s in slots
    ]
    context.scratch["department_id"] = department.id
    return prefix + "\n".join(lines)


@tool
def book_appointment(slot_id: int, reason: str) -> str:
    """Book a slot for the current patient.

    Args:
        slot_id: A slot_id returned by find_available_slots. Never guess one.
        reason: Short administrative reason, e.g. "cardiology follow-up".
            Describe the appointment's purpose only — never a diagnosis.
    """
    context = current_context()
    try:
        appointment = appointment_service.book_appointment(
            context.db,
            patient_id=context.patient_id,
            slot_id=slot_id,
            reason=reason,
            actor_id=context.actor_id,
            actor_label="appointment_agent",
            workflow_run_id=context.workflow_run_id,
        )
    except ServiceError as exc:
        # Conflicts are recoverable — the agent should offer another slot, not give up.
        return f"Could not book: {exc}"

    detail = appointment_service.get_appointment_detail(context.db, appointment.id)
    context.scratch["appointment_id"] = appointment.id
    return (
        f"Booked appointment #{detail.appointment_id}: {detail.department_name} with "
        f"{detail.doctor_name} on {detail.start_time.strftime('%A %d %B %Y at %H:%M')}. "
        f"Status: {detail.status.value}."
    )


@tool
def list_my_appointments() -> str:
    """List the current patient's appointments, so you can reschedule or cancel the right one."""
    context = current_context()
    appointments = appointment_service.list_patient_appointments(context.db, context.patient_id)
    if not appointments:
        return "This patient has no appointments on record."

    lines = []
    for appointment in appointments:
        try:
            detail = appointment_service.get_appointment_detail(context.db, appointment.id)
        except ServiceError:
            continue
        lines.append(
            f"- appointment_id={detail.appointment_id} [{detail.status.value}]: "
            f"{detail.department_name} with {detail.doctor_name} on "
            f"{detail.start_time.strftime('%a %d %b %Y, %H:%M')}"
        )
    return "Appointments:\n" + "\n".join(lines)


@tool
def reschedule_appointment(appointment_id: int, new_slot_id: int) -> str:
    """Move an existing appointment to a different slot.

    Args:
        appointment_id: From list_my_appointments.
        new_slot_id: From find_available_slots.
    """
    context = current_context()
    try:
        appointment = appointment_service.reschedule_appointment(
            context.db,
            appointment_id=appointment_id,
            new_slot_id=new_slot_id,
            actor_id=context.actor_id,
            actor_label="appointment_agent",
            workflow_run_id=context.workflow_run_id,
        )
    except ServiceError as exc:
        return f"Could not reschedule: {exc}"

    detail = appointment_service.get_appointment_detail(context.db, appointment.id)
    context.scratch["appointment_id"] = appointment.id
    return (
        f"Rescheduled appointment #{detail.appointment_id} to "
        f"{detail.start_time.strftime('%A %d %B %Y at %H:%M')} with {detail.doctor_name}."
    )


@tool
def cancel_appointment(appointment_id: int, reason: str = "") -> str:
    """Cancel an appointment and release its slot.

    Args:
        appointment_id: From list_my_appointments.
        reason: The patient's administrative reason for cancelling.
    """
    context = current_context()
    try:
        appointment_service.cancel_appointment(
            context.db,
            appointment_id=appointment_id,
            reason=reason,
            actor_id=context.actor_id,
            actor_label="appointment_agent",
            workflow_run_id=context.workflow_run_id,
        )
    except ServiceError as exc:
        return f"Could not cancel: {exc}"

    # An appointment that no longer exists must not keep sending reminders.
    cancelled = reminder_service.cancel_reminders_for_appointment(context.db, appointment_id)
    return (
        f"Cancelled appointment #{appointment_id} and released the slot. "
        f"{cancelled} pending reminder(s) also cancelled."
    )
