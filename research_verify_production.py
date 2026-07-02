"""Offline verification of the SHIPPED production code path.

Drives the real ``promo.core.assign.packer.pack_clips`` (greedy vs
``global_assignment=True``) on real recorded sidecars, re-embedding narration
+ clip texts with the repo's own retriever so the rankings match production.
Confirms the consolidation reproduces the offline-tuned metrics: dramatic
misses → 0, same-category back-to-back kept low (NOT the naive blowup),
median/mean text-cosine up, share<0.20 down.

NOTE: run3x3's exact sidecars are gone from disk (only rendered mp4s remain);
this runs on the spike-compatible sidecar batches that ARE on disk.

ZERO writes. Reads sidecars + embeds (network) read-only.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys

import numpy as np

from promo.core.assign import clip_retriever
from promo.core.assign.packer import pack_clips

ROOTS = sys.argv[1:] or ["."]


def find_videos():
    out = []
    for root in ROOTS:
        for mq in glob.glob(os.path.join(root, "**", "match_quality_*.json"), recursive=True):
            d = os.path.dirname(mq)
            if glob.glob(os.path.join(d, "clip_assignments_*.json")) and \
               glob.glob(os.path.join(d, "run_manifest_*.json")):
                out.append(d)
    return sorted(set(out))


def load(d):
    mq = json.load(open(glob.glob(os.path.join(d, "match_quality_*.json"))[0]))
    ca = json.load(open(glob.glob(os.path.join(d, "clip_assignments_*.json"))[0]))
    rm = json.load(open(glob.glob(os.path.join(d, "run_manifest_*.json"))[0]))
    snap = rm.get("asset_snapshot") or []
    if not snap:
        return None
    metadata, durations = [], {}
    for a in snap:
        cid = str(a.get("clip_id")).zfill(4)
        text = (a.get("embedding_text") or a.get("scene_description") or "").strip()
        if not text:
            continue
        metadata.append({"id": cid, "category": str(a.get("category") or ""),
                         "scene_description": text, "dominant_motion_phase": "",
                         "text": text})
        durations[cid] = float(a.get("source_duration_sec") or a.get("duration_sec") or 0.0)
    if len(metadata) < 2:
        return None
    variant = ca["variants"][0]
    assigns = variant["assignments"]
    beats, queries = [], []
    for i, asn in enumerate(assigns):
        m = mq[i] if i < len(mq) else {}
        q = (m.get("narration_phrase") or "").strip()
        if not q:
            continue
        beats.append({"segment": int(asn.get("segment_idx") or asn.get("segment") or 1),
                      "start_word_idx": len(beats), "end_word_idx": len(beats)})
        queries.append(q)
    if not beats:
        return None
    return {"dir": d, "metadata": metadata, "durations": durations,
            "beats": beats, "queries": queries}


# embed clip texts -> rankings via the real retriever (matches production ranking)
_CACHE = {}


def embed(texts):
    from promo.core.assign.clip_embedder import embed_texts
    todo = [t for t in dict.fromkeys(texts) if t and t not in _CACHE]
    for i in range(0, len(todo), 256):
        for t, v in zip(todo[i:i+256], embed_texts(todo[i:i+256])):
            _CACHE[t] = v
    return {t: _CACHE[t] for t in texts if t}


def build_rankings(rec):
    emb = embed([m["text"] for m in rec["metadata"]] + rec["queries"])
    pool = [dict(m, embedding=emb[m["text"]]) for m in rec["metadata"]]
    return clip_retriever.rank_per_query(
        rec["queries"], pool, embed_query_fn=lambda qs: [emb[q] for q in qs])


# synthetic word_timestamps long enough that span<=duration for every beat
def wts_for(n):
    # one word per beat, 0.1s each -> display spans ~0.1s, always coverable.
    return [{"word": f"w{i}", "start": round(i*0.1, 3), "end": round((i+1)*0.1, 3)}
            for i in range(n + 1)]


def metrics(rec, rankings, assignments):
    idx_score = [{str(cid).zfill(4): sc for cid, sc in r} for r in rankings]
    cats = {m["id"]: m["category"] for m in rec["metadata"]}
    sc, dm, adj, prev = [], 0, 0, None
    for bi, a in enumerate(assignments):
        cid = a["clip_id"]
        s = idx_score[bi].get(cid, float("-inf"))
        sc.append(s)
        bf = max((v for v in idx_score[bi].values() if math.isfinite(v)), default=float("-inf"))
        if math.isfinite(bf) and (bf - s) >= 0.15 and s < 0.25:
            dm += 1
        cat = cats.get(cid, "")
        if prev is not None and cat == prev:
            adj += 1
        prev = cat
    return sc, dm, adj


def main():
    dirs = find_videos()
    print(f"found {len(dirs)} videos under {ROOTS}", flush=True)
    pooled = {"greedy": {"sc": [], "dm": 0, "adj": 0},
              "global": {"sc": [], "dm": 0, "adj": 0}}
    nbeats = 0
    nvid = 0
    for d in dirs:
        try:
            rec = load(d)
        except Exception as e:
            print("skip", d, e); continue
        if not rec:
            continue
        rankings = build_rankings(rec)
        beats = rec["beats"]
        wts = wts_for(len(beats))
        common = dict(word_timestamps=wts, clip_durations=rec["durations"],
                      clip_metadata=rec["metadata"], max_beat_sec=4.0)
        try:
            g_a, _ = pack_clips(beats, rankings, **common)
            gl_a, gl_p = pack_clips(beats, rankings, global_assignment=True, **common)
        except Exception as e:
            print("packer-skip", d.split("/")[-2], type(e).__name__, e); continue
        for name, asn in (("greedy", g_a), ("global", gl_a)):
            s, dm, adj = metrics(rec, rankings, asn)
            pooled[name]["sc"].extend(s)
            pooled[name]["dm"] += dm
            pooled[name]["adj"] += adj
        nbeats += len(beats)
        nvid += 1

    print(f"\nverified on {nvid} videos / {nbeats} beats (SHIPPED pack_clips)")
    print("arm      median   mean   share<0.20  dramatic_miss  same_cat_b2b")
    for name in ("greedy", "global"):
        s = pooled[name]["sc"]
        med = round(float(np.median(s)), 4) if s else float("nan")
        mean = round(float(np.mean(s)), 4) if s else float("nan")
        sh = round(float((np.array(s) < 0.20).mean()), 3) if s else float("nan")
        print(f"{name:8s} {med:7.4f} {mean:7.4f}  {sh:9.3f}  "
              f"{pooled[name]['dm']:13d}  {pooled[name]['adj']:11d}")


if __name__ == "__main__":
    main()
