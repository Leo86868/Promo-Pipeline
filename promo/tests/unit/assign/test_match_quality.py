"""Unit tests for promo.core.assign.match_quality."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

class TestSprint08MatchQuality:
    """match_quality sidecar: overlap scoring + entry construction."""

    def test_overlap_score_basic(self):
        from promo.core.assign.match_quality import compute_overlap_score
        assert compute_overlap_score("The pool is warm", "pool water turquoise") == 0.5
        assert compute_overlap_score("x y z", "nothing matches") == 0.0
        # Empty narration defines to 0.0, not a divide-by-zero crash.
        assert compute_overlap_score("", "anything") == 0.0

    def test_build_entries_shape(self):
        # the builder consumes the word-idx assignments list produced by
        # the packer (packer.pack_clips) plus the TTS
        # word_timestamps. Phrase text is reconstructed by slicing
        # word_timestamps on each assignment's start/end indices.
        from promo.core.assign.match_quality import build_match_quality_entries
        word_timestamps = [
            {"word": "The", "start": 0.00, "end": 0.10},
            {"word": "pool", "start": 0.10, "end": 0.30},
            {"word": "is", "start": 0.30, "end": 0.40},
            {"word": "warm.", "start": 0.40, "end": 0.70},
            {"word": "The", "start": 0.80, "end": 0.90},
            {"word": "sun", "start": 0.90, "end": 1.10},
            {"word": "is", "start": 1.10, "end": 1.20},
            {"word": "bright.", "start": 1.20, "end": 1.50},
        ]
        assignments = [
            {"segment": 1, "clip_id": "0001",
             "start_word_idx": 0, "end_word_idx": 3,
             "trim_start": 0.0, "display_span_sec": 0.7,
             "source_duration_sec": 5.0},
            {"segment": 1, "clip_id": "0002",
             "start_word_idx": 4, "end_word_idx": 7,
             "trim_start": 0.0, "display_span_sec": 0.7,
             "source_duration_sec": 5.0},
        ]
        meta = [
            {"id": "0001", "scene_description": "pool water turquoise", "category": "pool"},
            {"id": "0002", "scene_description": "blue sky light", "category": "scenic"},
        ]
        entries = build_match_quality_entries(
            assignments=assignments,
            clips_metadata=meta,
            word_timestamps=word_timestamps,
            variant_index=1,
        )
        assert len(entries) == 2
        for e in entries:
            assert set(e.keys()) == {
                "variant_index", "segment_idx", "clip_id", "narration_phrase",
                "scene_description", "overlap_score", "picked_category",
            }
        assert entries[0]["picked_category"] == "pool"
        assert entries[0]["narration_phrase"] == "The pool is warm."
        assert entries[1]["narration_phrase"] == "The sun is bright."
        assert 0.0 <= entries[0]["overlap_score"] <= 1.0

class TestSprint10C5MatchQualityRewire:
    """C5 touches match_quality.py — the builder now consumes Gemini #2
    assignments + word_timestamps, and the legacy _split_by_cut_after helper
    retired alongside the renderer's cut_after path."""

    def test_split_by_cut_after_retired(self):
        import inspect
        from promo.core.assign import match_quality
        src = inspect.getsource(match_quality)
        assert "def _split_by_cut_after" not in src
        assert not hasattr(match_quality, "_split_by_cut_after")

    def test_build_match_quality_entries_signature(self):
        import inspect
        from promo.core.assign.match_quality import build_match_quality_entries
        sig = inspect.signature(build_match_quality_entries)
        params = list(sig.parameters)
        assert "assignments" in params, (
            "match_quality builder must accept the word-idx assignments list"
        )
        assert "word_timestamps" in params, (
            "match_quality builder must receive TTS word_timestamps for "
            "phrase reconstruction by word-idx slice"
        )
        # script_segments parameter is gone — the old cut_after path retired.
        assert "script_segments" not in params

class TestSprint10C5MatchQualityLookupCollision:
    """Audit L-002 regression guard: ``meta_by_id`` must not key on the
    empty-string sentinel produced by a clip whose ``id`` field is
    missing — and the ``lstrip("0")`` fallback must not collapse a
    clip_id like ``"0000"`` onto that sentinel.
    """

    def test_missing_id_does_not_collide_with_zero_clip_id(self):
        from promo.core.assign.match_quality import build_match_quality_entries
        word_timestamps = [{"word": "hi.", "start": 0.0, "end": 0.5}]
        assignments = [
            {"segment": 1, "clip_id": "0000",
             "start_word_idx": 0, "end_word_idx": 0,
             "trim_start": 0.0, "display_span_sec": 0.5,
             "source_duration_sec": 5.0},
        ]
        # One meta entry has NO id field — would have keyed as "" before fix.
        meta = [
            {"scene_description": "WRONG poolside", "category": "pool"},
            {"id": "0000", "scene_description": "CORRECT beach", "category": "beach"},
        ]
        entries = build_match_quality_entries(
            assignments=assignments,
            clips_metadata=meta,
            word_timestamps=word_timestamps,
            variant_index=1,
        )
        assert len(entries) == 1
        # Must not inherit the id-less entry's metadata.
        assert entries[0]["picked_category"] == "beach"
        assert entries[0]["scene_description"] == "CORRECT beach"
