"""Sprint 14 item (b) — promo.core.logging_config tests.

Covers AC4 (idempotent configure_logging + JSON output + correlation_id) and
AC5 (zero import-time side effects on the root logger).
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest


@pytest.fixture(autouse=True)
def _snapshot_root_logger():
    """Snapshot + restore the root logger across each test."""
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    root.handlers = before_handlers
    root.setLevel(before_level)


def _promo_handler_count(root: logging.Logger) -> int:
    from promo.core.logging_config import _PROMO_HANDLER_MARK

    return sum(
        1 for h in root.handlers if getattr(h, _PROMO_HANDLER_MARK, False)
    )


def test_import_has_no_side_effects():
    """AC5: importing the module does not attach a handler to the root logger."""
    root = logging.getLogger()
    before = list(root.handlers)

    import importlib

    import promo.core.logging_config

    importlib.reload(promo.core.logging_config)

    after = list(root.handlers)
    assert before == after, (
        f"import of promo.core.logging_config added/removed handlers: "
        f"{before!r} -> {after!r}"
    )


def test_configure_logging_idempotent():
    """AC4: calling configure_logging() twice does not install a second handler."""
    from promo.core.logging_config import configure_logging

    root = logging.getLogger()
    assert _promo_handler_count(root) == 0

    configure_logging()
    assert _promo_handler_count(root) == 1

    configure_logging()
    assert _promo_handler_count(root) == 1, (
        "second configure_logging() call attached a duplicate handler"
    )


def test_configure_logging_emits_json():
    """AC4: records emitted after configure_logging() parse as JSON with required fields."""
    from promo.core.logging_config import configure_logging, _PROMO_HANDLER_MARK

    configure_logging()
    root = logging.getLogger()
    handler = next(h for h in root.handlers if getattr(h, _PROMO_HANDLER_MARK, False))

    buf = io.StringIO()
    original_stream = handler.stream
    handler.stream = buf
    try:
        logging.getLogger("promo.test").info("hello world")
    finally:
        handler.stream = original_stream

    raw = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(raw)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "promo.test"
    assert "timestamp" in payload
    assert "correlation_id" not in payload, (
        "correlation_id must be absent when not passed"
    )


def test_configure_logging_with_correlation_id():
    """AC4: passing correlation_id injects the field into emitted records."""
    from promo.core.logging_config import configure_logging, _PROMO_HANDLER_MARK

    configure_logging(correlation_id="sprint-14-trace")
    root = logging.getLogger()
    handler = next(h for h in root.handlers if getattr(h, _PROMO_HANDLER_MARK, False))

    buf = io.StringIO()
    original_stream = handler.stream
    handler.stream = buf
    try:
        logging.getLogger("promo.test").warning("correlated")
    finally:
        handler.stream = original_stream

    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["correlation_id"] == "sprint-14-trace"
    assert payload["level"] == "WARNING"


def test_configure_logging_updates_existing_handler_on_second_call():
    """AC4: second call updates level + correlation_id of the existing handler."""
    from promo.core.logging_config import configure_logging, _PROMO_HANDLER_MARK

    configure_logging(level=logging.INFO, correlation_id="first")
    configure_logging(level=logging.DEBUG, correlation_id="second")

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    handler = next(h for h in root.handlers if getattr(h, _PROMO_HANDLER_MARK, False))
    assert handler.level == logging.DEBUG

    buf = io.StringIO()
    original_stream = handler.stream
    handler.stream = buf
    try:
        logging.getLogger("promo.test").debug("after-second-call")
    finally:
        handler.stream = original_stream

    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["correlation_id"] == "second"
