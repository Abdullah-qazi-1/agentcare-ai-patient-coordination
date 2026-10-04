"""Coordinator — reads the raw request and records the administrative plan."""

from app.agents.agents import coordinator_agent
from app.agents.nodes.common import NodeFn, build_context, invoke_agent, node_error, persist_step
from app.db.models.enums import WorkflowStep


def make_coordinator(db, actor_id: int | None) -> NodeFn:
    def coordinator(state: dict) -> dict:
        context = build_context(state, db, actor_id=actor_id, actor_label="coordinator_agent")
        try:
            intent, scratch = invoke_agent(
                coordinator_agent(),
                (
                    "Understand this administrative request and record the plan.\n\n"
                    f"Patient request: {state['raw_request']}\n"
                    f"Documents attached to this request: {len(state.get('document_ids') or [])}\n\n"
                    "Review the patient record and saved workflow state first, then save "
                    "your reading of the request."
                ),
                context,
            )
        except Exception as exc:
            return node_error(state, "coordinator", exc)

        if intent is None:
            return {"errors": ["coordinator: agent returned no structured intent"]}

        patch = {
            "intent": intent.model_dump(),
            "needs_appointment": intent.needs_appointment,
            # Only claim document work if files were actually attached — a patient
            # *mentioning* a report is not the same as having uploaded one.
            "needs_documents": intent.needs_document_handling and bool(state.get("document_ids")),
            "current_step": "department_routing",
            "scratch": scratch,
        }
        persist_step(
            db,
            state,
            WorkflowStep.INTENT_DETECTION,
            {"primary_action": intent.primary_action, "intent_summary": intent.summary},
            actor_label="coordinator_agent",
        )
        return patch

    return coordinator
