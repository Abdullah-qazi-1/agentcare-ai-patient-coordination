"""FastAPI application factory.

`configure_observability()` must run before anything imports the agent layer (see
`app/core/observability.py`), so it — and `configure_logging()` — happen at module
import time, before the router imports below pull in `app.agents.*` transitively.
"""

from app.core.logging import configure_logging
from app.core.observability import configure_observability, instrument_fastapi

configure_observability()
configure_logging()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.api import (  # noqa: E402
    appointments,
    audit,
    auth,
    clinical,
    documents,
    escalations,
    patients,
    workflow,
)
from app.core.config import get_settings  # noqa: E402
from app.services.errors import ServiceError  # noqa: E402
from app.web import auth as web_auth  # noqa: E402
from app.web import documents as web_documents  # noqa: E402
from app.web import patient_routes, staff_routes  # noqa: E402
from app.web.deps import ForbiddenError, NotAuthenticatedError, render  # noqa: E402

app = FastAPI(title="AgentCare", version="1.0.0")

# Origins come from Settings (CORS_ORIGINS in .env), not a hardcoded list. The
# server-rendered UI is same-origin and unaffected; this only matters for a separate
# client calling the JSON API cross-origin, configured via env var alone like every
# other environment-specific value in this app.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ServiceError)
def handle_service_error(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


# The two web-only auth failures below have no JSON equivalent: a browser following a
# link expects a redirect or a styled error page, not a 401/403 body. Handled once here
# rather than in every `app/web/*` route.
@app.exception_handler(NotAuthenticatedError)
def handle_not_authenticated(request: Request, exc: NotAuthenticatedError) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(ForbiddenError)
def handle_forbidden(request: Request, exc: ForbiddenError):
    return render(request, "403.html", status_code=403)


app.include_router(auth.router)
app.include_router(patients.router)
app.include_router(appointments.router)
app.include_router(documents.router)
app.include_router(workflow.router)
app.include_router(escalations.router)
app.include_router(audit.router)
app.include_router(clinical.router)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(web_auth.router)
app.include_router(web_documents.router)
app.include_router(patient_routes.router)
app.include_router(staff_routes.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


instrument_fastapi(app)
