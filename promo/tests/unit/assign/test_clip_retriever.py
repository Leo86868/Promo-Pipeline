"""Unit tests for promo.core.assign.clip_retriever."""

import json
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import patch, MagicMock

from pathlib import Path

import pytest

def _unit_vec(dim_idx, dim=4):
    v = [0.0] * dim
    v[dim_idx] = 1.0
    return v

def _make_retriever_fixture():
    """10 clips with hand-crafted 4-D unit/near-unit vectors, known ranking."""
    return [
        {"id": f"c{i}", "embedding": _unit_vec(i % 4)} for i in range(10)
    ]

class TestSprint12aRetrieverTopK:
    """AC5 — cosine ranking, stateless."""

    def test_top_k_ranks_by_cosine_descending(self):
        from promo.core.assign.clip_retriever import top_k_by_vector

        md = [
            {"id": "c1", "embedding": [1.0, 0.0, 0.0]},
            {"id": "c2", "embedding": [0.0, 1.0, 0.0]},
            {"id": "c3", "embedding": [0.9, 0.1, 0.0]},
            {"id": "c4", "embedding": [0.7, 0.7, 0.0]},
            {"id": "c5", "embedding": [-1.0, 0.0, 0.0]},
        ]
        # Query aligned with +x → ranking c1 > c3 > c4 > c2 > c5
        out = top_k_by_vector([1.0, 0.0, 0.0], md, k=3)
        assert out == ["c1", "c3", "c4"]

    def test_top_k_is_stateless(self):
        """AC5 + AC7 — calling `top_k` twice must re-invoke the embed fn.
        No memoization across calls (load-bearing for F3 retry)."""
        from promo.core.assign.clip_retriever import top_k

        md = [
            {"id": "c1", "embedding": [1.0, 0.0]},
            {"id": "c2", "embedding": [0.0, 1.0]},
        ]
        calls = {"n": 0}

        def stub(queries):
            calls["n"] += 1
            return [[1.0, 0.0] for _ in queries]

        top_k("query A", md, k=1, embed_query_fn=stub)
        top_k("query A", md, k=1, embed_query_fn=stub)
        assert calls["n"] == 2, (
            "top_k appears to memoize; must re-call embed_fn every invocation"
        )

    def test_no_lru_cache_in_retriever_source(self):
        """Grep-style assertion — no @lru_cache or @cache decorators."""
        import inspect
        from promo.core.assign import clip_retriever

        src = inspect.getsource(clip_retriever)
        assert "@lru_cache" not in src, "lru_cache present — retriever must be stateless"
        assert "@functools.cache" not in src
        assert re.search(r"^\s*@cache\s*$", src, re.MULTILINE) is None

class TestSprint12aRetrieverBounds:
    """AC6 — k-bounds + empty-pool enforcement + NaN/Inf guard."""

    def test_k_zero_raises(self):
        from promo.core.assign.clip_retriever import top_k_by_vector

        md = [{"id": "c1", "embedding": [1.0]}]
        with pytest.raises(ValueError, match="positive"):
            top_k_by_vector([1.0], md, k=0)

    def test_empty_metadata_raises(self):
        from promo.core.assign.clip_retriever import top_k_by_vector

        with pytest.raises(ValueError, match="empty"):
            top_k_by_vector([1.0], [], k=1)

    def test_k_greater_than_pool_clamps_with_debug_log(self, caplog):
        from promo.core.assign.clip_retriever import top_k_by_vector

        md = [
            {"id": "c1", "embedding": [1.0, 0.0]},
            {"id": "c2", "embedding": [0.0, 1.0]},
        ]
        with caplog.at_level("DEBUG", logger="promo.core.assign.clip_retriever"):
            out = top_k_by_vector([1.0, 0.0], md, k=100)
        assert len(out) == 2, "clamped result should have full pool size"
        assert set(out) == {"c1", "c2"}
        clamp_logs = [
            r for r in caplog.records if "clamped" in r.message.lower()
        ]
        assert len(clamp_logs) == 1, (
            f"expected exactly 1 DEBUG clamp log, got {len(clamp_logs)}"
        )

    def test_nan_query_raises(self):
        from promo.core.assign.clip_retriever import top_k_by_vector

        md = [{"id": "c1", "embedding": [1.0, 0.0]}]
        with pytest.raises(ValueError, match="NaN"):
            top_k_by_vector([float("nan"), 0.0], md, k=1)

    def test_inf_query_raises(self):
        from promo.core.assign.clip_retriever import top_k_by_vector

        md = [{"id": "c1", "embedding": [1.0, 0.0]}]
        with pytest.raises(ValueError, match="Inf"):
            top_k_by_vector([float("inf"), 0.0], md, k=1)

class TestSprint12aUnionOfTopK:
    """AC7 — union dedupe, order, size; empty-queries raise."""

    def test_union_dedupe_size_and_order(self):
        """3 narration queries whose top-2s overlap in 1 clip → union_size=5."""
        from promo.core.assign.clip_retriever import union_of_top_k

        md = [
            {"id": "c1", "embedding": [1.0, 0.0, 0.0, 0.0]},
            {"id": "c2", "embedding": [0.0, 1.0, 0.0, 0.0]},
            {"id": "c3", "embedding": [0.0, 0.0, 1.0, 0.0]},
            {"id": "c4", "embedding": [0.0, 0.0, 0.0, 1.0]},
            {"id": "c5", "embedding": [0.9, 0.1, 0.0, 0.0]},
            {"id": "c6", "embedding": [0.0, 0.9, 0.1, 0.0]},
        ]

        # Three queries each pull top-2:
        # q1 = +x     → [c1, c5]
        # q2 = +y     → [c2, c6]
        # q3 = +x+y   → [c1, c2]  (overlap with both)
        # Union preserving first occurrence: [c1, c5, c2, c6]  (c1 from q3 already seen)
        def stub(queries):
            mapping = {
                "qx": [1.0, 0.0, 0.0, 0.0],
                "qy": [0.0, 1.0, 0.0, 0.0],
                "qxy": [0.7, 0.7, 0.0, 0.0],
            }
            return [mapping[q] for q in queries]

        union, size = union_of_top_k(
            ["qx", "qy", "qxy"], md, k=2, embed_query_fn=stub,
        )
        assert size == len(union)
        assert len(union) == len(set(union)), "union has duplicates"
        # First-occurrence order: q1 hits c1+c5, q2 hits c2+c6, q3's hits
        # already in union.
        assert union == ["c1", "c5", "c2", "c6"]

    def test_union_empty_queries_raises(self):
        """Amendment — symmetric with empty-pool raise."""
        from promo.core.assign.clip_retriever import union_of_top_k

        md = [{"id": "c1", "embedding": [1.0]}]
        with pytest.raises(ValueError, match="narration_queries"):
            union_of_top_k([], md, k=1, embed_query_fn=lambda qs: [])

    def test_union_stateless_embed_fn_called_every_time(self):
        from promo.core.assign.clip_retriever import union_of_top_k

        md = [
            {"id": "c1", "embedding": [1.0, 0.0]},
            {"id": "c2", "embedding": [0.0, 1.0]},
        ]
        calls = {"n": 0}

        def stub(queries):
            calls["n"] += 1
            return [[1.0, 0.0] for _ in queries]

        union_of_top_k(["q"], md, k=1, embed_query_fn=stub)
        union_of_top_k(["q"], md, k=1, embed_query_fn=stub)
        assert calls["n"] == 2
