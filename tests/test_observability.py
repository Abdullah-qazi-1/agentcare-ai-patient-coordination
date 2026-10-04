"""LangSmith tracing must actually export its env vars, not just live on the Settings
object — see the module docstring in app/core/observability.py for why: pydantic-
settings parses `.env` into our Settings instance without pushing it back into
`os.environ`, and the LangSmith SDK reads directly from `os.environ`. Before this,
adding LANGSMITH_* to `.env` alone did nothing.
"""

import os

from app.core.config import Settings
from app.core.observability import _configure_langsmith

_LANGSMITH_VARS = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT")


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _clear_langsmith_env(monkeypatch):
    for var in _LANGSMITH_VARS:
        monkeypatch.delenv(var, raising=False)


class TestLangsmithTracingDisabled:
    def test_disabled_by_default_exports_nothing(self, monkeypatch):
        _clear_langsmith_env(monkeypatch)
        settings = _settings()
        assert settings.langsmith_tracing is False

        result = _configure_langsmith(settings)

        assert result is False
        for var in _LANGSMITH_VARS:
            assert var not in os.environ


class TestLangsmithTracingEnabled:
    def test_enabling_exports_all_four_variables(self, monkeypatch):
        _clear_langsmith_env(monkeypatch)
        settings = _settings(
            langsmith_tracing=True,
            langsmith_api_key="lsv2_test_key",
            langsmith_project="agentcare2026",
        )

        result = _configure_langsmith(settings)

        assert result is True
        assert os.environ["LANGSMITH_TRACING"] == "true"
        assert os.environ["LANGSMITH_API_KEY"] == "lsv2_test_key"
        assert os.environ["LANGSMITH_PROJECT"] == "agentcare2026"
        assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"

    def test_custom_endpoint_is_respected(self, monkeypatch):
        _clear_langsmith_env(monkeypatch)
        settings = _settings(langsmith_tracing=True, langsmith_endpoint="https://eu.smith.langchain.com")

        _configure_langsmith(settings)

        assert os.environ["LANGSMITH_ENDPOINT"] == "https://eu.smith.langchain.com"

    def test_an_existing_env_var_is_not_overwritten(self, monkeypatch):
        """`setdefault` semantics: an operator's real shell env wins over `.env`."""
        _clear_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_PROJECT", "already-set-in-shell")
        settings = _settings(langsmith_tracing=True, langsmith_project="agentcare2026")

        _configure_langsmith(settings)

        assert os.environ["LANGSMITH_PROJECT"] == "already-set-in-shell"

    def test_no_api_key_still_enables_tracing(self, monkeypatch):
        """Tracing can point at a self-hosted or key-less endpoint; only `LANGSMITH_
        TRACING` itself gates whether tracing is attempted at all."""
        _clear_langsmith_env(monkeypatch)
        settings = _settings(langsmith_tracing=True, langsmith_api_key="")

        result = _configure_langsmith(settings)

        assert result is True
        assert "LANGSMITH_API_KEY" not in os.environ
