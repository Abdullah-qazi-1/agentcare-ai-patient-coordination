"""Domain errors raised by the service layer.

These are deliberately transport-agnostic: the API layer maps them to HTTP status
codes, while agent tools catch them and turn them into a message the model can reason
about (and retry or escalate on). Neither layer leaks a raw traceback to a patient.
"""


class ServiceError(Exception):
    """Base for all expected, business-rule failures."""

    status_code = 400


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    """The requested change collides with existing state (e.g. slot already booked)."""

    status_code = 409


class PermissionDeniedError(ServiceError):
    status_code = 403


class ValidationError(ServiceError):
    status_code = 422


class SafetyViolationError(ServiceError):
    """Raised when an action would cross the clinical safety boundary."""

    status_code = 403
