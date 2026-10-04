"""Follow-up agent tools: reminders, follow-up tasks, notification dispatch.

Reminder timing is computed from the appointment's persisted start time, not from the
model's sense of when "a day before" falls — date arithmetic is something code gets
right every time.
"""

from langchain_core.tools import tool

from app.agents.context import current_context
from app.services import appointment_service, reminder_service
from app.services.errors import ServiceError


@tool
def create_appointment_reminder(appointment_id: int, days_before: int = 1) -> str:
    """Schedule a reminder ahead of an appointment.

    Args:
        appointment_id: The appointment to remind about.
        days_before: How many days before the appointment to send it (0-30).
    """
    context = current_context()
    try:
        reminder = reminder_service.create_appointment_reminder(
            context.db,
            patient_id=context.patient_id,
            appointment_id=appointment_id,
            days_before=days_before,
            workflow_run_id=context.workflow_run_id,
        )
    except ServiceError as exc:
        return f"Could not create reminder: {exc}"

    context.scratch["reminder_id"] = reminder.id
    return (
        f"Reminder #{reminder.id} scheduled for "
        f"{reminder.scheduled_at.strftime('%A %d %B %Y at %H:%M')} "
        f"({days_before} day(s) before appointment #{appointment_id})."
    )


@tool
def schedule_followup_task(days_after: int, appointment_id: int = 0) -> str:
    """Schedule an administrative follow-up task.

    This is for coordination only — prompting the patient to book their next routine
    review, for example. Never schedule follow-ups that imply a clinical instruction.

    Args:
        days_after: Days from now for the follow-up (must be at least 1).
        appointment_id: Related appointment, or 0 if not tied to one.
    """
    context = current_context()
    try:
        reminder = reminder_service.schedule_followup_task(
            context.db,
            patient_id=context.patient_id,
            days_after=days_after,
            appointment_id=appointment_id or None,
            workflow_run_id=context.workflow_run_id,
        )
    except ServiceError as exc:
        return f"Could not schedule follow-up: {exc}"

    return (
        f"Follow-up task #{reminder.id} scheduled for "
        f"{reminder.scheduled_at.strftime('%d %B %Y')} ({days_after} days from now)."
    )


@tool
def send_notification(reminder_id: int, message: str) -> str:
    """Dispatch a reminder to the patient now.

    Args:
        reminder_id: The reminder to send.
        message: Administrative message text. Confirmations and logistics only —
            no clinical instructions of any kind.
    """
    context = current_context()
    try:
        notification = reminder_service.dispatch_notification(
            context.db, reminder_id=reminder_id, message=message
        )
    except ServiceError as exc:
        return f"Could not send notification: {exc}"

    return f"Notification #{notification.id} sent via {notification.channel}."


@tool
def list_my_reminders() -> str:
    """List reminders and follow-up tasks already scheduled for this patient.

    Check this before creating a reminder, so the patient isn't messaged twice about
    the same appointment.
    """
    context = current_context()
    reminders = reminder_service.list_patient_reminders(context.db, context.patient_id)
    if not reminders:
        return "No reminders are scheduled for this patient."

    lines = [
        f"- reminder_id={r.id} [{r.status.value}] {r.reminder_type.value} on "
        f"{r.scheduled_at.strftime('%d %b %Y, %H:%M')}"
        + (f" (appointment #{r.appointment_id})" if r.appointment_id else "")
        for r in reminders
    ]
    return "Scheduled reminders:\n" + "\n".join(lines)


@tool
def get_appointment_summary(appointment_id: int) -> str:
    """Read back a confirmed appointment from the database.

    Use this to build a confirmation from what was actually persisted, rather than from
    memory of what you intended to do.
    """
    context = current_context()
    try:
        detail = appointment_service.get_appointment_detail(context.db, appointment_id)
    except ServiceError as exc:
        return f"Error: {exc}"

    return (
        f"Appointment #{detail.appointment_id} [{detail.status.value}]: "
        f"{detail.department_name} with {detail.doctor_name} on "
        f"{detail.start_time.strftime('%A %d %B %Y at %H:%M')}. Reason: {detail.reason or 'not stated'}."
    )
