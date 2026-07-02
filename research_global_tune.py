"""Offline tuning harness (read-only, network=embed only) for the GLOBAL
assignment consolidation.

Reconstructs each video's [beat x clip] text-cosine matrix by re-embedding the
recorded narration phrases (match_quality) and clip embedding_texts
(run_manifest.asset_snapshot) with the repo's own embed_texts — the SAME model
the packer ranked on — then compares:

  greedy   : reproduction of the production greedy relax-ladder (window-blind)
  global   : linear_sum_assignment on cost = -cosine (coverage infeasible)
  global+P : global, then a deterministic adjacency LOCAL-REPAIR pass at a
             swap-cost budget P (the production candidate)

Metrics per arm (pooled across all videos): dramatic misses, same-category
back-to-back count, median/mean chosen cosine, share<0.20.

NOTE: run3x3's exact sidecars are gone from disk (only the rendered mp4s
remain). This harness runs on whatever spike-compatible sidecar batches ARE
on disk to demonstrate the same allocation effect on REAL recorded runs.

ZERO writes to DB/Drive/release_candidates. Reads sidecars read-only.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys

import numpy as np
from scipy.optimize import linear_sum_assignment

from promo.core.assign.clip_embedder import embed_texts
from promo.core.assign.clip_assignment_validator import HARD_CONSTRAINT_TOL_SEC

ROOTS = sys.argv[1:] or ["."]

_EMB: dict[str, list[float]] = {}


def embed_cached(texts: list[str]) -> dict[str, np.ndarray]:
    todo = [t for t in dict.fromkeys(texts) if t and t not in _EMB]
    for i in range(0, len(todo), 256):
        chunk = todo[i:i + 256]
        for t, v in zip(chunk, embed_texts(chunk)):
            _EMB[t] = v
    return {t: np.asarray(_EMB[t], dtype=np.float64) for t in texts if t}


def cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return float("-inf")
    return float(a @ b / (na * nb))


def find_videos() -> list[str]:
    dirs = []
    for root in ROOTS:
        for mq in glob.glob(os.path.join(root, "**", "match_quality_*.json"), recursive=True):
            d = os.path.dirname(mq)
            if glob.glob(os.path.join(d, "clip_assignments_*.json")) and \
               glob.glob(os.path.join(d, "run_manifest_*.json")):
                dirs.append(d)
    return sorted(set(dirs))


def load(d: str) -> dict | None:
    mq = json.load(open(glob.glob(os.path.join(d, "match_quality_*.json"))[0]))
    ca = json.load(open(glob.glob(os.path.join(d, "clip_assignments_*.json"))[0]))
    rm = json.load(open(glob.glob(os.path.join(d, "run_manifest_*.json"))[0]))
    snap = rm.get("asset_snapshot") or []
    if not snap:
        return None
    clips = {}
    for a in snap:
        cid = str(a.get("clip_id")).zfill(4)
        clips[cid] = {
            "id": cid,
            "text": (a.get("embedding_text") or a.get("scene_description") or "").strip(),
            "category": str(a.get("category") or ""),
            "dur": float(a.get("source_duration_sec") or a.get("duration_sec") or 0.0),
        }
    clip_ids = [c for c in clips if clips[c]["text"]]
    if len(clip_ids) < 2:
        return None
    # beats: align match_quality entries with the chosen assignment order.
    # match_quality is parallel to assignments (variant 0). Use display span
    # from assignments when present, else fall back to a coverage-permissive span.
    variant = ca["variants"][0]
    assigns = variant["assignments"]
    beats = []
    for i, asn in enumerate(assigns):
        m = mq[i] if i < len(mq) else {}
        q = (m.get("narration_phrase") or "").strip()
        if not q:
            continue
        span = float(asn.get("display_span_sec") or asn.get("display_duration_sec") or 0.0)
        beats.append({
            "beat": len(beats), "query": q, "span": span,
            "chosen": str(asn.get("clip_id")).zfill(4),
            "chosen_cat": str(m.get("picked_category") or ""),
        })
    if not beats:
        return None
    texts = [clips[c]["text"] for c in clip_ids] + [b["query"] for b in beats]
    emb = embed_cached(texts)
    clip_vec = {c: emb[clips[c]["text"]] for c in clip_ids}
    S = np.full((len(beats), len(clip_ids)), -np.inf)
    for bi, b in enumerate(beats):
        qv = emb[b["query"]]
        for ci, cid in enumerate(clip_ids):
            S[bi, ci] = cos(qv, clip_vec[cid])
    return {"dir": d, "clips": clips, "clip_ids": clip_ids, "beats": beats, "S": S}


def coverable(span: float, dur: float) -> bool:
    # span 0 (older sidecars w/o display_span) => any clip coverable.
    return span <= 0 or dur + HARD_CONSTRAINT_TOL_SEC >= span


# ---- greedy reproduction (no-reuse + coverage + adjacency two-pass) ----
def greedy(rec: dict, adjacency: bool = True) -> list[str]:
    clips, ids, beats, S = rec["clips"], rec["clip_ids"], rec["beats"], rec["S"]
    seen, prev, out = set(), None, []
    for bi, b in enumerate(beats):
        order = np.argsort(-S[bi])
        chosen = None
        for enf in ((True, False) if adjacency else (False,)):
            for ci in order:
                cid = ids[ci]
                if cid in seen or not coverable(b["span"], clips[cid]["dur"]):
                    continue
                if enf and prev and clips[cid]["category"] == prev:
                    continue
                chosen = cid
                break
            if chosen:
                break
        if chosen is None:
            for ci in order:
                cid = ids[ci]
                if cid not in seen:
                    chosen = cid
                    break
        seen.add(chosen)
        prev = clips[chosen]["category"] or prev
        out.append(chosen)
    return out


# ---- global: linear_sum_assignment on cost = -cosine, coverage infeasible ----
BIG = 1e6


def cost_matrix(rec: dict) -> np.ndarray:
    clips, ids, beats, S = rec["clips"], rec["clip_ids"], rec["beats"], rec["S"]
    nb, nc = len(beats), len(ids)
    C = np.full((nb, nc), BIG)
    for bi, b in enumerate(beats):
        for ci, cid in enumerate(ids):
            if not coverable(b["span"], clips[cid]["dur"]):
                continue
            s = S[bi, ci]
            C[bi, ci] = -s if math.isfinite(s) else BIG
    return C


def solve(C: np.ndarray, rec: dict) -> list[str]:
    ids, beats, S = rec["clip_ids"], rec["beats"], rec["S"]
    rows, cols = linear_sum_assignment(C)
    assign = {int(r): int(c) for r, c in zip(rows, cols)}
    used = set()
    out = []
    for bi in range(len(beats)):
        ci = assign.get(bi)
        if ci is not None and C[bi, ci] < BIG:
            out.append(ids[ci])
            used.add(ids[ci])
        else:  # infeasible row -> best coverable unused fallback
            order = np.argsort(-S[bi])
            pick = next((ids[c] for c in order if ids[c] not in used
                         and coverable(beats[bi]["span"], rec["clips"][ids[c]]["dur"])), None)
            if pick is None:
                pick = next((ids[c] for c in order if ids[c] not in used), ids[int(order[0])])
            out.append(pick)
            used.add(pick)
    return out


def global_assign(rec: dict) -> list[str]:
    return solve(cost_matrix(rec), rec)


# ---- iterative penalty: re-solve, each round adding a soft same-category
#      penalty against the PREVIOUS round's neighbour categories. Converges
#      when the assignment stops changing (or max rounds). Deterministic. ----
def global_penalty(rec: dict, weight: float, rounds: int = 6) -> list[str]:
    ids, beats = rec["clip_ids"], rec["beats"]
    clips = rec["clips"]
    base = cost_matrix(rec)
    picks = solve(base, rec)
    for _ in range(rounds):
        C = base.copy()
        prev_cats = [clips[p]["category"] for p in picks]
        for bi in range(len(beats)):
            neigh = set()
            if bi > 0:
                neigh.add(prev_cats[bi - 1])
            if bi + 1 < len(beats):
                neigh.add(prev_cats[bi + 1])
            neigh.discard("")
            if not neigh:
                continue
            for ci, cid in enumerate(ids):
                if C[bi, ci] >= BIG:
                    continue
                if clips[cid]["category"] in neigh:
                    C[bi, ci] += weight
        new = solve(C, rec)
        if new == picks:
            break
        picks = new
    return picks


# ---- adjacency LOCAL-REPAIR: break same-cat neighbours by swapping two beats'
#      clips when the swap's total cosine loss <= budget. Deterministic. ----
def global_repair(rec: dict, budget: float) -> list[str]:
    return _repair_on(rec, global_assign(rec), budget)


def _repair_on(rec: dict, start: list[str], budget: float) -> list[str]:
    ids, beats, S = rec["clip_ids"], rec["beats"], rec["S"]
    clips = rec["clips"]
    idx = {c: i for i, c in enumerate(ids)}
    picks = list(start)
    nb = len(beats)

    def cat(i):
        return clips[picks[i]]["category"]

    def feasible(bi, cid):
        return coverable(beats[bi]["span"], clips[cid]["dur"])

    # one deterministic sweep: for each adjacency violation (i,i+1 same cat),
    # try to swap clip(i+1) with some clip(j) so neither new neighbour-pair is
    # same-cat AND total cosine loss <= budget. Pick the lowest-loss valid swap.
    changed = True
    guard = 0
    while changed and guard < nb * 4:
        changed = False
        guard += 1
        for i in range(nb - 1):
            if cat(i) != cat(i + 1):
                continue
            best_j, best_loss = None, budget + 1e-9
            for j in range(nb):
                if j == i + 1:
                    continue
                ca, cb = picks[i + 1], picks[j]
                if not (feasible(i + 1, cb) and feasible(j, ca)):
                    continue
                # new categories after swap
                new_ip1 = clips[cb]["category"]
                new_j = clips[ca]["category"]
                # check the swap actually fixes i..i+1 and doesn't create worse
                left_i = cat(i)
                right_ip2 = cat(i + 2) if i + 2 < nb else None
                if new_ip1 == left_i:
                    continue  # didn't fix the target violation
                if right_ip2 is not None and new_ip1 == right_ip2:
                    continue  # creates a new violation on the right
                # j's neighbours
                jl = cat(j - 1) if j - 1 >= 0 and j - 1 != i + 1 else None
                jr = cat(j + 1) if j + 1 < nb and j + 1 != i + 1 else None
                if (jl is not None and new_j == jl) or (jr is not None and new_j == jr):
                    continue  # creates a violation around j
                loss = ((S[i + 1, idx[ca]] - S[i + 1, idx[cb]])
                        + (S[j, idx[cb]] - S[j, idx[ca]]))
                if loss <= best_loss:
                    best_loss, best_j = loss, j
            if best_j is not None:
                picks[i + 1], picks[best_j] = picks[best_j], picks[i + 1]
                changed = True
    return picks


# ---------- metrics ----------
def scores(rec, picks):
    idx = {c: i for i, c in enumerate(rec["clip_ids"])}
    return [rec["S"][bi, idx[c]] for bi, c in enumerate(picks)]


def best_feasible(rec, bi):
    clips, ids, beats, S = rec["clips"], rec["clip_ids"], rec["beats"], rec["S"]
    best = -np.inf
    for ci, cid in enumerate(ids):
        if coverable(beats[bi]["span"], clips[cid]["dur"]) and S[bi, ci] > best:
            best = S[bi, ci]
    return best


def dramatic(rec, picks, gap=0.15, ceil=0.25):
    idx = {c: i for i, c in enumerate(rec["clip_ids"])}
    n = 0
    for bi in range(len(rec["beats"])):
        bf = best_feasible(rec, bi)
        sc = rec["S"][bi, idx[picks[bi]]]
        if math.isfinite(bf) and (bf - sc) >= gap and sc < ceil:
            n += 1
    return n


def adj_count(rec, picks):
    clips = rec["clips"]
    c, prev = 0, None
    for cid in picks:
        cat = clips[cid]["category"]
        if prev is not None and cat == prev:
            c += 1
        prev = cat
    return c


def main():
    dirs = find_videos()
    print(f"found {len(dirs)} spike-compatible videos under {ROOTS}", flush=True)
    recs = []
    for d in dirs:
        try:
            r = load(d)
            if r:
                recs.append(r)
        except Exception as e:
            print("skip", d, type(e).__name__, e)
    print(f"loaded {len(recs)} videos, {sum(len(r['beats']) for r in recs)} beats total", flush=True)

    budgets = [0.0, 0.03, 0.05, 0.08, 0.12, 0.20]
    pen_weights = [0.03, 0.05, 0.08, 0.12, 0.20]
    arms = {"actual": [], "greedy": [], "global": []}
    for b in budgets:
        arms[f"global+P{b}"] = []
    for w in pen_weights:
        arms[f"global+IP{w}"] = []
    for k in ["global+IP0.05+R0.05", "global+IP0.05+R0.08", "global+IP0.08+R0.08"]:
        arms[k] = []
    pooled = {k: {"sc": [], "dm": 0, "adj": 0} for k in arms}

    for rec in recs:
        sets = {
            "actual": [b["chosen"] for b in rec["beats"]],
            "greedy": greedy(rec, True),
            "global": global_assign(rec),
        }
        for b in budgets:
            sets[f"global+P{b}"] = global_repair(rec, b)
        for w in pen_weights:
            sets[f"global+IP{w}"] = global_penalty(rec, w)
        # combined: iterative penalty THEN a light local repair within budget
        for w, b in [(0.05, 0.05), (0.05, 0.08), (0.08, 0.08)]:
            base_pen = global_penalty(rec, w)
            sets[f"global+IP{w}+R{b}"] = _repair_on(rec, base_pen, b)
        for name, picks in sets.items():
            pooled[name]["sc"].extend(scores(rec, picks))
            pooled[name]["dm"] += dramatic(rec, picks)
            pooled[name]["adj"] += adj_count(rec, picks)

    def med(x):
        return round(float(np.median(x)), 4) if x else float("nan")

    print("\narm                 median   mean   share<0.20  dramatic_miss  same_cat_b2b")
    order_names = (["actual", "greedy", "global"]
                   + [f"global+P{b}" for b in budgets]
                   + [f"global+IP{w}" for w in pen_weights]
                   + ["global+IP0.05+R0.05", "global+IP0.05+R0.08", "global+IP0.08+R0.08"])
    for name in order_names:
        sc = pooled[name]["sc"]
        sh = round(float((np.array(sc) < 0.20).mean()), 3) if sc else float("nan")
        print(f"{name:18s} {med(sc):7.4f} {round(float(np.mean(sc)),4):7.4f}  {sh:9.3f}  "
              f"{pooled[name]['dm']:13d}  {pooled[name]['adj']:11d}")


if __name__ == "__main__":
    main()
