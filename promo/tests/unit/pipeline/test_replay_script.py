"""翻转二 B6 — script recording + --replay-script tests."""

import json

import pytest

from promo.core.pipeline.steps import load_replay_script
from promo.core.pipeline.steps import _step_generate_script


def test_loader_accepts_clip_assignments_sidecar(tmp_path):
    f = tmp_path / "clip_assignments_x_65s.json"
    f.write_text(json.dumps({
        "retrieval_contract": "soft_hint",
        "variants": [{
            "variant_index": 1,
            "script": {
                "segments": [{"segment": 1, "text": "a b c", "pause_weight": 2}],
                "format_mode": "long",
            },
        }],
    }), encoding="utf-8")
    script = load_replay_script(str(f))
    assert script["segments"][0]["text"] == "a b c"


def test_loader_accepts_bare_script_json(tmp_path):
    f = tmp_path / "script.json"
    f.write_text(json.dumps({
        "segments": [{"segment": 1, "text": "x y", "pause_weight": 1}],
    }), encoding="utf-8")
    assert load_replay_script(str(f))["segments"][0]["text"] == "x y"


def test_loader_rejects_pre_recording_sidecar(tmp_path):
    f = tmp_path / "clip_assignments_old.json"
    f.write_text(json.dumps({"variants": [{"variant_index": 1}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="predates script recording"):
        load_replay_script(str(f))


def test_loader_rejects_unknown_shape(tmp_path):
    f = tmp_path / "junk.json"
    f.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="expected a clip_assignments"):
        load_replay_script(str(f))


def test_replay_skips_gemini_and_runs_pause_budget(monkeypatch):
    from promo.core.script import script_generator

    def _no_gemini(**kwargs):
        pytest.fail("Gemini #1 must not be called on the replay path")

    monkeypatch.setattr(
        script_generator, "generate_script_variants", _no_gemini,
    )
    replay = {
        "segments": [
            {"segment": 1, "text": "alpha beta gamma delta", "pause_weight": 2},
            {"segment": 2, "text": "epsilon zeta eta theta", "pause_weight": 1},
        ],
        "format_mode": "long",
    }
    scripts = _step_generate_script(
        poi_name="Test Hotel",
        location="Nowhere",
        clips_metadata=[],
        n_variants=1,
        script_candidates=1,
        target_duration_sec=65.0,
        hotel_description="",
        notable_details="",
        wpm_search_dirs=[],
        resolved_voice_keys=["jarnathan"],
        replay_script=replay,
    )
    assert len(scripts) == 1
    s = scripts[0]
    assert s["variant_index"] == 1
    assert s["total_words"] == 8
    assert s["effective_wpm"] > 0  # wpm calibration loop ran
    # Pause budget recomputed: non-last weight>=2 segment got pause_after_ms.
    assert "pause_after_ms" in s["segments"][0]
    # Narration text held verbatim.
    assert s["segments"][0]["text"] == "alpha beta gamma delta"


def test_env_var_routes_replay(monkeypatch, tmp_path):
    """PROMO_REPLAY_SCRIPT env var (paired with PROMO_CLIP_ASSIGNER) feeds
    the replay path with zero CLI plumbing."""
    import json as _json

    from promo.core.script import script_generator

    monkeypatch.setattr(
        script_generator, "generate_script_variants",
        lambda **kwargs: pytest.fail("Gemini #1 must not be called"),
    )
    f = tmp_path / "script.json"
    f.write_text(_json.dumps({
        "segments": [{"segment": 1, "text": "one two three", "pause_weight": 1}],
    }), encoding="utf-8")
    monkeypatch.setenv("PROMO_REPLAY_SCRIPT", str(f))
    scripts = _step_generate_script(
        poi_name="Test Hotel", location="", clips_metadata=[],
        n_variants=1, script_candidates=1, target_duration_sec=65.0,
        hotel_description="", notable_details="",
        wpm_search_dirs=[], resolved_voice_keys=["jarnathan"],
    )
    assert scripts[0]["total_words"] == 3


def test_replay_rejects_multi_variant():
    with pytest.raises(ValueError, match="n-variants 1|n_variants=1"):
        _step_generate_script(
            poi_name="x", location="", clips_metadata=[],
            n_variants=2, script_candidates=1, target_duration_sec=65.0,
            hotel_description="", notable_details="",
            wpm_search_dirs=[], resolved_voice_keys=["jarnathan"],
            replay_script={"segments": [{"segment": 1, "text": "a"}]},
        )


def test_clip_assignments_row_records_script():
    """The variant_loop sidecar row carries the final script (recorded
    at the same success-gated commit point as the assignments)."""
    import inspect

    from promo.core.pipeline import variant_loop

    source = inspect.getsource(variant_loop)
    assert '"script": {' in source
    assert "pause_weight" in source


# --- 2026-06-11 review: A/B validity + sticky-switch interlock --------------


def test_replay_recounts_word_count_for_pause_budget(monkeypatch):
    """Bug ①: the pause budget estimates spoken time from segment
    word_count labels ALONE — a replay without them reads as a zero-word
    script and maxes every gap's silence, silently changing the B arm's
    pacing. Replay must recount from text."""
    from promo.core.script import script_generator

    monkeypatch.setattr(
        script_generator, "generate_script_variants",
        lambda **kwargs: pytest.fail("Gemini #1 must not be called"),
    )
    replay = {
        "segments": [
            {"segment": 1, "text": "one two three four five", "pause_weight": 2},
            {"segment": 2, "text": "six seven eight", "pause_weight": 1},
        ],
    }
    scripts = _step_generate_script(
        poi_name="Test Hotel", location="", clips_metadata=[],
        n_variants=1, script_candidates=1, target_duration_sec=65.0,
        hotel_description="", notable_details="",
        wpm_search_dirs=[], resolved_voice_keys=["jarnathan"],
        replay_script=replay,
    )
    assert [seg["word_count"] for seg in scripts[0]["segments"]] == [5, 3]
    assert scripts[0]["replay_source"] == "replay"


def test_loader_stamps_source_path(tmp_path):
    f = tmp_path / "script.json"
    f.write_text(json.dumps({
        "segments": [{"segment": 1, "text": "a b", "pause_weight": 1}],
    }), encoding="utf-8")
    loaded = load_replay_script(str(f))
    assert loaded["_source_path"] == str(f)


def test_batch_refuses_sticky_replay_env(monkeypatch, tmp_path):
    """Bug ②: a forgotten PROMO_REPLAY_SCRIPT would render ONE script for
    every POI in the batch — run_batch and resume_batch fail loud at the
    door instead."""
    from promo.cli.run_batch import resume_batch, run_batch

    monkeypatch.setenv("PROMO_REPLAY_SCRIPT", "/tmp/some_script.json")
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({
        "pois": [{"poi_id": "p1", "name": "X"}],
        "videos_per_poi": 1,
        "target_duration_sec": 65,
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="PROMO_REPLAY_SCRIPT"):
        run_batch(
            batch_path=str(batch), output_root=str(tmp_path / "out"),
            command_runner=lambda c: pytest.fail("must not render"),
        )
    with pytest.raises(ValueError, match="PROMO_REPLAY_SCRIPT"):
        resume_batch(
            receipt_path=str(tmp_path / "missing.json"),
            command_runner=lambda c: pytest.fail("must not render"),
        )
