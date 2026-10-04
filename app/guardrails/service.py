"""NVIDIA NeMo Guardrails — an additional semantic input/output rail.

Layer 6 of the clinical safety boundary (see README): a semantic, LLM-based check on
top of the deterministic regex scanner (`app/services/safety_service.py`) and the
Safety agent's judgement. It exists to catch paraphrased jailbreaks and off-topic
requests the deterministic scan can miss — it never replaces any of the existing
layers, which remain the floor regardless of whether this is even installed.

Uses the library's built-in "self check input"/"self check output" rails (LLM
self-check moderation, config in `app/guardrails/config/`) rather than hand-written
Colang dialog flows — there's no conversation to author here, every request already
goes through AgentCare's own fixed LangGraph pipeline. The rails run in isolation
(`GenerationOptions(rails={...})` restricted to just "input" or just "output") against
the project's own `ChatOpenAI` client, so credentials and model tiering stay configured
in exactly one place (`app/core/config.py`), not duplicated here.

Off by default (`NEMOGUARDRAILS_ENABLED=false`) and every call is wrapped so a missing
dependency, a config error, or a library bug degrades to a pass-through rather than
taking down a request.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_CONFIG_PATH = str(Path(__file__).parent / "config")

# `nemoguardrails` pulls in a heavy transitive dependency chain (pandas, onnxruntime,
# fastembed, ...) that noticeably slows down interpreter startup. It is imported lazily
# — only inside `_build_rails()`, on the first real check — so importing this module (and
# therefore every test run, since `NEMOGUARDRAILS_ENABLED=false` by default) never pays
# that cost. `_REFUSAL_MESSAGE` is filled in on first successful import.
_REFUSAL_MESSAGE = ""

# Each restricted to exactly one rail type so a single check never pays for (or
# triggers) the dialog/retrieval machinery it doesn't need — see GenerationRailsOptions.
_INPUT_ONLY = {
    "rails": {
        "input": True, "dialog": False, "output": False,
        "retrieval": False, "tool_input": False, "tool_output": False,
    }
}
_OUTPUT_ONLY = {
    "rails": {
        "input": False, "dialog": False, "output": True,
        "retrieval": False, "tool_input": False, "tool_output": False,
    }
}


@dataclass
class GuardrailResult:
    is_flagged: bool = False
    reason: str = ""


@lru_cache(maxsize=1)
def is_available() -> bool:
    """Whether the `nemoguardrails` package is importable at all.

    Cached: this triggers the actual (heavy) import on first call, exactly once.
    """
    try:
        import nemoguardrails  # noqa: F401
    except ImportError:  # pragma: no cover - exercised only when the dependency is absent
        return False
    return True


def is_configured() -> bool:
    """Whether the guardrails check should actually run for this request.

    Checked before `is_available()` so the disabled-by-default common path never
    triggers the heavy import at all.
    """
    settings = get_settings()
    if not settings.nemoguardrails_enabled or not settings.llm_api_key:
        return False
    return is_available()


@lru_cache(maxsize=1)
def _build_rails() -> Any:
    from nemoguardrails import LLMRails, RailsConfig
    from nemoguardrails.guardrails.iorails import REFUSAL_MESSAGE
    from nemoguardrails.integrations.langchain.llm_adapter import LangChainLLMAdapter

    global _REFUSAL_MESSAGE
    _REFUSAL_MESSAGE = REFUSAL_MESSAGE

    from app.agents.llm import get_llm

    llm = get_llm("guardrails")
    config = RailsConfig.from_path(_CONFIG_PATH)
    return LLMRails(config, llm=LangChainLLMAdapter(llm))


def _last_message_content(response: Any) -> str:
    messages = getattr(response, "response", None) or []
    if not messages:
        return ""
    last = messages[-1]
    if isinstance(last, dict):
        return str(last.get("content", ""))
    return str(getattr(last, "content", ""))


def check_input(text: str) -> GuardrailResult:
    """Semantic check on an inbound patient message. Never raises."""
    if not text or not is_configured():
        return GuardrailResult()
    try:
        response = _build_rails().generate(
            messages=[{"role": "user", "content": text}], options=_INPUT_ONLY
        )
        if _last_message_content(response) == _REFUSAL_MESSAGE:
            return GuardrailResult(
                is_flagged=True, reason="NeMo Guardrails input rail flagged this request."
            )
        return GuardrailResult()
    except Exception:
        # A guardrails bug or transient failure must never take a request down — the
        # deterministic scanner and the Safety agent remain the floor regardless.
        logger.exception("nemoguardrails_input_check_failed")
        return GuardrailResult()


def check_output(user_text: str, bot_text: str) -> GuardrailResult:
    """Semantic check on an agent's own output before it reaches a patient. Never raises."""
    if not bot_text or not is_configured():
        return GuardrailResult()
    try:
        response = _build_rails().generate(
            messages=[
                {"role": "user", "content": user_text or ""},
                {"role": "assistant", "content": bot_text},
            ],
            options=_OUTPUT_ONLY,
        )
        if _last_message_content(response) == _REFUSAL_MESSAGE:
            return GuardrailResult(
                is_flagged=True, reason="NeMo Guardrails output rail flagged this response."
            )
        return GuardrailResult()
    except Exception:
        logger.exception("nemoguardrails_output_check_failed")
        return GuardrailResult()
