import pytest


def _row(poi_id, asset_id, clip_id, *, name, canonical_key=None, **overrides):
    row = {
        "poi_id": poi_id,
        "asset_id": asset_id,
        "clip_id": clip_id,
        "display_name": name,
        "canonical_key": canonical_key or name.lower(),
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
    }
    row.update(overrides)
    return row


def _rows_for_poi(poi_id, *, count, name, **overrides):
    prefix = poi_id.removeprefix("poi_")
    return [
        _row(
            poi_id,
            f"asset_{prefix}{index:04d}",
            f"{index:04d}",
            name=name,
            **overrides,
        )
        for index in range(1, count + 1)
    ]


def test_build_selection_payload_applies_threshold_cooldown_and_equal_random():
    from promo.core.batch_selection import build_selection_payload

    rows = (
        _rows_for_poi("poi_aaa", count=70, name="A Hotel")
        + _rows_for_poi("poi_bbb", count=69, name="B Hotel")
        + _rows_for_poi("poi_ccc", count=75, name="C Hotel")
        + _rows_for_poi("poi_ddd", count=80, name="D Hotel")
    )

    payload = build_selection_payload(
        rows=rows,
        poi_count=2,
        videos_per_poi=3,
        cooldown_poi_ids={"poi_ccc"},
        cooldown_days=3,
        seed=8,
    )

    assert payload["status"] == "ok"
    assert payload["request"]["filters"]["required_active_assets"] == 70
    assert {poi["poi_id"] for poi in payload["eligible_pois"]} == {"poi_aaa", "poi_ddd"}
    assert {poi["poi_id"] for poi in payload["selected_pois"]} == {"poi_aaa", "poi_ddd"}
    skipped = {poi["poi_id"]: poi["reason"] for poi in payload["skipped_pois"]}
    assert skipped == {
        "poi_bbb": "insufficient_active_assets",
        "poi_ccc": "cooldown",
    }
    assert [poi["poi_id"] for poi in payload["batch_spec"]["pois"]] == [
        poi["poi_id"] for poi in payload["selected_pois"]
    ]


def test_build_selection_payload_reports_shortage_unless_allowed():
    from promo.core.batch_selection import BatchSelectionError, build_selection_payload

    rows = _rows_for_poi("poi_aaa", count=70, name="A Hotel")

    with pytest.raises(BatchSelectionError, match="not enough eligible POIs"):
        build_selection_payload(
            rows=rows,
            poi_count=2,
            videos_per_poi=3,
        )

    payload = build_selection_payload(
        rows=rows,
        poi_count=2,
        videos_per_poi=3,
        allow_shortage=True,
    )

    assert payload["status"] == "shortage"
    assert payload["shortage_count"] == 1
    assert len(payload["selected_pois"]) == 1


def test_build_selection_payload_requires_available_classification_field():
    from promo.core.batch_selection import BatchSelectionError, build_selection_payload

    rows = _rows_for_poi("poi_aaa", count=70, name="A Hotel")

    with pytest.raises(BatchSelectionError, match="classification field"):
        build_selection_payload(
            rows=rows,
            poi_count=1,
            videos_per_poi=3,
            classification_field="market",
            classification_value="EU",
        )


def test_build_selection_payload_filters_by_classification():
    from promo.core.batch_selection import build_selection_payload

    rows = (
        _rows_for_poi("poi_aaa", count=70, name="A Hotel", market="EU")
        + _rows_for_poi("poi_bbb", count=70, name="B Hotel", market="US")
    )

    payload = build_selection_payload(
        rows=rows,
        poi_count=1,
        videos_per_poi=3,
        classification_field="market",
        classification_value="EU",
    )

    assert [poi["poi_id"] for poi in payload["selected_pois"]] == ["poi_aaa"]
    assert payload["batch_spec"]["pois"][0]["name"] == "A Hotel"


def test_cooldown_cutoff_iso_uses_utc_days():
    from datetime import datetime, timezone

    from promo.core.batch_selection import cooldown_cutoff_iso

    assert cooldown_cutoff_iso(
        3,
        now=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
    ) == "2026-06-05T12:00:00Z"


def test_fetch_valid_clip_rows_paginates_supabase_results():
    from promo.core.batch_selection import fetch_valid_clip_rows

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows
            self.ranges = []
            self.current_range = (0, len(rows) - 1)

        def select(self, _columns):
            return self

        def range(self, start, end):
            self.current_range = (start, end)
            self.ranges.append((start, end))
            return self

        def execute(self):
            start, end = self.current_range
            return self.rows[start:end + 1]

    class FakeClient:
        def __init__(self, rows):
            self.query = FakeQuery(rows)

        def table(self, name):
            assert name == "poi_asset_valid_clips"
            return self.query

    rows = [{"row": index} for index in range(5)]
    client = FakeClient(rows)

    assert fetch_valid_clip_rows(client, page_size=2) == rows
    assert client.query.ranges == [(0, 1), (2, 3), (4, 5)]
