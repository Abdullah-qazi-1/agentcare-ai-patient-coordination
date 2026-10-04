"""The standard middleware stack applied to every AgentCare agent.

Assembling this in one place is deliberate: it means "what protections does agent X
have?" has exactly one answer for every X, and adding a protection is a one-line change
that lands on all six agents at once.

Ordering matters and is set by the framework: `before_*` hooks run first-to-last,
`after_*` run in reverse, and `wrap_*` hooks nest with the first entry outermost.
Audit is therefore listed first so it wraps everything — including calls that safety or
HITL subsequently block, which are precisely the ones a reviewer wants recorded.
"""

from langchain.agents.middleware import (
    AgentMiddleware,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    PIIMiddleware,
    ToolRetryMiddleware,
)

from app.agents.middleware.audit import AuditMiddleware
from app.agents.middleware.guardrails import NemoGuardrailsMiddleware
from app.agents.middleware.safety import ClinicalSafetyMiddleware
from app.agents.middleware.token_budget import TokenBudgetMiddleware
from app.core.config import get_settings

# Healthcare identifiers the built-in PII types don't cover. Detected on the way out so
# an agent can work with a phone number internally but never echo one back in prose.
PHONE_PATTERN = r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
MRN_PATTERN = r"\b(?:MRN|mrn)[-:\s]?\d{6,10}\b"


def standard_middleware(
    agent_name: str,
    *,
    sensitive_tools: dict[str, object] | None = None,
    scan_output: bool = True,
    run_limit: int | None = None,
) -> list[AgentMiddleware]:
    """Build the middleware stack for one agent.

    Args:
        agent_name: Used in audit records and log lines to attribute actions.
        sensitive_tools: Tool-name -> approval config. Present only for agents that can
            take an action a human should sign off on (cancel, reschedule).
        scan_output: Disable only for agents whose output is never patient-facing.
        run_limit: Model calls allowed per run. Defaults to the configured limit.
    """
    settings = get_settings()

    stack: list[AgentMiddleware] = [
        # Outermost: sees every tool call, including ones later middleware blocks.
        AuditMiddleware(agent_name),
        # Deterministic floor first (free on the common path) — semantic layer second,
        # and only when NEMOGUARDRAILS_ENABLED=true (off by default).
        ClinicalSafetyMiddleware(agent_name, scan_output=scan_output),
        NemoGuardrailsMiddleware(agent_name, check_output=scan_output),
        # One instance per PII type — PIIMiddleware takes a single type each.
        PIIMiddleware("email", strategy="redact", apply_to_output=True),
        PIIMiddleware("phone", strategy="mask", detector=PHONE_PATTERN, apply_to_output=True),
        PIIMiddleware("mrn", strategy="hash", detector=MRN_PATTERN, apply_to_output=True),
        # Transient failures (a busy DB, a flaky gateway) retry before anyone is told.
        ToolRetryMiddleware(
            max_retries=2,
            backoff_factor=2.0,
            initial_delay=0.5,
            max_delay=8.0,
            jitter=True,
            on_failure="continue",
        ),
        # --- Three complementary cost ceilings -------------------------------
        # Calls are capped per agent and per thread...
        ModelCallLimitMiddleware(
            run_limit=run_limit or settings.agent_run_call_limit,
            thread_limit=settings.workflow_thread_call_limit,
            exit_behavior="end",
        ),
        # ...tokens are capped across the whole workflow, because a few very large
        # calls can cost more than many small ones...
        TokenBudgetMiddleware(agent_name, budget=settings.workflow_token_budget),
        # ...and tool results are trimmed so a long run's context stops growing.
        ContextEditingMiddleware(),
    ]

    if sensitive_tools:
        from langchain.agents.middleware import HumanInTheLoopMiddleware

        stack.append(
            HumanInTheLoopMiddleware(
                interrupt_on=sensitive_tools,
                description_prefix="AgentCare requires staff approval",
            )
        )

    return stack
