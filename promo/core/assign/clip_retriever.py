"""Stateless cosine ranking over pre-embedded clip metadata.

Pairs with ``clip_embedder``/the platform embedding table: vectors come
pre-computed, and this module ranks them against beat-text queries for
the packer (``rank_per_query``). The legacy LLM-assigner inventory-narrowing
API (``top_k``/``union_of_top_k``) retired with that chain on 2026-06-11.

**Stateless by design** — no lru_cache, no module memo; every call
re-embeds and re-ranks. Query embedding is delegated via the
``embed_query_fn`` parameter (defaults to ``clip_embedder.embed_texts``;
trivially injectable in tests).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


EmbedQueryFn = Callable[[list[str]], list[list[float]]]


def _default_embed_query_fn(queries: list[str]) -> list[list[float]]:
    from promo.core.assign.clip_embedder import embed_texts

    return embed_texts(queries)


def _extract_clip_vectors(
    clip_metadata: list[dict],
) -> tuple[list[str], np.ndarray]:
    """Pull ``(clip_ids, matrix)`` out of metadata. Caller already validated non-empty."""
    ids: list[str] = []
    vecs: list[list[float]] = []
    for clip in clip_metadata:
        if "embedding" not in clip:
            raise ValueError(
                f"clip_metadata entry missing 'embedding' field: id={clip.get('id')!r}"
            )
        ids.append(str(clip["id"]))
        vecs.append(clip["embedding"])
    matrix = np.asarray(vecs, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError(
            f"clip embeddings must be 2-D (got shape {matrix.shape})"
        )
    return ids, matrix


def _cosine_rank(
    query_vec: np.ndarray, matrix: np.ndarray, ids: list[str],
) -> list[tuple[str, float]]:
    """Return ``[(clip_id, cosine_similarity), ...]`` sorted by similarity descending."""
    q = query_vec.astype(np.float32).reshape(-1)
    if not np.isfinite(q).all():
        raise ValueError("query vector contains NaN or Inf")
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        raise ValueError("query vector has zero norm")
    mat_norms = np.linalg.norm(matrix, axis=1)
    safe_norms = np.where(mat_norms == 0, 1.0, mat_norms)
    sims = (matrix @ q) / (safe_norms * q_norm)
    sims = np.where(mat_norms == 0, -np.inf, sims)
    order = np.argsort(-sims)
    return [(ids[i], float(sims[i])) for i in order]


def rank_per_query(
    queries: list[str],
    clip_metadata: list[dict],
    *,
    embed_query_fn: Optional[EmbedQueryFn] = None,
) -> list[list[tuple[str, float]]]:
    """翻转二 B2 — full ranked candidates PER query (one list per beat).

    Unlike the retired ``union_of_top_k`` (which fed the LLM assigner a deduped id pool),
    the packer needs every beat's own ranking WITH scores so it can walk
    down the list applying house rules (no reuse, duration coverage,
    window rotation, adjacency penalty). Returns, for each query, the
    complete ``[(clip_id, cosine_similarity), ...]`` ordering over the
    pool — the packer does its own filtering, so no k-truncation here.

    Queries are embedded in ONE batched ``embed_query_fn`` call.
    **No memoization** (same F3/stateless rationale as the rest of this
    module — and the packer may re-plan beats after a seatbelt stretch).
    """
    if not clip_metadata:
        raise ValueError("clip_metadata is empty — no pool to retrieve from")
    if not queries:
        raise ValueError("queries is empty — beat planner emits ≥1 beat per segment")

    embed_fn = embed_query_fn or _default_embed_query_fn
    query_vecs = embed_fn(queries)
    if len(query_vecs) != len(queries):
        raise RuntimeError(
            f"embed_query_fn returned {len(query_vecs)} vectors "
            f"for {len(queries)} queries"
        )

    ids, matrix = _extract_clip_vectors(clip_metadata)
    return [
        _cosine_rank(np.asarray(qvec, dtype=np.float32), matrix, ids)
        for qvec in query_vecs
    ]
