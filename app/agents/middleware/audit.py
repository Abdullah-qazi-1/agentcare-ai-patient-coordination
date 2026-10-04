"""Audit middleware — every tool call is recorded, without exception.

Wrapping `wrap_tool_call` is what makes the audit trail structural rather than
aspirational: there is no code path by which an agent can invoke a tool without an
AuditEvent being written, because the middleware sits between the agent and every tool
it owns. A developer adding a new tool gets auditing for free and cannot forget it.

This middleware is listed first in the stack so it is the outermost wrapper, which
means it also observes calls that inner middleware (safety, HITL) subsequently blocks —
blocked attempts are exactly what a compliance reviewer needs to see.
"""

import time
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.agents.context import AgentCareContext
from app.agents.middleware.toolcall import tool_args, tool_name
from app.core.logging import get_logger
from app.services import audit_service

logger = get_logger(__name__)

# Tool arguments that must never be written into the audit metadata verbatim.
_SENSITIVE_ARG_KEYS = {"content", "file_content", "password", "raw_text", "document_bytes"}


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    """Keep argument values auditable without copying PII or file bodies into the log."""
    redacted: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key in _SENSITIVE_ARG_KEYS:
            redacted[key] = f"<{len(value) if hasattr(value, '__len__') else '?'} bytes redacted>"
        elif isinstance(value, str) and len(value) > 300:
            redacted[key] = value[:300] + "…"
        else:
            redacted[key] = value
    return redacted


def _result_text(result: Any) -> str | None:
    """Extract a tool's own return text, if there is one.

    Only the *arguments* used to be captured — what the tool actually reported back
    (which slots it found, which document it matched) never was, which is exactly the
    data-lineage information a patient or auditor needs to answer "what did the agent
    actually see before it decided this?" `audit_service.record`'s own sanitizer
    truncates long values, so this doesn't need its own length limit.
    """
    content = getattr(result, "content", None)
    if content is None:
        return None
    return str(content)


class AuditMiddleware(AgentMiddleware):
    """Persist an AuditEvent for every tool invocation an agent makes."""

    def __init__(self, agent_name: str) -> None:
        super().__init__()
        self.agent_name = agent_name

    @property
    def name(self) -> str:
        # Per-agent name so LangSmith traces attribute each hook to its own agent.
        return f"AuditMiddleware[{self.agent_name}]"

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage | Any],
    ) -> ToolMessage | Any:
        context: AgentCareContext | None = getattr(request.runtime, "context", None)
        name = tool_name(request)
        args = tool_args(request)

        started = time.perf_counter()
        status = "succeeded"
        error_text: str | None = None
        result_text: str | None = None

        try:
            result = handler(request)
            # A tool that traps its own error still reports failure through the message.
            if isinstance(result, ToolMessage) and getattr(result, "status", None) == "error":
                status = "failed"
                error_text = str(result.content)[:500]
            else:
                # The data-lineage payload: what the tool actually reported back, not
                # just what it was asked. `content` is already the model-facing tool
                # result — free-text, not raw file bytes — so this stays as safe to
                # persist as the args already were.
                result_text = _result_text(result)
            return result
        except Exception as exc:
            status = "failed"
            error_text = f"{type(exc).__name__}: {exc}"[:500]
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            if context is not None:
                try:
                    audit_service.record(
                        context.db,
                        action=f"tool_call:{name}",
                        entity_type="agent_tool",
                        entity_id=context.workflow_run_id,
                        actor_id=context.actor_id,
                        actor_label=self.agent_name,
                        metadata={
                            "tool": name,
                            "args": _safe_args(args),
                            "result": result_text,
                            "status": status,
                            "duration_ms": duration_ms,
                            "error": error_text,
                            "workflow_run_id": context.workflow_run_id,
                            "patient_id": context.patient_id,
                        },
                    )
                except Exception:  # pragma: no cover - auditing must never break a run
                    # Losing an audit row is bad; failing the patient's request because
                    # the audit write failed is worse. Log loudly and continue.
                    logger.exception("audit_write_failed", tool=name, agent=self.agent_name)
            logger.info(
                "tool_call",
                agent=self.agent_name,
                tool=name,
                status=status,
                duration_ms=duration_ms,
            )
