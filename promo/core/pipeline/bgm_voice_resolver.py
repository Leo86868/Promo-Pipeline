"""Pipeline helpers: BGM discovery / selection, voice-key rotation,
per-variant output-path derivation, and the empty retrieval-provenance
initializer shared between ``_step_assign_clips`` (per-variant) and
``full_pipeline`` (run-level).

Extracted from ``promo/cli/compile_promo.py`` lines 86-245 in
promo-handoff-readiness Sprint 4 A-001 (narrow — ``compile_promo.py``
decomposition). Behavior byte-identical to the pre-extraction site.
"""

import logging
import os
import re

from promo.core.backend import PromoBackend
from promo.core.errors import NoSuitableBGMError
from promo.core.render.remotion_renderer import REMOTION_DIR

logger = logging.getLogger(__name__)


_TRAILING_DUR_RE = re.compile(r"_\d+s$")


def _variant_output_path(
    base_output_path: str,
    variant_index: int,
    n_variants: int,
    variant_target_duration_sec: float | None = None,
) -> str:
    """Derive a per-variant output path when rendering multiple outputs.

    When ``variant_target_duration_sec`` is supplied, any trailing
    ``_<N>s`` segment baked into ``base_output_path`` (the run-level
    ``--target-duration-sec`` label) is stripped and replaced with the
    variant's own duration so a random-selector long variant does not
    ship as ``..._30s_v1.mp4``.
    """
    if n_variants <= 1 and variant_target_duration_sec is None:
        return base_output_path
    root, ext = os.path.splitext(base_output_path)
    ext = ext or ".mp4"
    if variant_target_duration_sec is not None:
        root = _TRAILING_DUR_RE.sub("", root)
        dur_label = f"{int(round(variant_target_duration_sec))}s"
        if n_variants <= 1:
            return f"{root}_{dur_label}{ext}"
        return f"{root}_v{variant_index}_{dur_label}{ext}"
    return f"{root}_v{variant_index}{ext}"


def _discover_bgm_files(
    bgm_dir: str | None = None,
    *,
    min_duration_sec: float | None = None,
) -> list[str]:
    """Discover BGM .mp3 files from a directory.

    Default: promo/remotion/public/. Returns sorted list of absolute paths.

    Sprint 08.5: when ``min_duration_sec`` is given, any track shorter than
    that cutoff is filtered out (operator requirement — BGM that ends before
    the video leaves a silent tail). Set to ``None`` to skip the filter.

    Sprint 09a M-005: when ``min_duration_sec`` is set and no track in the
    directory meets the minimum duration, this function RAISES
    ``NoSuitableBGMError`` instead of silently falling back to the
    unfiltered pool. Callers must catch this explicitly (see ``main()``
    and ``full_pipeline`` fallback for examples).
    """
    import glob as globmod
    if bgm_dir is None:
        bgm_dir = os.path.join(REMOTION_DIR, "public")
    patterns = [os.path.join(bgm_dir, "*.mp3"), os.path.join(bgm_dir, "*.MP3")]
    files: list[str] = []
    for pat in patterns:
        files.extend(globmod.glob(pat))
    # Deduplicate and sort for deterministic ordering.
    unique = sorted(set(os.path.abspath(f) for f in files))

    if min_duration_sec is None or min_duration_sec <= 0:
        return unique

    from promo.core.render.remotion_renderer import get_clip_duration
    filtered: list[str] = []
    for path in unique:
        try:
            dur = float(get_clip_duration(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("BGM ffprobe failed for %s: %s", path, exc)
            continue
        if dur >= min_duration_sec:
            filtered.append(path)
        else:
            logger.info(
                "BGM filtered: %s is %.1fs (below %.1fs min) — skipped",
                os.path.basename(path), dur, min_duration_sec,
            )
    if not filtered:
        # Sprint 09a M-005: the pre-09a silent fallback to the unfiltered
        # pool defeated the filter — the short BGM the filter rejected
        # would still get picked and leave a silent tail. Raising forces
        # the caller to handle the condition (return False, prompt for
        # longer BGM, etc.) rather than quietly rendering a broken video.
        raise NoSuitableBGMError(
            f"No BGM track in {bgm_dir} meets the minimum duration "
            f"of {min_duration_sec:.1f}s; checked {len(unique)} file(s). "
            f"Provide a longer track via --bgm / --bgm-paths or lower "
            f"the target duration."
        )
    return filtered


def _resolve_bgm_paths(
    *,
    bgm_paths: list[str] | None,
    bgm_path: str | None,
    poi_name: str,
    backend: PromoBackend,
    tmp_dir: str,
    target_duration_sec: float,
    count: int = 1,
) -> list[str]:
    """Resolve the final BGM path list for a promo run.

    Sprint 09b C6 extraction from ``full_pipeline``'s Step 6. Priority
    order (unchanged):
    1. Explicit ``bgm_paths`` (from CLI --bgm-dir rotation) — use verbatim.
    2. Explicit ``bgm_path`` (from CLI --bgm) — single-track list.
    3. Backend-supplied BGM list (``backend.fetch_bgms``) when available.
    4. Backend-supplied BGM (``backend.fetch_bgm``).
    5. Fallback to default ``promo/remotion/public/*.mp3`` filtered by
       ``target_duration_sec`` (Sprint 08.5 filter).

    Raises NoSuitableBGMError if the fallback discovery step runs and
    no track meets the minimum duration. Caller converts to a
    ``full_pipeline`` False return.
    """
    if bgm_paths and len(bgm_paths) > 0:
        return list(bgm_paths)
    if bgm_path is not None:
        return [bgm_path]
    fetch_bgms = (
        getattr(backend, "fetch_bgms", None)
        if "fetch_bgms" in type(backend).__dict__
        else None
    )
    if callable(fetch_bgms):
        fetched_many = fetch_bgms(poi_name, tmp_dir, count=count)
        if fetched_many:
            return list(fetched_many)
    fetched = backend.fetch_bgm(poi_name, tmp_dir)
    if fetched:
        return [fetched]
    # Sprint 09a M-005: the filter raises NoSuitableBGMError instead of
    # silently falling back to the unfiltered pool. Let it propagate;
    # full_pipeline catches and returns False.
    return _discover_bgm_files(min_duration_sec=float(target_duration_sec))


def _resolve_voice_keys(voice_key: str | None) -> list[str]:
    """Resolve the voice_key rotation list for a promo run.

    Sprint 09b C6 extraction from ``full_pipeline``'s Step 6. When
    ``voice_key`` is set explicitly (CLI --voice), all variants use it;
    otherwise voices rotate through ``VOICE_CATALOG`` in declared order
    (AC14 from Sprint 07).

    Raises ValueError if ``voice_key`` is set to a key not present in
    ``VOICE_CATALOG``. Caller converts to a ``full_pipeline`` False
    return.
    """
    # Delayed import preserves the pre-09b lazy-load pattern — tests
    # that stub the TTS layer don't pay the import cost.
    from promo.core.narrate.tts_engine import VOICE_CATALOG

    catalog_keys = list(VOICE_CATALOG.keys())
    if voice_key:
        if voice_key not in VOICE_CATALOG:
            raise ValueError(
                f"Unknown --voice '{voice_key}'. "
                f"Catalog keys: {catalog_keys}"
            )
        return [voice_key]
    return catalog_keys


def _empty_retrieval_provenance() -> dict:
    """Sprint 13 AC19 default retrieval provenance ("retrieval inactive").

    Consumed by both ``_step_assign_clips`` (per-variant init) and
    ``full_pipeline`` (run-level accumulator) so the two stay in lockstep.

    Sprint 18 F: ``retrieval_contract`` is seeded here so every emitted
    ``clip_assignments_*.json`` sidecar carries the explicit "soft_hint"
    declaration. The literal documents that retrieval is advisory — the
    assigner does NOT reject assignments that name a ``clip_id``
    outside the retrieved subset, and the four ``fallback_reason`` codes
    (``no_sidecar`` / ``m4_attach_shrinkage`` / ``h2_union_shortfall`` /
    ``retrieval_exception``) encode the cases where retrieval did not
    even produce a hint. See ``docs/schemas/clip_assignments.md`` for
    the field-level prose and ``architecture.md`` "Sprint 12b retrieval
    soft hint" addendum for the full design intent.
    """
    return {
        "retrieval_active": False,
        "embedded_pool_size": 0,
        "reduced_pool_size": 0,
        "mimo_prompt_sha1": None,
        "fallback_reason": None,
        "retrieval_contract": "soft_hint",
    }
