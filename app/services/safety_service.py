"""Deterministic clinical-safety scanning.

This is layer 2 of the safety boundary (layer 1 being system prompts, layer 3 the
Safety agent's LLM verdict). It is pure Python with no model call, which is the point:
a regex cannot be talked out of firing by a cleverly worded request or by instructions
smuggled inside an uploaded document. It runs on every model call and every tool call
via ClinicalSafetyMiddleware.

The scanner is deliberately tuned to over-trigger rather than under-trigger. A false
positive costs a staff member ten seconds of review; a false negative means AgentCare
gave medical advice, which fails the challenge outright and could harm someone.
"""

import re
from dataclasses import dataclass, field

from app.db.models.enums import EscalationReason

# --- Emergency indicators -------------------------------------------------------
# Phrases describing an in-progress medical emergency. These route to immediate human
# attention regardless of anything else in the request.
EMERGENCY_PATTERNS = [
    r"\bchest pain\b",
    r"\bheart attack\b",
    r"\bstroke\b",
    r"\bcan'?t breathe\b",
    r"\b(difficulty|trouble) breathing\b",
    r"\bshortness of breath\b",
    r"\bunconscious\b",
    r"\bnot breathing\b",
    r"\bsevere bleeding\b",
    r"\bbleeding heavily\b",
    r"\bsuicid(e|al)\b",
    r"\bkill myself\b",
    r"\bself[- ]harm\b",
    r"\boverdose\b",
    r"\bpoison(ed|ing)?\b",
    r"\bseizure\b",
    r"\bcollapsed?\b",
    r"\banaphyla(xis|ctic)\b",
    r"\bemergency\b",
    r"\bambulance\b",
    r"\bdying\b",
    r"\bsevere pain\b",
]

# --- Requests for medical advice ------------------------------------------------
# Asking the system to diagnose, treat, or dose. These are the behaviors the challenge
# rules prohibit outright.
DIAGNOSIS_PATTERNS = [
    r"\bdiagnos(e|is|ing)\b",
    r"\bwhat('?s| is) wrong with me\b",
    r"\bwhat (disease|condition|illness)\b",
    r"\bdo i have\b",
    r"\bam i (sick|ill|dying)\b",
    r"\bis (this|it|that)\b.{0,20}?\b(serious|cancer|fatal|dangerous)\b",
    # The statement form of the same question ("this rash is serious", "that lump is
    # cancer") — a patient echoing what someone told them, or a jailbreak-style prompt
    # ("tell me if this rash is serious") that puts words between the subject and "is".
    r"\b(this|it|that)\b.{0,25}?\bis\b.{0,10}?\b(serious|cancer|fatal|dangerous)\b",
    # Allow qualifiers between the determiner and the noun ("my blood results", "these
    # latest scan reports"), otherwise the most natural phrasings slip straight through.
    r"\bwhat do (my|these|the)\b.{0,25}?\b(results?|reports?|scans?|symptoms?|numbers?|levels?)\b"
    r".{0,15}?\bmean\b",
    r"\bwhat does (my|this|the)\b.{0,25}?\b(result|report|scan|ecg|reading|level)\b.{0,15}?\bmean\b",
    r"\binterpret\b.{0,20}?\b(result|report|scan|ecg|blood|x-?ray)\b",
    r"\bexplain\b.{0,20}?\b(my|these|the)\b.{0,20}?\b(results?|reports?|scans?)\b",
    r"\bshould i be worried\b",
]

PRESCRIPTION_PATTERNS = [
    # "prescribe/prescribing" is the system being asked to act as a prescriber.
    # The *noun* "prescription" is deliberately excluded here — "attach my prescription
    # record" is an ordinary document-upload request, and flagging it would escalate a
    # core administrative flow on every use.
    r"\bprescrib(e|ing|ed to me)\b",
    r"\bwrite me a prescription\b",
    r"\bwhat medicine\b",
    r"\bwhich (medicine|drug|tablet|medication)\b",
    r"\b(should|can) i take\b",
    r"\bincrease (my|the) dose\b",
    r"\bdecrease (my|the) dose\b",
    r"\bchange (my|the) (dose|dosage|medication)\b",
    r"\bstop taking\b",
    r"\bhow (much|many) (should|do) i take\b",
    r"\bis it safe to take\b",
    r"\brefill\b.*\b(without|no) (doctor|prescription)\b",
]

# Dosage expressed numerically — "500mg twice daily", "2 tablets a day".
DOSAGE_PATTERNS = [
    r"\b\d+\s?(mg|mcg|ml|g|iu|units?)\b",
    r"\b\d+\s?(tablet|capsule|pill|dose)s?\b",
    r"\b(twice|thrice|once)\s+(a|per)\s+day\b",
    r"\b\d+\s?times?\s+(a|per)\s+day\b",
]

# A small illustrative formulary. Real deployments would load a full drug vocabulary
# (RxNorm or similar); the pattern of checking against a maintained list is the point.
DRUG_NAME_PATTERNS = [
    r"\bmetformin\b", r"\binsulin\b", r"\baspirin\b", r"\bibuprofen\b",
    r"\bparacetamol\b", r"\bacetaminophen\b", r"\bamoxicillin\b", r"\batorvastatin\b",
    r"\bwarfarin\b", r"\bomeprazole\b", r"\bamlodipine\b", r"\bstatins?\b",
    r"\bantibiotics?\b", r"\bsteroids?\b", r"\bpainkillers?\b",
]

_COMPILED: dict[str, list[re.Pattern]] = {
    "emergency": [re.compile(p, re.IGNORECASE) for p in EMERGENCY_PATTERNS],
    "diagnosis": [re.compile(p, re.IGNORECASE) for p in DIAGNOSIS_PATTERNS],
    "prescription": [re.compile(p, re.IGNORECASE) for p in PRESCRIPTION_PATTERNS],
    "dosage": [re.compile(p, re.IGNORECASE) for p in DOSAGE_PATTERNS],
    "drug_name": [re.compile(p, re.IGNORECASE) for p in DRUG_NAME_PATTERNS],
}


@dataclass
class SafetyScanResult:
    """Outcome of a deterministic scan.

    `is_flagged` means *action is required* (escalate / block), not merely that some
    pattern matched. A bare drug name or dosage figure is recorded in `categories` for
    the audit trail but does not by itself flag the request — "attach my metformin
    prescription record" is ordinary administrative work, and escalating it would both
    bury staff in noise and break a core document-upload flow.
    """

    is_flagged: bool = False
    is_emergency: bool = False
    is_medical_advice: bool = False
    categories: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)

    @property
    def escalation_reason(self) -> EscalationReason | None:
        if self.is_emergency:
            return EscalationReason.EMERGENCY_LANGUAGE
        if self.is_medical_advice:
            return EscalationReason.MEDICAL_ADVICE_REQUEST
        return None

    def summary(self) -> str:
        if not self.is_flagged:
            return "No safety concerns detected."
        return (
            f"Flagged categories: {', '.join(self.categories)}. "
            f"Matched: {', '.join(self.matched_terms[:8])}"
        )


def scan_text(text: str) -> SafetyScanResult:
    """Scan text for emergency and medical-advice indicators."""
    result = SafetyScanResult()
    if not text or not text.strip():
        return result

    hits: dict[str, list[str]] = {}
    for category, patterns in _COMPILED.items():
        matches = [m.group(0) for pattern in patterns if (m := pattern.search(text))]
        if matches:
            hits[category] = matches

    if not hits:
        return result

    # Record everything seen, for the audit trail.
    result.categories = sorted(hits)
    result.matched_terms = [term for terms in hits.values() for term in terms]

    result.is_emergency = "emergency" in hits
    result.is_medical_advice = any(c in hits for c in ("diagnosis", "prescription"))

    # A drug name or dosage figure is only advice-seeking when the patient is asking
    # about taking or changing medication, rather than naming a document.
    if not result.is_medical_advice and {"dosage", "drug_name"} & set(hits):
        if re.search(
            r"\b(should|can|could|may|is it safe to|how much|how many)\b.{0,40}?\b(i|my|me)\b",
            text,
            re.IGNORECASE,
        ):
            result.is_medical_advice = True

    result.is_flagged = result.is_emergency or result.is_medical_advice
    return result


def scan_agent_output(text: str) -> SafetyScanResult:
    """Scan an agent's outgoing text for content it must never produce.

    Stricter than `scan_text`: for patient-bound output, any diagnostic assertion,
    prescription, or dosage instruction is a violation regardless of phrasing, since
    the system is only ever permitted to speak administratively.
    """
    result = SafetyScanResult()
    if not text or not text.strip():
        return result

    assertion_patterns = [
        r"\byou (have|likely have|probably have|may have|might have)\b",
        r"\byou('re| are) (suffering from|experiencing)\b",
        r"\bthis (indicates|suggests|means you)\b",
        r"\byour (results?|reports?) (show|indicate|suggest)\b",
        r"\bi (recommend|suggest) (taking|you take)\b",
        r"\byou should take\b",
        r"\bdiagnos(ed|is) (is|of)\b",
    ]
    hits: list[str] = [
        m.group(0)
        for pattern in assertion_patterns
        if (m := re.search(pattern, text, re.IGNORECASE))
    ]

    dosage_hits = [m.group(0) for p in _COMPILED["dosage"] if (m := p.search(text))]
    prescription_hits = [m.group(0) for p in _COMPILED["prescription"] if (m := p.search(text))]

    if hits or dosage_hits or prescription_hits:
        result.is_flagged = True
        result.is_medical_advice = True
        result.matched_terms = hits + dosage_hits + prescription_hits
        result.categories = ["unsafe_agent_output"]

    return result


SAFE_DEFLECTION_MESSAGE = (
    "I can help with administrative tasks such as booking appointments, routing your "
    "request to the right department, and organising your documents. I can't provide "
    "medical guidance, so I've passed this to our staff for a clinician to review."
)

EMERGENCY_MESSAGE = (
    "This looks like it may be urgent. If this is a medical emergency, please call your "
    "local emergency number or go to the nearest emergency department now. I've flagged "
    "your request for immediate staff attention."
)
