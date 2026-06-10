"""Unit tests for promo.cli.revert_usage.

2026-06-10: productizes the skill's prose revert procedure (previously
hand-written agent SQL against prod). Contract under test: dry-run by
default, usage revert via the platform RPC, full cleanup rejects (never
deletes) candidates, and distribution_status presence blocks release
mutations entirely.
"""

from types import SimpleNamespace

import pytest

from promo.cli import revert_usage as ru


class _FakeQuery:
    def __init__(self, client, table_name):
        self._client = client
        self._table = table_name
        self._filters = {}
        self._operation = "select"
        self._update_payload = None

    def select(self, _columns):
        self._operation = "select"
        return self

    def update(self, payload):
        self._operation = "update"
        self._update_payload = dict(payload)
        return self

    def delete(self):  # pragma: no cover — must never be reached
        raise AssertionError("revert CLI must NEVER delete rows")

    def in_(self, column, values):
        self._filters[column] = list(values)
        return self

    def eq(self, column, value):
        self._filters[column] = value
        return self

    def like(self, column, pattern):
        self._filters[column] = pattern
        return self

    def limit(self, _count):
        return self

    def execute(self):
        return SimpleNamespace(
            data=self._client.handle(
                self._table, self._operation, self._filters, self._update_payload,
            )
        )


class _FakeSupabase:
    """Programmable in-memory stand-in for the four tables/views + RPC."""

    def __init__(
        self,
        *,
        usage_rows=None,
        candidates=None,
        distribution_rows=None,
        distribution_column="candidate_id",
    ):
        self.usage_rows = list(usage_rows or [])
        self.candidates = {str(c["candidate_id"]): dict(c) for c in (candidates or [])}
        self.distribution_rows = list(distribution_rows or [])
        self.distribution_column = distribution_column
        self.rpc_calls = []
        self.updates = []

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        manifest_ids = set(params["p_manifest_ids"])
        reverted = [r for r in self.usage_rows if r["manifest_id"] in manifest_ids]
        self.usage_rows = [
            r for r in self.usage_rows if r["manifest_id"] not in manifest_ids
        ]
        payload = {
            "out_reverted_event_count": len(reverted),
            "out_affected_asset_count": len({r["asset_id"] for r in reverted}),
        }
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=payload))

    def handle(self, table, operation, filters, update_payload):
        if table == ru.USAGE_EVENTS_TABLE:
            wanted = set(filters.get("manifest_id") or [])
            return [r for r in self.usage_rows if r["manifest_id"] in wanted]
        if table == ru.RELEASE_CANDIDATES_TABLE:
            if operation == "update":
                candidate_id = str(filters.get("candidate_id"))
                self.updates.append((candidate_id, dict(update_payload)))
                row = self.candidates.get(candidate_id)
                if row is not None:
                    row.update(update_payload)
                return [dict(row)] if row else []
            pattern = str(filters.get("source_video_key") or "").strip("%")
            return [
                dict(c) for c in self.candidates.values()
                if pattern in str(c.get("source_video_key") or "")
            ]
        if table == ru.DISTRIBUTION_TABLE:
            if self.distribution_column not in filters:
                raise RuntimeError(f"column {list(filters)} does not exist")
            wanted = set(filters[self.distribution_column])
            return [
                dict(r) for r in self.distribution_rows
                if str(r.get(self.distribution_column)) in wanted
            ]
        if table == ru.UNASSIGNED_VIEW:
            wanted = set(filters.get("candidate_id") or [])
            return [
                {"candidate_id": cid}
                for cid, c in self.candidates.items()
                if cid in wanted and c.get("status") == "approved"
            ]
        raise AssertionError(f"unexpected table {table}")


def _usage_row(manifest_id="m1", asset_id="a1", role="assigned_phrase"):
    return {
        "event_id": f"e-{manifest_id}-{asset_id}",
        "manifest_id": manifest_id,
        "asset_id": asset_id,
        "usage_role": role,
    }


def _candidate(candidate_id="c1", manifest_id="m1", status="approved"):
    return {
        "candidate_id": candidate_id,
        "source_video_key": f"pgc/{manifest_id}/video_001",
        "status": status,
        "source_output_uri": "drive:abc",
        "poi_id": "poi_1",
        "poi_name": "Hotel",
    }


def test_dry_run_previews_without_any_writes():
    client = _FakeSupabase(
        usage_rows=[_usage_row(asset_id="a1"), _usage_row(asset_id="a2")],
        candidates=[_candidate()],
    )
    report, exit_code = ru.run_revert(
        client, ["m1"], full_cleanup=False, execute=False,
    )
    assert exit_code == ru.EXIT_OK
    assert report["dry_run"] is True
    assert report["inspection"]["usage"]["event_count"] == 2
    assert report["inspection"]["usage"]["affected_asset_count"] == 2
    # Approved candidate surfaced as a warning, exactly like the skill says.
    assert "c1" in report["warning"]
    # Nothing written: no RPC, no updates, usage rows intact.
    assert client.rpc_calls == []
    assert client.updates == []
    assert len(client.usage_rows) == 2


def test_usage_only_execute_calls_rpc_and_verifies_zero_rows():
    client = _FakeSupabase(usage_rows=[_usage_row()], candidates=[])
    report, exit_code = ru.run_revert(
        client, ["m1"], full_cleanup=False, execute=True,
    )
    assert exit_code == ru.EXIT_OK
    assert client.rpc_calls == [
        (ru.REVERT_RPC, {"p_manifest_ids": ["m1"]}),
    ]
    assert report["revert_result"]["reverted_event_count"] == 1
    assert report["verification"]["usage_rows_zero"] is True
    assert client.updates == []  # usage-only never touches candidates


def test_full_cleanup_blocked_by_distribution_row_even_with_execute():
    client = _FakeSupabase(
        usage_rows=[_usage_row()],
        candidates=[_candidate()],
        distribution_rows=[{"candidate_id": "c1", "status": "assigned"}],
    )
    report, exit_code = ru.run_revert(
        client, ["m1"], full_cleanup=True, execute=True,
    )
    assert exit_code == ru.EXIT_BLOCKED_BY_DISTRIBUTION
    assert "distribution" in report["blocked"]
    # Hard stop: zero writes of any kind.
    assert client.rpc_calls == []
    assert client.updates == []
    assert len(client.usage_rows) == 1


def test_full_cleanup_distribution_check_failure_fails_closed():
    """If the distribution column probe cannot resolve, full cleanup must
    refuse to run rather than assume no distribution rows exist."""
    client = _FakeSupabase(
        usage_rows=[_usage_row()],
        candidates=[_candidate()],
        distribution_column="some_future_column",
    )
    report, exit_code = ru.run_revert(
        client, ["m1"], full_cleanup=True, execute=True,
    )
    assert exit_code == ru.EXIT_BLOCKED_BY_DISTRIBUTION
    assert "distribution check failed" in report["blocked"]
    assert client.rpc_calls == []
    assert client.updates == []


def test_full_cleanup_rejects_candidate_and_verifies_view_absence():
    """Zero usage rows must NOT short-circuit the candidate withdrawal
    (the skill's zero_usage_approved_candidate case)."""
    client = _FakeSupabase(
        usage_rows=[],  # usage already zero
        candidates=[_candidate(status="approved")],
    )
    report, exit_code = ru.run_revert(
        client, ["m1"], full_cleanup=True, execute=True,
    )
    assert exit_code == ru.EXIT_OK
    assert client.rpc_calls == []  # nothing to revert
    assert report["revert_result"] == {"skipped": "no usage rows for manifest(s)"}
    # Candidate withdrawn via status update (never delete) + updated_at set.
    assert len(client.updates) == 1
    candidate_id, payload = client.updates[0]
    assert candidate_id == "c1"
    assert payload["status"] == "rejected"
    assert payload["updated_at"]
    verification = report["verification"]
    assert verification["all_candidates_rejected"] is True
    assert verification["absent_from_unassigned_view"] is True
    assert verification["verified"] is True


def test_verification_failure_returns_nonzero():
    """A candidate that somehow stays approved after the update fails the
    run loudly instead of reporting success."""

    class _StubbornFake(_FakeSupabase):
        def handle(self, table, operation, filters, update_payload):
            if table == ru.RELEASE_CANDIDATES_TABLE and operation == "update":
                self.updates.append((str(filters.get("candidate_id")), dict(update_payload)))
                return []  # update silently does nothing
            return super().handle(table, operation, filters, update_payload)

    client = _StubbornFake(usage_rows=[], candidates=[_candidate(status="approved")])
    report, exit_code = ru.run_revert(
        client, ["m1"], full_cleanup=True, execute=True,
    )
    assert exit_code == ru.EXIT_VERIFICATION_FAILED
    assert report["verification"]["verified"] is False
    assert report["verification"]["all_candidates_rejected"] is False
