"""Gemini text-generation adapter.

The underlying SDK import remains quarantined in ``promo.core.llm``. This
module is the provider-facing surface used by pipeline stages.
"""

from __future__ import annotations

from typing import Any

from promo.core.llm.gemini_client import GeminiModel, resolve_gemini_model

__all__ = [
    "GeminiModel",
    "generate_content_text",
    "resolve_gemini_model",
]


def generate_content_text(
    model: GeminiModel,
    prompt: str,
    *,
    generation_config: dict[str, Any],
) -> str:
    """Call Gemini ``generate_content`` and return the response text."""
    response = model.generate_content(
        prompt,
        generation_config=generation_config,
    )
    return response.text
