"""Document download — the one web-only route with no JSON API equivalent."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import document_service
from app.web.deps import WebSession, get_current_user_from_cookie

router = APIRouter(tags=["web-documents"])


@router.get("/documents/{document_id}/download")
def download_document(
    document_id: int,
    current: WebSession = Depends(get_current_user_from_cookie),
    db: Session = Depends(get_db),
):
    document = document_service.get_document(db, document_id)
    if not current.is_staff and document.patient_id != current.patient_id:
        raise HTTPException(status_code=404, detail="Not found.")

    path = Path(document.storage_reference)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk.")
    return FileResponse(path, filename=document.original_filename)
