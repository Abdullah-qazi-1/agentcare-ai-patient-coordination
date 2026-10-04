"""Request-scoped context handed to agents and read by their tools.

LangGraph's `context` channel carries this into every node and tool for a run, so tools
never need module-level globals or a thread-local session. Crucially, `patient_id` and
`actor_id` arrive from the authenticated request — **not** from anything the model
produced — so an agent cannot act on a patient it wasn't given.
"""

from dataclasses import dataclass, field
from typing import Any

from langgraph.runtime import get_runtime
from sqlalchemy.orm import Session


@dataclass
class AgentCareContext:
    """Everything the tools need that must not come from the model."""

    db: Session
    patient_id: int
    workflow_run_id: int
    actor_id: int | None = None
    actor_label: str = "agent"
    # Documents the patient attached to this request, resolved before the run starts.
    pending_document_ids: list[int] = field(default_factory=list)
    # Scratch space for values one agent computes and a later one reads (e.g. the
    # department chosen by Routing, consumed by Appointment).
    scratch: dict[str, Any] = field(default_factory=dict)


def current_context() -> AgentCareContext:
    """Fetch the active run's context from inside a tool."""
    runtime = get_runtime(AgentCareContext)
    context = runtime.context
    if context is None:
        raise RuntimeError(
            "No AgentCareContext bound to this run. Agents must be invoked with "
            "context=AgentCareContext(...)."
        )
    return context
