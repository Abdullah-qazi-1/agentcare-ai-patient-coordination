"""Halt — a run stopped by escalation or rejection."""

from app.agents.nodes.common import NodeFn
from app.core.logging import get_logger
from app.services import workflow_service

logger = get_logger(__name__)


def make_halted(db) -> NodeFn:
    def halted(state: dict) -> dict:
        summary = state.get("summary") or (
            "Your request has been passed to our staff for review. Someone will contact you shortly."
        )
        try:
            workflow_service.save_state(
                db,
                workflow_run_id=state["workflow_run_id"],
                state_patch={"summary": summary, "halted": True},
                actor_label="system",
            )
        except Exception:
            logger.exception("halt_persist_failed", run=state.get("workflow_run_id"))
        return {"summary": summary, "status": state.get("status") or "awaiting_review"}

    return halted
