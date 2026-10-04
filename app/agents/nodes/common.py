"""Shared helpers for graph nodes."""

from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage

from app.agents.context import AgentCareContext
from app.core.logging import get_logger
from app.db.models.enums import WorkflowStep
from app.services import workflow_service

logger = get_logger(__name__)

NodeFn = Callable[[dict], dict]


def build_context(state: dict, db, *, actor_id: int | None, actor_label: str) -> AgentCareContext:
    """Construct the per-run context handed to an agent's tools."""
    return AgentCareContext(
        db=db,
        patient_id=state["patient_id"],
        workflow_run_id=state["workflow_run_id"],
        actor_id=actor_id,
        actor_label=actor_label,
        pending_document_ids=list(state.get("document_ids") or []),
        scratch=dict(state.get("scratch") or {}),
    )


def invoke_agent(agent, instruction: str, context: AgentCareContext) -> tuple[Any, dict]:
    """Run an agent with a single focused instruction.

    Deliberately *not* passing accumulated conversation history: each agent gets exactly
    the facts it needs, so per-node token cost is flat regardless of how long the overall
    workflow becomes.

    Returns (structured_response, updated_scratch).
    """
    result = agent.invoke({"messages": [HumanMessage(content=instruction)]}, context=context)
    return result.get("structured_response"), context.scratch


def persist_step(
    db,
    state: dict,
    step: WorkflowStep,
    patch: dict | None = None,
    *,
    actor_label: str,
) -> None:
    """Mirror the graph's progress into the database for the Staff Dashboard."""
    try:
        workflow_service.save_state(
            db,
            workflow_run_id=state["workflow_run_id"],
            step=step,
            state_patch=patch or {},
            actor_label=actor_label,
        )
    except Exception:  # pragma: no cover - bookkeeping must not abort the run
        logger.exception("persist_step_failed", step=step.value, run=state.get("workflow_run_id"))


def node_error(state: dict, node_name: str, exc: Exception) -> dict:
    """Turn an unexpected node failure into recorded state rather than a crash.

    The run continues to the next stage where it safely can; what it must never do is
    disappear silently, so the error is written into state and surfaced to staff.
    """
    logger.exception("node_failed", node=node_name, run=state.get("workflow_run_id"))
    return {"errors": [f"{node_name}: {type(exc).__name__}: {exc}"[:400]]}
