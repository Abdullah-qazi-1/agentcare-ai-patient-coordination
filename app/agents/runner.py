"""Entry points the API uses to run and resume the agent workflow.

Two functions, matching the two ways a run moves forward: a patient submitting a
request, and a staff member approving a paused one. Both return the same shape, so the
caller doesn't care which happened.
"""

from typing import Any

from langgraph.types import Command
from sqlalchemy.orm import Session

from app.agents.graph import build_graph, thread_config
from app.agents.llm import LLMNotConfiguredError, is_llm_configured
from app.agents.state import initial_state
from app.core.logging import get_logger
from app.db.models.enums import WorkflowStatus
from app.schemas.workflow import WorkflowRunResult
from app.services import escalation_service, workflow_service

logger = get_logger(__name__)


def _result_from_state(db: Session, workflow_run_id: int, state: dict[str, Any]) -> WorkflowRunResult:
    """Build the API response from the graph's final state plus the persisted run."""
    run = workflow_service.get_run(db, workflow_run_id)
    pending = escalation_service.get_pending_for_workflow(db, workflow_run_id)

    return WorkflowRunResult(
        workflow_run_id=workflow_run_id,
        status=run.status,
        current_step=run.current_step,
        summary=state.get("summary") or (run.state_snapshot or {}).get("summary", ""),
        appointment_id=state.get("appointment_id"),
        escalation_id=pending.id if pending else state.get("escalation_id"),
        awaiting_human_review=pending is not None,
    )


def _interrupt_result(db: Session, workflow_run_id: int, interrupts: Any) -> WorkflowRunResult:
    """Build a response for a run that suspended awaiting human review."""
    run = workflow_service.get_run(db, workflow_run_id)
    pending = escalation_service.get_pending_for_workflow(db, workflow_run_id)

    if pending is None and run.status != WorkflowStatus.AWAITING_REVIEW:
        # A safety escalation flips WorkflowRun.status itself (in
        # escalation_service.create_escalation), so it's already discoverable via
        # `GET /workflow?status=awaiting_review`. A HumanInTheLoopMiddleware tool-approval
        # interrupt (reschedule/cancel) has no Escalation row to do that for it — without
        # this, the run stays "in_progress" in the database forever, indistinguishable
        # from one that's actually still executing, and staff have no way to find it.
        run = workflow_service.save_state(
            db, workflow_run_id=workflow_run_id, status=WorkflowStatus.AWAITING_REVIEW
        )

    message = "Your request needs a quick review by our staff before we continue."
    # The interrupt payload carries a patient-safe message written by the safety layer;
    # prefer it, since it is tailored (an emergency gets very different wording).
    try:
        for item in interrupts or []:
            value = getattr(item, "value", None) or item
            if isinstance(value, dict) and value.get("patient_message"):
                message = value["patient_message"]
                break
    except Exception:  # pragma: no cover - never fail a response over message formatting
        logger.exception("interrupt_message_parse_failed", run=workflow_run_id)

    return WorkflowRunResult(
        workflow_run_id=workflow_run_id,
        status=WorkflowStatus.AWAITING_REVIEW,
        current_step=run.current_step,
        summary=message,
        appointment_id=None,
        escalation_id=pending.id if pending else None,
        awaiting_human_review=True,
    )


def start_workflow(
    db: Session,
    *,
    patient_id: int,
    request_text: str,
    document_ids: list[int] | None = None,
    actor_id: int | None = None,
) -> WorkflowRunResult:
    """Create a workflow run and execute the agent graph over it."""
    if not is_llm_configured():
        raise LLMNotConfiguredError(
            "LLM_API_KEY is not set, so the agent workflow cannot run. "
            "Copy .env.example to .env and add your LLM provider's key."
        )

    run = workflow_service.create_run(
        db,
        patient_id=patient_id,
        raw_request=request_text,
        actor_id=actor_id,
        document_ids=document_ids or [],
    )

    logger.info("workflow_start", run=run.id, patient=patient_id)
    try:
        # Building the graph opens the checkpointer's own SQLite connection, which can
        # throw ("database is locked") under concurrent access just as easily as
        # `graph.invoke` can — it belongs inside the same try, or a failure here leaves
        # `run` permanently stuck at its just-created IN_PROGRESS/REGISTRATION state
        # instead of being marked FAILED.
        graph = build_graph(db, actor_id=actor_id)
        config = thread_config(run.id)
        state = initial_state(
            workflow_run_id=run.id,
            patient_id=patient_id,
            raw_request=request_text,
            document_ids=document_ids or [],
        )
        final = graph.invoke(state, config=config)
    except Exception as exc:
        logger.exception("workflow_crashed", run=run.id)
        workflow_service.fail_run(db, workflow_run_id=run.id, error=f"{type(exc).__name__}: {exc}")
        return WorkflowRunResult(
            workflow_run_id=run.id,
            status=WorkflowStatus.FAILED,
            current_step=run.current_step,
            summary=(
                "Something went wrong while processing your request. "
                "Our staff have been notified and will follow up."
            ),
            awaiting_human_review=False,
        )

    if final.get("__interrupt__"):
        return _interrupt_result(db, run.id, final["__interrupt__"])
    return _result_from_state(db, run.id, final)


def _pending_interrupt_values(graph, config: dict) -> list[Any]:
    """Read the interrupt payloads a suspended run is waiting on."""
    try:
        snapshot = graph.get_state(config)
    except Exception:  # pragma: no cover - a missing checkpoint is handled by the caller
        logger.exception("get_state_failed")
        return []

    values: list[Any] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            values.append(getattr(item, "value", item))
    return values


def _build_resume_payload(graph, config: dict, approved: bool) -> Any:
    """Construct the resume value in whichever dialect the pending interrupt expects.

    Two different mechanisms can suspend a run, and they resume differently:

    * The safety gate's own `interrupt()` takes ``{"decision": "approve"|"reject"}``.
    * ``HumanInTheLoopMiddleware`` takes ``{"decisions": [...]}`` with exactly one entry
      per interrupted tool call — sending the wrong shape, or the wrong number of
      entries, raises inside the middleware rather than resuming.

    Sniffing the pending payload keeps that distinction out of the API layer, which
    should only have to say "approved" or "rejected".
    """
    decision_type = "approve" if approved else "reject"

    for value in _pending_interrupt_values(graph, config):
        if isinstance(value, dict) and value.get("type") == "safety_escalation":
            return {"decision": decision_type}

        # HITLRequest carries one action_request per tool call awaiting review.
        action_requests = None
        if isinstance(value, dict):
            action_requests = value.get("action_requests")
        else:
            action_requests = getattr(value, "action_requests", None)

        if action_requests is not None:
            count = max(1, len(action_requests))
            decision: dict[str, Any] = {"type": decision_type}
            if not approved:
                # A rejection must carry a message; the middleware feeds it back to the
                # agent as the tool result so it can respond sensibly.
                decision["message"] = "A staff reviewer declined this action."
            return {"decisions": [dict(decision) for _ in range(count)]}

    # No pending interrupt found — fall back to the safety-gate dialect.
    return {"decision": decision_type}


def resume_workflow(
    db: Session, *, workflow_run_id: int, approved: bool, actor_id: int | None = None
) -> WorkflowRunResult:
    """Resume a suspended run after a staff decision.

    The graph picks up at the exact node that interrupted, reading its position from the
    checkpoint — so this works even if the process restarted since the pause.
    """
    logger.info("workflow_resume", run=workflow_run_id, approved=approved)
    try:
        # See the matching comment in `start_workflow`: building the graph can throw
        # just as `graph.invoke` can, and needs to land in the same graceful-failure path.
        graph = build_graph(db, actor_id=actor_id)
        config = thread_config(workflow_run_id)
        resume_value = _build_resume_payload(graph, config, approved)
        final = graph.invoke(Command(resume=resume_value), config=config)
    except Exception as exc:
        logger.exception("workflow_resume_crashed", run=workflow_run_id)
        workflow_service.fail_run(
            db, workflow_run_id=workflow_run_id, error=f"resume: {type(exc).__name__}: {exc}"
        )
        run = workflow_service.get_run(db, workflow_run_id)
        return WorkflowRunResult(
            workflow_run_id=workflow_run_id,
            status=run.status,
            current_step=run.current_step,
            summary="We could not resume this request automatically. Staff will handle it manually.",
            awaiting_human_review=False,
        )

    if final.get("__interrupt__"):
        return _interrupt_result(db, workflow_run_id, final["__interrupt__"])
    return _result_from_state(db, workflow_run_id, final)
