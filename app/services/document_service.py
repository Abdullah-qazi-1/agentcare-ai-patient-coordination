"""Document ingestion, classification support, duplicate detection and gap checks.

Files are stored on disk under a per-patient directory and referenced from the
database by path — the DB holds metadata and the checksum, never the file bytes.
Storage paths are derived from the SHA-256 checksum rather than the uploaded
filename, so a hostile filename can never escape the storage root.
"""

import hashlib
import re
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import Department, DocumentType, PatientDocument
from app.schemas.document import DuplicateCheckResult, MissingDocumentsResult
from app.services import audit_service, safety_service
from app.services.errors import NotFoundError, ValidationError

logger = get_logger(__name__)

STORAGE_ROOT = Path("storage/documents")


def max_file_bytes() -> int:
    """The configured report-size cap, in bytes (`MAX_DOCUMENT_SIZE_MB` in `.env`).

    A function rather than a module constant so it stays live against `Settings` —
    tests can monkeypatch `get_settings` to exercise a smaller cap without needing to
    reach into this module's globals.
    """
    return get_settings().max_document_size_mb * 1024 * 1024

# Filename signals for the deterministic classification fast-path. The Document agent
# calls the LLM only when none of these match, which covers most real uploads.
FILENAME_SIGNALS: list[tuple[DocumentType, tuple[str, ...]]] = [
    (DocumentType.ECG_REPORT, ("ecg", "ekg", "electrocardiogram")),
    (DocumentType.BLOOD_REPORT, ("blood", "cbc", "hemogram", "haemogram", "lipid", "glucose")),
    (
        DocumentType.IMAGING_REPORT,
        ("xray", "x-ray", "mri", "ct-scan", "ctscan", "ultrasound", "sonography", "scan"),
    ),
    (DocumentType.PRESCRIPTION_RECORD, ("prescription", "rx")),
    (DocumentType.DISCHARGE_SUMMARY, ("discharge", "summary")),
    (DocumentType.INSURANCE_CARD, ("insurance", "policy", "tpa")),
    (DocumentType.IDENTITY_PROOF, ("aadhaar", "passport", "id-proof", "idproof", "license")),
    (DocumentType.REFERRAL_LETTER, ("referral", "refer")),
]

_DATE_PATTERNS = [
    (re.compile(r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})"), ("y", "m", "d")),
    (re.compile(r"(\d{1,2})[-_](\d{1,2})[-_](20\d{2})"), ("d", "m", "y")),
]


def compute_checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def classify_by_filename(filename: str) -> tuple[DocumentType | None, float]:
    """Deterministic classification from the filename. Returns (type, confidence)."""
    lowered = filename.lower()
    for doc_type, signals in FILENAME_SIGNALS:
        if any(signal in lowered for signal in signals):
            return doc_type, 0.85
    return None, 0.0


def extract_date_from_filename(filename: str) -> date | None:
    for pattern, order in _DATE_PATTERNS:
        match = pattern.search(filename)
        if not match:
            continue
        parts = dict(zip(order, match.groups(), strict=True))
        try:
            return date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
        except (ValueError, KeyError):
            continue
    return None


def check_duplicate(
    db: Session, *, patient_id: int, checksum: str, document_type: DocumentType | None = None,
    document_date: date | None = None,
) -> DuplicateCheckResult:
    """Two-tier duplicate detection.

    An identical checksum is a certain duplicate (the same file uploaded twice). A
    same-type/same-date match is a likely re-scan of one underlying report — reported
    separately because it warrants a human look rather than automatic rejection.
    """
    exact = db.execute(
        select(PatientDocument).where(
            PatientDocument.patient_id == patient_id, PatientDocument.checksum == checksum
        )
    ).scalars().first()
    if exact:
        return DuplicateCheckResult(is_duplicate=True, existing_document_id=exact.id, match_kind="checksum")

    if document_type and document_date:
        similar = db.execute(
            select(PatientDocument).where(
                PatientDocument.patient_id == patient_id,
                PatientDocument.document_type == document_type,
                PatientDocument.document_date == document_date,
            )
        ).scalars().first()
        if similar:
            return DuplicateCheckResult(
                is_duplicate=True, existing_document_id=similar.id, match_kind="same_type_and_date"
            )

    return DuplicateCheckResult(is_duplicate=False)


def store_document(
    db: Session,
    *,
    patient_id: int,
    filename: str,
    content: bytes,
    document_type: DocumentType,
    confidence: float = 0.0,
    document_date: date | None = None,
    actor_id: int | None = None,
    actor_label: str = "document_agent",
    workflow_run_id: int | None = None,
) -> PatientDocument:
    if not content:
        raise ValidationError("The uploaded file is empty.")
    limit = max_file_bytes()
    if len(content) > limit:
        raise ValidationError(f"File exceeds the {limit // (1024 * 1024)} MB limit.")

    checksum = compute_checksum(content)
    duplicate = check_duplicate(
        db, patient_id=patient_id, checksum=checksum, document_type=document_type, document_date=document_date
    )

    patient_dir = STORAGE_ROOT / f"patient_{patient_id}"
    patient_dir.mkdir(parents=True, exist_ok=True)
    # Name on disk comes from the checksum, never from user input.
    suffix = Path(filename).suffix[:10]
    stored_path = patient_dir / f"{checksum[:16]}{suffix}"
    stored_path.write_bytes(content)

    document = PatientDocument(
        patient_id=patient_id,
        document_type=document_type,
        original_filename=Path(filename).name[:255],
        storage_reference=str(stored_path.as_posix()),
        document_date=document_date,
        checksum=checksum,
        classification_confidence=confidence,
        is_duplicate_of=duplicate.existing_document_id if duplicate.is_duplicate else None,
    )
    db.add(document)
    db.flush()

    audit_service.record(
        db,
        action="document_stored",
        entity_type="patient_document",
        entity_id=document.id,
        actor_id=actor_id,
        actor_label=actor_label,
        metadata={
            "document_type": document_type.value,
            "is_duplicate": duplicate.is_duplicate,
            "duplicate_kind": duplicate.match_kind,
            "confidence": confidence,
            "workflow_run_id": workflow_run_id,
        },
        commit=False,
    )
    db.commit()
    db.refresh(document)
    return document


def check_missing_documents(db: Session, *, patient_id: int, department_id: int) -> MissingDocumentsResult:
    """Compare the department's required document types against what the patient has."""
    dept = db.get(Department, department_id)
    if not dept:
        raise NotFoundError(f"Department {department_id} not found.")

    required: list[DocumentType] = []
    for raw in dept.required_document_types or []:
        try:
            required.append(DocumentType(raw))
        except ValueError:
            continue  # a mis-seeded value must not break the whole gap check

    present_rows = db.execute(
        select(PatientDocument.document_type).where(
            PatientDocument.patient_id == patient_id,
            PatientDocument.is_duplicate_of.is_(None),
        )
    ).scalars().all()
    present = sorted(set(present_rows), key=lambda d: d.value)

    return MissingDocumentsResult(
        department_name=dept.name,
        required=required,
        present=present,
        missing=[doc_type for doc_type in required if doc_type not in present],
    )


def list_patient_documents(db: Session, patient_id: int) -> list[PatientDocument]:
    return list(
        db.execute(
            select(PatientDocument)
            .where(PatientDocument.patient_id == patient_id)
            .order_by(PatientDocument.created_at.desc())
        ).scalars().all()
    )


def get_document(db: Session, document_id: int) -> PatientDocument:
    document = db.get(PatientDocument, document_id)
    if not document:
        raise NotFoundError(f"Document {document_id} not found.")
    return document


# --- Document summaries ------------------------------------------------------------
# On-demand, administrative-only summaries of an uploaded report. Deliberately not
# generated at upload time — not every upload needs one, and every summary costs a
# model call, which is the same "deterministic/cheap first, LLM only when asked"
# posture the rest of this app takes (see README's "Cost and token strategy").

NO_EXTRACTABLE_TEXT_SUMMARY = (
    "Automatic summary isn't available for this file type — please open the file "
    "directly to review its contents."
)

_MAX_SUMMARY_INPUT_CHARS = 8000

_SUMMARY_SYSTEM_PROMPT = (
    "You summarize the ADMINISTRATIVE metadata of a hospital document for staff filing "
    "it, not its clinical content. State only: the report/document type, the date if "
    "stated, the issuing facility or doctor if stated, and a one-line, purely factual "
    "description of what sections or tests the document contains (e.g. 'a lipid panel "
    "with five measured values' — never the values themselves or whether they are "
    "normal). You must NEVER interpret results, state whether anything is normal or "
    "abnormal, diagnose, or suggest treatment — that is not your role and the system "
    "will discard your answer if you do. If the text is unclear or too sparse to state "
    "these facts safely, say exactly that instead of guessing. Keep it to 2-3 sentences."
)


def _extract_text(storage_reference: str, original_filename: str) -> str:
    """Best-effort text extraction for summary generation. Empty string means "can't"."""
    path = Path(storage_reference)
    if not path.exists():
        return ""

    suffix = Path(original_filename).suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if suffix in (".txt", ".text"):
            return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:  # pragma: no cover - a malformed/encrypted file must not crash the request
        logger.exception("document_text_extraction_failed", path=str(path))
        return ""
    return ""


def summarize_document(
    db: Session,
    *,
    document_id: int,
    actor_id: int | None = None,
    actor_label: str = "staff",
) -> PatientDocument:
    """Generate (or regenerate) an administrative-only summary for a document.

    A single direct LLM call — not a tool-calling agent, since there is nothing for an
    agent to decide here — followed by the same output safety scan every agent response
    goes through (`safety_service.scan_agent_output`), because this call bypasses the
    agent graph and its `ClinicalSafetyMiddleware.after_model` hook entirely.
    """
    document = get_document(db, document_id)
    text = _extract_text(document.storage_reference, document.original_filename)

    if not text:
        summary = NO_EXTRACTABLE_TEXT_SUMMARY
    else:
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.agents.llm import get_llm

        llm = get_llm("document")
        response = llm.invoke(
            [
                SystemMessage(content=_SUMMARY_SYSTEM_PROMPT),
                HumanMessage(content=text[:_MAX_SUMMARY_INPUT_CHARS]),
            ]
        )
        candidate = str(response.content).strip()
        scan = safety_service.scan_agent_output(candidate)
        if scan.is_flagged:
            logger.warning(
                "document_summary_blocked", document_id=document_id, matched=scan.matched_terms[:5]
            )
            summary = (
                "Automatic summary was withheld because it read as clinical interpretation "
                "rather than administrative fact — a staff member should review this "
                "document directly."
            )
        else:
            summary = candidate[:2000]

    document.summary = summary
    document.summary_generated_at = datetime.now(UTC)
    db.flush()

    audit_service.record(
        db,
        action="document_summarized",
        entity_type="patient_document",
        entity_id=document.id,
        actor_id=actor_id,
        actor_label=actor_label,
        metadata={"had_extractable_text": bool(text)},
        commit=False,
    )
    db.commit()
    db.refresh(document)
    return document
