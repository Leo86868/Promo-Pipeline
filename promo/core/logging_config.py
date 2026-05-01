"""Central logging configuration for pgc-pipeline scripts.

Replaces per-script stdlib basic-config calls with a single idempotent
``configure_logging()`` entry point. Output is JSON-per-line with a fixed
field set. Handlers install only when ``configure_logging()`` is called —
importing this module has zero side effects on the root logger.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_PROMO_HANDLER_MARK = "_pgc_pipeline_configured"


class _JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON documents."""

    def __init__(self, correlation_id: str | None = None) -> None:
        super().__init__()
        self._correlation_id = correlation_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self._correlation_id is not None:
            payload["correlation_id"] = self._correlation_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: int = logging.INFO,
    correlation_id: str | None = None,
) -> None:
    """Install the pgc-pipeline JSON-per-line handler on the root logger.

    Idempotent: a second call updates the existing pgc-pipeline handler in
    place (level + formatter) rather than attaching a new one. Unrelated
    handlers installed by other libraries are left alone.
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, _PROMO_HANDLER_MARK, False):
            existing.setLevel(level)
            existing.setFormatter(_JsonFormatter(correlation_id=correlation_id))
            root.setLevel(level)
            return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_JsonFormatter(correlation_id=correlation_id))
    setattr(handler, _PROMO_HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level)
