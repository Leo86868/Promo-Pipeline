import json
from types import SimpleNamespace

import pytest


class _TableCall:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.source_video_keys = []

    def insert(self, records):
        self.client.calls.append(("insert", self.table_name, records))
        self.insert_records = records
        return self

    def select(self, columns):
        self.client.calls.append(("select", self.table_name, columns))
        return self

    def in_(self, column, values):
        self.client.calls.append(("in_", self.table_name, column, values))
        self.source_video_keys = list(values)
        return self

    def execute(self):
        if hasattr(self, "insert_records"):
            self.client.rows.extend(self.insert_records)
            return SimpleNamespace(data=self.insert_records)
        return SimpleNamespace(data=[
            row for row in self.client.rows
            if row.get("source_video_key") in set(self.source_video_keys)
        ])


class _Client:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return _TableCall(self, name)


def _record(source_video_key="manifest:manifest_1:variant:1", status="approved"):
    return {
        "source_pipeline": "pgc_65s",
        "source_video_key": source_video_key,
        "source_run_id": "pgc_run_1",
        "source_batch_id": "pgc_batch_1",
        "poi_id": "poi_1",
        "poi_name": "Test Resort",
        "source_output_uri": "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "status": status,
        "music_label": "Run Away with Me",
    }


def test_load_release_candidate_records_accepts_handoff_object(tmp_path):
    from promo.core.release_candidates import load_release_candidate_records

    record = _record()
    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_text(
        json.dumps({"release_candidates": [record]}),
        encoding="utf-8",
    )

    assert load_release_candidate_records(handoff_path) == [record]


def test_load_release_candidate_records_defaults_missing_status(tmp_path):
    from promo.core.release_candidates import load_release_candidate_records

    record = _record()
    record.pop("status")
    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_text(
        json.dumps({"release_candidates": [record]}),
        encoding="utf-8",
    )

    [loaded] = load_release_candidate_records(handoff_path)

    assert loaded["status"] == "approved"


def test_load_release_candidate_records_requires_drive_uri(tmp_path):
    from promo.core.release_candidates import (
        ReleaseCandidateRegistrationError,
        load_release_candidate_records,
    )

    record = {**_record(), "source_output_uri": "/tmp/final.mp4"}
    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_text(
        json.dumps({"release_candidates": [record]}),
        encoding="utf-8",
    )

    with pytest.raises(ReleaseCandidateRegistrationError, match="drive:<file_id>"):
        load_release_candidate_records(handoff_path)


def test_insert_release_candidates_uses_release_candidates_table():
    from promo.core.release_candidates import (
        RELEASE_CANDIDATES_TABLE,
        insert_release_candidates,
    )

    record = _record()
    client = _Client()

    result = insert_release_candidates(client, [record])

    assert result == {"inserted_count": 1, "skipped_count": 0}
    assert client.calls == [
        ("table", RELEASE_CANDIDATES_TABLE),
        ("insert", RELEASE_CANDIDATES_TABLE, [record]),
    ]


class _ApiError(Exception):
    """Mimics postgrest.exceptions.APIError: carries a SQLSTATE .code."""

    def __init__(self, code, message="error"):
        super().__init__(message)
        self.code = code
        self.message = message


class _CollidingTable:
    """Table double that raises a per-row error for chosen source_video_keys."""

    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name

    def insert(self, records):
        self._records = records if isinstance(records, list) else [records]
        return self

    def execute(self):
        for record in self._records:
            key = record.get("source_video_key")
            err = self.client.errors_by_key.get(key)
            if err is not None:
                self.client.attempts.append(("raise", key, err.code))
                raise err
            self.client.attempts.append(("insert", key))
            self.client.rows.append(record)
        return SimpleNamespace(data=list(self._records))


class _CollidingClient:
    def __init__(self, errors_by_key):
        self.errors_by_key = errors_by_key
        self.rows = []
        self.attempts = []

    def table(self, name):
        return _CollidingTable(self, name)


def test_insert_release_candidates_skips_23505_per_row_not_whole_batch(caplog):
    import logging

    from promo.core.release_candidates import insert_release_candidates

    row_a = _record(source_video_key="manifest:m_a:variant:1")
    row_b = _record(source_video_key="manifest:m_b:variant:1")  # collides
    row_c = _record(source_video_key="manifest:m_c:variant:1")
    client = _CollidingClient({
        "manifest:m_b:variant:1": _ApiError("23505", "duplicate key"),
    })

    with caplog.at_level(logging.WARNING):
        result = insert_release_candidates(client, [row_a, row_b, row_c])

    # The colliding row is skipped; the OTHER rows still land (per-row, not
    # whole-batch dropped).
    assert result == {"inserted_count": 2, "skipped_count": 1}
    landed_keys = {row["source_video_key"] for row in client.rows}
    assert landed_keys == {"manifest:m_a:variant:1", "manifest:m_c:variant:1"}
    # The batch attempt rolled back (it raised), then per-row retry ran.
    assert "manifest:m_b:variant:1" in caplog.text
    assert "23505" in caplog.text


def test_insert_release_candidates_reraises_non_23505(caplog):
    import logging

    from promo.core.release_candidates import insert_release_candidates

    row_a = _record(source_video_key="manifest:m_a:variant:1")
    row_b = _record(source_video_key="manifest:m_b:variant:1")
    client = _CollidingClient({
        "manifest:m_b:variant:1": _ApiError("23502", "not-null violation"),
    })

    with caplog.at_level(logging.WARNING):
        with pytest.raises(_ApiError) as exc_info:
            insert_release_candidates(client, [row_a, row_b])

    # A non-23505 error is NOT swallowed — it propagates.
    assert exc_info.value.code == "23502"


def test_is_unique_violation_matches_real_postgrest_shape():
    """Pin P1g's live capture: the REAL postgrest APIError for a P1e 23505.

    PGC's ``_is_unique_violation`` depends on the SQLSTATE living in
    ``APIError.code``. If a future postgrest upgrade stops mapping the SQLSTATE
    into ``.code``, this breaks loudly here instead of silently letting a real
    cross-paradigm collision abort the whole batch. The error body below was
    captured LIVE from prod on 2026-06-23 (see workflow/p1g-23505-shape.md):
    the index name lives in ``.message`` and the colliding key in ``.details``.
    """
    from postgrest.exceptions import APIError

    from promo.core.release_candidates import _is_unique_violation

    raw = {
        "code": "23505",
        "message": (
            "duplicate key value violates unique constraint "
            '"uq_release_candidates_poi_recipe_fingerprint"'
        ),
        "details": (
            "Key (poi_id, recipe_fingerprint)=(poi_15d874e8787d, "
            "rfp2:bccd5f3be3436084e0f9f2b7da8b2f44318a59d97adf83eb03791693d250a05e)"
            " already exists."
        ),
        "hint": None,
    }
    exc = APIError(raw)

    # The matcher's load-bearing assumption: SQLSTATE is exposed as .code.
    assert exc.code == "23505"
    assert _is_unique_violation(exc) is True
    # WHICH index fired is recoverable from the error text (.message), and the
    # colliding columns from .details — both used by the operator-facing log.
    assert "uq_release_candidates_poi_recipe_fingerprint" in exc.message
    assert "(poi_id, recipe_fingerprint)" in exc.details


def test_verify_release_candidates_confirms_matching_rows():
    from promo.core.release_candidates import verify_release_candidates

    record = _record()
    client = _Client([record])

    result = verify_release_candidates(client, [record])

    assert result["verified"] is True
    assert result["expected_count"] == 1
    assert result["observed_count"] == 1
    assert result["missing_count"] == 0
    assert result["mismatch_count"] == 0


def test_verify_release_candidates_reports_missing_rows():
    from promo.core.release_candidates import verify_release_candidates

    record = _record()
    client = _Client([])

    result = verify_release_candidates(client, [record])

    assert result["verified"] is False
    assert result["missing_source_video_keys"] == [record["source_video_key"]]


def test_verify_release_candidates_reports_field_mismatches():
    from promo.core.release_candidates import verify_release_candidates

    expected = _record(status="approved")
    observed = _record(status="queued")
    client = _Client([observed])

    result = verify_release_candidates(client, [expected])

    assert result["verified"] is False
    assert result["mismatches"] == [{
        "source_video_key": expected["source_video_key"],
        "fields": [{
            "field": "status",
            "expected": "approved",
            "observed": "queued",
        }],
    }]


def test_register_release_candidates_inserts_missing_rows_and_verifies():
    from promo.core.release_candidates import register_release_candidates

    record = _record()
    client = _Client([])

    result = register_release_candidates(client, [record])

    assert result["inserted_count"] == 1
    assert result["already_registered_count"] == 0
    assert result["verification"]["verified"] is True


def test_register_release_candidates_skips_existing_matching_rows():
    from promo.core.release_candidates import register_release_candidates

    record = _record()
    client = _Client([record])

    result = register_release_candidates(client, [record])

    assert result["inserted_count"] == 0
    assert result["already_registered_count"] == 1
    assert result["verification"]["verified"] is True


def test_register_release_candidates_rejects_existing_conflict():
    from promo.core.release_candidates import (
        ReleaseCandidateRegistrationError,
        register_release_candidates,
    )

    expected = _record(status="approved")
    observed = _record(status="queued")
    client = _Client([observed])

    with pytest.raises(
        ReleaseCandidateRegistrationError,
        match="conflict",
    ):
        register_release_candidates(client, [expected])


def test_register_release_candidates_cli_dry_run_writes_summary(tmp_path, capsys):
    from promo.cli.register_release_candidates import main

    handoff_path = tmp_path / "handoff.json"
    handoff_path.write_text(
        json.dumps({"release_candidates": [_record()]}),
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "sys.argv",
            [
                "register_release_candidates",
                "--handoff",
                str(handoff_path),
            ],
        )
        assert main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["execute"] is False
    assert payload["summary"]["candidate_count"] == 1
