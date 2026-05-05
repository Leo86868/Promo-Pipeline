# promo/core/analyze/ — Stage 1: describe clips with MiMo

Sends each `.mp4` clip to MiMo V2 Omni (via OpenRouter) and returns a structured description per clip (`scene_description`, `category`, `camera_motion`, `dominant_motion_phase`). Gemini #1 reads these descriptions when generating narration; without them, the script would have no idea what's on screen.

> **Read upstream first:** [`README.md`](../../../README.md) → [`promo/core/architecture.md`](../architecture.md) (defines POI, sidecar, retry helper, why two Gemini passes). This doc covers stage 1 only.

## Vocabulary (new terms in this doc)

- **content hash** — a 32-char fingerprint computed from the first 4MB of the clip file. Same clip → same hash; different clips → different hashes. Why only 4MB? Video file headers are unique per encode, so the first 4MB suffices to identify the file without reading the full 30-50MB payload.
- **version suffix** — an 8-char fingerprint of `(analysis prompt + "\0" + model name)`. Changing either side produces a new suffix.
- **two-axis cache key** — joining content hash + version suffix as the cache filename (`<hash>-<suffix>.json`). Means the cache is safe by construction: change a clip OR change the prompt → new filename → fresh analysis. No way to silently serve a stale result from an older prompt.

## Files (inventory)

| File | I/O surface |
|---|---|
| `__init__.py` | Stage marker; no exports. |
| `clip_analyzer.py` | The whole stage. **Provides:** `analyze_clips` (public; concurrent thread pool over the `clip_paths` dict) + `analyze_single_clip` + cache helpers (`clip_cache_key`, `_cache_version_suffix`, `_load_cached_analysis`, `_save_cached_analysis`). **`analyze_clips`:** In `clip_paths: dict[str, str]`, optional `cache_dir`. Out `list[ClipMetadata]` (TypedDict shape from `schema`). Side: per-clip OpenRouter HTTP call wrapped in the retry helper from `llm/retry`; reads the analysis prompt from `arsenal/system_prompts/mimo_clip_analysis_v1.md` via `arsenal_loader`; resolves `PROMO_CLIP_MODEL` via `config.clip_model()`; writes per-clip `material/<slug>/.mimo_cache/<hash>-<sfx>.json` (atomic `os.replace`). Raises `MimoAnalysisError` after the retry budget is exhausted (one failed clip aborts the concurrent pool — this replaces an older silent-stub behavior where unanalyzed clips would ship with `{"scene_description": "analysis failed"}`, letting Gemini assign mismatched clips). **Consumers:** `pipeline/_step_prepare_clips` (lazy import) + `cli/build_embedding_index` (cache-only mode reads existing `.mimo_cache/` entries). |

## How it wires together

**Invariants:**

- **Two-axis cache key** — `<content_hash>-<version_suffix>`. Any prompt or model change invalidates the cache automatically (the suffix changes → new filename → cache miss).
- **Cross-cache version lock** — `assign/clip_embedder.current_mimo_prompt_sha1()` calls `_cache_version_suffix` here directly, so the embedding sidecar invalidates in lockstep with the MiMo cache when the MiMo prompt changes.
- **Loud failures only** — `MimoAnalysisError` propagates after retry exhaustion; one failed clip aborts the concurrent pool. Replaces an older silent-stub-substitution behavior.
- **Atomic writes** — every cache entry uses `tmp → os.replace` so concurrent `ThreadPoolExecutor` workers cannot corrupt the sidecar.
