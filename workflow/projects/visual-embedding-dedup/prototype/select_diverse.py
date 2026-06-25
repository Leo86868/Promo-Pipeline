#!/usr/bin/env python3
"""
REFERENCE IMPLEMENTATION — visual-diversity selection from DINOv2 clip vectors.

This is the "how to USE the visual_embedding_vector column" piece. The asset
platform produces the vectors (one per clip); a CONSUMER (e.g. PGC packer)
selects a diverse / non-redundant subset using the functions below.

It is intentionally dependency-light (numpy only) and self-contained so anyone
can read it and port it. It is NOT wired to the DB — `vectors` is whatever you
SELECT out of poi_asset_embeddings for one POI.

Verified on real data (2026-06-24, Round-2): from Huatulco (158 clips, 159
near-duplicate pool pairs) and Villa D'Este (43 clips), picking 30 yielded a
worst in-set pairwise cosine of 0.53 / 0.55 — i.e. genuinely distinct picks even
from a redundant or a small pool. See ../ROUND2-REPORT.md.
"""
from __future__ import annotations
import numpy as np


def normalize(vectors: np.ndarray) -> np.ndarray:
    """L2-normalize rows. DINOv2 vectors must be unit-length so dot == cosine.
    Forgetting this is the #1 silent bug (magnitude leaks into 'similarity')."""
    return vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)


def whiten(vectors: np.ndarray) -> np.ndarray:
    """Optional: mean-center over the POI's own clips, then renormalize.
    DINOv2 is anisotropic — unrelated clips often sit at cosine >0.8. Centering
    on the local pool widens the gap between 'same' and 'different'
    (measured: twin/similar AUC 0.841 -> 0.881). Cheap; recommended for the
    in-POI comparison used here. Do NOT persist whitened vectors — whiten at
    query time against the current pool."""
    v = vectors - vectors.mean(axis=0)
    return normalize(v)


def select_maxmin(vectors: np.ndarray,
                  k: int | None = None,
                  too_similar: float | None = None) -> list[int]:
    """Greedy farthest-point (max-min) diverse selection. Returns row indices.

    At each step it adds the clip whose similarity to the *nearest already
    chosen* clip is smallest — i.e. the most novel remaining clip. A redundant
    cluster (e.g. 40 near-identical pool aerials) contributes ~one pick, because
    once one is chosen the rest are 'close to chosen' and lose. Seeded from the
    medoid (most central clip) so an outlier/corrupt frame isn't picked first.

    Two stop modes (pass exactly one, or both — whichever triggers first):
      k            : stop after k picks (e.g. PGC wants 30).
      too_similar  : stop when the next-best pick is still closer than this
                     cosine to the chosen set, i.e. the pool has no genuinely
                     new content left. This turns 'how many?' into 'how
                     redundant is too redundant?' — more robust across pools of
                     different sizes. Calibrate on your own clips; do not
                     hardcode blindly (see threshold note in README).

    Cosine, not Euclidean: pass L2-normalized (optionally whitened) vectors.
    """
    if k is None and too_similar is None:
        raise ValueError("pass k and/or too_similar")
    n = len(vectors)
    sim = vectors @ vectors.T                       # full cosine matrix (small n)
    seed = int(sim.sum(axis=1).argmax())            # medoid = most central
    chosen = [seed]
    while len(chosen) < n:
        if k is not None and len(chosen) >= k:
            break
        rest = [i for i in range(n) if i not in chosen]
        # nearest-chosen similarity for each candidate; pick the min (most novel)
        cand = min(rest, key=lambda i: max(sim[i, c] for c in chosen))
        closeness = max(sim[cand, c] for c in chosen)
        if too_similar is not None and closeness > too_similar:
            break                                   # nothing new left
        chosen.append(cand)
    return chosen


def near_duplicate_pairs(vectors: np.ndarray, threshold: float = 0.85):
    """List clip-index pairs whose cosine >= threshold (candidate redundancies).
    Useful for an audit/report, or as a hard de-dup gate before selection."""
    sim = vectors @ vectors.T
    n = len(vectors)
    return [(i, j, float(sim[i, j]))
            for i in range(n) for j in range(i + 1, n)
            if sim[i, j] >= threshold]


if __name__ == "__main__":
    # tiny smoke test on random vectors
    rng = np.random.default_rng(0)
    v = normalize(rng.standard_normal((50, 768)))
    picks = select_maxmin(v, k=10)
    print("picked", len(picks), "indices:", picks)
    picks2 = select_maxmin(whiten(v), too_similar=0.5)
    print("threshold-stop picked", len(picks2))
