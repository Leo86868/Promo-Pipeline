"""Sprint 10 C6 — fixture-replay validation.

Reproduces the exact Gemini #2 → hard-constraint flow against the two
Sprint 09b production scripts (LPI v1, Jashita v1), using committed
fixtures captured from one live Gemini #2 call per POI. The test's
purpose is to prove that **the 09b narrations would not require
freeze-prevention bridges under Sprint 10's two-pass architecture** —
and that the L-001 (missing-segment) and L-003 (phrase-tiling) guards
added to ``clip_assigner._enforce_hard_constraint_and_enrich`` by the
Sprint 10a audit-fix commit stay load-bearing against real-shape
inputs.

The fixture recipe is documented at
``promo/tests/fixtures/sprint-10/_recipe.md``. Synthetic uniform
word_timestamps are a deliberate simplification — under the Sprint 10.5
C1.2 peek-ahead formula, display span is
``word_timestamps[next_phrase.start].start -
word_timestamps[this_phrase.start].start`` (or ``word_timestamps[-1].end
- this_phrase.first_word.start`` for the very last phrase — narration_end-
bounded, not final_display_end), which is conserved under uniform word
spacing within a segment.

Re-capture:

    python3 -m promo.tests._helpers.sprint_10_fixtures --live

This refreshes the happy-path fixture hashes below AND the two derived
negative fixtures per POI. If you re-capture, update
``EXPECTED_HASH_*`` to match the new ``sprint_10_fixtures.py``
log line ``Gemini #2 {poi} sha256: ...``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from promo.core.assign.clip_assigner import (
    HARD_CONSTRAINT_TOL_SEC,
    _enforce_hard_constraint_and_enrich,
)
from promo.core.errors import ClipAssignmentError
# Audit L-001 fix: import the exact same synthetic-word-timestamp function
# the fixture-builder used during live capture, so the enforcer sees
# byte-identical word_timestamps to what Gemini #2 was prompted with.
#
# Gated behind importorskip so test collection survives in environments
# where the helper module is intentionally absent (e.g. a stripped CI
# bundle). Working-tree runs always execute the test.
_builder_module = pytest.importorskip("promo.tests._helpers.sprint_10_fixtures")
_builder_synth = _builder_module._synthetic_word_timestamps


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "sprint-10"

# Sprint 09b variant-1 scripts consistently emit 5 segments under the LONG
# format profile; named constant rather than a magic 5 so the recipe-
# invariant test can cite why (audit L-004 fix).
EXPECTED_SEGMENT_COUNT_09B_LONG = 5

# Sprint 10 C6 — hashes of the committed Gemini #2 captures. Mismatch
# means the committed JSON was hand-edited without going through the
# live-capture path, which would invalidate the test's claim that
# Gemini #2 "on production-shape input" satisfies the hard constraint.
EXPECTED_HASH_LPI = (
    "e9c06a636279da7dc10bf4414a8e3299f8d5ed7cb18fd1431bf35793b3611b5f"
)
EXPECTED_HASH_JASHITA = (
    "d2e04b8c07ea4217ea045c07914886a13b5352ced4e5c2badf3a76c3aef14a8a"
)


# ---------------------------------------------------------------------------
#  Fixture helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict | list:
    return json.loads((FIXTURES_DIR / name).read_text())


# Audit L-001 fix: single source of truth — delegate to the builder.
_synthetic_word_timestamps = _builder_synth


@pytest.fixture(scope="module")
def _clip_durations_cache():
    """Audit L-002 fix: ffprobe each material pool at most once per
    pytest session. Without this fixture the ``material/<poi>/clips``
    pool is re-probed on every test method (2 POIs × 3 methods = 6
    batches × ~20 mp4 = ~120 redundant ffprobe subprocesses)."""
    cache: dict[str, dict[str, float]] = {}

    def _for(clips_dir: Path) -> dict[str, float]:
        key = str(clips_dir)
        if key in cache:
            return cache[key]
        import re as _re
        from promo.core.render.remotion_renderer import get_clip_duration
        out: dict[str, float] = {}
        for p in sorted(clips_dir.glob("*.mp4")):
            m = _re.search(r"(\d{4})", p.name)
            if not m:
                continue
            out[m.group(1)] = get_clip_duration(str(p))
        cache[key] = out
        return out

    return _for


_POI_CONFIG = {
    "lpi": {
        "script_fixture": "lpi_v1_script.json",
        "gemini_fixture": "gemini2_response_lpi_v1.json",
        "drop_fixture": "gemini2_response_lpi_v1_drop_segment.json",
        "overlap_fixture": "gemini2_response_lpi_v1_overlap.json",
        "clips_dir": Path("material/little-palm-island-resort/clips"),
        "expected_hash": EXPECTED_HASH_LPI,
    },
    "jashita": {
        "script_fixture": "jashita_v1_script.json",
        "gemini_fixture": "gemini2_response_jashita_v1.json",
        "drop_fixture": "gemini2_response_jashita_v1_drop_segment.json",
        "overlap_fixture": "gemini2_response_jashita_v1_overlap.json",
        "clips_dir": Path("material/jashita-hotel-tulum/clips"),
        "expected_hash": EXPECTED_HASH_JASHITA,
    },
}


def _skip_if_material_missing(clips_dir: Path) -> None:
    abs_dir = (Path(__file__).resolve().parents[3] / clips_dir)
    if not abs_dir.exists():
        pytest.skip(
            f"Material pool not available at {abs_dir}; fixture-replay "
            "test requires real clip durations via ffprobe."
        )


# ---------------------------------------------------------------------------
#  Happy-path: Gemini #2 captures satisfy the hard constraint end-to-end
# ---------------------------------------------------------------------------


class TestSprint10C6FixtureHashes:
    """Committed fixtures must match the hash produced by the last
    ``--live`` capture. Drift = someone hand-edited the fixture, which
    invalidates the "zero bridges on production scripts" claim."""

    @pytest.mark.parametrize("poi_key", ["lpi", "jashita"])
    def test_gemini_fixture_hash_matches(self, poi_key):
        cfg = _POI_CONFIG[poi_key]
        payload = _load_fixture(cfg["gemini_fixture"])
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert digest == cfg["expected_hash"], (
            f"{poi_key} Gemini #2 fixture hash drift: expected "
            f"{cfg['expected_hash']}, got {digest}. Re-run "
            "`python3 -m promo.tests._helpers.sprint_10_fixtures --live` "
            f"and update EXPECTED_HASH_{poi_key.upper()} in this file."
        )


class TestSprint10C6HappyPath:
    """C6 criterion 5 — ``assign_clips`` (via the hard-constraint
    enforcement path) on the committed Gemini #2 response raises no
    ClipAssignmentError. Proves 09b's LPI v1 + Jashita v1 narrations
    would have rendered without bridges under Sprint 10's architecture.
    """

    @pytest.mark.parametrize("poi_key", ["lpi", "jashita"])
    def test_production_script_satisfies_hard_constraint(
        self, poi_key, _clip_durations_cache,
    ):
        cfg = _POI_CONFIG[poi_key]
        _skip_if_material_missing(cfg["clips_dir"])
        repo_root = Path(__file__).resolve().parents[3]
        script = _load_fixture(cfg["script_fixture"])
        response = _load_fixture(cfg["gemini_fixture"])
        word_ts = _synthetic_word_timestamps(script)
        pauses = [int(s["pause_after_ms"]) for s in script["segments"]]
        clip_durations = _clip_durations_cache(repo_root / cfg["clips_dir"])

        enriched = _enforce_hard_constraint_and_enrich(
            response,
            {"segments": script["segments"]},
            word_ts,
            clip_durations,
        )

        # Every enriched phrase satisfies source_duration − trim_start ≥ display_span.
        # Audit L-003 fix: share the imported tolerance constant so tightening
        # the enforcer's slack tightens this assertion in lockstep.
        for entry in enriched:
            clip_id = entry["clip_id"]
            trim_start = float(entry["trim_start"])
            span = float(entry["display_span_sec"])
            source = float(entry["source_duration_sec"])
            assert source - trim_start + HARD_CONSTRAINT_TOL_SEC >= span, (
                f"{poi_key} post-enrich hard-constraint hole at clip "
                f"{clip_id}: source={source:.2f}s − trim_start={trim_start:.2f}s "
                f"< span={span:.2f}s"
            )
        # The count reflects one phrase per Gemini #2 entry, not per segment.
        assert len(enriched) == len(response), (
            f"{poi_key} enriched count ({len(enriched)}) != response "
            f"count ({len(response)}) — enforcement dropped phrases silently"
        )


# ---------------------------------------------------------------------------
#  Negative fixtures — guard L-001 and L-003 against future Gemini drift
# ---------------------------------------------------------------------------


class TestSprint10C6NegativeFixtures:
    """C6 criterion 5 + Sprint 10a reflection 2a: mutated Gemini #2
    responses must raise ClipAssignmentError via the specific guard the
    mutation targets. These are frozen regression guards — if a future
    refactor weakens the guard, these tests catch it."""

    @pytest.mark.parametrize("poi_key", ["lpi", "jashita"])
    def test_drop_segment_triggers_l001_missing_segment_guard(
        self, poi_key, _clip_durations_cache,
    ):
        """L-001 guard (``_enforce_hard_constraint_and_enrich`` checks
        every script segment received assignments) fires when one
        segment's phrases are removed from the Gemini #2 response."""
        cfg = _POI_CONFIG[poi_key]
        _skip_if_material_missing(cfg["clips_dir"])
        repo_root = Path(__file__).resolve().parents[3]
        script = _load_fixture(cfg["script_fixture"])
        mutated = _load_fixture(cfg["drop_fixture"])
        assert "dropped_segment" in mutated, (
            "drop-segment fixture must record which segment it removed"
        )
        response = mutated["assignments"]
        word_ts = _synthetic_word_timestamps(script)
        pauses = [int(s["pause_after_ms"]) for s in script["segments"]]
        clip_durations = _clip_durations_cache(repo_root / cfg["clips_dir"])

        with pytest.raises(ClipAssignmentError) as exc_info:
            _enforce_hard_constraint_and_enrich(
                response,
                {"segments": script["segments"]},
                word_ts,
                clip_durations,
            )
        assert "no assignment for segments" in str(exc_info.value), (
            f"{poi_key} drop-segment mutation must raise the L-001 "
            f"missing-segment diagnostic (got {exc_info.value!r})"
        )

    @pytest.mark.parametrize("poi_key", ["lpi", "jashita"])
    def test_overlap_phrases_triggers_l003_tiling_guard(
        self, poi_key, _clip_durations_cache,
    ):
        """L-003 guard (phrase tiling end-to-end with no overlaps) fires
        when two phrases within a segment have colliding word-idx ranges.
        """
        cfg = _POI_CONFIG[poi_key]
        _skip_if_material_missing(cfg["clips_dir"])
        repo_root = Path(__file__).resolve().parents[3]
        script = _load_fixture(cfg["script_fixture"])
        mutated = _load_fixture(cfg["overlap_fixture"])
        assert "overlap_segment" in mutated, (
            "overlap fixture must record which segment it mutated"
        )
        response = mutated["assignments"]
        word_ts = _synthetic_word_timestamps(script)
        pauses = [int(s["pause_after_ms"]) for s in script["segments"]]
        clip_durations = _clip_durations_cache(repo_root / cfg["clips_dir"])

        with pytest.raises(ClipAssignmentError) as exc_info:
            _enforce_hard_constraint_and_enrich(
                response,
                {"segments": script["segments"]},
                word_ts,
                clip_durations,
            )
        assert "expected start_word_idx" in str(exc_info.value), (
            f"{poi_key} overlap mutation must raise the L-003 tiling "
            f"diagnostic (got {exc_info.value!r})"
        )


# ---------------------------------------------------------------------------
#  Recipe / metadata invariants — if these drift the recipe is stale
# ---------------------------------------------------------------------------


class TestSprint10C6RecipeInvariants:
    """Lightweight shape checks on the committed fixtures, so a broken
    recipe (e.g. wrong word_count parsing) fails here rather than in
    the hard-constraint path where the error is harder to diagnose."""

    @pytest.mark.parametrize("poi_key", ["lpi", "jashita"])
    def test_script_fixture_has_expected_segment_count_with_expected_keys(self, poi_key):
        cfg = _POI_CONFIG[poi_key]
        script = _load_fixture(cfg["script_fixture"])
        assert len(script["segments"]) == EXPECTED_SEGMENT_COUNT_09B_LONG, (
            f"{poi_key} fixture must have "
            f"{EXPECTED_SEGMENT_COUNT_09B_LONG} segments (09b LONG format)"
        )
        required = {"segment", "text", "pause_after_ms", "word_count", "start_sec", "end_sec"}
        for seg in script["segments"]:
            assert required.issubset(seg.keys()), (
                f"segment keys missing {required - set(seg.keys())}"
            )
            # word_count must match the actual split.
            assert len(seg["text"].split()) == int(seg["word_count"]), (
                f"{poi_key} seg {seg['segment']}: word_count={seg['word_count']} "
                f"but text.split() has {len(seg['text'].split())}"
            )

    @pytest.mark.parametrize("poi_key", ["lpi", "jashita"])
    def test_gemini_fixture_is_list_of_phrase_dicts(self, poi_key):
        cfg = _POI_CONFIG[poi_key]
        response = _load_fixture(cfg["gemini_fixture"])
        assert isinstance(response, list) and response, (
            "Gemini #2 response fixture must be a non-empty list"
        )
        required = {"segment", "clip_id", "start_word_idx", "end_word_idx", "trim_start"}
        for entry in response:
            assert required.issubset(entry.keys()), (
                f"phrase dict missing {required - set(entry.keys())}"
            )
