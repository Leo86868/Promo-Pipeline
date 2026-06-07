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
