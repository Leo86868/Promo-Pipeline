"""OpenRouter text-generation quarantine module — Gemini #1 failover lane.

Sibling of ``promo/core/llm/gemini_client.py`` (the Google GenAI SDK
quarantine). This module owns the OpenRouter chat/completions path for
Gemini #1 script generation: it resolves the model handle, maps the
GenAI-style ``generation_config`` to OpenAI-style params, issues the
call via the ``model_adapters.openrouter`` HTTP adapter (which owns
``requests`` — mirroring how ``clip_embedder`` reaches OpenRouter via
``post_embeddings``), and extracts the completion text.

Selected via ``PROMO_SCRIPT_LLM_PROVIDER=openrouter``
(``config.script_llm_provider``); the switch itself lives one layer up in
``model_adapters/gemini.py`` so script code stays provider-agnostic. The
returned string is drop-in equivalent to the Google SDK's
``response.text`` — script code does its own JSON parsing unchanged.

Retries are NOT taken here: the single call is wrapped by
``retry_with_backoff`` in the caller (``script_gemini_caller.generate_one``),
whose default ``give_up`` (``retry.is_non_retryable_client_error``) already
classifies the ``requests.HTTPError`` this module raises — 429/5xx/timeouts
retry, 400/401/402/403 fail fast *within one ``generate_one`` call*; the
outer per-variant budget loop in ``script_generator`` still retries on the
resulting ``None`` returns (same behaviour as the Gemini SDK path).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from promo.core.model_adapters.openrouter import post_chat_completion
from promo.core.model_adapters.registry import OPENROUTER_SCRIPT_MODEL_API_ID

__all__ = [
    "OpenRouterTextModel",
    "map_generation_config",
    "generate_text",
    "resolve_openrouter_text_model",
]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenRouterTextModel:
    """Opaque handle returned by :func:`resolve_openrouter_text_model`.

    Carries only the model id sent to OpenRouter. The adapter layer
    (``model_adapters.gemini.generate_content_text``) dispatches on this
    type; script code treats it as the same opaque ``model`` token the
    Google SDK path returns.
    """

    model: str


def resolve_openrouter_text_model(*, log_context: str = "OpenRouter") -> OpenRouterTextModel:
    """Return the OpenRouter text-model handle for script generation.

    Fails loud (``ConfigError``) if ``OPENROUTER_API_KEY`` is missing — the
    provider was explicitly selected, so a missing key is an operator error,
    not a fallback. The model id defaults to
    ``registry.OPENROUTER_SCRIPT_MODEL_API_ID`` and may be overridden via
    ``OPENROUTER_SCRIPT_MODEL`` (mirrors ``GEMINI_MODEL`` on the SDK path).
    """
    from promo.core.config import openrouter_api_key

    # Resolve eagerly so a missing key fails at model-resolve time (same
    # moment the SDK path calls gemini_api_key()), not mid-generation.
    openrouter_api_key()
    model_name = os.getenv("OPENROUTER_SCRIPT_MODEL", OPENROUTER_SCRIPT_MODEL_API_ID)
    _logger.info("%s model resolved: %s", log_context, model_name)
    return OpenRouterTextModel(model=model_name)


def map_generation_config(generation_config: dict[str, Any]) -> dict[str, Any]:
    """Translate a Google GenAI ``generation_config`` to OpenAI-style params.

    Faithful key mapping for the params Gemini #1 actually passes plus the
    JSON-response knob:
      - ``temperature``       → ``temperature``
      - ``top_p``             → ``top_p``
      - ``max_output_tokens`` → ``max_tokens``
      - ``response_mime_type == "application/json"``
                              → ``response_format={"type": "json_object"}``

    Unknown keys raise ``ValueError`` rather than being silently dropped —
    a new GenAI knob must be mapped deliberately, not lost on the failover
    path (fail-loud over silent divergence).
    """
    mapped: dict[str, Any] = {}
    for key, value in generation_config.items():
        if key == "temperature":
            mapped["temperature"] = value
        elif key == "top_p":
            mapped["top_p"] = value
        elif key == "max_output_tokens":
            mapped["max_tokens"] = value
        elif key == "response_mime_type":
            if value == "application/json":
                mapped["response_format"] = {"type": "json_object"}
            else:
                raise ValueError(
                    f"Unsupported response_mime_type {value!r} for OpenRouter "
                    "script provider (only 'application/json' is mapped)."
                )
        else:
            raise ValueError(
                f"Unmapped generation_config key {key!r} for OpenRouter script "
                "provider — add an explicit mapping in map_generation_config."
            )
    return mapped


def generate_text(
    model: OpenRouterTextModel,
    prompt: str,
    *,
    generation_config: dict[str, Any],
) -> str:
    """Call OpenRouter chat/completions and return the completion text.

    Drop-in equivalent of the Google SDK's ``response.text``. Raises loudly
    (``RuntimeError``) if the response carries no message content, so an
    empty completion surfaces as a retryable failure rather than a silent
    empty string flowing into the script JSON parser.
    """
    response = post_chat_completion(
        model=model.model,
        messages=[{"role": "user", "content": prompt}],
        extra_body=map_generation_config(generation_config),
    )
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError(
            f"OpenRouter chat completion returned no choices: {response!r}"
        )
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError(
            "OpenRouter chat completion returned empty message content"
        )
    return content
