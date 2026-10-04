"""Clinical safety middleware — the guardrail that wraps every agent.

Three hooks, three different jobs:

* `before_model`  — scan the incoming request. Emergency or advice-seeking content is
  stopped *before* a single token is spent, and the run jumps straight to the end with
  a safe deflection.
* `after_model`   — scan what the agent produced. Even a correctly-prompted model can
  drift into diagnostic phrasing; this catches it before the patient ever sees it.
* `wrap_tool_call` — inspect tool arguments. Unsafe text can arrive from an uploaded
  document rather than the patient's message, so the tool boundary is checked too.

Every layer is deterministic Python (`safety_service`), with no model call. That is the
whole point: a regex cannot be argued out of firing by a persuasive request or by
instructions smuggled inside a document. The Safety *agent* adds semantic judgement on
top; this middleware is the floor beneath it.
"""

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.context import AgentCareContext
from app.agents.middleware.toolcall import tool_args, tool_call_id, tool_name
from app.core.logging import get_logger
from app.db.models.enums import EscalationReason
from app.services import escalation_service, safety_service

logger = get_logger(__name__)

# Tool arguments carrying free text that could contain unsafe content.
_TEXT_ARG_KEYS = {"request_text", "reason", "message", "note", "detail", "summary", "text"}


class ClinicalSafetyMiddleware(AgentMiddleware):
    """Blocks diagnosis/prescription/dosage behavior and escalates emergencies."""

    def __init__(self, agent_name: str, *, scan_output: bool = True) -> None:
        super().__init__()
        self.agent_name = agent_name
        self.scan_output = scan_output

    @property
    def name(self) -> str:
        # Per-agent name so LangSmith traces attribute each hook to its own agent.
        return f"ClinicalSafetyMiddleware[{self.agent_name}]"

    # --- helpers ---------------------------------------------------------------

    def _escalate(
        self,
        context: AgentCareContext | None,
        reason: EscalationReason,
        detail: str,
    ) -> None:
        if context is None:
            return
        try:
            escalation_service.create_escalation(
                context.db,
                workflow_run_id=context.workflow_run_id,
                reason=reason,
                detail=detail[:1000],
                actor_label=f"safety_middleware[{self.agent_name}]",
            )
        except Exception:  # pragma: no cover - never let escalation bookkeeping crash a run
            logger.exception("escalation_write_failed", agent=self.agent_name)

    @staticmethod
    def _last_human_text(messages: list[Any]) -> str:
        for message in reversed(messages or []):
            if getattr(message, "type", None) == "human":
                return str(message.content)
        return ""

    # --- hooks -----------------------------------------------------------------

    def before_model(self, state: dict, runtime: Any) -> dict | None:
        """Screen the inbound request before spending any tokens on it."""
        text = self._last_human_text(state.get("messages", []))
        if not text:
            return None

        result = safety_service.scan_text(text)
        if not result.is_flagged:
            return None

        context: AgentCareContext | None = getattr(runtime, "context", None)
        reason = result.escalation_reason or EscalationReason.SENSITIVE_ACTION
        self._escalate(context, reason, f"Inbound request flagged. {result.summary()}")

        message = (
            safety_service.EMERGENCY_MESSAGE
            if result.is_emergency
            else safety_service.SAFE_DEFLECTION_MESSAGE
        )
        logger.warning(
            "safety_block_input",
            agent=self.agent_name,
            emergency=result.is_emergency,
            categories=result.categories,
        )
        # jump_to="end" stops the agent loop entirely — the model is never asked the
        # clinical question, so it cannot answer it.
        return {
            "messages": [AIMessage(content=message)],
            "jump_to": "end",
        }

    def after_model(self, state: dict, runtime: Any) -> dict | None:
        """Screen the agent's own output before it can reach a patient."""
        if not self.scan_output:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or not last.content:
            return None
        # Tool-call turns carry no patient-facing prose yet.
        if getattr(last, "tool_calls", None):
            return None

        result = safety_service.scan_agent_output(str(last.content))
        if not result.is_flagged:
            return None

        context: AgentCareContext | None = getattr(runtime, "context", None)
        self._escalate(
            context,
            EscalationReason.MEDICAL_ADVICE_REQUEST,
            f"Agent output blocked before delivery. {result.summary()}",
        )
        logger.warning(
            "safety_block_output", agent=self.agent_name, matched=result.matched_terms[:5]
        )
        return {
            "messages": [AIMessage(content=safety_service.SAFE_DEFLECTION_MESSAGE)],
            "jump_to": "end",
        }

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """Block unsafe content from reaching a tool, whatever its source."""
        args = tool_args(request)
        name = tool_name(request)

        for key, value in args.items():
            if key not in _TEXT_ARG_KEYS or not isinstance(value, str):
                continue
            result = safety_service.scan_agent_output(value)
            if not result.is_flagged:
                continue

            context: AgentCareContext | None = getattr(request.runtime, "context", None)
            self._escalate(
                context,
                EscalationReason.MEDICAL_ADVICE_REQUEST,
                f"Blocked unsafe argument '{key}' to tool '{name}'. {result.summary()}",
            )
            logger.warning("safety_block_tool_arg", agent=self.agent_name, tool=name, arg=key)
            return ToolMessage(
                content=(
                    "Blocked: this argument contained clinical guidance, which AgentCare "
                    "is not permitted to record or act on. Rephrase administratively."
                ),
                tool_call_id=tool_call_id(request),
                status="error",
            )

        return handler(request)
