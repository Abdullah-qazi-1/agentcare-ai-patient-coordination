"""Follow-up — schedules a reminder for a confirmed appointment, if there is one."""

from app.agents.agents import followup_agent
from app.agents.nodes.common import NodeFn, build_context, invoke_agent, node_error, persist_step
from app.db.models.enums import WorkflowStep


def make_followup(db, actor_id: int | None) -> NodeFn:
    def followup(state: dict) -> dict:
        appointment_id = state.get("appointment_id")
        if not appointment_id:
            # Nothing was booked, so there is nothing to remind anyone about.
            return {"current_step": "confirmation"}

        context = build_context(state, db, actor_id=actor_id, actor_label="followup_agent")
        missing = state.get("missing_documents") or []
        try:
            _, scratch = invoke_agent(
                followup_agent(),
                (
                    "Set up reminders for this confirmed appointment.\n\n"
                    f"Appointment ID: {appointment_id}\n"
                    f"Department: {state.get('department_name')}\n"
                    f"Documents still outstanding: {', '.join(missing) if missing else 'none'}\n\n"
                    "Check existing reminders first to avoid duplicates, then schedule "
                    "an appointment reminder."
                ),
                context,
            )
        except Exception as exc:
            return node_error(state, "followup", exc)

        persist_step(
            db,
            state,
            WorkflowStep.FOLLOW_UP,
            {"reminder_id": scratch.get("reminder_id")},
            actor_label="followup_agent",
        )
        return {
            "reminder_id": scratch.get("reminder_id"),
            "current_step": "confirmation",
            "scratch": scratch,
        }

    return followup
