# promo/cli/ — user-facing CLI scripts

Six standalone CLI entry points that drive the pipeline (or pieces of it) for operators. The CLIs are shells: argparse + `load_dotenv()` + backend construction; pipeline orchestration lives in `promo/core/pipeline/`.

## Files (inventory)

| File | Role |
|---|---|
| `__init__.py` | Empty (no re-exports). |
| `compile_promo.py` | Primary user-facing CLI. Full pipeline (analyze → script → narrate → assign → render). Backend-agnostic via `PromoBackend` Protocol. Supports `--render-props` shortcut for rendering from an existing `props.json` without re-running upstream stages. |
| `smoke_local_render.py` | Minimal standalone smoke path for local clips → render. Wraps the pipeline with `LocalBackend` defaults; useful for fast end-to-end verification without the full `compile_promo` flag surface. |
| `generate_narration.py` | TTS-only CLI: reads a script JSON, emits audio + `word_timestamps`. Useful when iterating on TTS without re-running Gemini #1. Also reachable via the back-compat shim `python3 -m promo.core.narrate.tts_engine generate ...`. |
| `list_voices.py` | Lists every voice in `arsenal/voices/catalog.yaml`. Reachable via the same back-compat shim (`tts_engine list`) per arsenal-externalization contract AC-16. |
| `build_embedding_index.py` | Sprint 12a harness — populates the per-POI embedding sidecar at `material/<slug>/.embedding_cache/`. Cache-only by default (raises if a clip is un-analyzed). Prints the AC8 summary line on success. |
| `render_architecture.py` | Renders the repo-root `/architecture.md` to `/architecture.html` (browser-viewable, Mermaid via CDN). Subfolder `architecture.md` files are NOT auto-rendered — they're consumed in-repo and on GitHub. |

## How they wire together

**Cross-folder consumers:**

- `compile_promo` is the only public caller of `pipeline.full_pipeline`. Wires `LocalBackend` (the standalone default), reads env vars via `core.config`, and surfaces `MimoAnalysisError` / `NoSuitableBGMError` as user-facing exit messages instead of stack traces.
- `generate_narration` calls `narrate.tts_engine.generate_narration` with a script JSON loaded from disk.
- `list_voices` reads `arsenal_loader.load_voice_catalog()` and prints a formatted table.
- `build_embedding_index` calls `assign.clip_embedder.embed_clips_for_poi` against `material/<slug>/clips/` + `material/<slug>/.mimo_cache/`, writing to `.embedding_cache/`.
- `render_architecture` is repo-tooling only — reads `/architecture.md` from the repo root and writes a self-contained HTML companion with Mermaid + Markdown rendered client-side.
- `smoke_local_render` wraps `pipeline.full_pipeline` with `LocalBackend` for fast end-to-end smoke runs; it short-circuits the full `compile_promo` argparse surface.

**Conventions:**

- Every CLI loads `.env` via `load_dotenv()` at module-import. Required env vars (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`) fail fast through `core.config` typed resolvers.
- `compile_promo` retains private helper re-exports (`_variant_output_path`, `_discover_bgm_files`, `_write_sidecar`, `_step_tts_narration`, `_step_assign_clips`, `_filter_clips_by_ids`) from their post-Sprint-4 subpackage homes — keeps the `from promo.cli.compile_promo import _foo` test-import surface stable.
- `render_architecture` regen discipline: any commit that edits `/architecture.md` should also re-run this script. See CLAUDE.md for the enforcement rule. (Subfolder `architecture.md` files are exempt — no HTML companion is generated for them.)
