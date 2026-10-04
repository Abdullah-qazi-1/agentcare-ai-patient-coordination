"""Fake scripted agents for exercising the LangGraph pipeline with no live LLM call.

Each fake mimics the interface `app/agents/nodes/common.py::invoke_agent` expects —
`.invoke({"messages": [...]}, context=AgentCareContext) -> {"structured_response": <model>}`
— matching exactly what `create_agent()` returns from a real call. Where a real agent's
tool would write to the database, the fake calls the same service function directly, so
a graph test using these still exercises the real service layer end to end (a real
`Appointment` row, a real `Escalation` row) rather than mocking it away. This is what
`create_run` → `build_graph` → `graph.invoke` are for real, minus the network call.

`patch_agents` monkeypatches the six factory functions where each `app/agents/nodes/*.py`
module imported them (patching `app.agents.agents` itself would miss this, since
`from x import y` binds a local name in the importing module).
"""

from __future__ import annotations

from app.db.models import DocumentType, EscalationReason, ReminderType
from app.schemas.agent import (
    AdminIntent,
    AppointmentOutcome,
    DocumentClassification,
    FollowUpPlan,
    RoutingDecision,
    SafetyVerdict,
)
from app.services import appointment_service, department_service, escalation_service, reminder_service


class FakeCoordinatorAgent:
    def __init__(
        self,
        *,
        needs_appointment: bool = True,
        needs_documents: bool = False,
        primary_action: str = "book_appointment",
        mentioned_department: str | None = None,
        timing_preference: str = "next week",
    ):
        self.needs_appointment = needs_appointment
        self.needs_documents = needs_documents
        self.primary_action = primary_action
        self.mentioned_department = mentioned_department
        self.timing_preference = timing_preference
        self.call_count = 0

    def invoke(self, _input, *, context):
        self.call_count += 1
        intent = AdminIntent(
            primary_action=self.primary_action,
            needs_appointment=self.needs_appointment,
            needs_document_handling=self.needs_documents,
            mentioned_department=self.mentioned_department,
            timing_preference=self.timing_preference,
            summary="Patient requests administrative action.",
        )
        return {"structured_response": intent}


class FakeRoutingAgent:
    def __init__(
        self,
        *,
        department_name: str | None = "Cardiology",
        confidence: float = 0.9,
        needs_human_routing: bool = False,
        is_administrative_request: bool = True,
    ):
        self.department_name = department_name
        self.confidence = confidence
        self.needs_human_routing = needs_human_routing
        self.is_administrative_request = is_administrative_request
        self.call_count = 0

    def invoke(self, _input, *, context):
        self.call_count += 1
        # Written to scratch so the fake appointment agent below knows which department's
        # slots to search — the harness's stand-in for what a real routing tool call
        # would leave behind for a later stage to read.
        department = (
            department_service.get_department_by_name(context.db, self.department_name)
            if self.department_name
            else None
        )
        if department:
            context.scratch["department_id"] = department.id
        decision = RoutingDecision(
            department_name=self.department_name,
            confidence=self.confidence,
            rationale="Patient asked for this department by name.",
            needs_human_routing=self.needs_human_routing,
            is_administrative_request=self.is_administrative_request,
        )
        return {"structured_response": decision}


class FakeAppointmentAgent:
    """Books the first open slot in the department routing already chose."""

    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail
        self.call_count = 0

    def invoke(self, _input, *, context):
        self.call_count += 1
        department_id = context.scratch.get("department_id")
        slots = [] if self.should_fail else appointment_service.get_available_slots(
            context.db, department_id=department_id, limit=1
        )
        if not slots:
            outcome = AppointmentOutcome(action_taken="failed", explanation="No open slots available.")
            return {"structured_response": outcome}

        appointment = appointment_service.book_appointment(
            context.db,
            patient_id=context.patient_id,
            slot_id=slots[0].slot_id,
            reason="cardiology follow-up",
            actor_id=context.actor_id,
            actor_label="appointment_agent",
            workflow_run_id=context.workflow_run_id,
        )
        context.scratch["appointment_id"] = appointment.id
        outcome = AppointmentOutcome(
            action_taken="booked",
            appointment_id=appointment.id,
            slot_id=slots[0].slot_id,
            explanation="Booked the first available slot.",
        )
        return {"structured_response": outcome}


class FakeDocumentAgent:
    def __init__(self):
        self.call_count = 0

    def invoke(self, _input, *, context):
        self.call_count += 1
        classification = DocumentClassification(
            document_type=DocumentType.OTHER, confidence=0.5, reasoning="stub classification"
        )
        return {"structured_response": classification}


class FakeFollowupAgent:
    def __init__(self):
        self.call_count = 0

    def invoke(self, _input, *, context):
        self.call_count += 1
        appointment_id = context.scratch.get("appointment_id")
        if appointment_id:
            reminder = reminder_service.create_appointment_reminder(
                context.db,
                patient_id=context.patient_id,
                appointment_id=appointment_id,
                days_before=1,
                actor_label="followup_agent",
                workflow_run_id=context.workflow_run_id,
            )
            context.scratch["reminder_id"] = reminder.id
        plan = FollowUpPlan(
            reminder_type=ReminderType.APPOINTMENT_REMINDER,
            days_before_appointment=1,
            message="Reminder: you have an upcoming appointment.",
        )
        return {"structured_response": plan}


class FakeSafetyAgent:
    """Mimics the real safety agent's `create_escalation` tool call.

    `call_count` is what proves (or disproves) the idempotent-resume guarantee: a
    correctly-behaving resume, after staff have already recorded a decision, must not
    invoke this a second time.
    """

    def __init__(
        self,
        *,
        is_emergency: bool = True,
        reason: EscalationReason = EscalationReason.EMERGENCY_LANGUAGE,
        patient_safe_message: str = "A staff member will review this shortly.",
    ):
        self.is_emergency = is_emergency
        self.reason = reason
        self.patient_safe_message = patient_safe_message
        self.call_count = 0

    def invoke(self, _input, *, context):
        self.call_count += 1
        escalation_service.create_escalation(
            context.db,
            workflow_run_id=context.workflow_run_id,
            reason=self.reason,
            detail="Flagged by the deterministic scan (scripted test).",
            actor_label="safety_agent",
        )
        verdict = SafetyVerdict(
            is_emergency=self.is_emergency,
            is_medical_advice_request=not self.is_emergency,
            is_sensitive=False,
            confidence=0.95,
            escalation_reason=self.reason,
            patient_safe_message=self.patient_safe_message,
        )
        return {"structured_response": verdict}


def patch_agents(
    monkeypatch,
    *,
    coordinator=None,
    routing=None,
    appointment=None,
    document=None,
    followup=None,
    safety=None,
):
    """Monkeypatch all six agent factories, one per node module since the split of the
    old monolithic pipeline.py means each factory is now imported into its own module.

    Every stage gets a harmless default fake so a test only needs to override the ones
    its scenario actually exercises; the rest won't be reached given the request text
    they use, but are safe if they are.
    """
    import app.agents.nodes.appointment as appointment_module
    import app.agents.nodes.coordinator as coordinator_module
    import app.agents.nodes.document as document_module
    import app.agents.nodes.followup as followup_module
    import app.agents.nodes.routing as routing_module
    import app.agents.nodes.safety_gate as safety_module

    fakes = {
        "coordinator_agent": coordinator or FakeCoordinatorAgent(),
        "routing_agent": routing or FakeRoutingAgent(),
        "appointment_agent": appointment or FakeAppointmentAgent(),
        "document_agent": document or FakeDocumentAgent(),
        "followup_agent": followup or FakeFollowupAgent(),
        "safety_agent": safety or FakeSafetyAgent(),
    }
    modules = {
        "coordinator_agent": coordinator_module,
        "routing_agent": routing_module,
        "appointment_agent": appointment_module,
        "document_agent": document_module,
        "followup_agent": followup_module,
        "safety_agent": safety_module,
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(modules[name], name, lambda _fake=fake: _fake)
    return fakes
