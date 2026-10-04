"""Appointment — books, reschedules, or cancels using real slots only."""

from app.agents.agents import appointment_agent
from app.agents.nodes.common import NodeFn, build_context, invoke_agent, node_error, persist_step
from app.db.models.enums import WorkflowStep


def make_appointment(db, actor_id: int | None) -> NodeFn:
    def appointment(state: dict) -> dict:
        intent = state.get("intent") or {}
        context = build_context(state, db, actor_id=actor_id, actor_label="appointment_agent")

        action = intent.get("primary_action", "book_appointment")
        try:
            outcome, scratch = invoke_agent(
                appointment_agent(),
                (
                    f"Handle this appointment request. Requested action: {action}.\n\n"
                    f"Patient request: {state['raw_request']}\n"
                    f"Department: {state.get('department_name')}\n"
                    f"Timing preference: {intent.get('timing_preference') or 'not stated'}\n\n"
                    "Find real slots before booking, and use only slot IDs the tools return."
                ),
                context,
            )
        except Exception as exc:
            return node_error(state, "appointment", exc)

        if outcome is None:
            return {"errors": ["appointment: agent returned no outcome"]}

        # Trust the scratch value written by the booking tool over the model's own
        # report of what it did — the tool only writes it on a real database commit.
        appointment_id = scratch.get("appointment_id") or outcome.appointment_id

        persist_step(
            db,
            state,
            WorkflowStep.APPOINTMENT,
            {"appointment_id": appointment_id, "appointment_action": outcome.action_taken},
            actor_label="appointment_agent",
        )
        return {
            "appointment_id": appointment_id,
            "appointment_action": outcome.action_taken,
            "current_step": "document_coordination",
            "scratch": scratch,
        }

    return appointment
