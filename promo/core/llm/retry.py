"""Exponential-backoff retry helper for LLM calls."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


# 4xx codes that ARE worth retrying: rate limits and request timeouts can
# clear on their own; every other 4xx is deterministic (bad key, geo-block,
# invalid argument) and will fail identically on every attempt.
_TRANSIENT_HTTP_CODES = {408, 429}

# Fallback text patterns for clients that flatten HTTP failures into plain
# RuntimeError strings. Each pattern is anchored to a known error shape so
# an incidental 3-digit number in a payload cannot misclassify:
#   - google-genai:  "400 User location is not supported for the API use."
#   - our adapters:  "ElevenLabs API error 401: ..."
#   - requests:      "404 Client Error: Not Found for url: ..."
_CLIENT_ERROR_TEXT_RES = (
    re.compile(r"^\s*(4\d\d)\b"),
    re.compile(r"\bapi error (4\d\d)\b", re.IGNORECASE),
    re.compile(r"\b(4\d\d) client error\b", re.IGNORECASE),
)


def is_non_retryable_client_error(exc: Exception) -> bool:
    """True when ``exc`` is a deterministic HTTP 4xx (excluding 408/429).

    2026-06-09 fail-fast fix: a production batch ground every video through
    the full retry budget on ``400 User location is not supported`` —
    retrying a deterministic client error only burns paid calls and
    minutes. Status is read from common exception/response attributes
    first; the text patterns above are the fallback for clients that
    flatten status into the message.
    """
    candidates: list[Any] = [
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
        getattr(getattr(exc, "resp", None), "status", None),
    ]
    for value in candidates:
        try:
            code = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if 100 <= code < 600:
            return 400 <= code < 500 and code not in _TRANSIENT_HTTP_CODES
    text = str(exc)
    for pattern in _CLIENT_ERROR_TEXT_RES:
        match = pattern.search(text)
        if match and int(match.group(1)) not in _TRANSIENT_HTTP_CODES:
            return True
    return False


def retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple = (Exception,),
    *,
    give_up: Callable[[Exception], bool] | None = None,
) -> Any:
    """Retry a function with exponential backoff.

    Doubles the delay on each retry (``base_delay``, ``base_delay * 2``,
    ``base_delay * 4``, ...) capped at ``max_delay``.  Only exceptions
    listed in *exceptions* trigger a retry; all others propagate immediately.

    ``give_up`` decides fail-fast: when it returns True for a caught
    exception, the error propagates without further attempts. Defaults to
    :func:`is_non_retryable_client_error` so deterministic HTTP 4xx
    failures (bad key, geo-block, invalid argument) stop burning paid
    calls; pass ``give_up=lambda exc: False`` to restore blind retrying.

    Returns the function's return value on success.
    Raises the last caught exception after *max_retries* failures.
    """
    last_exception: Optional[Exception] = None
    delay = base_delay
    should_give_up = give_up if give_up is not None else is_non_retryable_client_error

    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except exceptions as exc:
            last_exception = exc
            if should_give_up(exc):
                logger.error(
                    "Non-retryable client error — failing fast on attempt %d/%d: %s",
                    attempt,
                    max_retries,
                    exc,
                )
                raise
            if attempt == max_retries:
                logger.error(
                    "All %d retries exhausted. Last error: %s", max_retries, exc
                )
                raise
            logger.warning(
                "Attempt %d/%d failed: %s — retrying in %.1fs",
                attempt,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, max_delay)

    raise last_exception  # type: ignore[misc]
