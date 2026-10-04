"""Department Routing agent tools.

`suggest_department_by_keywords` is the cost lever for the whole system: most requests
name their department outright, so the agent resolves them without the model ever
needing to reason about it. The LLM is spent only on genuine ambiguity.
"""

from langchain_core.tools import tool

from app.agents.context import current_context
from app.db.models.enums import EscalationReason
from app.services import department_service, escalation_service
from app.services.errors import ServiceError


@tool
def lookup_departments() -> str:
    """List every active department with its description and required documents.

    Always call this before deciding a department, so you route to a department that
    genuinely exists rather than one you assume exists.
    """
    context = current_context()
    departments = department_service.list_departments(context.db)
    if not departments:
        return "No active departments are configured."

    lines = []
    for dept in departments:
        required = ", ".join(dept.required_document_types or []) or "none"
        lines.append(f"- {dept.name} (id={dept.id}): {dept.description}. Required documents: {required}.")
    return "Active departments:\n" + "\n".join(lines)


@tool
def suggest_department_by_keywords(request_text: str) -> str:
    """Check the request against the hospital's department keyword table.

    This is a deterministic lookup, not a judgement. If it returns a confident match,
    use it. If it returns no match, decide yourself from the department list.

    Args:
        request_text: The patient's administrative request, in their own words.
    """
    context = current_context()
    department, confidence = department_service.match_department_by_keywords(context.db, request_text)

    if department is None:
        return (
            "No confident keyword match. Either the request names no department, or two "
            "departments matched equally. Decide from the department list and state your "
            "confidence honestly."
        )
    return (
        f"Keyword match: {department.name} (id={department.id}) with confidence {confidence}. "
        f"Description: {department.description}"
    )


@tool
def flag_uncertain_routing(request_text: str, reason: str) -> str:
    """Escalate to a human when a genuine hospital matter's correct department cannot
    be determined.

    Use this instead of guessing. A wrongly routed patient wastes a real appointment
    slot and a real trip to the hospital.

    Do NOT use this for a request that isn't a hospital matter at all (buying a
    product, booking travel, anything unrelated to healthcare) — that needs no human
    judgement. Set `is_administrative_request=False` on your final answer instead;
    it will be rejected directly with no staff review.

    Args:
        request_text: The request that could not be routed.
        reason: Why routing is uncertain, in administrative terms.
    """
    context = current_context()
    try:
        escalation = escalation_service.create_escalation(
            context.db,
            workflow_run_id=context.workflow_run_id,
            reason=EscalationReason.UNCERTAIN_ROUTING,
            detail=f"Request: {request_text[:400]} | Reason: {reason[:400]}",
            actor_label="routing_agent",
        )
    except ServiceError as exc:
        return f"Error: {exc}"

    context.scratch["escalation_id"] = escalation.id
    return (
        f"Escalation #{escalation.id} created for human routing review. "
        "Tell the patient a staff member will confirm the right department shortly."
    )
