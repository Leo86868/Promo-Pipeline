"""Release-candidate registration helpers for approved PGC videos."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

#: Postgres unique-violation SQLSTATE. Raised when an insert collides with the
#: UNIQUE (poi_id, recipe_fingerprint) index — i.e. the same content recipe was
#: already registered (cross-paradigm content dedup, H1).
UNIQUE_VIOLATION_CODE = "23505"

RELEASE_CANDIDATES_TABLE = "release_candidates"
VERIFY_SELECT_COLUMNS = (
    "source_pipeline,source_video_key,source_run_id,source_batch_id,"
    "poi_id,poi_name,source_output_uri,status,music_label"
)
VERIFY_COMPARE_FIELDS = (
    "source_pipeline",
    "source_video_key",
    "source_run_id",
    "source_batch_id",
    "poi_id",
    "poi_name",
    "source_output_uri",
    "status",
    "music_label",
)


class ReleaseCandidateRegistrationError(ValueError):
    """Raised when release candidate records are unsafe to register."""


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def _required_text(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCandidateRegistrationError(f"{field} is required")
    return value.strip()


def _optional_compare_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _chunked(items: list[str], chunk_size: int) -> list[list[str]]:
    if chunk_size <= 0:
        raise ReleaseCandidateRegistrationError("chunk_size must be positive")
    return [
        items[index:index + chunk_size]
        for index in range(0, len(items), chunk_size)
    ]


def load_release_candidate_records(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if isinstance(payload, dict):
        payload = payload.get("release_candidates")
    if not isinstance(payload, list) or not payload:
        raise ReleaseCandidateRegistrationError(
            "handoff JSON must contain non-empty release_candidates"
        )

    records: list[dict[str, Any]] = []
    source_video_keys: set[str] = set()
    drive_uris: set[str] = set()
    for index, record in enumerate(payload, start=1):
        if not isinstance(record, dict):
            raise ReleaseCandidateRegistrationError(
                f"release_candidates[{index}] must be an object"
            )
        normalized = dict(record)
        if not _optional_compare_value(normalized.get("status")):
            normalized["status"] = "approved"
        for field in (
            "source_pipeline",
            "source_video_key",
            "source_run_id",
            "poi_id",
            "poi_name",
            "source_output_uri",
            "music_label",
        ):
            _required_text(normalized, field)
        _required_text(normalized, "status")
        source_output_uri = _required_text(normalized, "source_output_uri")
        if not source_output_uri.startswith("drive:") or source_output_uri == "drive:":
            raise ReleaseCandidateRegistrationError(
                "source_output_uri must be drive:<file_id>"
            )
        source_video_key = _required_text(normalized, "source_video_key")
        if source_video_key in source_video_keys:
            raise ReleaseCandidateRegistrationError(
                "duplicate source_video_key in release_candidates"
            )
        if source_output_uri in drive_uris:
            raise ReleaseCandidateRegistrationError(
                "duplicate source_output_uri in release_candidates"
            )
        source_video_keys.add(source_video_key)
        drive_uris.add(source_output_uri)
        records.append(normalized)
    return records


def summarize_release_candidates(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "candidate_count": len(records),
        "poi_count": len({record["poi_id"] for record in records}),
        "drive_uri_count": len({record["source_output_uri"] for record in records}),
    }


def _is_unique_violation(exc: BaseException) -> bool:
    """True iff ``exc`` is a Postgres 23505 unique-violation.

    Inspects the supabase/postgrest ``APIError`` shape (a ``.code`` attribute
    carrying the SQLSTATE) without importing postgrest, so this stays usable
    against test doubles. Only the 23505 code is matched — every other error is
    treated as fatal by the caller (never swallowed).
    """
    return getattr(exc, "code", None) == UNIQUE_VIOLATION_CODE


def _inserted_count_from_response(response: Any, fallback_count: int) -> int:
    data = _response_data(response)
    if data is None:
        return fallback_count
    if isinstance(data, list):
        return len(data)
    raise ReleaseCandidateRegistrationError(
        "release_candidates insert returned an unexpected shape"
    )


def insert_release_candidates(
    client: Any,
    records: list[dict[str, Any]],
) -> dict[str, int]:
    """Insert release-candidate rows, tolerating per-row content-dedup collisions.

    A batch insert is atomic, so a single ``(poi_id, recipe_fingerprint)``
    unique-violation (Postgres 23505) would otherwise abort the WHOLE batch.
    Here we try the batch first; on a 23505 we fall back to inserting row by
    row, SKIPPING (and logging) only the rows that collide on the content index
    and keeping every other row. Any non-23505 error still raises — collisions
    are tolerated, real failures are not.
    """
    if not records:
        return {"inserted_count": 0, "skipped_count": 0}

    try:
        response = client.table(RELEASE_CANDIDATES_TABLE).insert(records).execute()
    except Exception as exc:  # noqa: BLE001 - re-raised below unless 23505
        if not _is_unique_violation(exc):
            raise
        # At least one row in the batch collided on content dedup; the atomic
        # batch rolled back. Retry per-row so the non-colliding rows still land.
        return _insert_release_candidates_per_row(client, records)

    return {
        "inserted_count": _inserted_count_from_response(response, len(records)),
        "skipped_count": 0,
    }


def _insert_release_candidates_per_row(
    client: Any,
    records: list[dict[str, Any]],
) -> dict[str, int]:
    inserted_count = 0
    skipped_count = 0
    for record in records:
        try:
            response = (
                client.table(RELEASE_CANDIDATES_TABLE).insert(record).execute()
            )
        except Exception as exc:  # noqa: BLE001 - re-raised below unless 23505
            if not _is_unique_violation(exc):
                raise
            skipped_count += 1
            logger.warning(
                "release_candidates: skipping content-dedup collision (23505): "
                "poi_id=%s source_video_key=%s — already registered under "
                "UNIQUE(poi_id, recipe_fingerprint)",
                record.get("poi_id"),
                record.get("source_video_key"),
            )
            continue
        inserted_count += _inserted_count_from_response(response, 1)
    return {"inserted_count": inserted_count, "skipped_count": skipped_count}


def register_release_candidates(
    client: Any,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    preflight = verify_release_candidates(client, records)
    if preflight["mismatch_count"] or preflight["duplicate_observed_key_count"]:
        raise ReleaseCandidateRegistrationError(
            "existing release_candidates conflict with handoff"
        )

    missing_keys = set(preflight["missing_source_video_keys"])
    records_to_insert = [
        record for record in records
        if record["source_video_key"] in missing_keys
    ]
    inserted_count = 0
    skipped_count = 0
    if records_to_insert:
        insert_result = insert_release_candidates(client, records_to_insert)
        inserted_count = insert_result["inserted_count"]
        skipped_count = insert_result.get("skipped_count", 0)

    verification = verify_release_candidates(client, records)
    return {
        "inserted_count": inserted_count,
        "skipped_count": skipped_count,
        "already_registered_count": len(records) - len(records_to_insert),
        "verification": verification,
    }


def fetch_release_candidate_rows(
    client: Any,
    source_video_keys: list[str],
    *,
    chunk_size: int = 100,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk in _chunked(source_video_keys, chunk_size):
        response = (
            client.table(RELEASE_CANDIDATES_TABLE)
            .select(VERIFY_SELECT_COLUMNS)
            .in_("source_video_key", chunk)
            .execute()
        )
        data = _response_data(response) or []
        if not isinstance(data, list):
            raise ReleaseCandidateRegistrationError(
                "release candidate verification query returned non-list data"
            )
        rows.extend(data)
    return rows


def verify_release_candidates(
    client: Any,
    records: list[dict[str, Any]],
    *,
    chunk_size: int = 100,
) -> dict[str, Any]:
    expected_by_key = {record["source_video_key"]: record for record in records}
    if len(expected_by_key) != len(records):
        raise ReleaseCandidateRegistrationError(
            "duplicate source_video_key in release_candidates"
        )
    if not expected_by_key:
        return {
            "verified": True,
            "expected_count": 0,
            "observed_count": 0,
            "missing_count": 0,
            "mismatch_count": 0,
            "duplicate_observed_key_count": 0,
            "missing_source_video_keys": [],
            "mismatches": [],
            "duplicate_observed_source_video_keys": [],
        }

    observed_rows = fetch_release_candidate_rows(
        client,
        list(expected_by_key),
        chunk_size=chunk_size,
    )
    observed_by_key: dict[str, dict[str, Any]] = {}
    duplicate_observed_keys: list[str] = []
    for row in observed_rows:
        key = str(row.get("source_video_key") or "")
        if key in observed_by_key:
            duplicate_observed_keys.append(key)
        observed_by_key[key] = row

    missing_keys = sorted(set(expected_by_key) - set(observed_by_key))
    mismatches: list[dict[str, Any]] = []
    for key, expected in expected_by_key.items():
        observed = observed_by_key.get(key)
        if observed is None:
            continue
        fields: list[dict[str, Any]] = []
        for field in VERIFY_COMPARE_FIELDS:
            expected_value = _optional_compare_value(expected.get(field))
            observed_value = _optional_compare_value(observed.get(field))
            if expected_value != observed_value:
                fields.append({
                    "field": field,
                    "expected": expected.get(field),
                    "observed": observed.get(field),
                })
        if fields:
            mismatches.append({
                "source_video_key": key,
                "fields": fields,
            })

    verified = not (missing_keys or mismatches or duplicate_observed_keys)
    return {
        "verified": verified,
        "expected_count": len(expected_by_key),
        "observed_count": len(observed_rows),
        "missing_count": len(missing_keys),
        "mismatch_count": len(mismatches),
        "duplicate_observed_key_count": len(duplicate_observed_keys),
        "missing_source_video_keys": missing_keys,
        "mismatches": mismatches,
        "duplicate_observed_source_video_keys": sorted(set(duplicate_observed_keys)),
    }
