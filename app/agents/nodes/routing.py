"""Department routing — matches the request to exactly one hospital department, or
rejects/escalates when it can't."""

from app.agents.agents import routing_agent
from app.agents.nodes.common import NodeFn, build_context, invoke_agent, node_error, persist_step
from app.db.models.enums import EscalationReason, WorkflowStatus, WorkflowStep
from app.services import department_service, escalation_service, workflow_service


def make_routing(db, actor_id: int | None) -> NodeFn:
    def routing(state: dict) -> dict:
        intent = state.get("intent") or {}
        context = build_context(state, db, actor_id=actor_id, actor_label="routing_agent")

        mentioned = intent.get("mentioned_department") or "none stated"
        try:
            decision, scratch = invoke_agent(
                routing_agent(),
                (
                    "Route this request to exactly one department.\n\n"
                    f"Patient request: {state['raw_request']}\n"
                    f"Department the patient named: {mentioned}\n"
                    f"Administrative summary: {intent.get('summary', '')}\n\n"
                    "List the departments, try the keyword table, then decide. "
                    "Escalate rather than guess if it is genuinely unclear."
                ),
                context,
            )
        except Exception as exc:
            return node_error(state, "routing", exc)

        if decision is None:
            return {"errors": ["routing: agent returned no routing decision"]}

        if not decision.is_administrative_request:
            # Not a hospital matter at all (e.g. "I want to buy a mobile") — this needs
            # no human judgement, unlike a genuinely ambiguous department. Reject it
            # directly. If the agent's own flag_uncertain_routing tool already escalated
            # this anyway (despite the prompt saying not to), don't fight it or leave an
            # orphaned pending escalation behind — defer to the human path that's
            # already in motion rather than silently dropping it.
            pending = escalation_service.get_pending_for_workflow(db, state["workflow_run_id"])
            if pending is not None:
                return {
                    "escalation_id": pending.id,
                    "halted": True,
                    "status": "awaiting_review",
                    "summary": (
                        "We couldn't automatically match this to one of our hospital "
                        "departments, so a staff member will review it and follow up."
                    ),
                }
            message = (
                "AgentCare handles hospital administrative tasks only — things like booking "
                "appointments, managing documents, or scheduling reminders. This doesn't look "
                "like something we can help with here."
            )
            workflow_service.save_state(
                db,
                workflow_run_id=state["workflow_run_id"],
                status=WorkflowStatus.TERMINATED,
                state_patch={
                    "summary": message,
                    "out_of_scope": True,
                    "routing_rationale": decision.rationale,
                },
                actor_label="routing_agent",
            )
            return {"halted": True, "status": "terminated", "summary": message}

        department = (
            department_service.get_department_by_name(db, decision.department_name)
            if decision.department_name
            else None
        )
        if department is None:
            # The model named a department that doesn't exist. Escalate rather than
            # proceeding with a dangling reference.
            escalation = escalation_service.create_escalation(
                db,
                workflow_run_id=state["workflow_run_id"],
                reason=EscalationReason.UNCERTAIN_ROUTING,
                detail=f"Agent proposed unknown department '{decision.department_name}'.",
                actor_label="routing_node",
            )
            return {
                "escalation_id": escalation.id,
                "halted": True,
                "status": "awaiting_review",
                # Deliberately doesn't say "confirm the right department" — that
                # overclaims when the real issue is that the request isn't a hospital
                # administrative matter at all (e.g. "I want to buy a mobile"), not
                # just an ambiguous one. This wording is honest either way.
                "summary": (
                    "We couldn't automatically match this to one of our hospital "
                    "departments, so a staff member will review it and follow up."
                ),
            }

        if decision.needs_human_routing or decision.confidence < 0.5:
            escalation = escalation_service.get_pending_for_workflow(db, state["workflow_run_id"])
            if escalation is None:
                escalation = escalation_service.create_escalation(
                    db,
                    workflow_run_id=state["workflow_run_id"],
                    reason=EscalationReason.UNCERTAIN_ROUTING,
                    detail=f"Low routing confidence ({decision.confidence}). {decision.rationale}",
                    actor_label="routing_node",
                )
            return {
                "department_id": department.id,
                "department_name": department.name,
                "routing_confidence": decision.confidence,
                "escalation_id": escalation.id,
                "halted": True,
                "status": "awaiting_review",
                # Deliberately doesn't say "confirm the right department" — that
                # overclaims when the real issue is that the request isn't a hospital
                # administrative matter at all (e.g. "I want to buy a mobile"), not
                # just an ambiguous one. This wording is honest either way.
                "summary": (
                    "We couldn't automatically match this to one of our hospital "
                    "departments, so a staff member will review it and follow up."
                ),
            }

        persist_step(
            db,
            state,
            WorkflowStep.DEPARTMENT_ROUTING,
            {
                "department_id": department.id,
                "department_name": department.name,
                "routing_confidence": decision.confidence,
                "routing_rationale": decision.rationale,
            },
            actor_label="routing_agent",
        )
        return {
            "department_id": department.id,
            "department_name": department.name,
            "routing_confidence": decision.confidence,
            "current_step": "appointment",
            "scratch": scratch,
        }

    return routing
