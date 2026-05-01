"""Sprint 12a harness — populate the embedding index for one POI.

Usage::

    python3 -m promo.cli.build_embedding_index --poi hotel-xcaret-arte

Reads clips from ``material/<slug>/clips/``, pulls MiMo analyses from
``material/<slug>/.mimo_cache/`` (cache-only by default — raises if any
clip is un-analyzed), and writes the embedding sidecar to
``material/<slug>/.embedding_cache/text-embedding-3-small-1536-<sha1>.json``.

Exits 0 on success. Prints a summary line matching ``AC8`` exactly:
``POI=<slug> clips_embedded=<N> cache_hits=<N> incremental=<N> mimo_prompt_sha1=<8-hex>``.
"""
# dev utility

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys
from pathlib import Path

# Repo root on sys.path for ``promo.core`` / ``common`` imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv  # noqa: E402

from promo.core import material_poi_slug  # noqa: E402
from promo.core.analyze import clip_analyzer  # noqa: E402
from promo.core.assign import clip_embedder  # noqa: E402

logger = logging.getLogger("build_embedding_index")

_CLIP_ID_PATTERN = re.compile(r"clip[_\-]?(\d{4})")
_CLIP_ID_FALLBACK = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _extract_clip_id(filename: str) -> str | None:
    m = _CLIP_ID_PATTERN.search(filename)
    if m:
        return m.group(1)
    matches = _CLIP_ID_FALLBACK.findall(filename)
    return matches[-1] if matches else None


def _validate_slug(slug: str) -> None:
    """Reject slugs that would escape or rewrite the material root.

    Sprint 12a audit finding L-5. This function is the single source of truth
    for what constitutes a safe material slug. Sprint 12b library callers
    invoke ``build_index_for_poi`` directly — the CLI-level guard in
    ``main()`` does not cover that path, so the check lives here. Mirrors
    the material-slug convention pinned in ``architecture.md`` "Pool
    conventions": lowercase + hyphens, single path component, no traversal.
    """
    if not slug:
        raise ValueError("POI slug is empty")
    if "/" in slug or "\\" in slug:
        raise ValueError(
            f"Invalid POI slug {slug!r}: must not contain path separators. "
            f"Material pools use hyphenated slugs, e.g. 'hotel-xcaret-arte'."
        )
    if ".." in slug:
        raise ValueError(
            f"Invalid POI slug {slug!r}: must not contain '..' (path traversal)."
        )
    if slug.startswith("."):
        raise ValueError(
            f"Invalid POI slug {slug!r}: must not start with '.' (hidden path)."
        )


def _collect_clip_paths(clips_dir: str) -> dict[str, str]:
    """Glob the POI's clips dir, map clip_id → absolute path.

    Files without an extractable 4-digit clip_id are silently skipped
    (non-clip files get mixed in, and the skip is the right behavior).
    Files WITH a clip_id that collides with an already-mapped clip emit a
    warning and are skipped — matches ``backend.py:268-273`` precedent.
    Sprint 12a audit finding L-3.
    """
    clip_paths: dict[str, str] = {}
    for pattern in ("*.mp4", "*.MP4"):
        for filepath in sorted(glob.glob(os.path.join(clips_dir, pattern))):
            filename = os.path.basename(filepath)
            cid = _extract_clip_id(filename)
            if not cid:
                continue
            if cid in clip_paths:
                logger.warning(
                    "Clip ID collision: '%s' already mapped to %s, skipping %s",
                    cid, os.path.basename(clip_paths[cid]), filename,
                )
                continue
            clip_paths[cid] = filepath
    return clip_paths


def build_index_for_poi(
    slug: str,
    *,
    material_root: str = "material",
) -> dict:
    """Populate the embedding sidecar for one material-slug POI.

    Returns the ``embed_clips_for_poi`` result dict (includes ``stats`` +
    ``sidecar_path``). Raises on invalid slug, missing MiMo cache, missing
    clips, or OpenAI API failure.
    """
    _validate_slug(slug)
    poi_dir = os.path.join(material_root, slug)
    clips_dir = os.path.join(poi_dir, "clips")
    mimo_cache_dir = os.path.join(poi_dir, ".mimo_cache")
    embed_cache_dir = os.path.join(poi_dir, clip_embedder.CACHE_DIR_NAME)

    if not os.path.isdir(clips_dir):
        raise FileNotFoundError(
            f"Clips directory not found: {clips_dir}. "
            f"Hint: material pools use hyphenated slugs (e.g. 'hotel-xcaret-arte'). "
            f"If you typed a display name or an underscore-form slug, try the "
            f"hyphenated material-directory name."
        )
    if not os.path.isdir(mimo_cache_dir):
        raise FileNotFoundError(
            f"MiMo cache directory not found: {mimo_cache_dir}. "
            "Run compile_promo or clip_analyzer against this POI first."
        )

    clip_paths = _collect_clip_paths(clips_dir)
    if not clip_paths:
        raise RuntimeError(f"No .mp4 clips discovered under {clips_dir}")

    logger.info("POI=%s clips_found=%d (mimo_cache=%s)", slug, len(clip_paths), mimo_cache_dir)

    # Sprint 12a runs on already-analyzed material pools — analyze_clips
    # consults the .mimo_cache sidecars and makes zero OpenRouter calls
    # when every clip is already cached. Any cache miss surfaces as a
    # real MiMo call, which is the right behavior for a brand-new POI.
    analyses = clip_analyzer.analyze_clips(
        clip_paths, cache_dir=mimo_cache_dir,
    )

    result = clip_embedder.embed_clips_for_poi(
        analyses, cache_dir=embed_cache_dir,
    )

    stats = result["stats"]
    summary = (
        f"POI={slug} "
        f"clips_embedded={stats['clips_embedded']} "
        f"cache_hits={stats['cache_hits']} "
        f"incremental={stats['incremental']} "
        f"mimo_prompt_sha1={result['mimo_prompt_sha1']}"
    )
    print(summary)
    logger.info("sidecar=%s embeddings=%d", result["sidecar_path"], len(result["embeddings"]))
    return result


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from promo.core.logging_config import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(
        description="Build the embedding sidecar for one POI's clip pool.",
    )
    parser.add_argument(
        "--poi",
        required=True,
        help="Material slug (hyphenated), e.g. hotel-xcaret-arte. "
             "Must match an existing material/<slug>/ directory.",
    )
    parser.add_argument(
        "--material-root",
        default="material",
        help="Root directory for material pools (default: material).",
    )
    args = parser.parse_args(argv)

    # NW5 discipline: if a caller ever passes a display name here, route it
    # to the canonical hyphenated material-dir slug before touching disk.
    # Existing callers that already pass a slug keep their exact value.
    slug = args.poi
    if any(ch.isspace() or ch.isupper() for ch in slug):
        original = slug
        slug = material_poi_slug(slug)
        logger.info("Normalized --poi %r → %r (material_poi_slug)", original, slug)

    try:
        build_index_for_poi(slug, material_root=args.material_root)
    except Exception as exc:
        logger.error("Failed to build embedding index for %s: %s", slug, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
