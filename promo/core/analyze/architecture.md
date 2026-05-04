# promo/core/analyze/ — Stage 1: describe clips with MiMo

Sends each `.mp4` clip to MiMo V2 Omni (via OpenRouter) and returns a structured description per clip (`scene_description`, `category`, `camera_motion`, `dominant_motion_phase`). Gemini #1 reads these descriptions when generating narration; without them, the script would have no idea what's on screen.

## Files (inventory)

| File | Role |
|---|---|
| `__init__.py` | Stage marker; no exports. |
| `clip_analyzer.py` | The whole stage. `analyze_clips()` (concurrent thread pool, public entry) + `analyze_single_clip()` + cache helpers (`clip_cache_key`, `_cache_version_suffix`, `_load_cached_analysis`, `_save_cached_analysis`). |

## How it wires together

**Cross-file seams:**

- Reads the prompt body from `arsenal/system_prompts/mimo_clip_analysis_v1.md` via `arsenal_loader.load_system_prompt("mimo_clip_analysis")`.
- Resolves `PROMO_CLIP_MODEL` through `config.clip_model()` (typed env-var resolver, fail-fast).
- Wraps the OpenRouter HTTP call in `llm.retry.retry_with_backoff`.
- Raises `errors.MimoAnalysisError` after the retry budget is exhausted (Sprint 09b C4 — the prior silent stub-substitution path was retired).
- Output shape conforms to `schema.ClipMetadata` (TypedDict).
- Producer of `material/<slug>/.mimo_cache/<content_hash>-<version_suffix>.json` (atomic `os.replace`); the version suffix is consumed by `assign/clip_embedder.current_mimo_prompt_sha1` for cross-cache version locking.

**Invariants:**

- **Two-axis cache key** — `<content_hash>-<version_suffix>`, where `content_hash` is blake2b16 over the first 4MB of the clip file and `version_suffix` is the first 8 hex of `sha1(prompt + "\0" + model)`. Any prompt or model change invalidates the cache automatically (Sprint 09b C3).
- **Cross-cache version lock** — `assign/clip_embedder.current_mimo_prompt_sha1()` calls `_cache_version_suffix` here directly, so the embedding sidecar invalidates in lockstep with the MiMo cache when the MiMo prompt changes.
- **Loud failures only** — `MimoAnalysisError` propagates after retry exhaustion; one failed clip aborts the concurrent pool (Sprint 09b C4 replaced the prior `{"scene_description": "analysis failed"}` stub that would let unlabeled clips ship to Gemini).
- **Atomic writes** — every cache entry uses `tmp → os.replace` so concurrent `ThreadPoolExecutor` workers cannot corrupt the sidecar.
