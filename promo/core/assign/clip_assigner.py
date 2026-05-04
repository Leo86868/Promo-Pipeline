"""Sprint 10 — Gemini #2 clip assignment on real TTS timing.

Second pass of the two-pass Gemini architecture. Consumes the script text
produced by Gemini #1 (``segments[].text + pause_weight``) plus the
ElevenLabs TTS output (``word_timestamps``, ``segment_timestamps``) plus
the ``pause_after_ms`` authored by ``compute_pause_budget``, and emits
per-phrase clip assignments:

    {
        "segment": 1,
        "clip_id": "0007",
        "start_word_idx": 0,        # global index into word_timestamps
        "end_word_idx": 5,
        "trim_start": 2.4,          # seconds into the clip's source
        "display_span_sec": 3.82,   # peek-ahead span: next_phrase.first_word.start −
                                    # this.first_word.start (or narration_end −
                                    # this.first_word.start for the very last phrase)
        "source_duration_sec": 6.2,
    }

Hard constraint per phrase (TOL=0.05s):

    clip_durations[clip_id] - trim_start + TOL  >=  display_span_sec

Violation raises :class:`promo.core.errors.ClipAssignmentError` naming the
first offending segment + phrase indices. Under Sprint 10.5 C1.2, the
constraint ensures each assigned phrase is covered by its clip up to
``narration_end``; the tail ``(narration_end, target_duration_sec)`` is
canonical bridge territory — the renderer extends the last clip and
inserts bridge clips from the unused-clip pool to cover any remaining
gap (per the Sprint 10b amendment). Bridges are expected infrastructure,
not a defect the assigner tries to eliminate.

F3 policy (operator-approved at /plan time): on violation, the pipeline
issues ONE retry to Gemini #1 with a structured "tighten segment X" hint
derived from the error; a second failure aborts the variant without a
third call. :func:`assign_clips_with_f3_retry` encapsulates that loop
behind two injected callables so the retry logic is unit-testable without
running the real Gemini #1 or TTS stacks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from string import Template
from typing import Any, Callable, Iterable

from promo.core import arsenal_loader
from promo.core.llm.retry import retry_with_backoff
from promo.core.llm.gemini_client import resolve_gemini_model
from promo.core.errors import ClipAssignmentError
from promo.core.format_profiles import PromoFormatProfile, get_promo_format_profile
from promo.core.schema import (
    ClipAssignment,
    ClipMetadata,
    Narration,
    Script,
    WordTimestamp,
)
from promo.core.script.script_generator import _format_clip_inventory

# ---------------------------------------------------------------------------
#  Back-compat re-exports — extracted symbols (Sprint S2b)
# ---------------------------------------------------------------------------
# Tests + downstream code import these names directly from
# ``clip_assigner`` (e.g. ``from promo.core.assign.clip_assigner import
# load_latest_clip_assignments``). After S2b these symbols live in
# sibling modules; ``clip_assigner`` remains the single import path the
# test + caller surface targets.
from promo.core.assign.clip_assignment_validator import (  # noqa: E402
    HARD_CONSTRAINT_TOL_SEC,
    _enforce_hard_constraint_and_enrich,
    _phrase_display_span_sec,
    _segment_phrase_layout,
)
from promo.core.assign.clip_assignment_gemini import (  # noqa: E402
    _FENCE_RE,
    _build_gemini2_prompt,
    _call_gemini2,
    _format_phrase_timing_block,
    _parse_gemini2_json,
)
from promo.core.assign.clip_assignment_sidecar import (  # noqa: E402
    load_latest_clip_assignments,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def assign_clips(
    script: Script,
    word_timestamps: list[WordTimestamp],
    pause_after_ms: Iterable[int] | list[int],
    clips_metadata: list[ClipMetadata],
    clip_durations: dict[str, float],
    variant_index: int = 1,
) -> list[ClipAssignment]:
    """Single Gemini #2 pass with hard-constraint enforcement.

    Raises :class:`ClipAssignmentError` on the first constraint violation.
    Returns the enriched per-phrase assignment list on success.

    Under the Sprint 10.5 C1.2 peek-ahead formula, display span for each
    phrase is ``word_timestamps[next_phrase.start].start −
    word_timestamps[this_phrase.start].start`` (or
    ``word_timestamps[-1].end − this_phrase.first_word.start`` for the
    very last phrase). The last-phrase ceiling is narration_end, NOT
    ``final_display_end = max(target, narration_end)`` — that buffer is
    canonical bridge territory (renderer extends last clip + bridges the
    tail per the Sprint 10b amendment). ``pause_after_ms`` is still
    authored by Gemini #1 and passed through here for Gemini #2 prompt
    rendering (narration context) + downstream ffmpeg silence concat,
    but it no longer drives the enforcer's span math — inter-segment
    silence is already encoded in ``word_timestamps`` via TTS delivery.
    """
    pause_list = list(pause_after_ms)
    target_duration_sec = float(script.get("target_duration_sec") or 65.0)
    profile = get_promo_format_profile(target_duration_sec)

    prompt = _build_gemini2_prompt(
        script, word_timestamps, pause_list, clips_metadata,
        variant_index, profile,
        target_duration_sec=target_duration_sec,
    )
    raw = _call_gemini2(prompt)
    return _enforce_hard_constraint_and_enrich(
        raw, script, word_timestamps, clip_durations,
    )


def build_tighten_hint(exc: ClipAssignmentError) -> str:
    """Structured hint fed back to Gemini #1 on the F3 single retry.

    Format is stable so future tools (dashboards, sprint logs) can parse
    it deterministically.
    """
    return (
        f"Segment {exc.segment_index} required {exc.required_span:.2f}s "
        f"of visual but pool max usable is {exc.actual_max_usable:.2f}s; "
        f"tighten segment {exc.segment_index} or redistribute words."
    )


def assign_clips_with_f3_retry(
    script: Script,
    narration: Narration,
    clips_metadata: list[ClipMetadata],
    clip_durations: dict[str, float],
    *,
    variant_index: int = 1,
    regenerate_script_fn: Callable[[str], Script] | None = None,
    regenerate_narration_fn: Callable[[Script], Narration] | None = None,
    retrieve_clips_fn: Callable[[Script], list[ClipMetadata]] | None = None,
) -> tuple[Script, Narration, list[ClipAssignment]]:
    """Apply :func:`assign_clips` with the F3 single-retry policy.

    On the initial call's :class:`ClipAssignmentError`, if both
    ``regenerate_script_fn`` and ``regenerate_narration_fn`` are supplied,
    builds a :func:`build_tighten_hint` string from the error, calls
    ``regenerate_script_fn(hint)`` to get a new script, calls
    ``regenerate_narration_fn(new_script)`` to get fresh TTS output, then
    invokes :func:`assign_clips` ONE more time. A second
    :class:`ClipAssignmentError` propagates to the caller (full_pipeline
    aborts the variant). No third attempt is made.

    Returns ``(final_script, final_narration, assignments)``. Tests
    exercise this by patching :func:`assign_clips` with a side-effect
    sequence and asserting call counts — the actual Gemini #2 API is
    never hit in the retry-policy tests.

    Sprint 12b — ``retrieve_clips_fn`` lets a caller inject a
    retrieval-based narrowing of ``clips_metadata`` for each Gemini #2
    attempt. When supplied, the callable receives the current ``script``
    (original on the initial attempt; regenerated on the F3 retry) and
    returns a filtered ``list[dict]`` that replaces ``clips_metadata``
    for that attempt only. Default ``None`` = Sprint 11 no-op (byte-
    identical behavior). The callable is invoked on BOTH attempts so
    the F3 retry sees a freshly-retrieved inventory matched to its
    regenerated segment texts — retrieval is stateless by construction
    (see ``clip_retriever.py`` absence of ``@lru_cache``).

    Sprint 18 F — retrieval is a **soft hint** (advisory), not a strict
    gate. The retrieved subset is what Gemini #2 *sees* in its prompt,
    but its reply is NOT rejected if it names a ``clip_id`` outside that
    subset: the closure below catches every exception and falls back to
    the full pool, and ``_enforce_hard_constraint_and_enrich`` carries
    no ``clip_id in retrieved_ids`` guard. Four documented fallback codes
    encode cases where retrieval did not even produce a hint and the full
    pool was passed unmodified to Gemini #2:

      - ``no_sidecar`` — ``embedding_cache_dir`` was threaded but the
        embedding sidecar is missing for this POI / MiMo prompt.
      - ``m4_attach_shrinkage`` — sidecar-embedded pool < MiMo metadata
        pool (some clips missing vectors).
      - ``h2_union_shortfall`` — union of top-k retrieval over segment-
        text queries came out shorter than the segment count.
      - ``retrieval_exception`` — the retrieval closure raised; the
        defensive wrap below logs and falls through.

    These codes are recorded in ``retrieval_provenance`` (consumed by
    ``_step_assign_clips``) and surface in the ``clip_assignments_*.json``
    sidecar's ``fallback_reason`` field alongside the ``retrieval_contract:
    "soft_hint"`` declaration. See ``docs/schemas/clip_assignments.md``
    "Soft hint contract" for the operator-facing summary.
    """
    def _retrieved(current_script: Script) -> list[ClipMetadata]:
        if retrieve_clips_fn is None:
            return clips_metadata
        try:
            return retrieve_clips_fn(current_script)
        except Exception as exc:  # noqa: BLE001 — defensive: retrieval
            # Sprint 12b audit L-001 fix. Retrieval-layer failures (OpenRouter
            # transient, ValueError from a degenerate query set, etc.) must
            # NOT propagate past this point — the closure's design intent is
            # already "fall back to full pool on any retrieval problem" (see
            # compile_promo._step_assign_clips). Catching here preserves that
            # intent for ANY exception type, not just the ones the closure
            # itself explicitly guards.
            logger.warning(
                "Sprint 12b retrieval closure raised %s: %s — falling back "
                "to full clips_metadata (%d clips) for this Gemini #2 attempt.",
                type(exc).__name__, exc, len(clips_metadata),
            )
            return clips_metadata

    word_ts = narration["word_timestamps"]
    pause_list = [seg.get("pause_after_ms", 0) for seg in script.get("segments", [])]
    try:
        assignments = assign_clips(
            script, word_ts, pause_list,
            _retrieved(script), clip_durations, variant_index,
        )
        return script, narration, assignments
    except ClipAssignmentError as exc:
        if regenerate_script_fn is None or regenerate_narration_fn is None:
            raise

        hint = build_tighten_hint(exc)
        logger.warning(
            "F3 retry for variant %d: Gemini #2 raised on first attempt; "
            "regenerating Gemini #1 with hint: %s",
            variant_index, hint,
        )
        new_script = regenerate_script_fn(hint)
        new_narration = regenerate_narration_fn(new_script)
        new_word_ts = new_narration["word_timestamps"]
        new_pause_list = [
            seg.get("pause_after_ms", 0) for seg in new_script.get("segments", [])
        ]
        # Second attempt — any raise here propagates (no third call).
        assignments = assign_clips(
            new_script, new_word_ts, new_pause_list,
            _retrieved(new_script), clip_durations, variant_index,
        )
        return new_script, new_narration, assignments
