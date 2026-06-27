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


# Default soft-penalty weights for the global-assignment path (armed only).
# ADJACENCY 0.05 and NEAR_DUP 0.50 are tuned on the run3x3-equivalent offline
# sweep (research_global_tune.py): at 0.05 the iterative adjacency penalty cuts
# same-category-back-to-back ~45% while *raising* median text-cosine and holding
# dramatic misses at 0. The near-dup weight is on the same cost scale (text
# cosine ≈ [-0.1, 0.7]); 0.50 is a strong-but-soft push that displaces a visual
# twin only when an alternative is within ~0.5 cosine — never fails the video.
GLOBAL_ADJACENCY_PENALTY = 0.05
GLOBAL_NEAR_DUP_PENALTY = 0.50
# claim-1 (window-freshness soft cost): a (beat, clip) pairing with NO free
# source window long enough for the beat's span pays this soft penalty — same
# cost scale as text cosine (≈[-0.1, 0.7]); 0.10 nudges the solver toward a
# clip that still has fresh footage when one exists, but is SOFT (never
# `_INFEASIBLE`) so window exhaustion stays the soft contract (packer.py:19-24).
GLOBAL_STALE_WINDOW_PENALTY = 0.10
# Coverage-infeasible / forbidden cell cost. Large enough that the solver never
# prefers an infeasible cell over any real assignment, but finite so a fully
# infeasible row still yields a (flagged) pick instead of crashing the solve.
_INFEASIBLE_COST = 1e6


def select_bridge_reserve(
    clip_metadata: list[dict],
    clip_durations: dict[str, float],
    assigned_clip_ids: list[str],
    used_windows: dict[str, list[UsedWindow]] | None,
    count: int,
) -> list[str]:
    """Pick ``count`` unassigned, coverable clips for the DB-first download set.

    The reserve is the freeze-prevention bridge pool. DB-first downloads only
    ``assigned ∪ reserve`` (no padded top-30), so this is what keeps
    ``remotion_renderer`` from raising ``FreezeWouldOccurError`` when a beat's
    clip runs out of footage. Criteria — NOT relevance (bridges are caption-free
    visual filler) — mirror ``retrieval.py``'s existing reserve sort:
    **lowest ``usage_count`` → longest ``duration`` → clip_id** (spread the
    library's least-shown footage, prefer long clips that bridge more per pick).

    ``used_windows`` (keyed by asset_id) is accepted for interface symmetry with
    the packer's window machinery and breaks ties toward clips with more fresh
    (unshown) source seconds; coverability itself is duration-based (a bridge
    plays from ``trim_start=0``). Returns ``zfill(4)`` clip ids, deterministic.
    """
    used_windows = used_windows or {}
    assigned = {str(c).zfill(4) for c in assigned_clip_ids}
    meta_by_id = {str(m.get("id", "")).zfill(4): m for m in clip_metadata}
    candidates: list[tuple[int, float, float, str]] = []
    for cid, meta in meta_by_id.items():
        if not cid or cid in assigned:
            continue
        dur = clip_durations.get(cid)
        if dur is None or float(dur) <= 0.0:
            continue  # coverable = real, positive-duration footage
        usage = int(meta.get("usage_count") or 0)
        # Fresh seconds = source not yet shown (advisory tie-break only). No
        # asset mapping / no ledger entry → whole source is fresh.
        used = used_windows.get(str(meta.get("asset_id") or ""), [])
        shown = sum(max(0.0, w.end_sec - w.start_sec) for w in used)
        fresh = max(0.0, float(dur) - shown)
        candidates.append((usage, -float(dur), -fresh, cid))
    candidates.sort()
    return [cid for _, _, _, cid in candidates[: max(0, int(count))]]


def _solve_assignment(
    cost: "Any", clip_ids: list[str], spans: list[float],
    durations: list[float],
) -> list[int]:
    """One Hungarian solve over ``cost`` [beats × clips]; returns a clip-column
    index per beat. Rows the solver could only fill with an infeasible cell (or
    left unfilled, when clips < beats) fall back to the cheapest still-unused
    coverable column, else the cheapest unused column — deterministic, never
    crashes. ``scipy.optimize.linear_sum_assignment`` is deterministic: same
    matrix, same result, forever (preserves the no-LLM/no-randomness contract).
    """
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    nb, nc = cost.shape
    rows, cols = linear_sum_assignment(cost)
    assigned = {int(r): int(c) for r, c in zip(rows, cols)}
    # Reserve every FEASIBLE Hungarian column first, so an infeasible row's
    # fallback can never steal a column a later feasible row was assigned
    # (which would force a spurious 1-to-1 violation downstream).
    used: set[int] = {
        c for bi, c in assigned.items()
        if cost[bi, c] < _INFEASIBLE_COST
    }
    out: list[int | None] = [None] * nb
    for bi in range(nb):
        ci = assigned.get(bi)
        if ci is not None and cost[bi, ci] < _INFEASIBLE_COST:
            out[bi] = ci
    for bi in range(nb):
        if out[bi] is not None:
            continue
        # Infeasible/unfilled row: cheapest unused coverable column, else
        # cheapest unused column (matches greedy's least-overlap fallback intent).
        order = list(np.argsort(cost[bi]))
        pick = next(
            (c for c in order if c not in used
             and durations[c] + HARD_CONSTRAINT_TOL_SEC >= spans[bi]),
            None,
        )
        if pick is None:
            pick = next((c for c in order if c not in used), int(order[0]))
        out[bi] = int(pick)
        used.add(int(pick))
    return [int(c) for c in out]


def _global_assign(
    rankings: list[list[tuple[str, float]]],
    clip_ids: list[str],
    spans: list[float],
    durations: list[float],
    categories: list[str],
    *,
    adjacency_penalty: float,
    near_dup_penalty: float,
    near_dup_threshold: float | None,
    unit_by_id: dict[str, list[float]],
    used_windows: dict[str, list[UsedWindow]] | None = None,
    clip_to_asset: dict[str, str] | None = None,
    stale_window_penalty: float = GLOBAL_STALE_WINDOW_PENALTY,
    rounds: int = 6,
) -> list[int]:
    """Global clip↔beat assignment as ONE declarative objective (heuristic).

    NOT "global optimal" once the soft penalties below are added — it is a
    global *heuristic*: a Hungarian solve gives the optimum of the current cost
    matrix, then sequencing penalties (adjacency / near-dup / window-freshness)
    re-shape the matrix and it re-solves to a fixed point.

    Base cost = ``-text_cosine`` (the ranking score already computed per beat);
    coverage-infeasible cells = ``_INFEASIBLE_COST``; no-reuse is structural
    (1-to-1 matching). Three SOFT terms fold into the cost matrix:

    - **window-freshness (claim-1)**: a (beat, clip) with NO free source window
      long enough for the beat's span gets ``+stale_window_penalty``. This is a
      STATIC property of the (beat, clip) pair (not of the other picks), so it
      is baked into ``base`` once — it stays SOFT (never ``_INFEASIBLE``), so
      window exhaustion remains the soft contract and merely loses a tie.
    - **adjacency-variety**: ``+adjacency_penalty`` when a clip's category equals
      a NEIGHBOUR beat's category from the previous round (sequencing term).
    - **near-dup (claim-3)**: ``+near_dup_penalty`` when a clip's whitened visual
      cosine to ANY OTHER already-assigned clip is ``>= near_dup_threshold`` —
      the **all-prior** semantics greedy uses (compare to every chosen clip, not
      just the immediate neighbour), so DB-first's sole visual-dedup defense does
      not regress "one shot apart" twins.

    The adjacency + near-dup terms depend on the current assignment, so they are
    resolved by an iterative re-solve to a fixed point (or ``rounds``).
    """
    import numpy as np

    used_windows = used_windows or {}
    clip_to_asset = clip_to_asset or {}
    nb, nc = len(spans), len(clip_ids)
    col = {cid: j for j, cid in enumerate(clip_ids)}
    base = np.full((nb, nc), _INFEASIBLE_COST, dtype=np.float64)
    for bi, ranking in enumerate(rankings):
        for clip_id, score in ranking:
            j = col.get(str(clip_id).zfill(4))
            if j is None:
                continue
            if durations[j] + HARD_CONSTRAINT_TOL_SEC < spans[bi]:
                continue  # coverage infeasible
            cost_ij = -score if math.isfinite(score) else _INFEASIBLE_COST
            # claim-1: STATIC window-freshness soft cost. A clip with no free
            # window for this span is stale for this beat — soft, never blocked.
            if (
                stale_window_penalty > 0
                and math.isfinite(cost_ij)
                and not free_windows(
                    durations[j],
                    used_windows.get(clip_to_asset.get(clip_ids[j], ""), []),
                    min_len_sec=spans[bi],
                )
            ):
                cost_ij += stale_window_penalty
            base[bi, j] = cost_ij

    picks = _solve_assignment(base, clip_ids, spans, durations)
    gate_on = near_dup_threshold is not None and bool(unit_by_id)
    if adjacency_penalty <= 0 and not gate_on:
        return picks

    for _ in range(rounds):
        cost = base.copy()
        cats = [categories[c] for c in picks]
        # All-prior near-dup (claim-3): the unit vector chosen for EVERY beat,
        # so a candidate can be compared against all other assigned clips.
        chosen_units = [unit_by_id.get(clip_ids[c]) for c in picks]
        for bi in range(nb):
            neigh_cats = set()
            for nb_i in (bi - 1, bi + 1):
                if 0 <= nb_i < nb and cats[nb_i]:
                    neigh_cats.add(cats[nb_i])
            # near-dup compares to ALL other beats' picks (all-prior), not just
            # the immediate neighbour — the armed visual-dedup semantics.
            other_units = [
                chosen_units[k]
                for k in range(nb)
                if k != bi and chosen_units[k] is not None
            ]
            if not neigh_cats and not other_units:
                continue
            for j in range(nc):
                if cost[bi, j] >= _INFEASIBLE_COST:
                    continue
                if adjacency_penalty > 0 and categories[j] in neigh_cats:
                    cost[bi, j] += adjacency_penalty
                if (
                    gate_on
                    and other_units
                    and max_cosine_to_chosen(
                        unit_by_id.get(clip_ids[j]), other_units,
                    ) >= near_dup_threshold
                ):
                    cost[bi, j] += near_dup_penalty
        new_picks = _solve_assignment(cost, clip_ids, spans, durations)
        if new_picks == picks:
            break
        picks = new_picks
    return picks


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
    global_assignment: bool = False,
    adjacency_penalty: float = GLOBAL_ADJACENCY_PENALTY,
    near_dup_penalty: float = GLOBAL_NEAR_DUP_PENALTY,
    bridge_reserve_count: int | None = None,
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

    ``global_assignment`` (DEFAULT False = OFF → byte-identical greedy path)
    swaps the greedy first-fit relax-ladder for ONE global (HEURISTIC, not
    "optimal" once the soft penalties are added) clip↔beat assignment: a single
    ``scipy.optimize.linear_sum_assignment`` over a [beats × clips] cost matrix
    (base = ``-text_cosine``; coverage-infeasible forbidden; no-reuse structural
    via 1-to-1 matching). Adjacency-variety, near-dup, and window-freshness
    become SOFT PENALTY terms folded into that one matrix and resolved by an
    iterative re-solve (see ``_global_assign``) — fixing the
    greedy stranding where an earlier beat spends a clip a later beat needed
    more. The armed path SUPERSEDES the entire greedy relax-ladder (the nested
    ``for enforce_diversity / for enforce_adjacency`` passes + per-pick
    no-reuse/coverage/adjacency/near-dup checks); window resolution (rule 3/5)
    and the least-overlap fallback are RELOCATED to run per assigned clip AFTER
    the solve, unchanged. ``adjacency_penalty`` / ``near_dup_penalty`` weigh the
    two soft terms (defaults tuned offline). When armed, provenance gains
    ``global_assignment: True`` and ``displaced_beats`` (any beat whose assigned
    clip is >0.15 below its best-feasible text-cosine — the silent-displacement
    gap the greedy packer cannot see).
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

    if global_assignment:
        return _pack_clips_global(
            beats, rankings, spans,
            meta_by_id=meta_by_id,
            clip_durations=clip_durations,
            used_windows=used_windows,
            clip_to_asset=clip_to_asset,
            max_beat_sec=max_beat_sec,
            near_dup_threshold=near_dup_threshold,
            adjacency_penalty=adjacency_penalty,
            near_dup_penalty=near_dup_penalty,
            unit_by_id=unit_by_id,
            bridge_reserve_count=bridge_reserve_count,
        )

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


def _pack_clips_global(
    beats: list[dict],
    rankings: list[list[tuple[str, float]]],
    spans: list[float],
    *,
    meta_by_id: dict[str, dict],
    clip_durations: dict[str, float],
    used_windows: dict[str, list[UsedWindow]],
    clip_to_asset: dict[str, str],
    max_beat_sec: float,
    near_dup_threshold: float | None,
    adjacency_penalty: float,
    near_dup_penalty: float,
    unit_by_id: dict[str, list[float]],
    bridge_reserve_count: int | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Armed path (``global_assignment=True``): ONE global (heuristic) clip↔beat
    assignment + RELOCATED window resolution.

    Replaces the greedy relax-ladder entirely: the [beats × clips] cost matrix
    (base ``-text_cosine``, coverage forbidden, no-reuse structural) is solved
    once by ``_global_assign`` with adjacency + near-dup folded in as soft
    penalties. Window rotation (rule 3/5) and the least-overlap fallback are the
    SAME ``free_windows`` / ``_phase_trim`` / ``_least_overlap_trim`` helpers the
    greedy path uses, just MOVED to run per assigned clip after the solve.
    """
    # Pool of coverable clips the solver may draw from (mirrors greedy's
    # eligibility: present in clip_durations). Order is stable (ranking-major
    # union) so the Hungarian solve is reproducible.
    clip_ids: list[str] = []
    seen_pool: set[str] = set()
    for ranking in rankings:
        for clip_id, _ in ranking:
            norm = str(clip_id).zfill(4)
            if norm in seen_pool or norm not in clip_durations:
                continue
            seen_pool.add(norm)
            clip_ids.append(norm)
    durations = [float(clip_durations[c]) for c in clip_ids]
    categories = [str(meta_by_id.get(c, {}).get("category") or "") for c in clip_ids]

    if len(clip_ids) < len(beats):
        # Same fail-loud contract as greedy: cannot give every beat a unique
        # coverable clip. Raise on the first uncoverable beat.
        for beat_i, (beat, span) in enumerate(zip(beats, spans)):
            coverable = [
                c for j, c in enumerate(clip_ids)
                if durations[j] + HARD_CONSTRAINT_TOL_SEC >= span
            ]
            if len(coverable) < 1:
                raise ClipAssignmentError(
                    segment_index=int(beat["segment"]),
                    phrase_index=1 + sum(
                        1 for b in beats[:beat_i] if b["segment"] == beat["segment"]
                    ),
                    required_span=span,
                    actual_max_usable=0.0,
                    clip_id="<packer-global: no coverable candidate>",
                )

    picks = _global_assign(
        rankings, clip_ids, spans, durations, categories,
        adjacency_penalty=adjacency_penalty,
        near_dup_penalty=near_dup_penalty,
        near_dup_threshold=near_dup_threshold,
        unit_by_id=unit_by_id,
        used_windows=used_windows,
        clip_to_asset=clip_to_asset,
    )
    # claim-2 (too-short fail-loud): the solver's last-resort fallback can fill a
    # row with the cheapest unused column even when it cannot cover the span
    # (``_solve_assignment``). A too-short pick silently truncates the beat or
    # mis-reports as window_exhausted downstream — so assert coverage here and
    # fail loud (same contract as greedy:512 / the global thin-pool check),
    # rather than ship a clip that cannot cover its beat.
    for beat_i, (beat, span) in enumerate(zip(beats, spans)):
        if durations[picks[beat_i]] + HARD_CONSTRAINT_TOL_SEC < span:
            raise ClipAssignmentError(
                segment_index=int(beat["segment"]),
                phrase_index=1 + sum(
                    1 for b in beats[:beat_i] if b["segment"] == beat["segment"]
                ),
                required_span=span,
                actual_max_usable=float(durations[picks[beat_i]]),
                clip_id="<packer-global: assigned clip too short to cover span>",
            )
    if len(set(picks)) != len(picks):
        # 1-to-1 matching guarantees uniqueness; a collision means the fallback
        # had no unused coverable clip — fail loud rather than ship a reuse.
        raise ClipAssignmentError(
            segment_index=int(beats[0]["segment"]),
            phrase_index=1,
            required_span=max(spans) if spans else 0.0,
            actual_max_usable=0.0,
            clip_id="<packer-global: assignment not 1-to-1 (pool exhausted)>",
        )

    # Pre-compute per-beat best-feasible cosine for the silent-displacement flag.
    best_feasible = _best_feasible_cosines(rankings, clip_durations, spans)

    assignments: list[dict] = []
    provenance: dict[str, Any] = {
        "assigner": "packer",
        "window_exhausted_beats": [],
        "adjacency_relaxed_beats": [],  # N/A under global; kept for shape parity
        "picks": [],
        "global_assignment": True,
        "displaced_beats": [],
    }
    if near_dup_threshold is not None:
        provenance["near_dup_threshold"] = near_dup_threshold
    # DB-first bridge reserve: unassigned coverable clips to download alongside
    # the assigned set so the renderer's freeze-prevention pool is non-empty.
    # Added ONLY when a count is requested (DB-first), so the legacy global-path
    # provenance shape is unchanged.
    if bridge_reserve_count is not None:
        assigned_ids = [clip_ids[picks[bi]] for bi in range(len(beats))]
        provenance["reserve_clip_ids"] = select_bridge_reserve(
            list(meta_by_id.values()),
            clip_durations,
            assigned_ids,
            used_windows,
            bridge_reserve_count,
        )

    score_by_beat_clip = [
        {str(cid).zfill(4): sc for cid, sc in ranking} for ranking in rankings
    ]

    for beat_i, (beat, span) in enumerate(zip(beats, spans)):
        norm = clip_ids[picks[beat_i]]
        meta = meta_by_id.get(norm, {})
        source = float(clip_durations[norm])
        used = used_windows.get(clip_to_asset.get(norm, ""), [])
        # RELOCATED window resolution (rule 3 then rule 5; rule-3 fallback).
        gaps = free_windows(source, used, min_len_sec=span)
        if gaps:
            trim = _phase_trim(
                gaps[0], span, str(meta.get("dominant_motion_phase") or ""),
            )
            exhausted = False
        else:
            trim = _least_overlap_trim(source, span, used)
            exhausted = True
            provenance["window_exhausted_beats"].append(beat_i)
            logger.warning(
                "packer-global: source windows exhausted for beat %d "
                "(span %.2fs) — least-overlap fallback on clip %s",
                beat_i, span, norm,
            )

        score = float(score_by_beat_clip[beat_i].get(norm, float("-inf")))
        bf = best_feasible[beat_i]
        if math.isfinite(bf) and math.isfinite(score) and (bf - score) >= 0.15:
            provenance["displaced_beats"].append({
                "beat": beat_i,
                "clip_id": norm,
                "score": round(score, 4),
                "best_feasible": round(bf, 4),
                "gap": round(bf - score, 4),
            })

        assignments.append({
            "segment": int(beat["segment"]),
            "clip_id": norm,
            "start_word_idx": int(beat["start_word_idx"]),
            "end_word_idx": int(beat["end_word_idx"]),
            "trim_start": round(float(trim), 3),
        })
        provenance["picks"].append({
            "beat": beat_i,
            "clip_id": norm,
            "score": round(score, 4) if math.isfinite(score) else None,
            "trim_start": round(float(trim), 3),
            "window_exhausted": exhausted,
        })

    provenance["beat_count"] = len(beats)
    provenance["unique_clip_count"] = len({a["clip_id"] for a in assignments})
    provenance["overlong_beats"] = [
        i for i, s in enumerate(spans)
        if s > max_beat_sec + HARD_CONSTRAINT_TOL_SEC
    ]
    return assignments, provenance


def _best_feasible_cosines(
    rankings: list[list[tuple[str, float]]],
    clip_durations: dict[str, float],
    spans: list[float],
) -> list[float]:
    """Per beat, the highest text-cosine over coverable clips in its ranking —
    the bar the assigned clip is measured against for silent-displacement."""
    out: list[float] = []
    for ranking, span in zip(rankings, spans):
        best = float("-inf")
        for clip_id, score in ranking:
            norm = str(clip_id).zfill(4)
            dur = clip_durations.get(norm)
            if dur is None or dur + HARD_CONSTRAINT_TOL_SEC < span:
                continue
            if math.isfinite(score) and score > best:
                best = score
        out.append(best)
    return out
