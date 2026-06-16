# PROVENANCE: VERBATIM VENDORED COPY.
# Source of truth: asset_platform/poi_assets/recipe_fingerprint.py (AIGC platform).
# This file MUST NOT be hand-edited — it is a byte-faithful copy of the upstream
# logic so that PGC and the platform produce identical recipe fingerprints. The
# golden-vector parity test (promo/tests/.../test_recipe_fingerprint.py against
# fixtures/recipe_fingerprint_golden.json) is the drift guard: if upstream
# changes, re-vendor the whole file and re-pin the golden vectors — never patch
# a function body here.
"""Content-recipe fingerprint for release-candidate dedup (cross-paradigm).

The fingerprint is a stable hash of a rendered video's RECIPE — the ordered
list of source-clip CONTENT it shows — used to detect the same visual content
being produced/distributed twice (the double-publish failure).

Anchored on ``source_content_hash`` — NOT ``asset_id`` or ``clip_id``:
``asset_id`` varies by ingest ``run_id`` / ingest path / upscale-replacement
(three different generators), and ``clip_id`` is a per-run sequence number
(``0001``…). ``source_content_hash`` is the only content-stable identity and
the common ingredient of every ``asset_id`` generator.

Two things are intentionally EXCLUDED from the recipe:
  * Music — same picture + different music is still a duplicate (the
    Margaritaville failure mode).
  * Trim / segment — a clip reused at a different in-point is the same footage;
    dropping trim also removes all floating-point bucketing, so the fingerprint
    is a pure string hash that cannot diverge across languages.

This module is the OFFLINE ORACLE. At runtime the canonical fingerprint is
derived by a ``BEFORE INSERT`` trigger on ``release_candidates`` from the
``recipe_input`` each paradigm supplies (see ``asset_platform/CONTRACT.md``
§2.3). The trigger and this module MUST produce identical fingerprints for the
same ordered ``source_content_hash`` list — both ``sha256`` the same
``"|"``-joined string — and both are pinned by the golden vectors in
``tests/fixtures/recipe_fingerprint_golden.json``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

#: Bump when the canonicalization changes (old fingerprints become non-comparable).
#: rfp2 = sha256 of the ordered source_content_hash list (no trim, no music).
FINGERPRINT_VERSION = "rfp2"


def recipe_fingerprint(ordered_content_hashes: Sequence[str]) -> str:
    """Return the canonical recipe fingerprint for an ordered content-hash list.

    ``ordered_content_hashes``: the video's ``source_content_hash`` values in
    play (occurrence) order — order is significant (a reordered video is a
    different video). Music and trim are already excluded by construction.

    Fail-loud (``ValueError``) on an empty list or any empty/missing hash —
    never silently drops an entry (that would let distinct content collide).
    """
    if not ordered_content_hashes:
        raise ValueError("recipe_fingerprint: ordered_content_hashes is empty")

    parts: list[str] = []
    for i, content_hash in enumerate(ordered_content_hashes):
        if not content_hash:
            raise ValueError(
                f"recipe_fingerprint: empty source_content_hash at position {i} "
                "(refusing to drop it — would let distinct content collide)"
            )
        parts.append(str(content_hash))

    # Parts are joined by "|". This is unambiguous because a content_hash is a
    # fixed "sha256:<64 hex>" token (contains no "|"). Cross-impl implementers
    # (the SQL trigger, PGC) MUST keep this property — if a future content_hash
    # format could contain "|", switch to a length-prefixed/JSON canonical form,
    # or distinct content could collide to one fingerprint (false positive).
    canonical = "|".join(parts)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_VERSION}:{digest}"


def recipe_input_from_render_manifest(
    render_manifest: Mapping,
    *,
    variant_index: int | None = None,
) -> list[str]:
    """Build the ordered ``source_content_hash`` list (the stored ``recipe_input``).

    Works on the shared shape used by music_remix render plans and PGC
    ``run_manifest`` files: a list ``asset_snapshot[]`` (each with ``asset_id``
    and ``source_content_hash``) plus ``timeline_entries[]`` (each with
    ``asset_id``, ``occurrence_index``, ``variant_index``). ``trim_start_sec`` is
    ignored on purpose (trim is excluded from the recipe).

    A music_remix render plan holds a single variant; pass ``variant_index`` to
    select one from a multi-variant manifest, else a multi-variant manifest is a
    fail-loud error. Fail-loud on a missing content hash — never falls back to
    ``asset_id`` (that would reintroduce the instability the anchor avoids).
    """
    snapshot = render_manifest.get("asset_snapshot") or []
    content_hash_by_asset_id: dict[str, str] = {}
    for asset in snapshot:
        asset_id = asset.get("asset_id")
        content_hash = asset.get("source_content_hash")
        if asset_id and content_hash:
            content_hash_by_asset_id[str(asset_id)] = str(content_hash)

    entries = list(render_manifest.get("timeline_entries") or [])
    variants = {entry.get("variant_index") for entry in entries}
    if variant_index is None:
        if len(variants) > 1:
            raise ValueError(
                "recipe_input_from_render_manifest: manifest spans variants "
                f"{sorted(v for v in variants if v is not None)}; pass variant_index"
            )
    else:
        entries = [entry for entry in entries if entry.get("variant_index") == variant_index]

    if not entries:
        raise ValueError("recipe_input_from_render_manifest: no timeline entries")

    entries.sort(key=lambda entry: entry["occurrence_index"])
    ordered: list[str] = []
    for entry in entries:
        asset_id = str(entry["asset_id"])
        content_hash = content_hash_by_asset_id.get(asset_id)
        if not content_hash:
            raise ValueError(
                f"recipe_input_from_render_manifest: no source_content_hash for "
                f"asset_id={asset_id!r} (refusing to fall back to asset_id — "
                "would break content stability)"
            )
        ordered.append(content_hash)
    return ordered


def recipe_fingerprint_from_render_manifest(
    render_manifest: Mapping,
    *,
    variant_index: int | None = None,
) -> str:
    """Convenience: fingerprint of the manifest's ordered content-hash list."""
    return recipe_fingerprint(
        recipe_input_from_render_manifest(render_manifest, variant_index=variant_index)
    )
