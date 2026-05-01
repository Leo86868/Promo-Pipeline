"""JSON-from-LLM-text helper."""

from __future__ import annotations

import json
import re


def parse_json_response(text: str) -> dict:
    """Parse JSON from an AI model response.

    Handles common markdown fencing (``\\`\\`\\`json ... \\`\\`\\`` or
    ``\\`\\`\\` ... \\`\\`\\``) and strips surrounding whitespace before
    parsing.

    Raises ``ValueError`` with a descriptive message on failure.
    """
    cleaned = text.strip()

    fence_pattern = re.compile(
        r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL
    )
    match = fence_pattern.match(cleaned)
    if match:
        cleaned = match.group(1).strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Failed to parse JSON from AI response: {exc}. "
            f"Response text (first 200 chars): {text[:200]!r}"
        ) from exc

    if not isinstance(result, dict):
        raise ValueError(
            f"Expected a JSON object (dict), got {type(result).__name__}"
        )
    return result
