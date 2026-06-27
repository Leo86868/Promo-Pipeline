"""DB-first assignment wiring (flag-gated, default OFF).

Pins the re-architecture: (1) the pipeline assigns over the WHOLE library and
does NO run-level download/filter-to-30 when armed; (2) the variant loop
materializes ``assigned ∪ reserve`` per variant and folds the union back into
the run-level accumulators so the manifest / usage chain carries every
downloaded asset_id (codex's biggest catch); (3) flag OFF is byte-identical to
the legacy download-then-filter path.
"""

from types import SimpleNamespace

import pytest

from promo.core.assets.retrieval import ReadyAsset


def _hash(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _row(*, clip_id: str, asset_id: str, data: bytes = b"clip", **overrides):
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


def _ready(row):
    return ReadyAsset(
        poi_id="poi_123",
        asset_id=row["asset_id"],
        clip_id=row["clip_id"],
        category=row.get("category"),
        scene_description=row.get("scene_description"),
        shot_size=row.get("shot_size"),
        main_subject=row.get("main_subject"),
        camera_motion=row.get("camera_motion"),
        embedding_text=f"{row.get('scene_description')} | {row.get('category')}",
        duration_sec=float(row["duration_sec"]),
        usage_count=0,
        source_storage_bucket=row["source_storage_bucket"],
        source_storage_path=row["source_storage_path"],
        source_content_hash=row["source_content_hash"],
        embedding_vector=(1.0, 0.0, 0.0) if int(row["clip_id"]) % 2 else (0.0, 1.0, 0.0),
    )


def _candidate_backend(shared_assets, ready_assets, tmp_path, downloaded_asset_ids):
    class Backend:
        def fetch_clips(self, poi_name, tmp_dir):
            raise AssertionError("candidate-only path must not download full pool")

        def fetch_candidate_clips(self, poi_name, tmp_dir, asset_ids):
            downloaded_asset_ids.append(list(asset_ids))
            by_id = {row["asset_id"]: row for row in shared_assets}
            self._shared_assets = [by_id[a] for a in asset_ids]
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

    return Backend()


def _patch_common(monkeypatch, pipeline, tmp_path):
    monkeypatch.setattr(
        pipeline, "_step_prepare_clips",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not prepare full pool")),
    )
    monkeypatch.setattr(pipeline, "_resolve_voice_keys", lambda voice_key: ["kore"])
    monkeypatch.setattr(
        pipeline, "_build_variant_selections", lambda **k: (["profile"], ["persona"]),
    )
    monkeypatch.setattr(
        pipeline, "_step_generate_script",
        lambda **k: [{
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
            [1.0, 0.0, 0.0] if i == 0 else [0.0, 1.0, 0.0]
            for i, _ in enumerate(queries)
        ],
    )
    monkeypatch.setattr(
        pipeline, "_resolve_bgm_paths", lambda **k: [str(tmp_path / "bgm.mp3")],
    )


def test_db_first_pipeline_assigns_whole_library_no_run_level_download(
    monkeypatch, tmp_path,
):
    """DB-first armed: the variant loop receives the WHOLE 51-clip library WITH
    inline embeddings (NOT a filtered-to-30 pool — the download ceiling is gone),
    clip_paths empty + db_first True, and NO run-level fetch_candidate_clips
    fires. The manifest's asset_id set == the per-variant materialized
    assigned ∪ reserve union (codex's biggest catch), and a clip ranked >35
    (clip 0040) flows all the way into the manifest's usage chain."""
    from promo.core.pipeline import pipeline

    monkeypatch.setenv("PROMO_DB_FIRST_ASSIGNMENT", "1")
    shared_assets = [
        _row(clip_id=f"{i:04d}", asset_id=f"asset_{i:04d}",
             category="pool" if i % 2 else "exterior",
             scene_description=f"asset {i} scene", main_subject=f"asset {i}")
        for i in range(1, 52)
    ]
    ready_assets = [_ready(r) for r in shared_assets]
    downloaded_asset_ids: list = []
    backend = _candidate_backend(shared_assets, ready_assets, tmp_path, downloaded_asset_ids)

    seen = {}
    assigned_clip_ids = ["0040", "0007"]   # 0040 = 40th clip → proves no 30-cap
    reserve_clip_ids = ["0002", "0004"]

    def fake_variant_loop(**kwargs):
        # DB-first contract: whole library, inline embeddings, empty clip_paths.
        seen["db_first"] = kwargs["db_first"]
        seen["clip_paths_in"] = dict(kwargs["clip_paths"])
        seen["metadata_len"] = len(kwargs["clips_metadata"])
        seen["all_inline_embedding"] = all(
            "embedding" in m for m in kwargs["clips_metadata"]
        )
        seen["all_usage_count"] = all(
            "usage_count" in m for m in kwargs["clips_metadata"]
        )
        seen["no_run_level_download"] = not downloaded_asset_ids
        # Simulate Step 4.6 materialization the real loop performs.
        download_clip_ids = assigned_clip_ids + reserve_clip_ids
        asset_ids = sorted(f"asset_{c}" for c in download_clip_ids)
        paths = kwargs["backend"].fetch_candidate_clips("Hotel Maya", str(tmp_path), asset_ids)
        kwargs["clip_paths"].update(paths)
        kwargs["materialized_shared_assets"].extend(kwargs["backend"].shared_assets())
        kwargs["rendered_outputs"].append({
            "variant_index": 1, "variant_status": "rendered",
            "render_output_path": str(tmp_path / "v1.mp4"),
            "final_output_path": str(tmp_path / "v1.mp4"),
            "target_duration_sec": 30.0, "format_mode": "short",
            "voice_key": "kore", "bgm_path": str(tmp_path / "bgm.mp3"),
            "file_size_bytes": 10,
            "timeline_entries": [
                {"clip_id": cid, "usage_role": "assigned_phrase", "segment": i + 1,
                 "trim_start_sec": 0.0, "trim_end_sec": 2.0,
                 "display_start_sec": 0.0, "display_end_sec": 2.0,
                 "source_duration_sec": 5.5}
                for i, cid in enumerate(assigned_clip_ids)
            ],
        })
        kwargs["clip_assignments_entries"].append({"variant_index": 1})
        return True, 0, {"retrieval_contract": "soft_hint"}

    captured_sidecar = {}

    def fake_emit_sidecars(**kwargs):
        captured_sidecar.update(kwargs["run_retrieval_provenance"])
        return SimpleNamespace(
            ok=True, paths={"clip_assignments": str(tmp_path / "ca.json")},
            sidecar_dir=str(tmp_path),
        )

    captured_manifest = {}

    def fake_emit_manifest(**kwargs):
        captured_manifest.update(kwargs["manifest"])
        return SimpleNamespace(ok=True)

    _patch_common(monkeypatch, pipeline, tmp_path)
    monkeypatch.setattr(pipeline, "_run_variant_loop", fake_variant_loop)
    monkeypatch.setattr(pipeline, "_emit_run_sidecars_result", fake_emit_sidecars)
    monkeypatch.setattr(pipeline, "emit_run_manifest", fake_emit_manifest)

    ok = pipeline.full_pipeline(
        poi_name="Hotel Maya", location="Long Beach",
        output_path=str(tmp_path / "promo.mp4"),
        backend=backend, skip_analysis=True,
    )

    assert ok is True
    # (1) whole-library assignment, no filter-to-30, inline embeddings.
    assert seen["db_first"] is True
    assert seen["clip_paths_in"] == {}
    assert seen["metadata_len"] == 51            # NOT 30 — the download cap is gone
    assert seen["all_inline_embedding"] is True
    assert seen["all_usage_count"] is True
    assert seen["no_run_level_download"] is True  # ② retired: no run-level fetch
    # (2) only assigned ∪ reserve was downloaded (per variant), nothing else.
    assert len(downloaded_asset_ids) == 1         # one variant, one materialize call
    assert sorted(downloaded_asset_ids[0]) == [
        "asset_0002", "asset_0004", "asset_0007", "asset_0040",
    ]
    # (3) manifest asset_id set == downloaded assigned ∪ reserve (codex #5).
    snapshot_asset_ids = {
        row["asset_id"] for row in captured_manifest["asset_snapshot"]
    }
    assert snapshot_asset_ids == {
        "asset_0002", "asset_0004", "asset_0007", "asset_0040",
    }
    # The rank>35 clip 0040 carries through the usage chain with its asset_id.
    timeline_asset_ids = {
        e["asset_id"] for e in captured_manifest["timeline_entries"]
    }
    assert "asset_0040" in timeline_asset_ids
    assert all(e["asset_id"] for e in captured_manifest["timeline_entries"])
    # (4) sidecar observability: under DB-first a field named "download" MUST
    # equal what was actually downloaded (not the legacy relevance pool).
    sar = captured_sidecar["shared_asset_retrieval"]
    materialized = ["asset_0002", "asset_0004", "asset_0007", "asset_0040"]
    assert sar["download_asset_ids"] == materialized
    assert sar["materialized_asset_ids"] == materialized
    assert sar["download_pool_count"] == 4
    # the legacy relevance pool is preserved under a clearly-named field …
    assert "semantic_candidate_asset_ids" in sar
    assert set(sar["download_asset_ids"]).issubset(set(materialized))  # ⊆ assigned∪reserve
    # … and reduced_pool_size reflects the WHOLE-library packer pool (no 30/35).
    assert captured_sidecar["reduced_pool_size"] == 51


def test_db_first_off_is_byte_identical_filters_to_30(monkeypatch, tmp_path):
    """Parity: with the flag OFF the candidate-only path still downloads + filters
    to 30 at the run level and passes db_first False to the variant loop."""
    from promo.core.pipeline import pipeline

    monkeypatch.delenv("PROMO_DB_FIRST_ASSIGNMENT", raising=False)
    shared_assets = [
        _row(clip_id=f"{i:04d}", asset_id=f"asset_{i:04d}",
             category="pool" if i % 2 else "exterior",
             scene_description=f"asset {i} scene", main_subject=f"asset {i}")
        for i in range(1, 52)
    ]
    ready_assets = [_ready(r) for r in shared_assets]
    downloaded_asset_ids: list = []
    backend = _candidate_backend(shared_assets, ready_assets, tmp_path, downloaded_asset_ids)

    seen = {}

    def fake_variant_loop(**kwargs):
        seen["db_first"] = kwargs["db_first"]
        seen["metadata_len"] = len(kwargs["clips_metadata"])
        seen["clip_paths_len"] = len(kwargs["clip_paths"])
        kwargs["clip_assignments_entries"].append({"variant_index": 1})
        return True, 0, {"retrieval_contract": "soft_hint"}

    _patch_common(monkeypatch, pipeline, tmp_path)
    monkeypatch.setattr(pipeline, "_run_variant_loop", fake_variant_loop)
    monkeypatch.setattr(
        pipeline, "_emit_run_sidecars_result",
        lambda **k: SimpleNamespace(ok=True, paths={}, sidecar_dir=str(tmp_path)),
    )

    ok = pipeline.full_pipeline(
        poi_name="Hotel Maya", location="Long Beach",
        output_path=str(tmp_path / "promo.mp4"),
        backend=backend, skip_analysis=True,
    )

    assert ok is True
    assert seen["db_first"] is False
    assert seen["metadata_len"] == 30          # legacy filter-to-30 ceiling
    assert seen["clip_paths_len"] == 30        # run-level download happened
    assert len(downloaded_asset_ids[0]) == 30  # ② path fetched the 30-pool


# ---------------------------------------------------------------------------
#  variant_loop-level materialization (Step 4.6)
# ---------------------------------------------------------------------------

def _variant_loop_kwargs(*, clips_metadata, scripts, backend, tmp_path, **over):
    base = dict(
        scripts=scripts,
        clip_paths={},
        clips_metadata=clips_metadata,
        clip_durations={m["id"]: m["source_duration_sec"] for m in clips_metadata},
        resolved_voice_keys=["kore"],
        resolved_bgm_paths=[str(tmp_path / "bgm.mp3")],
        variant_profiles=["profile"],
        variant_personas=["persona"],
        output_path=str(tmp_path / "out.mp4"),
        backend=backend,
        poi_name="Hotel Maya",
        location="Long Beach",
        hotel_description="",
        notable_details="",
        tmp_dir=str(tmp_path),
        tts_speed=0.95,
        target_duration_sec=30.0,
        script_candidates=1,
        embedding_cache_dir=None,
        tts_metrics=[],
        match_quality_entries=[],
        clip_assignments_entries=[],
        rendered_outputs=[],
        asset_visual_brief=None,
        db_first=True,
        materialized_shared_assets=[],
    )
    base.update(over)
    return base


def _stub_variant_inner_steps(monkeypatch, vl, *, assignments, provenance, captured):
    monkeypatch.setattr(
        vl, "_step_tts_narration",
        lambda *a, **k: {"audio_path": "n.wav", "word_timestamps": [], "duration": 10.0},
    )
    monkeypatch.setattr(
        vl, "_step_assign_clips",
        lambda script, narration, *a, **k: (script, narration, assignments, provenance),
    )
    monkeypatch.setattr(vl, "_build_variant_tts_metrics", lambda *a, **k: {})

    def fake_build_props(**kwargs):
        captured["build_props_clip_paths"] = dict(kwargs["clip_paths"])
        return {"clips": [1], "captions": {"wordTimestamps": []}, "segments": [1]}

    monkeypatch.setattr(vl, "build_props_from_script", fake_build_props)
    monkeypatch.setattr(vl, "validate_props", lambda *a, **k: [])

    def fake_stage(**kwargs):
        captured["stage_media_clip_paths"] = list(kwargs["clip_paths"])

    monkeypatch.setattr(vl, "stage_media", fake_stage)

    def fake_render(props, output_path):
        with open(output_path, "wb") as fh:
            fh.write(b"x")  # the loop reads os.path.getsize after a render
        return True

    monkeypatch.setattr(vl, "render_promo", fake_render)
    monkeypatch.setattr(vl, "build_match_quality_entries", lambda **k: [])


def _recording_db_first_backend():
    class B:
        def __init__(self):
            self.fetch_calls = []
            self._shared = []

        def fetch_candidate_clips(self, poi_name, tmp_dir, asset_ids):
            self.fetch_calls.append(list(asset_ids))
            self._shared = [
                {"clip_id": a.removeprefix("asset_"), "asset_id": a}
                for a in asset_ids
            ]
            return {a.removeprefix("asset_"): f"/p/{a}.mp4" for a in asset_ids}

        def shared_assets(self):
            return list(self._shared)

        def save_output(self, poi_name, video_path):
            return video_path

    return B()


def test_variant_loop_db_first_materializes_assigned_union(monkeypatch, tmp_path):
    """The variant loop downloads exactly this variant's assigned ∪ reserve
    (incl. clip 0040, ranked >30), uses those per-variant paths for build_props /
    stage_media, and folds the union back into run-level clip_paths +
    materialized_shared_assets (success-gated)."""
    from promo.core.pipeline import variant_loop as vl

    clips_metadata = [
        {"id": f"{i:04d}", "asset_id": f"asset_{i:04d}", "usage_count": 0,
         "source_duration_sec": 5.0, "embedding": [1.0, 0.0, 0.0]}
        for i in range(1, 41)
    ]
    scripts = [{
        "variant_index": 1,
        "segments": [{"segment": 1, "text": "a"}, {"segment": 2, "text": "b"}],
        "effective_wpm": 150, "target_duration_sec": 30.0,
    }]
    assignments = [
        {"segment": 1, "clip_id": "0040", "start_word_idx": 0, "end_word_idx": 2},
        {"segment": 2, "clip_id": "0007", "start_word_idx": 3, "end_word_idx": 5},
    ]
    provenance = {"assigner": "packer", "packer": {"reserve_clip_ids": ["0002", "0004"]}}
    captured: dict = {}
    _stub_variant_inner_steps(
        monkeypatch, vl, assignments=assignments, provenance=provenance, captured=captured,
    )

    backend = _recording_db_first_backend()
    clip_paths: dict = {}
    materialized: list = []
    kwargs = _variant_loop_kwargs(
        clips_metadata=clips_metadata, scripts=scripts, backend=backend,
        tmp_path=tmp_path, clip_paths=clip_paths, materialized_shared_assets=materialized,
    )

    all_ok, hard_fails, _ = vl._run_variant_loop(**kwargs)

    assert all_ok is True and hard_fails == 0
    # Downloaded exactly assigned ∪ reserve — not a padded top-30.
    assert len(backend.fetch_calls) == 1
    assert sorted(backend.fetch_calls[0]) == [
        "asset_0002", "asset_0004", "asset_0007", "asset_0040",
    ]
    # build_props / stage_media used the per-variant materialized paths.
    assert set(captured["build_props_clip_paths"]) == {"0040", "0007", "0002", "0004"}
    assert len(captured["stage_media_clip_paths"]) == 4
    # Run-level union folded back for the manifest / usage chain (codex #5).
    assert set(clip_paths) == {"0040", "0007", "0002", "0004"}
    assert {r["asset_id"] for r in materialized} == {
        "asset_0040", "asset_0007", "asset_0002", "asset_0004",
    }


def test_variant_loop_db_first_fails_loud_on_unmapped_assigned(monkeypatch, tmp_path):
    """An assigned clip with NO asset mapping must abort the variant (never
    silently drop an asset_id from the render + ledger)."""
    from promo.core.pipeline import variant_loop as vl

    clips_metadata = [
        {"id": f"{i:04d}", "asset_id": f"asset_{i:04d}", "usage_count": 0,
         "source_duration_sec": 5.0, "embedding": [1.0, 0.0, 0.0]}
        for i in range(1, 6)
    ]
    scripts = [{
        "variant_index": 1,
        "segments": [{"segment": 1, "text": "a"}],
        "effective_wpm": 150, "target_duration_sec": 30.0,
    }]
    # 0099 is NOT in clips_metadata → no asset mapping.
    assignments = [
        {"segment": 1, "clip_id": "0099", "start_word_idx": 0, "end_word_idx": 2},
    ]
    provenance = {"assigner": "packer", "packer": {"reserve_clip_ids": []}}
    captured: dict = {}
    _stub_variant_inner_steps(
        monkeypatch, vl, assignments=assignments, provenance=provenance, captured=captured,
    )

    backend = _recording_db_first_backend()
    clip_paths: dict = {}
    materialized: list = []
    kwargs = _variant_loop_kwargs(
        clips_metadata=clips_metadata, scripts=scripts, backend=backend,
        tmp_path=tmp_path, clip_paths=clip_paths, materialized_shared_assets=materialized,
    )

    all_ok, _, _ = vl._run_variant_loop(**kwargs)

    assert all_ok is False                 # variant aborted, fail-loud
    assert backend.fetch_calls == []       # never attempted a partial download
    assert clip_paths == {}                # nothing folded back
    assert materialized == []
