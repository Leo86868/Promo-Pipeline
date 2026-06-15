"""Gemini SDK quarantine module.

This module is the ONLY allowed ``import google.generativeai`` site in the
repo. Every other production file talks to Gemini through the helpers
exported here (``configure_gemini`` / ``reset_for_tests`` /
``resolve_gemini_model``) plus the ``GeminiModel`` type alias — see
``architecture.md`` Pluggability Charter Rule 1.

``GEMINI_API_KEY`` resolves via the typed resolver
``promo.core.config.gemini_api_key`` (raises ``ConfigError`` on missing).
``GEMINI_MODEL`` is a direct ``os.getenv`` defaulting to
``registry.GEMINI_TEXT_MODEL`` — the LLM quarantine module is carved out of
Rule 2.
"""

from __future__ import annotations

import logging
import os
import threading

import google.generativeai as genai

from promo.core.model_adapters.registry import GEMINI_TEXT_MODEL


GeminiModel = genai.GenerativeModel

__all__ = [
    "GeminiModel",
    "configure_gemini",
    "reset_for_tests",
    "resolve_gemini_model",
]

_lock = threading.Lock()
_configured = False
_logger = logging.getLogger(__name__)


def configure_gemini(api_key: str) -> None:
    """Run ``genai.configure(api_key=...)`` exactly once per process.

    Callers MUST supply a non-empty api_key. Passing ``None`` or an empty
    string raises ``ValueError`` — preventing the silent-no-op path that
    would leave subsequent ``genai.*`` calls configured against an empty
    key. The typed resolvers in ``promo.core.config`` are the single
    source of truth for "GEMINI_API_KEY is missing"; this helper trusts
    their output.
    """
    if not api_key:
        raise ValueError(
            "configure_gemini requires a non-empty api_key. Resolve one "
            "via promo.core.config.gemini_api_key() (raises ConfigError "
            "on missing) before calling this helper."
        )
    global _configured
    with _lock:
        if _configured:
            return
        genai.configure(api_key=api_key)
        _configured = True


def reset_for_tests() -> None:
    """Clear the module-global ``_configured`` flag so tests can re-run
    configuration under a fresh api_key.

    Production code MUST NOT call this.
    """
    global _configured
    with _lock:
        _configured = False


def resolve_gemini_model(*, log_context: str = "Gemini") -> GeminiModel:
    """Return a ``genai.GenerativeModel`` configured against the project key.

    Reads ``GEMINI_API_KEY`` through the typed resolver
    ``promo.core.config.gemini_api_key`` (raises ``ConfigError`` on missing
    values) and ``GEMINI_MODEL`` as a plain ``os.getenv`` defaulting to
    ``registry.GEMINI_TEXT_MODEL``.

    ``log_context`` tags the info-log so the two Gemini-call sites remain
    identifiable in captured logs — e.g. tagging the "Gemini #1" script call.
    """
    from promo.core.config import gemini_api_key

    api_key = gemini_api_key()
    configure_gemini(api_key)
    gemini_model_name = os.getenv("GEMINI_MODEL", GEMINI_TEXT_MODEL)
    _logger.info("%s model resolved: %s", log_context, gemini_model_name)
    return genai.GenerativeModel(gemini_model_name)
