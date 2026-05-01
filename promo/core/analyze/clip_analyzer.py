"""Describe video clips with MiMo V2 Omni for script generation.

Sends 5-second video clips to MiMo V2 Omni via OpenRouter to get accurate
scene descriptions. The script generator uses these to write narration that
matches what the viewer actually sees.

No quality gating here — clip quality is a pipeline-level concern.

Sprint 08 adds a per-clip content-hash sidecar cache under
``<cache_dir>/<blake2b16-first-4MB>.json`` so repeat runs against the
same material skip the OpenRouter call. Writes are atomic (tmp →
os.replace) so concurrent ThreadPoolExecutor workers cannot corrupt the
cache.

Sprint 09b C3 (Codex #4): the cache key now mixes ``(prompt + model)``
into an 8-hex sha1 suffix, so any change to ``_ANALYSIS_PROMPT`` or the
model identifier invalidates the cache automatically. Without this,
prompt iterations (e.g., the Sprint 09b luxury-bias fix) would be
silently invisible on cached clips.

Usage:
    from promo.core.analyze.clip_analyzer import analyze_clips

    results = analyze_clips(clip_paths, cache_dir="material/hotel-xcaret-arte/.mimo_cache")
    # [{"id": "0086", "scene_description": "...", "category": "scenic", "camera_motion": "push_in"}, ...]
"""

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional


from promo.core import arsenal_loader
from promo.core.llm.retry import retry_with_backoff
from promo.core.errors import MimoAnalysisError

logger = logging.getLogger(__name__)

# Cache key is a blake2b digest (16 bytes / 32 hex) over the first
# CACHE_HASH_BYTES of the clip file, followed by an 8-hex sha1 suffix
# derived from (prompt + model). Clip content keys the base; the suffix
# version-locks the cache so prompt/model changes invalidate the entry.
# That's enough entropy for collision-free keying without paying the
# full-file hash cost on every 30-50 MB .mp4 — mp4 headers are unique
# per encode.
CACHE_HASH_BYTES = 4 * 1024 * 1024
CACHE_HASH_DIGEST_SIZE = 16
CACHE_VERSION_SUFFIX_LEN = 8


def _cache_version_suffix(prompt: str, model: str) -> str:
    """Short sha1 digest over (prompt + model) for cache-key versioning.

    Sprint 09b C3 (Codex #4). Any change to ``_ANALYSIS_PROMPT`` or the
    resolved model identifier produces a different suffix, and therefore
    a different cache filename, so stale cached analyses do not mask
    prompt/model iterations.
    """
    payload = f"{prompt}\0{model}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:CACHE_VERSION_SUFFIX_LEN]


def clip_cache_key(
    clip_path: str,
    *,
    prompt: str | None = None,
    model: str | None = None,
) -> Optional[str]:
    """Return a versioned cache key for ``clip_path``.

    The key is ``<content_hash>-<version_suffix>`` where ``content_hash``
    is the blake2b hex digest over the first ``CACHE_HASH_BYTES`` of
    ``clip_path`` and ``version_suffix`` is the 8-hex sha1 of
    ``prompt + model``. When either ``prompt`` or ``model`` is ``None``
    (legacy callers), the bare content_hash is returned so existing
    consumers keep working, but the cache is then version-agnostic and
    should be migrated.

    Returns ``None`` if the file cannot be read.
    """
    try:
        h = hashlib.blake2b(digest_size=CACHE_HASH_DIGEST_SIZE)
        with open(clip_path, "rb") as f:
            h.update(f.read(CACHE_HASH_BYTES))
    except OSError as exc:
        logger.warning("Cache-key read failed for %s: %s", clip_path, exc)
        return None
    content_hash = h.hexdigest()
    if prompt is None or model is None:
        return content_hash
    return f"{content_hash}-{_cache_version_suffix(prompt, model)}"


def _cache_sidecar_path(cache_dir: str, cache_key: str) -> str:
    return os.path.join(cache_dir, f"{cache_key}.json")


def _load_cached_analysis(cache_dir: str, cache_key: str) -> Optional[dict]:
    path = _cache_sidecar_path(cache_dir, cache_key)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_cached_analysis(cache_dir: str, cache_key: str, analysis: dict) -> None:
    """Atomic write: tmp file in the same dir, then os.replace."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as exc:
        logger.warning("Cache dir create failed (%s): %s", cache_dir, exc)
        return
    final = _cache_sidecar_path(cache_dir, cache_key)
    try:
        fd, tmp = tempfile.mkstemp(
            prefix=f"{cache_key}.", suffix=".json.tmp", dir=cache_dir,
        )
        with os.fdopen(fd, "w") as f:
            json.dump(analysis, f)
        os.replace(tmp, final)
    except OSError as exc:
        logger.warning("Cache write failed for %s: %s", final, exc)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "xiaomi/mimo-v2-omni"
DEFAULT_FPS = 4
MAX_VIDEO_SIZE_MB = 10
MAX_CONCURRENT = 3

# Sprint Arsenal Externalization (Commit 2): the literal prompt body
# moved to ``promo/arsenal/system_prompts/mimo_clip_analysis_v1.md``.
# The string symbol stays here as a re-export so callers that still
# do ``from promo.core.analyze.clip_analyzer import _ANALYSIS_PROMPT``
# (in particular the cache key derivation at line 296 + the Sprint 09b
# C3 cache-invalidation tests) keep working byte-identically. The
# arsenal loader strips trailing whitespace so the
# ``_cache_version_suffix`` hash ("3c0efc35" baseline) is preserved.
_ANALYSIS_PROMPT = arsenal_loader.load_system_prompt("mimo_clip_analysis")


# ---------------------------------------------------------------------------
#  OpenRouter API
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    from promo.core.config import openrouter_api_key

    return openrouter_api_key()


def _compress_video(video_path: str, target_mb: float = 8) -> str:
    """Compress video for API upload. Returns path to temp file."""
    import tempfile
    fd = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    compressed = fd.name
    fd.close()
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        if os.path.exists(compressed):
            os.unlink(compressed)
        raise RuntimeError(
            f"ffprobe failed for {video_path} (exit {result.returncode}): "
            f"{result.stderr.strip()[:200]}"
        )
    duration = float(result.stdout.strip())
    bitrate_k = int(target_mb * 8 * 1024 / duration)

    try:
        subprocess.run(
            ["ffmpeg", "-i", video_path, "-vf", "scale=-2:720",
             "-c:v", "libx264", "-preset", "fast", "-b:v", f"{bitrate_k}k",
             "-an", "-y", compressed],
            capture_output=True, check=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        if os.path.exists(compressed):
            os.unlink(compressed)
        raise
    return compressed


def _call_openrouter(video_path: str, model: str = None) -> dict:
    """Send a video clip to MiMo V2 Omni via OpenRouter."""
    import requests

    from promo.core.config import clip_model as _clip_model
    from promo.core.config import openrouter_http_referer

    api_key = _get_api_key()
    model = model or _clip_model()

    # Compress if needed
    compressed = None
    send_path = video_path
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    if size_mb > MAX_VIDEO_SIZE_MB:
        logger.info("Compressing %s (%.1f MB)...", os.path.basename(video_path), size_mb)
        compressed = _compress_video(video_path)
        send_path = compressed

    try:
        with open(send_path, "rb") as f:
            video_b64 = base64.b64encode(f.read()).decode("utf-8")

        response = requests.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": openrouter_http_referer(),
                "X-OpenRouter-Title": "pgc-pipeline",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _ANALYSIS_PROMPT},
                    {"type": "video_url",
                     "video_url": {"url": f"data:video/mp4;base64,{video_b64}"},
                     "fps": DEFAULT_FPS, "media_resolution": "default"},
                ]}],
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()
    finally:
        if compressed and os.path.exists(compressed):
            os.unlink(compressed)


def _parse_response(response: dict) -> dict:
    """Extract JSON from OpenRouter response."""
    text = response.get("choices", [{}])[0].get("message", {}).get("content", "")
    # Try markdown code block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Try raw JSON
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    logger.warning("Could not parse JSON: %s", text[:200])
    return {}


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def analyze_single_clip(
    clip_path: str,
    clip_id: str,
    model: str = None,
    cache_dir: Optional[str] = None,
) -> Optional[dict]:
    """Analyze a single clip. Returns {scene_description, category, camera_motion}.

    When ``cache_dir`` is set, a content-hash-plus-prompt-version sidecar
    is consulted before calling OpenRouter. Cache hits log
    ``mimo-cache hit`` and skip the API call entirely. Misses write the
    analysis to the sidecar after success.

    Sprint 09b C3 (Codex #4): the cache key includes a suffix derived
    from ``_ANALYSIS_PROMPT`` + the resolved model identifier, so prompt
    or model changes invalidate cached entries automatically.
    """
    from promo.core.config import clip_model as _clip_model
    resolved_model = model or _clip_model()
    cache_key = (
        clip_cache_key(clip_path, prompt=_ANALYSIS_PROMPT, model=resolved_model)
        if cache_dir else None
    )

    if cache_dir and cache_key:
        cached = _load_cached_analysis(cache_dir, cache_key)
        if cached is not None:
            # Sprint 17 E: cache-hit branch must apply the same
            # scene_description non-empty raise the fresh path enforces
            # below (Sprint 09b C8). Pre-C8 cache files written under the
            # current _ANALYSIS_PROMPT + model survive the version-suffix
            # invalidation and would otherwise replay malformed entries
            # through to Gemini. Mirror the fresh-path normalization
            # exactly: `(value or "").strip()`.
            cached_scene = (
                (cached.get("scene_description") or "").strip()
                if isinstance(cached, dict) else ""
            )
            if not cached_scene:
                # Log + raise mirrors the fresh-path pattern below
                # (logger.error + raise). Includes the on-disk cache
                # file path so the operator can locate and repair the
                # malformed entry — without this the same bad file
                # blocks every future run.
                bad_cache_file = _cache_sidecar_path(cache_dir, cache_key)
                raw_keys = (
                    sorted(cached.keys()) if isinstance(cached, dict)
                    else type(cached).__name__
                )
                logger.error(
                    "Clip %s analysis failed: cached MiMo entry at %s "
                    "missing or empty scene_description (raw keys: %s)",
                    clip_id, bad_cache_file, raw_keys,
                )
                raise MimoAnalysisError(
                    clip_id=clip_id,
                    clip_path=clip_path,
                    cause=RuntimeError(
                        "Cached MiMo entry missing or empty scene_description "
                        f"at {bad_cache_file} (raw keys: {raw_keys})"
                    ),
                )
            dmp = cached.get("dominant_motion_phase", "middle")
            if dmp not in ("early", "middle", "late"):
                dmp = "middle"
            cached["dominant_motion_phase"] = dmp
            logger.info(
                "Clip %s [mimo-cache hit]: %s [%s] phase=%s",
                clip_id,
                cached.get("scene_description", "?")[:70],
                cached.get("category", "?"), dmp,
            )
            return cached

    try:
        def _call():
            return _parse_response(_call_openrouter(clip_path, model=resolved_model))

        # Sprint 09b C4: retry budget bumped 1 -> 3 so transient
        # OpenRouter 5xx/timeouts are absorbed before the strict raise
        # below fires. Paired with the raise so "analysis failed" is a
        # real, persistent failure and not a transient blip.
        result = retry_with_backoff(_call, max_retries=3, base_delay=3.0)

        # Sprint 09b C8 (09b audit L-001 fix): _parse_response returns {}
        # when the model's response contains no parseable JSON block.
        # The empty dict is truthy after mutation, so the downstream
        # `if not analysis:` guard in analyze_clips never fired — the
        # C4 MimoAnalysisError sentinel was silently bypassed and the
        # clip would ship with scene_description="" to Gemini. Validate
        # the required "scene_description" field here; absent or empty
        # is a real analysis failure and must raise.
        scene_desc = (result.get("scene_description") or "").strip() if isinstance(result, dict) else ""
        if not scene_desc:
            raise MimoAnalysisError(
                clip_id=clip_id,
                clip_path=clip_path,
                cause=RuntimeError(
                    "MiMo response missing or empty scene_description "
                    f"(raw keys: {sorted(result.keys()) if isinstance(result, dict) else type(result).__name__})"
                ),
            )

        # Validate dominant_motion_phase — default to "middle" if missing/invalid
        dmp = result.get("dominant_motion_phase", "middle")
        if dmp not in ("early", "middle", "late"):
            dmp = "middle"
        result["dominant_motion_phase"] = dmp
        logger.info("Clip %s: %s [%s] phase=%s", clip_id,
                     scene_desc[:70],
                     result.get("category", "?"), dmp)
        if cache_dir and cache_key:
            _save_cached_analysis(cache_dir, cache_key, result)
        return result
    except MimoAnalysisError:
        # already wrapped by a downstream caller — don't double-wrap
        raise
    except Exception as exc:
        # Sprint 09b C4 (Codex #6): fail loud instead of returning None.
        # The prior stub-substitute path let a clip with no valid MiMo
        # description through to Gemini, which would then assign it to
        # a narration phrase and ship a render with content mismatch.
        logger.error("Clip %s analysis failed after retries: %s", clip_id, exc)
        raise MimoAnalysisError(clip_id=clip_id, clip_path=clip_path, cause=exc)


def analyze_clips(
    clip_paths: dict[str, str],
    max_concurrent: int = MAX_CONCURRENT,
    model: str = None,
    cache_dir: Optional[str] = None,
) -> list[dict]:
    """Analyze all clips and return scene descriptions for script generation.

    Args:
        clip_paths: Dict mapping clip_id -> local file path.
        cache_dir: When set, MiMo analyses are cached as per-clip sidecar
            JSON files at ``<cache_dir>/<cache_key>.json`` where
            ``cache_key`` includes a prompt+model version suffix (C3).
            Repeat runs against the same clip content + prompt + model
            skip the API call.

    Returns:
        List of {id, scene_description, category, camera_motion, dominant_motion_phase}.

    Raises:
        MimoAnalysisError: if any clip fails analysis after the retry
            budget is exhausted. Sprint 09b C4 (Codex #6) — the prior
            stub-substitute path let a description-less clip through to
            the script generator; strict raise here means the first
            persistent failure aborts the run with an actionable error.
    """
    results = []

    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {
            executor.submit(analyze_single_clip, path, cid, model, cache_dir): cid
            for cid, path in sorted(clip_paths.items())
        }
        for fut in as_completed(futures):
            clip_id = futures[fut]
            analysis = fut.result()  # may raise MimoAnalysisError; propagate
            # analysis is None only if analyze_single_clip returned None —
            # which post-C4 should not happen. Guard defensively: if a
            # future returns None without raising, treat it as a failure.
            if not analysis:
                raise MimoAnalysisError(
                    clip_id=clip_id,
                    clip_path=clip_paths.get(clip_id, "<unknown>"),
                    cause=RuntimeError("analyze_single_clip returned None unexpectedly"),
                )
            results.append({
                "id": clip_id,
                "scene_description": analysis.get("scene_description", ""),
                "category": analysis.get("category", "unknown"),
                "camera_motion": analysis.get("camera_motion", ""),
                "dominant_motion_phase": analysis.get("dominant_motion_phase", "middle"),
            })

    results.sort(key=lambda r: r["id"])
    logger.info("Analyzed %d clips", len(results))
    return results


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    from promo.core.logging_config import configure_logging
    configure_logging()

    parser = argparse.ArgumentParser(description="Describe video clips with MiMo V2 Omni")
    parser.add_argument("clips", nargs="+", help="Paths to video clip files")
    parser.add_argument("--model", default=None)
    parser.add_argument("--output", "-o", default=None)
    args = parser.parse_args()

    clip_paths = {}
    for path in args.clips:
        m = re.search(r"(\d+)", os.path.basename(path))
        cid = m.group(1).lstrip("0") or "0" if m else os.path.splitext(os.path.basename(path))[0]
        clip_paths[cid] = os.path.abspath(path)

    results = analyze_clips(clip_paths, model=args.model)
    out = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w") as f:
            f.write(out)
        logger.info("Saved to %s", args.output)
    else:
        print(out)
