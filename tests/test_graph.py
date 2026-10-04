"""LangGraph pipeline tests: topology, a golden end-to-end run, and the safety-escalation
interrupt/resume/idempotency guarantees the README describes.

These use the scripted fakes in `tests/agent_fakes.py` instead of a live LLM — no
LLM API key is needed to run this file, matching how every other test in this suite
runs. What's real: the graph, the conditional routing, the interrupt/resume mechanics,
the checkpointer, and every database write (appointments, reminders, escalations,
workflow runs, audit events) the fakes trigger via the actual service layer.
"""

from langgraph.types import Command

from app.db.models import AppointmentStatus, EscalationReason, EscalationStatus, WorkflowStatus
from app.services import appointment_service, escalation_service, workflow_service
from tests.agent_fakes import FakeCoordinatorAgent, FakeRoutingAgent, FakeSafetyAgent, patch_agents


def _new_run(db, patient, text):
    from app.agents.state import initial_state

    run = workflow_service.create_run(db, patient_id=patient.id, raw_request=text)
    state = initial_state(workflow_run_id=run.id, patient_id=patient.id, raw_request=text)
    return run, state


class TestGraphTopology:
    def test_build_graph_compiles(self, db):
        from app.agents.graph import build_graph

        graph = build_graph(db, with_checkpointer=False)
        assert graph is not None


class TestGoldenPath:
    def test_benign_appointment_request_completes_and_persists(
        self, db, monkeypatch, patient, cardiology, doctor, slots
    ):
        from app.agents.graph import build_graph, thread_config

        patch_agents(
            monkeypatch,
            coordinator=FakeCoordinatorAgent(needs_appointment=True, needs_documents=False),
            routing=FakeRoutingAgent(department_name="Cardiology"),
        )
        run, state = _new_run(db, patient, "I need a cardiology appointment next week")
        graph = build_graph(db, with_checkpointer=False)

        final = graph.invoke(state, config=thread_config(run.id))

        assert final["status"] == "completed"
        assert final["appointment_id"] is not None
        assert "reminder" in final["summary"].lower() or final.get("reminder_id")

        db.refresh(run)
        assert run.status == WorkflowStatus.COMPLETED
        assert run.state_snapshot.get("summary")

        from app.services import appointment_service

        booked = appointment_service.list_patient_appointments(db, patient.id)
        assert len(booked) == 1
        assert booked[0].status == AppointmentStatus.CONFIRMED

        from app.services import reminder_service

        reminders = reminder_service.list_patient_reminders(db, patient.id)
        assert len(reminders) == 1
        assert reminders[0].appointment_id == booked[0].id


class TestSafetyEscalationInterrupt:
    def test_flagged_request_suspends_the_graph_and_creates_one_escalation(
        self, db, monkeypatch, patient, in_memory_checkpointer
    ):
        from app.agents.graph import build_graph, thread_config

        safety = FakeSafetyAgent(is_emergency=True, reason=EscalationReason.EMERGENCY_LANGUAGE)
        patch_agents(monkeypatch, safety=safety)
        run, state = _new_run(db, patient, "I have severe chest pain")
        graph = build_graph(db)

        final = graph.invoke(state, config=thread_config(run.id))

        assert final.get("__interrupt__")
        assert safety.call_count == 1

        db.refresh(run)
        assert run.status == WorkflowStatus.AWAITING_REVIEW
        pending = escalation_service.get_pending_for_workflow(db, run.id)
        assert pending is not None
        assert pending.reason == EscalationReason.EMERGENCY_LANGUAGE
        assert len(escalation_service.list_for_workflow(db, run.id)) == 1

    def test_approving_without_prior_resolution_still_completes_with_no_duplicate_escalation(
        self, db, monkeypatch, patient, cardiology, in_memory_checkpointer
    ):
        """The graph replays the interrupted node on resume. Even when staff approve via
        the raw `resume_workflow` mechanics (bypassing `resolve_escalation`, as the CLI
        currently does), `create_escalation`'s own idempotency must still hold: exactly
        one escalation row, never two."""
        from app.agents.graph import build_graph, thread_config

        safety = FakeSafetyAgent(is_emergency=True)
        patch_agents(
            monkeypatch,
            safety=safety,
            coordinator=FakeCoordinatorAgent(needs_appointment=False, needs_documents=False),
            routing=FakeRoutingAgent(department_name="Cardiology"),
        )
        run, state = _new_run(db, patient, "I have severe chest pain")
        graph = build_graph(db)
        config = thread_config(run.id)

        first = graph.invoke(state, config=config)
        assert first.get("__interrupt__")

        final = graph.invoke(Command(resume={"decision": "approve"}), config=config)

        assert final.get("status") == "completed"
        assert len(escalation_service.list_for_workflow(db, run.id)) == 1
        db.refresh(run)
        assert run.status == WorkflowStatus.COMPLETED

    def test_resolving_first_then_resuming_does_not_reinvoke_the_safety_agent(
        self, db, monkeypatch, patient, cardiology, actor, in_memory_checkpointer
    ):
        """The regression the README claims but no test previously verified: once a
        human decision is recorded via `resolve_escalation` (as the real API does), the
        safety gate must short-circuit on replay rather than re-running the LLM check."""
        from app.agents.graph import build_graph, thread_config

        safety = FakeSafetyAgent(is_emergency=True)
        patch_agents(
            monkeypatch,
            safety=safety,
            coordinator=FakeCoordinatorAgent(needs_appointment=False, needs_documents=False),
            routing=FakeRoutingAgent(department_name="Cardiology"),
        )
        run, state = _new_run(db, patient, "I have severe chest pain")
        graph = build_graph(db)
        config = thread_config(run.id)

        graph.invoke(state, config=config)
        assert safety.call_count == 1

        pending = escalation_service.get_pending_for_workflow(db, run.id)
        escalation_service.resolve_escalation(
            db, escalation_id=pending.id, decision=EscalationStatus.APPROVED, reviewed_by=actor.id
        )

        final = graph.invoke(Command(resume={"decision": "approve"}), config=config)

        assert final.get("status") == "completed"
        assert safety.call_count == 1, "safety agent was re-invoked on an already-decided resume"

    def test_rejecting_terminates_the_run(self, db, monkeypatch, patient, actor, in_memory_checkpointer):
        from app.agents.graph import build_graph, thread_config

        safety = FakeSafetyAgent(is_emergency=True)
        patch_agents(monkeypatch, safety=safety)
        run, state = _new_run(db, patient, "I have severe chest pain")
        graph = build_graph(db)
        config = thread_config(run.id)

        graph.invoke(state, config=config)
        pending = escalation_service.get_pending_for_workflow(db, run.id)
        escalation_service.resolve_escalation(
            db, escalation_id=pending.id, decision=EscalationStatus.REJECTED, reviewed_by=actor.id
        )

        final = graph.invoke(Command(resume={"decision": "reject"}), config=config)

        assert final.get("status") == "terminated"
        db.refresh(run)
        assert run.status == WorkflowStatus.TERMINATED
        from app.services import appointment_service

        assert appointment_service.list_patient_appointments(db, patient.id) == []


class TestHitlInterruptDiscoverability:
    """A `HumanInTheLoopMiddleware` tool-approval interrupt (reschedule/cancel) has no
    `Escalation` row of its own — unlike a safety escalation, nothing else flips
    `WorkflowRun.status` for it. `_interrupt_result` (called from both `start_workflow`
    and `resume_workflow` whenever the graph suspends) is where that gap was closed;
    reproducing a genuine HITL interrupt needs the real `create_agent()` middleware
    stack (out of scope for the scripted-agent harness), so this exercises the fix
    directly against the exact shape LangChain's HITL middleware produces.
    """

    def test_hitl_style_interrupt_marks_the_run_awaiting_review(self, db, patient):
        from app.agents.runner import _interrupt_result

        run, _ = _new_run(db, patient, "please reschedule my appointment")
        assert run.status == WorkflowStatus.IN_PROGRESS

        # Shape of a real HITL interrupt payload: an `action_requests` list, no
        # `type: safety_escalation` marker — see app/agents/runner.py::_build_resume_payload.
        action_requests = [{"tool": "reschedule_appointment"}]
        interrupts = [type("Interrupt", (), {"value": {"action_requests": action_requests}})()]

        result = _interrupt_result(db, run.id, interrupts)

        assert result.awaiting_human_review is True
        assert result.escalation_id is None
        db.refresh(run)
        assert run.status == WorkflowStatus.AWAITING_REVIEW

    def test_safety_escalation_interrupt_is_not_double_written(self, db, patient):
        """The safety-escalation path already sets AWAITING_REVIEW via
        `create_escalation` — `_interrupt_result` must not redundantly call
        `save_state` again for it (that would show as a spurious extra audit event)."""
        from app.agents.runner import _interrupt_result

        run, _ = _new_run(db, patient, "I have chest pain")
        escalation_service.create_escalation(
            db, workflow_run_id=run.id, reason=EscalationReason.EMERGENCY_LANGUAGE, detail="test"
        )
        db.refresh(run)
        assert run.status == WorkflowStatus.AWAITING_REVIEW
        before = run.updated_at

        interrupts = [type("Interrupt", (), {"value": {"type": "safety_escalation", "escalation_id": 1}})()]
        _interrupt_result(db, run.id, interrupts)

        db.refresh(run)
        assert run.updated_at == before


class TestUnroutableRequest:
    """Two distinct cases, both unrelated to a specific department but not the same
    thing: a request that IS hospital administration but genuinely ambiguous which
    department fits (needs a human) versus one that isn't hospital administration at
    all, like "I want to buy a mobile" (needs no human judgement — reject directly)."""

    def test_low_confidence_routing_halts_with_an_honest_message_and_books_nothing(
        self, db, monkeypatch, patient, cardiology
    ):
        from app.agents.graph import build_graph, thread_config

        patch_agents(
            monkeypatch,
            coordinator=FakeCoordinatorAgent(needs_appointment=False, needs_documents=False),
            # A real routing agent would call `flag_uncertain_routing` and return low
            # confidence for something like "buy a mobile" — this is that outcome.
            routing=FakeRoutingAgent(department_name="Cardiology", confidence=0.2, needs_human_routing=True),
        )
        run, state = _new_run(db, patient, "i want to buy mobile")
        graph = build_graph(db, with_checkpointer=False)

        final = graph.invoke(state, config=thread_config(run.id))

        assert final.get("halted") is True
        assert final.get("status") == "awaiting_review"
        assert "confirm the right department" not in final["summary"]
        assert "couldn't automatically match" in final["summary"]

        db.refresh(run)
        assert run.status == WorkflowStatus.AWAITING_REVIEW
        pending = escalation_service.get_pending_for_workflow(db, run.id)
        assert pending is not None
        assert pending.reason == EscalationReason.UNCERTAIN_ROUTING
        assert appointment_service.list_patient_appointments(db, patient.id) == []

    def test_agent_naming_a_nonexistent_department_halts_the_same_way(self, db, monkeypatch, patient):
        from app.agents.graph import build_graph, thread_config

        patch_agents(
            monkeypatch,
            coordinator=FakeCoordinatorAgent(needs_appointment=False, needs_documents=False),
            routing=FakeRoutingAgent(department_name="Not A Real Department"),
        )
        run, state = _new_run(db, patient, "i want to buy mobile")
        graph = build_graph(db, with_checkpointer=False)

        final = graph.invoke(state, config=thread_config(run.id))

        assert final.get("halted") is True
        assert final.get("status") == "awaiting_review"
        assert "confirm the right department" not in final["summary"]

        pending = escalation_service.get_pending_for_workflow(db, run.id)
        assert pending is not None
        assert pending.reason == EscalationReason.UNCERTAIN_ROUTING


class TestOutOfScopeRequest:
    """`is_administrative_request=False` — the request isn't a hospital matter at all.
    Unlike genuine routing uncertainty, this needs no staff review: reject directly,
    tell the patient clearly, and don't leave anything sitting in the staff queue."""

    def test_terminates_directly_with_no_escalation_and_no_staff_review(
        self, db, monkeypatch, patient
    ):
        from app.agents.graph import build_graph, thread_config

        patch_agents(
            monkeypatch,
            coordinator=FakeCoordinatorAgent(needs_appointment=False, needs_documents=False),
            routing=FakeRoutingAgent(department_name=None, is_administrative_request=False),
        )
        run, state = _new_run(db, patient, "i want to buy mobile")
        graph = build_graph(db, with_checkpointer=False)

        final = graph.invoke(state, config=thread_config(run.id))

        assert final.get("halted") is True
        assert final.get("status") == "terminated"
        assert "AgentCare handles hospital administrative tasks only" in final["summary"]
        assert "confirm the right department" not in final["summary"]

        db.refresh(run)
        assert run.status == WorkflowStatus.TERMINATED
        assert escalation_service.get_pending_for_workflow(db, run.id) is None
        assert escalation_service.list_for_workflow(db, run.id) == []
        assert appointment_service.list_patient_appointments(db, patient.id) == []

    def test_defers_to_a_pending_escalation_if_the_agent_made_one_anyway(
        self, db, monkeypatch, patient
    ):
        """Defense in depth: if the agent's own tool call escalated this despite the
        prompt saying not to for out-of-scope requests, don't fight it or leave an
        orphaned pending escalation — the safe fallback is to let the human path that's
        already in motion continue, not to silently override it."""
        from app.agents.graph import build_graph, thread_config

        run, state = _new_run(db, patient, "i want to buy mobile")

        def routing_agent_that_also_escalates():
            class _Agent:
                def invoke(self, _input, *, context):
                    escalation_service.create_escalation(
                        db,
                        workflow_run_id=run.id,
                        reason=EscalationReason.UNCERTAIN_ROUTING,
                        detail="Escalated by the tool despite being out of scope.",
                        actor_label="routing_agent",
                    )
                    from app.schemas.agent import RoutingDecision

                    return {
                        "structured_response": RoutingDecision(
                            department_name=None,
                            confidence=0.0,
                            rationale="Not a hospital matter.",
                            is_administrative_request=False,
                        )
                    }

            return _Agent()

        patch_agents(
            monkeypatch,
            coordinator=FakeCoordinatorAgent(needs_appointment=False, needs_documents=False),
        )
        import app.agents.nodes.routing as routing_module

        monkeypatch.setattr(routing_module, "routing_agent", routing_agent_that_also_escalates)

        graph = build_graph(db, with_checkpointer=False)
        final = graph.invoke(state, config=thread_config(run.id))

        assert final.get("halted") is True
        assert final.get("status") == "awaiting_review"
        pending = escalation_service.get_pending_for_workflow(db, run.id)
        assert pending is not None
        assert len(escalation_service.list_for_workflow(db, run.id)) == 1
