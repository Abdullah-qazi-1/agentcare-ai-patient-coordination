"""Audit trail.

Every state-changing action in AgentCare — whether performed by a human via the API
or by an agent via a tool — routes through `record()`. The agent side is additionally
guaranteed by AuditMiddleware, which wraps every tool call.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import AuditEvent
from app.schemas.workflow import LineageEntry

logger = get_logger(__name__)

# Tool -> human-readable stage label, for the data-lineage view. Deliberately an
# allowlist rather than "show every tool call": lineage answers "what data did the
# pipeline actually use" (doctor availability, documents, department matching), not
# "what happened" in general — that broader question is what the raw audit trail is for.
_LINEAGE_STEP_LABELS: dict[str, str] = {
    "find_available_slots": "Checked doctor availability",
    "book_appointment": "Booked appointment",
    "reschedule_appointment": "Rescheduled appointment",
    "cancel_appointment": "Cancelled appointment",
    "list_my_appointments": "Reviewed existing appointments",
    "lookup_departments": "Looked up hospital departments",
    "suggest_department_by_keywords": "Matched department by keyword",
    "flag_uncertain_routing": "Flagged routing for staff review",
    "list_pending_documents": "Reviewed attached documents",
    "suggest_document_type": "Classified a document",
    "classify_and_store_document": "Filed a document",
    "check_missing_documents": "Checked for missing documents",
    "create_appointment_reminder": "Scheduled a reminder",
    "schedule_followup_task": "Scheduled a follow-up",
}


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    actor_id: int | None = None,
    actor_label: str = "system",
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditEvent:
    """Write one audit event.

    `commit=False` lets a caller enlist the audit write in an surrounding transaction
    so the action and its audit record commit atomically — used by appointment booking.
    """
    event = AuditEvent(
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        event_metadata=_sanitize(metadata or {}),
        created_at=datetime.now(UTC),
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()

    logger.info("audit", action=action, entity_type=entity_type, entity_id=entity_id, actor=actor_label)
    return event


def _sanitize(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep audit metadata JSON-serializable and bounded in size.

    Audit rows are read by staff and can be dumped for review, so oversized blobs
    (a whole document body, a full message history) are truncated rather than stored.
    """
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool, type(None))):
            safe[key] = value[:2000] if isinstance(value, str) else value
        elif isinstance(value, (list, dict)):
            text = str(value)
            safe[key] = value if len(text) <= 2000 else f"{text[:2000]}…[truncated]"
        else:
            safe[key] = str(value)[:2000]
    return safe


def list_events(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    actor_id: int | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    return list(db.execute(stmt).scalars().all())


def list_for_workflow(db: Session, workflow_run_id: int, limit: int = 200) -> list[AuditEvent]:
    """Full action trail for one workflow run — what the Staff Dashboard renders.

    Matches both events anchored on the run itself and events emitted by agents while
    executing it (which carry the run id in their metadata).
    """
    stmt = (
        select(AuditEvent)
        .where(
            (AuditEvent.entity_type == "workflow_run") & (AuditEvent.entity_id == workflow_run_id)
            | (AuditEvent.event_metadata["workflow_run_id"].as_integer() == workflow_run_id)
        )
        .order_by(AuditEvent.created_at.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def list_lineage_for_workflow(db: Session, workflow_run_id: int) -> list[LineageEntry]:
    """Data lineage for one run: which doctor availability was checked, which document
    was matched, which department a keyword table hit — built from the same
    AuditEvent rows as `list_for_workflow`, filtered to the tools in
    `_LINEAGE_STEP_LABELS` and worded for a patient, not an engineer.

    A "confirmation-shaped view assembled from persisted rows" in the same spirit as
    `appointment_service.get_appointment_detail` — this reads what genuinely happened,
    it does not ask an agent to describe itself.
    """
    events = list_for_workflow(db, workflow_run_id)
    entries: list[LineageEntry] = []
    for event in events:
        if not event.action.startswith("tool_call:"):
            continue
        metadata = event.event_metadata or {}
        tool = metadata.get("tool") or event.action.removeprefix("tool_call:")
        step = _LINEAGE_STEP_LABELS.get(tool)
        if step is None:
            continue
        if metadata.get("status") == "failed":
            detail = f"Failed: {metadata.get('error') or 'unknown error'}"
        else:
            detail = metadata.get("result") or f"Called with: {metadata.get('args')}"
        entries.append(
            LineageEntry(
                step=step,
                agent=event.actor_label,
                tool=tool,
                detail=str(detail)[:600],
                created_at=event.created_at,
            )
        )
    return entries
