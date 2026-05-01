"""Dynamic pause budget computation for narration authoring.

Gemini authors per-segment ``pause_weight`` (narrative intent: 1=STANDARD,
2=STANDARD+, 3=REVEAL); this module computes the ``pause_after_ms`` values
from target-duration × WPM math so coverage is a code guarantee, not a
Gemini arithmetic prayer.

Sprint 08.5 policy (see design memo
``<operator-memory-root>/project_pause_budget_design.md``):

- Only ``pause_weight >= 2`` gaps receive explicit silence ms. ``pause_weight
  == 1`` (STANDARD) and invalid/missing weights get ``pause_after_ms = 0`` —
  their pauses are produced by merging consecutive segments into a single
  ElevenLabs call, where the model's natural prosody carries the beat.
- Each weight≥2 gap's silence duration is inflated by
  ``SILENCE_BUFFER_SCALE`` (1.10) to give the final pause a little extra
  room; the inflated total is capped at ``PER_GAP_CAP_MS``.

Sprint 10 C4 removed the tail-safety cap (previously
``narration_end ≤ target_sec − tail_source_sec + safety_buffer_sec``). The
tail cap existed to prevent last-clip-freeze when the pre-10 one-pass
Gemini picked the tail clip without knowing TTS timing. Under the two-pass
architecture, Gemini #2's hard constraint
``source_dur(clip_id) − trim_start ≥ display_span`` makes every per-phrase
clip self-sufficient, so no tail reserve is needed in the pause budget.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Per-backend cold-start WPM bootstraps. ``load_calibrated_wpm`` overrides
# these from the most recent matching ``tts_metrics_*.json`` sidecar
# (same-POI same-duration prior runs); bootstraps only fire on the first
# run of a new POI+duration combination.
#
# Each OBSERVED_*_WPM is measured on a specific Gemini-#1 × TTS-backend
# combo. If either side swaps (new Gemini #1 model or new TTS voice/
# backend), recalibrate by running one POI end-to-end and reading the
# ``measured_wpm`` off the emitted ``tts_metrics`` sidecar.
#
# ElevenLabs bootstrap — Sprint 07/08/08.5 baseline:
#   - Gemini 2.0 Flash + speed=0.95 → ~165 WPM (Sprint 08)
#   - Gemini 2.5 Pro  + speed=0.95 → ~195 WPM (Sprint 08.5 after B1 model flip)
#
# Gemini TTS bootstrap — Sprint TTS-Migration Phase 1 voice A/B lock:
#   - Kore + directorial prompt "Read at a confident, engaging pace,
#     warm but never sluggish:" → 147.9 WPM measured on the 99-word test
#     script (decision memo: 2026-04-21-gemini-voice-lock.md). Rounded
#     to 148 for the bootstrap constant.
OBSERVED_ELEVENLABS_WPM = 195
OBSERVED_GEMINI_WPM_BOOTSTRAP = 148


def bootstrap_wpm_for_backend(backend: str) -> int:
    """Return the cold-start per-backend WPM bootstrap.

    Sprint TTS-Migration Phase 4 — ONE of the two allowed
    backend-equality sites in the codebase (contract N3; the other is
    ``tts_engine.generate_narration``). Callers receive an integer;
    they never re-branch on ``backend`` themselves.

    Unknown backends fall back to the ElevenLabs bootstrap to preserve
    Sprint 07/08/08.5 behavior if a future persona YAML names a backend
    that hasn't landed in code yet.
    """
    if backend == "gemini":
        return OBSERVED_GEMINI_WPM_BOOTSTRAP
    return OBSERVED_ELEVENLABS_WPM

DEFAULT_TARGET_COVERAGE = 0.90

# Per-gap pause ceiling. Sprint 08 uses per-batch TTS + ffmpeg silence
# concat (no SSML break involvement), so there's no engine-side cap. The
# cap here is a pacing sanity rail. Empirically on LONG 65 s targets with
# 130-140 word scripts, the required budget distributes to 4.5-5.5 s on
# the highest-weight gap — 7000 ms gives enough headroom to cover the full
# math budget without truncation while still flagging scripts that would
# need unreasonably long (>7 s) pauses as pacing warnings.
PER_GAP_CAP_MS = 7000

# Silence buffer (Sprint 08.5, Item 4): each weight>=2 gap's silence gets
# an extra 10% beyond the raw distribution. Operator's "multi-给一点空间"
# directive — a slightly longer hold at an intentional beat lands better
# than a cramped one. Scaled output is still capped at PER_GAP_CAP_MS.
SILENCE_BUFFER_SCALE = 1.10


def load_calibrated_wpm(
    poi_slug: str,
    duration_sec: float,
    sidecar_search_dirs: Iterable[str | os.PathLike],
    backend: str | None = None,
) -> int | None:
    """Return a measured-WPM calibration from the most recent matching
    ``tts_metrics_{poi_slug}_{round(duration_sec)}s.json`` sidecar, or
    ``None`` if no matching sidecar / variant is found.

    Sprint 09b C7 — replaces the hardcoded ``OBSERVED_ELEVENLABS_WPM``
    bootstrap with per-POI per-duration calibration so each run pulls
    its pause budget from the prior run's actual measured tempo.

    Sprint TTS-Migration Phase 5 — the ``backend`` parameter scopes
    calibration to the same TTS backend as the current run. Without
    this filter, a Gemini run's 137 WPM sidecar polluted the pause
    budget of a subsequent ElevenLabs run (which renders at ~166 WPM),
    underestimating required silence and dropping coverage below 85%.
    When ``backend`` is None, all entries are counted (back-compat for
    any pre-migration sidecar that lacked the field).

    Scoping rule (operator-approved — same-POI same-duration only):
    cross-POI averaging pollutes pause budget because LPI (178 WPM)
    and Jashita (187 WPM) have ~10 WPM variance on identical speed/
    model settings. First run of a new POI falls back to the bootstrap.

    Selection: if multiple matching sidecars exist across
    ``sidecar_search_dirs``, the most-recent by mtime wins. Across
    variants inside that sidecar, the arithmetic mean of the available
    ``measured_wpm`` (or ``measured_wpm_spoken``, whichever is
    populated) values is returned, rounded to an int. Malformed
    sidecars (missing keys, unreadable JSON) are skipped without
    crashing.

    Sprint 16 — entries are additionally filtered by their own
    ``target_duration_sec`` field so a mixed-mode sidecar (Sprint 16
    `RandomFormatSelector` may write 30s and 65s variants into the
    same file when the run-level CLI duration is one of them) does
    not blend short-form and long-form WPM measurements into a single
    poisoned calibration. Entries without a ``target_duration_sec``
    field (pre-Sprint-16 sidecars) are still counted — same back-compat
    posture as the ``backend`` filter above.
    """
    target_dur = int(round(duration_sec))
    # Sprint 09b C8 (09b audit L-002 fix): match both the base filename
    # and the collision-bump variants written by _write_sidecar, so a
    # second run in the same output dir does not leave the calibration
    # reader pointing at the first run's stale sidecar.
    target_stem = f"tts_metrics_{poi_slug}_{target_dur}s"
    match_exact = f"{target_stem}.json"
    match_bumped = f"{target_stem}-*.json"

    candidates: list[tuple[float, Path]] = []
    for d in sidecar_search_dirs:
        if d is None:
            continue
        base = Path(d)
        if not base.exists() or not base.is_dir():
            continue
        matched = []
        exact = base / match_exact
        if exact.exists():
            matched.append(exact)
        matched.extend(base.glob(match_bumped))
        for path in matched:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, path))

    if not candidates:
        return None

    # Most recent mtime wins.
    candidates.sort(reverse=True)
    for _mtime, path in candidates:
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        values: list[float] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if backend is not None:
                entry_backend = entry.get("backend")
                if entry_backend is not None and entry_backend != backend:
                    continue
            entry_dur = entry.get("target_duration_sec")
            if entry_dur is not None:
                try:
                    if int(round(float(entry_dur))) != target_dur:
                        continue
                except (TypeError, ValueError):
                    pass
            for key in ("measured_wpm", "measured_wpm_spoken"):
                v = entry.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    values.append(float(v))
                    break
        if values:
            return int(round(sum(values) / len(values)))

    return None


def _apply_silence_buffer(ms: int) -> int:
    """Return ``ms * SILENCE_BUFFER_SCALE`` rounded, capped at ``PER_GAP_CAP_MS``.

    Exposed for unit testing the AC10 scaling semantics directly.
    """
    if ms <= 0:
        return 0
    scaled = int(round(ms * SILENCE_BUFFER_SCALE))
    return min(scaled, PER_GAP_CAP_MS)


def compute_pause_budget(
    segments: list[dict],
    target_sec: float,
    wpm: int = OBSERVED_ELEVENLABS_WPM,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
) -> list[dict]:
    """Populate ``pause_after_ms`` on each segment.

    Mutates each segment in-place. Returns the same list for convenience.

    Distribution rules:
    - Only segments with ``pause_weight >= 2`` receive nonzero
      ``pause_after_ms``. ``pause_weight == 1`` / invalid / last-segment
      always get ``0`` — their "pauses" come from the merged ElevenLabs call.
    - Target end = ``coverage_target_end = target_sec * target_coverage``.
    - Raw required budget = ``target_end - predicted_spoken_sec``, then
      divided by ``SILENCE_BUFFER_SCALE`` to reserve room for the buffer.
    - Per-gap ms is ``_apply_silence_buffer(raw_share)`` (capped at
      ``PER_GAP_CAP_MS``).

    Sprint 10 C4: ``tail_source_sec`` / ``safety_buffer_sec`` kwargs
    removed. The tail-cap branch was load-bearing only when one-pass
    Gemini could under-pick the tail clip's source duration; two-pass
    Gemini #2 enforces ``source_dur − trim_start ≥ display_span`` per
    phrase, which makes the tail-reserve redundant.

    Warnings:
    - "narration already fills target" when spoken duration ≥ coverage target.
    - "no hard gaps available" when all weights < 2 (planner will merge the
      whole script into one ElevenLabs call; no explicit silence emitted).
    """
    if not segments:
        return segments

    # Zero everything first; populate hard-gap indices below.
    for seg in segments:
        seg["pause_after_ms"] = 0

    n_gaps = len(segments) - 1
    if n_gaps <= 0:
        return segments

    # Identify hard-gap indices: segments whose following gap has weight >= 2.
    hard_gaps: list[tuple[int, int]] = []  # (segment_index, weight)
    for i in range(n_gaps):
        raw = segments[i].get("pause_weight")
        try:
            w = int(raw) if raw is not None else 1
        except (TypeError, ValueError):
            w = 1
        if w >= 2:
            hard_gaps.append((i, w))

    total_words = sum(int(s.get("word_count") or 0) for s in segments)
    predicted_spoken_sec = (total_words / wpm) * 60 if wpm > 0 else 0.0
    target_sec = float(target_sec)
    coverage_target_end = target_sec * float(target_coverage)
    required_pause_sec = coverage_target_end - predicted_spoken_sec

    if required_pause_sec <= 0:
        logger.warning(
            "Pause budget: narration already fills target "
            "(%d words at %d WPM = %.2fs ≥ %.2fs goal). All pauses set to 0.",
            total_words, wpm, predicted_spoken_sec, coverage_target_end,
        )
        return segments

    if not hard_gaps:
        logger.warning(
            "Pause budget: no hard gaps available (no pause_weight >= 2 across "
            "%d gap(s)) — pause budget cannot redistribute. All pauses set to 0. "
            "Script will merge into a single ElevenLabs call (natural phrasing only).",
            n_gaps,
        )
        return segments

    # Reserve room for the +10% silence buffer when computing raw budget.
    raw_required_ms = int(round(required_pause_sec * 1000 / SILENCE_BUFFER_SCALE))
    total_weight = sum(w for _i, w in hard_gaps)

    scaled_assigned_ms = 0
    capped_count = 0
    for idx, w in hard_gaps:
        raw_share = int(round(raw_required_ms * (w / total_weight)))
        scaled = _apply_silence_buffer(raw_share)
        if scaled == PER_GAP_CAP_MS and int(round(raw_share * SILENCE_BUFFER_SCALE)) > PER_GAP_CAP_MS:
            capped_count += 1
        segments[idx]["pause_after_ms"] = scaled
        scaled_assigned_ms += scaled

    requested_scaled_ms = int(round(required_pause_sec * 1000))
    shortfall_ms = requested_scaled_ms - scaled_assigned_ms
    if capped_count > 0 and shortfall_ms > 0:
        logger.warning(
            "Pause budget: %d/%d hard gap(s) capped at %dms — total silence "
            "%dms vs requested %dms (shortfall %dms ≈ %.2fs). Video may fall "
            "short of target coverage.",
            capped_count, len(hard_gaps), PER_GAP_CAP_MS,
            scaled_assigned_ms, requested_scaled_ms, shortfall_ms,
            shortfall_ms / 1000.0,
        )

    final_narration_end = predicted_spoken_sec + scaled_assigned_ms / 1000.0
    logger.info(
        "Pause budget: %d words at %d WPM → %.2fs spoken + %.2fs silence "
        "across %d hard gap(s); narration_end=%.2fs (target_end=%.2fs)",
        total_words, wpm, predicted_spoken_sec, scaled_assigned_ms / 1000.0,
        len(hard_gaps), final_narration_end, coverage_target_end,
    )

    return segments


def measure_wpm(word_timestamps: list[dict]) -> float | None:
    """Measure effective WPM from ElevenLabs alignment data.

    ``word_timestamps`` is the list emitted by ``tts_engine.generate_narration``.
    Returns ``words_per_minute`` (float) or ``None`` when the input is too
    short or malformed to measure.
    """
    if not word_timestamps or len(word_timestamps) < 2:
        return None
    try:
        first_start = float(word_timestamps[0]["start"])
        last_end = float(word_timestamps[-1]["end"])
    except (KeyError, TypeError, ValueError):
        return None
    duration_sec = last_end - first_start
    if duration_sec <= 0:
        return None
    return len(word_timestamps) / duration_sec * 60.0
