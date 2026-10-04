"""The six AgentCare agents.

Each is a real `create_agent()` tool-calling loop with its own system prompt, its own
tools, and its own structured output contract — not a prompt-wrapped function. Agents
are built lazily and cached, because constructing one opens an HTTP client and the same
six are reused for every request.

`TodoListMiddleware` is added to the Coordinator alone: it is the only agent facing a
request that may contain several asks at once ("book an appointment AND attach my ECG"),
where an explicit, inspectable plan stops the second ask being dropped.
"""

from functools import lru_cache

from langchain.agents import create_agent
from langchain.agents.middleware import TodoListMiddleware

from app.agents.context import AgentCareContext
from app.agents.llm import get_llm
from app.agents.middleware.stack import standard_middleware
from app.agents.prompts import (
    APPOINTMENT_PROMPT,
    COORDINATOR_PROMPT,
    DOCUMENT_PROMPT,
    FOLLOWUP_PROMPT,
    ROUTING_PROMPT,
    SAFETY_PROMPT,
)
from app.agents.tools import (
    appointment_tools,
    document_tools,
    followup_tools,
    patient_tools,
    routing_tools,
    safety_tools,
)
from app.schemas.agent import (
    AdminIntent,
    AppointmentOutcome,
    DocumentClassification,
    FollowUpPlan,
    RoutingDecision,
    SafetyVerdict,
)

# Actions a human should sign off on. Cancelling or moving an appointment affects a real
# clinic slot and a real patient's plans, so staff approve those; booking a free slot is
# reversible and does not warrant a gate on every request.
SENSITIVE_APPOINTMENT_TOOLS = {
    "cancel_appointment": {"allowed_decisions": ["approve", "edit", "reject"]},
    "reschedule_appointment": {"allowed_decisions": ["approve", "edit", "reject"]},
}


@lru_cache(maxsize=1)
def coordinator_agent():
    """Understands the request, records the plan, tracks the workflow."""
    return create_agent(
        model=get_llm("coordinator"),
        tools=[
            patient_tools.get_patient_record,
            patient_tools.load_workflow_state,
            patient_tools.save_workflow_state,
        ],
        system_prompt=COORDINATOR_PROMPT,
        response_format=AdminIntent,
        middleware=[*standard_middleware("coordinator"), TodoListMiddleware()],
        context_schema=AgentCareContext,
        name="coordinator_agent",
    )


@lru_cache(maxsize=1)
def routing_agent():
    """Maps an administrative request to exactly one department."""
    return create_agent(
        model=get_llm("routing"),
        tools=[
            routing_tools.lookup_departments,
            routing_tools.suggest_department_by_keywords,
            routing_tools.flag_uncertain_routing,
        ],
        system_prompt=ROUTING_PROMPT,
        response_format=RoutingDecision,
        middleware=standard_middleware("routing"),
        context_schema=AgentCareContext,
        name="routing_agent",
    )


@lru_cache(maxsize=1)
def appointment_agent():
    """Finds slots and books, reschedules, or cancels appointments."""
    return create_agent(
        model=get_llm("appointment"),
        tools=[
            appointment_tools.find_available_slots,
            appointment_tools.book_appointment,
            appointment_tools.list_my_appointments,
            appointment_tools.reschedule_appointment,
            appointment_tools.cancel_appointment,
        ],
        system_prompt=APPOINTMENT_PROMPT,
        response_format=AppointmentOutcome,
        middleware=standard_middleware(
            "appointment", sensitive_tools=SENSITIVE_APPOINTMENT_TOOLS
        ),
        context_schema=AgentCareContext,
        name="appointment_agent",
    )


@lru_cache(maxsize=1)
def document_agent():
    """Classifies, files, and gap-checks patient documents."""
    return create_agent(
        model=get_llm("document"),
        tools=[
            document_tools.list_pending_documents,
            document_tools.suggest_document_type,
            document_tools.classify_and_store_document,
            document_tools.check_missing_documents,
        ],
        system_prompt=DOCUMENT_PROMPT,
        response_format=DocumentClassification,
        middleware=standard_middleware("document"),
        context_schema=AgentCareContext,
        name="document_agent",
    )


@lru_cache(maxsize=1)
def followup_agent():
    """Schedules reminders and follow-ups, and assembles the final confirmation."""
    return create_agent(
        model=get_llm("followup"),
        tools=[
            followup_tools.list_my_reminders,
            followup_tools.create_appointment_reminder,
            followup_tools.schedule_followup_task,
            followup_tools.send_notification,
            followup_tools.get_appointment_summary,
        ],
        system_prompt=FOLLOWUP_PROMPT,
        response_format=FollowUpPlan,
        middleware=standard_middleware("followup"),
        context_schema=AgentCareContext,
        name="followup_agent",
    )


@lru_cache(maxsize=1)
def safety_agent():
    """Judges flagged content and creates escalation records."""
    return create_agent(
        model=get_llm("safety"),
        tools=[
            safety_tools.scan_for_unsafe_content,
            safety_tools.check_existing_escalations,
            safety_tools.create_escalation,
        ],
        system_prompt=SAFETY_PROMPT,
        response_format=SafetyVerdict,
        # scan_output is off for this agent alone: its whole job is to *discuss* unsafe
        # content in order to escalate it. Running the output scanner here would flag the
        # safety agent's own escalation notes and suppress the very warning being raised.
        middleware=standard_middleware("safety", scan_output=False, run_limit=6),
        context_schema=AgentCareContext,
        name="safety_agent",
    )


def all_agents() -> dict[str, object]:
    """Every agent, keyed by role — used by the graph and by diagnostics."""
    return {
        "coordinator": coordinator_agent(),
        "routing": routing_agent(),
        "appointment": appointment_agent(),
        "document": document_agent(),
        "followup": followup_agent(),
        "safety": safety_agent(),
    }
