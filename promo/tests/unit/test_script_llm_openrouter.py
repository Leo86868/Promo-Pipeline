"""Unit tests for the OpenRouter script-generation failover provider.

Pins the switch designed at the adapter layer (``model_adapters.gemini``)
plus its quarantine client (``promo.core.llm.openrouter_text_client``):

1. default-off — env absent leaves the Gemini SDK path untouched, no
   OpenRouter HTTP;
2. provider switch — ``openrouter`` hits the right URL / model / headers;
3. generation_config mapping (GenAI-style → OpenAI-style);
4. retry classification — 429 retryable, 402/403 fail-fast (the same
   ``retry.is_non_retryable_client_error`` the caller's retry wrapper uses);
5. missing OPENROUTER_API_KEY → ConfigError when the provider is selected.
"""

from unittest.mock import MagicMock, patch

import pytest

import requests

from promo.core.model_adapters import gemini as gemini_adapter
from promo.core.llm import openrouter_text_client as otc
from promo.core.llm.openrouter_text_client import (
    OpenRouterTextModel,
    map_generation_config,
)
from promo.core.llm.retry import is_non_retryable_client_error
from promo.core.model_adapters.registry import (
    OPENROUTER_CHAT_COMPLETIONS_API_URL,
    OPENROUTER_SCRIPT_MODEL_API_ID,
)


# Exactly what script_gemini_caller.generate_one passes to generate_content_text.
_SCRIPT_GEN_CONFIG = {"temperature": 0.85, "top_p": 0.9, "max_output_tokens": 10000}


def _fake_http_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class TestDefaultOffGeminiUntouched:
    """(1) With the env absent, resolve_gemini_model takes the Google SDK
    path and no OpenRouter HTTP is issued — byte-identical to before."""

    def test_resolve_routes_to_sdk_when_env_absent(self, monkeypatch):
        monkeypatch.delenv("PROMO_SCRIPT_LLM_PROVIDER", raising=False)
        sentinel = object()
        with patch.object(
            gemini_adapter, "_resolve_gemini_sdk_model", return_value=sentinel
        ) as sdk, patch.object(otc, "post_chat_completion") as http:
            result = gemini_adapter.resolve_gemini_model(log_context="Gemini #1")
        assert result is sentinel
        sdk.assert_called_once_with(log_context="Gemini #1")
        http.assert_not_called()

    def test_generate_content_text_uses_sdk_for_gemini_model(self, monkeypatch):
        monkeypatch.delenv("PROMO_SCRIPT_LLM_PROVIDER", raising=False)
        gemini_model = MagicMock()
        gemini_model.generate_content.return_value.text = "SDK-TEXT"
        with patch.object(otc, "post_chat_completion") as http:
            out = gemini_adapter.generate_content_text(
                gemini_model, "prompt", generation_config=_SCRIPT_GEN_CONFIG
            )
        assert out == "SDK-TEXT"
        gemini_model.generate_content.assert_called_once_with(
            "prompt", generation_config=_SCRIPT_GEN_CONFIG
        )
        http.assert_not_called()


class TestProviderSwitchHitsOpenRouter:
    """(2) provider=openrouter selects the OpenRouter handle and the call
    lands on the right URL / model / auth headers."""

    def test_resolve_returns_openrouter_handle(self, monkeypatch):
        monkeypatch.setenv("PROMO_SCRIPT_LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-TEST")
        monkeypatch.delenv("OPENROUTER_SCRIPT_MODEL", raising=False)
        model = gemini_adapter.resolve_gemini_model(log_context="Gemini #1")
        assert isinstance(model, OpenRouterTextModel)
        assert model.model == OPENROUTER_SCRIPT_MODEL_API_ID == "google/gemini-2.5-pro"

    def test_generate_content_text_posts_to_openrouter(self, monkeypatch):
        monkeypatch.setenv("PROMO_SCRIPT_LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-TEST")
        monkeypatch.delenv("OPENROUTER_HTTP_REFERER", raising=False)

        model = gemini_adapter.resolve_gemini_model(log_context="Gemini #1")
        fake = _fake_http_response(
            {"choices": [{"message": {"content": '{"segments": []}'}}]}
        )
        # Patch requests at the HTTP-owning adapter module (the quarantine
        # boundary), so we exercise the real header/payload assembly.
        with patch(
            "promo.core.model_adapters.openrouter.requests.post", return_value=fake
        ) as post:
            text = gemini_adapter.generate_content_text(
                model, "PROMPT-BODY", generation_config=_SCRIPT_GEN_CONFIG
            )

        assert text == '{"segments": []}'
        post.assert_called_once()
        args, kwargs = post.call_args
        assert args[0] == OPENROUTER_CHAT_COMPLETIONS_API_URL
        payload = kwargs["json"]
        assert payload["model"] == "google/gemini-2.5-pro"
        assert payload["messages"] == [{"role": "user", "content": "PROMPT-BODY"}]
        assert payload["temperature"] == 0.85
        assert payload["top_p"] == 0.9
        assert payload["max_tokens"] == 10000
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-or-TEST"
        assert headers["Content-Type"] == "application/json"

    def test_empty_content_raises_loud(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-TEST")
        model = OpenRouterTextModel(model="google/gemini-2.5-pro")
        fake = _fake_http_response({"choices": [{"message": {"content": ""}}]})
        with patch(
            "promo.core.model_adapters.openrouter.requests.post", return_value=fake
        ):
            with pytest.raises(RuntimeError, match="empty message content"):
                gemini_adapter.generate_content_text(
                    model, "p", generation_config=_SCRIPT_GEN_CONFIG
                )


class TestGenerationConfigMapping:
    """(3) GenAI generation_config → OpenAI-style params."""

    def test_script_config_maps_faithfully(self):
        assert map_generation_config(_SCRIPT_GEN_CONFIG) == {
            "temperature": 0.85,
            "top_p": 0.9,
            "max_tokens": 10000,
        }

    def test_json_mime_maps_to_response_format(self):
        assert map_generation_config(
            {"response_mime_type": "application/json"}
        ) == {"response_format": {"type": "json_object"}}

    def test_unsupported_mime_raises(self):
        with pytest.raises(ValueError, match="Unsupported response_mime_type"):
            map_generation_config({"response_mime_type": "text/plain"})

    def test_unmapped_key_raises(self):
        with pytest.raises(ValueError, match="Unmapped generation_config key"):
            map_generation_config({"top_k": 40})


class TestRetryClassification:
    """(4) OpenRouter HTTP errors classify correctly under the caller's
    retry wrapper: 429 retryable, 402/403 fail-fast. requests.HTTPError
    from raise_for_status() carries .response.status_code."""

    def _http_error(self, status_code: int) -> requests.exceptions.HTTPError:
        err = requests.exceptions.HTTPError(f"{status_code} error")
        err.response = MagicMock(status_code=status_code)
        return err

    def test_429_is_retryable(self):
        assert is_non_retryable_client_error(self._http_error(429)) is False

    def test_403_is_non_retryable(self):
        assert is_non_retryable_client_error(self._http_error(403)) is True

    def test_402_payment_required_is_non_retryable(self):
        # The exact billing-suspension failure mode this failover exists for.
        assert is_non_retryable_client_error(self._http_error(402)) is True

    def test_401_is_non_retryable(self):
        assert is_non_retryable_client_error(self._http_error(401)) is True

    def test_500_is_retryable(self):
        assert is_non_retryable_client_error(self._http_error(500)) is False

    def test_timeout_is_retryable(self):
        assert is_non_retryable_client_error(requests.exceptions.Timeout()) is False


class TestCallerChainOnOpenRouter:
    """End-to-end through the real caller (script_gemini_caller.generate_one):
    OpenRouter handle → adapter → HTTP → parse_json_response, unchanged."""

    def test_generate_one_parses_openrouter_json(self, monkeypatch):
        from promo.core.script import script_gemini_caller

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-TEST")
        model = OpenRouterTextModel(model="google/gemini-2.5-pro")
        fake = _fake_http_response(
            {"choices": [{"message": {"content": '{"segments": [{"segment": 1}]}'}}]}
        )
        with patch(
            "promo.core.model_adapters.openrouter.requests.post", return_value=fake
        ):
            result = script_gemini_caller.generate_one("PROMPT", model)
        assert result == {"segments": [{"segment": 1}]}


class TestMissingKeyFailsLoud:
    """(5) provider=openrouter + missing OPENROUTER_API_KEY → ConfigError
    at model-resolve time (same style as gemini_api_key)."""

    def test_resolve_raises_configerror_when_key_missing(self, monkeypatch):
        from promo.core.config import ConfigError

        monkeypatch.setenv("PROMO_SCRIPT_LLM_PROVIDER", "openrouter")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(ConfigError, match="OPENROUTER_API_KEY is required"):
            gemini_adapter.resolve_gemini_model(log_context="Gemini #1")
