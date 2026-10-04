"""Structured-output contracts for the agents.

Each of these is passed as `response_format=` to a `create_agent()` instance, so the
model returns exactly these fields in one call — no free-text parsing and no
"please reformat as JSON" retry tax. Field descriptions are part of the prompt the
model sees, so they are written as instructions, not as internal notes.
"""

from pydantic import BaseModel, Field

from app.db.models.enums import DocumentType, EscalationReason, ReminderType


class AdminIntent(BaseModel):
    """Coordinator output: what the patient administratively wants."""

    primary_action: str = Field(
        description=(
            "One of: book_appointment, reschedule_appointment, cancel_appointment, "
            "upload_documents, check_status, general_enquiry"
        )
    )
    needs_appointment: bool = Field(
        description="True if this request involves booking/changing an appointment."
    )
    needs_document_handling: bool = Field(description="True if documents are attached or referenced.")
    mentioned_department: str | None = Field(
        default=None, description="Department named by the patient, if any. Do not infer from symptoms."
    )
    timing_preference: str | None = Field(
        default=None, description="Any timing preference in the patient's own words, e.g. 'next week'."
    )
    summary: str = Field(
        description="One neutral administrative sentence restating the request. No clinical judgement."
    )


class RoutingDecision(BaseModel):
    """Department Routing agent output."""

    department_name: str | None = Field(
        default=None,
        description="Exact name of an existing department from the lookup tool. Omit only when "
        "is_administrative_request is False — there is no department to name.",
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0.0-1.0 confidence in this routing.")
    rationale: str = Field(
        description=(
            "Administrative reason for the routing, e.g. 'patient asked for a cardiology follow-up'. "
            "Never state or imply a diagnosis."
        )
    )
    needs_human_routing: bool = Field(
        default=False,
        description="True if the request IS hospital administration but which department is genuinely "
        "unclear. Use is_administrative_request instead for requests unrelated to a hospital at all.",
    )
    is_administrative_request: bool = Field(
        default=True,
        description=(
            "False ONLY if the request has nothing to do with hospital administration at all — "
            "unrelated shopping, travel, or any other non-healthcare request. This is rejected "
            "directly with no staff review, so set it conservatively: any genuine medical or "
            "administrative ambiguity must use needs_human_routing instead, not this field."
        ),
    )


class AppointmentOutcome(BaseModel):
    """Appointment agent output."""

    action_taken: str = Field(description="One of: booked, rescheduled, cancelled, no_action, failed")
    appointment_id: int | None = Field(default=None, description="ID returned by the booking tool.")
    slot_id: int | None = None
    explanation: str = Field(description="Plain-language administrative explanation of what was done.")


class DocumentClassification(BaseModel):
    """Document agent per-file classification."""

    document_type: DocumentType = Field(description="Best-matching document type from the allowed set.")
    confidence: float = Field(ge=0.0, le=1.0)
    document_date_iso: str | None = Field(
        default=None, description="Date on the document in YYYY-MM-DD form, if one is present."
    )
    reasoning: str = Field(description="Why this type was chosen, based on filename/content only.")


class FollowUpPlan(BaseModel):
    """Follow-up agent output."""

    reminder_type: ReminderType
    days_before_appointment: int = Field(
        default=1, ge=0, le=30, description="How many days before the appointment to send the reminder."
    )
    create_followup_task: bool = Field(default=False)
    followup_days_after: int | None = Field(
        default=None, description="Days after the visit for an administrative follow-up task."
    )
    message: str = Field(
        description="Administrative reminder text. Never include clinical advice or instructions."
    )


class SafetyVerdict(BaseModel):
    """Safety agent judgement on a request or an agent response."""

    is_emergency: bool = Field(
        description="True if the text describes a medical emergency needing immediate care."
    )
    is_medical_advice_request: bool = Field(
        description="True if the text asks for diagnosis, treatment, medication, or dosage guidance."
    )
    is_sensitive: bool = Field(description="True if the request touches a sensitive administrative matter.")
    confidence: float = Field(ge=0.0, le=1.0)
    escalation_reason: EscalationReason | None = Field(
        default=None, description="Which escalation category applies, if any."
    )
    patient_safe_message: str = Field(
        description=(
            "Message shown to the patient. If unsafe, explain that a staff member will review — "
            "never answer the clinical question, and never suggest what the condition might be."
        )
    )
