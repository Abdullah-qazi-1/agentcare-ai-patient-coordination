"""Token budget middleware — a hard, persisted ceiling on what one request may cost.

`ModelCallLimitMiddleware` caps how *many* calls an agent makes, but says nothing about
how large they are: eight calls with a huge context can cost more than forty small ones.
This middleware caps the thing that actually appears on the bill — total tokens across
every agent in a workflow run.

The running total lives in `WorkflowRun.state_snapshot`, not in memory, for two reasons:
the graph spans multiple agents each with their own fresh context, so an in-memory
counter would reset between stages; and a persisted number is one staff can actually
see. "Optimum token use" becomes evidence rather than a claim.

On exceeding the budget the run escalates to a human rather than failing silently or
continuing to spend.
"""

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from app.agents.context import AgentCareContext
from app.core.logging import get_logger
from app.db.models.enums import EscalationReason
from app.services import escalation_service, workflow_service

logger = get_logger(__name__)

_SNAPSHOT_KEY = "token_usage"


def read_usage(db, workflow_run_id: int) -> dict[str, int]:
    """Current cumulative token usage for a workflow run."""
    try:
        run = workflow_service.get_run(db, workflow_run_id)
    except Exception:  # pragma: no cover
        return {"input": 0, "output": 0, "total": 0, "calls": 0}
    usage = (run.state_snapshot or {}).get(_SNAPSHOT_KEY) or {}
    return {
        "input": int(usage.get("input", 0)),
        "output": int(usage.get("output", 0)),
        "total": int(usage.get("total", 0)),
        "calls": int(usage.get("calls", 0)),
    }


def _record_usage(db, workflow_run_id: int, *, agent: str, tokens: dict[str, int]) -> dict[str, int]:
    """Add one call's usage to the run's running total and persist it."""
    current = read_usage(db, workflow_run_id)
    updated = {
        "input": current["input"] + tokens.get("input", 0),
        "output": current["output"] + tokens.get("output", 0),
        "total": current["total"] + tokens.get("total", 0),
        "calls": current["calls"] + 1,
        "last_agent": agent,
    }
    try:
        workflow_service.save_state(
            db,
            workflow_run_id=workflow_run_id,
            state_patch={_SNAPSHOT_KEY: updated},
            actor_label=f"token_budget[{agent}]",
        )
    except Exception:  # pragma: no cover - accounting must never break a run
        logger.exception("token_usage_persist_failed", run=workflow_run_id)
    return updated


def _extract_tokens(response: Any) -> dict[str, int]:
    """Pull token counts out of a model response.

    Providers vary in where they put usage, and a missing count must not be read as
    zero cost — an unmetered call would let a run drift past its budget unnoticed. When
    nothing is reported we fall back to a rough character-based estimate.
    """
    messages = getattr(response, "result", None) or []
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if usage:
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            total = int(usage.get("total_tokens", 0) or 0) or (input_tokens + output_tokens)
            return {"input": input_tokens, "output": output_tokens, "total": total}

    estimated = 0
    for message in messages:
        if isinstance(message, AIMessage) and message.content:
            estimated += len(str(message.content)) // 4  # ~4 chars per token
    return {"input": 0, "output": estimated, "total": estimated, "estimated": 1}


class TokenBudgetMiddleware(AgentMiddleware):
    """Enforce a cumulative token ceiling across a whole workflow run."""

    def __init__(self, agent_name: str, *, budget: int) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.budget = budget

    @property
    def name(self) -> str:
        return f"TokenBudgetMiddleware[{self.agent_name}]"

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        context: AgentCareContext | None = getattr(request.runtime, "context", None)
        if context is None:
            return handler(request)

        before = read_usage(context.db, context.workflow_run_id)

        # Refuse the call outright rather than making it and noticing afterwards —
        # checking after the fact would mean always overspending by one call.
        if before["total"] >= self.budget:
            logger.warning(
                "token_budget_exceeded",
                agent=self.agent_name,
                run=context.workflow_run_id,
                used=before["total"],
                budget=self.budget,
            )
            self._escalate(context, before["total"])
            return self._budget_response()

        response = handler(request)

        tokens = _extract_tokens(response)
        after = _record_usage(context.db, context.workflow_run_id, agent=self.agent_name, tokens=tokens)

        if after["total"] >= self.budget:
            logger.warning(
                "token_budget_reached",
                agent=self.agent_name,
                run=context.workflow_run_id,
                used=after["total"],
                budget=self.budget,
            )
            self._escalate(context, after["total"])

        return response

    def _budget_response(self) -> Any:
        """Return a terminal message instead of calling the model."""
        from langchain.agents.middleware import ModelResponse

        return ModelResponse(
            result=[
                AIMessage(
                    content=(
                        "This request has reached its processing budget and has been passed "
                        "to our staff to complete. They will be in touch shortly."
                    )
                )
            ]
        )

    def _escalate(self, context: AgentCareContext, used: int) -> None:
        try:
            escalation_service.create_escalation(
                context.db,
                workflow_run_id=context.workflow_run_id,
                reason=EscalationReason.TOKEN_BUDGET_EXCEEDED,
                detail=(
                    f"Token budget exhausted at {used:,} of {self.budget:,} tokens "
                    f"while running the {self.agent_name} agent. A staff member should "
                    "complete this request manually."
                ),
                actor_label=f"token_budget[{self.agent_name}]",
            )
        except Exception:  # pragma: no cover
            logger.exception("token_budget_escalation_failed", run=context.workflow_run_id)
