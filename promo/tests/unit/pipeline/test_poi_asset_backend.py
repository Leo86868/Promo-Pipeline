from types import SimpleNamespace

import pytest


def _hash(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _row(*, clip_id: str, asset_id: str, data: bytes, **overrides):
    poi_id = overrides.pop("poi_id", "poi_123")
    row = {
        "poi_id": poi_id,
        "asset_id": asset_id,
        "clip_id": clip_id,
        "display_name": "Hotel Maya",
        "canonical_key": "hotel maya",
        "source_storage_bucket": "poi-assets",
        "source_storage_path": f"{poi_id}/clips/{asset_id}.mp4",
        "source_content_hash": _hash(data),
        "duration_sec": 5.5,
        "width": 720,
        "height": 1280,
        "fps": 30,
        "container": "mp4",
        "video_codec": "h264",
        "file_size_bytes": len(data),
        "embedding_status": "pending",
        "status": "active",
    }
    row.update(overrides)
    return row


class _FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def eq(self, field, value):
        self.calls.append(("eq", field, value))
        self.rows = [row for row in self.rows if row.get(field) == value]
        return self

    def order(self, field):
        self.calls.append(("order", field))
        self.rows = sorted(self.rows, key=lambda row: row[field])
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        self.rows = self.rows[:count]
        return self

    def execute(self):
        self.calls.append(("execute",))
        return SimpleNamespace(data=self.rows)


class _FakeBucket:
    def __init__(self, downloads):
        self._downloads = downloads

    def get_public_url(self, path):
        # Mirrors storage3.get_public_url: pure string build, no egress/auth.
        self._downloads.append(path)
        return f"https://cdn.test/{path}"


class _FakeStorage:
    def __init__(self):
        self.buckets = []
        self.downloads = []

    def from_(self, bucket):
        self.buckets.append(bucket)
        return _FakeBucket(self.downloads)


class _FakeSupabase:
    def __init__(self, rows, blobs):
        self.query = _FakeQuery(rows)
        self.storage = _FakeStorage()
        self.tables = []

    def table(self, name):
        self.tables.append(name)
        return self.query


class _FakeResponse:
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


def _install_fake_http(monkeypatch, blobs):
    """Route _download_clip_blob's public-CDN GET back to in-memory blobs.

    get_public_url returns ``https://cdn.test/<path>``; this maps that URL back
    to the blob bytes (HTTP 200) or, for an unknown path, a JSON error body with
    HTTP 404 — mirroring how a real public bucket answers a missing object.
    """
    from promo.core import poi_asset_backend

    def fake_get(url, timeout=None):
        path = url.removeprefix("https://cdn.test/")
        if path in blobs:
            return _FakeResponse(200, blobs[path])
        return _FakeResponse(404, b'{"statusCode":"404","error":"not_found"}')

    monkeypatch.setattr(poi_asset_backend.requests, "get", fake_get)


def test_poi_asset_supabase_backend_reads_view_and_downloads_clips(monkeypatch, tmp_path):
    from promo.core.poi_asset_backend import PoiAssetSupabaseBackend

    first = b"clip-one"
    second = b"clip-two"
    rows = [
        _row(clip_id="0002", asset_id="asset_b2", data=second),
        _row(clip_id="0001", asset_id="asset_a1", data=first),
    ]
    blobs = {
        "poi_123/clips/asset_a1.mp4": first,
        "poi_123/clips/asset_b2.mp4": second,
    }
    client = _FakeSupabase(rows, blobs)
    _install_fake_http(monkeypatch, blobs)
    backend = PoiAssetSupabaseBackend(client, canonical_key="hotel maya")

    clip_paths = backend.fetch_clips("Hotel Maya", str(tmp_path))

    assert client.tables == ["poi_asset_valid_clips"]
    assert ("select", "*") in client.query.calls
    assert ("eq", "canonical_key", "hotel maya") in client.query.calls
    assert ("order", "clip_id") in client.query.calls
    assert client.storage.buckets == ["poi-assets", "poi-assets"]
    assert client.storage.downloads == [
        "poi_123/clips/asset_a1.mp4",
        "poi_123/clips/asset_b2.mp4",
    ]
    assert sorted(clip_paths) == ["0001", "0002"]
    assert (tmp_path / "clip_0001_asset_a1.mp4").read_bytes() == first
    assert backend.shared_poi_id() == "poi_123"
    assert backend.shared_canonical_key() == "hotel maya"
    assert [row["asset_id"] for row in backend.shared_assets()] == [
        "asset_a1",
        "asset_b2",
    ]


def test_poi_asset_supabase_backend_rejects_hash_mismatch(monkeypatch, tmp_path):
    from promo.core.poi_asset_backend import PoiAssetBackendError, PoiAssetSupabaseBackend

    data = b"clip-one"
    row = _row(
        clip_id="0001",
        asset_id="asset_a1",
        data=data,
        source_content_hash="sha256:" + "0" * 64,
    )
    blobs = {"poi_123/clips/asset_a1.mp4": data}
    client = _FakeSupabase([row], blobs)
    _install_fake_http(monkeypatch, blobs)
    backend = PoiAssetSupabaseBackend(client, poi_id="poi_123")

    with pytest.raises(PoiAssetBackendError, match="content hash mismatch"):
        backend.fetch_clips("Hotel Maya", str(tmp_path))


def test_poi_asset_supabase_backend_retries_transient_download_error(
    monkeypatch,
    tmp_path,
):
    from promo.core import poi_asset_backend
    from promo.core.poi_asset_backend import PoiAssetSupabaseBackend

    data = b"clip-one"
    row = _row(clip_id="0001", asset_id="asset_a1", data=data)
    client = _FakeSupabase([row], {"poi_123/clips/asset_a1.mp4": data})

    calls = {"n": 0}

    def flaky_get(url, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("temporary timeout")
        return _FakeResponse(200, data)

    monkeypatch.setattr(poi_asset_backend.requests, "get", flaky_get)
    monkeypatch.setattr(poi_asset_backend.time, "sleep", lambda seconds: None)

    backend = PoiAssetSupabaseBackend(client, poi_id="poi_123")

    assert backend.fetch_clips("Hotel Maya", str(tmp_path)) == {
        "0001": str(tmp_path / "clip_0001_asset_a1.mp4"),
    }
    assert calls["n"] == 2


def _clip_row(**overrides):
    row = {
        "source_storage_bucket": "poi-assets",
        "source_storage_path": "poi_123/clips/asset_a1.mp4",
        "asset_id": "asset_a1",
    }
    row.update(overrides)
    return row


def test_download_clip_blob_fetches_via_public_cdn_get(monkeypatch):
    from promo.core.poi_asset_backend import PoiAssetSupabaseBackend

    data = b"video-bytes"
    blobs = {"poi_123/clips/asset_a1.mp4": data}
    client = _FakeSupabase([], blobs)
    _install_fake_http(monkeypatch, blobs)
    backend = PoiAssetSupabaseBackend(client, poi_id="poi_123")

    assert backend._download_clip_blob(_clip_row()) == data
    # Routed through the public-URL path (no authenticated .download()).
    assert client.storage.buckets == ["poi-assets"]
    assert client.storage.downloads == ["poi_123/clips/asset_a1.mp4"]


def test_download_clip_blob_raises_on_non_200(monkeypatch):
    from promo.core import poi_asset_backend
    from promo.core.poi_asset_backend import (
        PoiAssetBackendError,
        PoiAssetSupabaseBackend,
    )

    # Empty blob map -> fake_get returns HTTP 404 with a JSON error body.
    _install_fake_http(monkeypatch, {})
    monkeypatch.setattr(poi_asset_backend.time, "sleep", lambda seconds: None)
    backend = PoiAssetSupabaseBackend(_FakeSupabase([], {}), poi_id="poi_123")

    with pytest.raises(PoiAssetBackendError, match="failed to download") as excinfo:
        backend._download_clip_blob(_clip_row(source_storage_path="poi_123/clips/missing.mp4"))
    # The JSON error body is NEVER returned as clip bytes — it fails loud.
    assert "HTTP 404" in str(excinfo.value.__cause__)


def test_download_clip_blob_raises_on_empty_body(monkeypatch):
    from promo.core import poi_asset_backend
    from promo.core.poi_asset_backend import (
        PoiAssetBackendError,
        PoiAssetSupabaseBackend,
    )

    monkeypatch.setattr(
        poi_asset_backend.requests,
        "get",
        lambda url, timeout=None: _FakeResponse(200, b""),
    )
    monkeypatch.setattr(poi_asset_backend.time, "sleep", lambda seconds: None)
    backend = PoiAssetSupabaseBackend(_FakeSupabase([], {}), poi_id="poi_123")

    with pytest.raises(PoiAssetBackendError, match="failed to download") as excinfo:
        backend._download_clip_blob(_clip_row())
    assert "empty body" in str(excinfo.value.__cause__)


def test_poi_asset_supabase_backend_downloads_only_candidate_assets(monkeypatch, tmp_path):
    from promo.core.poi_asset_backend import PoiAssetSupabaseBackend

    first = b"clip-one"
    second = b"clip-two"
    third = b"clip-three"
    rows = [
        _row(clip_id="0001", asset_id="asset_a1", data=first),
        _row(clip_id="0002", asset_id="asset_b2", data=second),
        _row(clip_id="0003", asset_id="asset_c3", data=third),
    ]
    blobs = {
        "poi_123/clips/asset_a1.mp4": first,
        "poi_123/clips/asset_b2.mp4": second,
        "poi_123/clips/asset_c3.mp4": third,
    }
    client = _FakeSupabase(rows, blobs)
    _install_fake_http(monkeypatch, blobs)
    backend = PoiAssetSupabaseBackend(client, poi_id="poi_123")

    clip_paths = backend.fetch_candidate_clips(
        "Hotel Maya",
        str(tmp_path),
        ["asset_c3", "asset_b2"],
    )

    assert sorted(clip_paths) == ["0002", "0003"]
    assert client.storage.downloads == [
        "poi_123/clips/asset_b2.mp4",
        "poi_123/clips/asset_c3.mp4",
    ]
    assert [row["asset_id"] for row in backend.shared_assets()] == [
        "asset_b2",
        "asset_c3",
    ]
    assert not (tmp_path / "clip_0001_asset_a1.mp4").exists()


def test_poi_asset_supabase_backend_rejects_candidate_outside_source_policy(tmp_path):
    from promo.core.poi_asset_backend import PoiAssetBackendError, PoiAssetSupabaseBackend

    first = b"clip-one"
    second = b"clip-two"
    rows = [
        _row(clip_id="0001", asset_id="asset_a1", data=first),
        _row(
            clip_id="0002",
            asset_id="asset_b2",
            data=second,
            width=1080,
            height=1920,
        ),
    ]
    blobs = {
        "poi_123/clips/asset_a1.mp4": first,
        "poi_123/clips/asset_b2.mp4": second,
    }
    backend = PoiAssetSupabaseBackend(
        _FakeSupabase(rows, blobs),
        poi_id="poi_123",
        source_resolution_policy={
            "mode": "transition_low_res_only",
            "target_width": 720,
            "tolerance_px": 40,
        },
    )

    with pytest.raises(PoiAssetBackendError, match="source_resolution_policy"):
        backend.fetch_candidate_clips("Hotel Maya", str(tmp_path), ["asset_b2"])


def test_poi_asset_supabase_backend_threads_source_policy_to_ready_assets(monkeypatch):
    from promo.core.poi_asset_backend import PoiAssetSupabaseBackend

    captured = {}

    def fake_fetch_ready_assets(client, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "promo.core.assets.retrieval.fetch_ready_assets",
        fake_fetch_ready_assets,
    )
    backend = PoiAssetSupabaseBackend(
        _FakeSupabase([], {}),
        poi_id="poi_123",
        source_resolution_policy={
            "mode": "transition_low_res_only",
            "target_width": 720,
            "tolerance_px": 40,
        },
    )

    assert backend.ready_assets_for_retrieval() == []
    assert captured["poi_id"] == "poi_123"
    assert captured["source_resolution_policy"].target_width == 720


def test_poi_asset_supabase_backend_rejects_mixed_canonical_keys(tmp_path):
    from promo.core.poi_asset_backend import PoiAssetBackendError, PoiAssetSupabaseBackend

    first = b"clip-one"
    second = b"clip-two"
    rows = [
        _row(clip_id="0001", asset_id="asset_a1", data=first),
        _row(
            clip_id="0002",
            asset_id="asset_b2",
            data=second,
            canonical_key="other hotel",
        ),
    ]
    blobs = {
        "poi_123/clips/asset_a1.mp4": first,
        "poi_123/clips/asset_b2.mp4": second,
    }
    backend = PoiAssetSupabaseBackend(_FakeSupabase(rows, blobs), poi_id="poi_123")

    with pytest.raises(PoiAssetBackendError, match="multiple canonical_key"):
        backend.fetch_clips("Hotel Maya", str(tmp_path))


def test_poi_asset_supabase_backend_requires_stable_lookup_key():
    from promo.core.poi_asset_backend import PoiAssetBackendError, PoiAssetSupabaseBackend

    with pytest.raises(PoiAssetBackendError, match="poi_id or canonical_key"):
        PoiAssetSupabaseBackend(_FakeSupabase([], {}))


def test_compile_promo_build_backend_accepts_supabase_lookup(monkeypatch, tmp_path):
    from promo.cli import compile_promo

    parser = compile_promo._build_parser()
    args = parser.parse_args([
        "--poi",
        "Hotel Maya",
        "--supabase-poi-id",
        "poi_123",
        "--output-dir",
        str(tmp_path),
    ])
    captured = {}
    sentinel = object()

    def fake_from_env(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        compile_promo.PoiAssetSupabaseBackend,
        "from_env",
        fake_from_env,
    )

    assert compile_promo._build_backend(args) is sentinel
    assert captured == {
        "poi_id": "poi_123",
        "canonical_key": None,
        "output_dir": str(tmp_path),
        "use_music_library": False,
        "music_id": None,
        "music_min_duration_sec": args.target_duration_sec,
    }


def test_compile_promo_build_backend_passes_supabase_music_flags(monkeypatch, tmp_path):
    from promo.cli import compile_promo

    parser = compile_promo._build_parser()
    args = parser.parse_args([
        "--poi",
        "Hotel Maya",
        "--supabase-poi-id",
        "poi_123",
        "--supabase-music-id",
        "11111111-1111-1111-1111-111111111111",
        "--target-duration-sec",
        "65",
        "--output-dir",
        str(tmp_path),
    ])
    captured = {}
    sentinel = object()

    def fake_from_env(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        compile_promo.PoiAssetSupabaseBackend,
        "from_env",
        fake_from_env,
    )

    assert compile_promo._build_backend(args) is sentinel
    assert captured["use_music_library"] is True
    assert captured["music_id"] == "11111111-1111-1111-1111-111111111111"
    assert captured["music_min_duration_sec"] == 65


def test_compile_promo_build_backend_passes_source_resolution_policy(monkeypatch, tmp_path):
    from promo.cli import compile_promo

    parser = compile_promo._build_parser()
    args = parser.parse_args([
        "--poi",
        "Hotel Maya",
        "--supabase-poi-id",
        "poi_123",
        "--output-dir",
        str(tmp_path),
        "--source-resolution-policy-mode",
        "transition_low_res_only",
        "--source-target-width",
        "720",
        "--source-width-tolerance-px",
        "40",
    ])
    captured = {}
    sentinel = object()

    def fake_from_env(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        compile_promo.PoiAssetSupabaseBackend,
        "from_env",
        fake_from_env,
    )

    assert compile_promo._build_backend(args) is sentinel
    assert captured["source_resolution_policy"]["mode"] == "transition_low_res_only"
    assert captured["source_resolution_policy"]["target_width"] == 720
    assert captured["source_resolution_policy"]["tolerance_px"] == 40


def test_compile_promo_build_backend_rejects_mixed_clip_sources(tmp_path):
    from promo.cli import compile_promo

    parser = compile_promo._build_parser()
    args = parser.parse_args([
        "--poi",
        "Hotel Maya",
        "--local-clips",
        str(tmp_path),
        "--supabase-poi-id",
        "poi_123",
    ])

    with pytest.raises(ValueError, match="choose either"):
        compile_promo._build_backend(args)


def test_full_pipeline_passes_backend_shared_assets_to_manifest(tmp_path):
    from promo.core.pipeline import pipeline

    shared_assets = [{
        "poi_id": "poi_123",
        "asset_id": "asset_a1",
        "clip_id": "0001",
        "source_storage_bucket": "poi-assets",
        "source_storage_path": "poi_123/clips/asset_a1.mp4",
        "source_content_hash": "sha256:" + "a" * 64,
        "duration_sec": 5.5,
    }]
    captured = {}

    class Backend:
        def fetch_clips(self, poi_name, tmp_dir):
            return {"0001": "/tmp/clip_0001.mp4"}

        def fetch_bgm(self, poi_name, tmp_dir):
            return None

        def save_output(self, poi_name, video_path):
            return video_path

        def clips_dir(self):
            return None

        def output_dir(self):
            return str(tmp_path)

        def shared_assets(self):
            return shared_assets

        def shared_poi_id(self):
            return "poi_123"

        def shared_canonical_key(self):
            return "hotel maya"

    def fake_variant_loop(**kwargs):
        kwargs["rendered_outputs"].append({
            "variant_index": 1,
            "variant_status": "rendered",
            "render_output_path": str(tmp_path / "render.mp4"),
            "final_output_path": str(tmp_path / "final.mp4"),
            "target_duration_sec": 30.0,
            "format_mode": "short",
            "voice_key": "kore",
            "bgm_path": str(tmp_path / "bgm.mp3"),
            "file_size_bytes": 10,
            "timeline_entries": [{
                "clip_id": "0001",
                "usage_role": "assigned_phrase",
                "segment": 1,
                "trim_start_sec": 0.0,
                "trim_end_sec": 2.0,
                "display_start_sec": 0.0,
                "display_end_sec": 2.0,
                "source_duration_sec": 5.5,
            }],
        })
        return True, 0, {}

    def fake_build_run_manifest(**kwargs):
        captured.update(kwargs)
        return {
            "manifest_id": "manifest_test",
            "run_id": "pgc_run_test",
            "created_at": "2026-05-27T00:00:00Z",
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        pipeline,
        "_step_prepare_clips",
        lambda **kwargs: (
            {"0001": "/tmp/clip_0001.mp4"},
            [{"id": "0001"}],
            {"0001": 5.5},
        ),
    )
    monkeypatch.setattr(pipeline, "_resolve_voice_keys", lambda voice_key: ["kore"])
    monkeypatch.setattr(
        pipeline,
        "_build_variant_selections",
        lambda **kwargs: (["profile"], ["persona"]),
    )
    monkeypatch.setattr(
        pipeline,
        "_step_generate_script",
        lambda **kwargs: [{"variant_index": 1}],
    )
    monkeypatch.setattr(
        pipeline,
        "_resolve_bgm_paths",
        lambda **kwargs: [str(tmp_path / "bgm.mp3")],
    )
    monkeypatch.setattr(pipeline, "_run_variant_loop", fake_variant_loop)
    monkeypatch.setattr(
        pipeline,
        "_emit_run_sidecars_result",
        lambda **kwargs: SimpleNamespace(
            ok=True,
            paths={"clip_assignments": str(tmp_path / "clip_assignments.json")},
            sidecar_dir=str(tmp_path),
        ),
    )
    monkeypatch.setattr(pipeline, "build_run_manifest", fake_build_run_manifest)
    monkeypatch.setattr(
        pipeline,
        "emit_run_manifest",
        lambda **kwargs: SimpleNamespace(ok=True),
    )
    try:
        ok = pipeline.full_pipeline(
            poi_name="Hotel Maya",
            location="Long Beach",
            output_path=str(tmp_path / "promo.mp4"),
            backend=Backend(),
            skip_analysis=True,
        )
    finally:
        monkeypatch.undo()

    assert ok is True
    assert captured["poi_id"] == "poi_123"
    assert captured["canonical_key"] == "hotel maya"
    assert captured["shared_assets"] == shared_assets


def test_full_pipeline_records_shared_asset_semantic_retrieval(
    monkeypatch,
    tmp_path,
):
    from promo.core.assets.retrieval import ReadyAsset
    from promo.core.pipeline import pipeline

    shared_assets = [
        _row(
            clip_id=f"{index:04d}",
            asset_id=f"asset_{index:04d}",
            data=b"clip",
            category="pool" if index % 2 else "exterior",
            scene_description=f"asset {index} scene",
            main_subject=f"asset {index}",
        )
        for index in range(1, 52)
    ]
    ready_assets = [
        ReadyAsset(
            poi_id="poi_123",
            asset_id=row["asset_id"],
            clip_id=row["clip_id"],
            category=row.get("category"),
            scene_description=row.get("scene_description"),
            shot_size=row.get("shot_size"),
            main_subject=row.get("main_subject"),
            camera_motion=row.get("camera_motion"),
            embedding_text=f"{row['scene_description']} | {row['category']}",
            duration_sec=float(row["duration_sec"]),
            usage_count=0,
            source_storage_bucket=row["source_storage_bucket"],
            source_storage_path=row["source_storage_path"],
            source_content_hash=row["source_content_hash"],
            embedding_vector=(1.0, 0.0, 0.0)
            if int(row["clip_id"]) % 2
            else (0.0, 1.0, 0.0),
        )
        for row in shared_assets
    ]
    captured_sidecar = {}

    class Backend:
        def fetch_clips(self, poi_name, tmp_dir):
            return {
                row["clip_id"]: str(tmp_path / f"clip_{row['clip_id']}.mp4")
                for row in shared_assets
            }

        def fetch_bgm(self, poi_name, tmp_dir):
            return None

        def save_output(self, poi_name, video_path):
            return video_path

        def clips_dir(self):
            return None

        def output_dir(self):
            return str(tmp_path)

        def shared_assets(self):
            return shared_assets

        def shared_poi_id(self):
            return "poi_123"

        def shared_canonical_key(self):
            return "hotel maya"

        def ready_assets_for_retrieval(self):
            return ready_assets

    def fake_variant_loop(**kwargs):
        kwargs["clip_assignments_entries"].append({"variant_index": 1})
        return True, 0, {"retrieval_contract": "soft_hint"}

    def fake_emit_sidecars(**kwargs):
        captured_sidecar.update(kwargs["run_retrieval_provenance"])
        return SimpleNamespace(
            ok=True,
            paths={"clip_assignments": str(tmp_path / "clip_assignments.json")},
            sidecar_dir=str(tmp_path),
        )

    monkeypatch.setattr(
        pipeline,
        "_step_prepare_clips",
        lambda **kwargs: (
            {
                row["clip_id"]: str(tmp_path / f"clip_{row['clip_id']}.mp4")
                for row in shared_assets
            },
            [{"id": row["clip_id"]} for row in shared_assets],
            {row["clip_id"]: float(row["duration_sec"]) for row in shared_assets},
        ),
    )
    monkeypatch.setattr(pipeline, "_resolve_voice_keys", lambda voice_key: ["kore"])
    monkeypatch.setattr(
        pipeline,
        "_build_variant_selections",
        lambda **kwargs: (["profile"], ["persona"]),
    )
    monkeypatch.setattr(
        pipeline,
        "_step_generate_script",
        lambda **kwargs: [{
            "variant_index": 1,
            "segments": [
                {"segment": 1, "text": "Oceanfront pool and coastal arrival."},
                {"segment": 2, "text": "Guest rooms with resort views."},
            ],
        }],
    )
    monkeypatch.setattr(
        "promo.core.assign.clip_embedder.embed_texts",
        lambda queries: [
            [1.0, 0.0, 0.0] if index == 0 else [0.0, 1.0, 0.0]
            for index, _query in enumerate(queries)
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "_resolve_bgm_paths",
        lambda **kwargs: [str(tmp_path / "bgm.mp3")],
    )
    monkeypatch.setattr(pipeline, "_run_variant_loop", fake_variant_loop)
    monkeypatch.setattr(pipeline, "_emit_run_sidecars_result", fake_emit_sidecars)

    ok = pipeline.full_pipeline(
        poi_name="Hotel Maya",
        location="Long Beach",
        output_path=str(tmp_path / "promo.mp4"),
        backend=Backend(),
        skip_analysis=True,
    )

    assert ok is True
    shared_retrieval = captured_sidecar["shared_asset_retrieval"]
    assert shared_retrieval["retrieval_active"] is True
    assert shared_retrieval["retrieval_contract"] == "shared_asset_semantic_candidates_v1"
    assert shared_retrieval["eligible_asset_pool_size"] == 51
    assert shared_retrieval["query_count"] == 2
    assert shared_retrieval["candidate_count"] > 0
    assert shared_retrieval["candidate_asset_ids"][0].startswith("asset_")
    assert shared_retrieval["candidates"][0]["scene_description"]


def test_full_pipeline_candidate_only_downloads_retrieved_pool(
    monkeypatch,
    tmp_path,
):
    from promo.core.assets.retrieval import ReadyAsset
    from promo.core.pipeline import pipeline

    shared_assets = [
        _row(
            clip_id=f"{index:04d}",
            asset_id=f"asset_{index:04d}",
            data=b"clip",
            category="pool" if index % 2 else "exterior",
            scene_description=f"asset {index} scene",
            main_subject=f"asset {index}",
        )
        for index in range(1, 52)
    ]
    ready_assets = [
        ReadyAsset(
            poi_id="poi_123",
            asset_id=row["asset_id"],
            clip_id=row["clip_id"],
            category=row.get("category"),
            scene_description=row.get("scene_description"),
            shot_size=row.get("shot_size"),
            main_subject=row.get("main_subject"),
            camera_motion=row.get("camera_motion"),
            embedding_text=f"{row['scene_description']} | {row['category']}",
            duration_sec=float(row["duration_sec"]),
            usage_count=0,
            source_storage_bucket=row["source_storage_bucket"],
            source_storage_path=row["source_storage_path"],
            source_content_hash=row["source_content_hash"],
            embedding_vector=(1.0, 0.0, 0.0)
            if int(row["clip_id"]) % 2
            else (0.0, 1.0, 0.0),
        )
        for row in shared_assets
    ]
    captured_sidecar = {}
    captured_script_kwargs = {}
    downloaded_asset_ids = []

    class Backend:
        def fetch_clips(self, poi_name, tmp_dir):
            raise AssertionError("candidate-only path must not download full pool")

        def fetch_candidate_clips(self, poi_name, tmp_dir, asset_ids):
            downloaded_asset_ids.extend(asset_ids)
            selected = {
                row["asset_id"]: row
                for row in shared_assets
            }
            self._shared_assets = [selected[asset_id] for asset_id in asset_ids]
            return {
                row["clip_id"]: str(tmp_path / f"clip_{row['clip_id']}.mp4")
                for row in self._shared_assets
            }

        def fetch_bgm(self, poi_name, tmp_dir):
            return None

        def save_output(self, poi_name, video_path):
            return video_path

        def clips_dir(self):
            return None

        def output_dir(self):
            return str(tmp_path)

        def shared_assets(self):
            return list(getattr(self, "_shared_assets", []))

        def shared_poi_id(self):
            return "poi_123"

        def shared_canonical_key(self):
            return "hotel maya"

        def ready_assets_for_retrieval(self):
            return ready_assets

    def fake_variant_loop(**kwargs):
        assert len(kwargs["clip_paths"]) == 30
        assert len(kwargs["clips_metadata"]) == 30
        kwargs["clip_assignments_entries"].append({"variant_index": 1})
        return True, 0, {"retrieval_contract": "soft_hint"}

    def fake_emit_sidecars(**kwargs):
        captured_sidecar.update(kwargs["run_retrieval_provenance"])
        return SimpleNamespace(
            ok=True,
            paths={"clip_assignments": str(tmp_path / "clip_assignments.json")},
            sidecar_dir=str(tmp_path),
        )

    def fake_generate_script(**kwargs):
        captured_script_kwargs.update(kwargs)
        return [{
            "variant_index": 1,
            "segments": [
                {"segment": 1, "text": "Oceanfront pool and coastal arrival."},
                {"segment": 2, "text": "Guest rooms with resort views."},
            ],
        }]

    monkeypatch.setattr(
        pipeline,
        "_step_prepare_clips",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("candidate-only path must not prepare full pool")
        ),
    )
    monkeypatch.setattr(pipeline, "_resolve_voice_keys", lambda voice_key: ["kore"])
    monkeypatch.setattr(
        pipeline,
        "_build_variant_selections",
        lambda **kwargs: (["profile"], ["persona"]),
    )
    monkeypatch.setattr(pipeline, "_step_generate_script", fake_generate_script)
    monkeypatch.setattr(
        "promo.core.assign.clip_embedder.embed_texts",
        lambda queries: [
            [1.0, 0.0, 0.0] if index == 0 else [0.0, 1.0, 0.0]
            for index, _query in enumerate(queries)
        ],
    )
    monkeypatch.setattr(
        pipeline,
        "_resolve_bgm_paths",
        lambda **kwargs: [str(tmp_path / "bgm.mp3")],
    )
    monkeypatch.setattr(pipeline, "_run_variant_loop", fake_variant_loop)
    monkeypatch.setattr(pipeline, "_emit_run_sidecars_result", fake_emit_sidecars)

    ok = pipeline.full_pipeline(
        poi_name="Hotel Maya",
        location="Long Beach",
        output_path=str(tmp_path / "promo.mp4"),
        backend=Backend(),
        skip_analysis=True,
    )

    assert ok is True
    assert captured_script_kwargs["asset_visual_brief"]["eligible_asset_count"] == 51
    assert len(downloaded_asset_ids) == 30
    assert len(set(downloaded_asset_ids)) == 30
    assert captured_sidecar["retrieval_active"] is True
    assert captured_sidecar["retrieval_contract"] == (
        "shared_asset_semantic_candidates_v1"
    )
    assert captured_sidecar["embedded_pool_size"] == 51
    assert captured_sidecar["reduced_pool_size"] == 30
    shared_retrieval = captured_sidecar["shared_asset_retrieval"]
    assert shared_retrieval["retrieval_active"] is True
    assert shared_retrieval["candidate_count"] < shared_retrieval["download_pool_count"]
    assert shared_retrieval["download_pool_count"] == 30
    assert shared_retrieval["download_asset_ids"] == downloaded_asset_ids
