"""Sprint 12a — stateless top-k retrieval over pre-embedded clip metadata.

Pairs with ``clip_embedder``: the embedder persists a per-POI sidecar of
clip vectors, and this retriever ranks those vectors against one or more
phrase queries via numpy cosine similarity.

**Stateless by design.** ``top_k`` and ``union_of_top_k`` both re-read
``clip_metadata`` and re-embed the query on every call — no lru_cache,
no module-level memo. This is load-bearing for Sprint 12b's F3 retry
path: when Gemini #1 regenerates the script on retry, the phrase queries
change, and any memoization would surface stale top-k results.

No file I/O inside ``top_k``. Query embedding is delegated via the
``embed_query_fn`` parameter, which defaults to
``clip_embedder.embed_texts`` in production but is trivially injectable
in tests.
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


def top_k_by_vector(
    query_vec: list[float] | np.ndarray,
    clip_metadata: list[dict],
    k: int = 6,
) -> list[str]:
    """Pure-numpy top-k. Exposed for callers that already hold a query vector
    (e.g. 12b's ``union_of_top_k`` batches query embedding upstream).

    No network I/O. No file I/O. No memoization.
    """
    if k <= 0:
        raise ValueError(f"k must be positive (got {k})")
    if not clip_metadata:
        raise ValueError("clip_metadata is empty — no pool to retrieve from")

    ids, matrix = _extract_clip_vectors(clip_metadata)
    pool_size = len(ids)
    effective_k = k
    if k > pool_size:
        logger.debug(
            "top_k clamped: k=%d > pool_size=%d; returning %d",
            k, pool_size, pool_size,
        )
        effective_k = pool_size

    q = np.asarray(query_vec, dtype=np.float32)
    ranked = _cosine_rank(q, matrix, ids)
    return [cid for cid, _ in ranked[:effective_k]]


def top_k(
    phrase_query: str,
    clip_metadata: list[dict],
    k: int = 6,
    *,
    embed_query_fn: Optional[EmbedQueryFn] = None,
) -> list[str]:
    """Return up to ``k`` clip_ids ranked by cosine similarity to ``phrase_query``.

    ``clip_metadata`` must be a list of dicts each carrying at minimum
    ``id`` and ``embedding`` (1-D float list). The embedding dimension
    must match across entries.

    ``embed_query_fn`` — accepts ``list[str]`` and returns ``list[list[float]]``.
    Defaults to ``clip_embedder.embed_texts`` at first call (lazy import).
    Tests inject a stub to avoid network I/O.

    **Raises**
      - ``ValueError`` on ``k <= 0`` or empty ``clip_metadata``.

    **Clamps** ``k`` to ``len(clip_metadata)`` with a DEBUG log when ``k > pool_size``.

    **No memoization.** A repeated call issues a fresh embed + rank.
    """
    if k <= 0:
        raise ValueError(f"k must be positive (got {k})")
    if not clip_metadata:
        raise ValueError("clip_metadata is empty — no pool to retrieve from")

    embed_fn = embed_query_fn or _default_embed_query_fn
    query_vecs = embed_fn([phrase_query])
    if not query_vecs:
        raise RuntimeError("embed_query_fn returned no vectors")
    return top_k_by_vector(query_vecs[0], clip_metadata, k=k)


def rank_per_query(
    queries: list[str],
    clip_metadata: list[dict],
    *,
    embed_query_fn: Optional[EmbedQueryFn] = None,
) -> list[list[tuple[str, float]]]:
    """翻转二 B2 — full ranked candidates PER query (one list per beat).

    Unlike ``union_of_top_k`` (which feeds Gemini #2 a deduped id pool),
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


def union_of_top_k(
    narration_queries: list[str],
    clip_metadata: list[dict],
    k: int = 6,
    *,
    embed_query_fn: Optional[EmbedQueryFn] = None,
) -> tuple[list[str], int]:
    """Return ``(deduped_clip_ids, union_size)`` — first-occurrence-ordered.

    Batches the query embedding into a single ``embed_query_fn`` call for
    efficiency (one OpenAI round-trip instead of N).

    Parameter is ``narration_queries`` because Sprint 12b feeds this the
    per-segment narration text (``script["segments"][i]["text"]``), not
    phrase-level sub-spans. Phrase boundaries are an EMITTED product of
    Gemini #2 (``start_word_idx``/``end_word_idx`` in the assignment
    output), so they do not exist at retrieval time, which runs BEFORE
    Gemini #2. Segment text is the narration unit available pre-Gemini-#2.

    Sprint 12b consumer: feeds the deduped list into the Gemini #2 inventory
    block. ``union_size`` lets 12b implement the "union_size ≥ phrase_count
    OR fall back to full pool" rule the advisor flagged as hazard #2.

    **No memoization.** Each call re-embeds queries and re-ranks. Load-bearing
    for F3 retry correctness: Gemini #1 may rewrite the script on retry,
    changing ``narration_queries``; a memoized top-k would return stale
    results.
    """
    if k <= 0:
        raise ValueError(f"k must be positive (got {k})")
    if not clip_metadata:
        raise ValueError("clip_metadata is empty — no pool to retrieve from")
    if not narration_queries:
        raise ValueError(
            "narration_queries is empty — upstream pipeline error "
            "(Gemini #1 should never produce a zero-segment script)"
        )

    embed_fn = embed_query_fn or _default_embed_query_fn
    query_vecs = embed_fn(narration_queries)
    if len(query_vecs) != len(narration_queries):
        raise RuntimeError(
            f"embed_query_fn returned {len(query_vecs)} vectors "
            f"for {len(narration_queries)} queries"
        )

    seen: set[str] = set()
    union: list[str] = []
    for qvec in query_vecs:
        for cid in top_k_by_vector(qvec, clip_metadata, k=k):
            if cid not in seen:
                seen.add(cid)
                union.append(cid)
    return union, len(union)
