#!/usr/bin/env python3
"""工单② wiring evidence — runs the ACTUAL modified retrieval module on the VPS
against real POI data. Read-only. Proves:
  (1) flag OFF  -> download set byte-identical to the DEPLOYED function;
  (2) flag ON   -> relevance-seeded visual max-min, count unchanged, near-dups gone.
The new module is loaded verbatim from /tmp/retrieval_new.py (scp'd from the
worker branch); production deps resolve from the deployed repo on sys.path.
"""
import glob
import importlib.util
import json
import logging
import os
import sys

import numpy as np
from dotenv import load_dotenv

REPO = "/home/deploy/pgc_batch_worktrees/main_20260624T_huatulco_mock"  # has retrieval module
RUNS = "/home/deploy/pgc_batch_runs"
load_dotenv(os.path.join(REPO, ".env"))
sys.path.insert(0, REPO)
logging.basicConfig(level=logging.INFO, format="    [log] %(message)s")
for noisy in ("httpx", "httpcore", "hpack", "supabase", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from supabase import create_client                                   # noqa: E402
from promo.core.assign.clip_embedder import embed_texts              # noqa: E402
from promo.core.assets.retrieval import (                            # noqa: E402
    DEFAULT_MAX_CANDIDATES, DEFAULT_MIN_DOWNLOAD_CANDIDATES,
    DEFAULT_MIN_ELIGIBLE_ASSETS, DEFAULT_TOP_K_PER_QUERY,
    fetch_ready_assets, retrieve_candidates,
    candidate_asset_ids_for_download as DEPLOYED_fn,
)

# load the modified module verbatim from the worker branch
spec = importlib.util.spec_from_file_location("retrieval_new", "/tmp/retrieval_new.py")
NEW = importlib.util.module_from_spec(spec)
sys.modules["retrieval_new"] = NEW          # so @dataclass can resolve __module__
spec.loader.exec_module(NEW)

sb = create_client(os.environ["SUPABASE_URL"],
                   os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_KEY"])

POIS = [
    ("poi_f56ae0cc60d3", "Secrets Huatulco", "secrets_huatulco"),
    ("poi_2486048463ba", "Club Wyndham Bonnet Creek", "club_wyndham_bonnet_creek"),
    ("poi_741112d4f6cf", "Sandpearl Resort", "sandpearl_resort"),
    ("poi_92c8aabd3adb", "Tu Tu Tun Lodge [small]", "tu_tu_tun_lodge"),
]


def real_queries(dsub):
    hits = sorted(glob.glob(f"{RUNS}/**/*{dsub}*/**/clip_assignments_*.json", recursive=True),
                  key=os.path.getmtime, reverse=True)
    for f in hits:
        sar = json.load(open(f)).get("shared_asset_retrieval") or {}
        qs = [q.get("text") if isinstance(q, dict) else q for q in (sar.get("queries") or [])]
        qs = [q for q in qs if q]
        if qs:
            return qs
    return None


def visual_vectors(asset_ids):
    out = {}
    ids = sorted(set(asset_ids))
    for i in range(0, len(ids), 200):
        for r in (sb.table("poi_asset_visual_embeddings")
                  .select("asset_id,embedding_vector,status")
                  .in_("asset_id", ids[i:i + 200]).eq("status", "ready").execute().data) or []:
            v = np.fromstring(str(r["embedding_vector"]).strip()[1:-1], sep=",")
            if v.shape == (768,):
                out[r["asset_id"]] = v
    return out


def ndup(id_set, vis, thr=0.85):
    """near-dup pairs >=thr among id_set, whitened over the id_set's own vectors."""
    M = np.array([vis[a] for a in id_set if a in vis])
    if len(M) < 2:
        return 0, 0.0
    C = M - M.mean(axis=0)
    W = C / (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)
    S = W @ W.T
    n = len(W)
    pairs = [S[i, j] for i in range(n) for j in range(i + 1, n)]
    return int(sum(1 for p in pairs if p >= thr)), round(float(max(pairs)), 3)


print(f"{'POI':30s} {'off==deployed':13s} {'#off':>4s} {'#armed':>6s} "
      f"{'base dup':>8s} {'armed dup':>9s} {'overlap':>7s}")
print("-" * 86)
for poi_id, name, dsub in POIS:
    qs = real_queries(dsub)
    assets = fetch_ready_assets(sb, poi_id=poi_id)
    if not qs or len(assets) < DEFAULT_MIN_ELIGIBLE_ASSETS:
        print(f"{name:30s} skip (q={bool(qs)} pool={len(assets)})")
        continue
    qv = [tuple(v) for v in embed_texts(qs)]
    cands = retrieve_candidates(assets=assets, queries=qs, query_vectors=qv,
                                top_k_per_query=DEFAULT_TOP_K_PER_QUERY,
                                max_candidates=DEFAULT_MAX_CANDIDATES,
                                min_eligible_assets=DEFAULT_MIN_ELIGIBLE_ASSETS)
    kw = dict(candidates=cands, assets=assets,
              min_candidates=DEFAULT_MIN_DOWNLOAD_CANDIDATES,
              max_candidates=DEFAULT_MAX_CANDIDATES)
    deployed = DEPLOYED_fn(**kw)                                  # today, deployed code
    off = NEW.candidate_asset_ids_for_download(**kw, diversity_window=None)  # new, flag OFF
    identical = (deployed == off)

    vis = visual_vectors([a.asset_id for a in assets])
    rel = NEW.relevance_by_asset(assets, qv)
    armed = NEW.candidate_asset_ids_for_download(**kw, visual_by_asset=vis,
                                                 relevance_scores=rel, diversity_window=45)
    bdup, _ = ndup(deployed, vis)
    adup, _ = ndup(armed, vis)
    ov = len(set(deployed) & set(armed))
    print(f"{name:30s} {str(identical):13s} {len(off):4d} {len(armed):6d} "
          f"{bdup:8d} {adup:9d} {ov:5d}/30")
