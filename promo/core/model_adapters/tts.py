"""TTS provider HTTP adapters."""

from __future__ import annotations

import base64
from typing import Any, Callable

import requests

from promo.core.config import elevenlabs_api_key, gemini_api_key
from promo.core.model_adapters.registry import (
    ELEVENLABS_API_BASE,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_OUTPUT_FORMAT,
    GEMINI_TTS_API_BASE,
)


def generate_gemini_tts_pcm(
    *,
    text: str,
    model: str,
    voice: str,
    timeout: int = 180,
) -> bytes:
    """Call Gemini TTS ``generateContent`` and return raw PCM bytes."""
    url = f"{GEMINI_TTS_API_BASE}/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": gemini_api_key(),
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }
    response = requests.post(url, headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    parts = payload["candidates"][0]["content"]["parts"]
    for part in parts:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError(
        f"Gemini TTS response missing inlineData audio (keys: "
        f"{list(payload.get('candidates', [{}])[0].get('content', {}).keys())})"
    )


def call_elevenlabs_with_timestamps(
    *,
    text: str,
    voice_id: str,
    voice_settings: dict[str, Any],
    model_id: str = ELEVENLABS_MODEL_ID,
    output_format: str = ELEVENLABS_OUTPUT_FORMAT,
    timeout: int = 180,
    requests_post: Callable[..., Any] | None = None,
) -> dict[str, object]:
    """Call ElevenLabs ``with-timestamps`` and return parsed JSON."""
    post = requests_post or requests.post
    url = f"{ELEVENLABS_API_BASE}/v1/text-to-speech/{voice_id}/with-timestamps"
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }
    params = {"output_format": output_format}
    headers = {
        "xi-api-key": elevenlabs_api_key(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        response = post(
            url, params=params, json=payload, headers=headers, timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"ElevenLabs request failed: {exc}") from exc

    if response.status_code >= 400:
        body = response.text[:800]
        raise RuntimeError(
            f"ElevenLabs API error {response.status_code}: {body}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"ElevenLabs returned non-JSON response: {response.text[:300]!r}"
        ) from exc
