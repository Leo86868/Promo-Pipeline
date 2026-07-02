"""Gemini text-generation adapter — provider switch for Gemini #1.

The underlying SDK import remains quarantined in ``promo.core.llm``. This
module is the provider-facing surface used by pipeline stages
(``script_generator`` / ``script_gemini_caller``) and is the single switch
point between providers: ``config.script_llm_provider()`` decides whether
``resolve_gemini_model`` hands back a Google GenAI ``GeminiModel`` (default)
or an :class:`~promo.core.llm.openrouter_text_client.OpenRouterTextModel`
(the ``OPENROUTER_API_KEY`` failover lane). ``generate_content_text`` then
dispatches on the returned handle's type. Script code stays provider-agnostic
and, with the env unset, the Gemini SDK path is byte-identical to before.
"""

from __future__ import annotations

from typing import Any

from promo.core.llm.gemini_client import GeminiModel
from promo.core.llm.gemini_client import resolve_gemini_model as _resolve_gemini_sdk_model
from promo.core.llm.openrouter_text_client import (
    OpenRouterTextModel,
    generate_text as _generate_openrouter_text,
)

__all__ = [
    "GeminiModel",
    "generate_content_text",
    "resolve_gemini_model",
]


def resolve_gemini_model(*, log_context: str = "Gemini"):
    """Resolve the script-generation model handle for the active provider.

    Default (``PROMO_SCRIPT_LLM_PROVIDER`` unset or ``gemini``) returns a
    Google GenAI ``GeminiModel`` exactly as before. ``openrouter`` returns
    an ``OpenRouterTextModel`` handle instead — fail-loud on a missing
    ``OPENROUTER_API_KEY``. The return type is opaque to callers.
    """
    from promo.core.config import script_llm_provider

    if script_llm_provider() == "openrouter":
        from promo.core.llm.openrouter_text_client import resolve_openrouter_text_model

        return resolve_openrouter_text_model(log_context=log_context)
    return _resolve_gemini_sdk_model(log_context=log_context)


def generate_content_text(
    model: Any,
    prompt: str,
    *,
    generation_config: dict[str, Any],
) -> str:
    """Return generated text from whichever provider ``model`` belongs to.

    ``OpenRouterTextModel`` → OpenRouter chat/completions; anything else is
    the Google GenAI ``GeminiModel`` whose ``generate_content(...).text`` is
    called exactly as before.
    """
    if isinstance(model, OpenRouterTextModel):
        return _generate_openrouter_text(model, prompt, generation_config=generation_config)
    response = model.generate_content(
        prompt,
        generation_config=generation_config,
    )
    return response.text
