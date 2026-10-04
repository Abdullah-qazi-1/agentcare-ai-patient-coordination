"""Documents — files whatever was attached and checks for gaps against the routed
department's requirements."""

from app.agents.agents import document_agent
from app.agents.nodes.common import NodeFn, build_context, invoke_agent, node_error, persist_step
from app.core.logging import get_logger
from app.db.models.enums import WorkflowStep
from app.services import document_service

logger = get_logger(__name__)


def make_document(db, actor_id: int | None) -> NodeFn:
    def document(state: dict) -> dict:
        context = build_context(state, db, actor_id=actor_id, actor_label="document_agent")
        department_name = state.get("department_name") or ""

        try:
            _, scratch = invoke_agent(
                document_agent(),
                (
                    "File the documents attached to this request.\n\n"
                    f"Patient request: {state['raw_request']}\n"
                    f"Department: {department_name or 'not yet determined'}\n\n"
                    "List the attachments, classify and store each one, then check for "
                    "missing required documents."
                ),
                context,
            )
        except Exception as exc:
            return node_error(state, "document", exc)

        # Read the gap check from the database rather than the agent's prose — the
        # patient is told what to bring based on what is actually on file.
        missing: list[str] = []
        if state.get("department_id"):
            try:
                gap = document_service.check_missing_documents(
                    db, patient_id=state["patient_id"], department_id=state["department_id"]
                )
                missing = [doc_type.value for doc_type in gap.missing]
            except Exception:
                logger.exception("missing_doc_check_failed", run=state["workflow_run_id"])

        processed = len(state.get("document_ids") or [])
        persist_step(
            db,
            state,
            WorkflowStep.DOCUMENT_COORDINATION,
            {"documents_processed": processed, "missing_documents": missing},
            actor_label="document_agent",
        )
        return {
            "documents_processed": processed,
            "missing_documents": missing,
            "current_step": "follow_up",
            "scratch": scratch,
        }

    return document
