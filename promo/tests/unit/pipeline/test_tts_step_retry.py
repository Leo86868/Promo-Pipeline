import pytest


def _script():
    return {
        "variant_index": 1,
        "segments": [
            {
                "segment": 1,
                "text": "A quiet lodge by the river.",
                "pause_after_ms": 0,
            },
        ],
    }


def test_step_tts_narration_retries_small_assembly_drift(monkeypatch, tmp_path):
    from promo.core.pipeline import steps

    calls = []

    def fake_generate_narration(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError(
                "Narration assembly drift too large: concat ffprobe=60.761s "
                "vs stitched offsets=60.255s (drift=0.506s, tolerance=0.5s)."
            )
        return {"audio_path": str(tmp_path / "narration.mp3")}

    monkeypatch.setattr(
        "promo.core.narrate.tts_engine.generate_narration",
        fake_generate_narration,
    )

    result = steps._step_tts_narration(
        _script(),
        voice_key="jarnathan",
        tmp_dir=str(tmp_path),
        speed=0.95,
    )

    assert result["audio_path"] == str(tmp_path / "narration.mp3")
    assert len(calls) == 2


def test_step_tts_narration_does_not_retry_large_assembly_drift(monkeypatch, tmp_path):
    from promo.core.pipeline import steps

    calls = []

    def fake_generate_narration(**kwargs):
        calls.append(kwargs)
        raise RuntimeError(
            "Narration assembly drift too large: concat ffprobe=65.000s "
            "vs stitched offsets=60.000s (drift=5.000s, tolerance=0.5s)."
        )

    monkeypatch.setattr(
        "promo.core.narrate.tts_engine.generate_narration",
        fake_generate_narration,
    )

    with pytest.raises(RuntimeError, match="drift too large"):
        steps._step_tts_narration(
            _script(),
            voice_key="jarnathan",
            tmp_dir=str(tmp_path),
            speed=0.95,
        )

    assert len(calls) == 1
