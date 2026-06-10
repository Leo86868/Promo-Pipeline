# promo/cli/ — user-facing CLI scripts

Seven standalone CLI entry points that drive the pipeline (or pieces of it) for operators. The CLIs are shells: argparse + `load_dotenv()` + backend construction; pipeline orchestration lives in `promo/core/pipeline/`.

> **Read upstream first:** [`README.md`](../../README.md) → [`promo/core/architecture.md`](../core/architecture.md) (defines POI, sidecar, arsenal, why two Gemini passes). This doc covers the user-facing CLI layer.

## Vocabulary (new terms in this doc)

- **render-props shortcut** — `compile_promo --render-props path/to/props.json` skips the upstream pipeline (analyze → script → narrate → assign) and goes straight to Remotion render. Useful for iterating on render-side changes (clip layout, BGM mix, captions) without re-running the expensive AI stages.
- **back-compat re-exports** — `compile_promo` re-imports private helpers that have moved to the `pipeline/` subpackage so the existing `from promo.cli.compile_promo import _foo` test-import surface stays stable. The re-export list is intentionally wide (covers ~14 private helpers used across the test suite).

## Files (inventory)

| File | I/O surface |
|---|---|
| `__init__.py` | Empty (no re-exports). |
| `compile_promo.py` | Primary user-facing CLI. Full pipeline (analyze → script → narrate → assign → render). **In:** `--poi`, `--local-clips`, `--target-duration-sec`, `--n-variants`, `--output-dir`, `--voice`, `--bgm-dir`, `--render-props` (shortcut), env vars (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`). **Out:** rendered `.mp4` files at `--output-dir` + 3 per-run sidecars. **Side:** wires `LocalBackend` (standalone default); reads env vars via `core.config`; surfaces `MimoAnalysisError` / `NoSuitableBGMError` as user-facing exit messages instead of stack traces. **Re-exports** (back-compat for test imports) ~14 private helpers from `pipeline/` subpackage and `core/` (e.g. `_variant_output_path`, `_discover_bgm_files`, `_write_sidecar`, `_resolve_bgm_paths`, `_resolve_voice_keys`, `_emit_run_sidecars`, `_build_variant_selections`, `_step_*` helpers, `analyze_clips_for_script`, `full_pipeline`). **Calls:** `pipeline.full_pipeline` (only public caller). |
| `run_batch.py` | Thin production batch shell. Expands a JSON POI list into one `compile_promo --n-variants 1` subprocess per requested output video. **In:** `--batch`, `--output-dir`, optional `--videos-per-poi`, `--target-duration-sec`, `--voices`, `--supabase-music-library`, `--seed`, `--jobs`, `--tail-workers`/`--serial-tail`. **Out:** one isolated output folder per video under `<output-dir>/<poi_slug>/video_###/`. **Side:** optionally reads Supabase Music Library once to assign exact `--supabase-music-id` values; does not call `full_pipeline` in-process. `--jobs` is intentionally limited to `1` until staging/no-repeat policies are ready for parallelism. **Tail pipelining (2026-06-10):** in autopilot mode the tail of video N (upscale/Drive/usage/release) runs on `--tail-workers` threads (default 1) while video N+1 renders; `plan_batch_items` orders items POI-round-robin and the loop never overlaps two same-POI videos (usage-event ordering); each worker thread builds its own Drive/Supabase clients (googleapiclient is not thread-safe); receipt flushes and timings-key inserts are lock-protected in `run_receipt`. `--serial-tail` (= `--tail-workers 0`) is the rollback switch to strictly serial behavior. |
| `smoke_local_render.py` | **Render-only smoke path — does NOT invoke `pipeline.full_pipeline`.** Loads a bundled fixture (`promo/tests/fixtures/local_render_smoke.json`), builds props directly, validates, stages media, and calls `render_promo`. Bypasses analyze / script / narrate / assign stages entirely — useful for verifying the Remotion render path independently of vendor calls. **In:** `--local-clips`, `--dry-run` (skips ffmpeg). **Out:** rendered `.mp4` (or props-only on `--dry-run`). **Calls:** `render.{build_props, validate_props, stage_media, render_promo}` directly. |
| `generate_narration.py` | TTS-only CLI: reads a script JSON, emits audio + `word_timestamps`. Useful when iterating on TTS without re-running Gemini #1. **In:** `--script-json`, optional `--voice`, `--output-dir`. **Out:** `narration.mp3` + word-timestamps JSON. **Side:** ElevenLabs / Gemini TTS API calls via `narrate.tts_engine.generate_narration`. Also reachable via the back-compat shim `python3 -m promo.core.narrate.tts_engine generate ...`. |
| `list_voices.py` | Lists every voice in `arsenal/voices/catalog.yaml` with backend + voice_id columns. **In:** none. **Out:** formatted table on stdout. **Calls:** `arsenal_loader.load_voice_catalog()`. Reachable via the same back-compat shim (`python3 -m promo.core.narrate.tts_engine list`). |
| `build_embedding_index.py` | Operator harness — populates the per-POI embedding sidecar at `material/<slug>/.embedding_cache/`. **In:** `--poi <slug>` (required). **Out:** prints a summary line `POI=<slug> clips_embedded=<N> cache_hits=<N> incremental=<N> mimo_prompt_sha1=<8-hex>`. **Side:** reads `material/<slug>/clips/` + `material/<slug>/.mimo_cache/` (cache-only by default — raises if any clip is un-analyzed). **Calls:** `assign.clip_embedder.embed_clips_for_poi`. **Raises:** `MimoAnalysisError` (if a clip lacks a MiMo cache entry); OpenRouter API errors propagate. |
| `render_architecture.py` | Renders the repo-root `/architecture.md` to `/architecture.html` (browser-viewable, Mermaid via CDN). **In:** none (reads `/architecture.md` from the repo root). **Out:** `/architecture.html`. **Side:** writes one HTML file. Subfolder `architecture.md` files are NOT auto-rendered — they're consumed in-repo and on GitHub. |

## How they wire together

**Cross-folder consumers:**

- `compile_promo` is the only public caller of `pipeline.full_pipeline`. Wires `LocalBackend` (the standalone default), reads env vars via `core.config`.
- `generate_narration` calls `narrate.tts_engine.generate_narration` with a script JSON loaded from disk.
- `list_voices` reads `arsenal_loader.load_voice_catalog()` and prints a formatted table.
- `build_embedding_index` calls `assign.clip_embedder.embed_clips_for_poi` against `material/<slug>/clips/` + `material/<slug>/.mimo_cache/`, writing to `.embedding_cache/`.
- `render_architecture` is repo-tooling only — reads `/architecture.md` from the repo root and writes a self-contained HTML companion with Mermaid + Markdown rendered client-side.
- `smoke_local_render` does NOT call `pipeline.full_pipeline`. It builds a `props.json` from a bundled test fixture and calls `render_promo` directly — purely a render-path smoke test.

**Conventions:**

- Every CLI loads `.env` via `load_dotenv()` at module-import. Required env vars (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`) fail fast through `core.config` typed resolvers.
- `compile_promo` retains a wide back-compat re-export surface for test imports — see Vocabulary above for the rationale.
- `render_architecture` regen discipline: any commit that edits `/architecture.md` should also re-run this script. (Subfolder `architecture.md` files are exempt — no HTML companion is generated for them.)
