from functools import lru_cache
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The shipped default — fine for local dev, where anyone reading this file already has
# equivalent access to whatever it would protect. A production deployment that leaves
# this unchanged would sign JWTs with a secret published in the repo's own history.
_INSECURE_DEFAULT_JWT_SECRET = "change-me-to-a-long-random-string"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, values: Any) -> Any:
        """Treat an empty env var as absent so the field default applies.

        `.env.example` lists the optional per-agent overrides as bare `KEY=` lines to
        document that they exist. Pydantic would otherwise hand `""` to an `int` field
        and refuse to construct Settings at all — turning a documentation convenience
        into a hard startup failure on a fresh clone.
        """
        if not isinstance(values, dict):
            return values
        return {key: value for key, value in values.items() if value != ""}

    @model_validator(mode="after")
    def _reject_insecure_jwt_secret_in_production(self) -> "Settings":
        if self.env == "production" and self.jwt_secret == _INSECURE_DEFAULT_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is still the published default — set a real secret in .env "
                "before running with ENV=production."
            )
        return self

    # LLM — any OpenAI-compatible provider (e.g. Groq, OpenAI, a self-hosted gateway)
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_model__routing: str = ""
    llm_model__document: str = ""
    llm_model__safety: str = ""

    # --- Token limits -------------------------------------------------------
    # Cap on *output* tokens per model call. Every agent returns a small structured
    # object, so a low ceiling is safe and stops a runaway generation from becoming an
    # expensive one.
    llm_max_tokens: int = 1024
    llm_max_tokens__routing: int = 0  # 0 = inherit the default above
    llm_max_tokens__document: int = 0
    llm_max_tokens__safety: int = 0

    # Cap on total tokens (input + output) one workflow run may consume across all
    # agents. Exceeding it escalates to a human instead of continuing to spend.
    workflow_token_budget: int = 120_000
    # Model calls a single agent may make before being stopped.
    agent_run_call_limit: int = 8
    # Model calls one workflow thread may make in total.
    workflow_thread_call_limit: int = 40
    # Conversation tokens after which an agent summarizes rather than resending history.
    summarization_trigger_tokens: int = 6000

    # --- Observability ------------------------------------------------------
    langsmith_api_key: str = ""
    langsmith_project: str = "agentcare-dev"
    langsmith_tracing: bool = False
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    logfire_token: str = ""
    logfire_project_name: str = "agentcare"
    # Sends traces to Logfire's cloud. Off by default so a fresh clone, and the
    # hackathon CI (which runs with no credentials), never blocks on a network call.
    logfire_enabled: bool = False
    # Emit spans to the console instead of the cloud — useful locally with no token.
    logfire_console: bool = False
    # Route LangChain/LangGraph spans to Logfire via LangSmith's OpenTelemetry bridge.
    logfire_instrument_langchain: bool = True

    # --- Guardrails ----------------------------------------------------------
    # NVIDIA NeMo Guardrails as an additional *semantic* input/output rail on top of
    # the deterministic safety scanner (app/services/safety_service.py) and the Safety
    # agent. Off by default — like LangSmith/Logfire above, a fresh clone or CI run with
    # no LLM key must never be blocked by this. See app/guardrails/service.py.
    nemoguardrails_enabled: bool = False

    # Database
    database_url: str = "sqlite:///./agentcare.db"

    # --- Documents -------------------------------------------------------------
    # Cap on one uploaded report/document. Enforced in app/services/document_service.py
    # and mirrored client-side (VITE_MAX_DOCUMENT_SIZE_MB) so an oversized file is
    # rejected instantly instead of after a full upload round trip.
    max_document_size_mb: int = 10

    # Auth
    jwt_secret: str = _INSECURE_DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 120

    # App
    env: str = "development"
    api_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    # Comma-separated, not a JSON list — keeps a one-line .env entry instead of needing
    # quoted/escaped JSON. The server-rendered UI is same-origin and unaffected by this;
    # it only matters for a separate client calling the JSON API cross-origin.
    cors_origins: str = "http://localhost:8000"

    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def model_for(self, agent_name: str) -> str:
        """Per-agent model override, falling back to the default model.

        Lets cost-sensitive deployments route high-volume classification agents
        (routing, document, safety) to a cheaper model via env vars, without
        touching any agent code.
        """
        override = getattr(self, f"llm_model__{agent_name.lower()}", "")
        return override or self.llm_model

    def max_tokens_for(self, agent_name: str) -> int:
        """Per-agent output-token ceiling, falling back to the global default."""
        override = getattr(self, f"llm_max_tokens__{agent_name.lower()}", 0)
        return override or self.llm_max_tokens


@lru_cache
def get_settings() -> Settings:
    return Settings()
