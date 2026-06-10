#!/usr/bin/env python3
"""Productized usage revert + smoke cleanup — dry-run by default.

2026-06-10: the revert procedure previously lived only as prose in the
pgc-production-batch skill, executed as hand-written agent SQL against
production — one typo away from corrupting the usage counters that drive
POI selection for every future batch. This CLI encodes the exact same
contract (SKILL.md "Reverts And Smoke Cleanup") behind a preview/--execute
gate, mirroring ``usage_events_writeback``'s pattern:

- **usage-only revert** (default): call
  ``rpc_revert_poi_asset_usage_manifests(p_manifest_ids)`` — the platform
  RPC deletes the manifest's usage rows AND recomputes asset counters
  from the remaining ledger atomically (never blind subtraction) — then
  verify rows are zero and report any linked approved release candidate
  that remains visible to ``release_unassigned_candidates``.
- **full smoke cleanup** (``--full-cleanup``): additionally mark linked
  ``release_candidates`` rows ``status='rejected'`` (NEVER deleted — the
  audit trail stays) and verify they left
  ``release_unassigned_candidates``. If ANY ``distribution_status`` row
  already claims a candidate, the command refuses to touch release state
  (exit 2) even with --execute — distribution truth belongs to zhongtai.

Drive files are never touched. Without ``--execute`` nothing is written:
the command prints an inspection/preview JSON and exits 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from promo.cli.usage_events_writeback import _create_supabase_client_from_env
from promo.core.release_candidates import RELEASE_CANDIDATES_TABLE

REVERT_RPC = "rpc_revert_poi_asset_usage_manifests"
USAGE_EVENTS_TABLE = "poi_asset_usage_events"
UNASSIGNED_VIEW = "release_unassigned_candidates"
DISTRIBUTION_TABLE = "distribution_status"
CANDIDATE_SELECT = "candidate_id,source_video_key,status,source_output_uri,poi_id,poi_name"
# distribution_status's foreign-key column name is owned by the AIGC repo;
# probe the known spellings and fail closed if neither resolves.
DISTRIBUTION_CANDIDATE_COLUMNS = ("candidate_id", "release_candidate_id")

EXIT_OK = 0
EXIT_VERIFICATION_FAILED = 1
EXIT_BLOCKED_BY_DISTRIBUTION = 2


def _response_data(response: Any) -> Any:
    return getattr(response, "data", response)


def _fetch_usage_rows(client: Any, manifest_ids: list[str]) -> list[dict[str, Any]]:
    rows = _response_data(
        client.table(USAGE_EVENTS_TABLE)
        .select("event_id,manifest_id,asset_id,usage_role")
        .in_("manifest_id", manifest_ids)
        .limit(10000)
        .execute()
    ) or []
    if not isinstance(rows, list):
        raise ValueError(f"{USAGE_EVENTS_TABLE} query returned non-list data")
    return rows


def _fetch_linked_candidates(client: Any, manifest_ids: list[str]) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for manifest_id in manifest_ids:
        rows = _response_data(
            client.table(RELEASE_CANDIDATES_TABLE)
            .select(CANDIDATE_SELECT)
            .like("source_video_key", f"%{manifest_id}%")
            .execute()
        ) or []
        if not isinstance(rows, list):
            raise ValueError(f"{RELEASE_CANDIDATES_TABLE} query returned non-list data")
        for row in rows:
            key = str(row.get("candidate_id"))
            candidates[key] = dict(row)
    return list(candidates.values())


def _fetch_distribution_rows(
    client: Any, candidate_ids: list[str],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Returns ``(rows, error)``. ``rows is None`` means the check itself
    failed — callers must treat that as blocking for full cleanup."""
    if not candidate_ids:
        return [], None
    last_error: str | None = None
    for column in DISTRIBUTION_CANDIDATE_COLUMNS:
        try:
            rows = _response_data(
                client.table(DISTRIBUTION_TABLE)
                .select("*")
                .in_(column, candidate_ids)
                .execute()
            ) or []
            if isinstance(rows, list):
                return rows, None
            last_error = f"{DISTRIBUTION_TABLE} returned non-list data"
        except Exception as exc:  # noqa: BLE001 — probing column spellings
            last_error = str(exc)
    return None, last_error


def _usage_summary(usage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    roles: dict[str, int] = {}
    for row in usage_rows:
        role = str(row.get("usage_role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
    return {
        "event_count": len(usage_rows),
        "affected_asset_count": len({
            str(row.get("asset_id")) for row in usage_rows if row.get("asset_id")
        }),
        "usage_role_counts": roles,
    }


def inspect_state(client: Any, manifest_ids: list[str], *, full_cleanup: bool) -> dict[str, Any]:
    usage_rows = _fetch_usage_rows(client, manifest_ids)
    candidates = _fetch_linked_candidates(client, manifest_ids)
    candidate_ids = [str(c.get("candidate_id")) for c in candidates]
    state: dict[str, Any] = {
        "manifest_ids": manifest_ids,
        "usage": _usage_summary(usage_rows),
        "release_candidates": candidates,
    }
    if full_cleanup:
        distribution_rows, error = _fetch_distribution_rows(client, candidate_ids)
        state["distribution"] = {
            "row_count": None if distribution_rows is None else len(distribution_rows),
            "check_error": error,
        }
    return state


def revert_usage_rows(client: Any, manifest_ids: list[str]) -> dict[str, int]:
    response = client.rpc(REVERT_RPC, {"p_manifest_ids": manifest_ids}).execute()
    data = _response_data(response)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError("revert RPC returned an unexpected shape")
    return {
        "reverted_event_count": int(data.get("out_reverted_event_count") or 0),
        "affected_asset_count": int(data.get("out_affected_asset_count") or 0),
    }


def reject_candidates(client: Any, candidate_ids: list[str]) -> int:
    """Withdraw candidates via status='rejected' — NEVER delete (audit
    trail must survive; zhongtai simply stops seeing them as approved).
    The count is informational; verify_after re-queries actual statuses."""
    updated_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        .replace("+00:00", "Z")
    )
    for candidate_id in candidate_ids:
        client.table(RELEASE_CANDIDATES_TABLE).update(
            {"status": "rejected", "updated_at": updated_at},
        ).eq("candidate_id", candidate_id).execute()
    return len(candidate_ids)


def verify_after(
    client: Any,
    manifest_ids: list[str],
    candidate_ids: list[str],
    *,
    full_cleanup: bool,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    remaining = _fetch_usage_rows(client, manifest_ids)
    checks["usage_rows_zero"] = len(remaining) == 0
    checks["remaining_usage_rows"] = len(remaining)
    if full_cleanup and candidate_ids:
        candidates = _fetch_linked_candidates(client, manifest_ids)
        statuses = {str(c.get("candidate_id")): str(c.get("status")) for c in candidates}
        checks["candidate_statuses"] = statuses
        checks["all_candidates_rejected"] = all(
            statuses.get(cid) == "rejected" for cid in candidate_ids
        )
        try:
            visible = _response_data(
                client.table(UNASSIGNED_VIEW)
                .select("candidate_id")
                .in_("candidate_id", candidate_ids)
                .execute()
            ) or []
            checks["still_in_unassigned_view"] = [
                str(row.get("candidate_id")) for row in visible
            ]
            checks["absent_from_unassigned_view"] = not visible
        except Exception as exc:  # noqa: BLE001 — view check must fail closed
            checks["still_in_unassigned_view"] = None
            checks["absent_from_unassigned_view"] = False
            checks["unassigned_view_error"] = str(exc)
    required = [checks["usage_rows_zero"]]
    if full_cleanup and candidate_ids:
        required.extend([
            checks["all_candidates_rejected"],
            checks["absent_from_unassigned_view"],
        ])
    checks["verified"] = all(required)
    return checks


def run_revert(
    client: Any,
    manifest_ids: list[str],
    *,
    full_cleanup: bool,
    execute: bool,
) -> tuple[dict[str, Any], int]:
    state = inspect_state(client, manifest_ids, full_cleanup=full_cleanup)
    report: dict[str, Any] = {
        "mode": "full_cleanup" if full_cleanup else "usage_only",
        "dry_run": not execute,
        "inspection": state,
    }
    candidate_ids = [
        str(c.get("candidate_id")) for c in state["release_candidates"]
    ]

    if full_cleanup:
        distribution = state["distribution"]
        if distribution["check_error"] or (distribution["row_count"] or 0) > 0:
            report["blocked"] = (
                "distribution_status already references linked candidate(s) — "
                "release state must not be touched without operator + zhongtai "
                "coordination"
                if not distribution["check_error"]
                else f"distribution check failed: {distribution['check_error']}"
            )
            return report, EXIT_BLOCKED_BY_DISTRIBUTION

    approved_remaining = [
        c for c in state["release_candidates"] if str(c.get("status")) == "approved"
    ]
    if not full_cleanup and approved_remaining:
        report["warning"] = (
            "linked approved release candidate(s) remain and may still appear "
            f"in {UNASSIGNED_VIEW}: "
            + ", ".join(str(c.get("candidate_id")) for c in approved_remaining)
        )

    if not execute:
        return report, EXIT_OK

    if state["usage"]["event_count"] > 0:
        report["revert_result"] = revert_usage_rows(client, manifest_ids)
    else:
        report["revert_result"] = {"skipped": "no usage rows for manifest(s)"}
    if full_cleanup and candidate_ids:
        report["rejected_candidate_count"] = reject_candidates(client, candidate_ids)
    report["verification"] = verify_after(
        client, manifest_ids, candidate_ids, full_cleanup=full_cleanup,
    )
    return report, (
        EXIT_OK if report["verification"]["verified"] else EXIT_VERIFICATION_FAILED
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-id",
        action="append",
        required=True,
        dest="manifest_ids",
        help="Manifest id to revert (repeatable).",
    )
    parser.add_argument(
        "--full-cleanup",
        action="store_true",
        help=(
            "Smoke/test cleanup: also mark linked release_candidates rejected "
            "and verify they left the unassigned view. Refuses to run if "
            "distribution_status already claims a candidate."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the revert. Without this flag the command only previews.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parser().parse_args(argv)
    try:
        client = _create_supabase_client_from_env()
        report, exit_code = run_revert(
            client,
            list(args.manifest_ids),
            full_cleanup=args.full_cleanup,
            execute=args.execute,
        )
    except Exception as exc:  # noqa: BLE001 — operator-facing CLI boundary
        print(f"revert failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
