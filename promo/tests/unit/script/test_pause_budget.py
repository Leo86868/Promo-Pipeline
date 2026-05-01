"""Unit tests for promo.core.script.pause_budget."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestSprint08PauseBudget:
    """compute_pause_budget: weights → ms from target × WPM math.

    Sprint 08.5 rewrite: only ``pause_weight >= 2`` gaps receive nonzero
    ms. ``pause_weight == 1`` / missing / invalid get 0 (their pauses are
    produced by the merged ElevenLabs call via ``plan_tts_batches``).
    """

    def test_proportional_distribution_across_hard_gaps(self):
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 26, "pause_weight": 2},
            {"word_count": 28, "pause_weight": 2},
            {"word_count": 30, "pause_weight": 2},
            {"word_count": 28, "pause_weight": 3},
            {"word_count": 20, "pause_weight": 1},
        ]
        compute_pause_budget(segs, target_sec=65, wpm=140)
        gaps = [s["pause_after_ms"] for s in segs]
        # Last segment always 0.
        assert gaps[-1] == 0
        # First three gaps are weight=2 → equal; gap 3 is weight=3 → larger.
        assert gaps[0] == gaps[1] == gaps[2]
        assert gaps[3] > gaps[0]
        assert sum(gaps) > 0

    def test_weight_one_gets_zero_ms(self):
        """Sprint 08.5: pause_weight=1 gaps get 0 ms (merged by planner)."""
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 26, "pause_weight": 1},
            {"word_count": 28, "pause_weight": 2},
            {"word_count": 30, "pause_weight": 3},
            {"word_count": 28, "pause_weight": 1},
            {"word_count": 20, "pause_weight": 1},
        ]
        compute_pause_budget(segs, target_sec=65, wpm=140)
        gaps = [s["pause_after_ms"] for s in segs]
        # Only indices 1 and 2 (weight 2 and 3) get ms; 0, 3, 4 are zero.
        assert gaps[0] == 0
        assert gaps[3] == 0
        assert gaps[4] == 0
        assert gaps[1] > 0
        assert gaps[2] > 0

    def test_invalid_weights_treated_as_one(self, caplog):
        """Missing / invalid pause_weight values are treated as weight=1 (zero ms).

        No "even fallback" anymore — contrary to Sprint 08 behavior.
        """
        import logging
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 26, "pause_weight": None},
            {"word_count": 28, "pause_weight": "oops"},
            {"word_count": 30, "pause_weight": 0},
            {"word_count": 28},  # missing entirely
            {"word_count": 20},
        ]
        with caplog.at_level(logging.WARNING, logger="promo.core.script.pause_budget"):
            compute_pause_budget(segs, target_sec=65, wpm=140)
        gaps = [s["pause_after_ms"] for s in segs]
        # All four gaps resolve to weight=1 (or last-seg ignored) → all zero.
        assert gaps == [0, 0, 0, 0, 0]
        # And the "no hard gaps available" warning is emitted.
        assert any(
            "no hard gaps available" in rec.getMessage() for rec in caplog.records
        )

    def test_under_budget_yields_zero_pauses(self, caplog):
        import logging
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 100, "pause_weight": 2},
            {"word_count": 100, "pause_weight": 2},
        ]
        with caplog.at_level(logging.WARNING, logger="promo.core.script.pause_budget"):
            compute_pause_budget(segs, target_sec=30, wpm=140)
        assert all(s["pause_after_ms"] == 0 for s in segs)
        assert any(
            "narration already fills target" in rec.getMessage()
            for rec in caplog.records
        )

    def test_empty_input_returns_empty(self):
        from promo.core.script.pause_budget import compute_pause_budget
        assert compute_pause_budget([], target_sec=30) == []

class TestSprint08MeasureWPM:
    """measure_wpm extracts throughput from ElevenLabs alignment data."""

    def test_measure_wpm_basic(self):
        from promo.core.script.pause_budget import measure_wpm
        wts = [
            {"word": "a", "start": 0.0, "end": 0.5},
            {"word": "b", "start": 0.5, "end": 1.0},
            {"word": "c", "start": 1.0, "end": 1.5},
        ]
        assert measure_wpm(wts) == 120.0

    def test_measure_wpm_empty_returns_none(self):
        from promo.core.script.pause_budget import measure_wpm
        assert measure_wpm([]) is None
        assert measure_wpm([{"word": "x", "start": 0, "end": 1}]) is None

def _mk_seg(idx: int, text: str, *, word_count: int | None = None,
            pause_weight: int | None = None,
            pause_after_ms: int | None = None) -> dict:
    seg: dict = {"segment": idx, "text": text}
    if word_count is not None:
        seg["word_count"] = word_count
    if pause_weight is not None:
        seg["pause_weight"] = pause_weight
    if pause_after_ms is not None:
        seg["pause_after_ms"] = pause_after_ms
    return seg

class TestSprint085BatchPlanner:
    """AC4: plan_tts_batches merges pause_weight=1 boundaries into one batch."""

    def test_all_standard_weight_one_batch(self):
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        # 5 segments, 4 gaps all weight=1 → 1 batch holding all 5.
        segs = [_mk_seg(i, f"s{i}", pause_weight=1) for i in range(1, 6)]
        batches = plan_tts_batches(segs)
        assert len(batches) == 1
        assert len(batches[0]["segments"]) == 5
        assert batches[0]["post_batch_silence_ms"] is None

    def test_all_hard_gaps_n_batches(self):
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        # 5 segments, 4 gaps all weight>=2 → 5 batches, 1 segment each.
        weights = [2, 2, 2, 3, 1]  # last ignored
        mses = [1100, 1200, 1300, 1400, 0]
        segs = [
            _mk_seg(i + 1, f"s{i}", pause_weight=w, pause_after_ms=m)
            for i, (w, m) in enumerate(zip(weights, mses))
        ]
        batches = plan_tts_batches(segs)
        assert len(batches) == 5
        assert all(len(b["segments"]) == 1 for b in batches)
        # Non-final batches' post_batch_silence_ms == preceding seg pause_after_ms.
        assert batches[0]["post_batch_silence_ms"] == 1100
        assert batches[1]["post_batch_silence_ms"] == 1200
        assert batches[2]["post_batch_silence_ms"] == 1300
        assert batches[3]["post_batch_silence_ms"] == 1400
        assert batches[4]["post_batch_silence_ms"] is None

    def test_mixed_pattern_1_2_3_1(self):
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        # pause_weights = [1, 2, 3, 1, _] → 3 batches: [s1,s2], [s3], [s4,s5]
        weights = [1, 2, 3, 1, 1]
        mses = [0, 2200, 3300, 0, 0]
        segs = [
            _mk_seg(i + 1, f"s{i + 1}", pause_weight=w, pause_after_ms=m)
            for i, (w, m) in enumerate(zip(weights, mses))
        ]
        batches = plan_tts_batches(segs)
        assert len(batches) == 3
        # Batch 1: [s1, s2], silence = s2's pause_after_ms = 2200
        assert len(batches[0]["segments"]) == 2
        assert batches[0]["segments"][0]["segment"] == 1
        assert batches[0]["segments"][1]["segment"] == 2
        assert batches[0]["post_batch_silence_ms"] == 2200
        # Batch 2: [s3], silence = s3's pause_after_ms = 3300
        assert len(batches[1]["segments"]) == 1
        assert batches[1]["segments"][0]["segment"] == 3
        assert batches[1]["post_batch_silence_ms"] == 3300
        # Batch 3: [s4, s5], post = None
        assert len(batches[2]["segments"]) == 2
        assert batches[2]["segments"][0]["segment"] == 4
        assert batches[2]["segments"][1]["segment"] == 5
        assert batches[2]["post_batch_silence_ms"] is None

    def test_two_segments_weight_one(self):
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        segs = [
            _mk_seg(1, "a", pause_weight=1),
            _mk_seg(2, "b", pause_weight=1),
        ]
        batches = plan_tts_batches(segs)
        assert len(batches) == 1
        assert len(batches[0]["segments"]) == 2
        assert batches[0]["post_batch_silence_ms"] is None

    def test_single_segment(self):
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        batches = plan_tts_batches([_mk_seg(1, "solo")])
        assert len(batches) == 1
        assert len(batches[0]["segments"]) == 1
        assert batches[0]["post_batch_silence_ms"] is None

    def test_empty_input(self):
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        assert plan_tts_batches([]) == []

    def test_invalid_weight_treated_as_one(self):
        """Missing/invalid pause_weight keeps segments in the same batch."""
        from promo.core.narrate.tts_batch_planner import plan_tts_batches
        segs = [
            _mk_seg(1, "a"),  # no weight
            _mk_seg(2, "b", pause_weight="oops"),  # non-integer
            _mk_seg(3, "c", pause_weight=None),
        ]
        batches = plan_tts_batches(segs)
        assert len(batches) == 1
        assert len(batches[0]["segments"]) == 3

class TestSprint085PauseBudgetTailConstraint:
    """AC7+AC8: weight>=2-only distribution + tail-safety constraint."""

    def test_only_hard_gaps_receive_ms(self):
        """Sprint 08.5 AC7 test case 1: weights=[1,2,3,1,_] → only idx 1,2 nonzero."""
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 26, "pause_weight": 1},
            {"word_count": 28, "pause_weight": 2},
            {"word_count": 30, "pause_weight": 3},
            {"word_count": 28, "pause_weight": 1},
            {"word_count": 20, "pause_weight": 1},  # last ignored
        ]
        compute_pause_budget(segs, target_sec=65, wpm=165)
        gaps = [s["pause_after_ms"] for s in segs]
        # Segments 0, 3 (weight=1) and 4 (last) get 0. 1 and 2 get nonzero.
        assert gaps[0] == 0
        assert gaps[3] == 0
        assert gaps[4] == 0
        assert gaps[1] > 0
        assert gaps[2] > gaps[1]  # weight 3 > weight 2

    def test_no_hard_gaps_warns_and_zeros(self, caplog):
        """AC7 test case 2: all weights=1 → all zero + warning."""
        import logging
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 26, "pause_weight": 1},
            {"word_count": 28, "pause_weight": 1},
            {"word_count": 30, "pause_weight": 1},
            {"word_count": 28, "pause_weight": 1},
            {"word_count": 20, "pause_weight": 1},
        ]
        with caplog.at_level(logging.WARNING, logger="promo.core.script.pause_budget"):
            compute_pause_budget(segs, target_sec=65, wpm=165)
        assert all(s["pause_after_ms"] == 0 for s in segs)
        assert any(
            "no hard gaps available" in r.getMessage() for r in caplog.records
        )

    def test_last_seg_zero_regardless_of_weight(self):
        """AC7 test case 3: weights=[2,2,2,2,3] → first 4 gaps nonzero, last=0."""
        from promo.core.script.pause_budget import compute_pause_budget
        segs = [
            {"word_count": 26, "pause_weight": 2},
            {"word_count": 28, "pause_weight": 2},
            {"word_count": 30, "pause_weight": 2},
            {"word_count": 28, "pause_weight": 2},
            {"word_count": 20, "pause_weight": 3},  # weight 3 IGNORED (last seg)
        ]
        compute_pause_budget(segs, target_sec=65, wpm=165)
        gaps = [s["pause_after_ms"] for s in segs]
        assert all(g > 0 for g in gaps[:4])
        assert gaps[4] == 0  # last segment ignored despite weight=3

class TestSprint085SilenceBuffer:
    """AC10: SILENCE_BUFFER_SCALE = 1.10 applied to weight>=2 gap silence."""

    def test_apply_silence_buffer_scales_by_1_10(self):
        from promo.core.script.pause_budget import _apply_silence_buffer
        assert _apply_silence_buffer(4000) == 4400

    def test_apply_silence_buffer_caps_at_per_gap_cap(self):
        from promo.core.script.pause_budget import _apply_silence_buffer, PER_GAP_CAP_MS
        # 6500 * 1.10 = 7150 → capped at 7000.
        assert _apply_silence_buffer(6500) == PER_GAP_CAP_MS

    def test_apply_silence_buffer_zero_in_zero_out(self):
        from promo.core.script.pause_budget import _apply_silence_buffer
        assert _apply_silence_buffer(0) == 0

class TestSprint085DocstringFix:
    """AC13: pause_budget.py docstring's per-gap cap matches actual 7000 ms."""

    def test_compute_pause_budget_docstring_no_3s(self):
        """No '(3 s)' or '3s)' in pause_budget.py — the cap is 7000 ms now."""
        import promo.core.script.pause_budget as pb
        source = open(pb.__file__).read()
        assert "(3 s)" not in source
        # The 7-second cap comment / constant should be present.
        assert "7000" in source

class TestSprint09bC7WPMCalibration:
    """Sprint 09b C7 (ACs 28-31): load_calibrated_wpm reads measured
    WPM from the most recent matching sidecar, scoped to same-POI
    same-duration only, with explicit bootstrap fallback."""

    def _make_sidecar(self, dirpath, filename, variants: list[dict]):
        import json
        path = dirpath / filename
        path.write_text(json.dumps(variants))
        return path

    def test_returns_none_when_no_sidecars(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result is None

    def test_reads_measured_wpm_from_sidecar(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_foo_65s.json", [
            {"variant_index": 1, "measured_wpm": 180.0},
            {"variant_index": 2, "measured_wpm": 190.0},
        ])
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result == 185  # mean of 180 and 190

    def test_fallback_to_measured_wpm_spoken(self, tmp_path):
        """When measured_wpm is absent, falls back to measured_wpm_spoken."""
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_foo_65s.json", [
            {"variant_index": 1, "measured_wpm_spoken": 195.0},
        ])
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result == 195

    def test_same_poi_scoping_ignores_other_pois(self, tmp_path):
        """AC29: cross-POI sidecars are NOT read."""
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_other_poi_65s.json", [
            {"variant_index": 1, "measured_wpm": 150.0},
        ])
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result is None

    def test_same_duration_scoping_ignores_other_durations(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_foo_30s.json", [
            {"variant_index": 1, "measured_wpm": 150.0},
        ])
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result is None

    def test_most_recent_mtime_wins(self, tmp_path):
        import os
        from promo.core.script.pause_budget import load_calibrated_wpm
        d1 = tmp_path / "older"
        d2 = tmp_path / "newer"
        d1.mkdir()
        d2.mkdir()
        older = self._make_sidecar(d1, "tts_metrics_foo_65s.json", [
            {"measured_wpm": 170.0},
        ])
        newer = self._make_sidecar(d2, "tts_metrics_foo_65s.json", [
            {"measured_wpm": 200.0},
        ])
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        result = load_calibrated_wpm("foo", 65, [str(d1), str(d2)])
        assert result == 200  # newer wins

    def test_malformed_sidecar_returns_none_without_crashing(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        (tmp_path / "tts_metrics_foo_65s.json").write_text("{not valid json")
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result is None

    def test_missing_key_variants_skipped(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_foo_65s.json", [
            {"variant_index": 1},  # no measured_wpm
            {"variant_index": 2, "measured_wpm": 180.0},
        ])
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result == 180  # only valid variant averaged

    def test_all_missing_values_returns_none(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_foo_65s.json", [
            {"variant_index": 1},
            {"variant_index": 2},
        ])
        result = load_calibrated_wpm("foo", 65, [str(tmp_path)])
        assert result is None

    def test_none_dir_in_search_ignored(self, tmp_path):
        from promo.core.script.pause_budget import load_calibrated_wpm
        self._make_sidecar(tmp_path, "tts_metrics_foo_65s.json", [
            {"measured_wpm": 180.0},
        ])
        # One None and one valid dir — must not crash.
        result = load_calibrated_wpm("foo", 65, [None, str(tmp_path)])
        assert result == 180

class TestSprint10C4PauseBudgetTailCapRemoved:
    """C4 criterion 11: compute_pause_budget's tail_source_sec /
    safety_buffer_sec kwargs are gone; _assign_tail_clip is gone."""

    def test_compute_pause_budget_signature_has_no_tail_kwargs(self):
        import inspect
        from promo.core.script.pause_budget import compute_pause_budget

        params = list(inspect.signature(compute_pause_budget).parameters)
        assert "tail_source_sec" not in params
        assert "safety_buffer_sec" not in params

    def test_compute_pause_budget_source_has_no_tail_cap_code(self):
        """Check the executable body (not the docstring — which legitimately
        references the removed kwargs to explain their retirement)."""
        import ast
        import inspect
        from promo.core.script import pause_budget

        module_src = inspect.getsource(pause_budget)
        tree = ast.parse(module_src)
        fn_node = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "compute_pause_budget"),
            None,
        )
        assert fn_node is not None
        # Skip the docstring (ast.Expr with Constant-str value) — check
        # every other statement's source text for forbidden symbols.
        body_stmts = fn_node.body
        if (body_stmts and isinstance(body_stmts[0], ast.Expr)
                and isinstance(body_stmts[0].value, ast.Constant)
                and isinstance(body_stmts[0].value.value, str)):
            body_stmts = body_stmts[1:]
        body_source = "\n".join(ast.unparse(s) for s in body_stmts)
        assert "tail_cap_end" not in body_source
        assert "tail_constrained" not in body_source
        assert "tail_source_sec" not in body_source

    def test_assign_tail_clip_is_deleted(self):
        from promo.cli import compile_promo

        assert not hasattr(compile_promo, "_assign_tail_clip"), (
            "Sprint 10 C4 criterion 11: _assign_tail_clip must be deleted "
            "from compile_promo.py"
        )

    def test_no_tail_source_sec_references_in_production_code(self):
        """Grep verify: criterion 11 requires zero matches for
        tail_source_sec / _assign_tail_clip across promo/ excluding tests."""
        import inspect
        from promo.core.script import pause_budget
        from promo.cli import compile_promo

        for mod in (pause_budget, compile_promo):
            src = inspect.getsource(mod)
            # Comments that reference the retirement are fine; function
            # / kwarg usage is NOT. The deleted kwargs would show up as
            # 'tail_source_sec:' or 'tail_source_sec=' in real code.
            assert "tail_source_sec:" not in src
            assert "tail_source_sec=" not in src
            assert "_assign_tail_clip(" not in src
