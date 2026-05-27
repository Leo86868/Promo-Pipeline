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
    def __init__(self, blobs, downloads):
        self._blobs = blobs
        self._downloads = downloads

    def download(self, path):
        self._downloads.append(path)
        return self._blobs[path]


class _FakeStorage:
    def __init__(self, blobs):
        self._blobs = blobs
        self.buckets = []
        self.downloads = []

    def from_(self, bucket):
        self.buckets.append(bucket)
        return _FakeBucket(self._blobs, self.downloads)


class _FakeSupabase:
    def __init__(self, rows, blobs):
        self.query = _FakeQuery(rows)
        self.storage = _FakeStorage(blobs)
        self.tables = []

    def table(self, name):
        self.tables.append(name)
        return self.query


def test_poi_asset_supabase_backend_reads_view_and_downloads_clips(tmp_path):
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


def test_poi_asset_supabase_backend_rejects_hash_mismatch(tmp_path):
    from promo.core.poi_asset_backend import PoiAssetBackendError, PoiAssetSupabaseBackend

    data = b"clip-one"
    row = _row(
        clip_id="0001",
        asset_id="asset_a1",
        data=data,
        source_content_hash="sha256:" + "0" * 64,
    )
    client = _FakeSupabase([row], {"poi_123/clips/asset_a1.mp4": data})
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

    class FlakyBucket:
        def __init__(self):
            self.calls = 0

        def download(self, path):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary timeout")
            return data

    class FlakyStorage:
        def __init__(self):
            self.bucket = FlakyBucket()

        def from_(self, bucket):
            return self.bucket

    client = _FakeSupabase([row], {"poi_123/clips/asset_a1.mp4": data})
    client.storage = FlakyStorage()
    monkeypatch.setattr(poi_asset_backend.time, "sleep", lambda seconds: None)

    backend = PoiAssetSupabaseBackend(client, poi_id="poi_123")

    assert backend.fetch_clips("Hotel Maya", str(tmp_path)) == {
        "0001": str(tmp_path / "clip_0001_asset_a1.mp4"),
    }
    assert client.storage.bucket.calls == 2


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
