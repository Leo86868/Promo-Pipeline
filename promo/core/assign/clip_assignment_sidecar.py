"""Sidecar reader for ``clip_assignments_{slug}_{Ns}s.json`` payloads.

Extracted from ``clip_assigner.py`` (Sprint S2b) — orthogonal to the
Gemini #2 assignment path (validator + prompt + API call). The reader
is invoked at fixture-replay time and by ``compile_promo``'s sidecar
loader to materialise prior variant outputs without re-running Gemini.

The writer side lives in ``compile_promo._write_sidecar``; this reader
is paired with it (Sprint 10 C3 / 09b L-002 template).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


def load_latest_clip_assignments(
    poi_slug: str,
    duration_sec: float,
    sidecar_search_dirs: Iterable[str | os.PathLike],
) -> list | None:
    """Return the most-recent-by-mtime variants-list payload from
    ``clip_assignments_{slug}_{round(duration_sec)}s.json`` across
    ``sidecar_search_dirs``, or ``None`` if nothing matches or every
    candidate payload is malformed.

    Sprint 10 C3 (F1 invariant): the writer in ``compile_promo.py`` and
    this reader are paired. Collision-bumped suffixes (``-2.json``,
    ``-3.json``, ...) produced by ``_write_sidecar`` are covered by the
    glob so a second run in the same output dir doesn't leave the reader
    pointing at stale data. Directly mirrors ``pause_budget.load_calibrated_wpm``
    by design — that 09b C8 fix is the template (see Sprint 09b L-002).

    Sprint 13 AC19 (D-004): the sidecar schema extended from a bare
    variants list to a top-level dict with retrieval provenance
    (``retrieval_active`` / ``embedded_pool_size`` / ``reduced_pool_size``
    / ``mimo_prompt_sha1`` / ``fallback_reason``) plus a ``variants``
    key. This reader tolerates both shapes — old Sprint 12b sidecars
    (bare list) and new Sprint 13 sidecars (dict with ``variants``) — so
    fixture-replay tests against committed 12b sidecars remain green.
    The reader always returns the variants list; the provenance fields
    are accessed via a separate reader if a caller needs them.

    Malformed sidecars (missing keys, unreadable JSON, unexpected shape)
    are skipped silently so one bad file in a search dir does not mask a
    good one; the reader returns the first valid payload under mtime
    ordering.
    """
    target_dur = int(round(duration_sec))
    target_stem = f"clip_assignments_{poi_slug}_{target_dur}s"
    match_exact = f"{target_stem}.json"
    match_bumped = f"{target_stem}-*.json"

    candidates: list[tuple[float, Path]] = []
    for d in sidecar_search_dirs:
        if d is None:
            continue
        base = Path(d)
        if not base.exists() or not base.is_dir():
            continue
        matched: list[Path] = []
        exact = base / match_exact
        if exact.exists():
            matched.append(exact)
        matched.extend(base.glob(match_bumped))
        for path in matched:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, path))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    for _mtime, path in candidates:
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            continue
        # Sprint 13 AC19: unwrap the new shape into the variants list.
        # Old shape (Sprint 12b and earlier): bare `[...]`.
        # New shape (Sprint 13+): `{"variants": [...], "retrieval_active": ...}`.
        if isinstance(payload, dict):
            variants = payload.get("variants")
        elif isinstance(payload, list):
            variants = payload
        else:
            continue
        if not isinstance(variants, list):
            continue
        # Light schema validation — the variants list holds per-variant
        # entries; each must be a dict with an 'assignments' list inside.
        # If a sidecar was written by an older schema or got corrupted,
        # skip it and try the next candidate.
        if not all(
            isinstance(entry, dict) and isinstance(entry.get("assignments"), list)
            for entry in variants
        ):
            continue
        return variants

    return None
