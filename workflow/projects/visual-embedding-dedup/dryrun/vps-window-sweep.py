#!/usr/bin/env python3
"""工单② STEP-1 dry-run — VPS edition. MEASUREMENT ONLY, read-only.

Does NOT touch retrieval.py / production path. Compares today's
relevance-30 download set vs relevance-seeded greedy max-min (whitened
DINOv2 visual cosine) across consideration windows {35,45,60}, on real
POIs, using REAL per-POI relevance:

  relevance = max cosine of each clip's text embedding to the POI's REAL
  production script-segment queries (pulled from that POI's most-recent
  clip_assignments_*.json -> shared_asset_retrieval.queries), embedded
  live with the production OpenRouter key. No centroid proxy.

Whitening = mean-center over the top-60 working pool, then L2 (handoff
consumer-side whitening). Same whitened space for every selection.
"""
import glob
import json
import os
import sys

import numpy as np
from dotenv import load_dotenv

REPO = "/home/deploy/promo-pipeline-readiness"
RUNS = "/home/deploy/pgc_batch_runs"
load_dotenv(os.path.join(REPO, ".env"))
sys.path.insert(0, REPO)

from supabase import create_client  # noqa: E402
from promo.core.assign.clip_embedder import embed_texts  # noqa: E402

URL = os.environ["SUPABASE_URL"]
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"]
sb = create_client(URL, KEY)

# (poi_id, display name, dir substring to find the real-script artifact)
POIS = [
    ("poi_f56ae0cc60d3", "Secrets Huatulco [hard]", "secrets_huatulco"),
    ("poi_2486048463ba", "Club Wyndham Bonnet Creek [hard]", "club_wyndham_bonnet_creek"),
    ("poi_741112d4f6cf", "Sandpearl Resort", "sandpearl_resort"),
    ("poi_851a5b2ebb1b", "Bolt Farm Treehouse", "bolt_farm_treehouse"),
    ("poi_edee371de594", "Ranch at Rock Creek", "the_ranch_at_rock_creek"),
    ("poi_96e6a36b8e56", "Ambiente Sedona [desert]", "ambiente_sedona"),
    ("poi_92c8aabd3adb", "Tu Tu Tun Lodge [small pool]", "tu_tu_tun_lodge"),
    ("poi_00bf1e49b204", "Marriott Marquis Houston [urban]", "marriott_marquis_houston"),
    ("poi_344b66371fce", "Little Palm Island [beach]", "little_palm_island"),
    ("poi_f0d7b1d50f3d", "Southall Farm & Inn", "southall_farm"),
    ("poi_abaecf14178d", "CIVANA Wellness", "civana_wellness"),
    ("poi_e6fba068792b", "The Cliffs Hotel & Spa", "the_cliffs_hotel_and_spa"),
    ("poi_e3809c0e2bca", "Lido Beach Resort", "lido_beach_resort"),
    ("poi_f921f5ba7960", "Turquoise Place [smaller]", "turquoise_place"),
    ("poi_7ab94f48df36", "Blue Hills Ranch", "blue_hills_ranch"),
]

NDUP, SAME = 0.85, 0.92          # handoff: same-place / same-shot
WIN = [35, 45, 60]


def real_queries(dir_sub):
    """Newest clip_assignments artifact's real production queries."""
    hits = glob.glob(f"{RUNS}/**/*{dir_sub}*/**/clip_assignments_*.json", recursive=True)
    hits.sort(key=os.path.getmtime, reverse=True)
    for f in hits:
        sar = (json.load(open(f)).get("shared_asset_retrieval") or {})
        qs = sar.get("queries") or []
        qs = [q.get("text") if isinstance(q, dict) else q for q in qs]
        qs = [q for q in qs if q]
        if qs:
            return qs, os.path.basename(f)
    return None, None


def _vec(s, dim):
    if isinstance(s, (list, tuple)):
        a = np.asarray(s, float)
    else:
        a = np.fromstring(str(s).strip()[1:-1], sep=",")
    return a if a.shape == (dim,) else None


def fetch_pool(poi_id):
    """Eligible clips (usage<3, text+visual ready) -> (asset_ids, text NxD, visual NxV)."""
    rows = (sb.table("poi_asset_valid_clips")
            .select("asset_id,usage_count,embedding_status,visual_embedding_status")
            .eq("poi_id", poi_id).execute().data) or []
    ids = sorted({r["asset_id"] for r in rows
                 if (r.get("usage_count") or 0) < 3
                 and r.get("embedding_status") == "ready"
                 and r.get("visual_embedding_status") == "ready"})
    if not ids:
        return [], None, None
    tv, vv = {}, {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        for r in (sb.table("poi_asset_embeddings").select("asset_id,embedding_vector,status")
                  .in_("asset_id", chunk).eq("status", "ready").execute().data) or []:
            v = _vec(r["embedding_vector"], 1536)
            if v is not None:
                tv[r["asset_id"]] = v
        for r in (sb.table("poi_asset_visual_embeddings").select("asset_id,embedding_vector,status")
                  .in_("asset_id", chunk).eq("status", "ready").execute().data) or []:
            v = _vec(r["embedding_vector"], 768)
            if v is not None:
                vv[r["asset_id"]] = v
    keep = [a for a in ids if a in tv and a in vv]
    if not keep:
        return [], None, None
    return keep, np.vstack([tv[a] for a in keep]), np.vstack([vv[a] for a in keep])


def l2(m):
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)


def maxmin(sim, k, seed=0):
    """Greedy farthest-point, relevance-#1 seed. sim = whitened cosine matrix."""
    chosen = [seed]
    while len(chosen) < k and len(chosen) < sim.shape[0]:
        rest = [i for i in range(sim.shape[0]) if i not in chosen]
        nxt = min(rest, key=lambda i: max(sim[i, c] for c in chosen))
        chosen.append(nxt)
    return chosen


def metrics(idx, sim, relrank):
    """idx = positions into the top-60 working pool (relrank[i] = relevance rank, 1-based)."""
    s = sorted(idx, key=lambda i: relrank[i])
    pairs = [sim[a, b] for ai, a in enumerate(s) for b in s[ai + 1:]]
    pairs = np.array(pairs) if pairs else np.array([0.0])
    leaders = sum(1 for ai, a in enumerate(s)
                  if not any(sim[a, b] >= NDUP for b in s[:ai]))
    return dict(ndup85=int((pairs >= NDUP).sum()), ndup92=int((pairs >= SAME).sum()),
                worst=round(float(pairs.max()), 3), clusters=leaders,
                deepest=int(max(relrank[i] for i in idx)),
                top15=sum(1 for i in idx if relrank[i] <= 15))


print(f"{'POI':34s} {'selection':12s} {'dup85':>5s} {'dup92':>5s} {'worst':>6s} "
      f"{'clust':>5s} {'deep':>4s} {'top15':>5s} {'rel%':>5s}")
print("-" * 96)
agg = {f"maxmin@{w}": [] for w in WIN}
for poi_id, name, dsub in POIS:
    qs, art = real_queries(dsub)
    if not qs:
        print(f"{name:34s} NO real-query artifact found ({dsub})")
        continue
    ids, T, V = fetch_pool(poi_id)
    if not ids or len(ids) < 30:
        print(f"{name:34s} pool too small ({len(ids)})")
        continue
    Q = l2(np.asarray(embed_texts(qs), float))
    rel = (l2(T) @ Q.T).max(axis=1)                       # real relevance per clip
    order = np.argsort(-rel)                              # relevance rank
    top = order[:60]
    relrank = {int(p): r + 1 for r, p in enumerate(order)}  # global rank (1-based)
    Wn = l2(V[top] - V[top].mean(axis=0))                 # whiten over top-60 pool
    sim = Wn @ Wn.T
    pos = {int(p): i for i, p in enumerate(top)}          # global idx -> row in top-60
    rr = {i: relrank[int(top[i])] for i in range(len(top))}
    base = [pos[int(p)] for p in order[:30]]              # relevance-30 baseline
    relsum30 = float(rel[order[:30]].sum())
    rows = [("relevance-30", base)]
    for w in WIN:
        cand = [i for i in range(len(top)) if rr[i] <= w]
        seed = min(cand, key=lambda i: rr[i])
        sub = maxmin(sim[np.ix_(cand, cand)], 30, cand.index(seed))
        rows.append((f"maxmin@{w}", [cand[j] for j in sub]))
    for lbl, idx in rows:
        m = metrics(idx, sim, rr)
        relpct = round(100 * float(rel[[int(top[i]) for i in idx]].sum()) / relsum30)
        if lbl != "relevance-30":
            agg[lbl].append(m)
        print(f"{name:34s} {lbl:12s} {m['ndup85']:5d} {m['ndup92']:5d} {m['worst']:6.3f} "
              f"{m['clusters']:5d} {m['deepest']:4d} {m['top15']:5d} {relpct:4d}%")
    print(f"   pool={len(ids):3d}  queries={len(qs)}  src={art}")

print("-" * 96)
for w in WIN:
    a = agg[f"maxmin@{w}"]
    if a:
        print(f"AGG maxmin@{w:<2d}  mean dup85={np.mean([x['ndup85'] for x in a]):.1f}  "
              f"mean clusters={np.mean([x['clusters'] for x in a]):.1f}/30  "
              f"mean worst={np.mean([x['worst'] for x in a]):.3f}  "
              f"mean top15kept={np.mean([x['top15'] for x in a]):.1f}  "
              f"POIs 0-dup={sum(1 for x in a if x['ndup85'] == 0)}/{len(a)}")
