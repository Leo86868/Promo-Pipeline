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

logger = logging.getLogger(__name__)

# Hard-constraint slack. The display-span ≤ usable-footage check allows up
# to 50 ms of tolerance because word_timestamps are measured to ~10 ms
# precision and clip source durations are ffprobed to ~1 ms; a tighter
# tolerance would make legitimate assignments fail on measurement noise.
HARD_CONSTRAINT_TOL_SEC = 0.05


# ---------------------------------------------------------------------------
#  Display-span math helpers
# ---------------------------------------------------------------------------

def _phrase_display_span_sec(
    this_start_word_idx: int,
    next_start_word_idx: int | None,
    word_timestamps: list[WordTimestamp],
) -> float:
    """Compute the visual duration a clip must cover for a phrase, using
    the peek-ahead formula that matches the renderer's bind-time span math
    (``_bind_clips_to_narration`` in ``remotion_renderer.py``) for
    middle phrases.

    The span is the gap from this phrase's first word's start to the NEXT
    phrase's first word's start in the delivered TTS timeline. Because
    ``word_timestamps`` is produced by TTS and back-allocated onto the
    concatenated audio, this gap naturally includes both inter-phrase TTS
    drift AND inter-segment authored silence — no separate pause term is
    needed (the pre-Sprint-10.5 formula's authored-silence kwarg was
    retired with this rewrite).

    For the VERY LAST phrase in emission order (``next_start_word_idx is
    None``), the span runs to ``word_timestamps[-1].end`` (narration end),
    NOT to ``final_display_end = max(target, narration_end)``. The
    target-extension buffer (``target_duration_sec − narration_end``) is
    explicitly renderer territory: the renderer extends the last clip
    past narration_end and — when clip source runs out — inserts bridge
    clips from the unused-clip pool to cover the remaining tail. Bridges
    are canonical silence-fill infrastructure per the Sprint 10b
    amendment. If the assigner tied the last-phrase hard-constraint to
    ``final_display_end`` instead, it would force the last clip to
    cover what the bridge mechanism is explicitly designed to handle,
    producing unrecoverable ``ClipAssignmentError`` raises for any
    narration whose final phrase starts more than ``pool_max`` seconds
    before ``target_duration_sec`` (Sprint 10.5 C1.2, 2026-04-18).
    """
    this_start = float(word_timestamps[this_start_word_idx]["start"])
    if next_start_word_idx is None:
        narration_end = (
            float(word_timestamps[-1].get("end", 0.0)) if word_timestamps else 0.0
        )
        span = narration_end - this_start
    else:
        span = float(word_timestamps[next_start_word_idx]["start"]) - this_start
    return max(0.0, span)


def _segment_phrase_layout(
    raw_assignments: list[ClipAssignment],
) -> dict[int, list[ClipAssignment]]:
    """Group raw assignments by segment index, preserving input order."""
    layout: dict[int, list[ClipAssignment]] = {}
    for entry in raw_assignments:
        layout.setdefault(int(entry["segment"]), []).append(entry)
    return layout


# ---------------------------------------------------------------------------
#  Hard-constraint enforcement
# ---------------------------------------------------------------------------

def _enforce_hard_constraint_and_enrich(
    raw_assignments: list[ClipAssignment],
    script: Script,
    word_timestamps: list[WordTimestamp],
    clip_durations: dict[str, float],
) -> list[ClipAssignment]:
    """Walk raw Gemini #2 assignments in-order, compute
    ``display_span_sec`` + ``source_duration_sec`` per phrase, and raise
    :class:`ClipAssignmentError` on the first constraint violation.

    Sprint 10.5 formula (peek-ahead, C1.2-amended): each phrase's span
    runs from THIS phrase's first word's start to the NEXT phrase's
    first word's start in emission order. Inter-segment silence is
    naturally included because the next segment's first word lands after
    the authored pause in the TTS-delivered timeline. The VERY LAST
    phrase's span runs to ``word_timestamps[-1].end`` (narration end) —
    NOT to ``final_display_end``. The target-extension buffer between
    ``narration_end`` and ``target_duration_sec`` is renderer territory:
    bridges (canonical silence-fill per the 10b amendment) fill any
    tail overflow when the last clip's source runs out past
    narration_end. Tying the last-phrase hard-constraint to the
    target-extension would force the assigner to treat
    ``FreezeWouldOccurError`` territory as ``ClipAssignmentError``
    territory, which breaks the 10b amendment.

    Returns the enriched list on success.
    """
    if not raw_assignments:
        raise ClipAssignmentError(
            segment_index=0,
            phrase_index=0,
            required_span=0.0,
            actual_max_usable=0.0,
            clip_id="<empty>",
        )

    script_segments = script.get("segments", [])
    segment_count = len(script_segments)
    layout = _segment_phrase_layout(raw_assignments)
    enriched: list[ClipAssignment] = []
    seen_clip_ids: set[str] = set()

    # Emission-order peek-ahead: flatten phrases across segments so the
    # last phrase of segment S sees the first phrase of segment S+1 as its
    # successor. Entry references stay live so downstream lookups work.
    sorted_seg_indices = sorted(layout.keys())
    flat_entries: list[ClipAssignment] = []
    for _seg_idx in sorted_seg_indices:
        flat_entries.extend(layout[_seg_idx])

    # L-001 fix: Gemini #2 must emit assignments for EVERY script segment.
    # Without this guard a truncated response silently produces a partial
    # assignments list that the renderer would consume downstream.
    missing_segments = [i + 1 for i in range(segment_count) if (i + 1) not in layout]
    if missing_segments:
        raise ClipAssignmentError(
            segment_index=missing_segments[0],
            phrase_index=0,
            required_span=0.0,
            actual_max_usable=0.0,
            clip_id=f"<no assignment for segments: {missing_segments}>",
        )

    # L-003 fix: precompute per-segment global word boundaries so we can
    # verify that phrases tile their segment end-to-end (the prompt's
    # stated invariant). Word indices here are into the GLOBAL
    # word_timestamps list — same as Gemini #2's output uses.
    segment_word_ranges: list[tuple[int, int]] = []
    cursor = 0
    for seg in script_segments:
        text = (seg.get("text") or "")
        n = len(text.split())
        if n == 0:
            # Defensive: a segment with zero words cannot tile anything.
            segment_word_ranges.append((cursor, cursor - 1))
        else:
            segment_word_ranges.append((cursor, cursor + n - 1))
        cursor += n

    emission_pos = 0
    for segment_index in sorted_seg_indices:
        phrases = layout[segment_index]
        # Zero-indexed position of THIS segment in the script segments list
        # (contract uses 1-based "segment" field).
        seg_pos = segment_index - 1
        if seg_pos < 0 or seg_pos >= segment_count:
            raise ClipAssignmentError(
                segment_index=segment_index,
                phrase_index=1,
                required_span=0.0,
                actual_max_usable=0.0,
                clip_id=str(phrases[0].get("clip_id", "<missing>")),
            )

        # L-003 fix: verify phrases tile this segment end-to-end with no
        # gaps or overlaps (the Gemini #2 prompt's stated invariant). Word
        # indices must be contiguous: first phrase starts at segment_start,
        # each subsequent phrase starts at prev.end + 1, last phrase ends
        # at segment_end.
        seg_word_lo, seg_word_hi = segment_word_ranges[seg_pos]
        prev_end = seg_word_lo - 1  # so phrase 1 must start at seg_word_lo
        for phrase_i, entry in enumerate(phrases, start=1):
            try:
                pi_start = int(entry["start_word_idx"])
                pi_end = int(entry["end_word_idx"])
            except (KeyError, TypeError, ValueError):
                # Detailed per-field raise happens below; skip tiling check.
                break
            if pi_start != prev_end + 1:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=str(entry.get("clip_id", "<tiling>"))
                    + f" (expected start_word_idx {prev_end + 1}, got {pi_start})",
                )
            prev_end = pi_end
        if phrases and prev_end != seg_word_hi:
            raise ClipAssignmentError(
                segment_index=segment_index,
                phrase_index=len(phrases),
                required_span=0.0,
                actual_max_usable=0.0,
                clip_id=f"<tiling: last phrase ends at {prev_end}, "
                f"segment ends at {seg_word_hi}>",
            )

        for phrase_i, entry in enumerate(phrases, start=1):
            clip_id = str(entry.get("clip_id", ""))
            if not clip_id:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id="<missing>",
                )
            # Normalize before dedup AND inventory lookup so Gemini
            # emitting "7" and "0007" for the same physical clip cannot
            # slip through the "each clip_id appears AT MOST ONCE"
            # invariant (Sprint 10b post-close audit-fix 2026-04-18,
            # logic-auditor L-002 + Codex re-review catch). zfill(4)
            # matches the canonical form the renderer + clip inventory
            # use (``compile_promo._step_prepare_clips`` keys
            # ``clip_durations`` via ``clip_paths`` which is built from
            # zero-padded filenames). Downstream ``clip_durations``
            # lookups must use ``clip_id_norm`` or a Gemini-emitted
            # unpadded ID will raise "missing from inventory" even
            # when the padded form IS present in the pool.
            clip_id_norm = clip_id.zfill(4)
            if clip_id_norm in seen_clip_ids:
                # Reuse across phrases is structurally disallowed.
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} (duplicate)",
                )
            seen_clip_ids.add(clip_id_norm)

            if clip_id_norm not in clip_durations:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} (missing from inventory)",
                )

            try:
                start_idx = int(entry["start_word_idx"])
                end_idx = int(entry["end_word_idx"])
                trim_start = float(entry.get("trim_start", 0.0))
            except (KeyError, TypeError, ValueError) as exc:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} ({exc})",
                )

            if (
                start_idx < 0 or end_idx < start_idx
                or end_idx >= len(word_timestamps)
            ):
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=0.0,
                    actual_max_usable=0.0,
                    clip_id=f"{clip_id} (bad word indices [{start_idx},{end_idx}])",
                )

            # Peek-ahead across segment boundaries via the flat emission-order
            # list. The last phrase of segment S sees segment S+1's first
            # phrase's first word as its successor; the very last phrase of
            # the last segment passes None and uses ``word_timestamps[-1].end``
            # (narration_end) — C1.2 amendment; target-extension past
            # narration_end is renderer bridge territory.
            # Audit-fix L-010 (2026-04-18): assert the emission-order invariant
            # before indexing flat_entries, so a silent drift between the outer
            # loop's traversal and the flat list can't produce wrong peek-ahead.
            assert flat_entries[emission_pos] is entry, (
                f"emission_pos drift at segment {segment_index} phrase {phrase_i}"
            )
            if emission_pos + 1 < len(flat_entries):
                next_entry = flat_entries[emission_pos + 1]
                try:
                    next_start_idx: int | None = int(next_entry["start_word_idx"])
                except (KeyError, TypeError, ValueError) as exc:
                    # Audit-fix L-009 (2026-04-18): a malformed successor entry
                    # would silently promote this phrase to "last phrase" status
                    # and inflate its span via narration_end fallback. Raise
                    # instead so the structural error surfaces where it lives.
                    raise ClipAssignmentError(
                        segment_index=int(next_entry.get("segment", segment_index + 1)),
                        phrase_index=1,
                        required_span=0.0,
                        actual_max_usable=0.0,
                        clip_id=(
                            f"{next_entry.get('clip_id', '<missing>')} "
                            f"(malformed start_word_idx: {exc})"
                        ),
                    )
            else:
                next_start_idx = None
            emission_pos += 1

            display_span_sec = _phrase_display_span_sec(
                start_idx, next_start_idx, word_timestamps,
            )
            source_duration_sec = float(clip_durations[clip_id_norm])
            usable = source_duration_sec - trim_start

            if trim_start < 0 or (usable + HARD_CONSTRAINT_TOL_SEC) < display_span_sec:
                raise ClipAssignmentError(
                    segment_index=segment_index,
                    phrase_index=phrase_i,
                    required_span=display_span_sec,
                    actual_max_usable=usable,
                    clip_id=clip_id,
                )

            enriched.append(ClipAssignment(
                segment=segment_index,
                clip_id=clip_id,
                start_word_idx=start_idx,
                end_word_idx=end_idx,
                trim_start=trim_start,
                display_span_sec=display_span_sec,
                source_duration_sec=source_duration_sec,
            ))

    return enriched


# ---------------------------------------------------------------------------
#  Gemini #2 prompt + API call
# ---------------------------------------------------------------------------

def _format_phrase_timing_block(
    script: Script,
    word_timestamps: list[WordTimestamp],
    pause_after_ms_per_segment: list[int],
    profile: PromoFormatProfile,
) -> str:
    """Render the narration-with-timing block Gemini #2 reads to plan
    phrase boundaries. Word indices here are the same GLOBAL indices
    Gemini #2 must echo back in its response.
    """
    lines: list[str] = []
    cursor = 0
    segments = script.get("segments", [])
    for seg_i, seg in enumerate(segments):
        text = seg.get("text", "") or ""
        seg_words = text.split()
        seg_pos = seg_i  # 0-indexed
        seg_num = seg.get("segment", seg_i + 1)
        pause_ms = 0
        if seg_pos < len(pause_after_ms_per_segment):
            raw_pause = pause_after_ms_per_segment[seg_pos]
            try:
                pause_ms = int(raw_pause) if raw_pause is not None else 0
            except (TypeError, ValueError):
                pause_ms = 0
        # Per-segment phrase count range from profile. Neither IndexError
        # (script has more segments than the profile declares) nor
        # AttributeError (SegmentPlan drifted off its ``clip_range``
        # contract) is silently absorbed: either is a real data
        # mismatch that would render a misleading "1-4 clips" prompt
        # while Gemini #2 is planning against different bounds. Sprint
        # 10b post-close audit-fix 2026-04-18 closed the AttributeError
        # axis; this block also raises on IndexError for the same
        # reason — letting the caller see the failure surface instead
        # of a silent fallback.
        clip_range_display = profile.segment_plans[seg_pos].clip_range_display

        is_last_segment = (seg_pos == len(segments) - 1)
        trailing_desc = (
            f"{pause_ms}ms pause follows" if (pause_ms > 0 and not is_last_segment)
            else "no trailing pause"
        )
        lines.append(
            f"Segment {seg_num} ({clip_range_display}; {trailing_desc}):"
        )
        for word_i, word in enumerate(seg_words):
            global_idx = cursor + word_i
            if global_idx < len(word_timestamps):
                wt = word_timestamps[global_idx]
                start = float(wt.get("start", 0.0))
                end = float(wt.get("end", 0.0))
                lines.append(
                    f"  [{global_idx}] {word!r:<20s} start={start:.3f} end={end:.3f}"
                )
            else:
                # Defensive — TTS did not produce a timestamp for this index.
                lines.append(f"  [{global_idx}] {word!r} (no timestamp)")
        cursor += len(seg_words)
    return "\n".join(lines)


def _build_gemini2_prompt(
    script: Script,
    word_timestamps: list[WordTimestamp],
    pause_after_ms_per_segment: list[int],
    clips_metadata: list[ClipMetadata],
    variant_index: int,
    profile: PromoFormatProfile,
    *,
    target_duration_sec: float | None = None,
) -> str:
    """Construct the Gemini #2 clip-assignment prompt.

    Audit-fix L-011 (2026-04-18): ``target_duration_sec`` is accepted
    explicitly so the caller (``assign_clips``) can pass the same value
    it already resolved for profile selection, avoiding a redundant
    re-derivation that could silently diverge if the defaults differ.
    Falls back to ``script["target_duration_sec"] or 65.0`` for
    backwards compat with any caller that doesn't pass it.
    """
    poi_name = script.get("poi_name", "?")
    location = script.get("location", "?")
    if target_duration_sec is None:
        target_duration_sec = float(script.get("target_duration_sec") or 65.0)
    else:
        target_duration_sec = float(target_duration_sec)
    timing_block = _format_phrase_timing_block(
        script, word_timestamps, pause_after_ms_per_segment, profile,
    )
    inventory = _format_clip_inventory(clips_metadata, duration_precision=2)

    # Precomputed for prompt clarity — eliminates Gemini arithmetic slips
    # on the last-phrase edge case. Sprint 10.5 C1.2: the last phrase's
    # ceiling is narration_end, not final_display_end; the renderer's
    # bridge mechanism handles the target-extension buffer.
    narration_end = (
        float(word_timestamps[-1].get("end", 0.0)) if word_timestamps else 0.0
    )
    pool_max_source_dur = max(
        (float(c.get("source_duration_sec") or 0.0) for c in clips_metadata),
        default=0.0,
    )

    segments = script.get("segments", [])
    seg_range_lines: list[str] = []
    for seg_i, seg in enumerate(segments):
        # Neither IndexError nor AttributeError caught — see the twin
        # block in :func:`_format_phrase_timing_block` for the rationale
        # (Sprint 10b post-close audit-fix 2026-04-18).
        lo, hi = profile.segment_plans[seg_i].clip_range
        seg_range_lines.append(
            f"  Segment {seg.get('segment', seg_i + 1)}: {lo}-{hi} phrases (and thus {lo}-{hi} clips)"
        )
    seg_ranges = "\n".join(seg_range_lines)

    # Per-segment global word-index boundaries — tells Gemini exactly
    # which global word indices belong to each segment so peek-ahead
    # across segment boundaries is unambiguous.
    # Audit-fix L-008 (2026-04-18): use len(text.split()) — the same
    # source the enforcer's ``segment_word_ranges`` uses
    # (``_enforce_hard_constraint_and_enrich`` line ~191) — so the
    # prompt's boundary table cannot silently diverge from what the
    # enforcer validates against. seg["word_count"] is an author-declared
    # field that may drift from the actual tokenization of seg["text"].
    seg_word_boundaries: list[str] = []
    cursor = 0
    for seg_i, seg in enumerate(segments):
        text = seg.get("text") or ""
        n = len(text.split())
        if n <= 0:
            continue
        seg_word_boundaries.append(
            f"  Segment {seg.get('segment', seg_i + 1)}: words [{cursor}..{cursor + n - 1}]"
        )
        cursor += n
    seg_word_boundaries_str = "\n".join(seg_word_boundaries)

    # Sprint Arsenal Externalization (Commit 4): the prompt body lives in
    # ``promo/arsenal/system_prompts/gemini2_assign_v1.md``. Caller
    # pre-formats the 3 floats (`target_duration_sec`, `narration_end`,
    # `pool_max_source_dur`) so the MD template can stay flat
    # ``$identifier`` substitution — `string.Template` doesn't honour
    # f-string `:.3f` formatting natively. Sprint 10b post-close audit
    # arithmetic-slip regression test pins the 5 invariant substrings
    # below (PRECOMPUTED CONSTANTS / ARITHMETIC CHECK / "the last
    # phrase's constraint uses the last word's end, NOT
    # target_duration_sec") — these came across byte-identically in the
    # MD migration.
    template = Template(arsenal_loader.load_system_prompt("gemini2_assign"))
    return template.substitute(
        poi_name=poi_name,
        location=location,
        target_duration_sec=f"{target_duration_sec:.3f}",
        narration_end=f"{narration_end:.3f}",
        pool_max_source_dur=f"{pool_max_source_dur:.3f}",
        seg_word_boundaries_str=seg_word_boundaries_str,
        timing_block=timing_block,
        variant_index=variant_index,
        seg_ranges=seg_ranges,
        inventory=inventory,
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


def _parse_gemini2_json(text: str) -> list[ClipAssignment]:
    """Parse Gemini #2's response text as JSON. The expected shape is a
    top-level list of phrase-assignment dicts; some Gemini runs wrap the
    list under a key (``assignments`` / ``phrases`` / ``clips``), which
    is unwrapped here. Strips ```json``` fences first.

    Raises :class:`ValueError` on malformed JSON or unrecognizable shape.
    Distinct from :func:`promo.core.llm.json_response.parse_json_response` because
    that helper forbids top-level lists by design.
    """
    cleaned = text.strip()
    match = _FENCE_RE.match(cleaned)
    if match:
        cleaned = match.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini #2 JSON parse failure: {exc}. "
            f"Response (first 200 chars): {text[:200]!r}"
        ) from exc
    if isinstance(parsed, dict):
        for candidate_key in ("assignments", "phrases", "clips"):
            val = parsed.get(candidate_key)
            if isinstance(val, list):
                return val
        raise ValueError(
            f"Gemini #2 returned a dict without a recognizable list key; "
            f"keys={list(parsed.keys())}"
        )
    if not isinstance(parsed, list):
        raise ValueError(
            f"Gemini #2 response is not a list: {type(parsed).__name__}"
        )
    return parsed


def _call_gemini2(prompt: str) -> list[ClipAssignment]:
    """Call Gemini with the clip-assignment prompt. Returns parsed JSON.

    Separated out for test-patchability (tests patch this at the module
    level to inject fixture responses without touching the real API).
    """
    model = resolve_gemini_model(log_context="Gemini #2")

    def _call() -> Any:
        response = model.generate_content(
            prompt,
            generation_config={  # type: ignore[arg-type]
                "temperature": 0.35,
                "top_p": 0.9,
                # Raised from 10000 — Sprint 10.5 C1.1 prompt-hardening adds
                # a precomputed-constants block + segment-boundary table +
                # arithmetic-check step that makes Gemini 2.5-pro do more
                # thinking before emitting JSON. Observed first re-capture
                # crashed with finish_reason=2 (MAX_TOKENS). Per the
                # project's "Gemini token budget not a constraint" memory.
                "max_output_tokens": 32000,
            },
        )
        return _parse_gemini2_json(response.text)

    return retry_with_backoff(_call, max_retries=2, base_delay=2.0)


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


def load_latest_clip_assignments(
    poi_slug: str,
    duration_sec: float,
    sidecar_search_dirs: Iterable[str | os.PathLike],
) -> list | None:
    """Return the most-recent-by-mtime variants-list payload from
    ``clip_assignments_{slug}_{round(duration_sec)}s.json`` across
    ``sidecar_search_dirs``, or ``None`` if nothing matches or every
    candidate payload is malformed.

    Sprint 10 C3 (F1 invariant): the writer in ``compile_promo.py`` and
    this reader are paired. Collision-bumped suffixes (``-2.json``,
    ``-3.json``, ...) produced by ``_write_sidecar`` are covered by the
    glob so a second run in the same output dir doesn't leave the reader
    pointing at stale data. Directly mirrors ``pause_budget.load_calibrated_wpm``
    by design — that 09b C8 fix is the template (see Sprint 09b L-002).

    Sprint 13 AC19 (D-004): the sidecar schema extended from a bare
    variants list to a top-level dict with retrieval provenance
    (``retrieval_active`` / ``embedded_pool_size`` / ``reduced_pool_size``
    / ``mimo_prompt_sha1`` / ``fallback_reason``) plus a ``variants``
    key. This reader tolerates both shapes — old Sprint 12b sidecars
    (bare list) and new Sprint 13 sidecars (dict with ``variants``) — so
    fixture-replay tests against committed 12b sidecars remain green.
    The reader always returns the variants list; the provenance fields
    are accessed via a separate reader if a caller needs them.

    Malformed sidecars (missing keys, unreadable JSON, unexpected shape)
    are skipped silently so one bad file in a search dir does not mask a
    good one; the reader returns the first valid payload under mtime
    ordering.
    """
    target_dur = int(round(duration_sec))
    target_stem = f"clip_assignments_{poi_slug}_{target_dur}s"
    match_exact = f"{target_stem}.json"
    match_bumped = f"{target_stem}-*.json"

    candidates: list[tuple[float, Path]] = []
    for d in sidecar_search_dirs:
        if d is None:
            continue
        base = Path(d)
        if not base.exists() or not base.is_dir():
            continue
        matched: list[Path] = []
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

    candidates.sort(reverse=True)
    for _mtime, path in candidates:
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        # Sprint 13 AC19: unwrap the new shape into the variants list.
        # Old shape (Sprint 12b and earlier): bare `[...]`.
        # New shape (Sprint 13+): `{"variants": [...], "retrieval_active": ...}`.
        if isinstance(payload, dict):
            variants = payload.get("variants")
        elif isinstance(payload, list):
            variants = payload
        else:
            continue
        if not isinstance(variants, list):
            continue
        # Light schema validation — the variants list holds per-variant
        # entries; each must be a dict with an 'assignments' list inside.
        # If a sidecar was written by an older schema or got corrupted,
        # skip it and try the next candidate.
        if not all(
            isinstance(entry, dict) and isinstance(entry.get("assignments"), list)
            for entry in variants
        ):
            continue
        return variants

    return None


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
