"""Pipeline sidecar writers.

Emits the three per-run sidecar JSON files (tts_metrics,
match_quality, clip_assignments) with collision-bump safety.

Extracted from ``promo/cli/compile_promo.py`` (lines 246-351 +
966-1015) in promo-handoff-readiness Sprint 4 A-001.
``_emit_run_sidecars`` was added by promo-foundation Sprint 16 and
moved here for the Sprint 4 narrow decomposition.
"""

import json
import logging
import os
from dataclasses import dataclass

from promo.core import sanitize_poi_name as _safe_poi_dir
from promo.core.backend import PromoBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SidecarWriteResult:
    """Structured result for one sidecar write."""

    ok: bool
    path: str | None
    description: str
    base_name: str

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class RunSidecarEmitResult:
    """Structured result for one run's sidecar emission."""

    ok: bool
    sidecar_dir: str | None
    writes: tuple[SidecarWriteResult, ...]

    def __bool__(self) -> bool:
        return self.ok

    @property
    def paths(self) -> dict[str, str]:
        return {
            write.description: write.path
            for write in self.writes
            if write.ok and write.path is not None
        }


def _write_sidecar(
    sidecar_dir: str | None,
    base_name: str,
    payload,
    description: str,
) -> bool:
    """Write a sidecar JSON file under ``sidecar_dir`` with collision safety.

    Sprint 09b C2 (M-3 / DC-3). Consolidates the two per-run sidecar writes
    (``tts_metrics_*.json`` + ``match_quality_*.json``) that Sprint 09a
    introduced into a single helper.

    - ``base_name`` is the intended filename, e.g.
      ``tts_metrics_little_palm_island_resort_65s.json``.
    - If ``sidecar_dir`` is None or empty (Sprint 09a D-005 path: backend
      has no ``_output_dir`` AND ``output_path`` has no dirname), this logs
      a WARNING naming ``description`` and returns False so the caller can
      flip ``all_ok``.
    - If ``base_name`` already exists in ``sidecar_dir`` (Codex #3 —
      same-POI same-duration collision from back-to-back reruns), the
      helper appends ``-2``, ``-3``, ... until a clear slot is found so no
      approved-deliverable sidecar is silently clobbered.
    - Any OSError during the write is logged and returns False so the
      caller flips ``all_ok`` (Sprint 09b C2 M-1/D-004).

    Returns True on successful write, False on any failure path.
    """
    return _write_sidecar_result(
        sidecar_dir, base_name, payload, description,
    ).ok


def _write_sidecar_result(
    sidecar_dir: str | None,
    base_name: str,
    payload,
    description: str,
) -> SidecarWriteResult:
    """Write one sidecar and return the exact path on success.

    ``_write_sidecar`` remains the bool-compatible wrapper used by older
    callers and tests. This helper is the manifest-ready surface.
    """
    if not isinstance(sidecar_dir, str) or not sidecar_dir:
        logger.warning(
            "Skipping %s sidecar: sidecar_dir unresolved "
            "(backend._output_dir is None/empty and output_path has no dirname). "
            "Marking run as not-all-ok so the failure surfaces.",
            description,
        )
        return SidecarWriteResult(
            ok=False, path=None, description=description, base_name=base_name,
        )

    if not base_name.endswith(".json"):
        raise ValueError(
            f"_write_sidecar base_name must end in .json, got {base_name!r}"
        )

    stem = base_name[: -len(".json")]
    candidate = os.path.join(sidecar_dir, base_name)
    bump = 2
    while os.path.exists(candidate):
        candidate = os.path.join(sidecar_dir, f"{stem}-{bump}.json")
        bump += 1
        if bump > 999:
            logger.warning(
                "Giving up on %s sidecar collision bump after 999 attempts "
                "in %s; marking run as not-all-ok.",
                description, sidecar_dir,
            )
            return SidecarWriteResult(
                ok=False, path=None, description=description, base_name=base_name,
            )
    if candidate != os.path.join(sidecar_dir, base_name):
        logger.info(
            "Sidecar %s already exists; bumping to %s to preserve prior deliverable.",
            base_name, os.path.basename(candidate),
        )

    try:
        with open(candidate, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        # Sprint 13 post-audit D-005: a failure mid-json.dump leaves a
        # truncated/empty file at `candidate`. Clean it up so the next
        # collision-bump sequence doesn't skip past an orphan.
        logger.warning("Failed to emit %s: %s", os.path.basename(candidate), exc)
        try:
            os.unlink(candidate)
        except OSError:
            pass
        return SidecarWriteResult(
            ok=False, path=None, description=description, base_name=base_name,
        )

    # Sprint 13 post-audit D-003: dict payloads (e.g. clip_assignments under
    # AC19) have a top-level shape `{provenance_fields..., "variants": [...]}`;
    # `len(dict)` returns key count (always 6 for clip_assignments), masking
    # the real variant count. Prefer `payload["variants"]` when present.
    if isinstance(payload, dict) and isinstance(payload.get("variants"), list):
        size = len(payload["variants"])
    elif hasattr(payload, "__len__"):
        size = len(payload)
    else:
        size = 0
    logger.info("Wrote %s (%d entries)", candidate, size)
    return SidecarWriteResult(
        ok=True, path=candidate, description=description, base_name=base_name,
    )


def _emit_run_sidecars(
    *,
    backend: PromoBackend,
    output_path: str,
    poi_name: str,
    target_duration_sec: float,
    tts_metrics: list[dict],
    match_quality_entries: list[dict],
    clip_assignments_entries: list[dict],
    run_retrieval_provenance: dict,
) -> bool:
    """Write the per-run tts_metrics / match_quality / clip_assignments
    sidecars (Sprint 09a M-004 + Sprint 09b C2 + Sprint 10 C3 / Sprint
    13 AC19). Returns ``True`` on clean writes, ``False`` if any write
    failed or the sidecar dir could not be resolved while there were
    rows to commit (D-005 tripwire).
    """
    sidecar_result = _emit_run_sidecars_result(
        backend=backend,
        output_path=output_path,
        poi_name=poi_name,
        target_duration_sec=target_duration_sec,
        tts_metrics=tts_metrics,
        match_quality_entries=match_quality_entries,
        clip_assignments_entries=clip_assignments_entries,
        run_retrieval_provenance=run_retrieval_provenance,
    )
    return sidecar_result.ok


def _emit_run_sidecars_result(
    *,
    backend: PromoBackend,
    output_path: str,
    poi_name: str,
    target_duration_sec: float,
    tts_metrics: list[dict],
    match_quality_entries: list[dict],
    clip_assignments_entries: list[dict],
    run_retrieval_provenance: dict,
) -> RunSidecarEmitResult:
    """Write run sidecars and return exact write paths.

    This is the structured-result sibling of ``_emit_run_sidecars``. It is
    intentionally additive so existing bool callers remain simple while a
    future manifest writer can reference the real collision-bumped files.
    """
    sidecar_dir: str | None = backend.output_dir() or os.path.dirname(output_path)
    if not isinstance(sidecar_dir, str) or not sidecar_dir:
        sidecar_dir = None
    if sidecar_dir:
        try:
            os.makedirs(sidecar_dir, exist_ok=True)
        except OSError as exc:
            logger.warning("Failed to create sidecar dir %s: %s", sidecar_dir, exc)
            sidecar_dir = None
    sidecar_tag = f"{_safe_poi_dir(poi_name)}_{int(round(target_duration_sec))}s"
    ok = True
    writes: list[SidecarWriteResult] = []

    if tts_metrics:
        result = _write_sidecar_result(
            sidecar_dir, f"tts_metrics_{sidecar_tag}.json",
            tts_metrics, "tts_metrics",
        )
        writes.append(result)
        if not result.ok:
            ok = False
    if match_quality_entries:
        result = _write_sidecar_result(
            sidecar_dir, f"match_quality_{sidecar_tag}.json",
            match_quality_entries, "match_quality",
        )
        writes.append(result)
        if not result.ok:
            ok = False
    if clip_assignments_entries:
        result = _write_sidecar_result(
            sidecar_dir, f"clip_assignments_{sidecar_tag}.json",
            {**run_retrieval_provenance, "variants": clip_assignments_entries},
            "clip_assignments",
        )
        writes.append(result)
        if not result.ok:
            ok = False
    return RunSidecarEmitResult(
        ok=ok, sidecar_dir=sidecar_dir, writes=tuple(writes),
    )
