"""Unit tests for promo.core.render.remotion_renderer."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestP3DecoupledBinding:
    """P3 fix: remotion_renderer must NOT import from compiler.py (decoupled for Stage 7 deletion)."""

    def test_remotion_renderer_does_not_import_compiler(self):
        """remotion_renderer must be independent of compiler.py."""
        from promo.core.render import remotion_renderer
        import inspect
        source = inspect.getsource(remotion_renderer)
        assert "from promo.core.compiler" not in source, (
            "remotion_renderer imports compiler.py — must be self-contained"
        )


def test_render_promo_uses_configured_timeout(monkeypatch, tmp_path):
    from promo.core.render import remotion_renderer as rr

    output_path = tmp_path / "out.mp4"
    captured = {}

    def fake_run(*args, **kwargs):
        captured["cmd"] = args[0]
        captured["timeout"] = kwargs["timeout"]
        output_path.write_bytes(b"x" * 200_000)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("PROMO_RENDER_CONCURRENCY", "1")
    monkeypatch.setenv("PROMO_RENDER_TIMEOUT_SEC", "901")
    monkeypatch.setenv("PROMO_RENDER_X264_PRESET", "ultrafast")
    monkeypatch.setenv("PROMO_RENDER_CRF", "26")
    monkeypatch.setattr(rr, "validate_props", lambda props: [])
    monkeypatch.setattr(rr.subprocess, "run", fake_run)

    assert rr.render_promo(
        {"meta": {"poiName": "Timeout Test"}, "clips": [], "captions": {}},
        str(output_path),
    )
    assert captured["timeout"] == 901
    assert captured["cmd"][captured["cmd"].index("--concurrency") + 1] == "1"
    assert captured["cmd"][captured["cmd"].index("--x264-preset") + 1] == "ultrafast"
    assert captured["cmd"][captured["cmd"].index("--crf") + 1] == "26"


class TestSprint07PauseWindows:
    """AC12, AC13: audio.pauseWindows shape + filtering."""

    def _make_props(self, pause_values: list[int]):
        from promo.core.render.remotion_renderer import build_props_from_script

        n = len(pause_values)
        # Sprint 10 C5: fixture now passes a word-idx `assignments` list
        # straight through to build_props_from_script instead of embedding
        # a legacy `clips[]` array on each segment. One word per segment,
        # one phrase per segment, clip_id = segment index.
        segments = []
        word_timestamps = []
        segment_timestamps = []
        assignments: list[dict] = []
        t = 0.0
        for idx, pm in enumerate(pause_values):
            text = f"seg{idx}word"
            word_end = round(t + 0.3, 3)
            word_timestamps.append({"word": text, "start": round(t, 3), "end": word_end})
            segment_timestamps.append({
                "segment": idx + 1,
                "start": round(t, 3),
                "end": word_end,
                "duration": 0.3,
                "word_count": 1,
            })
            segments.append({
                "segment": idx + 1,
                "text": text,
                "pause_after_ms": pm,
            })
            assignments.append({
                "segment": idx + 1,
                "clip_id": f"{idx+1:04d}",
                "start_word_idx": idx,
                "end_word_idx": idx,
                "trim_start": 0.0,
                "display_span_sec": 0.3 + max(pm, 0) / 1000.0,
                "source_duration_sec": 5.0,
            })
            # Advance time past word + simulated pause
            t = word_end + 0.5 + max(pm, 0) / 1000.0

        clip_paths = {f"{i+1:04d}": f"/tmp/c{i}.mp4" for i in range(n)}
        narration_result = {
            "word_timestamps": word_timestamps,
            "segment_timestamps": segment_timestamps,
            "duration": t,
            "audio_path": "/tmp/narration.mp3",
        }
        with patch("promo.core.render.remotion_renderer.get_clip_duration", return_value=5.0):
            return build_props_from_script(
                poi_name="T", location="L",
                script_segments=segments, clip_paths=clip_paths,
                narration_result=narration_result,
                bgm_path="/tmp/b.mp3",
                assignments=assignments,
            )

    def test_filter_small_and_inter_segment_only(self):
        """pause_after_ms=[0, 200, 1500, 800, 0] -> 2 entries, correct afterSegmentIdx."""
        props = self._make_props([0, 200, 1500, 800, 0])
        pw = props["audio"]["pauseWindows"]
        assert len(pw) == 2
        indices = [w["afterSegmentIdx"] for w in pw]
        assert indices == [2, 3]

    def test_exact_shape_keys(self):
        """AC12: exactly four keys per entry."""
        props = self._make_props([1500, 0])
        pw = props["audio"]["pauseWindows"]
        assert len(pw) == 1
        assert set(pw[0].keys()) == {"startSec", "durationSec", "afterSegmentIdx", "strategy"}
        assert pw[0]["strategy"] is None
        assert isinstance(pw[0]["afterSegmentIdx"], int)

    def test_last_segment_pause_never_emitted(self):
        """Even if last segment has a large pause_after_ms, it's omitted."""
        props = self._make_props([0, 0, 5000])
        assert props["audio"]["pauseWindows"] == []

    def test_pause_windows_nested_in_audio(self):
        """pauseWindows lives inside the existing audio object."""
        props = self._make_props([1500, 0])
        assert "pauseWindows" in props["audio"]
        assert "pauseWindows" not in props

class TestP8OverlapValidation:
    """P8 fix: validate_props should not flag natural clip transitions as overlaps."""

    def test_sequential_clips_no_false_overlap(self):
        """Clips where videoStart == previous videoEnd should not be flagged."""
        from promo.core.render.remotion_renderer import validate_props

        props = {
            "meta": {"poiName": "Test", "location": "Here", "fps": 30, "width": 1080, "height": 1920},
            "clips": [
                {"clipId": "0001", "file": "http://example.com/a.mp4", "narration": "A",
                 "videoStart": 0.0, "videoEnd": 3.0, "trimStart": 0.0, "trimEnd": 3.0},
                {"clipId": "0002", "file": "http://example.com/b.mp4", "narration": "B",
                 "videoStart": 3.0, "videoEnd": 6.0, "trimStart": 0.0, "trimEnd": 3.0},
                {"clipId": "0003", "file": "http://example.com/c.mp4", "narration": "C",
                 "videoStart": 6.0, "videoEnd": 9.0, "trimStart": 0.0, "trimEnd": 3.0},
            ],
            "audio": {"narration": "http://x.com/n.wav", "bgm": "http://x.com/b.mp3"},
            "captions": {
                "wordTimestamps": [
                    {"word": w, "start": i * 0.5, "end": i * 0.5 + 0.3}
                    for i, w in enumerate("A B C D E F G H I".split())
                ],
            },
            "segments": [
                {"segment": 1, "text": "A B C", "startSec": 0.0, "endSec": 1.5},
                {"segment": 2, "text": "D E F", "startSec": 1.5, "endSec": 3.0},
                {"segment": 3, "text": "G H I", "startSec": 3.0, "endSec": 4.5},
            ],
        }

        errors = validate_props(props)
        overlap_errors = [e for e in errors if "overlap" in e.lower()]
        assert len(overlap_errors) == 0, f"False overlap errors: {overlap_errors}"

class TestBuildPropsFromScript:
    """Test the production Remotion props builder (AC5)."""

    def _make_narration_result(self, word_timestamps):
        """Helper: build a narration_result dict."""
        segments_ts = []
        if word_timestamps:
            segments_ts.append({
                "segment": 1, "start": word_timestamps[0]["start"],
                "end": word_timestamps[-1]["end"],
                "duration": word_timestamps[-1]["end"] - word_timestamps[0]["start"],
                "word_count": len(word_timestamps),
            })
        return {
            "word_timestamps": word_timestamps,
            "segment_timestamps": segments_ts,
            "duration": word_timestamps[-1]["end"] if word_timestamps else 0,
            "audio_path": "/tmp/narration.wav",
        }

    def test_basic_binding_with_word_idx_assignments(self):
        """Sprint 10 C5: assignments carry start_word_idx / end_word_idx
        instead of cut_after; narration text is reconstructed by slicing
        word_timestamps on those indices.
        """
        from promo.core.render.remotion_renderer import build_props_from_script

        segments = [
            {"segment": 1, "text": "First sentence. Second part here."},
        ]
        clip_paths = {"0001": "/tmp/c1.mp4", "0002": "/tmp/c2.mp4"}
        word_timestamps = [
            {"word": "First", "start": 0.0, "end": 0.2},
            {"word": "sentence.", "start": 0.2, "end": 0.5},
            {"word": "Second", "start": 0.6, "end": 0.8},
            {"word": "part", "start": 0.8, "end": 1.0},
            {"word": "here.", "start": 1.0, "end": 1.3},
        ]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 1,
             "trim_start": 0.0, "display_span_sec": 0.5,
             "source_duration_sec": 5.0},
            {"segment": 1, "clip_id": "0002",
             "start_word_idx": 2, "end_word_idx": 4,
             "trim_start": 0.0, "display_span_sec": 0.7,
             "source_duration_sec": 5.0},
        ]

        with patch("promo.core.render.remotion_renderer.get_clip_duration", return_value=5.0):
            props = build_props_from_script(
                poi_name="Test Hotel", location="Here",
                script_segments=segments, clip_paths=clip_paths,
                narration_result=self._make_narration_result(word_timestamps),
                bgm_path="/tmp/bgm.mp3",
                assignments=assignments,
            )

        assert len(props["clips"]) == 2
        assert props["clips"][0]["narration"] == "First sentence."
        assert props["clips"][1]["narration"] == "Second part here."

    def test_props_structure_has_required_keys(self):
        """Props dict must have all top-level keys Remotion expects."""
        from promo.core.render.remotion_renderer import build_props_from_script

        segments = [{"segment": 1, "text": "Hello world."}]
        clip_paths = {"0001": "/tmp/c.mp4"}
        wts = [
            {"word": "Hello", "start": 0.0, "end": 0.3},
            {"word": "world.", "start": 0.3, "end": 0.6},
        ]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 1,
             "trim_start": 0.0, "display_span_sec": 0.6,
             "source_duration_sec": 5.0},
        ]

        with patch("promo.core.render.remotion_renderer.get_clip_duration", return_value=5.0):
            props = build_props_from_script(
                poi_name="T", location="L",
                script_segments=segments, clip_paths=clip_paths,
                narration_result=self._make_narration_result(wts),
                bgm_path="/tmp/b.mp3",
                assignments=assignments,
            )

        for key in ("meta", "clips", "audio", "captions", "segments"):
            assert key in props, f"Missing required key: {key}"
        assert "wordTimestamps" in props["captions"]

    def test_single_segment_single_clip(self):
        """Edge case: one segment with one clip produces exactly 1 clip entry."""
        from promo.core.render.remotion_renderer import build_props_from_script

        segments = [{"segment": 1, "text": "One clip only."}]
        clip_paths = {"0001": "/tmp/c.mp4"}
        wts = [
            {"word": "One", "start": 0.0, "end": 0.2},
            {"word": "clip", "start": 0.2, "end": 0.4},
            {"word": "only.", "start": 0.4, "end": 0.6},
        ]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 2,
             "trim_start": 0.0, "display_span_sec": 0.6,
             "source_duration_sec": 5.0},
        ]

        with patch("promo.core.render.remotion_renderer.get_clip_duration", return_value=5.0):
            props = build_props_from_script(
                poi_name="T", location="L",
                script_segments=segments, clip_paths=clip_paths,
                narration_result=self._make_narration_result(wts),
                bgm_path="/tmp/b.mp3",
                assignments=assignments,
            )

        assert len(props["clips"]) == 1
        assert props["clips"][0]["narration"] == "One clip only."

class TestSprint08AudioMixShape:
    """Structural check — AudioMix source uses simple-span (no per-segment loop)."""

    def test_audiomix_has_no_per_segment_loop(self):
        """AudioMix.tsx should use first/last segment bounds, not iterate per-segment."""
        import os
        p = "promo/remotion/src/shared/AudioMix.tsx"
        assert os.path.exists(p)
        body = open(p).read()
        # The loop would contain `for (const seg of segments)` or similar.
        assert "for (const seg of segments)" not in body
        # And simple-span bounds use first/last segment references.
        assert "segments[0]" in body
        assert "segments[segments.length - 1]" in body

class TestSprint085AnchorLastFive:
    """Anchor startSec = max(0, video_duration - 5), durationSec = 5."""

    def test_anchor_on_long_video(self):
        from promo.core.render.remotion_renderer import build_props
        clips = [
            {
                "path": "/tmp/a.mp4", "clip_id": "0001",
                "trim_start": 0.0, "trim_end": 5.0, "video_start": 0.0,
                "narration": "x", "source_duration": 8.0,
            },
            {
                "path": "/tmp/b.mp4", "clip_id": "0002",
                "trim_start": 0.0, "trim_end": 8.0, "video_start": 5.0,
                "narration": "y", "source_duration": 8.0,
            },
        ]
        props = build_props(
            poi_name="Test", location="X",
            clip_assignments=clips,
            word_timestamps=[{"word": "hi", "start": 0.0, "end": 0.5}],
            segment_timestamps=[
                {"segment": 1, "start": 0.0, "end": 10.0, "duration": 10.0,
                 "word_count": 1},
            ],
            narration_path="/tmp/n.mp3",
            bgm_path="/tmp/bgm.mp3",
        )
        # Last clip videoEnd = 5 + (8-0) = 13. Anchor starts at 13 - 5 = 8.
        assert props["anchor"]["startSec"] == 8.0
        assert props["anchor"]["durationSec"] == 5.0

    def test_anchor_clamped_to_zero_for_short_video(self):
        from promo.core.render.remotion_renderer import build_props
        clips = [
            {
                "path": "/tmp/a.mp4", "clip_id": "0001",
                "trim_start": 0.0, "trim_end": 3.0, "video_start": 0.0,
                "narration": "x", "source_duration": 3.0,
            },
        ]
        props = build_props(
            poi_name="Test", location="X",
            clip_assignments=clips,
            word_timestamps=[{"word": "hi", "start": 0.0, "end": 0.5}],
            segment_timestamps=[
                {"segment": 1, "start": 0.0, "end": 3.0, "duration": 3.0,
                 "word_count": 1},
            ],
            narration_path="/tmp/n.mp3",
            bgm_path="/tmp/bgm.mp3",
        )
        # 3 - 5 = -2 → clamped to 0.
        assert props["anchor"]["startSec"] == 0.0
        assert props["anchor"]["durationSec"] == 5.0

class TestSprint10C5BindClipsSignature:
    """C5 criterion 1(b): _bind_clips_to_narration signature exposes the
    word-idx ``assignments`` parameter, not the legacy script-segments path.
    """

    def test_signature_lists_assignments_first(self):
        import inspect
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        sig = inspect.signature(_bind_clips_to_narration)
        params = list(sig.parameters)
        assert params[0] == "assignments", (
            "_bind_clips_to_narration must take `assignments` as its first parameter"
        )
        assert "script_segments" not in params, (
            "C5 retires the `script_segments`-based binding path"
        )
        assert "clips_metadata" not in params, (
            "C5 retires motion-phase sub-windowing (Gemini #2 owns trim_start)"
        )
        assert "variant_index" not in params, (
            "C5 retires per-variant sub-windowing kwarg"
        )

    def test_build_props_from_script_takes_assignments_kwarg(self):
        import inspect
        from promo.core.render.remotion_renderer import build_props_from_script
        sig = inspect.signature(build_props_from_script)
        assert "assignments" in sig.parameters, (
            "build_props_from_script must accept an `assignments` kwarg so "
            "full_pipeline can thread clip_assigner output through."
        )

class TestSprint10C5RetiredHelpers:
    """C5 criterion 2 verify (a): retired helpers absent from source.
    The grep-level invariant keeps the rewire from being quietly undone."""

    def test_resolve_cut_after_gone(self):
        import inspect
        from promo.core.render import remotion_renderer as rr
        src = inspect.getsource(rr)
        assert "def _resolve_cut_after" not in src
        assert not hasattr(rr, "_resolve_cut_after")

    def test_compute_subwindow_offset_gone(self):
        import inspect
        from promo.core.render import remotion_renderer as rr
        src = inspect.getsource(rr)
        assert "def _compute_subwindow_offset" not in src
        assert not hasattr(rr, "_compute_subwindow_offset")

    def test_reconstruct_segment_text_gone(self):
        import inspect
        from promo.core.render import remotion_renderer as rr
        src = inspect.getsource(rr)
        assert "def _reconstruct_segment_text" not in src
        assert not hasattr(rr, "_reconstruct_segment_text")

    def test_normalize_word_gone(self):
        # Orphaned alongside _resolve_cut_after — its only caller.
        import inspect
        from promo.core.render import remotion_renderer as rr
        src = inspect.getsource(rr)
        assert "def _normalize_word" not in src
        assert not hasattr(rr, "_normalize_word")

    def test_cut_after_not_in_renderer_source(self):
        """C5 criterion 1(a): grep ``cut_after`` in ``remotion_renderer.py``
        — zero matches."""
        import inspect
        from promo.core.render import remotion_renderer as rr
        src = inspect.getsource(rr)
        assert "cut_after" not in src

    def test_cut_after_not_in_compile_promo_source(self):
        """C5 criterion 3 verify: grep ``cut_after`` in ``compile_promo.py``
        — zero matches."""
        import inspect
        from promo.cli import compile_promo
        src = inspect.getsource(compile_promo)
        assert "cut_after" not in src

class TestSprint10C5BindHappyPath:
    """C5 criterion 1(c): exercising a happy-path assignments list produces
    the expected renderer entries shape."""

    def test_word_idx_binding_emits_expected_renderer_entries(self):
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        word_timestamps = [
            {"word": "Hello", "start": 0.00, "end": 0.30},
            {"word": "world.", "start": 0.30, "end": 0.70},
            {"word": "Again.", "start": 0.90, "end": 1.20},
        ]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 1,
             "trim_start": 1.5, "display_span_sec": 0.7,
             "source_duration_sec": 8.0},
            {"segment": 2, "clip_id": "0002",
             "start_word_idx": 2, "end_word_idx": 2,
             "trim_start": 0.0, "display_span_sec": 0.3,
             "source_duration_sec": 4.0},
        ]
        clip_paths = {"0001": "/tmp/a.mp4", "0002": "/tmp/b.mp4"}
        entries = _bind_clips_to_narration(
            assignments, clip_paths, word_timestamps,
        )
        assert len(entries) == 2
        # First entry picks up Gemini #2's trim_start verbatim.
        assert entries[0]["clip_id"] == "0001"
        assert entries[0]["trim_start"] == 1.5
        assert entries[0]["narration"] == "Hello world."
        assert entries[0]["video_start"] == 0.0
        # source_duration flows from the assignment, not re-ffprobed.
        assert entries[0]["source_duration"] == 8.0
        # Second entry's narration is reconstructed from the remaining word.
        assert entries[1]["clip_id"] == "0002"
        assert entries[1]["narration"] == "Again."

    def test_last_clip_receives_tail_within_source(self):
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        word_timestamps = [
            {"word": "One", "start": 0.00, "end": 0.25},
            {"word": "two.", "start": 0.25, "end": 0.60},
        ]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 1,
             "trim_start": 0.0, "display_span_sec": 0.6,
             "source_duration_sec": 10.0},
        ]
        entries = _bind_clips_to_narration(
            assignments, {"0001": "/tmp/a.mp4"}, word_timestamps,
        )
        # Trim_end = trim_start + span (0.6) + tail (0.3) = 0.9 when source has room.
        assert entries[0]["trim_end"] > 0.6
        assert entries[0]["trim_end"] <= 10.0

    def test_empty_assignments_returns_empty(self):
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        assert _bind_clips_to_narration([], {}, [{"word": "x", "start": 0, "end": 1}]) == []

    def test_empty_word_timestamps_returns_empty(self):
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        assigns = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 0,
             "trim_start": 0.0, "display_span_sec": 0.3,
             "source_duration_sec": 5.0},
        ]
        assert _bind_clips_to_narration(assigns, {"0001": "/tmp/a.mp4"}, []) == []

class TestSprint10C5BindDefensiveSkips:
    """Defensive skips for bad input shapes — C5 keeps warnings, not raises,
    because the upstream hard-constraint enforcement in clip_assigner owns
    the Gemini-#2-violation path."""

    def test_invalid_word_indices_skipped(self, caplog):
        import logging
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        word_timestamps = [
            {"word": "Hi", "start": 0.0, "end": 0.2},
        ]
        assignments = [
            # end_idx out of range — skipped.
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 99,
             "trim_start": 0.0, "display_span_sec": 0.2,
             "source_duration_sec": 5.0},
        ]
        caplog.set_level(logging.WARNING)
        entries = _bind_clips_to_narration(
            assignments, {"0001": "/tmp/a.mp4"}, word_timestamps,
        )
        assert entries == []
        assert any("invalid word indices" in r.message for r in caplog.records)

    def test_missing_clip_path_skipped(self, caplog):
        import logging
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        word_timestamps = [
            {"word": "Hi", "start": 0.0, "end": 0.2},
        ]
        assignments = [
            {"segment": 1, "clip_id": "9999",
             "start_word_idx": 0, "end_word_idx": 0,
             "trim_start": 0.0, "display_span_sec": 0.2,
             "source_duration_sec": 5.0},
        ]
        caplog.set_level(logging.WARNING)
        entries = _bind_clips_to_narration(
            assignments, {"0001": "/tmp/a.mp4"}, word_timestamps,
        )
        assert entries == []
        assert any("missing from clip_paths" in r.message for r in caplog.records)

class TestSprint10C5SegmentTextFromScriptSegments:
    """C5 criterion 2: segment text in props now comes from script_segments
    rather than being reconstructed from word_timestamps + time windows."""

    def test_build_props_reads_text_by_segment_number(self):
        from promo.core.render.remotion_renderer import build_props
        clip_assignments = [
            {"path": "/tmp/a.mp4", "clip_id": "0001",
             "trim_start": 0.0, "trim_end": 2.0,
             "video_start": 0.0, "narration": "ignored",
             "source_duration": 5.0},
        ]
        word_timestamps = [
            {"word": "anything", "start": 0.0, "end": 0.5},
        ]
        segment_timestamps = [
            {"segment": 1, "start": 0.0, "end": 2.0, "duration": 2.0},
        ]
        script_segments = [
            {"segment": 1, "text": "CANONICAL segment one copy."},
        ]
        props = build_props(
            poi_name="T", location="L",
            clip_assignments=clip_assignments,
            word_timestamps=word_timestamps,
            segment_timestamps=segment_timestamps,
            narration_path="/tmp/n.mp3",
            bgm_path="/tmp/b.mp3",
            script_segments=script_segments,
        )
        # Text comes straight from script_segments, not reconstructed from words.
        assert props["segments"][0]["text"] == "CANONICAL segment one copy."

class TestSprint10C5TailExtensionRegressionGuard:
    """Audit L-001 regression guard: the tail extension must apply to the
    last *renderer* entry, not the last *input* assignment. When any
    input assignment is skipped (invalid indices or missing path), the
    pre-fix code walked ``i == n - 1`` against the input list and
    silently dropped the tail if the last input was the one skipped.
    """

    def test_tail_applies_when_last_input_is_skipped(self):
        from promo.core.render.remotion_renderer import _bind_clips_to_narration
        word_timestamps = [{"word": "Solo.", "start": 0.0, "end": 0.5}]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 0,
             "trim_start": 0.0, "display_span_sec": 0.5,
             "source_duration_sec": 10.0},
            # Second assignment references a nonexistent clip_id — skipped.
            # Before the fix, the tail guard (``i == n - 1``) never fired
            # on the actual rendered entry.
            {"segment": 2, "clip_id": "9999",
             "start_word_idx": 0, "end_word_idx": 0,
             "trim_start": 0.0, "display_span_sec": 0.3,
             "source_duration_sec": 5.0},
        ]
        entries = _bind_clips_to_narration(
            assignments, {"0001": "/tmp/a.mp4"}, word_timestamps,
        )
        assert len(entries) == 1, "second (invalid) assignment must be skipped"
        # trim_end = 0.5 span + 0.3 tail = 0.8 when source has the room.
        assert entries[0]["trim_end"] > 0.5
