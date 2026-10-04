"""Document upload, listing, and the missing-documents gap check."""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, map_service_error, require_role
from app.db.models import DocumentType, UserRole
from app.db.session import get_db
from app.schemas.auth import CurrentUser
from app.schemas.document import DocumentOut, MissingDocumentsResult
from app.services import document_service
from app.services.errors import NotFoundError

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    document_type: DocumentType | None = Form(default=None),
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)),
    db: Session = Depends(get_db),
):
    content = await file.read()
    limit = document_service.max_file_bytes()
    if len(content) > limit:
        raise HTTPException(status_code=422, detail=f"File exceeds the {limit // (1024 * 1024)} MB limit.")

    resolved_type, confidence = document_type, 1.0 if document_type else 0.0
    if resolved_type is None:
        resolved_type, confidence = document_service.classify_by_filename(file.filename or "")
        resolved_type = resolved_type or DocumentType.OTHER

    document_date = document_service.extract_date_from_filename(file.filename or "")

    return document_service.store_document(
        db,
        patient_id=current.patient_id,
        filename=file.filename or "upload",
        content=content,
        document_type=resolved_type,
        confidence=confidence,
        document_date=document_date,
        actor_id=current.user_id,
        actor_label="patient",
    )


@router.get("/missing", response_model=MissingDocumentsResult)
def missing_documents(
    department_id: int,
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)),
    db: Session = Depends(get_db),
):
    return document_service.check_missing_documents(
        db, patient_id=current.patient_id, department_id=department_id
    )


@router.get("/me", response_model=list[DocumentOut])
def list_my_documents(
    current: CurrentUser = Depends(require_role(UserRole.PATIENT)), db: Session = Depends(get_db)
):
    return document_service.list_patient_documents(db, current.patient_id)


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    document = document_service.get_document(db, document_id)
    if not current.is_staff and document.patient_id != current.patient_id:
        raise map_service_error(NotFoundError(f"Document {document_id} not found."))
    return document


@router.post("/{document_id}/summarize", response_model=DocumentOut)
def summarize_document(
    document_id: int, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Generate an administrative-only summary — patient-self or staff/admin."""
    from app.agents.llm import LLMNotConfiguredError

    document = document_service.get_document(db, document_id)
    if not current.is_staff and document.patient_id != current.patient_id:
        raise map_service_error(NotFoundError(f"Document {document_id} not found."))

    try:
        return document_service.summarize_document(
            db, document_id=document_id, actor_id=current.user_id, actor_label=current.role.value
        )
    except LLMNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
