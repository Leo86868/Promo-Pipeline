"""Tests for the in-progress POI soft-lock (2026-06-18).

Ported from music_remix's receipt soft-lock, ADAPTED for PGC's two-file split:
claims live in selection_summary.json (written early), completion lives in
RUN_RECEIPT.json per-video ``state``. Release is fail-closed: a sibling frees
its POIs only when RUN_RECEIPT exists AND every video state == "complete".
"""

import json
import logging

import pytest

from promo.core.batch_selection import (
    build_selection_payload,
    collect_in_progress_poi_ids,
    summarize_pois,
)


def _make_run_dir(parent, name, *, claimed_pois, video_states=None):
    """Create a sibling run dir with selection_summary.json (claims) and,
    when ``video_states`` is given, a RUN_RECEIPT.json (per-video completion).
    ``video_states=None`` => no receipt written (selected but not yet rendering).
    """
    run_dir = parent / name
    run_dir.mkdir()
    (run_dir / "selection_summary.json").write_text(
        json.dumps({"selected_pois": [{"poi_id": p} for p in claimed_pois]}),
        encoding="utf-8",
    )
    if video_states is not None:
        (run_dir / "RUN_RECEIPT.json").write_text(
            json.dumps({"videos": [{"poi_id": "x", "state": s} for s in video_states]}),
            encoding="utf-8",
        )
    return run_dir


# ── AIGC mirror (a): non-completed sibling keeps POIs, completed releases ──────
def test_completed_sibling_releases_running_sibling_keeps(tmp_path):
    _make_run_dir(tmp_path, "batch_running", claimed_pois=["poi_001"],
                  video_states=["complete", "rendering"])
    _make_run_dir(tmp_path, "batch_done", claimed_pois=["poi_002"],
                  video_states=["complete", "complete"])
    locks = collect_in_progress_poi_ids(tmp_path)
    assert set(locks) == {"poi_001"}          # running held
    assert "poi_002" not in locks             # completed released
    assert locks["poi_001"] == "batch_running"  # maps to claiming batch dir


# ── AIGC mirror (b): missing root → {} no-op ─────────────────────────────────
def test_missing_root_returns_empty(tmp_path):
    assert collect_in_progress_poi_ids(tmp_path / "does_not_exist") == {}


# ── PGC add (e): two-file split — missing RUN_RECEIPT = locked (fail-closed) ──
def test_missing_receipt_keeps_locked(tmp_path):
    # claim exists (selection_summary) but no receipt yet = selected, not yet
    # rendering => still owns its POI.
    _make_run_dir(tmp_path, "batch_just_started", claimed_pois=["poi_777"],
                  video_states=None)
    assert set(collect_in_progress_poi_ids(tmp_path)) == {"poi_777"}


def test_partial_receipt_keeps_locked(tmp_path):
    _make_run_dir(tmp_path, "batch_partial", claimed_pois=["poi_888"],
                  video_states=["complete", "render_failed"])
    assert set(collect_in_progress_poi_ids(tmp_path)) == {"poi_888"}


def test_empty_videos_receipt_keeps_locked(tmp_path):
    _make_run_dir(tmp_path, "batch_empty", claimed_pois=["poi_999"],
                  video_states=[])
    assert set(collect_in_progress_poi_ids(tmp_path)) == {"poi_999"}


# ── self-exclusion: a batch never locks against its own dir ───────────────────
def test_excludes_self_dir(tmp_path):
    own = _make_run_dir(tmp_path, "batch_self", claimed_pois=["poi_self"],
                        video_states=None)
    _make_run_dir(tmp_path, "batch_other", claimed_pois=["poi_other"],
                  video_states=None)
    locks = collect_in_progress_poi_ids(tmp_path, exclude_dir=str(own))
    assert "poi_self" not in locks
    assert set(locks) == {"poi_other"}


# ── PGC add (f): corrupt selection_summary → warn + skip THAT dir only ───────
def test_corrupt_selection_summary_warns_and_skips_only_that_dir(tmp_path, caplog):
    bad = tmp_path / "batch_corrupt"
    bad.mkdir()
    (bad / "selection_summary.json").write_text("{not valid json", encoding="utf-8")
    _make_run_dir(tmp_path, "batch_ok", claimed_pois=["poi_ok"], video_states=None)
    with caplog.at_level(logging.WARNING):
        locks = collect_in_progress_poi_ids(tmp_path)
    assert set(locks) == {"poi_ok"}  # the good sibling still enforced
    assert any("unreadable" in r.message for r in caplog.records)


# ── PGC add (g): corrupt RUN_RECEIPT → fail-closed (stays locked) ────────────
def test_corrupt_receipt_keeps_locked(tmp_path, caplog):
    run_dir = _make_run_dir(tmp_path, "batch_badreceipt", claimed_pois=["poi_x"],
                            video_states=None)
    (run_dir / "RUN_RECEIPT.json").write_text("{broken", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        locks = collect_in_progress_poi_ids(tmp_path)
    assert set(locks) == {"poi_x"}  # unreadable receipt => treated as in-progress


# ── AIGC mirror (c): summarize_pois hard-excludes with reason in_progress_lock ─
def _rows_for_poi(poi_id, *, count, name):
    prefix = poi_id.removeprefix("poi_")
    rows = []
    for i in range(1, count + 1):
        asset_id = f"asset_{prefix}{i:04d}"
        rows.append({
            "poi_id": poi_id,
            "asset_id": asset_id,
            "clip_id": f"{i:04d}",
            "display_name": name,
            "canonical_key": name.lower(),
            "source_storage_bucket": "poi-assets",
            "source_storage_path": f"{poi_id}/clips/{asset_id}.mp4",
            "source_content_hash": "sha256:" + asset_id.removeprefix("asset_").ljust(64, "a")[:64],
            "duration_sec": 5.5,
            "width": 720,
            "height": 1280,
            "fps": 30,
            "container": "mp4",
            "video_codec": "h264",
            "status": "active",
            # The live view always exposes this POI-level column (NULL when the
            # POI is un-described); present it so the schema-drift sentinel in
            # summarize_pois stays quiet.
            "poi_description": "",
        })
    return rows


def test_summarize_pois_hard_excludes_in_progress_with_reason():
    # poi ids use hex-only prefixes (source_content_hash must be sha256:<hex>).
    rows = _rows_for_poi("poi_aaa", count=80, name="Locked") + _rows_for_poi(
        "poi_bbb", count=80, name="Free"
    )
    summary = summarize_pois(
        rows,
        min_active_assets=70,
        in_progress_poi_ids={"poi_aaa"},
    )
    eligible_ids = {p["poi_id"] for p in summary["eligible_pois"]}
    assert eligible_ids == {"poi_bbb"}  # locked never enters eligible
    locked = [p for p in summary["skipped_pois"] if p["poi_id"] == "poi_aaa"]
    assert locked and locked[0]["reason"] == "in_progress_lock"


def test_in_progress_reason_takes_precedence_over_asset_shortage():
    # asset-poor AND locked -> reports in_progress_lock (precedence), like AIGC.
    rows = _rows_for_poi("poi_aaa", count=10, name="Both")  # below the 70 floor
    summary = summarize_pois(rows, min_active_assets=70, in_progress_poi_ids={"poi_aaa"})
    skipped = {p["poi_id"]: p["reason"] for p in summary["skipped_pois"]}
    assert skipped["poi_aaa"] == "in_progress_lock"


# ── AIGC mirror (d) / integration: collected lock feeds selection end-to-end ──
def test_build_selection_payload_excludes_in_progress_from_selection(tmp_path):
    # A sibling batch is mid-run and claims poi_bbb.
    _make_run_dir(tmp_path, "sibling", claimed_pois=["poi_bbb"], video_states=None)
    locks = collect_in_progress_poi_ids(tmp_path)
    assert set(locks) == {"poi_bbb"}

    rows = (
        _rows_for_poi("poi_aaa", count=80, name="A Hotel")
        + _rows_for_poi("poi_bbb", count=80, name="B Hotel")
        + _rows_for_poi("poi_ccc", count=80, name="C Hotel")
    )
    payload = build_selection_payload(
        rows=rows,
        poi_count=2,
        videos_per_poi=3,
        in_progress_poi_ids=set(locks),
        seed=8,
    )
    selected_ids = {p["poi_id"] for p in payload["selected_pois"]}
    assert "poi_bbb" not in selected_ids
    assert selected_ids == {"poi_aaa", "poi_ccc"}
    assert payload["in_progress_locked_poi_count"] == 1


def test_locked_pool_starvation_fails_loud():
    # If the lock excludes enough that eligible < poi_count, selection must
    # raise (fail-loud), not silently under-fill — mirrors AIGC.
    from promo.core.batch_selection import BatchSelectionError

    rows = _rows_for_poi("poi_aaa", count=80, name="A") + _rows_for_poi(
        "poi_bbb", count=80, name="B"
    )
    with pytest.raises(BatchSelectionError, match="not enough eligible"):
        build_selection_payload(
            rows=rows,
            poi_count=2,
            videos_per_poi=3,
            in_progress_poi_ids={"poi_aaa"},  # only poi_bbb left, need 2
            seed=1,
        )


def test_build_selection_payload_default_no_lock_is_noop():
    rows = _rows_for_poi("poi_aaa", count=80, name="A") + _rows_for_poi(
        "poi_bbb", count=80, name="B"
    )
    payload = build_selection_payload(rows=rows, poi_count=2, videos_per_poi=3, seed=1)
    assert payload["in_progress_locked_poi_count"] == 0
    assert {p["poi_id"] for p in payload["selected_pois"]} == {"poi_aaa", "poi_bbb"}
