"""Single typed resolver for env vars (Pluggability Charter Rule 2).

The single place production pipeline code outside ``promo/core/llm/``
reads env vars. ``load_dotenv()`` runs at most once, guarded by the
module-level ``_DOTENV_LOADED`` flag. Required values raise
``ConfigError`` (subclass of ``RuntimeError``); optional values return
sensible defaults.
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv

from promo.core.model_adapters.registry import MIMO_CLIP_MODEL


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid."""


_DOTENV_LOADED = False


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
#  Pipeline env-var resolvers
# ------------------------------------------------------------------ #
#  Consumed by clip_embedder, clip_analyzer,
#  script_generator, tts_engine (Gemini + ElevenLabs paths). `_require`
#  strips whitespace so an accidental ``GEMINI_API_KEY=" "`` fails fast
#  instead of producing a silent 401 from the vendor.

def gemini_api_key() -> str:
    return _require("GEMINI_API_KEY")


def openrouter_api_key() -> str:
    return _require("OPENROUTER_API_KEY")


def elevenlabs_api_key() -> str:
    return _require("ELEVENLABS_API_KEY")


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

    Default 6 — the 2026-06-14 P4-health smoke ran at 2 on an 8-core VPS and
    left ~6 cores idle (render-bound, sequential ~7min/video). 6 uses the box
    while leaving headroom for the OS + tail workers. Low-core machines can
    lower it via ``PROMO_RENDER_CONCURRENCY``.
    """
    return _require_int("PROMO_RENDER_CONCURRENCY", default=6)


def render_timeout_sec() -> int:
    """Maximum seconds allowed for one Remotion render process."""
    return _require_int("PROMO_RENDER_TIMEOUT_SEC", default=900)


def render_x264_preset() -> str:
    """x264 preset for Remotion's final MP4 encode."""
    value = os.getenv("PROMO_RENDER_X264_PRESET", "veryfast").strip() or "veryfast"
    allowed = {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
    if value not in allowed:
        raise ConfigError(
            "PROMO_RENDER_X264_PRESET must be one of: "
            + ", ".join(sorted(allowed))
        )
    return value


def render_crf() -> int:
    """CRF value for Remotion's final MP4 encode."""
    value = _require_int("PROMO_RENDER_CRF", default=23)
    if value < 0 or value > 51:
        raise ConfigError("PROMO_RENDER_CRF must be between 0 and 51")
    return value


def near_dup_threshold() -> Optional[float]:
    """EXPERIMENTAL near-dup soft-gate threshold (default None = OFF).

    VISUAL-modality gate: the packer compares the whitened DINOv2
    ``visual_embedding`` (not the text embedding). Recommended armed value
    0.85. Render-path only (consumed in the packer step); never touches the
    autopilot registration tail. Set via ``PROMO_NEAR_DUP_THRESHOLD`` or
    ``compile_promo --near-dup-threshold``. Unset → gate off → packer
    behaviour byte-identical to today.
    """
    _ensure_loaded()
    raw = os.getenv("PROMO_NEAR_DUP_THRESHOLD", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"PROMO_NEAR_DUP_THRESHOLD must be a float, got {raw!r}"
        ) from exc
    if not 0.0 < value <= 1.0:
        raise ConfigError("PROMO_NEAR_DUP_THRESHOLD must be in (0, 1]")
    return value


def download_diversity_enabled() -> bool:
    """工单② — when True, the ~30-clip download pool is chosen by
    relevance-seeded visual max-min (window 45, near-dup 0.85) instead of
    pure top-relevance. Download COUNT is unchanged → no extra egress; only
    WHICH ~30 changes. Default False = selection byte-identical to today.
    Set via ``PROMO_DOWNLOAD_DIVERSITY`` (1/true/yes/on). Render-path knob;
    never touches release_candidates/usage. NOT auto-armed — arming waits on
    a render before/after.
    """
    _ensure_loaded()
    raw = os.getenv("PROMO_DOWNLOAD_DIVERSITY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def global_assignment_enabled() -> bool:
    """EXPERIMENTAL packer consolidation (default False = OFF → greedy path,
    byte-identical to today).

    When True, the packer swaps its greedy first-fit relax-ladder for ONE global
    optimal clip↔beat assignment (Hungarian solve over a [beats × clips] cost
    matrix; adjacency-variety and near-dup folded in as soft penalties). Fixes
    the greedy stranding where an earlier beat spends a clip a later beat needed
    more. Render-path knob (consumed in the packer step); never touches the
    autopilot registration tail. Set via ``PROMO_GLOBAL_ASSIGNMENT``
    (1/true/yes/on) or ``compile_promo --global-assignment``. NOT auto-armed —
    arming waits on a render before/after, same gate as the dedup rollout.
    """
    _ensure_loaded()
    raw = os.getenv("PROMO_GLOBAL_ASSIGNMENT", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def clip_model() -> str:
    """MiMo V2 Omni model id for clip analysis via OpenRouter.

    Default hardcoded to the current production model string rather than
    imported from `clip_analyzer.DEFAULT_MODEL` to avoid a `config` →
    `clip_analyzer` dependency inversion. Kept in sync manually; the
    canonical value lives here.
    """
    return os.getenv("PROMO_CLIP_MODEL", MIMO_CLIP_MODEL)


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


def google_credentials_file() -> str:
    """OAuth client secret JSON used for Google Drive uploads.

    PGC intentionally shares AIGC's OAuth-style Drive auth for now:
    ``client_secret.json`` plus a sibling ``token.pickle`` unless
    ``PGC_GOOGLE_TOKEN_FILE`` is set.
    """
    return _require("GOOGLE_CREDENTIALS_FILE")


def pgc_google_token_file() -> str:
    return os.getenv("PGC_GOOGLE_TOKEN_FILE", "").strip()


def pgc_drive_parent_folder_id() -> str:
    return os.getenv("PGC_DRIVE_PARENT_FOLDER_ID", "").strip()


def pgc_drive_parent_folder_name() -> str:
    return os.getenv("PGC_DRIVE_PARENT_FOLDER_NAME", "").strip() or "AIGC Production Masters"


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


def replay_script_path() -> Optional[str]:
    """Path to a recorded script for replay (翻转二 B6 same-script A/B).

    When set, ``_step_generate_script`` skips Gemini #1 and replays the
    recorded script (a ``clip_assignments_*.json`` sidecar or a bare
    ``{"segments": [...]}`` JSON). The A/B-era companion switch
    ``PROMO_CLIP_ASSIGNER`` retired with the legacy LLM-assigner chain (1f28902, 2026-06-11).
    """
    value = os.getenv("PROMO_REPLAY_SCRIPT", "").strip()
    return value or None
