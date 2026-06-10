"""翻转二 B3 — first reader of the usage ledger's source-window fields.

``poi_asset_usage_events`` has persisted ``trim_start_sec`` /
``display_start_sec`` / ``display_end_sec`` / ``source_duration_sec``
since AIGC migration 036 (3,343 rows verified complete 2026-06-10), but
until this module NOTHING ever read them back. The packer's window
rotation (anti-TikTok-dedup, 设计契约 rule ④) is the first consumer, so
the conventions below are the FAMILY STANDARD — music remix adopts the
same query when it retires its stateless-hash trim (notify the AIGC
side once this lands).

Query convention (the standard):

- **Key by ``asset_id``** (platform identity), never by local clip_id —
  clip numbering is per-run; the caller maps clip_id ↔ asset_id through
  the run's shared-assets metadata.
- **Count every ``usage_role``** — bridge showings are showings; a
  viewer who saw seconds 0-3 as a bridge has still seen seconds 0-3.
- **Used source window** = ``[trim_start_sec, trim_start_sec +
  (display_end_sec − display_start_sec))`` — display length mapped back
  onto the source clock at the recorded trim offset.
- **Stable pagination**: explicit ``ORDER BY event_id`` before
  ``.range()`` (the 2026-06-09 pagination fix — PostgREST pages are
  unstable without it and concurrent writers can shift rows).

Failure contract (设计契约 rule ②, fail-closed): any query error raises
``UsageWindowError``. In production the caller lets the video fail and
``--resume`` recovers; reads ride the same Supabase path as the usage
WRITES, so a genuine read failure is as rare as a writeback failure.
Rows with missing window fields are NOT read failures — they are
counted, logged, and skipped (the ledger predating migration 036 was
verified complete, so this is belt-and-braces, not an expected path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

USAGE_EVENTS_TABLE = "poi_asset_usage_events"
_WINDOW_FIELDS = (
    "asset_id",
    "trim_start_sec",
    "display_start_sec",
    "display_end_sec",
    "source_duration_sec",
)


class UsageWindowError(RuntimeError):
    """Ledger read failed — production callers fail the video (fail-closed)."""


@dataclass(frozen=True)
class UsedWindow:
    """Half-open ``[start_sec, end_sec)`` interval on the SOURCE clock."""

    start_sec: float
    end_sec: float


def fetch_used_windows(
    client: Any,
    asset_ids: list[str],
    *,
    page_size: int = 1000,
) -> dict[str, list[UsedWindow]]:
    """Return merged used windows per asset_id for ``asset_ids``.

    Assets with no usage history are absent from the result (the packer
    treats absence as "whole source free"). Raises
    :class:`UsageWindowError` on any query failure.
    """
    if not asset_ids:
        return {}
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    rows: list[dict[str, Any]] = []
    start = 0
    try:
        while True:
            page = (
                client.table(USAGE_EVENTS_TABLE)
                .select(",".join(_WINDOW_FIELDS))
                .in_("asset_id", list(asset_ids))
                .order("event_id")
                .range(start, start + page_size - 1)
                .execute()
                .data
            ) or []
            rows.extend(page)
            if len(page) < page_size:
                break
            start += page_size
    except Exception as exc:
        raise UsageWindowError(
            f"usage window query failed for {len(asset_ids)} asset(s): {exc}"
        ) from exc

    windows: dict[str, list[UsedWindow]] = {}
    malformed = 0
    stale = 0
    for row in rows:
        try:
            asset_id = str(row["asset_id"])
            trim = float(row["trim_start_sec"])
            display_len = float(row["display_end_sec"]) - float(row["display_start_sec"])
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        if display_len <= 0:
            malformed += 1
            continue
        start = max(0.0, trim)
        end = start + display_len
        # Clamp to the ROW's recorded source duration (2026-06-10 review
        # blocking #2). NOT because such rows exist today — a full-ledger
        # audit (2026-06-10) found 0 out-of-range windows in 4,532
        # comparable rows — but because ledger and source have no sync
        # guarantee, and an unclamped row would manufacture a false free
        # window that kills the video at the validator. One-line lifetime
        # insurance against metadata bugs / future pipelines / hand-edits.
        try:
            src = float(row["source_duration_sec"])
        except (KeyError, TypeError, ValueError):
            src = None
        if src is not None and src > 0:
            start = min(start, src)
            end = min(end, src)
            if end - start <= 0:
                stale += 1
                continue
        windows.setdefault(asset_id, []).append(
            UsedWindow(start_sec=start, end_sec=end)
        )
    if malformed or stale:
        logger.warning(
            "usage window ledger: skipped %d malformed row(s) and %d stale "
            "row(s) whose window lies beyond the recorded source duration",
            malformed, stale,
        )
    return {aid: merge_windows(ws) for aid, ws in windows.items()}


def merge_windows(windows: list[UsedWindow]) -> list[UsedWindow]:
    """Merge overlapping/touching intervals; returns sorted disjoint list."""
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: (w.start_sec, w.end_sec))
    merged = [ordered[0]]
    for w in ordered[1:]:
        last = merged[-1]
        if w.start_sec <= last.end_sec:
            if w.end_sec > last.end_sec:
                merged[-1] = UsedWindow(last.start_sec, w.end_sec)
        else:
            merged.append(w)
    return merged


def free_windows(
    source_duration_sec: float,
    used: list[UsedWindow],
    *,
    min_len_sec: float,
) -> list[UsedWindow]:
    """Gaps of ``source_duration_sec`` not covered by ``used`` (assumed
    merged+sorted), at least ``min_len_sec`` long — the packer picks
    ``trim_start`` inside one of these. Empty result = source exhausted
    for spans this long (packer falls to its least-overlap tie-break)."""
    if source_duration_sec <= 0 or min_len_sec <= 0:
        return []
    gaps: list[UsedWindow] = []
    cursor = 0.0
    for w in used:
        # Clamp BEFORE the length test (2026-06-10 review blocking #2):
        # judging the gap by the unclamped window start would let a
        # window beyond the source (e.g. [6,7) on a 5s clip) certify a
        # 1.5s tail as a 2s free window — packer places a span there,
        # validator kills the video. No such rows exist in today's
        # ledger (audited 2026-06-10: 0/4,532); this is the same
        # no-sync-guarantee insurance as the fetch-side clamp.
        w_start = min(max(0.0, w.start_sec), source_duration_sec)
        w_end = min(max(0.0, w.end_sec), source_duration_sec)
        if w_start - cursor >= min_len_sec:
            gaps.append(UsedWindow(cursor, w_start))
        cursor = max(cursor, w_end)
        if cursor >= source_duration_sec:
            return gaps
    if source_duration_sec - cursor >= min_len_sec:
        gaps.append(UsedWindow(cursor, source_duration_sec))
    return gaps
