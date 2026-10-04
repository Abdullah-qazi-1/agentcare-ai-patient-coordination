"""Safety agent tools: deterministic scanning and escalation records."""

from langchain_core.tools import tool

from app.agents.context import current_context
from app.db.models.enums import EscalationReason
from app.services import escalation_service, safety_service
from app.services.errors import ServiceError


@tool
def scan_for_unsafe_content(text: str) -> str:
    """Run the hospital's deterministic safety scan over some text.

    This is a pattern check, independent of your own judgement. Treat a positive result
    as authoritative — if it flags emergency or medical-advice content, escalate,
    whatever your own reading of the text is.

    Args:
        text: The text to scan.
    """
    result = safety_service.scan_text(text)
    if not result.is_flagged:
        return "Scan clear: no emergency or medical-advice indicators detected."

    return (
        f"FLAGGED. Emergency: {result.is_emergency}. "
        f"Medical advice sought: {result.is_medical_advice}. "
        f"Categories: {', '.join(result.categories)}. "
        f"Matched terms: {', '.join(result.matched_terms[:8])}. "
        f"Recommended escalation reason: "
        f"{result.escalation_reason.value if result.escalation_reason else 'sensitive_action'}."
    )


@tool
def create_escalation(reason: str, detail: str) -> str:
    """Create a human-review record and pause the workflow.

    Args:
        reason: One of: emergency_language, medical_advice_request, uncertain_routing,
            sensitive_action, repeated_tool_failure, low_confidence_classification.
        detail: What a reviewing staff member needs to know, stated administratively.
    """
    context = current_context()
    try:
        parsed_reason = EscalationReason(reason.strip().lower())
    except ValueError:
        allowed = ", ".join(r.value for r in EscalationReason)
        return f"Error: '{reason}' is not valid. Allowed reasons: {allowed}"

    try:
        escalation = escalation_service.create_escalation(
            context.db,
            workflow_run_id=context.workflow_run_id,
            reason=parsed_reason,
            detail=detail,
            actor_label="safety_agent",
        )
    except ServiceError as exc:
        return f"Error: {exc}"

    context.scratch["escalation_id"] = escalation.id
    return (
        f"Escalation #{escalation.id} created ({parsed_reason.value}); the workflow is now "
        "awaiting human review. Tell the patient a staff member will follow up — do not "
        "attempt to resolve the underlying question yourself."
    )


@tool
def check_existing_escalations() -> str:
    """Check whether this workflow already has escalations, to avoid raising a duplicate."""
    context = current_context()
    escalations = escalation_service.list_for_workflow(context.db, context.workflow_run_id)
    if not escalations:
        return "No escalations exist for this workflow."

    lines = [
        f"- escalation_id={e.id} [{e.status.value}] {e.reason.value}: {e.detail[:120]}"
        for e in escalations
    ]
    return "Existing escalations:\n" + "\n".join(lines)
