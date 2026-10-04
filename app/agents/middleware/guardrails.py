"""NeMo Guardrails middleware — the semantic layer on top of ClinicalSafetyMiddleware.

Same hook shape as `ClinicalSafetyMiddleware` (`app/agents/middleware/safety.py`):
`before_model` screens the inbound request, `after_model` screens the agent's own
output. Deliberately positioned right after it in the stack
(`app/agents/middleware/stack.py`) — the deterministic regex floor runs first and
costs nothing on the common path (`guardrails_service.is_configured()` short-circuits
instantly when the feature is off, which it is by default); this semantic check runs
second, only when the flag is on, and only adds an LLM call when the deterministic
layer didn't already resolve the question.
"""

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from app.agents.context import AgentCareContext
from app.core.logging import get_logger
from app.db.models.enums import EscalationReason
from app.guardrails import service as guardrails_service
from app.services import escalation_service, safety_service

logger = get_logger(__name__)


class NemoGuardrailsMiddleware(AgentMiddleware):
    """Blocks content the semantic rail flags, even when the deterministic scan missed it."""

    def __init__(self, agent_name: str, *, check_output: bool = True) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.check_output = check_output

    @property
    def name(self) -> str:
        return f"NemoGuardrailsMiddleware[{self.agent_name}]"

    def _escalate(self, context: AgentCareContext | None, detail: str) -> None:
        if context is None:
            return
        try:
            escalation_service.create_escalation(
                context.db,
                workflow_run_id=context.workflow_run_id,
                reason=EscalationReason.GUARDRAILS_BLOCKED,
                detail=detail[:1000],
                actor_label=f"nemoguardrails[{self.agent_name}]",
            )
        except Exception:  # pragma: no cover - never let escalation bookkeeping crash a run
            logger.exception("guardrails_escalation_write_failed", agent=self.agent_name)

    @staticmethod
    def _last_human_text(messages: list[Any]) -> str:
        for message in reversed(messages or []):
            if getattr(message, "type", None) == "human":
                return str(message.content)
        return ""

    def before_model(self, state: dict, runtime: Any) -> dict | None:
        if not guardrails_service.is_configured():
            return None
        text = self._last_human_text(state.get("messages", []))
        if not text:
            return None

        result = guardrails_service.check_input(text)
        if not result.is_flagged:
            return None

        context: AgentCareContext | None = getattr(runtime, "context", None)
        self._escalate(context, f"Inbound request flagged by NeMo Guardrails. {result.reason}")
        logger.warning("guardrails_block_input", agent=self.agent_name)
        return {
            "messages": [AIMessage(content=safety_service.SAFE_DEFLECTION_MESSAGE)],
            "jump_to": "end",
        }

    def after_model(self, state: dict, runtime: Any) -> dict | None:
        if not self.check_output or not guardrails_service.is_configured():
            return None

        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or not last.content:
            return None
        if getattr(last, "tool_calls", None):
            return None

        user_text = self._last_human_text(messages[:-1])
        result = guardrails_service.check_output(user_text, str(last.content))
        if not result.is_flagged:
            return None

        context: AgentCareContext | None = getattr(runtime, "context", None)
        self._escalate(context, f"Agent output blocked by NeMo Guardrails. {result.reason}")
        logger.warning("guardrails_block_output", agent=self.agent_name)
        return {
            "messages": [AIMessage(content=safety_service.SAFE_DEFLECTION_MESSAGE)],
            "jump_to": "end",
        }
