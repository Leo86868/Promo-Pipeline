"""Unit tests for promo.core.llm.retry.

2026-06-09 fail-fast fix: deterministic HTTP 4xx errors (bad key,
geo-block, invalid argument) must NOT be retried — a production batch
burned 15 identical Gemini calls per video on "400 User location is not
supported". 408/429 and 5xx stay retryable.
"""

import pytest

from promo.core.llm import retry as retry_mod
from promo.core.llm.retry import is_non_retryable_client_error, retry_with_backoff


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(retry_mod.time, "sleep", lambda seconds: None)


class TestClientErrorClassifier:
    def test_gemini_location_400_text_is_non_retryable(self):
        # Verbatim from runs/production-preview-20260601T105019Z/batch_run.log
        exc = RuntimeError("400 User location is not supported for the API use.")
        assert is_non_retryable_client_error(exc) is True

    def test_adapter_style_401_text_is_non_retryable(self):
        exc = RuntimeError("ElevenLabs API error 401: invalid api key")
        assert is_non_retryable_client_error(exc) is True

    def test_requests_style_404_text_is_non_retryable(self):
        exc = RuntimeError("404 Client Error: Not Found for url: https://x")
        assert is_non_retryable_client_error(exc) is True

    def test_429_and_408_stay_retryable(self):
        assert is_non_retryable_client_error(
            RuntimeError("ElevenLabs API error 429: rate limited")
        ) is False
        assert is_non_retryable_client_error(RuntimeError("408 Request Timeout")) is False

    def test_5xx_and_plain_errors_stay_retryable(self):
        assert is_non_retryable_client_error(
            RuntimeError("ElevenLabs API error 503: overloaded")
        ) is False
        assert is_non_retryable_client_error(RuntimeError("connection reset")) is False

    def test_incidental_3_digit_numbers_do_not_misclassify(self):
        assert is_non_retryable_client_error(
            RuntimeError("expected 480 frames but rendered 412")
        ) is False

    def test_status_code_attribute_wins_over_text(self):
        class FakeHttpError(RuntimeError):
            status_code = 403

        assert is_non_retryable_client_error(FakeHttpError("forbidden")) is True

        class FakeServerError(RuntimeError):
            status_code = 500

        # Numeric 5xx is authoritative even if the text mentions a 4xx.
        assert is_non_retryable_client_error(
            FakeServerError("500 while proxying a 404 upstream")
        ) is False


class TestRetryWithBackoffFailFast:
    def test_non_retryable_error_fails_on_first_attempt(self):
        calls = {"n": 0}

        def _always_400():
            calls["n"] += 1
            raise RuntimeError("400 User location is not supported for the API use.")

        with pytest.raises(RuntimeError, match="User location"):
            retry_with_backoff(_always_400, max_retries=3)
        assert calls["n"] == 1

    def test_transient_error_still_retries_to_success(self):
        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("connection reset by peer")
            return "ok"

        assert retry_with_backoff(_flaky, max_retries=3) == "ok"
        assert calls["n"] == 3

    def test_give_up_override_restores_blind_retrying(self):
        calls = {"n": 0}

        def _always_400():
            calls["n"] += 1
            raise RuntimeError("400 Bad Request")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                _always_400, max_retries=2, give_up=lambda exc: False,
            )
        assert calls["n"] == 2


class TestElevenLabsTtsRetry:
    """2026-06-09: the ElevenLabs with-timestamps call gets bounded retries
    for transient failures; deterministic 4xx still fail fast."""

    def test_transient_failure_then_success(self, monkeypatch):
        from promo.core.narrate import tts_elevenlabs

        calls = {"n": 0}

        def fake_adapter(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("ElevenLabs request failed: connection reset")
            return {"audio_base64": "ok"}

        monkeypatch.setattr(
            tts_elevenlabs, "call_elevenlabs_with_timestamps", fake_adapter,
        )
        result = tts_elevenlabs._call_elevenlabs_with_timestamps("hi", "voice-1")
        assert result == {"audio_base64": "ok"}
        assert calls["n"] == 2

    def test_bad_key_fails_fast_without_retries(self, monkeypatch):
        from promo.core.narrate import tts_elevenlabs

        calls = {"n": 0}

        def fake_adapter(**kwargs):
            calls["n"] += 1
            raise RuntimeError("ElevenLabs API error 401: invalid api key")

        monkeypatch.setattr(
            tts_elevenlabs, "call_elevenlabs_with_timestamps", fake_adapter,
        )
        with pytest.raises(RuntimeError, match="401"):
            tts_elevenlabs._call_elevenlabs_with_timestamps("hi", "voice-1")
        assert calls["n"] == 1
