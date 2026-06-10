"""翻转二 B3 — usage window ledger reader tests."""

import pytest

from promo.core.assign.usage_windows import (
    UsageWindowError,
    UsedWindow,
    fetch_used_windows,
    free_windows,
    merge_windows,
)


class _FakeQuery:
    def __init__(self, rows, *, fail=False):
        self._rows = rows
        self._fail = fail
        self._range = None
        self.ordered_by = None

    def select(self, _cols):
        return self

    def in_(self, _col, _vals):
        return self

    def order(self, col):
        self.ordered_by = col
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        if self._fail:
            raise ConnectionError("ledger unreachable")
        start, end = self._range

        class R:
            data = self._rows[start:end + 1]

        return R()


class _FakeClient:
    def __init__(self, rows, *, fail=False):
        self.query = _FakeQuery(rows, fail=fail)
        self.tables = []

    def table(self, name):
        self.tables.append(name)
        return self.query


def _row(asset, trim, d0, d1, src=8.0):
    return {
        "asset_id": asset,
        "trim_start_sec": trim,
        "display_start_sec": d0,
        "display_end_sec": d1,
        "source_duration_sec": src,
    }


def test_fetch_maps_display_length_onto_source_clock():
    # 3.0s displayed starting at source trim 2.0 → window [2.0, 5.0).
    client = _FakeClient([_row("asset_a", 2.0, 10.0, 13.0)])
    out = fetch_used_windows(client, ["asset_a"])
    assert out == {"asset_a": [UsedWindow(2.0, 5.0)]}
    assert client.tables == ["poi_asset_usage_events"]
    assert client.query.ordered_by == "event_id"  # 2026-06-09 stable paging


def test_fetch_merges_overlaps_and_paginates():
    rows = [_row("asset_a", 0.0, 0.0, 2.0)] * 3 + [
        _row("asset_a", 1.5, 0.0, 2.0),   # [1.5, 3.5) overlaps [0, 2)
        _row("asset_b", 4.0, 0.0, 1.0),
    ]
    client = _FakeClient(rows)
    out = fetch_used_windows(client, ["asset_a", "asset_b"], page_size=2)
    assert out["asset_a"] == [UsedWindow(0.0, 3.5)]
    assert out["asset_b"] == [UsedWindow(4.0, 5.0)]


def test_fetch_skips_malformed_rows_but_keeps_good_ones():
    rows = [
        _row("asset_a", 0.0, 0.0, 2.0),
        {"asset_id": "asset_a", "trim_start_sec": None,
         "display_start_sec": 0.0, "display_end_sec": 2.0,
         "source_duration_sec": 8.0},
    ]
    out = fetch_used_windows(_FakeClient(rows), ["asset_a"])
    assert out == {"asset_a": [UsedWindow(0.0, 2.0)]}


def test_fetch_failure_raises_usage_window_error():
    # 设计契约 rule ②: fail-closed — production callers fail the video.
    with pytest.raises(UsageWindowError, match="ledger unreachable"):
        fetch_used_windows(_FakeClient([], fail=True), ["asset_a"])


def test_fetch_empty_asset_list_short_circuits():
    client = _FakeClient([_row("asset_a", 0.0, 0.0, 1.0)])
    assert fetch_used_windows(client, []) == {}
    assert client.tables == []  # no query issued


def test_merge_windows_orders_and_joins_touching():
    merged = merge_windows([
        UsedWindow(5.0, 6.0), UsedWindow(0.0, 2.0), UsedWindow(2.0, 3.0),
    ])
    assert merged == [UsedWindow(0.0, 3.0), UsedWindow(5.0, 6.0)]


def test_free_windows_returns_usable_gaps_only():
    used = [UsedWindow(0.0, 3.0), UsedWindow(4.0, 5.0)]
    # Gaps in an 8s source: [3,4) too short for 2s, [5,8) qualifies.
    assert free_windows(8.0, used, min_len_sec=2.0) == [UsedWindow(5.0, 8.0)]
    # Fresh clip: the whole source is one gap.
    assert free_windows(8.0, [], min_len_sec=2.0) == [UsedWindow(0.0, 8.0)]
    # Exhausted clip: no gap long enough.
    assert free_windows(5.0, [UsedWindow(0.0, 4.5)], min_len_sec=2.0) == []
