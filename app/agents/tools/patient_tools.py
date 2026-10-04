"""Coordinator tools: patient identity and workflow state.

Every tool here reads `patient_id` from the run context rather than accepting it as an
argument. That is a deliberate authorization boundary: the model cannot ask for another
patient's record because it is never given the ability to name one.
"""

from langchain_core.tools import tool

from app.agents.context import current_context
from app.db.models.enums import WorkflowStep
from app.services import patient_service, workflow_service
from app.services.errors import ServiceError


@tool
def get_patient_record() -> str:
    """Retrieve the current patient's profile and administrative history.

    Returns the patient's registration details plus counts of their appointments and
    documents. Use this first to understand who you are helping.
    """
    context = current_context()
    try:
        profile = patient_service.get_profile(context.db, context.patient_id)
    except ServiceError as exc:
        return f"Error: {exc}"

    from app.services import appointment_service, document_service

    appointments = appointment_service.list_patient_appointments(context.db, profile.id)
    documents = document_service.list_patient_documents(context.db, profile.id)
    active = [a for a in appointments if a.status.value in ("pending", "confirmed", "rescheduled")]

    return (
        f"Patient #{profile.id} ({profile.user.name}).\n"
        f"Preferred language: {profile.preferred_language}.\n"
        f"Registered: {profile.created_at.date().isoformat()}.\n"
        f"Appointments: {len(appointments)} total, {len(active)} active.\n"
        f"Documents on file: {len(documents)}.\n"
        + (
            "Active appointments: "
            + "; ".join(f"#{a.id} ({a.status.value})" for a in active)
            if active
            else "No active appointments."
        )
    )


@tool
def load_workflow_state() -> str:
    """Load the saved state of the current workflow run.

    Use this to see what earlier steps already decided before you act, so work is not
    repeated after a resume.
    """
    context = current_context()
    try:
        run = workflow_service.get_run(context.db, context.workflow_run_id)
    except ServiceError as exc:
        return f"Error: {exc}"

    snapshot = run.state_snapshot or {}
    if not snapshot:
        return f"Workflow #{run.id} is at step '{run.current_step.value}' with no saved state yet."

    lines = "\n".join(f"  {key}: {value}" for key, value in sorted(snapshot.items()))
    return (
        f"Workflow #{run.id} — step '{run.current_step.value}', status '{run.status.value}'.\n"
        f"Saved state:\n{lines}"
    )


@tool
def save_workflow_state(step: str, notes: str = "") -> str:
    """Persist progress after completing a step.

    Args:
        step: The step just completed. One of: registration, intent_detection,
            department_routing, appointment, document_coordination, follow_up, confirmation.
        notes: A short administrative note about what was decided.
    """
    context = current_context()
    try:
        workflow_step = WorkflowStep(step.strip().lower())
    except ValueError:
        valid = ", ".join(s.value for s in WorkflowStep)
        return f"Error: '{step}' is not a valid step. Valid steps: {valid}"

    patch: dict = dict(context.scratch)
    if notes:
        patch[f"note_{workflow_step.value}"] = notes[:500]

    try:
        run = workflow_service.save_state(
            context.db,
            workflow_run_id=context.workflow_run_id,
            step=workflow_step,
            state_patch=patch,
        )
    except ServiceError as exc:
        return f"Error: {exc}"

    return f"Saved. Workflow #{run.id} is now at step '{run.current_step.value}'."
