"""Single typed resolver for env vars (Pluggability Charter Rule 2).

The single place production pipeline code outside ``promo/core/llm/``
reads env vars. ``load_dotenv()`` runs at most once, guarded by the
module-level ``_DOTENV_LOADED`` flag. Required values raise
``ConfigError`` (subclass of ``RuntimeError``); optional values return
sensible defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid."""


_DOTENV_LOADED = False
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_loaded() -> None:
    global _DOTENV_LOADED
    if not _DOTENV_LOADED:
        load_dotenv()
        _DOTENV_LOADED = True


_ensure_loaded()


def _require(name: str) -> str:
    # Audit L-004: strip + non-empty check so whitespace-only values
    # (e.g. KIE_API_KEY=" ") fail fast at startup rather than producing
    # a silent 401 from the vendor API.
    raw = os.getenv(name)
    value = raw.strip() if raw is not None else None
    if not value:
        raise ConfigError(
            f"{name} is required but not set. "
            f"Add it to .env or export it in the shell."
        )
    return value


def _require_int(name: str, default: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if default is None:
            raise ConfigError(f"{name} is required but not set.")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _require_float(name: str, default: Optional[float] = None) -> float:
    """Float twin of `_require_int`; added in promo-handoff-readiness
    Sprint 1 AC-B1 to back the `default_duration_sec()` resolver.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        if default is None:
            raise ConfigError(f"{name} is required but not set.")
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc


# ------------------------------------------------------------------ #
#  KIE API
# ------------------------------------------------------------------ #

def kie_api_key() -> str:
    return _require("KIE_API_KEY")


def kie_base_url() -> str:
    return os.getenv("KIE_BASE_URL", "https://api.kie.ai/api/v1")


def kie_video_model() -> str:
    return os.getenv("KIE_VIDEO_MODEL", "bytedance/seedance-1.5-pro")


# ------------------------------------------------------------------ #
#  SeeDream
# ------------------------------------------------------------------ #

def seedream_aspect_ratio() -> str:
    return os.getenv("SEEDREAM_ASPECT_RATIO", "9:16")


def seedream_quality() -> str:
    return os.getenv("SEEDREAM_QUALITY", "basic")


def seedream_poll_interval() -> int:
    return _require_int("SEEDREAM_POLL_INTERVAL", default=15)


def seedream_max_wait() -> int:
    return _require_int("SEEDREAM_MAX_WAIT", default=600)


# ------------------------------------------------------------------ #
#  Video (Seedance)
# ------------------------------------------------------------------ #

def video_model() -> str:
    return os.getenv("VIDEO_MODEL", kie_video_model())


def video_aspect_ratio() -> str:
    return os.getenv("VIDEO_ASPECT_RATIO", "9:16")


def video_resolution() -> str:
    return os.getenv("VIDEO_RESOLUTION", "720p")


def video_duration() -> str:
    return os.getenv("VIDEO_DURATION", "8")


def video_poll_interval() -> int:
    return _require_int("VIDEO_POLL_INTERVAL", default=15)


def video_max_wait() -> int:
    return _require_int("VIDEO_MAX_WAIT", default=600)


# ------------------------------------------------------------------ #
#  Supabase (storage backend)
# ------------------------------------------------------------------ #

def supabase_url() -> str:
    return _require("SUPABASE_URL")


def supabase_key() -> str:
    return _require("SUPABASE_KEY")


def supabase_bucket() -> str:
    return os.getenv("PROMO_LAB_SUPABASE_BUCKET", "pipeline-media")


# ------------------------------------------------------------------ #
#  Pipeline env-var resolvers
# ------------------------------------------------------------------ #
#  Consumed by clip_assigner, clip_embedder, clip_analyzer,
#  script_generator, tts_engine (Gemini + ElevenLabs paths). `_require`
#  strips whitespace so an accidental ``GEMINI_API_KEY=" "`` fails fast
#  instead of producing a silent 401 from the vendor.

def gemini_api_key() -> str:
    return _require("GEMINI_API_KEY")


def openrouter_api_key() -> str:
    return _require("OPENROUTER_API_KEY")


def elevenlabs_api_key() -> str:
    return _require("ELEVENLABS_API_KEY")


# ------------------------------------------------------------------ #
#  Carry-over resolvers (no v0 callers — kept for forward compatibility)
# ------------------------------------------------------------------ #
#
# The resolvers below are not consumed by any v0 production module.
# They survived the extraction because keeping them costs nothing,
# is type-safe, and lets a future caller adopt them by setting the env
# var alone. Required values raise ``ConfigError``; optional values
# return sensible defaults.

def bb_daemon_url() -> str:
    """bb-browser daemon HTTP URL. Overridden per-run by the orchestrator
    via `bb_source.set_daemon_url(url)` after `bb_pool.acquire()`."""
    return os.getenv("BB_DAEMON_URL", "http://127.0.0.1:19824")


def bb_browser_port() -> int:
    """CDP port the bb-browser daemon's Chrome listens on (informational;
    the daemon HTTP port is derived from `BB_DAEMON_URL`)."""
    return _require_int("BB_BROWSER_PORT", default=9222)


def mimo_model() -> str:
    """MIMO-Omni model id on OpenRouter (SERP reasoning)."""
    return os.getenv("MIMO_MODEL", "xiaomi/mimo-v2-omni")


def openrouter_base_url() -> str:
    return os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def openrouter_http_referer() -> str:
    """HTTP-Referer header sent with OpenRouter requests.

    OpenRouter uses this string for attribution but does not validate it.
    Default is a neutral handle — operators may override via env var to
    point at their own fork or project URL.

    promo-handoff-readiness Sprint 1 AC-D1: replaced the hardcoded
    operator-handle literal that previously lived in `clip_analyzer.py`
    and `clip_embedder.py` as part of the operator-identity scrub.
    """
    return os.getenv("OPENROUTER_HTTP_REFERER", "https://github.com/anonymous/pgc-pipeline")


def gemini_scraping_model() -> str:
    """Gemini model id used by the scraping metadata resolvers.

    Distinct from `GEMINI_MODEL` consumed by compile_promo (which
    defaults to `gemini-2.5-pro`). Scraping favors the faster 2.0-flash
    for low-latency location / domain lookups.
    """
    return os.getenv("GEMINI_SCRAPING_MODEL", "gemini-2.0-flash")


def kp_all_tab_quota() -> int:
    """Max images to pull from the 'All' KP tab per POI."""
    return _require_int("KP_ALL_TAB_QUOTA", default=30)


def kp_other_tab_quota() -> int:
    """Max images to pull from non-'All' KP tabs (Rooms, Exterior, ...)."""
    return _require_int("KP_OTHER_TAB_QUOTA", default=10)


def kp_total_cap() -> int:
    """Hard cap on total KP images per POI across all tabs."""
    return _require_int("KP_TOTAL_CAP", default=200)


def google_images_max() -> int:
    return _require_int("GOOGLE_IMAGES_MAX", default=50)


def official_site_max() -> int:
    return _require_int("OFFICIAL_SITE_MAX", default=50)


def scrape_max_image_bytes() -> int:
    """Oversized-image byte cap; files above this are dropped post-download.

    Default: 6 MB. Prevents runaway download of mis-tagged hero PDFs or
    video posters that the source-side image extractor mis-classifies.
    """
    return _require_int("SCRAPE_MAX_IMAGE_BYTES", default=6_291_456)


def aigc_scrape_local_dir() -> str:
    """Root directory for scraped raw-image pools: `<dir>/<slug>/scraped/raw/`.

    Defaults to the checkout-local `material/` under the pgc-pipeline repo
    root, matching the existing per-POI layout convention. Explicit
    `AIGC_SCRAPE_LOCAL_DIR` overrides are respected as-is.
    """
    raw = os.getenv("AIGC_SCRAPE_LOCAL_DIR")
    if raw is not None and raw.strip():
        return raw
    return str(_REPO_ROOT / "material")


# ------------------------------------------------------------------ #
#  promo-handoff-readiness Sprint 1 AC-B1: production env var migrations
# ------------------------------------------------------------------ #
#
# Five resolvers added to satisfy Charter Rule 2 (Config resolver) —
# previously these env vars were read via direct `os.getenv` at the call
# site. Migration sites: `remotion_renderer.py:768`,
# `clip_analyzer.py:215` + `:292`, `clip_embedder.py:89`,
# `compile_promo.py:1292` + `:1298` + `:1304`.

def render_concurrency() -> int:
    """Number of parallel Chromium tabs Remotion uses per render.

    Default 2 matches the existing tuning (single-machine operator
    workflow; higher values oversubscribe CPU per the serialize-heavy
    pipelines convention).
    """
    return _require_int("PROMO_RENDER_CONCURRENCY", default=2)


def clip_model() -> str:
    """MiMo V2 Omni model id for clip analysis via OpenRouter.

    Default hardcoded to the current production model string rather than
    imported from `clip_analyzer.DEFAULT_MODEL` to avoid a `config` →
    `clip_analyzer` dependency inversion. Kept in sync manually; the
    canonical value lives here.
    """
    return os.getenv("PROMO_CLIP_MODEL", "xiaomi/mimo-v2-omni")


def default_duration_sec() -> float:
    """Default `--target-duration-sec` for `compile_promo.py`.

    CLI-override semantics preserved: flag beats env beats hardcoded
    default (30.0). See AC-B3 regression tests in
    `promo/tests/test_compile_promo.py::TestArgparsePrecedence`.
    """
    return _require_float("PROMO_DEFAULT_DURATION_SEC", default=30.0)


def default_variants() -> int:
    """Default `--n-variants` for `compile_promo.py` (see
    `default_duration_sec` for precedence notes).
    """
    return _require_int("PROMO_DEFAULT_VARIANTS", default=1)


def default_script_candidates() -> int:
    """Default `--n-script-candidates` for `compile_promo.py` (see
    `default_duration_sec` for precedence notes).
    """
    return _require_int("PROMO_DEFAULT_SCRIPT_CANDIDATES", default=1)


# ------------------------------------------------------------------ #
#  Sprint 16 — selector seam resolver
# ------------------------------------------------------------------ #

_ALLOWED_FORMAT_SELECTORS = ("single", "random")


def promo_format_selector() -> str:
    """Return the name of the ``FormatSelector`` implementation to
    instantiate inside ``compile_promo.full_pipeline``.

    Default ``"single"`` preserves the pre-Sprint-16 operator contract:
    ``--target-duration-sec X`` pins every variant to X seconds. Operators
    who want a mixed-duration variant pack opt in via
    ``PROMO_FORMAT_SELECTOR=random``; the per-variant filename / BGM
    filter / sidecar-tag caveats under random are documented in
    ``architecture.md`` "Selector seams (Sprint 16)". Sprint 17 may add an
    ``explicit`` variant (operator-supplied per-variant duration list)
    and a later sprint may land ``smart`` (clip-metadata + POI-aware).
    Unknown values raise :class:`ConfigError`.
    """
    value = os.getenv("PROMO_FORMAT_SELECTOR", "single").strip().lower()
    if value not in _ALLOWED_FORMAT_SELECTORS:
        raise ConfigError(
            f"PROMO_FORMAT_SELECTOR must be one of {_ALLOWED_FORMAT_SELECTORS}; "
            f"got {value!r}."
        )
    return value
