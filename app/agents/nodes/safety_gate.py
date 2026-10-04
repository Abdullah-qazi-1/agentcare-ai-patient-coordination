"""Safety gate — runs before the pipeline, and can stop it entirely."""

from typing import Any

from langgraph.types import interrupt

from app.agents.agents import safety_agent
from app.agents.nodes.common import NodeFn, build_context, invoke_agent, persist_step
from app.core.logging import get_logger
from app.db.models.enums import EscalationReason, EscalationStatus, WorkflowStep
from app.services import escalation_service, safety_service

logger = get_logger(__name__)


def make_safety_gate(db, actor_id: int | None) -> NodeFn:
    """Screen the inbound request before any other agent sees it.

    The deterministic scan runs first and costs nothing. The Safety *agent* is invoked
    only when that scan trips, so the common case — an ordinary booking request — never
    pays for a safety model call.
    """

    def safety_gate(state: dict) -> dict:
        # LangGraph re-runs an interrupted node from its start when the graph resumes.
        # Without this check the gate would re-scan, re-escalate, and leave a fresh
        # pending escalation behind every time a staff member approved one — so the
        # queue would refill itself and the run would never read as reviewed.
        already_reviewed, reviewed_escalation_id = _prior_review_decision(
            db, state["workflow_run_id"]
        )
        if already_reviewed == "approved":
            logger.info("safety_gate_already_cleared", run=state["workflow_run_id"])
            return {
                "escalation_id": reviewed_escalation_id,
                "current_step": "intent_detection",
            }
        if already_reviewed == "rejected":
            return {
                "escalation_id": reviewed_escalation_id,
                "halted": True,
                "status": "terminated",
                "summary": state.get("summary")
                or "A staff member reviewed this request and it will be handled directly.",
            }

        request_text = state.get("raw_request", "")
        scan = safety_service.scan_text(request_text)

        if not scan.is_flagged:
            return {"current_step": "intent_detection"}

        logger.warning(
            "safety_gate_tripped",
            run=state["workflow_run_id"],
            emergency=scan.is_emergency,
            categories=scan.categories,
        )

        flag = {
            "stage": "inbound",
            "emergency": scan.is_emergency,
            "medical_advice": scan.is_medical_advice,
            "categories": scan.categories,
        }

        context = build_context(state, db, actor_id=actor_id, actor_label="safety_agent")
        verdict: Any = None
        try:
            verdict, _ = invoke_agent(
                safety_agent(),
                (
                    "Assess this patient request and escalate it if required.\n\n"
                    f"Request: {request_text}\n\n"
                    "Run the deterministic scan, check for existing escalations, and "
                    "create an escalation with the appropriate reason."
                ),
                context,
            )
        except Exception as exc:
            # If the safety agent itself fails we must fail closed, not open: escalate
            # deterministically rather than letting a flagged request proceed unreviewed.
            logger.exception("safety_agent_failed", run=state["workflow_run_id"])
            escalation = escalation_service.create_escalation(
                db,
                workflow_run_id=state["workflow_run_id"],
                reason=scan.escalation_reason or EscalationReason.SENSITIVE_ACTION,
                detail=f"Safety agent unavailable ({type(exc).__name__}); escalated on scan result. "
                f"{scan.summary()}",
                actor_label="safety_gate_fallback",
            )
            message = (
                safety_service.EMERGENCY_MESSAGE
                if scan.is_emergency
                else safety_service.SAFE_DEFLECTION_MESSAGE
            )
            return {
                "safety_flags": [flag],
                "escalation_id": escalation.id,
                "halted": True,
                "status": "awaiting_review",
                "summary": message,
                "errors": [f"safety_gate: {type(exc).__name__}"],
            }

        escalation = escalation_service.get_pending_for_workflow(db, state["workflow_run_id"])
        message = getattr(verdict, "patient_safe_message", None) or (
            safety_service.EMERGENCY_MESSAGE
            if scan.is_emergency
            else safety_service.SAFE_DEFLECTION_MESSAGE
        )

        if escalation is None:
            # The agent judged the flagged text benign. Record the disagreement and
            # continue — but the flag stays in state permanently for review.
            flag["resolved_without_escalation"] = True
            return {"safety_flags": [flag], "current_step": "intent_detection"}

        persist_step(
            db,
            state,
            WorkflowStep.INTENT_DETECTION,
            {"escalation_id": escalation.id, "safety_flagged": True},
            actor_label="safety_agent",
        )

        # Genuinely suspend the graph. The checkpoint holds this exact position until a
        # staff member resolves the escalation, surviving a process restart.
        decision = interrupt(
            {
                "type": "safety_escalation",
                "escalation_id": escalation.id,
                "reason": escalation.reason.value,
                "detail": escalation.detail,
                "patient_message": message,
            }
        )

        approved = _decision_is_approval(decision)
        if not approved:
            return {
                "safety_flags": [flag],
                "escalation_id": escalation.id,
                "halted": True,
                "status": "terminated",
                "summary": message,
            }

        return {
            "safety_flags": [flag],
            "escalation_id": escalation.id,
            "current_step": "intent_detection",
        }

    return safety_gate


def _prior_review_decision(db, workflow_run_id: int) -> tuple[str | None, int | None]:
    """Has a human already ruled on this run's safety escalation?

    Returns ("approved" | "rejected" | None, escalation_id). Used to make the safety
    gate idempotent, since LangGraph replays an interrupted node from the beginning when
    the graph resumes — without this, every approval would spawn a fresh escalation.
    """
    decided = [
        escalation
        for escalation in escalation_service.list_for_workflow(db, workflow_run_id)
        if escalation.status != EscalationStatus.PENDING
    ]
    if not decided:
        return None, None

    latest = max(decided, key=lambda e: e.resolved_at or e.created_at)
    if latest.status == EscalationStatus.REJECTED:
        return "rejected", latest.id
    return "approved", latest.id


def _decision_is_approval(decision: Any) -> bool:
    """Interpret whatever the resume value carries as approve/reject."""
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, str):
        return decision.strip().lower() in {"approve", "approved", "yes", "true", "continue"}
    if isinstance(decision, dict):
        for key in ("decision", "type", "action", "status"):
            if key in decision:
                return _decision_is_approval(decision[key])
    return False
