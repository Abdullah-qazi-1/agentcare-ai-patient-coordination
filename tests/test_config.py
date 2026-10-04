"""Regression tests for configuration loading.

Both cases here were live bugs that stopped the application from starting at all, so
they are worth pinning: a blank optional override crashed `Settings()` on import, and
the wrong base-URL default sent every request to the web console.
"""

import pytest
from sqlalchemy import create_engine, inspect

from app.core.config import Settings
from app.db.base import Base
from app.db.models import *  # noqa: F401,F403 — registers every mapper on Base.metadata


def _settings(**overrides) -> Settings:
    """Build Settings without reading the developer's real .env."""
    return Settings(_env_file=None, **overrides)


class TestBlankValuesMeanUnset:
    """`.env.example` documents optional overrides as bare `KEY=` lines.

    Pydantic would otherwise hand `""` to an `int` field and refuse to construct
    Settings at all, turning a documentation convenience into a startup failure on a
    fresh clone.
    """

    def test_blank_int_override_falls_back_to_the_default(self):
        settings = _settings(llm_max_tokens="4096", llm_max_tokens__routing="")
        assert settings.llm_max_tokens__routing == 0
        assert settings.max_tokens_for("routing") == 4096

    def test_every_blank_per_agent_override_is_tolerated(self):
        settings = _settings(
            llm_model__routing="",
            llm_model__document="",
            llm_model__safety="",
            llm_max_tokens__routing="",
            llm_max_tokens__document="",
            llm_max_tokens__safety="",
        )
        for agent in ("routing", "document", "safety"):
            assert settings.model_for(agent) == settings.llm_model

    def test_a_real_override_still_wins(self):
        settings = _settings(
            llm_model="anthropic/claude-opus-4.8",
            llm_model__routing="anthropic/claude-haiku-4.5",
            llm_max_tokens__routing="256",
        )
        assert settings.model_for("routing") == "anthropic/claude-haiku-4.5"
        assert settings.model_for("document") == "anthropic/claude-opus-4.8"
        assert settings.max_tokens_for("routing") == 256


class TestDefaults:
    def test_base_url_points_at_the_api_host(self):
        """Default points at Groq's OpenAI-compatible API host."""
        assert _settings().llm_base_url == "https://api.groq.com/openai/v1"

    def test_unknown_agent_name_falls_back_to_the_default_model(self):
        settings = _settings(llm_model="anthropic/claude-opus-4.8")
        assert settings.model_for("coordinator") == "anthropic/claude-opus-4.8"
        assert settings.max_tokens_for("coordinator") == settings.llm_max_tokens


class TestCorsOrigins:
    """`app/main.py` reads its allowed origins from here rather than a hardcoded list —
    a deployment points the frontend origin(s) at itself via env var alone."""

    def test_default_allows_the_same_origin_app(self):
        origins = _settings().cors_origin_list()
        assert "http://localhost:8000" in origins

    def test_custom_value_is_split_and_trimmed(self):
        settings = _settings(cors_origins="https://app.example.com, https://staff.example.com")
        assert settings.cors_origin_list() == ["https://app.example.com", "https://staff.example.com"]

    def test_blank_entries_are_dropped(self):
        settings = _settings(cors_origins="https://app.example.com,,  ,")
        assert settings.cors_origin_list() == ["https://app.example.com"]


class TestJwtSecretGuard:
    """A deployment that leaves JWT_SECRET at its published default would sign tokens
    with a secret anyone reading this repo already knows — refused outright once
    ENV=production, rather than left as a silent footgun."""

    def test_default_secret_is_rejected_in_production(self):
        with pytest.raises(ValueError, match="JWT_SECRET"):
            _settings(env="production")

    def test_default_secret_is_fine_outside_production(self):
        assert _settings(env="development").jwt_secret == "change-me-to-a-long-random-string"

    def test_a_real_secret_is_accepted_in_production(self):
        settings = _settings(env="production", jwt_secret="a-real-random-secret")
        assert settings.jwt_secret == "a-real-random-secret"


class TestSamplingParameterSupport:
    """Anthropic removed `temperature` on Opus 4.7 and everything after it — sending
    it is a 400. Older and non-Anthropic models still accept it, so this
    must stay a deny-list rather than a blanket removal."""

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-opus-4.7",
            "anthropic/claude-opus-4.8",
            "anthropic/claude-opus-4.8-fast",
            "anthropic/claude-opus-4.8-coding",
            "anthropic/claude-opus-5",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-fable-5",
            "claude-opus-4-8",  # Anthropic's own dashed spelling
            # Confirmed directly against the provider: kimi-k3 400s on any temperature
            # other than its own default — "invalid temperature: only 1 is allowed
            # for this model" — not documented anywhere, found by a live call.
            "moonshotai/kimi-k3",
        ],
    )
    def test_rejecting_models_do_not_receive_temperature(self, model):
        from app.agents.llm import supports_temperature

        assert supports_temperature(model) is False

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic/claude-haiku-4.5",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-opus-4.5",
            "anthropic/claude-sonnet-4.5",
            "anthropic/claude-sonnet-4.6",
            "openai/gpt-4o",
        ],
    )
    def test_accepting_models_still_receive_temperature(self, model):
        from app.agents.llm import supports_temperature

        assert supports_temperature(model) is True


def test_schema_matches_models():
    """`create_all` (used by the test fixtures) must produce the same tables Alembic does.

    Guards the one risk of not running migrations in tests: a model change that lands
    in the fixtures but never reaches a migration would otherwise pass here and fail
    in production.
    """
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())

    expected = {
        "users", "patient_profiles", "departments", "doctors", "appointment_slots",
        "appointments", "patient_documents", "workflow_runs", "escalations",
        "reminders", "notifications", "audit_events",
    }
    assert expected <= tables, f"missing tables: {expected - tables}"
    engine.dispose()
