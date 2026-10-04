from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models.enums import EscalationReason, EscalationStatus

if TYPE_CHECKING:
    from app.db.models.workflow import WorkflowRun


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id"))
    reason: Mapped[EscalationReason] = mapped_column(
        Enum(EscalationReason, native_enum=False, values_callable=lambda e: [m.value for m in e]),
    )
    detail: Mapped[str] = mapped_column(String(1000), default="")
    # `escalation_service.get_pending_for_workflow` and the Escalations dashboard both
    # filter on this.
    status: Mapped[EscalationStatus] = mapped_column(
        Enum(EscalationStatus, native_enum=False, values_callable=lambda e: [m.value for m in e]),
        default=EscalationStatus.PENDING,
        index=True,
    )
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution_note: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow_run: Mapped["WorkflowRun"] = relationship()
