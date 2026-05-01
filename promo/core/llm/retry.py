"""Exponential-backoff retry helper for LLM calls."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


def retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple = (Exception,),
) -> Any:
    """Retry a function with exponential backoff.

    Doubles the delay on each retry (``base_delay``, ``base_delay * 2``,
    ``base_delay * 4``, ...) capped at ``max_delay``.  Only exceptions
    listed in *exceptions* trigger a retry; all others propagate immediately.

    Returns the function's return value on success.
    Raises the last caught exception after *max_retries* failures.
    """
    last_exception: Optional[Exception] = None
    delay = base_delay

    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except exceptions as exc:
            last_exception = exc
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
