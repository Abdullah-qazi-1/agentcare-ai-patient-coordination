"""LLM client factory for an OpenAI-compatible provider.

Every agent is a `ChatOpenAI` pointed at whatever provider is configured via
`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`. Per-agent model selection goes through
`settings.model_for()`, so routing a high-volume classifier to a cheaper model is an
env-var change rather than a code change.

Clients are cached per (agent, model, temperature) because constructing one opens an
HTTP client; agents are built once at import and reused across requests.
"""

import re
from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Anthropic removed the sampling parameters on Opus 4.7 and every model after it —
# sending `temperature` to one of these is a 400, not a warning. Older models
# (Haiku 4.5, Sonnet 4.5, Opus 4.6) and most other models still accept it, so this is a
# deny-list rather than a blanket removal: dropping the parameter everywhere would
# silently un-pin the cheaper classifier models that per-agent tiering is meant to
# route to.
#
# `claude-(opus-4[.-][78]|opus-5|...)` matches both the dotted spelling
# (claude-opus-4.8) and Anthropic's own dashed model IDs (claude-opus-4-8). Kimi is a
# separate case for a different reason: the Kimi K3 endpoint 400s on any `temperature`
# other than its own default (1) — confirmed directly against the provider
# (`invalid temperature: only 1 is allowed for this model`), not documented anywhere.
_NO_SAMPLING_PARAMS = re.compile(
    r"claude-(opus-4[.-][78]|opus-5|sonnet-5|fable-5|mythos-5)|kimi",
    re.IGNORECASE,
)


def supports_temperature(model: str) -> bool:
    """Whether this model still accepts the `temperature` sampling parameter."""
    return not _NO_SAMPLING_PARAMS.search(model)


class LLMNotConfiguredError(RuntimeError):
    """Raised when an agent is invoked without an LLM API key configured.

    Surfaced explicitly rather than letting an auth error escape from deep inside the
    HTTP client, so the API can return an actionable message and the CI checks — which
    run with no keys at all — fail loudly instead of mysteriously.
    """


@lru_cache(maxsize=16)
def _build(
    model: str, temperature: float, max_tokens: int, api_key: str, base_url: str
) -> ChatOpenAI:
    kwargs = {}
    if supports_temperature(model):
        kwargs["temperature"] = temperature

    return ChatOpenAI(
        model=model,
        # Hard ceiling on generated tokens per call. Every agent returns a small
        # structured object, so a runaway generation is always a malfunction — capping
        # it turns an expensive incident into a cheap one.
        max_tokens=max_tokens,
        api_key=api_key,
        base_url=base_url,
        timeout=60,
        max_retries=2,
        **kwargs,
    )


def get_llm(agent_name: str, *, temperature: float = 0.0) -> ChatOpenAI:
    """Return the chat model for a named agent.

    Temperature defaults to 0: this system routes departments, picks appointment slots
    and classifies documents, where reproducibility matters more than variety. A judge
    re-running the same request should see the same decision.

    On models that no longer accept sampling parameters the value is dropped rather
    than sent — see `supports_temperature`. Reproducibility there comes from the
    structured-output contracts and the deterministic fast-paths, not from the request.
    """
    settings = get_settings()
    if not settings.llm_api_key:
        raise LLMNotConfiguredError(
            "LLM_API_KEY is not set. Copy .env.example to .env and add your LLM provider's key."
        )

    model = settings.model_for(agent_name)
    max_tokens = settings.max_tokens_for(agent_name)
    logger.debug("llm_resolved", agent=agent_name, model=model, max_tokens=max_tokens)
    return _build(
        model, temperature, max_tokens, settings.llm_api_key, settings.llm_base_url
    )


def is_llm_configured() -> bool:
    """Whether an LLM is available, without raising — used by health checks and the UI."""
    return bool(get_settings().llm_api_key)
