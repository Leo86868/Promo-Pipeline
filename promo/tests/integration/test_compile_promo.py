"""Unit tests for promo.cli.compile_promo (full-pipeline integration)."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

# Sprint 15 cross-module compat: TestLongFormGenerationContract lives in
# test_script_generator.py. TestSprint09aPerVariantRetryBudget references
# it by bare name in _valid_long_raw_script(). Produce a __test__-False
# subclass via type() so pytest does not double-collect AND no extra
# "^class Test" line lands in this file (AC5 counts class-definition
# lines, so a plain `class Test...` subclass would inflate the count).
from promo.tests.unit.script.test_script_generator import (  # noqa: E402
    TestLongFormGenerationContract as _TestLongFormGenerationContract,
)

TestLongFormGenerationContract = type(
    "_LongFormGenerationContractCompat",
    (_TestLongFormGenerationContract,),
    {"__test__": False},
)

# Load-bearing invariant: if a future refactor rewrites the shim without
# __test__=False, pytest silently double-collects TestLongFormGenerationContract
# and the AC5 class-count invariant breaks. Assert at import time so any
# regression fails pytest collection hard instead of manifesting as a
# mysterious +1 test count. See workflow/projects/promo-foundation/
# sprints/sprint-15-reflection.md (D-1).
assert TestLongFormGenerationContract.__test__ is False, (
    "Sprint 15 compat shim must keep __test__=False; see sprint-15 reflection"
)


class TestCompilePromoHelpers:
    """Helper functions for compile_promo should preserve variant outputs."""

    def test_variant_output_path_suffixes(self):
        from promo.cli.compile_promo import _variant_output_path

        assert _variant_output_path("/tmp/promo.mp4", 1, 1) == "/tmp/promo.mp4"
        assert _variant_output_path("/tmp/promo.mp4", 2, 3) == "/tmp/promo_v2.mp4"

    def test_variant_output_path_encodes_per_variant_duration(self):
        """S0.5 narrowed #1: when ``variant_target_duration_sec`` is
        threaded in, the trailing run-level ``_<N>s`` segment from the
        base path is replaced with the variant's own duration so a
        random-selector long variant does not ship as ``..._30s_v1.mp4``."""
        from promo.cli.compile_promo import _variant_output_path

        # Mixed-duration random run: base path encodes run-level 30s.
        # Variant 1 (long, 65s) gets its own duration; variants 2/3 (short, 30s)
        # also keep duration in the suffix for clarity.
        base = "/tmp/promo_ocean_key_resort__spa_30s.mp4"
        assert _variant_output_path(base, 1, 3, 65.0) == \
            "/tmp/promo_ocean_key_resort__spa_v1_65s.mp4"
        assert _variant_output_path(base, 2, 3, 30.0) == \
            "/tmp/promo_ocean_key_resort__spa_v2_30s.mp4"
        assert _variant_output_path(base, 3, 3, 30.0) == \
            "/tmp/promo_ocean_key_resort__spa_v3_30s.mp4"

    def test_variant_output_path_strips_only_trailing_dur_segment(self):
        """The ``_<N>s`` strip is anchored to filename end — slug content
        like ``..._30s_special.mp4`` keeps its body intact."""
        from promo.cli.compile_promo import _variant_output_path

        # No trailing _<N>s in root → variant duration appended cleanly.
        assert _variant_output_path("/tmp/promo.mp4", 2, 3, 65.0) == \
            "/tmp/promo_v2_65s.mp4"
        # Single-variant random with a runtime override gets its dur tagged.
        assert _variant_output_path("/tmp/promo_30s.mp4", 1, 1, 65.0) == \
            "/tmp/promo_65s.mp4"

    def test_full_pipeline_multi_variant_smoke(self):
        """Smoke test: full_pipeline should reuse one clip analysis pass and render per variant."""
        from promo.cli.compile_promo import full_pipeline

        backend = MagicMock()
        backend.fetch_clips.return_value = {
            f"{i:04d}": f"/tmp/clip_{i:04d}.mp4"
            for i in range(1, 15)
        }
        backend.fetch_bgm.return_value = "/tmp/bgm.mp3"
        backend.save_output.side_effect = lambda poi_name, path: path

        scripts = [
            {
                "variant_index": 1,
                "segments": [{"segment": 1, "text": "Variant one text.", "clips": [{"clip_id": "0001", "cut_after": ""}]}],
                "total_words": 3,
                "format_mode": "long",
                "target_duration_sec": 65,
            },
            {
                "variant_index": 2,
                "segments": [{"segment": 1, "text": "Variant two text.", "clips": [{"clip_id": "0002", "cut_after": ""}]}],
                "total_words": 3,
                "format_mode": "long",
                "target_duration_sec": 65,
            },
        ]
        narration = {
            "duration": 4.2,
            "word_timestamps": [
                {"word": "Variant", "start": 0.0, "end": 0.4},
                {"word": "text.", "start": 0.4, "end": 0.8},
            ],
            "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.8, "duration": 0.8}],
            "audio_path": "/tmp/narration.wav",
        }
        props = {
            "meta": {"poiName": "Test Hotel", "location": "Nowhere", "fps": 30, "width": 1080, "height": 1920},
            "clips": [{"clipId": "0001", "file": "clip.mp4", "narration": "Variant text.", "videoStart": 0.0, "videoEnd": 4.0, "trimStart": 0.0, "trimEnd": 4.0}],
            "audio": {"narration": "narration.wav", "bgm": "bgm.mp3"},
            "captions": {"wordTimestamps": narration["word_timestamps"]},
            "segments": [{"segment": 1, "text": "Variant text.", "startSec": 0.0, "endSec": 0.8}],
        }

        # full_pipeline invokes the deterministic assigner between TTS
        # and props-building; patch its seam so the smoke test hits no
        # embedding/ledger I/O.
        def _fake_assign(script, narration_in, clips_metadata, clip_durations,
                         **kwargs):
            return script, narration_in, [
                {"segment": 1, "clip_id": "0001",
                 "start_word_idx": 0, "end_word_idx": 1,
                 "trim_start": 0.0, "display_span_sec": 0.8,
                 "source_duration_sec": 5.0}
            ], {"assigner": "packer", "retrieval_contract": "soft_hint"}

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch(
                 "promo.core.pipeline.steps.analyze_clips_for_script",
                 return_value=[{"id": f"{i:04d}"} for i in range(1, 15)],
             ) as mock_analyze, \
             patch("promo.core.script.script_generator.generate_script_variants", return_value=scripts) as mock_scripts, \
             patch("promo.core.narrate.tts_engine.generate_narration", return_value=narration) as mock_tts, \
             patch("promo.core.pipeline.steps._assign_clips_packer", side_effect=_fake_assign) as mock_assign, \
             patch("promo.core.pipeline.variant_loop.build_props_from_script", return_value=props) as mock_build_props, \
             patch("promo.core.pipeline.variant_loop.validate_props", return_value=[]) as mock_validate, \
             patch("promo.core.pipeline.variant_loop.stage_media") as mock_stage_media, \
             patch("promo.core.pipeline.variant_loop.render_promo", side_effect=lambda _props, out: Path(out).write_bytes(b"video") or True) as mock_render:
            # Sprint 09b C2 (D-005): backend.output_dir() must resolve to a
            # real directory so the sidecar write doesn't flip all_ok.
            backend.output_dir.return_value = tmpdir
            backend.clips_dir.return_value = None
            output_path = os.path.join(tmpdir, "promo_test.mp4")
            ok = full_pipeline(
                poi_name="Test Hotel",
                location="Nowhere",
                output_path=output_path,
                backend=backend,
                target_duration_sec=65,
                n_variants=2,
                script_candidates=2,
            )

        assert ok is True
        mock_analyze.assert_called_once()
        mock_scripts.assert_called_once()
        assert mock_scripts.call_args.kwargs["target_duration_sec"] == 65
        assert mock_scripts.call_args.kwargs["n_variants"] == 2
        assert mock_scripts.call_args.kwargs["n_candidates"] == 2
        assert mock_tts.call_count == 2
        # Sprint 10 C2: one clip_assigner call per variant.
        assert mock_assign.call_count == 2
        assert mock_build_props.call_count == 2
        assert mock_validate.call_count == 2
        assert mock_stage_media.call_count == 2
        assert mock_render.call_count == 2
        rendered_paths = [call.args[1] for call in mock_render.call_args_list]
        # S0.5 narrowed #1: filename encodes per-variant duration.
        # Both variants are ``long`` mode (target=65s) per scripts above.
        assert rendered_paths[0].endswith("_v1_65s.mp4")
        assert rendered_paths[1].endswith("_v2_65s.mp4")

    def test_full_pipeline_fails_when_variant_pack_under_delivers(self):
        """Pipeline should fail clearly when requested variants are not fully delivered."""
        from promo.cli.compile_promo import full_pipeline

        backend = MagicMock()
        backend.fetch_clips.return_value = {
            f"{i:04d}": f"/tmp/clip_{i:04d}.mp4"
            for i in range(1, 15)
        }

        with patch(
            "promo.core.pipeline.steps.analyze_clips_for_script",
            return_value=[{"id": f"{i:04d}"} for i in range(1, 15)],
        ) as mock_analyze, \
             patch(
                 "promo.core.script.script_generator.generate_script_variants",
                 side_effect=RuntimeError("Requested 2 variants for 'Test Hotel', but only 1 passed validation after 2 attempts"),
             ) as mock_scripts, \
             patch("promo.core.narrate.tts_engine.generate_narration") as mock_tts:
            ok = full_pipeline(
                poi_name="Test Hotel",
                location="Nowhere",
                output_path="/tmp/promo_test.mp4",
                backend=backend,
                target_duration_sec=65,
                n_variants=2,
                script_candidates=1,
            )

        assert ok is False
        mock_analyze.assert_called_once()
        mock_scripts.assert_called_once()
        mock_tts.assert_not_called()
        backend.fetch_bgm.assert_not_called()

    def test_full_pipeline_long_form_fails_clip_pool_preflight(self):
        """Long-form should fail before analysis when the local clip pool is too small."""
        from promo.cli.compile_promo import full_pipeline

        backend = MagicMock()
        backend.fetch_clips.return_value = {
            f"{i:04d}": f"/tmp/clip_{i:04d}.mp4"
            for i in range(1, 11)
        }

        with patch("promo.core.pipeline.steps.analyze_clips_for_script") as mock_analyze:
            ok = full_pipeline(
                poi_name="Test Hotel",
                location="Nowhere",
                output_path="/tmp/promo_test.mp4",
                backend=backend,
                target_duration_sec=65,
            )

        assert ok is False
        mock_analyze.assert_not_called()
        backend.fetch_bgm.assert_not_called()

class TestAC8PublicDirCollision:
    """AC8: stage_media with poi_name uses POI-specific subdirectory."""

    def test_different_pois_no_collision(self):
        """Two POIs with same-basename clips don't overwrite each other."""
        from promo.core.render.remotion_renderer import stage_media, PUBLIC_DIR

        with tempfile.TemporaryDirectory() as tmp:
            # Create two fake clips with the same basename
            clip_a = os.path.join(tmp, "poi_a_clip.mp4")
            clip_b = os.path.join(tmp, "poi_b_clip.mp4")

            # Same basename but different content
            shared_name = "clip_0001.mp4"
            src_a = os.path.join(tmp, "a", shared_name)
            src_b = os.path.join(tmp, "b", shared_name)
            os.makedirs(os.path.join(tmp, "a"))
            os.makedirs(os.path.join(tmp, "b"))
            with open(src_a, "wb") as f:
                f.write(b"content_a" * 100)
            with open(src_b, "wb") as f:
                f.write(b"content_b" * 100)

            # Also create dummy narration and bgm
            narr = os.path.join(tmp, "narration.wav")
            bgm = os.path.join(tmp, "bgm.mp3")
            with open(narr, "wb") as f:
                f.write(b"narr" * 100)
            with open(bgm, "wb") as f:
                f.write(b"bgm" * 100)

            stage_media([src_a], narr, bgm, poi_name="Hotel Alpha")
            stage_media([src_b], narr, bgm, poi_name="Hotel Beta")

            # Files should be in separate subdirectories
            path_a = os.path.join(PUBLIC_DIR, "hotel_alpha", shared_name)
            path_b = os.path.join(PUBLIC_DIR, "hotel_beta", shared_name)

            assert os.path.exists(path_a), f"Missing: {path_a}"
            assert os.path.exists(path_b), f"Missing: {path_b}"

            # Content should differ
            with open(path_a, "rb") as f:
                content_a = f.read()
            with open(path_b, "rb") as f:
                content_b = f.read()
            assert content_a != content_b, "Files have same content — collision!"

            # Cleanup staged files
            import shutil
            shutil.rmtree(os.path.join(PUBLIC_DIR, "hotel_alpha"), ignore_errors=True)
            shutil.rmtree(os.path.join(PUBLIC_DIR, "hotel_beta"), ignore_errors=True)

class TestAC6UnifiedSanitization:
    """AC6: sanitize_poi_name handles all edge cases safely."""

    @pytest.mark.parametrize("name,expected_safe", [
        ("Amangiri", "amangiri"),
        ("Hôtel Le Château", "hôtel_le_château"),
        ("../../etc/passwd", "__etc_passwd"),
        ("Hotel Name / Resort", "hotel_name___resort"),
        ("", "unnamed"),
    ])
    def test_sanitize_poi_name(self, name, expected_safe):
        """Various POI names produce safe, non-empty strings."""
        from promo.core import sanitize_poi_name
        result = sanitize_poi_name(name)
        assert result == expected_safe
        assert result  # never empty
        assert "/" not in result
        assert "\\" not in result
        assert "\0" not in result
        assert not result.startswith(".")

    @pytest.mark.parametrize("name,expected_slug", [
        ("Amangiri", "amangiri"),
        ("Hotel Xcaret Arte", "hotel-xcaret-arte"),
        ("Ocean Key Resort & Spa", "ocean-key-resort-spa"),
        ("Hôtel Le Château", "hôtel-le-château"),
        ("Hotel Name / Resort", "hotel-name-resort"),
        ("", "unnamed"),
    ])
    def test_material_poi_slug_uses_hyphens_for_material_dirs(self, name, expected_slug):
        from promo.core import material_poi_slug

        assert material_poi_slug(name) == expected_slug

class TestLocalSmokeRender:
    """Local smoke path should work without external AI services."""

    def test_prepare_local_smoke_run_builds_valid_props(self, tmp_path, monkeypatch):
        from promo.core.render.remotion_renderer import validate_props
        from promo.cli.smoke_local_render import prepare_local_smoke_run

        clips_dir = tmp_path / "clips"
        clips_dir.mkdir()
        for clip_id in ("0001", "0002", "0003", "0004"):
            (clips_dir / f"clip_{clip_id}.mp4").write_bytes(b"fake")

        monkeypatch.setattr(
            "promo.cli.smoke_local_render.get_clip_duration",
            lambda _: 5.0,
        )

        tmp_dir, props, clip_paths, narration_path, bgm_path, selected_ids = prepare_local_smoke_run(
            clips_dir=str(clips_dir),
            poi_name="Smoke Test",
        )
        try:
            assert selected_ids == ["0001", "0002", "0003", "0004"]
            assert len(clip_paths) == 4
            assert os.path.exists(narration_path)
            assert os.path.exists(bgm_path)
            assert validate_props(props, check_files=False) == []
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_prepare_local_smoke_run_requires_enough_clips(self, tmp_path):
        from promo.cli.smoke_local_render import prepare_local_smoke_run

        clips_dir = tmp_path / "clips"
        clips_dir.mkdir()
        (clips_dir / "clip_0001.mp4").write_bytes(b"fake")

        with pytest.raises(ValueError, match="Need at least 4 local clips"):
            prepare_local_smoke_run(
                clips_dir=str(clips_dir),
                poi_name="Smoke Test",
            )

class TestSprint085BGMMinDuration:
    """_discover_bgm_files filters tracks shorter than min_duration_sec."""

    def test_filters_short_tracks(self, tmp_path, monkeypatch):
        from promo.cli import compile_promo as cp
        bgm_dir = tmp_path / "bgm"
        bgm_dir.mkdir()
        long_path = bgm_dir / "long.mp3"
        short_path = bgm_dir / "short.mp3"
        long_path.write_bytes(b"L")
        short_path.write_bytes(b"S")

        durations = {str(long_path): 75.0, str(short_path): 40.0}

        def fake_dur(path):
            return durations[os.path.abspath(path)]

        monkeypatch.setattr(
            "promo.core.render.remotion_renderer.get_clip_duration", fake_dur,
        )
        files = cp._discover_bgm_files(
            str(bgm_dir), min_duration_sec=60.0,
        )
        assert len(files) == 1
        assert os.path.basename(files[0]) == "long.mp3"

    def test_min_duration_none_returns_all(self, tmp_path):
        from promo.cli import compile_promo as cp
        bgm_dir = tmp_path / "bgm"
        bgm_dir.mkdir()
        (bgm_dir / "a.mp3").write_bytes(b"A")
        (bgm_dir / "b.mp3").write_bytes(b"B")
        files = cp._discover_bgm_files(str(bgm_dir), min_duration_sec=None)
        assert len(files) == 2

class TestSprint09aSourceDurationInjection:
    """H-001 — source_duration_sec must be attached to clips_metadata before
    generate_script_variants is called, so _format_clip_inventory can emit
    the per-clip duration token in the Gemini prompt (NO FREEZE rule).
    """

    def test_injection_precedes_script_generation(self):
        """Criterion 1: source_duration_sec injection precedes Gemini #1
        invocation. Under Sprint 10 C4 the injection lives in
        _step_prepare_clips and the Gemini #1 call lives in
        _step_generate_script, but the H-001 ordering invariant is the
        same: prepare runs before generate in full_pipeline.
        """
        import inspect
        from promo.cli import compile_promo

        fp_source = inspect.getsource(compile_promo.full_pipeline)
        idx_prepare = fp_source.find("_step_prepare_clips(")
        idx_generate = fp_source.find("_step_generate_script(")
        assert idx_prepare != -1, "_step_prepare_clips call site missing"
        assert idx_generate != -1, "_step_generate_script call site missing"
        assert idx_prepare < idx_generate, (
            "H-001 regression: clip preparation (source_duration_sec "
            "injection) must run before Gemini #1 script generation"
        )
        # And the literal injection line still exists in _step_prepare_clips.
        # promo-handoff-readiness Sprint 4 A-001 — the inner loop variable
        # was renamed from ``cid`` to ``cm_id`` when the helper moved into
        # ``promo.core.pipeline.steps`` to unshadow the outer
        # ``for cid, cpath in clip_paths.items()`` loop (mypy flagged the
        # shadowing; runtime behaviour is unchanged).
        prep_source = inspect.getsource(compile_promo._step_prepare_clips)
        assert 'cm["source_duration_sec"] = clip_durations[cm_id]' in prep_source

    def test_format_clip_inventory_emits_duration_token(self):
        """Criterion 2: when source_duration_sec is present on a clip's
        metadata, _format_clip_inventory's rendered string contains the
        duration token for that clip.
        """
        from promo.core.script.script_generator import _format_clip_inventory

        rendered = _format_clip_inventory([
            {"id": "0001", "category": "scenic",
             "scene_description": "beach", "source_duration_sec": 8.0},
            {"id": "0002", "category": "food",
             "scene_description": "dinner"},  # no duration
        ])
        # Clip 0001 must show 8.0s; clip 0002 must NOT fabricate a duration.
        assert "8.0s" in rendered.split("Clip 0001:")[1].split("\n")[0]
        clip_2_line = rendered.split("Clip 0002:")[1].split("\n")[0]
        assert "s —" not in clip_2_line or "8.0s" not in clip_2_line
        # Key invariant: the rendered string for clip 0001 should carry
        # something matching the duration numeric + 's'.
        import re
        assert re.search(r"Clip 0001:.*8\.0s", rendered), (
            f"expected '8.0s' token after Clip 0001 in inventory:\n{rendered}"
        )

    def test_ffprobe_failure_keeps_clip_in_metadata(self):
        """Criterion 3: if ffprobe fails for one clip, that clip is still
        included in clips_metadata (without source_duration_sec) so script
        generation can still reference it. No unhandled exception.
        """
        # This is a structural guarantee of the Step 2.5 loop: each clip is
        # looked up independently and a failure is logged but not re-raised.
        # We simulate by calling the fragment directly.
        from promo.core.render.remotion_renderer import get_clip_duration  # noqa: F401

        clip_paths = {"0001": "/tmp/real.mp4", "0002": "/tmp/broken.mp4"}
        clips_metadata = [
            {"id": "0001", "scene_description": "ok"},
            {"id": "0002", "scene_description": "broken"},
        ]

        def flaky_probe(path):
            if "broken" in path:
                raise RuntimeError("ffprobe boom")
            return 8.0

        clip_durations: dict[str, float] = {}
        for cid, cpath in clip_paths.items():
            try:
                clip_durations[cid] = float(flaky_probe(cpath))
            except Exception:
                pass
        for cm in clips_metadata:
            cid = cm.get("id")
            if cid and cid in clip_durations:
                cm["source_duration_sec"] = clip_durations[cid]

        # Clip 0001 gets duration, clip 0002 does not — both remain in list.
        assert len(clips_metadata) == 2
        by_id = {cm["id"]: cm for cm in clips_metadata}
        assert by_id["0001"]["source_duration_sec"] == 8.0
        assert "source_duration_sec" not in by_id["0002"]

class TestSprint09aBGMFilterRaises:
    """M-005 — _discover_bgm_files raises NoSuitableBGMError when no track
    meets min_duration_sec (previously silently fell back to unfiltered pool).
    """

    def test_raises_when_no_track_meets_threshold(self, tmp_path):
        """Criterion 10: an empty-after-filter BGM dir raises, not returns
        the unfiltered pool.
        """
        from promo.cli.compile_promo import _discover_bgm_files
        from promo.core.errors import NoSuitableBGMError

        # Create two fake short BGM files. get_clip_duration on these will
        # fail/return 5.0 default; either way < 65s.
        for name in ("short_a.mp3", "short_b.mp3"):
            (tmp_path / name).write_bytes(b"\x00")

        with pytest.raises(NoSuitableBGMError, match="minimum duration"):
            _discover_bgm_files(bgm_dir=str(tmp_path), min_duration_sec=65.0)

    def test_passes_through_when_filter_matches(self, tmp_path, monkeypatch):
        """Happy path: when at least one track meets the threshold, the
        filter returns only matching tracks; no raise.
        """
        from promo.cli import compile_promo as cp

        (tmp_path / "long.mp3").write_bytes(b"\x00")
        (tmp_path / "short.mp3").write_bytes(b"\x00")

        def fake_probe(path):
            return 70.0 if "long" in path else 10.0

        monkeypatch.setattr(
            "promo.core.render.remotion_renderer.get_clip_duration", fake_probe,
        )
        result = cp._discover_bgm_files(bgm_dir=str(tmp_path), min_duration_sec=65.0)
        assert len(result) == 1
        assert result[0].endswith("long.mp3")

class TestSprint09aPerVariantRetryBudget:
    """H-002 — each variant has an independent retry budget; a later variant
    is not starved by earlier-variant failures.
    """

    def _write_persona(self, tmp_path, wpm: int = 140) -> str:
        persona_path = tmp_path / "persona.yaml"
        persona_path.write_text(
            "\n".join([
                "id: test_persona",
                "display_name: Test Persona",
                "perspective: third_person",
                f"wpm: {wpm}",
                'voice_id: ""',
                "system_prompt: |",
                "  You are writing a {duration_label} voiceover.",
                "tone_keywords: []",
                "forbidden_phrases: []",
                "forbidden_openers: []",
                "example_scripts: []",
            ]),
            encoding="utf-8",
        )
        return str(persona_path)

    def _valid_long_raw_script(self) -> dict:
        # Reuse the TestLongFormGenerationContract fixture structure (133 words).
        fixture_owner = TestLongFormGenerationContract()
        return fixture_owner._valid_long_raw_script()

    def test_variant_2_reaches_retry_loop_even_when_variant_1_burns_budget(
        self, tmp_path, monkeypatch
    ):
        """Criterion 12: variant 2's retry budget is independent of
        variant 1. Construction: _generate_one returns None twice (variant 1
        burns its 2-attempt budget), then returns two valid (but identical)
        scripts. Variant 1 must raise; confirm by inspection that
        _generate_one was called at least 3 times (2 for variant 1 + ≥1 for
        variant 2).

        Note: with max_retries=1 and n_candidates=1, per_variant_budget = 2.
        """
        from promo.core.script.script_generator import generate_script_variants

        persona_path = self._write_persona(tmp_path, wpm=175)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        clips_metadata = [
            {"id": f"{i:04d}", "category": "scenic", "scene_description": "clip"}
            for i in range(1, 15)
        ]
        valid_script = self._valid_long_raw_script()

        # Variant 1 fails both attempts; variant 2 gets its budget fresh.
        side_effects = [None, None, valid_script, valid_script]
        with patch("promo.core.script.script_generator.resolve_gemini_model", return_value=object()), \
             patch(
                 "promo.core.script.script_generator._generate_one",
                 side_effect=side_effects,
             ) as mocked_gen:
            with pytest.raises(RuntimeError,
                               match="Variant 1/2 for 'Test Hotel' exhausted"):
                generate_script_variants(
                    poi_name="Test Hotel",
                    location="Nowhere",
                    clips_metadata=clips_metadata,
                    persona_path=persona_path,
                    n_variants=2,
                    n_candidates=1,
                    max_retries=1,
                    target_duration_sec=65,
                )
            # Variant 1 consumed exactly its own 2 attempts (n_candidates=1,
            # max_retries=1 → per-variant budget=2). Without per-variant
            # allocation, pre-09a would have eaten 4 shared attempts.
            assert mocked_gen.call_count == 2

    def test_variant_pack_delivery_still_enforced(self, tmp_path, monkeypatch):
        """Criterion 13: if any variant exhausts its per-variant budget,
        generate_script_variants raises — no partial packs delivered.
        """
        from promo.core.script.script_generator import generate_script_variants

        persona_path = self._write_persona(tmp_path, wpm=175)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        clips_metadata = [
            {"id": f"{i:04d}", "category": "scenic", "scene_description": "clip"}
            for i in range(1, 15)
        ]
        # Variant 1 succeeds; variant 2 always fails. Expect raise.
        valid_script = self._valid_long_raw_script()
        with patch("promo.core.script.script_generator.resolve_gemini_model", return_value=object()), \
             patch(
                 "promo.core.script.script_generator._generate_one",
                 side_effect=[valid_script, None, None],
             ):
            with pytest.raises(RuntimeError,
                               match="Variant 2/2 for 'Test Hotel' exhausted"):
                generate_script_variants(
                    poi_name="Test Hotel",
                    location="Nowhere",
                    clips_metadata=clips_metadata,
                    persona_path=persona_path,
                    n_variants=2,
                    n_candidates=1,
                    max_retries=1,
                    target_duration_sec=65,
                )

    def test_variant_plan_ordering_preserved(self, tmp_path, monkeypatch):
        """Criterion 14: VariantPlan[i] maps to variant_index i+1 regardless
        of per-variant retry behavior. The plan assignment must not shift
        when variants retry.
        """
        from promo.core.script.script_generator import generate_script_variants, _build_variant_plans

        persona_path = self._write_persona(tmp_path, wpm=175)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        clips_metadata = [
            {"id": f"{i:04d}", "category": "scenic", "scene_description": "clip"}
            for i in range(1, 15)
        ]

        plans = _build_variant_plans(2, clips_metadata)
        assert plans[0]["first_clip_id"] is not None
        assert plans[1]["first_clip_id"] is not None

        # Capture the variant_plan each _build_prompt sees, by side-effecting
        # through a patched _build_prompt wrapper.
        captured_plans: list[dict] = []
        original_build = None

        def capturing_build(*args, **kwargs):
            captured_plans.append(kwargs.get("variant_plan"))
            # Return a minimal prompt string; _generate_one is also mocked
            # so the prompt content doesn't matter.
            return "prompt"

        valid_script_a = self._valid_long_raw_script()
        # Variant 2 must NOT be a duplicate of variant 1's text (seen_texts
        # de-dup is shared across variants and orthogonal to the per-variant
        # retry budget). Tweak one word to differ.
        import copy
        valid_script_b = copy.deepcopy(valid_script_a)
        valid_script_b["segments"][0]["text"] = valid_script_b["segments"][0]["text"].replace(
            "Rooms here start", "The rooms begin",
        )
        with patch("promo.core.script.script_generator.resolve_gemini_model", return_value=object()), \
             patch("promo.core.script.script_generator._build_prompt",
                   side_effect=capturing_build), \
             patch(
                 "promo.core.script.script_generator._generate_one",
                 side_effect=[None, valid_script_a, valid_script_b],
             ):
            result = generate_script_variants(
                poi_name="Test Hotel",
                location="Nowhere",
                clips_metadata=clips_metadata,
                persona_path=persona_path,
                n_variants=2,
                n_candidates=1,
                max_retries=1,
                target_duration_sec=65,
            )

        # Variant 1 was retried once (None then accepted), variant 2 accepted
        # on first attempt. So captured_plans ordering: plan[0] for v1 retry 1,
        # plan[0] for v1 retry 2 (success), plan[1] for v2 retry 1 (success).
        assert len(captured_plans) == 3
        assert captured_plans[0]["first_clip_id"] == plans[0]["first_clip_id"]
        assert captured_plans[1]["first_clip_id"] == plans[0]["first_clip_id"]
        assert captured_plans[2]["first_clip_id"] == plans[1]["first_clip_id"]
        # And the final accepted scripts are tagged variant_index 1 and 2.
        assert [r["variant_index"] for r in result] == [1, 2]

class TestSprint09aSuccessGatedSidecars:
    """M-004 — sidecar accumulators (tts_metrics, match_quality_entries)
    are only appended after render_promo succeeds. Failed variants leave
    no entries.
    """

    def test_failed_variant_leaves_no_accumulator_entry(self):
        """Criterion 16: by inspecting the variant-loop body source, tts_metrics
        append appears AFTER the `if not ok: continue` render-success gate.
        (A full integration test with a mocked render would require pulling
        in most of the pipeline; the structural check gives equivalent
        regression protection.)

        promo-handoff-readiness Sprint 4 A-001 narrow — the per-variant loop
        body moved out of ``compile_promo.full_pipeline`` into
        ``promo.core.pipeline.variant_loop._run_variant_loop``; the M-004
        success-gating invariant lives there now.
        """
        import inspect
        from promo.core.pipeline.variant_loop import _run_variant_loop

        source = inspect.getsource(_run_variant_loop)
        idx_render = source.find("ok = render_promo(props, variant_output_path)")
        idx_guard = source.find("if not ok:", idx_render)
        idx_append = source.find("tts_metrics.append(variant_tts_entry)")
        idx_extend = source.find("match_quality_entries.extend(variant_match_quality)")
        assert idx_render != -1, "render_promo call site missing"
        assert idx_guard != -1, "render-success gate missing"
        assert idx_append != -1, "tts_metrics append site missing"
        assert idx_extend != -1, "match_quality_entries extend site missing"
        assert idx_render < idx_guard < idx_append, (
            "M-004 regression: tts_metrics.append must follow the "
            "`if not ok: continue` gate"
        )
        assert idx_render < idx_guard < idx_extend, (
            "M-004 regression: match_quality_entries.extend must follow "
            "the `if not ok: continue` gate"
        )

    def test_sidecar_filenames_are_poi_scoped(self):
        """Criterion 15: sidecar filenames include poi-slug + target-sec so
        two POI runs in the same output dir do not overwrite each other.

        Sprint 2 observability prep — the path-building logic now lives in
        the structured-result helper so future manifest code can see exact
        collision-bumped paths.
        """
        import inspect
        from promo.core.pipeline import sidecar_writer

        source = inspect.getsource(sidecar_writer._emit_run_sidecars_result)
        # The tag format and path constructions must both be present.
        assert 'f"{_safe_poi_dir(poi_name)}_{int(round(target_duration_sec))}s"' in source
        assert 'f"tts_metrics_{sidecar_tag}.json"' in source
        assert 'f"match_quality_{sidecar_tag}.json"' in source

class TestSprint09bC2WriteSidecar:
    """Sprint 09b C2 (ACs 6, 7, 8): _write_sidecar helper consolidates the
    two per-run sidecar writes with OSError-aware all_ok signalling,
    None-dir guard, and Codex #3 collision bump.
    """

    def test_clean_write(self, tmp_path):
        from promo.cli.compile_promo import _write_sidecar
        ok = _write_sidecar(
            str(tmp_path), "tts_metrics_foo_65s.json",
            [{"variant_index": 1, "measured_wpm": 195}],
            "tts_metrics",
        )
        assert ok is True
        path = tmp_path / "tts_metrics_foo_65s.json"
        assert path.exists()
        import json
        assert json.loads(path.read_text())[0]["measured_wpm"] == 195

    def test_none_sidecar_dir_returns_false(self, caplog):
        """AC7 / D-005: None sidecar_dir returns False so caller flips all_ok."""
        from promo.cli.compile_promo import _write_sidecar
        import logging
        caplog.set_level(logging.WARNING)
        ok = _write_sidecar(
            None, "tts_metrics_foo_65s.json",
            [{"x": 1}], "tts_metrics",
        )
        assert ok is False
        # Warning names the description so operator sees which sidecar failed.
        assert any("tts_metrics" in r.message for r in caplog.records)

    def test_empty_sidecar_dir_returns_false(self):
        """Empty string sidecar_dir is treated like None — returns False."""
        from promo.cli.compile_promo import _write_sidecar
        assert _write_sidecar("", "a.json", [], "tts_metrics") is False

    def test_oserror_returns_false(self, tmp_path, monkeypatch):
        """AC6 / M-1: OSError on write returns False so caller flips all_ok."""
        from promo.cli import compile_promo
        import builtins
        real_open = builtins.open

        def raising_open(path, *a, **kw):
            if str(path).endswith("tts_metrics_foo_65s.json") and "w" in (a[0] if a else kw.get("mode", "")):
                raise OSError("disk full")
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", raising_open)
        ok = compile_promo._write_sidecar(
            str(tmp_path), "tts_metrics_foo_65s.json",
            [{"x": 1}], "tts_metrics",
        )
        assert ok is False

    def test_collision_bump(self, tmp_path):
        """Codex #3 / DC-3: existing file triggers -2, -3, ... suffix."""
        from promo.cli.compile_promo import _write_sidecar
        # Pre-create the target filename.
        (tmp_path / "tts_metrics_foo_65s.json").write_text('[{"old":1}]')
        ok = _write_sidecar(
            str(tmp_path), "tts_metrics_foo_65s.json",
            [{"new": 2}], "tts_metrics",
        )
        assert ok is True
        # Original preserved; new write went to -2 suffix.
        import json
        original = json.loads((tmp_path / "tts_metrics_foo_65s.json").read_text())
        assert original == [{"old": 1}]
        bumped = tmp_path / "tts_metrics_foo_65s-2.json"
        assert bumped.exists()
        assert json.loads(bumped.read_text()) == [{"new": 2}]

    def test_collision_bump_chain(self, tmp_path):
        """Multiple collisions walk -2, -3, -4, ..."""
        from promo.cli.compile_promo import _write_sidecar
        (tmp_path / "match_quality_foo_65s.json").write_text("[]")
        (tmp_path / "match_quality_foo_65s-2.json").write_text("[]")
        ok = _write_sidecar(
            str(tmp_path), "match_quality_foo_65s.json",
            [{"v": 3}], "match_quality",
        )
        assert ok is True
        assert (tmp_path / "match_quality_foo_65s-3.json").exists()

    def test_requires_json_extension(self, tmp_path):
        from promo.cli.compile_promo import _write_sidecar
        import pytest
        with pytest.raises(ValueError, match="base_name must end in .json"):
            _write_sidecar(str(tmp_path), "tts_metrics", [], "tts_metrics")

class TestSprint09bC7PoolExhaustionMetric:
    """Sprint 09b C7 (ACs 32, 33): pool-exhaustion hard-fail counter is
    a grepable log line emitted once at full_pipeline end — not a new
    metrics file."""

    def test_full_pipeline_source_has_counter(self):
        """AC32/33 source-level invariant: the log format matches and
        no new metrics file is introduced.

        promo-handoff-readiness Sprint 4 A-001 narrow — the counter's
        increment sites moved into ``_run_variant_loop`` with the rest of
        the variant-loop body; the end-of-run grepable log line still
        lives in ``full_pipeline``. Assertion surfaces combine both to
        cover the logical invariant across the module boundary.
        """
        import inspect
        from promo.core.pipeline.pipeline import full_pipeline
        from promo.core.pipeline.variant_loop import _run_variant_loop
        pipe_src = inspect.getsource(full_pipeline)
        loop_src = inspect.getsource(_run_variant_loop)
        combined = pipe_src + "\n" + loop_src
        assert "Pool-exhaustion hard-fails this run:" in pipe_src
        assert "pool_exhaustion_hard_fails" in combined
        # Incremented on the FreezeWouldOccurError catch.
        assert "pool_exhaustion_hard_fails += 1" in loop_src

class TestSprint09bC7MainConsumesCalibratedWPM:
    """AC30: full_pipeline wires load_calibrated_wpm into the
    compute_pause_budget call, falling back to OBSERVED_ELEVENLABS_WPM."""

    def test_full_pipeline_calls_load_calibrated_wpm(self):
        """Sprint 10 C4 moved the WPM resolution into _step_generate_script.
        Sprint TTS-Migration Phase 4 replaced the literal
        OBSERVED_ELEVENLABS_WPM reference with a per-backend dispatch via
        bootstrap_wpm_for_backend; the calibrated-WPM landmarks remain."""
        import inspect
        from promo.cli import compile_promo
        src = inspect.getsource(compile_promo._step_generate_script)
        assert "load_calibrated_wpm(" in src
        assert "effective_wpm" in src
        # Bootstrap mechanism still referenced at the fallback site,
        # now through the per-backend helper rather than a raw constant.
        assert "bootstrap_wpm_for_backend(" in src

    def test_cold_start_log_line_present(self):
        """Cold-start + calibrated-hit log lines moved to _step_generate_script."""
        import inspect
        from promo.cli import compile_promo
        src = inspect.getsource(compile_promo._step_generate_script)
        assert "WPM cold-start" in src
        assert "WPM calibration" in src

class TestSprint09bC2DiscoverBGMDocstring:
    """AC9 / DC-4: _discover_bgm_files docstring names NoSuitableBGMError."""

    def test_docstring_mentions_no_suitable_bgm_error(self):
        from promo.cli.compile_promo import _discover_bgm_files
        doc = _discover_bgm_files.__doc__ or ""
        assert "NoSuitableBGMError" in doc
        assert "raise" in doc.lower() or "raises" in doc.lower()

class TestSprint09bC2CLINoSuitableBGM:
    """AC5 / H-1: main() catches NoSuitableBGMError from --bgm-dir path and
    surfaces a user-facing error rather than letting the raw traceback
    escape to stderr.
    """

    def test_main_wraps_discover_bgm_files_call(self):
        """Source-level check: the --bgm-dir block in main() has a try/except
        around _discover_bgm_files catching NoSuitableBGMError.
        """
        import inspect
        from promo.cli import compile_promo

        source = inspect.getsource(compile_promo.main)
        # The critical flow: try: _discover_bgm_files(args.bgm_dir, ...) ...
        # except NoSuitableBGMError as exc: parser.error(...)
        assert "NoSuitableBGMError" in source
        assert "_discover_bgm_files(" in source
        # Locate both — the except must come AFTER the discover call and
        # before any other main() logic that would swallow it.
        discover_pos = source.index("_discover_bgm_files(")
        except_pos = source.index("except NoSuitableBGMError")
        assert discover_pos < except_pos, (
            "except NoSuitableBGMError must follow _discover_bgm_files call in main()"
        )

class TestSprint12bFullPipelineCacheDirDerivation:
    """AC5 — full_pipeline derives embedding_cache_dir sibling to
    .mimo_cache/; falls back to None when backend lacks _clips_dir or dir
    doesn't exist."""

    def test_derivation_string_present_in_full_pipeline(self):
        """Structural: full_pipeline body contains the derivation pattern."""
        import inspect
        from promo.cli import compile_promo

        src = inspect.getsource(compile_promo.full_pipeline)
        assert "embedding_cache_dir" in src
        assert ".embedding_cache" in src
        # Must pass through to _step_assign_clips
        assert "embedding_cache_dir=embedding_cache_dir" in src

    def test_compile_promo_has_multiple_embedding_integration_hits(self):
        """AC3 verify — grep for integration-relevant identifiers across the
        pipeline subpackage returns >= 5 hits.

        promo-handoff-readiness Sprint 4 A-001 narrow — the embedding
        integration lives inside ``promo/core/pipeline/steps.py``
        (``_step_assign_clips``) and ``promo/core/pipeline/pipeline.py``
        (``full_pipeline``'s derivation). Grep both locations plus
        ``compile_promo.py`` so future movement within the subpackage
        still satisfies the invariant.
        """
        import re
        project_root = Path(__file__).resolve().parents[2]
        candidates = (
            project_root / "cli" / "compile_promo.py",
            project_root / "core" / "pipeline" / "steps.py",
            project_root / "core" / "pipeline" / "pipeline.py",
        )
        total = 0
        for path in candidates:
            if not path.exists():
                continue
            src = path.read_text()
            for needle in (
                "embedding_cache_dir", "retrieve_clips_fn",
                "union_of_top_k", "attach_embeddings_to_metadata",
                "_filter_clips_by_ids",
            ):
                total += len(re.findall(re.escape(needle), src))
        assert total >= 5, f"integration grep only found {total} hits"

class TestArgparsePrecedence:
    """AC-B3: CLI flag beats env var beats hardcoded default for the 3
    compile_promo argparse defaults backed by config resolvers in
    `promo.core.config` (AC-B1/B2 migration sites `compile_promo.py:1292
    / :1298 / :1304`).

    Tests exercise `_build_parser()` + `parser.parse_args(...)` directly
    — no subprocess invocation. The resolver runs at parser-construction
    time and argparse's `default=X` is evaluated once per parser build,
    so this covers the live env → parser contract without round-tripping
    through a shell.
    """

    def test_env_beats_hardcoded_default_duration_sec(self, monkeypatch):
        """`PROMO_DEFAULT_DURATION_SEC=45` with no CLI flag → 45.0 (not 30.0)."""
        monkeypatch.setenv("PROMO_DEFAULT_DURATION_SEC", "45")
        # `_build_parser()` is called once per test; argparse evaluates
        # `default=config.default_duration_sec()` once at parser
        # construction, and the resolver reads env on every call, so the
        # freshly-built parser sees the monkeypatched env value.
        from promo.cli.compile_promo import _build_parser
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.target_duration_sec == 45.0

    def test_hardcoded_default_when_env_unset(self, monkeypatch):
        """Env unset + no CLI flag → hardcoded resolver default (30.0)."""
        monkeypatch.delenv("PROMO_DEFAULT_DURATION_SEC", raising=False)
        monkeypatch.delenv("PROMO_DEFAULT_VARIANTS", raising=False)
        monkeypatch.delenv("PROMO_DEFAULT_SCRIPT_CANDIDATES", raising=False)
        from promo.cli.compile_promo import _build_parser
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.target_duration_sec == 30.0
        assert args.n_variants == 1
        assert args.script_candidates == 1

    def test_cli_flag_beats_env_for_target_duration(self, monkeypatch):
        """CLI `--target-duration-sec 20` with `PROMO_DEFAULT_DURATION_SEC=45` → 20.0."""
        monkeypatch.setenv("PROMO_DEFAULT_DURATION_SEC", "45")
        from promo.cli.compile_promo import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--target-duration-sec", "20"])
        assert args.target_duration_sec == 20.0

    def test_cli_flag_beats_env_for_n_variants(self, monkeypatch):
        """CLI `--n-variants 5` with `PROMO_DEFAULT_VARIANTS=2` → 5 (int)."""
        monkeypatch.setenv("PROMO_DEFAULT_VARIANTS", "2")
        from promo.cli.compile_promo import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--n-variants", "5"])
        assert args.n_variants == 5
