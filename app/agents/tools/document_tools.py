"""Document agent tools: classification, storage, duplicate and gap detection.

The agent classifies documents it cannot fully read — this is metadata coordination,
not content extraction. Deliberately so: parsing clinical content would drift toward
interpreting it, which is exactly the line AgentCare must not cross.
"""

from datetime import date

from langchain_core.tools import tool

from app.agents.context import current_context
from app.db.models.enums import DocumentType
from app.services import department_service, document_service
from app.services.errors import ServiceError


@tool
def list_pending_documents() -> str:
    """List documents the patient attached to this request, awaiting classification."""
    context = current_context()
    if not context.pending_document_ids:
        return "No documents were attached to this request."

    lines = []
    for document_id in context.pending_document_ids:
        try:
            document = document_service.get_document(context.db, document_id)
        except ServiceError:
            continue
        lines.append(
            f"- document_id={document.id}: '{document.original_filename}' "
            f"(currently typed '{document.document_type.value}', "
            f"confidence {document.classification_confidence})"
        )
    return "Attached documents:\n" + "\n".join(lines) if lines else "No readable attachments found."


@tool
def suggest_document_type(filename: str) -> str:
    """Check a filename against the hospital's document-naming table.

    Deterministic lookup. If it returns a match, trust it. If not, classify from the
    filename yourself using the allowed types.

    Args:
        filename: The document's original filename.
    """
    doc_type, confidence = document_service.classify_by_filename(filename)
    parsed_date = document_service.extract_date_from_filename(filename)
    allowed = ", ".join(t.value for t in DocumentType)

    if doc_type is None:
        return (
            f"No filename match for '{filename}'. Classify it yourself. "
            f"Allowed types: {allowed}."
            + (f" Date found in filename: {parsed_date.isoformat()}." if parsed_date else "")
        )
    return (
        f"Filename match: '{doc_type.value}' (confidence {confidence})."
        + (f" Date found in filename: {parsed_date.isoformat()}." if parsed_date else " No date in filename.")
    )


@tool
def classify_and_store_document(
    document_id: int, document_type: str, confidence: float, document_date_iso: str = ""
) -> str:
    """Record the final classification for an attached document.

    Args:
        document_id: From list_pending_documents.
        document_type: One of the allowed document types.
        confidence: Your confidence, 0.0 to 1.0.
        document_date_iso: Date on the document as YYYY-MM-DD, or blank if unknown.
    """
    context = current_context()
    try:
        parsed_type = DocumentType(document_type.strip().lower())
    except ValueError:
        allowed = ", ".join(t.value for t in DocumentType)
        return f"Error: '{document_type}' is not valid. Allowed types: {allowed}"

    try:
        document = document_service.get_document(context.db, document_id)
    except ServiceError as exc:
        return f"Error: {exc}"

    if document.patient_id != context.patient_id:
        # Defence in depth: the ID came from the model, so ownership is re-checked here
        # even though list_pending_documents only ever offers this patient's documents.
        return "Error: that document does not belong to the current patient."

    parsed_date: date | None = None
    if document_date_iso.strip():
        try:
            parsed_date = date.fromisoformat(document_date_iso.strip())
        except ValueError:
            return f"Error: '{document_date_iso}' is not a valid YYYY-MM-DD date."

    document.document_type = parsed_type
    document.classification_confidence = max(0.0, min(1.0, confidence))
    if parsed_date:
        document.document_date = parsed_date

    duplicate = document_service.check_duplicate(
        context.db,
        patient_id=context.patient_id,
        checksum=document.checksum,
        document_type=parsed_type,
        document_date=parsed_date,
    )
    # A document is never its own duplicate.
    if duplicate.is_duplicate and duplicate.existing_document_id != document.id:
        document.is_duplicate_of = duplicate.existing_document_id
    context.db.commit()

    message = f"Classified document #{document.id} as '{parsed_type.value}'."
    if document.is_duplicate_of:
        kind = "an identical file" if duplicate.match_kind == "checksum" else "the same report"
        message += (
            f" Note: this appears to duplicate document #{document.is_duplicate_of} ({kind}). "
            "Mention this to the patient — do not silently discard it."
        )
    return message


@tool
def check_missing_documents(department_name: str) -> str:
    """Check which documents a department requires that the patient has not provided.

    Args:
        department_name: Exact department name, e.g. "Cardiology".
    """
    context = current_context()
    department = department_service.get_department_by_name(context.db, department_name)
    if not department:
        return f"Error: no department named '{department_name}'."

    try:
        gap = document_service.check_missing_documents(
            context.db, patient_id=context.patient_id, department_id=department.id
        )
    except ServiceError as exc:
        return f"Error: {exc}"

    if not gap.required:
        return f"{gap.department_name} does not require any documents in advance."
    if not gap.missing:
        return f"All documents required by {gap.department_name} are on file."

    return (
        f"{gap.department_name} requires: {', '.join(t.value for t in gap.required)}.\n"
        f"On file: {', '.join(t.value for t in gap.present) or 'none'}.\n"
        f"Still missing: {', '.join(t.value for t in gap.missing)}. "
        "Ask the patient to upload these."
    )
