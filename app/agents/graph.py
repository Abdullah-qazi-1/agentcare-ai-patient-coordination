"""The AgentCare workflow graph.

A fixed `StateGraph`, deliberately. The *sequence* of stages is not something an LLM
improvises — every run takes the same shape, which is what makes it auditable, testable,
and safe to put in front of patients. The intelligence lives inside the nodes, where
each agent plans and calls tools freely within its own bounded remit.

Conditional edges skip stages that don't apply (no documents attached, no appointment
needed) and divert to a halt when safety or routing escalates.
"""

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agents.nodes.appointment import make_appointment
from app.agents.nodes.confirmation import make_confirmation
from app.agents.nodes.coordinator import make_coordinator
from app.agents.nodes.document import make_document
from app.agents.nodes.followup import make_followup
from app.agents.nodes.halted import make_halted
from app.agents.nodes.routing import make_routing
from app.agents.nodes.safety_gate import make_safety_gate
from app.agents.state import WorkflowState
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------------
# Checkpointer
# --------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_checkpointer():
    """Durable checkpointer — this is what makes `interrupt()` survive a restart.

    Kept in its own SQLite file rather than the application database: checkpoint blobs
    are framework-internal and churn far more than the business tables, and a judge
    inspecting `agentcare.db` should see the domain schema, not LangGraph's internals.
    """
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        path = Path("agentcare_checkpoints.db")
    else:
        # For Postgres deployments, install the `postgres` extra and swap in
        # PostgresSaver here; SQLite still works as a local fallback.
        path = Path("agentcare_checkpoints.db")

    connection = sqlite3.connect(str(path), check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    logger.info("checkpointer_ready", path=str(path))
    return saver


# --------------------------------------------------------------------------------
# Routing predicates
# --------------------------------------------------------------------------------
def _after_safety(state: dict) -> str:
    return "halted" if state.get("halted") else "coordinator"


def _after_coordinator(state: dict) -> str:
    if state.get("halted"):
        return "halted"
    # Everything needs a department, even a documents-only request, because the
    # missing-document check is department-specific.
    return "routing"


def _after_routing(state: dict) -> str:
    if state.get("halted"):
        return "halted"
    if state.get("needs_appointment"):
        return "appointment"
    if state.get("needs_documents"):
        return "document"
    return "confirmation"


def _after_appointment(state: dict) -> str:
    if state.get("halted"):
        return "halted"
    return "document" if state.get("needs_documents") else "followup"


def _after_document(state: dict) -> str:
    return "halted" if state.get("halted") else "followup"


# --------------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------------
def build_graph(db, *, actor_id: int | None = None, with_checkpointer: bool = True):
    """Compile the workflow graph bound to a live database session.

    Args:
        db: SQLAlchemy session used by every node and tool in this run.
        actor_id: Authenticated user id, recorded against actions the agents take.
        with_checkpointer: Disable only in tests that don't exercise interrupts.
    """
    builder = StateGraph(WorkflowState)

    builder.add_node("safety_gate", make_safety_gate(db, actor_id))
    builder.add_node("coordinator", make_coordinator(db, actor_id))
    builder.add_node("routing", make_routing(db, actor_id))
    builder.add_node("appointment", make_appointment(db, actor_id))
    builder.add_node("document", make_document(db, actor_id))
    builder.add_node("followup", make_followup(db, actor_id))
    builder.add_node("confirmation", make_confirmation(db))
    builder.add_node("halted", make_halted(db))

    builder.add_edge(START, "safety_gate")
    builder.add_conditional_edges(
        "safety_gate", _after_safety, {"coordinator": "coordinator", "halted": "halted"}
    )
    builder.add_conditional_edges(
        "coordinator", _after_coordinator, {"routing": "routing", "halted": "halted"}
    )
    builder.add_conditional_edges(
        "routing",
        _after_routing,
        {
            "appointment": "appointment",
            "document": "document",
            "confirmation": "confirmation",
            "halted": "halted",
        },
    )
    builder.add_conditional_edges(
        "appointment",
        _after_appointment,
        {"document": "document", "followup": "followup", "halted": "halted"},
    )
    builder.add_conditional_edges(
        "document", _after_document, {"followup": "followup", "halted": "halted"}
    )
    builder.add_edge("followup", "confirmation")
    builder.add_edge("confirmation", END)
    builder.add_edge("halted", END)

    checkpointer = get_checkpointer() if with_checkpointer else None
    return builder.compile(checkpointer=checkpointer)


def thread_config(workflow_run_id: int) -> dict[str, Any]:
    """Config that ties a graph run to its workflow.

    Using the workflow run id as `thread_id` is what lets a staff approval, arriving in
    a completely separate HTTP request minutes later, resume the exact suspended run.
    """
    return {"configurable": {"thread_id": f"workflow-{workflow_run_id}"}}
