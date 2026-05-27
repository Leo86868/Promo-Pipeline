"""Unit tests for promo.core.assign.clip_assigner."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

_C2_WORD_DUR_SEC = 0.2

_C2_INTER_SEGMENT_SILENCE_SEC = 0.5

def _make_c2_script(n_segments: int = 2) -> dict:
    """Minimal script fixture for clip_assigner tests — 2 segments by
    default, 5 words each (text.split() == 5 so the C4 L-003 tiling
    check computes segment_word_ranges correctly).

    ``target_duration_sec`` is set to the narration-end implied by
    :func:`_make_c2_word_timestamps` (``n_segments * words_per_seg *
    _C2_WORD_DUR_SEC + (n_segments - 1) * _C2_INTER_SEGMENT_SILENCE_SEC``)
    so the Sprint 10.5 peek-ahead enforcer's
    ``final_display_end = max(target_duration_sec, narration_end)``
    resolves to the narration end. Unit tests then assert phrase spans
    without needing to factor in a target-vs-narration extension.
    """
    words_per_seg = 5
    narration_end = (
        n_segments * words_per_seg * _C2_WORD_DUR_SEC
        + max(0, n_segments - 1) * _C2_INTER_SEGMENT_SILENCE_SEC
    )
    segments = []
    for i in range(n_segments):
        seg_num = i + 1
        # Text must have exactly 5 whitespace-split tokens so the
        # L-003 tiling check (based on text.split()) matches the
        # 5-per-segment word_timestamps fixture.
        seg = {
            "segment": seg_num,
            "text": f"seg{seg_num} alpha bravo charlie delta",
            "word_count": 5,
            "pause_after_ms": int(_C2_INTER_SEGMENT_SILENCE_SEC * 1000) if i < n_segments - 1 else 0,
        }
        if i < n_segments - 1:
            seg["pause_weight"] = 2
        segments.append(seg)
    return {
        "segments": segments,
        "poi_name": "Test Hotel",
        "location": "Nowhere",
        "target_duration_sec": round(narration_end, 4),
        "format_mode": "long",
    }

def _make_c2_word_timestamps(n_segments: int = 2, words_per_seg: int = 5) -> list[dict]:
    """Produce word_timestamps that match the production TTS-delivered
    shape: uniform ``_C2_WORD_DUR_SEC`` per word, with
    ``_C2_INTER_SEGMENT_SILENCE_SEC`` gap injected between segments.
    That silence gap is what the Sprint 10.5 peek-ahead enforcer reads
    when computing a segment-last phrase's span — next segment's first
    word's ``start`` lands after the authored pause in the delivered
    timeline.
    """
    word_ts: list[dict] = []
    t = 0.0
    for seg_i in range(n_segments):
        if seg_i > 0:
            t += _C2_INTER_SEGMENT_SILENCE_SEC
        for _ in range(words_per_seg):
            word_ts.append({"word": "w", "start": round(t, 4), "end": round(t + _C2_WORD_DUR_SEC, 4)})
            t += _C2_WORD_DUR_SEC
    return word_ts

class TestSprint10C2ClipAssignmentError:
    """C2: new ClipAssignmentError class in promo.core.errors carries the
    segment + phrase indices the F3 retry hint reads back."""

    def test_error_class_exists_and_exposes_attributes(self):
        from promo.core.errors import ClipAssignmentError

        exc = ClipAssignmentError(
            segment_index=2, phrase_index=1,
            required_span=3.0, actual_max_usable=1.5,
            clip_id="0042",
        )
        assert isinstance(exc, RuntimeError)
        assert exc.segment_index == 2
        assert exc.phrase_index == 1
        assert exc.required_span == 3.0
        assert exc.actual_max_usable == 1.5
        assert exc.clip_id == "0042"
        # Message names the segment + phrase + clip so F3 hints are accurate.
        assert "segment 2" in str(exc)
        assert "phrase 1" in str(exc)
        assert "0042" in str(exc)

class TestSprint10C2AssignClipsHardConstraint:
    """C2 criterion 5: source_dur - trim_start ≥ display_span (TOL=0.05s)."""

    def test_accepts_compliant_assignment(self):
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        # Two phrases total — one per segment. Clip 0001 covers seg 1
        # phrase (words 0-4 + 0.5s trailing silence); clip 0002 covers
        # seg 2 phrase (words 5-9, last segment so no trailing silence).
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        clip_durations = {"0001": 2.0, "0002": 2.0}
        pause_after_ms = [500, 0]
        out = _enforce_hard_constraint_and_enrich(
            raw, script, word_ts, clip_durations,
        )
        assert len(out) == 2
        # Seg 1 last phrase: spoken 5*0.2=1.0 + 0.5s silence = 1.5s span.
        assert abs(out[0]["display_span_sec"] - 1.5) < 0.01
        # Seg 2 last phrase: spoken 1.0s, no trailing silence.
        assert abs(out[1]["display_span_sec"] - 1.0) < 0.01
        assert out[0]["source_duration_sec"] == 2.0
        assert out[1]["source_duration_sec"] == 2.0

    def test_raises_on_span_overflow_with_correct_indices(self):
        """The violating segment + phrase indices + clip_id + arithmetic
        appear on the raised exception so build_tighten_hint can quote
        them back to Gemini #1."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        # Phrase 3 (seg 2, words 8-9) is the last phrase; under C1.2
        # its span = narration_end − word[8].start = 2.5 − 2.1 = 0.4s.
        # Clip 0003 is made 0.3s long → usable (0.3) + TOL (0.05) = 0.35
        # < 0.4s required span → ClipAssignmentError fires with the
        # segment_index, phrase_index, and clip_id of the violation so
        # build_tighten_hint can quote them back to Gemini #1.
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 7, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0003", "start_word_idx": 8,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        clip_durations = {"0001": 2.0, "0002": 2.0, "0003": 0.3}
        pause_after_ms = [500, 0]

        with pytest.raises(ClipAssignmentError) as excinfo:
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, clip_durations,
            )
        exc = excinfo.value
        assert exc.segment_index == 2
        assert exc.phrase_index == 2
        assert exc.clip_id == "0003"
        assert exc.required_span > exc.actual_max_usable

    def test_raises_on_duplicate_clip_id(self):
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0001", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},  # reused
        ]
        clip_durations = {"0001": 2.0}
        pause_after_ms = [500, 0]
        with pytest.raises(ClipAssignmentError, match="duplicate"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, clip_durations,
            )

    def test_raises_on_missing_clip_from_inventory(self):
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "9999", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},  # not in inventory
        ]
        clip_durations = {"0001": 2.0, "0002": 2.0}
        with pytest.raises(ClipAssignmentError, match="missing from inventory"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, clip_durations,
            )

    def test_raises_on_out_of_range_word_indices(self):
        """A phrase whose end_word_idx exceeds both the segment boundary
        and the word_timestamps length raises. Under C4 audit-followup
        the L-003 tiling check fires first with a "tiling" message;
        either error message is acceptable — the invariant is that this
        malformed response does not silently pass."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 50, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        with pytest.raises(ClipAssignmentError):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, {"0001": 2.0, "0002": 2.0},
            )

    def test_raises_when_gemini2_skips_a_segment(self):
        """L-001 audit fix: Gemini #2 response missing a script segment
        must raise (not silently produce a truncated assignments list)."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        # Response covers segment 1 only; segment 2 is missing entirely.
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
        ]
        with pytest.raises(ClipAssignmentError, match="no assignment for segments"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, {"0001": 2.0, "0002": 2.0},
            )

    def test_raises_when_phrases_leave_gap_inside_segment(self):
        """L-003 audit fix: phrases must tile their segment end-to-end
        with no gaps. A response where phrase 2 starts AFTER phrase 1
        ends + 1 raises."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        # Segment 1 has words 0-4. Phrase 1 covers 0-1 (words 0,1); phrase 2
        # covers 3-4 (leaving word 2 with no clip — a gap).
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 1, "trim_start": 0.0},
            {"segment": 1, "clip_id": "0002", "start_word_idx": 3,
             "end_word_idx": 4, "trim_start": 0.0},  # gap at word 2
            {"segment": 2, "clip_id": "0003", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        with pytest.raises(ClipAssignmentError, match="expected start_word_idx"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts,
                {"0001": 2.0, "0002": 2.0, "0003": 2.0},
            )

    def test_raises_when_phrases_overlap_inside_segment(self):
        """L-003 audit fix: phrases must not overlap — phrase 2 starting
        before phrase 1 ends raises."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        # Phrase 2 starts at 2 but phrase 1 ended at 3 — overlap on word 2,3.
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 3, "trim_start": 0.0},
            {"segment": 1, "clip_id": "0002", "start_word_idx": 2,
             "end_word_idx": 4, "trim_start": 0.0},  # overlap
            {"segment": 2, "clip_id": "0003", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        with pytest.raises(ClipAssignmentError, match="expected start_word_idx"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts,
                {"0001": 2.0, "0002": 2.0, "0003": 2.0},
            )

    def test_raises_when_last_phrase_undershoots_segment(self):
        """L-003 audit fix: last phrase of a segment must end at the
        segment's last word."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        # Segment 2's last phrase stops at word 7 — segment ends at word 9.
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 7, "trim_start": 0.0},  # undershoots
        ]
        with pytest.raises(ClipAssignmentError, match="tiling"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, {"0001": 2.0, "0002": 2.0},
            )

    def test_raises_on_negative_trim_start(self):
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": -0.5},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        with pytest.raises(ClipAssignmentError):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, {"0001": 2.0, "0002": 2.0},
            )

class TestSprint10C2AssignClipsPublicAPI:
    """C2 criterion 4: module exists + public API surface + schema."""

    def test_module_exposes_public_surface(self):
        """assign_clips, assign_clips_with_f3_retry, build_tighten_hint,
        and ClipAssignmentError are the module's public surface."""
        from promo.core.assign import clip_assigner

        assert callable(getattr(clip_assigner, "assign_clips", None))
        assert callable(getattr(clip_assigner, "assign_clips_with_f3_retry", None))
        assert callable(getattr(clip_assigner, "build_tighten_hint", None))
        # ClipAssignmentError is re-exported via the errors module; the
        # assigner imports it by name, so the symbol must be reachable.
        from promo.core.errors import ClipAssignmentError  # noqa: F401

    def test_assign_clips_happy_path_produces_contract_schema(self, monkeypatch):
        """Patch _call_gemini2 with a compliant stub; assign_clips enriches
        each phrase with display_span_sec + source_duration_sec and
        returns the contract schema."""
        from promo.core.assign import clip_assigner

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        clips_metadata = [
            {"id": "0001", "category": "scenic", "scene_description": "a",
             "source_duration_sec": 2.0},
            {"id": "0002", "category": "scenic", "scene_description": "b",
             "source_duration_sec": 2.0},
        ]
        clip_durations = {"0001": 2.0, "0002": 2.0}

        stub_response = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        monkeypatch.setattr(clip_assigner, "_call_gemini2", lambda _p: stub_response)

        out = clip_assigner.assign_clips(
            script, word_ts, [500, 0],
            clips_metadata, clip_durations, variant_index=1,
        )
        assert len(out) == 2
        for entry in out:
            for key in (
                "segment", "clip_id", "start_word_idx", "end_word_idx",
                "trim_start", "display_span_sec", "source_duration_sec",
            ):
                assert key in entry, f"assign_clips entry missing '{key}'"

class TestSprint10C2ParseGemini2Json:
    """C2: _parse_gemini2_json handles the real-world shapes Gemini emits —
    top-level lists, dict-wrapped lists, fenced output — distinct from the
    dict-only common.ai_utils.parse_json_response helper."""

    def test_parses_top_level_list(self):
        from promo.core.assign.clip_assigner import _parse_gemini2_json

        out = _parse_gemini2_json('[{"segment": 1, "clip_id": "0001", "start_word_idx": 0, "end_word_idx": 4, "trim_start": 0.0}]')
        assert isinstance(out, list)
        assert out[0]["clip_id"] == "0001"

    def test_parses_fenced_json(self):
        from promo.core.assign.clip_assigner import _parse_gemini2_json

        wrapped = '```json\n[{"segment": 1, "clip_id": "0001", "start_word_idx": 0, "end_word_idx": 4, "trim_start": 0.0}]\n```'
        out = _parse_gemini2_json(wrapped)
        assert len(out) == 1

    def test_unwraps_dict_with_assignments_key(self):
        from promo.core.assign.clip_assigner import _parse_gemini2_json

        wrapped = '{"assignments": [{"segment": 1, "clip_id": "0001", "start_word_idx": 0, "end_word_idx": 4, "trim_start": 0.0}]}'
        out = _parse_gemini2_json(wrapped)
        assert len(out) == 1

    def test_raises_on_malformed_json(self):
        from promo.core.assign.clip_assigner import _parse_gemini2_json

        with pytest.raises(ValueError, match="JSON parse failure"):
            _parse_gemini2_json("not json at all")

    def test_raises_on_unrecognized_dict_shape(self):
        from promo.core.assign.clip_assigner import _parse_gemini2_json

        with pytest.raises(ValueError, match="without a recognizable list key"):
            _parse_gemini2_json('{"something_else": [{"x": 1}]}')

class TestSprint10C2BuildTightenHint:
    """C2: build_tighten_hint produces a parseable, stable hint string."""

    def test_hint_names_segment_and_arithmetic(self):
        from promo.core.assign.clip_assigner import build_tighten_hint
        from promo.core.errors import ClipAssignmentError

        exc = ClipAssignmentError(
            segment_index=3, phrase_index=1,
            required_span=4.25, actual_max_usable=2.10,
            clip_id="0007",
        )
        hint = build_tighten_hint(exc)
        assert "Segment 3" in hint
        assert "4.25" in hint  # required_span
        assert "2.10" in hint  # usable
        assert "tighten segment 3" in hint.lower()

class TestSprint10C2F3RetryPolicy:
    """C2 criterion 6: F3 single-retry. Tests exercise
    assign_clips_with_f3_retry in isolation — the Gemini #2 API is never
    hit because we patch assign_clips directly; the F3 policy is the
    unit under test."""

    def _fake_clips(self):
        return [
            {"id": "0001", "category": "scenic", "source_duration_sec": 2.0},
            {"id": "0002", "category": "scenic", "source_duration_sec": 2.0},
        ]

    def _fake_narration(self):
        return {
            "word_timestamps": _make_c2_word_timestamps(),
            "segment_timestamps": [],
        }

    def test_happy_path_single_call_no_retry(self, monkeypatch):
        """First call succeeds → no retry. regenerate_* never invoked."""
        from promo.core.assign import clip_assigner

        calls = {"assign": 0, "regen_script": 0, "regen_narration": 0}

        def _fake_assign(script, wt, st, pa, cm, cd, variant_index=1):
            calls["assign"] += 1
            return [{"segment": 1, "clip_id": "0001",
                     "start_word_idx": 0, "end_word_idx": 4,
                     "trim_start": 0.0, "display_span_sec": 1.0,
                     "source_duration_sec": 2.0}]

        def _regen_script(hint):
            calls["regen_script"] += 1
            return _make_c2_script()

        def _regen_narration(new_script):
            calls["regen_narration"] += 1
            return self._fake_narration()

        monkeypatch.setattr(clip_assigner, "assign_clips", _fake_assign)

        out_script, out_narration, assignments = clip_assigner.assign_clips_with_f3_retry(
            _make_c2_script(), self._fake_narration(),
            self._fake_clips(), {"0001": 2.0, "0002": 2.0},
            variant_index=1,
            regenerate_script_fn=_regen_script,
            regenerate_narration_fn=_regen_narration,
        )
        assert calls == {"assign": 1, "regen_script": 0, "regen_narration": 0}
        assert len(assignments) == 1

    def test_retry_succeeds_on_second_attempt(self, monkeypatch):
        """Criterion 6(a): first Gemini #2 raises; regen + second call
        succeeds → returns successfully with retry artifacts."""
        from promo.core.assign import clip_assigner
        from promo.core.errors import ClipAssignmentError

        calls = {"assign": 0, "regen_script": 0, "regen_narration": 0}
        hints: list[str] = []

        successful_assignments = [
            {"segment": 1, "clip_id": "0002",
             "start_word_idx": 0, "end_word_idx": 4,
             "trim_start": 0.0, "display_span_sec": 1.0,
             "source_duration_sec": 2.0},
        ]

        def _fake_assign(script, wt, st, pa, cm, cd, variant_index=1):
            calls["assign"] += 1
            if calls["assign"] == 1:
                raise ClipAssignmentError(
                    segment_index=1, phrase_index=1,
                    required_span=3.0, actual_max_usable=1.0,
                    clip_id="0001",
                )
            return successful_assignments

        def _regen_script(hint):
            calls["regen_script"] += 1
            hints.append(hint)
            new = _make_c2_script()
            new["_retry_marker"] = True
            return new

        def _regen_narration(new_script):
            calls["regen_narration"] += 1
            nar = self._fake_narration()
            nar["_retry_marker"] = True
            return nar

        monkeypatch.setattr(clip_assigner, "assign_clips", _fake_assign)

        final_script, final_narration, assignments = clip_assigner.assign_clips_with_f3_retry(
            _make_c2_script(), self._fake_narration(),
            self._fake_clips(), {"0001": 2.0, "0002": 2.0},
            variant_index=1,
            regenerate_script_fn=_regen_script,
            regenerate_narration_fn=_regen_narration,
        )
        assert calls["assign"] == 2
        assert calls["regen_script"] == 1
        assert calls["regen_narration"] == 1
        assert final_script.get("_retry_marker") is True
        assert final_narration.get("_retry_marker") is True
        assert assignments == successful_assignments
        # Hint carries the failing segment index.
        assert hints and "Segment 1" in hints[0]

    def test_retry_propagates_on_second_failure_with_no_third_call(self, monkeypatch):
        """Criterion 6(b) + 6(c): both calls raise → propagates; exactly
        two calls, no third."""
        from promo.core.assign import clip_assigner
        from promo.core.errors import ClipAssignmentError

        calls = {"assign": 0}

        def _fake_assign(script, wt, st, pa, cm, cd, variant_index=1):
            calls["assign"] += 1
            raise ClipAssignmentError(
                segment_index=calls["assign"], phrase_index=1,
                required_span=3.0, actual_max_usable=1.0,
                clip_id=f"call{calls['assign']}",
            )

        def _regen_script(hint):
            return _make_c2_script()

        def _regen_narration(new_script):
            return self._fake_narration()

        monkeypatch.setattr(clip_assigner, "assign_clips", _fake_assign)

        with pytest.raises(ClipAssignmentError) as excinfo:
            clip_assigner.assign_clips_with_f3_retry(
                _make_c2_script(), self._fake_narration(),
                self._fake_clips(), {"0001": 2.0, "0002": 2.0},
                variant_index=1,
                regenerate_script_fn=_regen_script,
                regenerate_narration_fn=_regen_narration,
            )
        # The second call's error (not the first) propagates.
        assert excinfo.value.clip_id == "call2"
        assert calls["assign"] == 2  # Exactly two — NEVER three.

    def test_no_retry_when_callables_not_supplied(self, monkeypatch):
        """If neither regenerate fn is supplied, first raise propagates
        immediately — assign_clips called exactly once."""
        from promo.core.assign import clip_assigner
        from promo.core.errors import ClipAssignmentError

        calls = {"assign": 0}

        def _fake_assign(script, wt, st, pa, cm, cd, variant_index=1):
            calls["assign"] += 1
            raise ClipAssignmentError(
                segment_index=1, phrase_index=1,
                required_span=3.0, actual_max_usable=1.0,
                clip_id="0001",
            )

        monkeypatch.setattr(clip_assigner, "assign_clips", _fake_assign)

        with pytest.raises(ClipAssignmentError):
            clip_assigner.assign_clips_with_f3_retry(
                _make_c2_script(), self._fake_narration(),
                self._fake_clips(), {"0001": 2.0, "0002": 2.0},
                variant_index=1,
                regenerate_script_fn=None,
                regenerate_narration_fn=None,
            )
        assert calls["assign"] == 1

class TestSprint10C3SidecarWriter:
    """C3 criterion 7: clip_assignments_{slug}_{dur}s.json written via
    _write_sidecar; payload is per-variant list; success-gated.
    """

    def test_full_pipeline_writes_clip_assignments_sidecar(self):
        """Running full_pipeline once produces a clip_assignments sidecar
        whose payload lists the rendered variants. Running a second time
        in the same dir produces a collision-bumped -2.json without
        clobbering the first."""
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
                "segments": [{"segment": 1, "text": "Variant one text.",
                              "clips": [{"clip_id": "0001", "cut_after": ""}]}],
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
            "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.8}],
            "audio_path": "/tmp/narration.wav",
        }
        props = {
            "meta": {"poiName": "Test Hotel", "location": "X", "fps": 30, "width": 1080, "height": 1920},
            "clips": [{"clipId": "0001", "file": "clip.mp4", "narration": "x", "videoStart": 0.0, "videoEnd": 4.0, "trimStart": 0.0, "trimEnd": 4.0}],
            "audio": {"narration": "narration.wav", "bgm": "bgm.mp3"},
            "captions": {"wordTimestamps": narration["word_timestamps"]},
            "segments": [{"segment": 1, "text": "x", "startSec": 0.0, "endSec": 0.8}],
        }

        def _fake_f3_retry(script, narration_in, clips_metadata, clip_durations, **kwargs):
            return script, narration_in, [
                {"segment": 1, "clip_id": "0001",
                 "start_word_idx": 0, "end_word_idx": 1,
                 "trim_start": 0.0, "display_span_sec": 0.8,
                 "source_duration_sec": 5.0},
            ]

        def _run_once(tmpdir):
            backend.output_dir.return_value = tmpdir
            backend.clips_dir.return_value = None
            output_path = os.path.join(tmpdir, "promo_test.mp4")
            with patch("promo.core.pipeline.steps.analyze_clips_for_script",
                       return_value=[{"id": f"{i:04d}"} for i in range(1, 15)]), \
                 patch("promo.core.script.script_generator.generate_script_variants", return_value=scripts), \
                 patch("promo.core.narrate.tts_engine.generate_narration", return_value=narration), \
                 patch("promo.core.assign.clip_assigner.assign_clips_with_f3_retry", side_effect=_fake_f3_retry), \
                 patch("promo.core.pipeline.variant_loop.build_props_from_script", return_value=props), \
                 patch("promo.core.pipeline.variant_loop.validate_props", return_value=[]), \
                 patch("promo.core.pipeline.variant_loop.stage_media"), \
                 patch("promo.core.pipeline.variant_loop.render_promo",
                       side_effect=lambda _props, out: Path(out).write_bytes(b"video") or True):
                ok = full_pipeline(
                    poi_name="Test Hotel",
                    location="Nowhere",
                    output_path=output_path,
                    backend=backend,
                    target_duration_sec=65,
                    n_variants=1,
                    script_candidates=1,
                )
            return ok

        with tempfile.TemporaryDirectory() as tmpdir:
            assert _run_once(tmpdir) is True
            first_path = os.path.join(tmpdir, "clip_assignments_test_hotel_65s.json")
            assert os.path.exists(first_path), "first run must land the base sidecar"
            payload = json.loads(open(first_path).read())
            # Sprint 13 AC19 (D-004): sidecar is now a dict with retrieval
            # provenance at the top level + a `variants` key.
            assert isinstance(payload, dict)
            for key in (
                "retrieval_active", "embedded_pool_size", "reduced_pool_size",
                "mimo_prompt_sha1", "fallback_reason", "retrieval_contract",
                "variants",
            ):
                assert key in payload, f"missing top-level key {key!r}"
            variants = payload["variants"]
            assert isinstance(variants, list)
            assert len(variants) == 1
            assert variants[0]["variant_index"] == 1
            assert variants[0]["variant_status"] == "rendered"
            assert isinstance(variants[0]["assignments"], list)
            # Retrieval was inactive (no embedding_cache_dir — MagicMock
            # backend's clips_dir() returns None).
            assert payload["retrieval_active"] is False
            assert payload["embedded_pool_size"] == 0
            assert payload["mimo_prompt_sha1"] is None
            assert payload["fallback_reason"] is None
            # Sprint 18 F AC3: retrieval_contract == "soft_hint" on every run
            # (seeded by _empty_retrieval_provenance, NOT mutated downstream).
            assert payload["retrieval_contract"] == "soft_hint"

            # Second run in the SAME output dir — collision bump produces -2.json.
            assert _run_once(tmpdir) is True
            bumped_path = os.path.join(tmpdir, "clip_assignments_test_hotel_65s-2.json")
            assert os.path.exists(bumped_path), (
                "collision-bump must produce clip_assignments_..._-2.json "
                "when the base name already exists"
            )
            # Base file untouched.
            first_mtime_after = os.path.getmtime(first_path)
            assert first_mtime_after < os.path.getmtime(bumped_path)

    def test_aborted_variant_absent_from_sidecar(self):
        """Success-gating: a variant that fails F3 retry + renders nothing
        does NOT appear in the clip_assignments sidecar. Mirrors 09a M-004."""
        from promo.cli.compile_promo import full_pipeline
        from promo.core.errors import ClipAssignmentError

        backend = MagicMock()
        backend.fetch_clips.return_value = {
            f"{i:04d}": f"/tmp/clip_{i:04d}.mp4" for i in range(1, 15)
        }
        backend.fetch_bgm.return_value = "/tmp/bgm.mp3"
        backend.save_output.side_effect = lambda poi_name, path: path

        scripts = [
            {"variant_index": 1,
             "segments": [{"segment": 1, "text": "V1.",
                           "clips": [{"clip_id": "0001", "cut_after": ""}]}],
             "total_words": 1, "format_mode": "long", "target_duration_sec": 65},
            {"variant_index": 2,
             "segments": [{"segment": 1, "text": "V2.",
                           "clips": [{"clip_id": "0002", "cut_after": ""}]}],
             "total_words": 1, "format_mode": "long", "target_duration_sec": 65},
        ]
        narration = {
            "duration": 4.2,
            "word_timestamps": [{"word": "x", "start": 0.0, "end": 0.4}],
            "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.4}],
            "audio_path": "/tmp/narration.wav",
        }
        props = {
            "meta": {"poiName": "T", "location": "X", "fps": 30, "width": 1080, "height": 1920},
            "clips": [{"clipId": "0001", "file": "c.mp4", "narration": "x", "videoStart": 0.0, "videoEnd": 4.0, "trimStart": 0.0, "trimEnd": 4.0}],
            "audio": {"narration": "narration.wav", "bgm": "bgm.mp3"},
            "captions": {"wordTimestamps": narration["word_timestamps"]},
            "segments": [{"segment": 1, "text": "x", "startSec": 0.0, "endSec": 0.4}],
        }

        call_state = {"n": 0}

        def _fake_f3_retry(script, narration_in, clips_metadata, clip_durations, **kwargs):
            call_state["n"] += 1
            if call_state["n"] == 2:
                # Variant 2 aborts (post-retry failure).
                raise ClipAssignmentError(
                    segment_index=1, phrase_index=1,
                    required_span=3.0, actual_max_usable=1.0,
                    clip_id="0002",
                )
            return script, narration_in, [
                {"segment": 1, "clip_id": "0001",
                 "start_word_idx": 0, "end_word_idx": 0,
                 "trim_start": 0.0, "display_span_sec": 0.4,
                 "source_duration_sec": 5.0},
            ]

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch("promo.core.pipeline.steps.analyze_clips_for_script",
                   return_value=[{"id": f"{i:04d}"} for i in range(1, 15)]), \
             patch("promo.core.script.script_generator.generate_script_variants", return_value=scripts), \
             patch("promo.core.narrate.tts_engine.generate_narration", return_value=narration), \
             patch("promo.core.assign.clip_assigner.assign_clips_with_f3_retry", side_effect=_fake_f3_retry), \
             patch("promo.core.pipeline.variant_loop.build_props_from_script", return_value=props), \
             patch("promo.core.pipeline.variant_loop.validate_props", return_value=[]), \
             patch("promo.core.pipeline.variant_loop.stage_media"), \
             patch("promo.core.pipeline.variant_loop.render_promo",
                   side_effect=lambda _props, out: Path(out).write_bytes(b"video") or True):
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
                script_candidates=1,
            )
            assert ok is False  # variant 2 aborted → run not all_ok
            sidecar_path = os.path.join(tmpdir, "clip_assignments_test_hotel_65s.json")
            assert os.path.exists(sidecar_path)
            payload = json.loads(open(sidecar_path).read())
            # Sprint 13 AC19: new dict-shaped sidecar; variants list is
            # still the success-gated accumulator.
            assert isinstance(payload, dict)
            variants = payload["variants"]
            assert len(variants) == 1
            assert variants[0]["variant_index"] == 1

class TestSprint10C3LoadLatestClipAssignments:
    """C3 criterion 8: load_latest_clip_assignments globs base + bumped,
    mtime-sorts, skips malformed, returns the most recent valid payload.
    """

    def test_returns_none_when_no_sidecar_exists(self, tmp_path):
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        assert load_latest_clip_assignments("test_hotel", 65.0, [str(tmp_path)]) is None

    def test_reads_base_sidecar(self, tmp_path):
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        payload = [{"variant_index": 1, "variant_status": "rendered",
                    "assignments": [{"segment": 1, "clip_id": "0001"}]}]
        (tmp_path / "clip_assignments_test_hotel_65s.json").write_text(json.dumps(payload))

        out = load_latest_clip_assignments("test_hotel", 65.0, [str(tmp_path)])
        assert out == payload

    def test_returns_most_recent_bumped_variant(self, tmp_path):
        """Given base + multiple bumped sidecars, returns mtime-latest."""
        import time
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        base = [{"variant_index": 1, "variant_status": "rendered",
                 "assignments": [{"segment": 1, "clip_id": "0001"}]}]
        bumped2 = [{"variant_index": 1, "variant_status": "rendered",
                    "assignments": [{"segment": 1, "clip_id": "0002"}]}]
        bumped3 = [{"variant_index": 1, "variant_status": "rendered",
                    "assignments": [{"segment": 1, "clip_id": "0003"}]}]

        base_path = tmp_path / "clip_assignments_test_hotel_65s.json"
        bumped2_path = tmp_path / "clip_assignments_test_hotel_65s-2.json"
        bumped3_path = tmp_path / "clip_assignments_test_hotel_65s-3.json"
        base_path.write_text(json.dumps(base))
        time.sleep(0.01)
        bumped2_path.write_text(json.dumps(bumped2))
        time.sleep(0.01)
        bumped3_path.write_text(json.dumps(bumped3))

        out = load_latest_clip_assignments("test_hotel", 65.0, [str(tmp_path)])
        # -3 is mtime-latest.
        assert out is not None and out[0]["assignments"][0]["clip_id"] == "0003"

    def test_skips_malformed_and_falls_back_to_next(self, tmp_path):
        """Corrupt sidecars are skipped; reader returns the next valid one."""
        import time
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        valid = [{"variant_index": 1, "variant_status": "rendered",
                  "assignments": [{"segment": 1, "clip_id": "0001"}]}]
        base_path = tmp_path / "clip_assignments_test_hotel_65s.json"
        bumped_path = tmp_path / "clip_assignments_test_hotel_65s-2.json"
        base_path.write_text(json.dumps(valid))
        time.sleep(0.01)
        # Bumped is corrupt JSON — reader must fall back to base.
        bumped_path.write_text("}}}not json{{{")

        out = load_latest_clip_assignments("test_hotel", 65.0, [str(tmp_path)])
        assert out == valid

    def test_skips_non_list_payload(self, tmp_path):
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        (tmp_path / "clip_assignments_test_hotel_65s.json").write_text(
            json.dumps({"not": "a list"})
        )
        assert load_latest_clip_assignments("test_hotel", 65.0, [str(tmp_path)]) is None

    def test_skips_entry_missing_assignments_key(self, tmp_path):
        """Schema check: every payload entry must be a dict with an
        'assignments' list. Otherwise reader treats the file as malformed."""
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        bad = [{"variant_index": 1, "variant_status": "rendered"}]  # no assignments
        (tmp_path / "clip_assignments_test_hotel_65s.json").write_text(json.dumps(bad))
        assert load_latest_clip_assignments("test_hotel", 65.0, [str(tmp_path)]) is None

    def test_searches_multiple_dirs(self, tmp_path):
        """Given ``sidecar_search_dirs`` with multiple entries, reader
        finds the most-recent-mtime sidecar across ALL dirs (not just
        the first)."""
        import time
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        payload_a = [{"variant_index": 1, "variant_status": "rendered",
                      "assignments": [{"segment": 1, "clip_id": "AAAA"}]}]
        payload_b = [{"variant_index": 1, "variant_status": "rendered",
                      "assignments": [{"segment": 1, "clip_id": "BBBB"}]}]
        (dir_a / "clip_assignments_x_65s.json").write_text(json.dumps(payload_a))
        time.sleep(0.01)
        (dir_b / "clip_assignments_x_65s-2.json").write_text(json.dumps(payload_b))

        out = load_latest_clip_assignments(
            "x", 65.0, [str(dir_a), str(dir_b)],
        )
        assert out is not None and out[0]["assignments"][0]["clip_id"] == "BBBB"

class TestSprint13ClipAssignmentsProvenance:
    """AC19 (D-004): clip_assignments sidecar schema extended with
    retrieval provenance; reader tolerates both old (bare list) and new
    (dict-with-variants-key) shapes.
    """

    def test_new_shape_persists_provenance_on_retrieval_inactive_run(
        self, tmp_path,
    ):
        """Run with no embedding_cache_dir threaded → top-level fields
        describe "retrieval inactive": retrieval_active=False,
        embedded_pool_size=0, reduced_pool_size==full pool, sha1=null,
        fallback_reason=null."""
        from promo.cli.compile_promo import full_pipeline

        backend = MagicMock()
        backend.fetch_clips.return_value = {
            f"{i:04d}": f"/tmp/clip_{i:04d}.mp4" for i in range(1, 15)
        }
        backend.fetch_bgm.return_value = "/tmp/bgm.mp3"
        backend.save_output.side_effect = lambda poi_name, path: path

        scripts = [{
            "variant_index": 1,
            "segments": [{"segment": 1, "text": "hi.",
                          "clips": [{"clip_id": "0001", "cut_after": ""}]}],
            "total_words": 1, "format_mode": "long", "target_duration_sec": 65,
        }]
        narration = {
            "duration": 4.2,
            "word_timestamps": [{"word": "hi.", "start": 0.0, "end": 0.4}],
            "segment_timestamps": [{"segment": 1, "start": 0.0, "end": 0.4}],
            "audio_path": "/tmp/narration.wav",
        }
        props = {
            "meta": {"poiName": "T", "location": "X", "fps": 30, "width": 1080, "height": 1920},
            "clips": [{"clipId": "0001", "file": "c.mp4", "narration": "x", "videoStart": 0.0, "videoEnd": 4.0, "trimStart": 0.0, "trimEnd": 4.0}],
            "audio": {"narration": "narration.wav", "bgm": "bgm.mp3"},
            "captions": {"wordTimestamps": narration["word_timestamps"]},
            "segments": [{"segment": 1, "text": "x", "startSec": 0.0, "endSec": 0.4}],
        }

        def fake_f3(script, narration_in, clips_metadata, clip_durations, **kwargs):
            return script, narration_in, [
                {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
                 "end_word_idx": 0, "trim_start": 0.0, "display_span_sec": 0.4,
                 "source_duration_sec": 5.0},
            ]

        with tempfile.TemporaryDirectory() as tmpdir:
            backend.output_dir.return_value = tmpdir
            backend.clips_dir.return_value = None  # retrieval disabled
            output_path = os.path.join(tmpdir, "promo_test.mp4")
            with patch("promo.core.pipeline.steps.analyze_clips_for_script",
                       return_value=[{"id": f"{i:04d}"} for i in range(1, 15)]), \
                 patch("promo.core.script.script_generator.generate_script_variants",
                       return_value=scripts), \
                 patch("promo.core.narrate.tts_engine.generate_narration", return_value=narration), \
                 patch("promo.core.assign.clip_assigner.assign_clips_with_f3_retry",
                       side_effect=fake_f3), \
                 patch("promo.core.pipeline.variant_loop.build_props_from_script",
                       return_value=props), \
                 patch("promo.core.pipeline.variant_loop.validate_props", return_value=[]), \
                 patch("promo.core.pipeline.variant_loop.stage_media"), \
                 patch("promo.core.pipeline.variant_loop.render_promo",
                       side_effect=lambda _p, out: Path(out).write_bytes(b"v") or True):
                assert full_pipeline(
                    poi_name="Test Hotel", location="Nowhere",
                    output_path=output_path, backend=backend,
                    target_duration_sec=65, n_variants=1, script_candidates=1,
                ) is True

            payload = json.loads(
                open(os.path.join(tmpdir, "clip_assignments_test_hotel_65s.json")).read()
            )
            assert payload["retrieval_active"] is False
            assert payload["embedded_pool_size"] == 0
            assert payload["reduced_pool_size"] == 14  # full pool
            assert payload["mimo_prompt_sha1"] is None
            assert payload["fallback_reason"] is None

    def test_step_assign_clips_populates_provenance_on_active_retrieval(
        self, tmp_path, monkeypatch,
    ):
        """Unit test at the _step_assign_clips layer: wiring a sidecar
        (with a valid mimo_prompt_sha1) flips retrieval_active=True and
        records the sha1 + pool sizes. fallback_reason stays null on the
        happy path."""
        import numpy as np
        from promo.cli import compile_promo
        from promo.core.assign import clip_embedder

        clips_metadata = [
            {"id": "a", "scene_description": "pool", "category": "pool"},
            {"id": "b", "scene_description": "room", "category": "room"},
            {"id": "c", "scene_description": "lobby", "category": "lobby"},
        ]
        vecs = np.eye(3, 1536).tolist()
        sha1 = clip_embedder.current_mimo_prompt_sha1()
        sidecar = {
            "embeddings": {
                "a": {"vector": vecs[0], "input": "pool | pool"},
                "b": {"vector": vecs[1], "input": "room | room"},
                "c": {"vector": vecs[2], "input": "lobby | lobby"},
            },
            "model": "text-embedding-3-small",
            "dim": 1536,
            "mimo_prompt_sha1": sha1,
            "composition_version": 1,
        }
        sp = clip_embedder.sidecar_path(str(tmp_path), sha1, 1)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w") as f:
            json.dump(sidecar, f)

        # Two segments, each aligned with a different clip's vector, so
        # union_of_top_k emits 2 clips and reduces from 3 → 2 cleanly.
        monkeypatch.setattr(
            "promo.core.assign.clip_embedder.embed_texts",
            lambda qs: [vecs[i % len(vecs)] for i, _ in enumerate(qs)],
        )

        captured: dict = {}

        def fake_f3_retry(script, narration, clips_metadata, clip_durations,
                          *, variant_index, regenerate_script_fn,
                          regenerate_narration_fn, retrieve_clips_fn=None):
            if retrieve_clips_fn is not None:
                captured["reduced"] = retrieve_clips_fn(script)
            return script, narration, []

        monkeypatch.setattr(
            "promo.core.assign.clip_assigner.assign_clips_with_f3_retry", fake_f3_retry,
        )

        script = {"segments": [{"text": "about the pool"},
                               {"text": "about the room"}]}
        narration = {"word_timestamps": [], "segment_timestamps": []}

        result = compile_promo._step_assign_clips(
            script, narration, clips_metadata, {}, 1,
            poi_name="X", location="", hotel_description="",
            notable_details="", variant_voice_key="jarnathan",
            variant_tmp_dir="/tmp", tts_speed=1.0,
            target_duration_sec=30.0, effective_wpm=165,
            n_variants_total=1, script_candidates=1,
            embedding_cache_dir=str(tmp_path),
        )
        _final_script, _final_narr, _assigns, provenance = result
        assert provenance["retrieval_active"] is True
        assert provenance["embedded_pool_size"] == 3
        assert provenance["mimo_prompt_sha1"] == sha1
        # fallback_reason is null on the happy path.
        assert provenance["fallback_reason"] is None
        # reduced_pool_size reflects the union (2) or equals full pool
        # if the union_of_top_k path happened to return all 3.
        assert 1 <= provenance["reduced_pool_size"] <= 3

    def test_m4_shrinkage_warning_fires_once_even_on_f3_retry(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Sprint 13 post-audit D-006: `m4_attach_shrinkage` is a static
        property of the loaded sidecar, so it holds on both the initial
        Gemini #2 attempt AND the F3 retry. The unified WARNING must fire
        ONCE per variant — duplicating it defeats the D-002 consolidation
        intent by making operators double-count dropped clips."""
        import numpy as np
        from promo.cli import compile_promo
        from promo.core.assign import clip_embedder

        sha1 = clip_embedder.current_mimo_prompt_sha1()
        # Sidecar only has 2 of the 3 clips → attach shrinkage fallback fires.
        sidecar = {
            "embeddings": {
                "a": {"vector": (np.eye(1, 1536)[0]).tolist(), "input": "a"},
                "b": {"vector": (np.eye(2, 1536)[1]).tolist(), "input": "b"},
            },
            "model": "text-embedding-3-small",
            "dim": 1536,
            "mimo_prompt_sha1": sha1,
            "composition_version": 1,
        }
        sp = clip_embedder.sidecar_path(str(tmp_path), sha1, 1)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w") as f:
            json.dump(sidecar, f)

        clips_metadata = [
            {"id": "a", "scene_description": "a", "category": "x"},
            {"id": "b", "scene_description": "b", "category": "x"},
            {"id": "c", "scene_description": "c", "category": "x"},  # missing in sidecar
        ]

        def fake_f3_retry(script, narration, clips_metadata, clip_durations,
                          *, variant_index, regenerate_script_fn,
                          regenerate_narration_fn, retrieve_clips_fn=None):
            # Simulate BOTH the initial Gemini #2 attempt AND the F3 retry by
            # calling the retrieval closure twice — this is what the real
            # assign_clips_with_f3_retry does after a ClipAssignmentError.
            if retrieve_clips_fn is not None:
                retrieve_clips_fn(script)  # initial attempt
                retrieve_clips_fn(script)  # F3 retry against regenerated script
            return script, narration, []

        monkeypatch.setattr(
            "promo.core.assign.clip_assigner.assign_clips_with_f3_retry", fake_f3_retry,
        )

        script = {"segments": [{"text": "q1"}]}
        narration = {"word_timestamps": [], "segment_timestamps": []}

        with caplog.at_level("WARNING"):
            compile_promo._step_assign_clips(
                script, narration, clips_metadata, {}, 1,
                poi_name="X", location="", hotel_description="",
                notable_details="", variant_voice_key="jarnathan",
                variant_tmp_dir="/tmp", tts_speed=1.0,
                target_duration_sec=30.0, effective_wpm=165,
                n_variants_total=1, script_candidates=1,
                embedding_cache_dir=str(tmp_path),
            )

        shrinkage_warnings = [
            r for r in caplog.records
            if "m4_attach_shrinkage" in r.message
        ]
        assert len(shrinkage_warnings) == 1, (
            f"expected ONE m4_attach_shrinkage WARNING; got "
            f"{len(shrinkage_warnings)}: {[r.message for r in shrinkage_warnings]}"
        )

    def test_retrieval_closure_exception_sets_fallback_reason(
        self, tmp_path, monkeypatch,
    ):
        """Sprint 13 post-audit L-001: if the retrieval closure raises an
        unexpected exception, the swallowing wrap in
        assign_clips_with_f3_retry falls back to the full pool (design
        intent). retrieval_provenance must record
        fallback_reason='retrieval_exception' so the sidecar does not
        falsely advertise a clean retrieval."""
        from promo.cli import compile_promo
        from promo.core.assign import clip_embedder

        sha1 = clip_embedder.current_mimo_prompt_sha1()
        sidecar = {
            "embeddings": {
                "a": {"vector": [1.0] * 1536, "input": "a"},
                "b": {"vector": [0.0] * 1536, "input": "b"},
            },
            "model": "text-embedding-3-small",
            "dim": 1536,
            "mimo_prompt_sha1": sha1,
            "composition_version": 1,
        }
        sp = clip_embedder.sidecar_path(str(tmp_path), sha1, 1)
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w") as f:
            json.dump(sidecar, f)

        clips_metadata = [
            {"id": "a", "scene_description": "a", "category": "x"},
            {"id": "b", "scene_description": "b", "category": "x"},
        ]

        # Force union_of_top_k to raise — simulates OpenRouter transient blip
        # at embedding-query time.
        def _boom(*args, **kwargs):
            raise ValueError("simulated retrieval-layer failure")
        monkeypatch.setattr(
            "promo.core.assign.clip_retriever.union_of_top_k", _boom,
        )

        def fake_f3_retry(script, narration, clips_metadata, clip_durations,
                          *, variant_index, regenerate_script_fn,
                          regenerate_narration_fn, retrieve_clips_fn=None):
            # Mirror the real assign_clips_with_f3_retry._retrieved() defensive
            # wrap: ANY exception from the closure falls back to full pool so
            # the variant still runs (Sprint 12b L-001). Our retrieval_exception
            # provenance update happens before the re-raise that this wrap swallows.
            if retrieve_clips_fn is not None:
                try:
                    retrieve_clips_fn(script)
                except Exception:
                    pass
            return script, narration, []

        monkeypatch.setattr(
            "promo.core.assign.clip_assigner.assign_clips_with_f3_retry", fake_f3_retry,
        )

        script = {"segments": [{"text": "q1"}]}
        narration = {"word_timestamps": [], "segment_timestamps": []}

        _s, _n, _a, provenance = compile_promo._step_assign_clips(
            script, narration, clips_metadata, {}, 1,
            poi_name="X", location="", hotel_description="",
            notable_details="", variant_voice_key="jarnathan",
            variant_tmp_dir="/tmp", tts_speed=1.0,
            target_duration_sec=30.0, effective_wpm=165,
            n_variants_total=1, script_candidates=1,
            embedding_cache_dir=str(tmp_path),
        )
        assert provenance["retrieval_active"] is True
        assert provenance["fallback_reason"] == "retrieval_exception"
        assert provenance["reduced_pool_size"] == len(clips_metadata)

    def test_load_latest_tolerates_old_bare_list_shape(self, tmp_path):
        """Backward-read-compat: a Sprint 12b sidecar (bare variants list)
        still loads. The reader returns the variants list either way."""
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        old_payload = [
            {"variant_index": 1, "variant_status": "rendered",
             "assignments": [{"segment": 1, "clip_id": "0001"}]},
        ]
        (tmp_path / "clip_assignments_legacy_65s.json").write_text(
            json.dumps(old_payload),
        )
        out = load_latest_clip_assignments("legacy", 65.0, [str(tmp_path)])
        assert out == old_payload

    def test_load_latest_reads_new_dict_shape(self, tmp_path):
        """Forward-read: the new wrapped shape unwraps to the variants list."""
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        new_payload = {
            "retrieval_active": True,
            "embedded_pool_size": 14,
            "reduced_pool_size": 7,
            "mimo_prompt_sha1": "abcdef12",
            "fallback_reason": None,
            "variants": [
                {"variant_index": 1, "variant_status": "rendered",
                 "assignments": [{"segment": 1, "clip_id": "0001"}]},
            ],
        }
        (tmp_path / "clip_assignments_new_65s.json").write_text(
            json.dumps(new_payload),
        )
        out = load_latest_clip_assignments("new", 65.0, [str(tmp_path)])
        assert out == new_payload["variants"]

    def test_load_latest_rejects_dict_without_variants_key(self, tmp_path):
        """Malformed dict (no variants) is treated the same as a bare
        non-list payload — skipped, returns None."""
        from promo.core.assign.clip_assigner import load_latest_clip_assignments

        (tmp_path / "clip_assignments_broken_65s.json").write_text(
            json.dumps({"retrieval_active": True, "variants": "not a list"}),
        )
        assert load_latest_clip_assignments("broken", 65.0, [str(tmp_path)]) is None

    def test_write_sidecar_log_reports_variants_count_for_dict_payload(
        self, tmp_path, caplog,
    ):
        """Sprint 13 post-audit D-003: _write_sidecar's "(N entries)" log
        counts `payload["variants"]` when payload is the AC19 dict shape.
        Pre-fix: always reported len(dict)=6 regardless of variant count."""
        from promo.cli.compile_promo import _write_sidecar

        payload = {
            "retrieval_active": True,
            "embedded_pool_size": 14,
            "reduced_pool_size": 7,
            "mimo_prompt_sha1": "abcd1234",
            "fallback_reason": None,
            "variants": [
                {"variant_index": 1, "variant_status": "rendered", "assignments": []},
                {"variant_index": 2, "variant_status": "rendered", "assignments": []},
            ],
        }
        with caplog.at_level("INFO"):
            ok = _write_sidecar(str(tmp_path), "clip_assignments_x_30s.json",
                                payload, "clip_assignments")
        assert ok is True
        wrote_records = [r for r in caplog.records if "Wrote" in r.message]
        assert any("(2 entries)" in r.message for r in wrote_records), (
            f"expected '(2 entries)' in log; got {[r.message for r in wrote_records]}"
        )

class TestSprint10C3F1Invariant:
    """C3 criterion 9: the F1 invariant — every new sidecar writer has a
    matching discovery reader whose glob covers collision-bumped
    variants. Enforced by grep tests on the two modules.
    """

    def test_writer_exists_in_compile_promo(self):
        """Exactly ONE _write_sidecar() call whose base_name starts with
        'clip_assignments_' — no accidental second writer in Sprint 10.

        promo-handoff-readiness Sprint 4 A-001 narrow — the three per-run
        sidecar writes moved into ``promo.core.pipeline.sidecar_writer.
        _emit_run_sidecars`` (the Sprint 16 helper pulled out of
        ``full_pipeline``). The F1 invariant ("one writer per sidecar")
        still holds; the check now grep the new source location.
        """
        import inspect
        from promo.core.pipeline import sidecar_writer

        source = inspect.getsource(sidecar_writer)
        # Look for the literal base_name prefix inside a structured sidecar
        # write call.
        # Simple heuristic: count occurrences of the literal.
        call_signatures = re.findall(
            r"_write_sidecar_result\s*\([^)]*f\"clip_assignments_[^\"]*\"[^)]*\)",
            source,
            flags=re.DOTALL,
        )
        assert len(call_signatures) == 1, (
            f"Expected exactly one _write_sidecar call with a "
            f"'clip_assignments_' base_name; found {len(call_signatures)}"
        )

    def test_reader_glob_covers_collision_bump(self):
        """The reader's glob expression must match BOTH the base filename
        and the collision-bumped '-*.json' variants.
        """
        import inspect
        from promo.core.assign import clip_assigner

        source = inspect.getsource(clip_assigner.load_latest_clip_assignments)
        # Must reference the base form and the bumped-suffix glob pattern.
        assert 'f"clip_assignments_{poi_slug}_{target_dur}s-*.json"' in source or \
               "-*.json" in source, (
            "load_latest_clip_assignments glob must cover the collision-bump "
            "suffix pattern '-*.json'"
        )
        assert "poi_slug" in source and "target_dur" in source

    def test_reader_mirrors_load_calibrated_wpm_shape(self):
        """The 09b L-002 fix in pause_budget.load_calibrated_wpm is the
        template. Both readers accept ``sidecar_search_dirs`` + glob over
        base + bumped + mtime-sort + graceful-None on parse failure.
        """
        import inspect
        from promo.core.assign import clip_assigner
        from promo.core.script.pause_budget import load_calibrated_wpm

        a_sig = inspect.signature(clip_assigner.load_latest_clip_assignments)
        b_sig = inspect.signature(load_calibrated_wpm)
        # F1 invariant: the three base parameters are identical in the
        # same order. Optional tail parameters (Sprint TTS-Migration
        # added ``backend`` to load_calibrated_wpm for per-backend
        # calibration scoping) don't violate the shared-shape invariant.
        base_shape = ["poi_slug", "duration_sec", "sidecar_search_dirs"]
        assert list(a_sig.parameters.keys())[:3] == base_shape
        assert list(b_sig.parameters.keys())[:3] == base_shape

class TestSprint10bAuditFixDedupNormalization:
    """Logic-auditor L-002 + Codex re-review: ``clip_assigner._enforce_hard_constraint_and_enrich``
    must normalize ``clip_id`` before BOTH dedup AND inventory/duration
    lookup. Production ``clip_durations`` is keyed by the zero-padded
    form that ``compile_promo._step_prepare_clips`` produces, so an
    unpadded Gemini emission must still resolve in the inventory
    (the renderer already handles both forms by calling ``.zfill(4)``).
    """

    def test_padded_vs_unpadded_clip_id_collision_raises(self):
        """Gemini emits ``"7"`` for seg 1 and ``"0007"`` for seg 2 —
        same physical clip under two surface representations. Dedup
        must raise on the second. ``clip_durations`` uses the
        CANONICAL padded form only, mirroring production shape.
        """
        import pytest
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich
        from promo.core.errors import ClipAssignmentError

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "7", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0007", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        # Only the canonical padded form is present — matches
        # compile_promo._step_prepare_clips' output.
        clip_durations = {"0007": 2.0}
        pause_after_ms = [500, 0]
        with pytest.raises(ClipAssignmentError, match="duplicate"):
            _enforce_hard_constraint_and_enrich(
                raw, script, word_ts, clip_durations,
            )

    def test_unpadded_clip_id_resolves_against_padded_inventory(self):
        """Gemini emits unpadded ``"7"`` as a single phrase's clip; the
        inventory only has the padded ``"0007"``. The enforcer must
        normalize via zfill(4) before the inventory lookup, not raise
        'missing from inventory'. Regression guard for Codex HIGH
        2026-04-18 (half-applied normalization) — without the fix,
        ``clip_durations["7"]`` fails even though ``clip_durations["0007"]``
        is present.
        """
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "7", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "8", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        # Canonical padded form only — matches production shape.
        clip_durations = {"0007": 2.0, "0008": 2.0}
        pause_after_ms = [500, 0]
        # Must complete without raising.
        out = _enforce_hard_constraint_and_enrich(
            raw, script, word_ts, clip_durations,
        )
        assert len(out) == 2
        # source_duration_sec was resolved via the padded key.
        assert out[0]["source_duration_sec"] == 2.0
        assert out[1]["source_duration_sec"] == 2.0

class TestSprint10_5SpanFormulaAlignment:
    """Sprint 10.5 — the three readers (clip_assigner enforcer, Gemini #2
    prompt HARD CONSTRAINT, renderer bind-time) agree on span semantics.

    The display span for a phrase is the gap from its first word's start
    to the NEXT phrase's first word's start (peek-ahead across segment
    boundaries), with the very last phrase using ``word_timestamps[-1].end``
    (narration_end) — C1.2 amendment. The target-extension buffer between
    narration_end and target_duration_sec is purely renderer territory;
    bridges from the unused-clip pool fill any tail overflow.

    Inter-segment authored silence is encoded into word_timestamps by TTS
    delivery — the next segment's first word's start sits AFTER the
    pause — so ``pause_after_ms`` drops out of span math entirely.
    """

    def test_phrase_display_span_sec_peek_ahead_vs_last_phrase(self):
        """AC2 (C1.2-amended): helper returns ``next.start - this.start``
        when ``next_start_word_idx`` is given; ``word_timestamps[-1].end
        - this.start`` when it is ``None``.

        The last-phrase branch uses narration_end (the last word's end),
        NOT final_display_end. The target-extension buffer past
        narration_end is renderer territory (bridge-filled).
        """
        from promo.core.assign.clip_assigner import _phrase_display_span_sec

        # 4 words at 0.2s each with a 0.5s inter-phrase gap after word 1.
        word_ts = [
            {"word": "a", "start": 0.0, "end": 0.2},
            {"word": "b", "start": 0.2, "end": 0.4},
            # gap of 0.5s between word 1 (ending 0.4) and word 2 (starting 0.9)
            {"word": "c", "start": 0.9, "end": 1.1},
            {"word": "d", "start": 1.1, "end": 1.3},
        ]

        # Peek-ahead branch: phrase starting at word 0 with next phrase
        # starting at word 2 → span = word[2].start − word[0].start.
        span_peek = _phrase_display_span_sec(0, 2, word_ts)
        assert abs(span_peek - 0.9) < 1e-6, (
            f"peek-ahead span must be 0.9s (= word[2].start 0.9 − word[0].start 0.0), got {span_peek}"
        )

        # Last-phrase branch: no next → span runs to narration_end
        # (= word_timestamps[-1].end = 1.3). Phrase starts at word 2
        # (0.9s) → span = 1.3 − 0.9 = 0.4.
        span_last = _phrase_display_span_sec(2, None, word_ts)
        assert abs(span_last - 0.4) < 1e-6, (
            f"last-phrase span must be 0.4s (= narration_end 1.3 − word[2].start 0.9), got {span_last}"
        )

        # Last-phrase clamp: if narration_end somehow precedes the
        # phrase's first word (defensive, shouldn't arise in normal
        # flow), span clamps to 0.
        word_ts_inverted = [
            {"word": "a", "start": 2.0, "end": 2.5},
            {"word": "b", "start": 0.0, "end": 0.1},  # narration_end < phrase start
        ]
        span_clamp = _phrase_display_span_sec(0, None, word_ts_inverted)
        assert span_clamp == 0.0

    def test_enforce_peeks_across_segment_boundary_to_absorb_pause(self):
        """AC3: segment 1's last phrase peek-ahead's segment 2's first
        phrase, so the inter-segment authored silence is naturally
        included in the computed span."""
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich

        # 2 segments × 5 words each with a 4.0s inter-segment silence
        # injected into the delivered word_timestamps timeline.
        script = _make_c2_script(n_segments=2)
        # Override: stretch the inter-segment gap to 4.0s so the pause-driven
        # span is visibly larger than the spoken duration.
        word_ts = []
        t = 0.0
        for seg_i in range(2):
            if seg_i > 0:
                t += 4.0
            for _ in range(5):
                word_ts.append({"word": "w", "start": round(t, 4), "end": round(t + 0.2, 4)})
                t += 0.2
        # Realign the script's target so final_display_end sits at narration_end.
        narration_end = word_ts[-1]["end"]  # 2*5*0.2 + 4.0 = 6.0s
        script["target_duration_sec"] = narration_end

        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        # Clip 0001 MUST span the 4.0s pause + preceding spoken — 5.0s or more.
        clip_durations = {"0001": 6.0, "0002": 2.0}

        out = _enforce_hard_constraint_and_enrich(
            raw, script, word_ts, clip_durations,
        )
        # Seg 1 last phrase span = word[5].start (5.0s) − word[0].start (0.0)
        # = 5.0s. That's spoken 1.0s + silence 4.0s. Proves peek-ahead
        # naturally includes the inter-segment pause.
        assert abs(out[0]["display_span_sec"] - 5.0) < 0.01, (
            f"seg 1's last phrase must peek across the segment boundary "
            f"for span 5.0s (spoken 1.0 + pause 4.0), got {out[0]['display_span_sec']}"
        )
        # Seg 2 last phrase span = narration_end (6.0) − word[5].start (5.0) = 1.0s.
        # Under C1.2, the last phrase uses narration_end, not final_display_end;
        # target-extension is renderer territory (bridge-filled).
        assert abs(out[1]["display_span_sec"] - 1.0) < 0.01

    def test_last_phrase_uses_narration_end_regardless_of_target(self):
        """AC4 (C1.2-amended): the last phrase's span ALWAYS uses
        ``word_timestamps[-1].end`` (narration_end), regardless of
        ``target_duration_sec``. The target-extension buffer between
        narration_end and target is renderer territory — bridges fill
        any tail overflow from the unused-clip pool.

        Regression guard for the Sprint 10.5 Jashita v2 failure mode:
        tying the assigner's last-phrase constraint to
        ``final_display_end = max(target, narration_end)`` makes any
        narration with ``target − last_phrase.first_word.start >
        pool_max`` unrecoverably fail with ``ClipAssignmentError``,
        violating the 10b amendment that bridges are canonical
        silence-fill infrastructure.
        """
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich

        # Narration runs to ~2.5s with the default helpers.
        word_ts = _make_c2_word_timestamps(n_segments=2)
        narration_end = word_ts[-1]["end"]
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": "0002", "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        clip_durations = {"0001": 10.0, "0002": 2.0}

        # Branch A: target_duration_sec = 45s (way past narration_end ~2.5s).
        # Under C1.2, the last phrase span is NOT 43.5s; it's narration_end
        # minus phrase start = 2.5 − 1.5 = 1.0s. Bridge territory covers
        # the 45 − 2.5 = 42.5s tail at render time.
        script_extended = _make_c2_script(n_segments=2)
        script_extended["target_duration_sec"] = 45.0
        out_extended = _enforce_hard_constraint_and_enrich(
            raw, script_extended, word_ts, clip_durations,
        )
        expected = narration_end - 1.5
        assert abs(out_extended[1]["display_span_sec"] - expected) < 0.01, (
            f"under C1.2, last phrase span must use narration_end regardless "
            f"of target (expected {expected:.3f}s, got {out_extended[1]['display_span_sec']})"
        )

        # Branch B: target_duration_sec = narration_end (no extension).
        # Same span answer.
        script_flush = _make_c2_script(n_segments=2)
        script_flush["target_duration_sec"] = narration_end
        out_flush = _enforce_hard_constraint_and_enrich(
            raw, script_flush, word_ts, clip_durations,
        )
        assert abs(out_flush[1]["display_span_sec"] - expected) < 0.01, (
            f"target == narration_end case must also use narration_end "
            f"(expected {expected:.3f}s, got {out_flush[1]['display_span_sec']})"
        )

class TestSprint12bRetrieveClipsFnKwarg:
    """AC1 — assign_clips_with_f3_retry accepts retrieve_clips_fn kwarg,
    default None = no-op; supplied callable replaces clips_metadata."""

    def test_kwarg_signature_default_is_none(self):
        import inspect
        from promo.core.assign.clip_assigner import assign_clips_with_f3_retry

        params = inspect.signature(assign_clips_with_f3_retry).parameters
        assert "retrieve_clips_fn" in params
        assert params["retrieve_clips_fn"].default is None

    def test_default_none_passes_full_clips_metadata(self, monkeypatch):
        """When retrieve_clips_fn is None (Sprint 11 behavior), the full
        clips_metadata list is forwarded to assign_clips byte-identical."""
        from promo.core.assign import clip_assigner

        clips = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        calls = {"count": 0, "seen_metadata": None}

        def fake_assign_clips(script, wts, pl, clips_metadata, cd, vi):
            calls["count"] += 1
            calls["seen_metadata"] = clips_metadata
            return []

        monkeypatch.setattr(clip_assigner, "assign_clips", fake_assign_clips)
        script = {"segments": [{"text": "hi", "pause_after_ms": 0}]}
        narration = {"word_timestamps": [], "segment_timestamps": []}
        clip_assigner.assign_clips_with_f3_retry(
            script, narration, clips, {}, variant_index=1,
        )
        assert calls["count"] == 1
        assert calls["seen_metadata"] is clips  # same list reference

    def test_supplied_callable_replaces_metadata_for_call(self, monkeypatch):
        from promo.core.assign import clip_assigner

        clips = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        seen: list[list[dict]] = []

        def fake_assign_clips(script, wts, pl, clips_metadata, cd, vi):
            seen.append(clips_metadata)
            return []

        monkeypatch.setattr(clip_assigner, "assign_clips", fake_assign_clips)
        script = {"segments": [{"text": "hi", "pause_after_ms": 0}]}
        narration = {"word_timestamps": [], "segment_timestamps": []}

        def retrieve(current_script):
            return clips[:2]  # only first two

        clip_assigner.assign_clips_with_f3_retry(
            script, narration, clips, {}, variant_index=1,
            retrieve_clips_fn=retrieve,
        )
        assert len(seen) == 1
        assert [c["id"] for c in seen[0]] == ["a", "b"]

    def test_retrieve_raises_falls_back_to_full_pool_not_propagated(
        self, monkeypatch, caplog
    ):
        """Sprint 12b audit L-001 fix — a retrieve_clips_fn that raises ANY
        exception type (not just ClipAssignmentError) must NOT propagate;
        the wrap inside _retrieved() falls back to the full clips_metadata
        pool and logs a WARNING, preserving the 'no-op on retrieval
        failure' design intent documented in the closure."""
        from promo.core.assign import clip_assigner

        clips = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        seen: list[list[dict]] = []

        def fake_assign_clips(script, wts, pl, clips_metadata, cd, vi):
            seen.append(clips_metadata)
            return []

        monkeypatch.setattr(clip_assigner, "assign_clips", fake_assign_clips)

        def broken_retrieve(current_script):
            raise ValueError("simulated transient retrieval failure")

        script = {"segments": [{"text": "hi", "pause_after_ms": 0}]}
        narration = {"word_timestamps": [], "segment_timestamps": []}

        with caplog.at_level("WARNING"):
            clip_assigner.assign_clips_with_f3_retry(
                script, narration, clips, {}, variant_index=1,
                retrieve_clips_fn=broken_retrieve,
            )

        # Full pool reached assign_clips (not filtered, not raised).
        assert len(seen) == 1
        assert seen[0] is clips
        assert any(
            "retrieval closure raised" in r.message.lower()
            and "valueerror" in r.message.lower()
            for r in caplog.records
        )


class TestSprint18FEmptyRetrievalProvenanceSeedsContract:
    """Sprint 18 F regression guard — `_empty_retrieval_provenance` is the
    one canonical seed for every per-variant + run-level provenance
    dict. The `retrieval_contract` field must be present with the literal
    string `"soft_hint"` on every call so a future caller (or future
    refactor) can't accidentally route around it.
    """

    def test_seed_carries_soft_hint_literal(self):
        from promo.core.pipeline.bgm_voice_resolver import _empty_retrieval_provenance

        dict1 = _empty_retrieval_provenance()
        dict2 = _empty_retrieval_provenance()
        assert dict1["retrieval_contract"] == "soft_hint"
        # New dict per call — mutating one must not affect the next call's seed.
        dict1["retrieval_contract"] = "strict"
        assert _empty_retrieval_provenance()["retrieval_contract"] == "soft_hint"
        # All 6 keys must be present on every fresh seed (Sprint 13 AC19
        # five + Sprint 18 F's `retrieval_contract`). Audit-fix L-004:
        # the audit caught that this loop omitted `retrieval_contract`,
        # which would let a future refactor conditionally drop the field
        # while still passing this guard.
        for key in ("retrieval_active", "embedded_pool_size", "reduced_pool_size",
                    "mimo_prompt_sha1", "fallback_reason", "retrieval_contract"):
            assert key in dict2


class TestSprint18FRetrievalContractFieldInPayload:
    """Sprint 18 F AC3 — `retrieval_contract: "soft_hint"` on every run.

    Captures the payload dict passed to `_write_sidecar` for the
    `clip_assignments_*.json` base name and asserts the literal value.
    Pairs with the `_empty_retrieval_provenance` seed and the spread
    into the sidecar payload at `sidecar_writer.py:149`.
    """

    def test_clip_assignments_payload_carries_retrieval_contract_soft_hint(
        self, tmp_path,
    ):
        from promo.core.pipeline import sidecar_writer

        captured: list[tuple[str, dict]] = []

        def _fake_write(sidecar_dir, base_name, payload, description):
            from promo.core.pipeline.sidecar_writer import SidecarWriteResult

            captured.append((base_name, payload))
            return SidecarWriteResult(
                ok=True,
                path=str(tmp_path / base_name),
                description=description,
                base_name=base_name,
            )

        backend = MagicMock()
        backend.output_dir.return_value = str(tmp_path)
        provenance = {
            "retrieval_active": False,
            "embedded_pool_size": 0,
            "reduced_pool_size": 0,
            "mimo_prompt_sha1": None,
            "fallback_reason": None,
            "retrieval_contract": "soft_hint",
        }

        with patch.object(sidecar_writer, "_write_sidecar_result", side_effect=_fake_write):
            ok = sidecar_writer._emit_run_sidecars(
                backend=backend,
                output_path=str(tmp_path / "promo.mp4"),
                poi_name="Test Hotel",
                target_duration_sec=30.0,
                tts_metrics=[{"variant_index": 1}],
                match_quality_entries=[{"variant_index": 1}],
                clip_assignments_entries=[{"variant_index": 1, "assignments": []}],
                run_retrieval_provenance=provenance,
            )

        assert ok is True
        clip_assign_payloads = [
            p for base_name, p in captured
            if base_name.startswith("clip_assignments_")
        ]
        assert len(clip_assign_payloads) == 1
        payload = clip_assign_payloads[0]
        assert payload["retrieval_contract"] == "soft_hint"
        # The provenance fields must spread alongside the soft-hint flag.
        assert payload["retrieval_active"] is False
        # And the variants list lands at the same level.
        assert isinstance(payload.get("variants"), list)


class TestSprint18FRetrievalSoftHintContract:
    """Sprint 18 F AC6 — retrieval is a soft hint, not a strict gate.

    `_enforce_hard_constraint_and_enrich` is the only place a clip_id
    naming-violation could surface; the runtime carries no
    `clip_id in retrieved_ids` guard, so a Gemini #2 reply that names a
    clip outside the retrieved subset must pass through to the enriched
    output unchanged. This test pins that contract so a future strict-
    retrieval-mode change can't accidentally land without a contract
    bump.
    """

    def test_out_of_retrieved_subset_clip_id_is_accepted(self):
        from promo.core.assign.clip_assigner import _enforce_hard_constraint_and_enrich

        # Set up a non-trivial inventory: 10 clips, retrieved subset is 3.
        full_pool_ids = [f"{i:04d}" for i in range(10)]  # 0000..0009
        retrieved_ids = {"0001", "0002", "0003"}
        # Gemini #2 reply names clip_id "0007" — outside the retrieved
        # subset but inside the full pool. Under the soft-hint contract
        # this must be accepted.
        out_of_subset_clip = "0007"
        assert out_of_subset_clip not in retrieved_ids
        assert out_of_subset_clip in full_pool_ids

        script = _make_c2_script(n_segments=2)
        word_ts = _make_c2_word_timestamps()
        raw = [
            {"segment": 1, "clip_id": "0001", "start_word_idx": 0,
             "end_word_idx": 4, "trim_start": 0.0},
            {"segment": 2, "clip_id": out_of_subset_clip, "start_word_idx": 5,
             "end_word_idx": 9, "trim_start": 0.0},
        ]
        clip_durations = {cid: 2.0 for cid in full_pool_ids}

        out = _enforce_hard_constraint_and_enrich(
            raw, script, word_ts, clip_durations,
        )
        # Must return successfully — no exception.
        assert len(out) == 2
        # The out-of-subset clip survives into the enriched assignments.
        emitted_ids = [a["clip_id"] for a in out]
        assert out_of_subset_clip in emitted_ids


class TestSprint12bF3RetryReRetrieval:
    """AC2 / H1 — retrieve_clips_fn is invoked on BOTH initial and F3 retry;
    the retry invocation sees the REGENERATED script's segments."""

    def test_retry_reinvokes_retriever_on_regenerated_script(self, monkeypatch):
        from promo.core.assign import clip_assigner
        from promo.core.errors import ClipAssignmentError

        clips = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        # Stub assign_clips: raise on first call, succeed on second.
        call_state = {"count": 0}

        def fake_assign(script, wts, pl, clips_metadata, cd, vi):
            call_state["count"] += 1
            if call_state["count"] == 1:
                raise ClipAssignmentError(
                    segment_index=1, phrase_index=1,
                    required_span=1.5, actual_max_usable=1.0,
                    clip_id="a",
                )
            return []

        monkeypatch.setattr(clip_assigner, "assign_clips", fake_assign)

        script_v1 = {"segments": [{"text": "first-text", "pause_after_ms": 0}]}
        narration_v1 = {"word_timestamps": [], "segment_timestamps": []}
        script_v2 = {"segments": [{"text": "regenerated-text", "pause_after_ms": 0}]}
        narration_v2 = {"word_timestamps": [], "segment_timestamps": []}

        recorded_texts: list[str] = []

        def retrieve(current_script):
            recorded_texts.append(current_script["segments"][0]["text"])
            return clips

        def regen_script(hint):
            return script_v2

        def regen_narration(new_script):
            return narration_v2

        clip_assigner.assign_clips_with_f3_retry(
            script_v1, narration_v1, clips, {}, variant_index=1,
            regenerate_script_fn=regen_script,
            regenerate_narration_fn=regen_narration,
            retrieve_clips_fn=retrieve,
        )
        assert recorded_texts == ["first-text", "regenerated-text"]
        assert call_state["count"] == 2


class TestSprintArsenalExternalizationGemini2Template:
    """AC-11: the Gemini #2 prompt MD contains the verbatim invariant
    substrings that pin the two-space model + the arithmetic-check
    discipline.

    Sprint 10.5 C1.2 + Sprint 10b post-close audit both retracted
    sprint criteria because the load-bearing assumptions about
    ``narration_end`` vs ``target_duration_sec`` lived in code comments
    and Gemini-prompt prose that drifted independently. After arsenal
    externalization, the prose lives in
    ``arsenal/system_prompts/gemini2_assign_v1.md``; this test fires
    if anyone edits that MD without preserving the invariant phrasing.
    """

    def test_gemini2_md_pins_invariant_substrings(self):
        """The invariant phrases that anchor the two-space model in the
        Gemini #2 prompt MUST be present in the loaded MD. Each phrase
        addresses a different drift surface — the precomputed-constants
        reminder, the per-phrase arithmetic discipline, the last-phrase
        ceiling, and the renderer-bridge handover."""
        from promo.core.arsenal_loader import load_system_prompt

        md = load_system_prompt("gemini2_assign")
        assert "PRECOMPUTED CONSTANTS" in md
        assert "ARITHMETIC CHECK" in md
        assert "the last phrase's constraint uses the last word's end" in md
        assert "NOT target_duration_sec" in md
        assert "bridge mechanism" in md
