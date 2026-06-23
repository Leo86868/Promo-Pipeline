"""Near-dup phase-1 simulate (read-only, no production writes).

Replays real finished videos through the PRODUCTION assignment functions
(``clip_retriever.rank_per_query`` + ``packer.pack_clips``) with the gate
OFF (baseline) and ON at several thresholds, and measures benefit vs cost.

Faithfulness self-check: the gate-OFF reconstruction must reproduce the
real timeline picks. The report prints that match rate — trust the numbers
only to the extent baseline reproduction is high.

⚠️ The embedding is TEXT (scene-description) based, not pixels — this gate
catches description-similar clips, not a true visual dedup. See
workflow/near-dup-recon.md.

Run on the VPS (DB + OpenRouter key in .env):
    cd <worktree-with-this-branch's-packer>
    set -a; . /path/to/.env; set +a
    PYTHONPATH=. python3 workflow/near_dup_simulate.py
"""
import glob
import json
import os
import statistics

from supabase import create_client

from promo.core.assign import clip_retriever
from promo.core.assign.packer import pack_clips, unit_vector

ROOT = "/home/deploy/pgc_batch_runs"
BATCHES = [
    "winners_15x3_20260620T225732Z",
    "stock_5x3_20260617T212011Z",
    "topup_secrets_zion_2x2_20260619T060057Z",
    "stock_1x2_20260623T002353Z",
]
THRESH = [0.80, 0.82, 0.85, 0.88, 0.90]
MAX_BEAT_SEC = 4.0

url = os.environ["SUPABASE_URL"]
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
client = create_client(url, key)


def parsevec(v):
    if isinstance(v, str):
        v = json.loads(v)
    return [float(x) for x in v]


# ---- 1) gather videos + needed asset ids ----
videos = []
need = set()
for b in BATCHES:
    for mf in sorted(glob.glob(f"{ROOT}/{b}/*/video_*/run_manifest_*.json")):
        d = json.load(open(mf))
        snap = {a["asset_id"]: a for a in d["asset_snapshot"]}
        vdir = os.path.dirname(mf)
        mqf = glob.glob(f"{vdir}/match_quality_*.json")
        phrase_by_clip = {}
        if mqf:
            for e in json.load(open(mqf[0])):
                phrase_by_clip[str(e["clip_id"]).zfill(4)] = e.get("narration_phrase") or ""
        # Real packer beats only — drop renderer-inserted bridge clips
        # (segment/display times None) which are not packer picks.
        te = [t for t in d["timeline_entries"]
              if t.get("segment") is not None and t.get("clip_id")
              and t.get("display_start_sec") is not None
              and t.get("display_end_sec") is not None]
        beats_clip = [str(t["clip_id"]).zfill(4) for t in te]
        spans = [max(0.0, float(t["display_end_sec"]) - float(t["display_start_sec"])) for t in te]
        segs = [int(t["segment"]) for t in te]
        videos.append({"mf": mf, "snap": snap, "phrase": phrase_by_clip,
                       "beats_clip": beats_clip, "spans": spans, "segs": segs})
        need.update(snap.keys())

# ---- fetch embeddings ----
emb_by_asset = {}
ids = list(need)
for i in range(0, len(ids), 150):
    rows = client.table("poi_asset_embeddings").select(
        "asset_id,embedding_vector").in_("asset_id", ids[i:i + 150]).execute().data
    for r in rows:
        if r.get("embedding_vector") is not None and r["asset_id"] not in emb_by_asset:
            emb_by_asset[r["asset_id"]] = parsevec(r["embedding_vector"])

print(f"videos={len(videos)} distinct_assets={len(need)} embeddings={len(emb_by_asset)}")


def pairs_ge(ids, emb_by_clip, t):
    us = [unit_vector(emb_by_clip[x]) for x in ids if x in emb_by_clip]
    cnt = 0
    for i in range(len(us)):
        for j in range(i + 1, len(us)):
            if us[i] and us[j] and sum(p * q for p, q in zip(us[i], us[j])) >= t:
                cnt += 1
    return cnt


def real_redundant(ids, emb_by_clip, t):
    """EXACT benefit on the SHIPPED picks (no ranking reconstruction):
    greedy in real order, count picks that are near-dup (cos>=t) to an
    earlier KEPT pick — i.e. the beats the gate would swap out."""
    kept = []
    redundant = 0
    for x in ids:
        u = unit_vector(emb_by_clip.get(x))
        if u and kept and max(sum(p * q for p, q in zip(u, k)) for k in kept) >= t:
            redundant += 1
        elif u:
            kept.append(u)
    return redundant


# ---- 2) per video simulate ----
results = []
missing_q = 0
for v in videos:
    snap = v["snap"]
    pool, dur, emb_by_clip = [], {}, {}
    for asset_id, a in snap.items():
        cid = str(a.get("clip_id")).zfill(4)
        vec = emb_by_asset.get(asset_id)
        if vec is None or a.get("source_duration_sec") is None:
            continue
        pool.append({"id": cid, "embedding": vec,
                     "category": str(a.get("category") or ""),
                     "dominant_motion_phase": str(a.get("dominant_motion_phase") or "")})
        dur[cid] = float(a["source_duration_sec"])
        emb_by_clip[cid] = vec
    spans = v["spans"]
    n = len(spans)
    starts = [0.0]
    for s in spans:
        starts.append(starts[-1] + s)
    wts = [{"word": f"w{i}", "start": round(starts[i], 3), "end": round(starts[i + 1], 3)}
           for i in range(n)]
    beats = [{"segment": v["segs"][i], "start_word_idx": i, "end_word_idx": i} for i in range(n)]
    queries = []
    for i in range(n):
        q = v["phrase"].get(v["beats_clip"][i], "")
        if not q:
            missing_q += 1
            q = "x"
        queries.append(q)
    rankings = clip_retriever.rank_per_query(queries, pool)
    base_assign, base_prov = pack_clips(
        beats, rankings, word_timestamps=wts, clip_durations=dur,
        clip_metadata=pool, max_beat_sec=MAX_BEAT_SEC)
    base_ids = [a["clip_id"] for a in base_assign]
    real_ids = v["beats_clip"]
    match = sum(1 for x, y in zip(base_ids, real_ids) if x == y)
    base_scores = [p.get("score") for p in base_prov["picks"]]
    pt = {}
    for T in THRESH:
        g_assign, g_prov = pack_clips(
            beats, rankings, word_timestamps=wts, clip_durations=dur,
            clip_metadata=pool, max_beat_sec=MAX_BEAT_SEC, near_dup_threshold=T)
        g_ids = [a["clip_id"] for a in g_assign]
        g_scores = [p.get("score") for p in g_prov["picks"]]
        changed = [i for i in range(n) if g_ids[i] != base_ids[i]]
        drops = []
        for i in changed:
            bs = base_scores[i] if base_scores[i] is not None else 0.0
            gs = g_scores[i] if g_scores[i] is not None else 0.0
            drops.append(bs - gs)
        pt[T] = {
            "changed": len(changed),
            "drops": drops,
            "pairs_base": pairs_ge(base_ids, emb_by_clip, T),
            "pairs_gated": pairs_ge(g_ids, emb_by_clip, T),
            "relaxed": len(g_prov.get("diversity_relaxed_beats", [])),
            # EXACT on shipped picks (reconstruction-independent):
            "real_redundant": real_redundant(real_ids, emb_by_clip, T),
            "real_pairs": pairs_ge(real_ids, emb_by_clip, T),
        }
    results.append({"mf": v["mf"], "n": n, "match": match, "pt": pt})

# ---- 3) aggregate report ----
tot_beats = sum(r["n"] for r in results)
tot_match = sum(r["match"] for r in results)
print(f"\nbaseline reconstruction faithfulness: {tot_match}/{tot_beats} beats "
      f"= {100*tot_match/tot_beats:.1f}% reproduce the real pick "
      f"(missing narration phrases backfilled: {missing_q})")
print(f"videos={len(results)}  total_beats={tot_beats}\n")
print("=== EXACT benefit on SHIPPED picks (reconstruction-independent) ===")
print(f"{'thresh':>6} {'redundant_beats':>15} {'%beats':>7} {'vids_affected':>13} {'dup_pairs_present':>17}")
for T in THRESH:
    red = sum(r["pt"][T]["real_redundant"] for r in results)
    vids = sum(1 for r in results if r["pt"][T]["real_redundant"] > 0)
    rpairs = sum(r["pt"][T]["real_pairs"] for r in results)
    print(f"{T:>6} {red:>15} {100*red/tot_beats:>6.1f}% {vids:>13} {rpairs:>17}")
print("  redundant_beats = shipped clips the gate would swap (near-dup to an earlier kept pick)\n")

print(f"=== COST estimate via reconstructed replay (baseline repro {100*tot_match/tot_beats:.0f}% — directional) ===")
print(f"{'thresh':>6} {'beats_changed':>13} {'relaxed(饿到)':>13} {'med_score_drop':>14} {'p90_drop':>9} {'max_drop':>9}")
for T in THRESH:
    changed = sum(r["pt"][T]["changed"] for r in results)
    relaxed = sum(r["pt"][T]["relaxed"] for r in results)
    alldrops = [d for r in results for d in r["pt"][T]["drops"]]
    med = statistics.median(alldrops) if alldrops else 0.0
    p90 = statistics.quantiles(alldrops, n=10)[8] if len(alldrops) > 10 else (max(alldrops) if alldrops else 0.0)
    mx = max(alldrops) if alldrops else 0.0
    print(f"{T:>6} {changed:>13} {relaxed:>13} {med:>14.3f} {p90:>9.3f} {mx:>9.3f}")
print("\ncost = score_drop = beat↔clip match cosine given up when swapping to next-best diverse clip")
print("饿到  = relaxed beats where NO diverse clip existed and the gate allowed the dup")
