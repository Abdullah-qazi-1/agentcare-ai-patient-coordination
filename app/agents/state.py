"""The state threaded through the AgentCare workflow graph.

This is deliberately *not* a growing message transcript. Each node receives only the
structured facts it needs and is invoked with a freshly composed instruction, so token
cost per node stays flat no matter how many steps a run takes. The accumulated
narrative lives in `WorkflowRun.state_snapshot` and the audit trail, where humans read
it — not in the model's context window, where it would be re-sent on every call.
"""

from typing import Annotated, Any, TypedDict


def _merge_dicts(left: dict | None, right: dict | None) -> dict:
    """Reducer: later writes win, earlier keys survive."""
    return {**(left or {}), **(right or {})}


def _append(left: list | None, right: list | None) -> list:
    """Reducer: append-only. Safety findings are never removed once recorded."""
    return [*(left or []), *(right or [])]


class WorkflowState(TypedDict, total=False):
    # --- identity, set before the graph starts ---
    workflow_run_id: int
    patient_id: int
    raw_request: str
    document_ids: list[int]

    # --- produced by the Coordinator ---
    intent: dict[str, Any]
    needs_appointment: bool
    needs_documents: bool

    # --- produced by Routing ---
    department_id: int | None
    department_name: str | None
    routing_confidence: float

    # --- produced by Appointment ---
    appointment_id: int | None
    appointment_action: str | None

    # --- produced by Document ---
    documents_processed: int
    missing_documents: list[str]

    # --- produced by Follow-up ---
    reminder_id: int | None

    # --- safety, append-only across the whole run ---
    safety_flags: Annotated[list[dict[str, Any]], _append]
    escalation_id: int | None
    halted: bool

    # --- outcome ---
    current_step: str
    status: str
    summary: str
    errors: Annotated[list[str], _append]

    # --- free-form scratch shared between nodes ---
    scratch: Annotated[dict[str, Any], _merge_dicts]


def initial_state(
    *, workflow_run_id: int, patient_id: int, raw_request: str, document_ids: list[int] | None = None
) -> WorkflowState:
    return WorkflowState(
        workflow_run_id=workflow_run_id,
        patient_id=patient_id,
        raw_request=raw_request,
        document_ids=document_ids or [],
        needs_appointment=False,
        needs_documents=bool(document_ids),
        department_id=None,
        department_name=None,
        routing_confidence=0.0,
        appointment_id=None,
        appointment_action=None,
        documents_processed=0,
        missing_documents=[],
        reminder_id=None,
        safety_flags=[],
        escalation_id=None,
        halted=False,
        current_step="registration",
        status="in_progress",
        summary="",
        errors=[],
        scratch={},
    )
