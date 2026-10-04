"""Jinja filters/globals shared by every template: label/color formatting, the year."""

from datetime import UTC, datetime

# Reproduces StatusChip.tsx's exact status -> color mapping across the workflow,
# escalation, and appointment domains. Anything not listed renders as "default".
_STATUS_COLORS = {
    "in_progress": "info",
    "rescheduled": "info",
    "awaiting_review": "warning",
    "pending": "warning",
    "completed": "success",
    "approved": "success",
    "confirmed": "success",
    "terminated": "default",
    "cancelled": "default",
    "resolved": "default",
    "failed": "error",
    "rejected": "error",
}


def prettify(value) -> str:
    """snake_case -> Title Case, used for statuses, steps, document/reminder types."""
    if value is None or value == "":
        return ""
    raw = value.value if hasattr(value, "value") else str(value)
    return " ".join(word.capitalize() for word in raw.replace("_", " ").split())


def status_color(status) -> str:
    key = status.value if hasattr(status, "value") else str(status)
    return _STATUS_COLORS.get(key, "default")


def initials(name: str) -> str:
    """Doctor-card avatar initials: strip a 'Dr.' prefix, take the first two words."""
    cleaned = (name or "").replace("Dr.", "").strip()
    parts = cleaned.split()[:2]
    return "".join(p[0].upper() for p in parts if p) or "?"


def register_globals(templates) -> None:
    templates.env.filters["prettify"] = prettify
    templates.env.filters["initials"] = initials
    templates.env.globals["status_color"] = status_color
    templates.env.globals["current_year"] = datetime.now(UTC).year
