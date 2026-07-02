"""Research spike (read-only): allocation-vs-coverage + Lever A/B on run3x3.

Reconstructs the [beat x clip] text-cosine score matrix offline by re-embedding
each clip's embedding_text and each beat's narration_phrase via the repo's own
embed_texts (OpenRouter text-embedding-3-small, same model the packer ranked on),
then:
  - validates the matrix reproduces the packer's recorded chosen scores,
  - reproduces the greedy packer's picks,
  - classifies worst misses as ALLOCATION vs COVERAGE,
  - measures Lever A (adjacency off) and Lever B (global linear_sum_assignment).

ZERO writes to DB/Drive/release_candidates. Reads run3x3 sidecars read-only.
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

RUN_ROOT = sys.argv[1] if len(sys.argv) > 1 else "."

# ---- embedding cache (avoid re-embedding identical texts across videos) ----
_EMB_CACHE: dict[str, list[float]] = {}


def embed_cached(texts: list[str]) -> dict[str, np.ndarray]:
    todo = [t for t in dict.fromkeys(texts) if t not in _EMB_CACHE]
    for i in range(0, len(todo), 256):
        chunk = todo[i:i + 256]
        for t, v in zip(chunk, embed_texts(chunk)):
            _EMB_CACHE[t] = v
    return {t: np.asarray(_EMB_CACHE[t], dtype=np.float64) for t in texts}


def cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return float("-inf")
    return float(a @ b / (na * nb))


def load_videos() -> list[dict]:
    vids = []
    for mq_path in sorted(glob.glob(os.path.join(RUN_ROOT, "*/video_*/match_quality_*.json"))):
        vdir = os.path.dirname(mq_path)
        ca_path = glob.glob(os.path.join(vdir, "clip_assignments_*.json"))[0]
        rm_path = glob.glob(os.path.join(vdir, "run_manifest_*.json"))[0]
        mq = json.load(open(mq_path))
        ca = json.load(open(ca_path))
        rm = json.load(open(rm_path))
        poi = os.path.basename(os.path.dirname(vdir))
        vid = os.path.basename(vdir)
        vids.append({"poi": poi, "vid": vid, "mq": mq, "ca": ca, "rm": rm})
    return vids


def build(v: dict) -> dict:
    """Return reconstructed beat/clip structures for one video."""
    ca, rm, mq = v["ca"], v["rm"], v["mq"]
    pool = rm["asset_snapshot"]
    # clip pool fields
    clips = {}
    for a in pool:
        cid = str(a["clip_id"]).zfill(4)
        clips[cid] = {
            "id": cid,
            "text": a.get("embedding_text") or a.get("scene_description") or "",
            "category": str(a.get("category") or ""),
            "dur": float(a.get("source_duration_sec") or 0.0),
        }
    clip_ids = list(clips.keys())

    # beats from assignments (ordered) — match_quality is parallel-ordered
    variant = ca["variants"][0]
    assigns = variant["assignments"]
    picks = ca["packer"]["picks"]
    beats = []
    for i, asn in enumerate(assigns):
        m = mq[i] if i < len(mq) else {}
        beats.append({
            "beat": i,
            "query": (m.get("narration_phrase") or "").strip(),
            "span": float(asn["display_span_sec"]),
            "chosen": str(asn["clip_id"]).zfill(4),
            "chosen_score": picks[i].get("score"),
            "chosen_cat": str(m.get("picked_category") or ""),
        })

    # embed all texts
    texts = [c["text"] for c in clips.values()] + [b["query"] for b in beats]
    emb = embed_cached(texts)
    clip_vec = {cid: emb[clips[cid]["text"]] for cid in clip_ids}
    # score matrix S[beat][clip]
    S = np.full((len(beats), len(clip_ids)), -np.inf)
    for bi, b in enumerate(beats):
        qv = emb[b["query"]]
        for ci, cid in enumerate(clip_ids):
            S[bi, ci] = cos(qv, clip_vec[cid])
    return {"clips": clips, "clip_ids": clip_ids, "beats": beats, "S": S}


def coverable(span: float, dur: float) -> bool:
    return dur + HARD_CONSTRAINT_TOL_SEC >= span


# ---- greedy packer reproduction (no-reuse, coverage, adjacency) ----
def greedy(rec: dict, adjacency: bool) -> list[str]:
    clips, clip_ids, beats, S = rec["clips"], rec["clip_ids"], rec["beats"], rec["S"]
    seen: set[str] = set()
    prev_cat = None
    out = []
    for bi, b in enumerate(beats):
        order = np.argsort(-S[bi])
        chosen = None
        for enforce_adj in (True, False) if adjacency else (False,):
            for ci in order:
                cid = clip_ids[ci]
                if cid in seen:
                    continue
                c = clips[cid]
                if not coverable(b["span"], c["dur"]):
                    continue
                if enforce_adj and prev_cat and c["category"] == prev_cat:
                    continue
                chosen = cid
                break
            if chosen:
                break
        if chosen is None:  # coverage fallback: best coverable unused ignoring adj
            for ci in order:
                cid = clip_ids[ci]
                if cid in seen or not coverable(b["span"], clips[cid]["dur"]):
                    continue
                chosen = cid
                break
        if chosen is None:  # last resort: best unused
            for ci in order:
                cid = clip_ids[ci]
                if cid not in seen:
                    chosen = cid
                    break
        seen.add(chosen)
        prev_cat = clips[chosen]["category"] or prev_cat
        out.append(chosen)
    return out


# ---- Lever B: global optimal assignment ----
def global_assign(rec: dict) -> list[str]:
    clips, clip_ids, beats, S = rec["clips"], rec["clip_ids"], rec["beats"], rec["S"]
    nb, nc = len(beats), len(clip_ids)
    BIG = 1e6
    cost = np.full((nb, nc), BIG)
    for bi, b in enumerate(beats):
        for ci, cid in enumerate(clip_ids):
            if not coverable(b["span"], clips[cid]["dur"]):
                continue  # infeasible (coverage) -> stays BIG
            s = S[bi, ci]
            cost[bi, ci] = -s if math.isfinite(s) else BIG
    # rectangular: nb beats <= nc clips typically; linear_sum_assignment handles it
    rows, cols = linear_sum_assignment(cost)
    assign = {int(r): clip_ids[int(c)] for r, c in zip(rows, cols)}
    # every beat must get a clip (nb<=nc); fill any missing defensively
    out = []
    used = set(assign.values())
    for bi in range(nb):
        if bi in assign and cost[bi, clip_ids.index(assign[bi])] < BIG:
            out.append(assign[bi])
        else:
            # infeasible row -> best coverable unused, else best unused
            order = np.argsort(-S[bi])
            pick = None
            for ci in order:
                cid = clip_ids[ci]
                if cid in used:
                    continue
                if coverable(beats[bi]["span"], clips[cid]["dur"]):
                    pick = cid
                    break
            if pick is None:
                for ci in order:
                    cid = clip_ids[ci]
                    if cid not in used:
                        pick = cid
                        break
            out.append(pick)
        used.add(out[-1])
    return out


def picks_scores(rec: dict, picks: list[str]) -> np.ndarray:
    S, clip_ids = rec["S"], rec["clip_ids"]
    idx = {cid: i for i, cid in enumerate(clip_ids)}
    return np.array([S[bi, idx[cid]] for bi, cid in enumerate(picks)])


def best_feasible(rec: dict, bi: int, exclude: set[str] | None = None) -> float:
    """Best text-cosine over feasible (coverable) clips for a beat, ignoring reuse
    unless exclude given. Used to measure dramatic-miss gap."""
    clips, clip_ids, beats, S = rec["clips"], rec["clip_ids"], rec["beats"], rec["S"]
    exclude = exclude or set()
    best = -np.inf
    for ci, cid in enumerate(clip_ids):
        if cid in exclude:
            continue
        if not coverable(beats[bi]["span"], clips[cid]["dur"]):
            continue
        if S[bi, ci] > best:
            best = S[bi, ci]
    return best


def med(x):
    return float(np.median(x)) if len(x) else float("nan")


def share_below(x, t):
    x = np.array(x)
    return float((x < t).mean()) if len(x) else float("nan")


def main():
    vids = load_videos()
    print(f"loaded {len(vids)} videos")
    allrows = []
    summary = {}
    for v in vids:
        rec = build(v)
        beats = rec["beats"]
        clips = rec["clips"]
        clip_ids = rec["clip_ids"]
        key = f"{v['poi']}/{v['vid']}"

        # --- validation: reconstructed chosen cosine vs recorded packer score ---
        idx = {cid: i for i, cid in enumerate(clip_ids)}
        valid_err = []
        for b in beats:
            if b["chosen_score"] is None:
                continue
            recon = rec["S"][b["beat"], idx[b["chosen"]]]
            valid_err.append(abs(recon - b["chosen_score"]))

        actual = [b["chosen"] for b in beats]
        g_on = greedy(rec, adjacency=True)   # window-blind reproduction of prod greedy
        g_off = greedy(rec, adjacency=False)
        g_glob = global_assign(rec)

        s_actual = picks_scores(rec, actual)
        s_on = picks_scores(rec, g_on)
        s_off = picks_scores(rec, g_off)
        s_glob = picks_scores(rec, g_glob)

        # greedy-on reproduction match vs actual picks
        repro = sum(1 for a, g in zip(actual, g_on) if a == g)

        # same-category back-to-back counts
        def adj_count(picks):
            c = 0
            prev = None
            for cid in picks:
                cat = clips[cid]["category"]
                if prev is not None and cat == prev:
                    c += 1
                prev = cat
            return c

        # dramatic-miss: chosen cosine far below best feasible clip for that beat
        # (allocation-fixable definition uses reuse-aware best among UNUSED-at-that-point
        #  is path dependent; for the metric we use best feasible in whole pool)
        def dramatic(picks, gap=0.15):
            n = 0
            for bi in range(len(beats)):
                bf = best_feasible(rec, bi)
                sc = rec["S"][bi, idx[picks[bi]]]
                if math.isfinite(bf) and (bf - sc) >= gap and sc < 0.25:
                    n += 1
            return n

        summary[key] = {
            "n_beats": len(beats),
            "n_clips": len(clip_ids),
            "valid_max_err": max(valid_err) if valid_err else None,
            "valid_mean_err": (sum(valid_err) / len(valid_err)) if valid_err else None,
            "repro_greedy_on": f"{repro}/{len(beats)}",
            "median": {
                "actual": round(med(s_actual), 4),
                "greedy": round(med(s_on), 4),
                "adj_off": round(med(s_off), 4),
                "global": round(med(s_glob), 4),
            },
            "share_below_0.20": {
                "actual": round(share_below(s_actual, 0.20), 3),
                "greedy": round(share_below(s_on, 0.20), 3),
                "adj_off": round(share_below(s_off, 0.20), 3),
                "global": round(share_below(s_glob, 0.20), 3),
            },
            "mean": {
                "actual": round(float(np.mean(s_actual)), 4),
                "greedy": round(float(np.mean(s_on)), 4),
                "adj_off": round(float(np.mean(s_off)), 4),
                "global": round(float(np.mean(s_glob)), 4),
            },
            "same_cat_back_to_back": {
                "actual": adj_count(actual),
                "greedy": adj_count(g_on),
                "adj_off": adj_count(g_off),
                "global": adj_count(g_glob),
            },
            "dramatic_miss": {
                "actual": dramatic(actual),
                "greedy": dramatic(g_on),
                "adj_off": dramatic(g_off),
                "global": dramatic(g_glob),
            },
        }
        allrows.append((v, rec, actual, g_off, g_glob))

    # ---- Task 1: classify worst misses (pooled across all videos) ----
    worst = []
    for v, rec, actual, g_off, g_glob in allrows:
        idx = {cid: i for i, cid in enumerate(rec["clip_ids"])}
        clips = rec["clips"]
        for bi, b in enumerate(rec["beats"]):
            sc = rec["S"][bi, idx[b["chosen"]]]
            bf = best_feasible(rec, bi)  # best feasible in whole pool (reuse-blind)
            gap = bf - sc if math.isfinite(bf) else 0.0
            # allocation test: is there a clearly-better feasible clip that the
            # GLOBAL assigner could free up, i.e. better feasible clip exists in
            # pool but was used by another beat (greedy) -> allocation.
            # coverage test: best feasible clip == chosen (nothing better feasible)
            worst.append({
                "key": f"{v['poi']}/{v['vid']}",
                "beat": bi,
                "query": b["query"][:50],
                "chosen": b["chosen"],
                "chosen_cos": round(sc, 4),
                "best_feasible_cos": round(bf, 4) if math.isfinite(bf) else None,
                "gap": round(gap, 4),
            })
    worst.sort(key=lambda r: r["chosen_cos"])

    # For the N worst, classify allocation vs coverage:
    # ALLOCATION = a clearly-better feasible clip exists in the pool (gap>=0.10)
    #              AND that better clip was consumed by another beat in the actual run
    # COVERAGE   = the chosen clip already IS (near) the best feasible (gap<0.05),
    #              i.e. nothing materially better is coverable -> assignment can't fix
    N = 30
    topN = worst[:N]
    alloc = cov = ambiguous = 0
    used_by_video = {}
    for v, rec, actual, g_off, g_glob in allrows:
        used_by_video[f"{v['poi']}/{v['vid']}"] = set(actual)
    detail = []
    for r in topN:
        key = r["key"]
        rec = next(rc for vv, rc, a, o, gl in allrows if f"{vv['poi']}/{vv['vid']}" == key)
        bi = r["beat"]
        idx = {cid: i for i, cid in enumerate(rec["clip_ids"])}
        clips = rec["clips"]
        # find the best feasible clip id
        best_cid, best_s = None, -np.inf
        for cid in rec["clip_ids"]:
            if not coverable(rec["beats"][bi]["span"], clips[cid]["dur"]):
                continue
            s = rec["S"][bi, idx[cid]]
            if s > best_s:
                best_s, best_cid = s, cid
        chosen = rec["beats"][bi]["chosen"]
        gap = r["gap"]
        used_elsewhere = best_cid in used_by_video[key] and best_cid != chosen
        if gap >= 0.10 and used_elsewhere:
            cls = "ALLOCATION"
            alloc += 1
        elif gap < 0.05:
            cls = "COVERAGE"
            cov += 1
        elif gap >= 0.10 and not used_elsewhere:
            # better feasible clip exists and is UNUSED -> greedy bug/adjacency, still allocation-class fixable
            cls = "ALLOCATION(unused-better)"
            alloc += 1
        else:
            cls = "AMBIGUOUS"
            ambiguous += 1
        detail.append({**r, "best_cid": best_cid, "best_cos": round(best_s, 4),
                       "best_used_elsewhere": used_elsewhere, "class": cls})

    # ---- pooled (all videos) apples-to-apples: greedy vs adj_off vs global ----
    pooled = {"greedy": [], "adj_off": [], "global": [], "actual": []}
    pooled_dm = {"greedy": 0, "adj_off": 0, "global": 0, "actual": 0}
    pooled_adj = {"greedy": 0, "adj_off": 0, "global": 0, "actual": 0}
    for v, rec, actual, g_off, g_glob in allrows:
        clips = rec["clips"]
        idx = {cid: i for i, cid in enumerate(rec["clip_ids"])}
        g_on = greedy(rec, adjacency=True)
        sets = {"actual": actual, "greedy": g_on, "adj_off": g_off, "global": g_glob}
        for name, picks in sets.items():
            pooled[name].extend(list(picks_scores(rec, picks)))
            prev = None
            for cid in picks:
                cat = clips[cid]["category"]
                if prev is not None and cat == prev:
                    pooled_adj[name] += 1
                prev = cat
            for bi in range(len(rec["beats"])):
                bf = best_feasible(rec, bi)
                sc = rec["S"][bi, idx[picks[bi]]]
                if math.isfinite(bf) and (bf - sc) >= 0.15 and sc < 0.25:
                    pooled_dm[name] += 1
    pooled_summary = {
        name: {
            "median": round(med(pooled[name]), 4),
            "mean": round(float(np.mean(pooled[name])), 4),
            "share_below_0.20": round(share_below(pooled[name], 0.20), 3),
            "same_cat_back_to_back": pooled_adj[name],
            "dramatic_miss": pooled_dm[name],
            "n_beats": len(pooled[name]),
        }
        for name in ("actual", "greedy", "adj_off", "global")
    }

    out = {
        "pooled": pooled_summary,
        "per_video": summary,
        "task1_worst_misses": {
            "N": N,
            "allocation": alloc,
            "coverage": cov,
            "ambiguous": ambiguous,
            "detail": detail,
        },
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
