from types import SimpleNamespace


class _RpcCall:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return SimpleNamespace(data=self._data)


class _RpcClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _RpcCall(self.data)


class _TableQuery:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls
        self.event_ids = []

    def select(self, columns):
        self.calls.append(("select", columns))
        return self

    def in_(self, column, values):
        self.calls.append(("in_", column, values))
        self.event_ids = list(values)
        return self

    def execute(self):
        return SimpleNamespace(data=[
            row for row in self.rows
            if row.get("event_id") in set(self.event_ids)
        ])


class _TableClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return _TableQuery(self.rows, self.calls)


def _usage_event(event_id="event-1", asset_id="asset-1"):
    return {
        "event_id": event_id,
        "manifest_id": "manifest-1",
        "run_id": "run-1",
        "poi_id": "poi-1",
        "asset_id": asset_id,
        "clip_id": "clip-1",
        "variant_index": 1,
        "occurrence_index": 0,
        "occurrence_id": "v1_occ_0000",
        "usage_role": "assigned_phrase",
    }


def test_record_usage_events_calls_expected_rpc():
    from promo.cli.usage_events_writeback import RPC_NAME, record_usage_events

    client = _RpcClient([{
        "out_inserted_count": 2,
        "out_duplicate_count": 1,
    }])
    events = [{"event_id": "sha256:" + "a" * 64}]

    result = record_usage_events(client, events)

    assert result == {"inserted_count": 2, "duplicate_count": 1}
    assert client.calls == [(RPC_NAME, {"p_payload": events})]


def test_record_usage_events_accepts_dict_response():
    from promo.cli.usage_events_writeback import record_usage_events

    client = _RpcClient({
        "out_inserted_count": 1,
        "out_duplicate_count": 0,
    })

    assert record_usage_events(client, []) == {
        "inserted_count": 1,
        "duplicate_count": 0,
    }


def test_verify_usage_events_confirms_rows_exist_with_matching_fields():
    from promo.cli.usage_events_writeback import (
        USAGE_EVENTS_TABLE,
        verify_usage_events,
    )

    event = _usage_event()
    client = _TableClient([event])

    result = verify_usage_events(client, [event])

    assert result["verified"] is True
    assert result["expected_count"] == 1
    assert result["observed_count"] == 1
    assert result["missing_count"] == 0
    assert result["mismatch_count"] == 0
    assert client.calls[0] == ("table", USAGE_EVENTS_TABLE)


def test_verify_usage_events_reports_missing_event_ids():
    from promo.cli.usage_events_writeback import verify_usage_events

    event = _usage_event()
    client = _TableClient([])

    result = verify_usage_events(client, [event])

    assert result["verified"] is False
    assert result["missing_event_ids"] == ["event-1"]
    assert result["missing_count"] == 1


def test_verify_usage_events_reports_field_mismatches():
    from promo.cli.usage_events_writeback import verify_usage_events

    expected = _usage_event(asset_id="asset-1")
    observed = {**expected, "asset_id": "asset-2"}
    client = _TableClient([observed])

    result = verify_usage_events(client, [expected])

    assert result["verified"] is False
    assert result["mismatch_count"] == 1
    assert result["mismatches"] == [{
        "event_id": "event-1",
        "fields": [{
            "field": "asset_id",
            "expected": "asset-1",
            "observed": "asset-2",
        }],
    }]
