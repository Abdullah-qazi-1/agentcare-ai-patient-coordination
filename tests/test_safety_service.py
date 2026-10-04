"""The deterministic safety scanner — layer 2 of the clinical boundary.

No database and no model call: this is the floor the LLM layers sit on top of, and
the one layer that cannot be argued out of firing. The benign cases matter as much as
the adversarial ones — a scanner that flags "attach my metformin prescription record"
would bury staff in noise and break document upload, which is a core flow.
"""

import pytest

from app.db.models.enums import EscalationReason
from app.services.safety_service import scan_agent_output, scan_text


class TestEmergencyDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "I have severe chest pain right now",
            "I think I'm having a heart attack",
            "my father is unconscious and not breathing",
            "I can't breathe properly",
            "she had a seizure this morning",
            "I need an ambulance",
            "this is an emergency",
            "I've been having thoughts of suicide",
            "there is severe bleeding from the wound",
        ],
    )
    def test_emergency_language_flags(self, text):
        result = scan_text(text)
        assert result.is_flagged
        assert result.is_emergency
        assert result.escalation_reason is EscalationReason.EMERGENCY_LANGUAGE

    def test_emergency_outranks_advice(self):
        """An emergency reason wins even when advice patterns also match."""
        result = scan_text("I have chest pain, what medicine should I take?")
        assert result.is_emergency and result.is_medical_advice
        assert result.escalation_reason is EscalationReason.EMERGENCY_LANGUAGE


class TestMedicalAdviceDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "can you diagnose what I have?",
            "what's wrong with me?",
            "do I have diabetes?",
            "what do my blood results mean?",
            "what do these latest scan reports mean?",
            "can you interpret my ECG report",
            "should I be worried about this?",
            "which medicine should I use for this",
            "can you prescribe something",
            "should I increase my dose",
            "is it safe to take these together",
            "should I stop taking my tablets",
        ],
    )
    def test_advice_requests_flag(self, text):
        result = scan_text(text)
        assert result.is_flagged
        assert result.is_medical_advice
        assert result.escalation_reason is EscalationReason.MEDICAL_ADVICE_REQUEST


class TestBenignAdministrativeLanguage:
    """The false-positive guard. Each of these is ordinary administrative work."""

    @pytest.mark.parametrize(
        "text",
        [
            "I need a cardiology appointment next week",
            "please attach my metformin prescription record to my file",
            "can you upload my insurance card",
            "I want to reschedule my Tuesday appointment",
            "please cancel my appointment with Dr. Fischer",
            "I'd like to book a routine checkup",
            "here is my ECG report from last month",
            "can you send my discharge summary to the orthopedics department",
            "what time is my appointment tomorrow?",
            "I need to update my phone number",
        ],
    )
    def test_administrative_requests_pass(self, text):
        result = scan_text(text)
        assert not result.is_flagged, f"false positive on: {text!r} ({result.summary()})"
        assert result.escalation_reason is None

    def test_drug_name_alone_is_recorded_but_not_flagged(self):
        """A bare drug name is audit-worthy context, not an advice request."""
        result = scan_text("please file my metformin prescription record")
        assert not result.is_flagged
        assert "drug_name" in result.categories  # still recorded for the audit trail

    def test_dosage_figure_alone_does_not_flag(self):
        """A dosage on an uploaded document is a fact, not a question."""
        result = scan_text("the report lists metformin 500mg twice a day")
        assert not result.is_flagged

    def test_dosage_becomes_advice_when_asking_about_self(self):
        result = scan_text("should I take metformin 500mg twice a day?")
        assert result.is_flagged
        assert result.is_medical_advice


class TestScanEdgeCases:
    @pytest.mark.parametrize("text", ["", "   ", "\n\t "])
    def test_empty_input_is_not_flagged(self, text):
        result = scan_text(text)
        assert not result.is_flagged
        assert result.categories == []

    def test_case_insensitive(self):
        assert scan_text("SEVERE CHEST PAIN").is_emergency
        assert scan_text("Can You DIAGNOSE Me").is_medical_advice

    def test_summary_reports_categories_when_flagged(self):
        result = scan_text("I have chest pain")
        assert "Flagged categories" in result.summary()
        assert "No safety concerns" in scan_text("book me an appointment").summary()


class TestAgentOutputScanning:
    """Stricter: the system may only ever speak administratively."""

    @pytest.mark.parametrize(
        "text",
        [
            "You have a mild arrhythmia based on this reading.",
            "You may have an infection.",
            "Your results show elevated cholesterol.",
            "This indicates a problem with your heart valve.",
            "I recommend taking ibuprofen for the pain.",
            "You should take 500mg twice a day.",
        ],
    )
    def test_unsafe_output_is_flagged(self, text):
        result = scan_agent_output(text)
        assert result.is_flagged
        assert result.categories == ["unsafe_agent_output"]

    @pytest.mark.parametrize(
        "text",
        [
            "Your appointment with Dr. Iyer is confirmed for Tuesday at 10:00.",
            "I've filed your ECG report and routed your request to Cardiology.",
            "A staff member will review your request shortly.",
            "I've scheduled a reminder 24 hours before your appointment.",
        ],
    )
    def test_administrative_output_passes(self, text):
        assert not scan_agent_output(text).is_flagged

    def test_empty_output_passes(self):
        assert not scan_agent_output("").is_flagged
