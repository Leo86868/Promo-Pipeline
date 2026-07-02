"""OpenRouter HTTP adapter."""

from __future__ import annotations

from typing import Any

import requests

from promo.core.config import openrouter_api_key, openrouter_http_referer
from promo.core.model_adapters.registry import (
    OPENROUTER_CHAT_COMPLETIONS_API_URL,
    OPENROUTER_EMBEDDING_API_URL,
    OPENROUTER_EMBEDDING_MODEL_API_ID,
    OPENROUTER_TITLE,
)


def _headers(api_key: str | None = None) -> dict[str, str]:
    resolved_key = api_key or openrouter_api_key()
    return {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": openrouter_http_referer(),
        "X-OpenRouter-Title": OPENROUTER_TITLE,
    }


def post_chat_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    timeout: int = 120,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST an OpenRouter chat completion request and return parsed JSON.

    ``extra_body`` merges OpenAI-style sampling params (``temperature``,
    ``top_p``, ``max_tokens``, ``response_format``, ...) into the request
    payload. Omitting it (the default, and the clip-analyzer call site)
    keeps the request byte-identical to the pre-failover shape.
    """
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if extra_body:
        reserved = {"model", "messages"} & extra_body.keys()
        if reserved:
            raise ValueError(
                f"extra_body must not override reserved payload keys: {sorted(reserved)}"
            )
        payload.update(extra_body)
    response = requests.post(
        OPENROUTER_CHAT_COMPLETIONS_API_URL,
        headers=_headers(),
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()

def post_embeddings(
    inputs: list[str],
    *,
    api_key: str | None = None,
    model: str = OPENROUTER_EMBEDDING_MODEL_API_ID,
    timeout: int = 60,
) -> dict[str, Any]:
    """POST an OpenRouter embeddings request and return parsed JSON."""
    response = requests.post(
        OPENROUTER_EMBEDDING_API_URL,
        headers=_headers(api_key),
        json={"model": model, "input": inputs},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()
