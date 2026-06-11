"""翻转二 B5 — PROMO_CLIP_ASSIGNER dispatch + packer assignment path."""

import pytest

from promo.core.assign.usage_windows import UsageWindowError, UsedWindow
from promo.core.pipeline.steps import _assign_clips_packer


def _words(n, word_sec=0.4):
    return [
        {"word": f"w{i}", "start": round(i * word_sec, 3),
         "end": round((i + 1) * word_sec, 3)}
        for i in range(n)
    ]


def _two_segment_fixture():
    """2 segments × 10 words @0.4s → exactly one 4.0s beat per segment."""
    script = {"segments": [
        {"text": " ".join(f"a{i}" for i in range(10)), "pause_weight": 1},
        {"text": " ".join(f"b{i}" for i in range(10)), "pause_weight": 1},
    ]}
    narration = {"word_timestamps": _words(20)}
    metadata = [
        {"id": "0001", "category": "pool", "scene_description": "x"},
        {"id": "0002", "category": "room", "scene_description": "y"},
        {"id": "0003", "category": "spa", "scene_description": "z"},
    ]
    durations = {"0001": 5.0, "0002": 5.0, "0003": 5.0}
    return script, narration, metadata, durations


def _stub_embeddings(monkeypatch, metadata):
    from promo.core.assign import clip_embedder

    monkeypatch.setattr(
        clip_embedder, "load_embeddings_for_poi",
        lambda cache_dir: {"mimo_prompt_sha1": "deadbeef"},
    )
    monkeypatch.setattr(
        clip_embedder, "attach_embeddings_to_metadata",
        lambda clips, sidecar: (metadata, []),
    )


def _rank_all(queries, pool):
    ranking = [(m["id"], 0.9) for m in pool]
    return [list(ranking) for _ in queries]


def test_packer_path_returns_validated_assignments(monkeypatch):
    script, narration, metadata, durations = _two_segment_fixture()
    _stub_embeddings(monkeypatch, metadata)
    fetch_calls = []

    def windows_fetcher(client, asset_ids):
        fetch_calls.append((client, asset_ids))
        return {}

    out_script, out_narration, assignments, prov = _assign_clips_packer(
        script, narration, metadata, durations,
        embedding_cache_dir="/fake/cache",
        shared_assets=[
            {"clip_id": "0001", "asset_id": "asset_x"},
            {"clip_id": "0002", "asset_id": "asset_y"},
        ],
        rank_fn=_rank_all,
        windows_fetcher=windows_fetcher,
        usage_client_factory=lambda: "client-sentinel",
    )

    assert out_script is script and out_narration is narration  # no regen
    assert len(assignments) == 2
    # Enriched by the production validator (display_span/source populated).
    assert all("display_span_sec" in a for a in assignments)
    assert fetch_calls == [("client-sentinel", ["asset_x", "asset_y"])]
    assert prov["assigner"] == "packer"
    assert prov["usage_ledger"] == "loaded"
    assert prov["mimo_prompt_sha1"] == "deadbeef"
    assert prov["packer"]["beat_count"] == 2


def test_packer_ledger_failure_is_fail_closed(monkeypatch):
    script, narration, metadata, durations = _two_segment_fixture()
    _stub_embeddings(monkeypatch, metadata)

    def exploding_fetcher(client, asset_ids):
        raise UsageWindowError("ledger unreachable")

    # 设计契约 ②: the error propagates (variant aborts; --resume recovers).
    with pytest.raises(UsageWindowError):
        _assign_clips_packer(
            script, narration, metadata, durations,
            embedding_cache_dir="/fake/cache",
            shared_assets=[{"clip_id": "0001", "asset_id": "asset_x"}],
            rank_fn=_rank_all,
            windows_fetcher=exploding_fetcher,
            usage_client_factory=lambda: object(),
        )


def test_packer_without_asset_mapping_skips_ledger(monkeypatch):
    script, narration, metadata, durations = _two_segment_fixture()
    _stub_embeddings(monkeypatch, metadata)

    _, _, assignments, prov = _assign_clips_packer(
        script, narration, metadata, durations,
        embedding_cache_dir="/fake/cache",
        shared_assets=None,  # local-clips dev run
        rank_fn=_rank_all,
        windows_fetcher=lambda *a: pytest.fail("ledger must not be queried"),
        usage_client_factory=lambda: pytest.fail("client must not be built"),
    )
    assert len(assignments) == 2
    assert prov["usage_ledger"] == "no_asset_mapping"


def test_packer_requires_some_embedding_source(monkeypatch):
    from promo.core.assign import clip_embedder

    script, narration, metadata, durations = _two_segment_fixture()
    monkeypatch.setattr(
        clip_embedder, "load_embeddings_for_poi", lambda cache_dir: None,
    )
    # No inline vectors, no sidecar, no asset mapping → fail loud.
    with pytest.raises(RuntimeError, match="found no embeddings"):
        _assign_clips_packer(
            script, narration, metadata, durations,
            embedding_cache_dir="/fake/cache",
            shared_assets=None,
        )


class _FakeEmbeddingTable:
    """Supabase chain stub for poi_asset_embeddings."""

    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "poi_asset_embeddings"
        return self

    def select(self, _cols):
        return self

    def in_(self, _col, _vals):
        return self

    def eq(self, _col, _val):
        return self

    def order(self, _col):
        return self

    def execute(self):
        class R:
            data = self._rows

        return R()


def test_packer_platform_embedding_fallback(monkeypatch):
    """Production shape: no sidecar, vectors live in poi_asset_embeddings."""
    from promo.core.assign import clip_embedder

    script, narration, metadata, durations = _two_segment_fixture()
    monkeypatch.setattr(
        clip_embedder, "load_embeddings_for_poi", lambda cache_dir: None,
    )
    rows = [
        {"asset_id": f"asset_{cid}", "embedding_vector": [0.1] * 1536,
         "embedding_model": "text-embedding-3-small", "status": "ready"}
        for cid in ("0001", "0002", "0003")
    ]
    _, _, assignments, prov = _assign_clips_packer(
        script, narration, metadata, durations,
        embedding_cache_dir=None,
        shared_assets=[
            {"clip_id": cid, "asset_id": f"asset_{cid}"}
            for cid in ("0001", "0002", "0003")
        ],
        rank_fn=_rank_all,
        windows_fetcher=lambda client, ids: {},
        usage_client_factory=lambda: _FakeEmbeddingTable(rows),
    )
    assert len(assignments) == 2
    assert prov["embedding_source"] == "platform"
    assert prov["usage_ledger"] == "loaded"


def test_packer_prefers_inline_embeddings(monkeypatch):
    """candidate_only_mode may already carry vectors on metadata —
    highest rung of the ladder, no sidecar/platform lookup."""
    script, narration, metadata, durations = _two_segment_fixture()
    metadata = [{**m, "embedding": [0.2, 0.8]} for m in metadata]

    _, _, assignments, prov = _assign_clips_packer(
        script, narration, metadata, durations,
        embedding_cache_dir=None,
        shared_assets=None,
        rank_fn=_rank_all,
        windows_fetcher=lambda *a: pytest.fail("no mapping → no ledger"),
        usage_client_factory=lambda: pytest.fail("client must not be built"),
    )
    assert len(assignments) == 2
    assert prov["embedding_source"] == "inline"


def test_window_rotation_threads_through_to_trim(monkeypatch):
    """Ledger windows actually move trim_start (the rule this exists for)."""
    script, narration, metadata, durations = _two_segment_fixture()
    durations = dict(durations, **{"0001": 9.0})  # room for a 4s beat after [0,4.5)
    _stub_embeddings(monkeypatch, metadata)

    _, _, assignments, prov = _assign_clips_packer(
        script, narration, metadata, durations,
        embedding_cache_dir="/fake/cache",
        shared_assets=[{"clip_id": "0001", "asset_id": "asset_x"}],
        rank_fn=_rank_all,
        windows_fetcher=lambda client, ids: {"asset_x": [UsedWindow(0.0, 4.5)]},
        usage_client_factory=lambda: object(),
    )
    first = next(a for a in assignments if a["clip_id"] == "0001")
    assert first["trim_start"] == 4.5  # rotated past the shown window
    assert prov["packer"]["window_exhausted_beats"] == []


def test_dispatch_env_routes_to_packer(monkeypatch):
    import promo.core.pipeline.steps as steps

    monkeypatch.setenv("PROMO_CLIP_ASSIGNER", "packer")
    sentinel = ({"s": 1}, {"n": 1}, [], {"assigner": "packer"})
    monkeypatch.setattr(steps, "_assign_clips_packer", lambda *a, **k: sentinel)

    out = steps._step_assign_clips(
        {"segments": [{"text": "a", "pause_weight": 1}]},
        {"word_timestamps": _words(1)},
        [],
        {},
        1,
        poi_name="p", location="l", hotel_description="h",
        notable_details="n", variant_voice_key="v",
        variant_tmp_dir="/tmp/x", tts_speed=1.0,
        target_duration_sec=65.0, effective_wpm=150,
        n_variants_total=1, script_candidates=1,
    )
    assert out is sentinel


