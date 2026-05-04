"""Gemini #1 single-call wrapper — API + JSON parse + retry.

Extracted from ``script_generator.py`` (Sprint S2c, commit 2/4).
The single entry point :func:`generate_one` wraps one call to Gemini
#1 with the standard retry/backoff and JSON parsing; the entry-point
orchestrators in ``script_generator`` invoke it inside their per-attempt
loops.

Sampling parameters (``temperature=0.85``, ``top_p=0.9``,
``max_output_tokens=10000``) live here as call-site defaults rather
than module constants because no other call site needs to override
them — Sprint 08.5 bumped tokens 1500 → 10000 per operator directive
(token cost is not a constraint; memory: feedback_gemini_token_budget).
"""

from __future__ import annotations

import logging
from typing import Optional

from promo.core.llm.gemini_client import GeminiModel
from promo.core.llm.retry import retry_with_backoff
from promo.core.llm.json_response import parse_json_response

logger = logging.getLogger(__name__)


def generate_one(prompt: str, model: GeminiModel) -> Optional[dict]:
    """Generate a single script candidate. Returns parsed dict or None."""
    def _call():
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.85,
                "top_p": 0.9,
                # Sprint 08.5: bumped 1500→10000 per operator directive. Token
                # cost is not a constraint on this repo (memory:
                # feedback_gemini_token_budget). Headroom covers 130-140 word
                # scripts + the fuller clips[] arrays without truncation risk.
                "max_output_tokens": 10000,
            },
        )
        return parse_json_response(response.text)

    try:
        return retry_with_backoff(_call, max_retries=2, base_delay=2.0)
    except Exception as exc:
        logger.warning("Script generation failed: %s", exc)
        return None
