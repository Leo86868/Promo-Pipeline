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
        # The live view always exposes this POI-level column (NULL when the POI
        # is un-described); default "" keeps the schema-drift sentinel quiet.
        # Override per-test to exercise the DESCRIPTION-present path.
        "poi_description": "",
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


def test_build_selection_payload_applies_threshold_and_equal_random():
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
        candidate_ready_asset_ids=None,  # not exercising the readiness gate
        cooldown_poi_ids={"poi_ccc"},
        cooldown_days=3,
        seed=8,
    )

    assert payload["status"] == "ok"
    assert payload["request"]["filters"]["required_active_assets"] == 70
    # Soft cooldown: poi_ccc is cooled but passes the asset floor, so it is
    # eligible (flagged), no longer skipped. Three POIs clear the floor; the
    # fresh pair (aaa, ddd) is preferred over the cooled one.
    assert {poi["poi_id"] for poi in payload["eligible_pois"]} == {
        "poi_aaa",
        "poi_ccc",
        "poi_ddd",
    }
    assert {poi["poi_id"] for poi in payload["selected_pois"]} == {"poi_aaa", "poi_ddd"}
    skipped = {poi["poi_id"]: poi["reason"] for poi in payload["skipped_pois"]}
    assert skipped == {
        "poi_bbb": "insufficient_active_assets",
    }
    cooldown_flags = {poi["poi_id"]: poi["cooldown"] for poi in payload["eligible_pois"]}
    assert cooldown_flags == {
        "poi_aaa": False,
        "poi_ccc": True,
        "poi_ddd": False,
    }
    assert payload["fresh_eligible"] == 2
    assert payload["cooled_eligible"] == 1
    assert payload["cooled_fallback_used"] == 0
    assert [poi["poi_id"] for poi in payload["batch_spec"]["pois"]] == [
        poi["poi_id"] for poi in payload["selected_pois"]
    ]


def test_cooled_poi_passing_asset_floor_is_eligible_flagged_not_skipped():
    from promo.core.batch_selection import summarize_pois

    rows = (
        _rows_for_poi("poi_aaa", count=80, name="A Hotel")
        + _rows_for_poi("poi_ccc", count=80, name="C Hotel")
    )

    summary = summarize_pois(
        rows,
        min_active_assets=70,
        cooldown_poi_ids={"poi_ccc"},
    )

    eligible = {poi["poi_id"]: poi for poi in summary["eligible_pois"]}
    assert set(eligible) == {"poi_aaa", "poi_ccc"}
    assert eligible["poi_ccc"]["cooldown"] is True
    assert eligible["poi_aaa"]["cooldown"] is False
    assert "poi_ccc" not in {poi["poi_id"] for poi in summary["skipped_pois"]}


def test_cooled_poi_failing_asset_floor_keeps_asset_reason_not_cooldown():
    from promo.core.batch_selection import summarize_pois

    rows = (
        _rows_for_poi("poi_aaa", count=80, name="A Hotel")
        + _rows_for_poi("poi_ccc", count=10, name="C Hotel")
    )

    summary = summarize_pois(
        rows,
        min_active_assets=70,
        cooldown_poi_ids={"poi_ccc"},
    )

    assert {poi["poi_id"] for poi in summary["eligible_pois"]} == {"poi_aaa"}
    skipped = {poi["poi_id"]: poi["reason"] for poi in summary["skipped_pois"]}
    # Cooldown does NOT rescue an asset-poor POI, and it does NOT relabel it:
    # the asset reason wins, "cooldown" is never used as a skip reason.
    assert skipped == {"poi_ccc": "insufficient_active_assets"}
    assert all(poi["reason"] != "cooldown" for poi in summary["skipped_pois"])


def test_select_random_pois_prefers_fresh_over_cooled():
    from promo.core.batch_selection import select_random_pois

    eligible = [
        {"poi_id": "poi_f1", "poi_name": "F1", "cooldown": False},
        {"poi_id": "poi_f2", "poi_name": "F2", "cooldown": False},
        {"poi_id": "poi_c1", "poi_name": "C1", "cooldown": True},
        {"poi_id": "poi_c2", "poi_name": "C2", "cooldown": True},
    ]

    selected = select_random_pois(eligible, poi_count=2, seed=8)

    selected_ids = {poi["poi_id"] for poi in selected}
    # Two fresh POIs cover the request, so no cooled POI may be chosen.
    assert selected_ids == {"poi_f1", "poi_f2"}
    assert all(poi["selected_as"] == "fresh" for poi in selected)


def test_select_random_pois_falls_back_to_cooled_with_warning(caplog):
    import logging

    from promo.core.batch_selection import build_selection_payload

    rows = (
        _rows_for_poi("poi_aaa", count=80, name="A Hotel")
        + _rows_for_poi("poi_bbb", count=80, name="B Hotel")
        + _rows_for_poi("poi_ccc", count=80, name="C Hotel")
    )

    with caplog.at_level(logging.WARNING, logger="promo.core.batch_selection"):
        payload = build_selection_payload(
            rows=rows,
            poi_count=2,
            videos_per_poi=3,
            candidate_ready_asset_ids=None,  # not exercising the readiness gate
            cooldown_poi_ids={"poi_bbb", "poi_ccc"},
            cooldown_days=3,
            seed=8,
        )

    selected = {poi["poi_id"]: poi for poi in payload["selected_pois"]}
    # Only poi_aaa is fresh; the second slot must be filled from the cooled pool.
    assert "poi_aaa" in selected
    assert selected["poi_aaa"]["selected_as"] == "fresh"
    fallback = [poi for poi in selected.values() if poi["selected_as"] == "cooled_fallback"]
    assert len(fallback) == 1
    assert fallback[0]["poi_id"] in {"poi_bbb", "poi_ccc"}
    assert payload["fresh_eligible"] == 1
    assert payload["cooled_eligible"] == 2
    assert payload["cooled_fallback_used"] == 1
    assert any(
        "recently-used (cooled) POIs as fallback" in record.getMessage()
        for record in caplog.records
    )


def test_select_random_pois_is_deterministic_with_fixed_seed():
    from promo.core.batch_selection import select_random_pois

    eligible = [
        {"poi_id": "poi_f1", "poi_name": "F1", "cooldown": False},
        {"poi_id": "poi_f2", "poi_name": "F2", "cooldown": False},
        {"poi_id": "poi_f3", "poi_name": "F3", "cooldown": False},
        {"poi_id": "poi_c1", "poi_name": "C1", "cooldown": True},
        {"poi_id": "poi_c2", "poi_name": "C2", "cooldown": True},
    ]

    first = select_random_pois(list(eligible), poi_count=4, seed=42)
    second = select_random_pois(list(eligible), poi_count=4, seed=42)

    assert [poi["poi_id"] for poi in first] == [poi["poi_id"] for poi in second]


def test_select_random_pois_not_enough_only_when_fresh_plus_cooled_short():
    from promo.core.batch_selection import BatchSelectionError, select_random_pois

    eligible = [
        {"poi_id": "poi_f1", "poi_name": "F1", "cooldown": False},
        {"poi_id": "poi_c1", "poi_name": "C1", "cooldown": True},
    ]

    # fresh(1) + cooled(1) == 2 satisfies a request of 2 (no error).
    selected = select_random_pois(list(eligible), poi_count=2, seed=1)
    assert len(selected) == 2

    # Requesting 3 exceeds fresh+cooled and is not allowed -> error.
    with pytest.raises(BatchSelectionError, match="not enough eligible POIs"):
        select_random_pois(list(eligible), poi_count=3, seed=1)


def test_build_selection_payload_requires_candidate_ready_assets_when_provided():
    from promo.core.batch_selection import build_selection_payload

    ready_rows = _rows_for_poi("poi_aaa", count=50, name="Ready Hotel")
    stale_rows = _rows_for_poi("poi_bbb", count=56, name="Stale Hotel")
    rows = ready_rows + stale_rows
    ready_asset_ids = {row["asset_id"] for row in ready_rows}
    ready_asset_ids.update(row["asset_id"] for row in stale_rows[:28])

    payload = build_selection_payload(
        rows=rows,
        poi_count=1,
        videos_per_poi=1,
        candidate_ready_asset_ids=ready_asset_ids,
    )

    assert payload["request"]["filters"]["required_active_assets"] == 50
    assert payload["request"]["filters"]["required_candidate_ready_assets"] == 50
    assert [poi["poi_id"] for poi in payload["eligible_pois"]] == ["poi_aaa"]
    skipped = {poi["poi_id"]: poi for poi in payload["skipped_pois"]}
    assert skipped["poi_bbb"]["reason"] == "insufficient_candidate_ready_assets"
    assert skipped["poi_bbb"]["active_asset_count"] == 56
    assert skipped["poi_bbb"]["candidate_ready_asset_count"] == 28


def test_build_selection_payload_applies_source_width_policy_to_candidate_ready_assets():
    from promo.core.batch_selection import build_selection_payload

    low_res_rows = _rows_for_poi("poi_aaa", count=70, name="Low Res Hotel")
    mixed_rows = [
        _row(
            "poi_bbb",
            f"asset_bbb{index:04d}",
            f"{index:04d}",
            name="Mixed Hotel",
        )
        for index in range(1, 41)
    ] + [
        _row(
            "poi_bbb",
            f"asset_bbb{index:04d}",
            f"{index:04d}",
            name="Mixed Hotel",
            width=1080,
            height=1920,
        )
        for index in range(41, 81)
    ]
    rows = low_res_rows + mixed_rows
    ready_asset_ids = {row["asset_id"] for row in rows}

    payload = build_selection_payload(
        rows=rows,
        poi_count=1,
        videos_per_poi=3,
        candidate_ready_asset_ids=ready_asset_ids,
        source_resolution_policy={
            "mode": "transition_low_res_only",
            "target_width": 720,
            "tolerance_px": 40,
        },
    )

    assert [poi["poi_id"] for poi in payload["eligible_pois"]] == ["poi_aaa"]
    assert payload["request"]["filters"]["required_candidate_ready_assets"] == 70
    assert payload["request"]["filters"]["source_resolution_policy"]["target_width"] == 720
    assert payload["batch_spec"]["source_resolution_policy"]["mode"] == (
        "transition_low_res_only"
    )
    skipped = {poi["poi_id"]: poi for poi in payload["skipped_pois"]}
    assert skipped["poi_bbb"]["reason"] == (
        "insufficient_source_resolution_assets"
    )
    assert skipped["poi_bbb"]["active_asset_count"] == 80
    assert skipped["poi_bbb"]["source_resolution_asset_count"] == 40


def test_build_selection_payload_reports_shortage_unless_allowed():
    from promo.core.batch_selection import BatchSelectionError, build_selection_payload

    rows = _rows_for_poi("poi_aaa", count=70, name="A Hotel")

    with pytest.raises(BatchSelectionError, match="not enough eligible POIs"):
        build_selection_payload(
            rows=rows,
            poi_count=2,
            videos_per_poi=3,
            candidate_ready_asset_ids=None,
        )

    payload = build_selection_payload(
        rows=rows,
        poi_count=2,
        videos_per_poi=3,
        candidate_ready_asset_ids=None,
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
            candidate_ready_asset_ids=None,
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
        candidate_ready_asset_ids=None,
        classification_field="market",
        classification_value="EU",
    )

    assert [poi["poi_id"] for poi in payload["selected_pois"]] == ["poi_aaa"]
    assert payload["batch_spec"]["pois"][0]["name"] == "A Hotel"


def test_build_selection_payload_requires_explicit_readiness_set():
    """2026-06-22 footgun fix: candidate_ready_asset_ids has NO default, so a
    caller that FORGETS it crashes loud (TypeError) instead of silently
    degrading the gate to width-only and stranding POIs at retrieval. Skipping
    the readiness gate now demands a conscious, greppable `=None`."""
    from promo.core.batch_selection import build_selection_payload

    rows = _rows_for_poi("poi_aaa", count=70, name="A Hotel")
    with pytest.raises(TypeError, match="candidate_ready_asset_ids"):
        build_selection_payload(rows=rows, poi_count=1, videos_per_poi=3)


def test_cooldown_cutoff_iso_uses_utc_days():
    from datetime import datetime, timezone

    from promo.core.batch_selection import cooldown_cutoff_iso

    assert cooldown_cutoff_iso(
        3,
        now=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc),
    ) == "2026-06-05T12:00:00Z"


def test_fetch_recent_usage_scopes_to_pgc_run_prefix():
    # Pins the paradigm-scope convention (Leo 2026-06-17): cooldown must count
    # only PGC's own usage (run_id LIKE 'pgc_run_%'), never music_remix's —
    # which shares poi_asset_usage_events but uses other run_id prefixes.
    from datetime import datetime, timezone

    from promo.core.batch_selection import fetch_recent_usage_poi_ids

    all_rows = [
        {"poi_id": "poi_pgc1", "created_at": "2026-06-17T00:00:00Z",
         "run_id": "pgc_run_aaaaaaaaaaaa"},
        {"poi_id": "poi_pgc2", "created_at": "2026-06-17T00:00:00Z",
         "run_id": "pgc_run_bbbbbbbbbbbb"},
        {"poi_id": "poi_mr1", "created_at": "2026-06-17T00:00:00Z",
         "run_id": "music_remix_batch_20260615T_poi_x"},
        {"poi_id": "poi_mr2", "created_at": "2026-06-17T00:00:00Z",
         "run_id": "eu_expl_720drain_live_20260617_poi_y_v3"},
    ]

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows
            self.like_filters = []
            self.current_range = (0, len(rows) - 1)

        def select(self, _columns):
            return self

        def gte(self, _column, _value):
            return self

        def like(self, column, pattern):
            self.like_filters.append((column, pattern))
            self.rows = [
                row
                for row in self.rows
                if str(row.get(column, "")).startswith(pattern.rstrip("%"))
            ]
            self.current_range = (0, len(self.rows) - 1)
            return self

        def order(self, _column):
            return self

        def range(self, start, end):
            self.current_range = (start, end)
            return self

        def execute(self):
            start, end = self.current_range
            return self.rows[start:end + 1]

    class FakeClient:
        def __init__(self, rows):
            self.query = FakeQuery(rows)

        def table(self, name):
            assert name == "poi_asset_usage_events"
            return self.query

    client = FakeClient(all_rows)
    result = fetch_recent_usage_poi_ids(
        client,
        cooldown_days=3,
        now=datetime(2026, 6, 18, 0, 0, 0, tzinfo=timezone.utc),
    )

    # Only PGC's own POIs are cooled; music_remix usage is ignored.
    assert result == {"poi_pgc1", "poi_pgc2"}
    assert client.query.like_filters == [("run_id", "pgc_run_%")]


def test_fetch_valid_clip_rows_paginates_supabase_results():
    from promo.core.batch_selection import fetch_valid_clip_rows

    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows
            self.ranges = []
            self.order_columns = []
            self.current_range = (0, len(rows) - 1)

        def select(self, _columns):
            return self

        def order(self, column):
            self.order_columns.append(column)
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
    # 2026-06-09 fix: pagination must be ordered (stable pages under
    # concurrent writers) — exactly one ORDER BY, applied before paging.
    assert client.query.order_columns == ["asset_id"]


# ── poi_description bridge (pipeline B) ──────────────────────────────────────
def test_poi_description_value_threads_through_summarize_and_spec():
    """A non-empty poi_description is carried verbatim from the raw view row
    into the eligible record and the batch_spec POI dict."""
    from promo.core.batch_selection import build_batch_spec, summarize_pois

    facts = "Cliffside resort, 3 infinity pools, Forbes 5-star spa."
    rows = _rows_for_poi("poi_aaa", count=80, name="A Hotel", poi_description=facts)

    summary = summarize_pois(rows, min_active_assets=70)
    eligible = {poi["poi_id"]: poi for poi in summary["eligible_pois"]}
    assert eligible["poi_aaa"]["poi_description"] == facts

    spec = build_batch_spec(summary["eligible_pois"], videos_per_poi=3)
    assert spec["pois"][0]["poi_description"] == facts


def test_poi_description_null_value_is_tolerated_and_emptied():
    """A NULL poi_description (un-described POI) must NOT crash — it normalizes
    to "" so the DESCRIPTION段 is simply omitted downstream."""
    from promo.core.batch_selection import build_batch_spec, summarize_pois

    rows = _rows_for_poi("poi_aaa", count=80, name="A Hotel", poi_description=None)

    summary = summarize_pois(rows, min_active_assets=70)
    eligible = {poi["poi_id"]: poi for poi in summary["eligible_pois"]}
    assert eligible["poi_aaa"]["poi_description"] == ""

    spec = build_batch_spec(summary["eligible_pois"], videos_per_poi=3)
    assert spec["pois"][0]["poi_description"] == ""


def test_missing_poi_description_column_raises_fail_loud():
    """The COLUMN going missing (schema drift) is a hard stop — refuse to run a
    batch that would silently drop every POI's facts."""
    from promo.core.batch_selection import BatchSelectionError, summarize_pois

    rows = _rows_for_poi("poi_aaa", count=80, name="A Hotel")
    for row in rows:
        del row["poi_description"]  # simulate the view dropping the column

    with pytest.raises(BatchSelectionError, match="poi_description column"):
        summarize_pois(rows, min_active_assets=70)
