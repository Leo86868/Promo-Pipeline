"""翻转二 B4 — deterministic packer (排片员): beats × rankings → assignments.

Walks each beat's ranked candidate list (``clip_retriever.rank_per_query``)
and picks the first clip that passes the house rules, in the priority
order fixed by the 2026-06-10 设计契约 (roadmap §翻转二):

1. **No reuse within the video** — validator invariant, hard.
2. **Coverage** — a free source window of at least the beat's display
   span must exist (``usable = source − trim ≥ span − TOL``), hard.
3. **Window rotation** (anti-TikTok-dedup, the rule this rebuild exists
   for) — ``trim_start`` is chosen inside a window the usage ledger says
   was never shown. A candidate with no free window is skipped; when NO
   candidate has one, the packer falls back to the least-overlap window
   (deterministic) and flags ``window_exhausted`` in provenance rather
   than failing the video — exhaustion means the POI's whole pool has
   been shown for spans this long, which is a curation problem, not a
   packing error.

   **Contract clause (2026-06-10 review)**: window exhaustion is a SOFT
   preference, never an eligibility rule. The platform's use-count gate
   (``poi_asset_valid_clips``: <3 uses = valid) is the ONLY hard
   eligibility door, enforced UPSTREAM in the pool the packer receives —
   the packer adds no second eligibility system. Hardening exhaustion
   here would mint "valid but never selectable" zombie assets.
4. **Adjacency variety** (soft) — first pass refuses a candidate whose
   category equals the previous beat's pick; when no candidate passes
   with a different category, a second pass relaxes the rule (recorded
   in provenance).
5. **Motion-phase tie-break** — within the chosen free window,
   ``dominant_motion_phase`` nudges ``trim_start`` toward the clip's
   action (late → window end, mid → centre, else window start). Never
   overrides rule 3.

Output is RAW assignments (``segment / clip_id / start_word_idx /
end_word_idx / trim_start``) — the caller MUST run them through
``_enforce_hard_constraint_and_enrich``; the validator stays the single
arbiter of the renderer contract (fail loud on packer bugs).

Span math is the validator's peek-ahead formula via
``beat display span = next beat's first word start − this beat's first
word start`` (last beat → narration_end). No LLM, no network, no
randomness — same inputs, same output, forever.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from promo.core.assign.clip_assignment_validator import HARD_CONSTRAINT_TOL_SEC
from promo.core.assign.usage_windows import UsedWindow, free_windows
from promo.core.errors import ClipAssignmentError
from promo.core.schema import WordTimestamp

logger = logging.getLogger(__name__)


def _beat_spans(
    beats: list[dict], word_timestamps: list[WordTimestamp],
) -> list[float]:
    """Peek-ahead display span per beat (validator semantics)."""
    spans: list[float] = []
    narration_end = (
        float(word_timestamps[-1].get("end", 0.0)) if word_timestamps else 0.0
    )
    for i, beat in enumerate(beats):
        t0 = float(word_timestamps[int(beat["start_word_idx"])]["start"])
        if i + 1 < len(beats):
            t1 = float(word_timestamps[int(beats[i + 1]["start_word_idx"])]["start"])
        else:
            t1 = narration_end
        spans.append(max(0.0, t1 - t0))
    return spans


def _phase_trim(window: UsedWindow, span: float, phase: str) -> float:
    """Rule 5: place the span inside the free window per motion phase."""
    slack = (window.end_sec - window.start_sec) - span
    if slack <= 0:
        return window.start_sec
    label = (phase or "").lower()
    if "late" in label or "end" in label or "climax" in label:
        return window.start_sec + slack
    if "mid" in label or "peak" in label:
        return window.start_sec + slack / 2.0
    return window.start_sec


def _least_overlap_trim(
    source_duration: float, span: float, used: list[UsedWindow],
) -> float:
    """Rule 3 fallback: trim minimizing overlap with used windows.
    Deterministic: scans candidate starts at used-window edges + 0."""
    candidates = {0.0}
    for w in used:
        candidates.add(min(max(0.0, w.end_sec), max(0.0, source_duration - span)))
        candidates.add(min(max(0.0, w.start_sec - span), max(0.0, source_duration - span)))

    def overlap(trim: float) -> float:
        lo, hi = trim, trim + span
        return sum(
            max(0.0, min(hi, w.end_sec) - max(lo, w.start_sec)) for w in used
        )

    # Sort for determinism: least overlap, then earliest trim.
    valid = [t for t in candidates if t >= 0 and t + span <= source_duration + HARD_CONSTRAINT_TOL_SEC]
    if not valid:
        return 0.0
    return min(valid, key=lambda t: (overlap(t), t))


def unit_vector(vec: list[float] | tuple[float, ...] | None) -> list[float] | None:
    """L2-normalize an embedding; None for missing/zero-norm (uncomparable)."""
    if not vec:
        return None
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm == 0.0:
        return None
    return [float(x) / norm for x in vec]


def whiten_visual_pool(
    meta_by_id: dict[str, dict],
) -> dict[str, list[float]]:
    """Build {clip_id: whitened-unit-vector} from each clip's ``visual_embedding``.

    Consumer-side whitening per the AIGC handoff: mean-center over THIS POI's
    visual-vector pool, then L2-normalize. The near-dup gate keys off the
    VISUAL modality only — a clip without a ``visual_embedding`` is omitted
    here, so the gate fails open for it (cannot block what it cannot compare).
    The text ``embedding`` is NEVER used as a fallback (wrong modality).

    Mean-centering preserves twins (identical vectors stay identical after
    centering → cosine 1.0). Empty pool → ``{}``.
    """
    pool = {
        cid: [float(x) for x in m["visual_embedding"]]
        for cid, m in meta_by_id.items()
        if m.get("visual_embedding")
    }
    if not pool:
        return {}
    dim = len(next(iter(pool.values())))
    mean = [0.0] * dim
    for vec in pool.values():
        for j, x in enumerate(vec):
            mean[j] += x
    n = len(pool)
    mean = [s / n for s in mean]
    units: dict[str, list[float]] = {}
    for cid, vec in pool.items():
        u = unit_vector([x - mean[j] for j, x in enumerate(vec)])
        if u is not None:
            units[cid] = u
    return units


def max_cosine_to_chosen(
    cand_unit: list[float] | None, chosen_units: list[list[float]],
) -> float:
    """Largest cosine of a candidate (unit vec) against already-chosen clips.

    Pure predicate shared by the production gate AND the offline simulate
    harness, so both judge near-duplication identically. Inputs are
    PRE-NORMALIZED unit vectors (cosine = dot). Returns 0.0 when the
    candidate has no embedding (gate then cannot fire — fail-open, never
    blocks a clip we cannot compare).
    """
    if cand_unit is None or not chosen_units:
        return 0.0
    return max(
        sum(a * b for a, b in zip(cand_unit, uc)) for uc in chosen_units
    )


def pack_clips(
    beats: list[dict],
    rankings: list[list[tuple[str, float]]],
    *,
    word_timestamps: list[WordTimestamp],
    clip_durations: dict[str, float],
    clip_metadata: list[dict],
    max_beat_sec: float,
    used_windows: dict[str, list[UsedWindow]] | None = None,
    clip_to_asset: dict[str, str] | None = None,
    near_dup_threshold: float | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Pick one clip per beat. Returns ``(raw_assignments, provenance)``.

    ``max_beat_sec`` is the format card's ``pacing.beat_max_sec`` — used
    only to flag over-ceiling beats in provenance, same bound the
    planner cut against.

    ``used_windows`` keys by asset_id (``usage_windows.fetch_used_windows``);
    ``clip_to_asset`` maps local clip ids to platform asset ids — its keys
    MUST be ``zfill(4)``-normalized (the caller's contract; ``_assign_
    clips_packer`` normalizes when building it). Clips without a mapping
    (local dev) are treated as never shown.

    ``near_dup_threshold`` (DEFAULT None = OFF) enables the within-video
    near-duplicate soft gate: a candidate whose VISUAL-embedding cosine to
    ANY already-chosen clip is ``>= threshold`` is skipped in favour of the
    next-ranked clip. The gate compares on the whitened ``visual_embedding``
    (DINOv2), NOT the text ``embedding`` used for ranking — recommended
    armed value 0.85 (visual-cosine scale). SOFT/fail-soft — when a beat's
    whole ranking is gated out, a final relax pass allows the near-dup rather
    than failing the video (recorded in provenance ``diversity_relaxed_beats``).
    When None, behaviour is byte-identical to the pre-gate packer. The gate is
    fail-open per clip: a candidate without a visual embedding is never blocked.
    """
    if len(beats) != len(rankings):
        raise ValueError(
            f"beats ({len(beats)}) and rankings ({len(rankings)}) disagree"
        )
    used_windows = used_windows or {}
    clip_to_asset = clip_to_asset or {}
    meta_by_id = {str(m.get("id", "")).zfill(4): m for m in clip_metadata}
    spans = _beat_spans(beats, word_timestamps)

    assignments: list[dict] = []
    provenance: dict[str, Any] = {
        "assigner": "packer",
        "window_exhausted_beats": [],
        "adjacency_relaxed_beats": [],
        "picks": [],
    }
    # New gate-only provenance keys are added ONLY when armed, so the
    # serialized sidecar stays byte-identical with the gate off.
    if near_dup_threshold is not None:
        provenance["diversity_skipped_beats"] = []
        provenance["diversity_relaxed_beats"] = []
        provenance["near_dup_threshold"] = near_dup_threshold
    seen: set[str] = set()
    prev_category: str | None = None

    # Build the gate's comparison vectors once per video, only when armed.
    # The near-dup gate judges VISUAL similarity: it keys off the separate
    # ``visual_embedding`` (DINOv2, attached upstream), whitened consumer-side
    # (pool mean-center + L2). NEVER the text ``embedding`` — text cosine is
    # blind to visual twins. Clips without a visual vector are absent here →
    # fail-open (the gate cannot fire for them).
    unit_by_id: dict[str, list[float]] = {}
    if near_dup_threshold is not None:
        unit_by_id = whiten_visual_pool(meta_by_id)
    chosen_units: list[list[float]] = []

    for beat_i, (beat, ranking, span) in enumerate(zip(beats, rankings, spans)):
        chosen: dict[str, Any] | None = None
        # Relax ladder: near-dup diversity is the SOFTEST constraint —
        # relaxed last, only when the whole ranking is otherwise gated out.
        # Gate off → diversity_passes == (False,), collapsing to the
        # original adjacency two-pass (byte-identical).
        diversity_passes = (
            (True, False) if near_dup_threshold is not None else (False,)
        )
        diversity_skipped_here = False
        for enforce_diversity in diversity_passes:
            for enforce_adjacency in (True, False):
                for clip_id, score in ranking:
                    norm = str(clip_id).zfill(4)
                    if norm in seen:
                        continue  # rule 1
                    if norm not in clip_durations:
                        continue
                    source = float(clip_durations[norm])
                    if source + HARD_CONSTRAINT_TOL_SEC < span:
                        continue  # rule 2: cannot cover at any trim
                    meta = meta_by_id.get(norm, {})
                    category = str(meta.get("category") or "")
                    if enforce_adjacency and prev_category and category == prev_category:
                        continue  # rule 4 first pass
                    if (
                        enforce_diversity
                        and near_dup_threshold is not None
                        and max_cosine_to_chosen(
                            unit_by_id.get(norm), chosen_units,
                        ) >= near_dup_threshold
                    ):
                        diversity_skipped_here = True
                        continue  # near-dup soft gate (insertion point A)
                    used = used_windows.get(clip_to_asset.get(norm, ""), [])
                    gaps = free_windows(source, used, min_len_sec=span)
                    if gaps:
                        trim = _phase_trim(
                            gaps[0], span, str(meta.get("dominant_motion_phase") or ""),
                        )
                        exhausted = False
                    else:
                        continue  # rule 3: prefer ANY candidate with a free window
                    chosen = {
                        "clip_id": norm, "score": score, "trim": trim,
                        "category": category, "exhausted": exhausted,
                    }
                    break
                if chosen:
                    if not enforce_adjacency:
                        provenance["adjacency_relaxed_beats"].append(beat_i)
                    break
            if chosen:
                if not enforce_diversity and near_dup_threshold is not None:
                    provenance["diversity_relaxed_beats"].append(beat_i)
                break
        if diversity_skipped_here:
            provenance["diversity_skipped_beats"].append(beat_i)
        if chosen is None:
            # Rule 3 fallback: every coverable candidate's source is
            # exhausted — take the least-overlap window on the best-ranked
            # coverable, unused clip instead of failing the video.
            for clip_id, score in ranking:
                norm = str(clip_id).zfill(4)
                if norm in seen or norm not in clip_durations:
                    continue
                source = float(clip_durations[norm])
                if source + HARD_CONSTRAINT_TOL_SEC < span:
                    continue
                used = used_windows.get(clip_to_asset.get(norm, ""), [])
                chosen = {
                    "clip_id": norm, "score": score,
                    "trim": _least_overlap_trim(source, span, used),
                    "category": str(meta_by_id.get(norm, {}).get("category") or ""),
                    "exhausted": True,
                }
                provenance["window_exhausted_beats"].append(beat_i)
                logger.warning(
                    "packer: source windows exhausted for beat %d "
                    "(span %.2fs) — least-overlap fallback on clip %s",
                    beat_i, span, norm,
                )
                break
        if chosen is None:
            raise ClipAssignmentError(
                segment_index=int(beat["segment"]),
                phrase_index=1 + sum(
                    1 for b in beats[:beat_i] if b["segment"] == beat["segment"]
                ),
                required_span=span,
                actual_max_usable=0.0,
                clip_id="<packer: no coverable unused candidate>",
            )

        seen.add(chosen["clip_id"])
        if near_dup_threshold is not None:
            chosen_unit = unit_by_id.get(chosen["clip_id"])
            if chosen_unit is not None:
                chosen_units.append(chosen_unit)
        prev_category = chosen["category"] or prev_category
        assignments.append({
            "segment": int(beat["segment"]),
            "clip_id": chosen["clip_id"],
            "start_word_idx": int(beat["start_word_idx"]),
            "end_word_idx": int(beat["end_word_idx"]),
            "trim_start": round(float(chosen["trim"]), 3),
        })
        score = float(chosen["score"])
        provenance["picks"].append({
            "beat": beat_i,
            "clip_id": chosen["clip_id"],
            # -inf cosine scores (zero-norm embeddings) would serialize as
            # `-Infinity` — invalid JSON for the sidecar. None instead.
            "score": round(score, 4) if math.isfinite(score) else None,
            "trim_start": round(float(chosen["trim"]), 3),
            "window_exhausted": chosen["exhausted"],
        })

    # Clip-burn observability (2026-06-10 review): semantic-first beats
    # consume more clips per video than time-grid beats; these counts let
    # production data answer how much faster the 3-use caps fill up.
    provenance["beat_count"] = len(beats)
    provenance["unique_clip_count"] = len(seen)
    # Review blocking #3 companion: over-ceiling beats (long authored
    # pauses / seatbelt stretch) are visible in the sidecar, not just logs.
    provenance["overlong_beats"] = [
        i for i, s in enumerate(spans)
        if s > max_beat_sec + HARD_CONSTRAINT_TOL_SEC
    ]
    return assignments, provenance
