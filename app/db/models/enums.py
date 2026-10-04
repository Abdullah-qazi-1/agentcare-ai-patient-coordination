import enum


class UserRole(str, enum.Enum):
    PATIENT = "patient"
    STAFF = "staff"
    ADMIN = "admin"


class SlotStatus(str, enum.Enum):
    OPEN = "open"
    HELD = "held"
    BOOKED = "booked"


class AppointmentStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class DocumentType(str, enum.Enum):
    ECG_REPORT = "ecg_report"
    BLOOD_REPORT = "blood_report"
    IMAGING_REPORT = "imaging_report"
    PRESCRIPTION_RECORD = "prescription_record"
    DISCHARGE_SUMMARY = "discharge_summary"
    INSURANCE_CARD = "insurance_card"
    IDENTITY_PROOF = "identity_proof"
    REFERRAL_LETTER = "referral_letter"
    OTHER = "other"


class WorkflowStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    FAILED = "failed"


class WorkflowStep(str, enum.Enum):
    REGISTRATION = "registration"
    INTENT_DETECTION = "intent_detection"
    DEPARTMENT_ROUTING = "department_routing"
    APPOINTMENT = "appointment"
    DOCUMENT_COORDINATION = "document_coordination"
    FOLLOW_UP = "follow_up"
    CONFIRMATION = "confirmation"


class ReminderType(str, enum.Enum):
    APPOINTMENT_REMINDER = "appointment_reminder"
    DOCUMENT_REQUEST = "document_request"
    FOLLOW_UP_VISIT = "follow_up_visit"


class ReminderStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    CANCELLED = "cancelled"


class EscalationReason(str, enum.Enum):
    EMERGENCY_LANGUAGE = "emergency_language"
    MEDICAL_ADVICE_REQUEST = "medical_advice_request"
    UNCERTAIN_ROUTING = "uncertain_routing"
    SENSITIVE_ACTION = "sensitive_action"
    REPEATED_TOOL_FAILURE = "repeated_tool_failure"
    LOW_CONFIDENCE_CLASSIFICATION = "low_confidence_classification"
    # A run hit its token ceiling and handed off to a human. Distinct from a tool
    # failure: nothing broke, the request simply cost more than its budget allowed,
    # and staff triaging the queue need to see that difference.
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    # NeMo Guardrails' semantic input/output rail fired — distinct from the
    # deterministic MEDICAL_ADVICE_REQUEST/EMERGENCY_LANGUAGE reasons above, which come
    # from the regex scanner, not the LLM-based rail check.
    GUARDRAILS_BLOCKED = "guardrails_blocked"


class EscalationStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"
