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

def test_rank_per_query_returns_full_ranking_per_beat():
    from promo.core.assign.clip_retriever import rank_per_query

    pool = [
        {"id": "0001", "embedding": [1.0, 0.0]},
        {"id": "0002", "embedding": [0.0, 1.0]},
        {"id": "0003", "embedding": [0.7, 0.7]},
    ]
    # Beat 1 points at clip 0001's direction, beat 2 at clip 0002's.
    fake_embed = lambda queries: [[1.0, 0.1], [0.1, 1.0]]

    rankings = rank_per_query(["pool view", "lobby bar"], pool, embed_query_fn=fake_embed)

    assert len(rankings) == 2
    assert [cid for cid, _ in rankings[0]][:1] == ["0001"]
    assert [cid for cid, _ in rankings[1]][:1] == ["0002"]
    # Full pool present in every ranking, scores descending.
    for ranking in rankings:
        assert sorted(cid for cid, _ in ranking) == ["0001", "0002", "0003"]
        scores = [s for _, s in ranking]
        assert scores == sorted(scores, reverse=True)


def test_rank_per_query_validates_inputs():
    import pytest

    from promo.core.assign.clip_retriever import rank_per_query

    pool = [{"id": "0001", "embedding": [1.0, 0.0]}]
    with pytest.raises(ValueError, match="queries is empty"):
        rank_per_query([], pool, embed_query_fn=lambda q: [])
    with pytest.raises(ValueError, match="clip_metadata is empty"):
        rank_per_query(["x"], [], embed_query_fn=lambda q: [[1.0, 0.0]])
    with pytest.raises(RuntimeError, match="returned 1 vectors"):
        rank_per_query(["x", "y"], pool, embed_query_fn=lambda q: [[1.0, 0.0]])
