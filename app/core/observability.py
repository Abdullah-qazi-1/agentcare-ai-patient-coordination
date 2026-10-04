"""Tracing: native LangSmith, plus optional Logfire on top.

**Import-order matters here.** LangSmith (and Logfire's LangChain bridge, which rides
on the same LangSmith OTEL variables) reads its configuration from `os.environ` at
*import time*. If `langchain` is imported before `configure_observability()` runs,
tracing never activates and agent spans silently go missing — a failure that looks
like "tracing just doesn't work" with no error to explain it.

Populating `Settings.langsmith_*` from `.env` is not enough on its own: pydantic-
settings parses `.env` into this process's `Settings` object without exporting it back
to `os.environ`, and the LangSmith SDK — like Logfire's bridge — reads directly from
`os.environ`, not from our config object. So `configure_observability()` exports the
LangSmith variables itself, before anything else touches `langchain`.

So `configure_observability()` must be called before anything imports the agent layer.
Every entry point (`app/main.py`, the seed script) calls it first, and
`_assert_import_order()` warns loudly if something got there first.

Everything here degrades gracefully. With no credentials configured — which is exactly
how the hackathon's automated checks run — the application starts and behaves normally,
just untraced.
"""

from __future__ import annotations

import os
import sys

from app.core.config import get_settings

# Two flags, deliberately separate: `_attempted` is a plain idempotency guard (has
# configure_observability() run its body at all), while `_logfire_active` specifically
# gates the Logfire-only helpers below (instrument_fastapi/instrument_sqlalchemy/span),
# since calling those before `logfire.configure()` has actually succeeded — which can
# now happen even when only LangSmith tracing is enabled — produces a
# LogfireNotConfiguredWarning for no benefit.
_attempted = False
_logfire_active = False
_langsmith_active = False


def _assert_import_order() -> None:
    """Warn if langchain was imported before we could set tracing's env vars."""
    if "langchain" in sys.modules or "langgraph" in sys.modules:
        print(
            "[observability] WARNING: langchain/langgraph were imported before "
            "configure_observability() ran. Agent traces will be missing or incomplete. "
            "Call configure_observability() first in your entry point.",
            file=sys.stderr,
        )


def _configure_langsmith(settings) -> bool:
    """Export native LangSmith tracing env vars. Independent of Logfire being enabled.

    Returns True if tracing was actually turned on. `os.environ.setdefault` is used
    throughout so an operator's own shell-level env vars still win over `.env`.
    """
    if not settings.langsmith_tracing:
        return False
    _assert_import_order()
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
    if settings.langsmith_api_key:
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
    if settings.langsmith_project:
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
    print(f"[observability] LangSmith tracing active (project={settings.langsmith_project})", file=sys.stderr)
    return True


def configure_observability(*, service_name: str | None = None) -> bool:
    """Configure tracing (native LangSmith, plus optional Logfire) and instrument the
    stack. Returns True if any tracing backend is active.

    `service_name` defaults to `settings.logfire_project_name` rather than a hardcoded
    literal — that field existed but was never actually read anywhere, so `.env` had no
    way to affect what showed up as the service name in Logfire.

    Safe to call more than once; subsequent calls are no-ops.
    """
    global _attempted, _logfire_active, _langsmith_active
    if _attempted:
        return _logfire_active or _langsmith_active
    _attempted = True

    settings = get_settings()
    service_name = service_name or settings.logfire_project_name
    _langsmith_active = _configure_langsmith(settings)

    # Nothing more to do unless Logfire is explicitly enabled. Console mode still
    # counts as enabled, so a developer can get Logfire traces with no account at all.
    if not settings.logfire_enabled and not settings.logfire_console:
        return _langsmith_active

    try:
        import logfire
    except ImportError:
        print("[observability] logfire is not installed; continuing untraced.", file=sys.stderr)
        return _langsmith_active

    # --- LangChain/LangGraph bridge: must be set before langchain is imported ---
    if settings.logfire_instrument_langchain:
        _assert_import_order()
        os.environ.setdefault("LANGSMITH_OTEL_ENABLED", "true")
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        if not settings.langsmith_api_key:
            # Without a LangSmith key there is nowhere to double-write to, so keep
            # spans on the OTEL path only and avoid failing uploads to LangSmith.
            os.environ.setdefault("LANGSMITH_OTEL_ONLY", "true")

    if settings.logfire_token:
        os.environ.setdefault("LOGFIRE_TOKEN", settings.logfire_token)

    try:
        logfire.configure(
            service_name=service_name,
            environment=settings.env,
            send_to_logfire="if-token-present" if settings.logfire_enabled else False,
            console=logfire.ConsoleOptions(min_log_level="info") if settings.logfire_console else False,
            # Patient data must never leave the building in a trace payload. Logfire's
            # scrubber is a second line of defence behind PIIMiddleware, which already
            # redacts on the way out of an agent.
            scrubbing=logfire.ScrubbingOptions(
                extra_patterns=["phone", "emergency_contact", "date_of_birth", "checksum"]
            ),
        )
    except Exception as exc:  # pragma: no cover - never let telemetry break startup
        print(f"[observability] logfire.configure failed ({exc}); continuing untraced.", file=sys.stderr)
        return _langsmith_active

    # Instrument the HTTP client LLM calls travel over, so every LLM request shows
    # up with timing even though the provider is not a first-party Logfire integration.
    # `instrument_system_metrics` needs the optional `logfire[system-metrics]` extra
    # (opentelemetry-instrumentation-system-metrics + psutil) — `getattr` degrades to a
    # no-op rather than an ImportError when it isn't installed.
    for name, instrument in (
        ("httpx", getattr(logfire, "instrument_httpx", None)),
        ("system_metrics", getattr(logfire, "instrument_system_metrics", None)),
    ):
        if instrument is None:
            continue
        try:
            instrument()
        except Exception:  # pragma: no cover
            print(f"[observability] could not instrument {name}", file=sys.stderr)

    _logfire_active = True
    print(
        f"[observability] Logfire active (service={service_name}, "
        f"cloud={'on' if settings.logfire_enabled else 'off'}, "
        f"console={'on' if settings.logfire_console else 'off'})",
        file=sys.stderr,
    )
    return True


def instrument_fastapi(app) -> None:
    """Attach request tracing to the FastAPI app, if Logfire is active."""
    if not _logfire_active:
        return
    try:
        import logfire

        logfire.instrument_fastapi(app, capture_headers=False)
    except Exception:  # pragma: no cover
        print("[observability] could not instrument FastAPI", file=sys.stderr)


def instrument_sqlalchemy(engine) -> None:
    """Attach query tracing to the SQLAlchemy engine, if Logfire is active."""
    if not _logfire_active:
        return
    try:
        import logfire

        logfire.instrument_sqlalchemy(engine=engine)
    except Exception:  # pragma: no cover
        print("[observability] could not instrument SQLAlchemy", file=sys.stderr)


def span(name: str, **attributes):
    """Open a Logfire span, or a no-op context manager when tracing is off."""
    if _logfire_active:
        try:
            import logfire

            return logfire.span(name, **attributes)
        except Exception:  # pragma: no cover
            pass

    from contextlib import nullcontext

    return nullcontext()


def is_active() -> bool:
    return _logfire_active
