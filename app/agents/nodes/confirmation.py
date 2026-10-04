"""Confirmation — deterministic, assembled from persisted rows only."""

from app.agents.nodes.common import NodeFn
from app.core.logging import get_logger
from app.services import appointment_service, workflow_service

logger = get_logger(__name__)


def make_confirmation(db) -> NodeFn:
    """Build the patient-facing summary from the database, never from agent prose.

    This is the check that catches a silently failed booking: if the appointment row
    isn't there, the confirmation cannot claim it is.
    """

    def confirmation(state: dict) -> dict:
        parts: list[str] = []

        appointment_id = state.get("appointment_id")
        if appointment_id:
            try:
                detail = appointment_service.get_appointment_detail(db, appointment_id)
                parts.append(
                    f"Your {detail.department_name} appointment is {detail.status.value}: "
                    f"{detail.start_time.strftime('%A %d %B %Y at %H:%M')} with {detail.doctor_name} "
                    f"(reference #{detail.appointment_id})."
                )
            except Exception:
                logger.exception("confirmation_lookup_failed", appointment_id=appointment_id)
                parts.append(
                    "We could not confirm your appointment details. A staff member will contact you."
                )
        elif state.get("needs_appointment"):
            parts.append("We could not complete your appointment booking. Our staff will follow up.")

        processed = state.get("documents_processed") or 0
        if processed:
            parts.append(f"{processed} document(s) received and filed.")

        missing = state.get("missing_documents") or []
        if missing:
            readable = ", ".join(m.replace("_", " ") for m in missing)
            parts.append(f"Still needed before your visit: {readable}.")

        if state.get("reminder_id"):
            parts.append("A reminder has been scheduled ahead of your appointment.")

        if not parts:
            parts.append("Your request has been recorded and our staff will follow up.")

        summary = " ".join(parts)
        workflow_service.complete_run(db, workflow_run_id=state["workflow_run_id"], summary=summary)
        return {"summary": summary, "status": "completed", "current_step": "confirmation"}

    return confirmation
