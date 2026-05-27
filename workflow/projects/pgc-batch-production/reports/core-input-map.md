# Core Input Map

**Project:** PGC Batch Production
**Sprint:** 1 - Core map and input inventory
**Date:** 2026-05-26
**Status:** Verified from code inspection and targeted tests; no production code changes.

## Executive Summary

`architecture.md` has good diagrams for stage flow and the two-space
model, but it does not currently show all operator/config/data inputs or
where each input is consumed. The clearest existing orchestration diagram
is `promo/core/pipeline/architecture.md`, but it is a module wiring map,
not an input map.

The real main path is:

```text
compile_promo CLI
  -> full_pipeline
    -> _step_prepare_clips
    -> embedding cache dir resolution
    -> _resolve_voice_keys
    -> _build_variant_selections
    -> _step_generate_script
    -> _resolve_bgm_paths
    -> _run_variant_loop
      -> _step_tts_narration
      -> _step_assign_clips
      -> build_props_from_script / _bind_clips_to_narration
      -> validate_props
      -> stage_media
      -> build_match_quality_entries
      -> render_promo
      -> backend.save_output
      -> success-gated accumulator appends
    -> _emit_run_sidecars
```

The most important manifest finding: final visual timeline information
exists only inside the per-variant `props["clips"]` produced after
`_bind_clips_to_narration`; exact sidecar paths and final backend output
locations are currently not returned to `full_pipeline`.

## Verification Notes

An independent read-only code pass checked this map against the same
pipeline files and found several precision fixes, which are folded into
this report:

- `--bgm-dir` is pre-resolved by the CLI into `bgm_paths`; `_resolve_bgm_paths`
  receives that list.
- `full_pipeline` derives the embedding cache directory early, but actual
  sidecar loading and fallback provenance happen later inside
  `_step_assign_clips`.
- Remotion renders `props.clips`; `_bind_clips_to_narration` is the source
  of those bridge-aware entries, not the direct renderer input.
- The real variant path also includes props validation, media staging,
  match-quality row building, and `backend.save_output`.

Targeted tests also passed:

```bash
python3 -m pytest promo/tests/integration/test_compile_promo.py -k "full_pipeline_smoke or full_pipeline_long_form_fails_clip_pool_preflight" -q
python3 -m pytest promo/tests/unit/render/test_remotion_renderer.py -k "BindHappyPath or TailExtensionRegressionGuard" -q
```

## Terminal Visualization

```text
+----------------------------------------------------------------------------------+
| PGC compile_promo input flow                                                      |
+----------------------------------------------------------------------------------+

CLI flags / env / disk assets
  --poi --location --local-clips --output/--output-dir --target-duration-sec
  --n-variants --script-candidates --voice --bgm/--bgm-dir --tts-speed --seed
  env: OPENROUTER_API_KEY GEMINI_API_KEY ELEVENLABS_API_KEY + PROMO_* knobs
  disk: material clips, .mimo_cache, .embedding_cache, arsenal YAML/MD, BGM mp3

        | compile_promo.py: parser + LocalBackend + output/BGM pre-resolution
        v
  full_pipeline(poi_name, location, output_path, voice_key, bgm_path(s),
                skip_analysis, backend, target_duration_sec, n_variants,
                script_candidates, tts_speed, seed, hotel_description, notable_details)
        |
        +--> _step_prepare_clips
        |      backend.fetch_clips -> clip_paths
        |      MiMo or skip -> clips_metadata
        |      ffprobe -> clip_durations + source_duration_sec
        |
        +--> embedding cache dir derivation
        |      backend.clips_dir()/../.embedding_cache -> optional retrieval sidecar
        |
        +--> _resolve_voice_keys + _build_variant_selections
        |      voice catalog + PROMO_FORMAT_SELECTOR + seed -> profiles/personas
        |
        +--> _step_generate_script
        |      clips_metadata + POI text + profiles/personas + WPM sidecars
        |      -> scripts[] with pause_after_ms/effective_wpm
        |
        +--> _resolve_bgm_paths
        |      pre-resolved bgm_paths from --bgm-dir, else --bgm,
        |      else backend.fetch_bgm, else public/*.mp3
        |
        +--> _run_variant_loop per script
        |      _step_tts_narration -> narration audio + word/segment timestamps
        |      _step_assign_clips -> assignments + retrieval provenance
        |      build_props_from_script -> _bind_clips_to_narration
        |           inserts renderer bridge clips, then props["clips"] timeline
        |      validate_props + stage_media + build_match_quality_entries
        |      render_promo -> mp4
        |      backend.save_output -> final location
        |      append tts/match_quality/clip_assignment rows after render success
        |
        +--> _emit_run_sidecars
               writes clip_assignments + tts_metrics + match_quality
               CURRENT GAP: returns bool only, not exact written paths
```

## Existing Diagrams

| File | What It Shows | Gap |
|---|---|---|
| `architecture.md` | Stage overview from MiMo -> Gemini #1 -> TTS -> Gemini #2 -> Remotion; two-space model. | Does not show CLI/env/disk inputs or per-stage input ownership. |
| `promo/core/pipeline/architecture.md` | CLI -> `full_pipeline` -> `steps.py` / `variant_loop.py` / `sidecar_writer.py`. | Shows module ownership, not all runtime inputs or manifest facts. |

Recommendation: keep this report as the detailed inventory, then promote a
small cleaned-up diagram into `architecture.md` after the map is reviewed.

## Initial Input Layers

This section uses semantic layers: what kind of starting material the
video job needs, regardless of whether it comes from CLI flags, repo
YAML, env defaults, or future Supabase rows.

```text
+-------------------- PGC starting-input layers --------------------+
L1 Job identity / brief
  What are we making? poi, location, future poi_id/run_id.

L2 Visual material pool
  What footage can we use? clip MP4s today; future poi_asset_valid_clips assets.

L3 Recipe bundle
  What shape and sound should it have? duration, format, persona,
  script plan, voice, TTS speed, BGM.

L4 Execution/model settings
  How should the machine run it? API keys, models, seed, analysis mode,
  render concurrency, variant count.

L5 Output/record contract
  Where does the result go? output path/dir, sidecars, future run_manifest.

Generated later, not starting input
  script text, TTS audio/timestamps, clip assignments, final timeline,
  rendered MP4, usage-event draft rows.
```

### Layer 1 - Job Identity / Brief

This tells the pipeline what the video is about.

| Input | Meaning |
|---|---|
| `--poi` | The current POI/display name for prompts, sidecar names, and output names. |
| `--location` | Optional location context for script generation and render metadata. |
| future `poi_id` | Stable shared-library place identity; not present in local mode today. |
| future `run_id` / `manifest_id` | Stable run identity; not implemented yet. |

### Layer 2 - Visual Material Pool

This tells the pipeline what visual material it is allowed to choose
from.

| Input | Meaning |
|---|---|
| `--local-clips` | Folder containing source clip MP4s for local mode. |
| clip filenames | Current source of local 4-digit `clip_id` handles. |
| `.mimo_cache` | Optional prior visual analysis for those clips. |
| `.embedding_cache` | Optional retrieval sidecar for clip assignment. |
| future `poi_asset_valid_clips` rows | Shared-library source for `poi_id`, `asset_id`, `clip_id`, storage path, hash, metadata. |

### Layer 3 - Recipe Bundle

This tells the pipeline what kind of video to make and what it should
sound like. Duration, format, persona, script plan, voice, and BGM are
best understood as one recipe bundle, even though today they come from a
mix of CLI flags and repo YAML.

| Input | Current location / source | Meaning |
|---|---|
| duration | CLI `--target-duration-sec`; default env; profile YAML also carries `target_duration_sec`. | The requested length, for example 30s or 65s. It selects/constrains the format profile. |
| format/profile | `promo/arsenal/script_skeletons/short_30s.yaml`, `promo/arsenal/script_skeletons/long_65s.yaml`. | The video shape: segment count, total word range, per-segment word range, clip pool requirements, sentence rule, and segment plan. |
| script plan | Inside each format/profile YAML under `segment_plans`. | The outline Gemini should write into, for example HOOK -> FEEL -> HIGHLIGHTS -> CLOSE for short, or HOOK -> ARRIVAL -> LIVE IT -> HIGHLIGHTS -> CLOSE for long. It is not the final script text. |
| persona | `promo/arsenal/personas/third_person_promo.yaml`. | The narrator style: perspective, tone, system prompt, forbidden phrases/openers, pause guidance, and example scripts. |
| script prompts | `promo/arsenal/system_prompts/*.md`. | Stage-specific prompt bodies for MiMo, Gemini #1 script writing, F3 retry, and Gemini #2 assignment. |
| script hooks | `promo/arsenal/script_hooks.yaml`. | Additional script-generation guidance. |
| voice | CLI `--voice` or `promo/arsenal/voices/catalog.yaml`. | Narration voice/backend selection; defaults rotate through catalog order. |
| TTS speed | CLI `--tts-speed`. | Speed parameter passed to TTS. |
| BGM | CLI `--bgm` / `--bgm-dir`, backend BGM, then fallback `promo/remotion/public/*.mp3`. | Music source for variants. |
| `--script-candidates` | CLI/default env. | Number of script candidates to generate/retry from. |

Important: format and persona are conceptual starting inputs, but they
are not exposed as direct CLI flags today. The pipeline selects them
near the start using `target_duration_sec`, `n_variants`, `seed`, clip
metadata, repo YAML, and `PROMO_FORMAT_SELECTOR`.

Script plan is easy to confuse with final script. It is only the planned
container for the script: segment labels, approximate word counts, min/max
clips, guidance, and preferred clip categories. The actual spoken script
is generated later by Gemini #1.

### Layer 4 - Execution / Model Settings

This controls how the pipeline runs, but does not describe the creative
intent itself.

| Input | Meaning |
|---|---|
| API keys | Enable OpenRouter/MiMo, Gemini, and ElevenLabs paths. |
| model IDs | MiMo/Gemini model selection. |
| `--skip-analysis` | Uses stub clip metadata instead of MiMo analysis. |
| `--seed` | Deterministic selector seed. |
| `--n-variants` | Number of rendered variants to attempt. |
| render concurrency | Remotion execution setting. |
| default duration / variants / script candidates | Env-backed defaults when flags are omitted. |
| prior `tts_metrics_*.json` | WPM calibration for script generation. |

### Layer 5 - Output / Record Contract

This tells the pipeline where finished artifacts and evidence should go.

| Input | Meaning |
|---|---|
| `--output` / `--output-dir` | Where rendered MP4 outputs should be written. |
| sidecar output location | Currently derived from backend/output path. |
| `run_manifest` path | Local manifest artifact emitted in Sprint 3 after rendered outputs and sidecars exist. |
| future usage writeback target | Shared-library RPC/event table; not implemented in this repo yet. |

Short version: the actual starting bundle is not just "clips." It is
`brief + visual material + recipe bundle + execution settings + output
contract`.

### Directory Tree View

Current repo locations for the starting-input layers:

```text
promo/arsenal/
  personas/
    third_person_promo.yaml          # persona/style bundle
  script_skeletons/
    short_30s.yaml                   # 30s format + script plan
    long_65s.yaml                    # 65s format + script plan
  system_prompts/
    gemini1_script_v1.md             # script generation prompt
    gemini1_f3_retry_v1.md           # retry prompt
    gemini2_assign_v1.md             # clip assignment prompt
    mimo_clip_analysis_v1.md         # visual analysis prompt
  voices/
    catalog.yaml                     # voice/backend catalog
  script_hooks.yaml                  # extra script guidance

promo/remotion/public/
  *.mp3                              # default/fallback BGM

<operator local clip folder>/
  *.mp4                              # current visual material pool
  ../.mimo_cache                     # optional clip analysis cache
  ../.embedding_cache                # optional retrieval sidecar/cache

future AIGC Main shared library
  poi_asset_valid_clips view + poi-assets bucket # future visual material source
```

## Input Inventory

### CLI Inputs

The parser defines 15 CLI flags in `promo/cli/compile_promo.py`:

| CLI input | Destination / consumer |
|---|---|
| `--poi` | Required main-path POI name; passed to `full_pipeline.poi_name`; used for temp dir, sidecar slug, stage prompts, output naming. |
| `--location` | Passed to `full_pipeline.location`; consumed by script generation and render metadata. |
| `--output`, `-o` | Direct output MP4 path; used before `full_pipeline` and passed as `output_path`. |
| `--output-dir` | Builds default output path and initializes `LocalBackend.output_dir`. |
| `--local-clips` | Required in standalone mode; builds `LocalBackend(clips_dir=...)`. |
| `--voice` | Passed as `voice_key`; resolved by `_resolve_voice_keys`. |
| `--bgm` | Passed as explicit single `bgm_path`. |
| `--bgm-dir` | Pre-resolved by CLI into `bgm_paths` with duration filtering. |
| `--skip-analysis` | Passed to `_step_prepare_clips`; creates stub clip metadata instead of MiMo analysis. |
| `--render-props` | Render-only shortcut; bypasses the pipeline. |
| `--target-duration-sec` | Default from `PROMO_DEFAULT_DURATION_SEC`; drives output label, profile selection, BGM filtering, prompt duration, renderer final display end, and sidecar tag. |
| `--n-variants` | Default from `PROMO_DEFAULT_VARIANTS`; drives script count and variant loop. |
| `--script-candidates` | Default from `PROMO_DEFAULT_SCRIPT_CANDIDATES`; passed into script generation and F3 retry regeneration. |
| `--tts-speed` | Passed to TTS and F3 retry TTS. |
| `--seed` | Passed into `_build_variant_selections` for deterministic selector output. |

Source: `promo/cli/compile_promo.py` lines 128-193 and 290-304.

### Programmatic Inputs Not Exposed By CLI

`full_pipeline` accepts 15 parameters. The CLI passes 13 of them. These
two remain programmatic-only defaults from the CLI path:

| `full_pipeline` input | Current CLI behavior |
|---|---|
| `hotel_description` | Defaults to `""`; not exposed by `compile_promo`. |
| `notable_details` | Defaults to `""`; not exposed by `compile_promo`. |

Source: `promo/core/pipeline/pipeline.py` lines 40-56 and
`promo/cli/compile_promo.py` lines 290-304.

### Env Inputs

Resolved through `promo.core.config`, except `GEMINI_MODEL`, which is
the documented LLM quarantine carve-out.

| Env var | Required? | Consumer |
|---|---:|---|
| `OPENROUTER_API_KEY` | yes on MiMo / embedding paths | MiMo clip analysis and OpenRouter embeddings. |
| `GEMINI_API_KEY` | yes on Gemini paths | Gemini #1, Gemini #2, Gemini TTS. |
| `ELEVENLABS_API_KEY` | yes when ElevenLabs voice is used | ElevenLabs TTS backend. |
| `OPENROUTER_HTTP_REFERER` | optional | OpenRouter attribution header. |
| `GEMINI_MODEL` | optional | Gemini #1/#2 model id; default `gemini-2.5-pro`. |
| `PROMO_CLIP_MODEL` | optional | MiMo clip-analysis model id. |
| `PROMO_RENDER_CONCURRENCY` | optional | Remotion render concurrency. |
| `PROMO_FORMAT_SELECTOR` | optional | `single` or `random` format selector. |
| `PROMO_DEFAULT_DURATION_SEC` | optional | CLI default for `--target-duration-sec`. |
| `PROMO_DEFAULT_VARIANTS` | optional | CLI default for `--n-variants`. |
| `PROMO_DEFAULT_SCRIPT_CANDIDATES` | optional | CLI default for `--script-candidates`. |

Sources: `promo/core/config.py` lines 86-194,
`promo/core/llm/gemini_client.py` lines 73-90, `.env.example`.

### Disk/Data Inputs

| Input | Where consumed |
|---|---|
| Local clip MP4s under `--local-clips` | `LocalBackend.fetch_clips`; extracts 4-digit `clip_id` from filenames and copies to `tmp_dir`. |
| `.mimo_cache` sibling of clips dir | `_step_prepare_clips` passes as MiMo cache dir. |
| `.embedding_cache` sibling of clips dir | `full_pipeline` derives and passes to `_step_assign_clips`; retrieval is optional. |
| `promo/arsenal/system_prompts/*.md` | MiMo, Gemini #1, F3 retry, Gemini #2 prompt bodies. |
| `promo/arsenal/voices/catalog.yaml` | Voice rotation and TTS backend selection. |
| `promo/arsenal/personas/*.yaml` | Persona selector and script generation. |
| `promo/arsenal/script_skeletons/*.yaml` | Format profiles, segment plans, word bounds, clip pool requirements. |
| BGM MP3s | CLI `--bgm-dir`, explicit `--bgm`, backend BGM, or default `promo/remotion/public/*.mp3`. |
| Prior `tts_metrics_*.json` | `_step_generate_script` builds `wpm_search_dirs` and uses prior measured WPM for calibration. |

Sources: `promo/core/backend.py` lines 144-185,
`promo/core/pipeline/pipeline.py` lines 102-125 and 151-172,
`promo/core/pipeline/bgm_voice_resolver.py` lines 120-148,
`promo/core/pipeline/steps.py` lines 158-189.

## Stage Map

| Stage | Inputs | Outputs | Notes |
|---|---|---|---|
| CLI shell | argparse flags, env defaults, local paths | `LocalBackend`, `output`, optional `bgm_paths`, `full_pipeline(...)` call | Render-only mode bypasses this map after `render_from_props_file`. |
| `_step_prepare_clips` | `backend`, `poi_name`, `tmp_dir`, `target_duration_sec`, `skip_analysis` | `clip_paths`, `clips_metadata`, `clip_durations` | Attaches `source_duration_sec` to metadata after ffprobe. |
| embedding cache resolution | `backend.clips_dir()` | `embedding_cache_dir | None` | Directory must exist; missing sidecar becomes fallback provenance later. |
| `_resolve_voice_keys` | optional `voice_key`, voice catalog | voice rotation list | Explicit voice pins all variants; unset rotates catalog order. |
| `_build_variant_selections` | `n_variants`, `poi_name`, `clips_metadata`, `seed`, `target_duration_sec`, `PROMO_FORMAT_SELECTOR` | `variant_profiles`, `variant_personas` | `random` can produce per-variant durations but run-level sidecar/BGM labels still use scalar target. |
| `_step_generate_script` | POI text, `clips_metadata`, profiles/personas, WPM sidecars | `scripts[]` with `effective_wpm` and `pause_after_ms` | Gemini #1 stage; no clip assignments yet. |
| `_resolve_bgm_paths` | pre-resolved `bgm_paths`, `bgm_path`, backend BGM, default public BGM | resolved BGM list | CLI pre-resolves `--bgm-dir` into `bgm_paths`; priority is that list, then `--bgm`, backend, default public directory. |
| `_run_variant_loop` | scripts, clips, voices, BGM, profiles, output path, accumulators | `all_ok`, hard-fail count, retrieval provenance; mutates accumulator lists | Main per-variant body. |
| `_step_tts_narration` | script segments, voice key, variant temp dir, speed | narration audio path, word timestamps, segment timestamps, duration | Backend chosen by `VOICE_CATALOG[voice_key]["backend"]`. |
| `_step_assign_clips` | script, narration, clip metadata/durations, POI text, retrieval sidecar | final script/narration, assignments, retrieval provenance | F3 retry can regenerate script and narration. |
| `build_props_from_script` | script segments, `clip_paths`, narration, BGM, assignments, target duration | `props` | Calls `_bind_clips_to_narration`, which inserts renderer bridge clips and feeds `props["clips"]`. |
| `validate_props` / `stage_media` | `props`, variant temp dir | staged props/media | Validates render input and prepares media before Remotion render. |
| `build_match_quality_entries` | script, assignments, retrieval provenance | match-quality rows | Built before render, appended only after render success. |
| `render_promo` | `props`, variant output path | MP4 at variant path | Output path is chosen before render; backend save may copy/move to final operator-visible location. |
| success-gated accumulators | TTS metrics, match-quality rows, assignments | 3 in-memory row lists | Appended only after render success. |
| `_emit_run_sidecars` | accumulators, retrieval provenance, backend/output path | bool only | Writes three sidecars with collision bumps but does not return paths. |

## Manifest-Relevant Facts

### Available Today

| Fact | Current location |
|---|---|
| POI display name and slug | `poi_name`; `sanitize_poi_name(poi_name)`. |
| Local clip paths | `clip_paths` from `_step_prepare_clips`. |
| Clip metadata | `clips_metadata`, including `scene_description`, `category`, `source_duration_sec` when probed. |
| Clip durations | `clip_durations`. |
| Variant output path before backend save | `variant_output_path` inside `_run_variant_loop`. |
| Final visual timeline after bridge insertion | `props["clips"]` after `build_props_from_script`. |
| Rendered variant status | success-gated append in `_run_variant_loop`. |
| Retrieval provenance | `_step_assign_clips` return, last-variant-wins at run sidecar level. |
| TTS metrics / match-quality / assignments rows | success-gated accumulators in `_run_variant_loop`. |

### Not Available Cleanly Yet

| Missing / weak fact | Why it matters for manifest | Where the gap is |
|---|---|---|
| Exact sidecar paths written | Manifest should reference the real collision-bumped filenames. | `_write_sidecar` computes `candidate` but returns `bool`; `_emit_run_sidecars` returns `bool`. |
| Final backend output location | Manifest should record the final operator-visible MP4 location, not only the pre-save render path. | `backend.save_output` returns `final_loc`, but `_run_variant_loop` logs it and does not return/accumulate it. |
| Renderer timeline with role/segment/source duration together | Manifest needs `assigned_phrase` vs `bridge_tail`, segment, trim/display spans, and source duration. | `props["clips"]` has final display/trim spans but not `source_duration`, `segment`, or explicit role. `_bind_clips_to_narration` has richer entries but no role/segment and is hidden inside `build_props_from_script`. |
| `run_id` / `manifest_id` | Manifest identity does not exist yet. | No current module owns run identity. |
| Shared `poi_id`, `asset_id`, storage path, source hash | Required later for usage writeback. | Not present until PGC consumes AIGC Main `poi_asset_valid_clips`. |

## Messy Boundaries To Clean Later

These are not bugs; they are the places that make batch/manifest work
harder than it needs to be.

1. `full_pipeline` has a wide signature and threads many values through
   to closures inside `_step_assign_clips`. The values are real, but they
   are not grouped into run context / variant context objects.
2. `_run_variant_loop` owns render success, accumulator mutation, output
   save logging, and retrieval provenance selection. That makes it the
   natural manifest fact source, but it currently returns only a compact
   summary tuple.
3. `build_props_from_script` hides the only final bridge-aware timeline
   behind a props dict. For manifest work, a smaller timeline-returning
   helper would be clearer than scraping `props`.
4. `sidecar_writer` has collision safety but no observable result paths.
   This is the first mechanical blocker for accurate manifest references.
5. `hotel_description` and `notable_details` exist as programmatic
   `full_pipeline` inputs but are not exposed by the CLI; keep or remove
   should be decided before broad input cleanup.

## Recommended Next Sprint

Do not edit `architecture.md` first. Promote a small diagram there only
after this map is accepted.

Next implementation prep should be:

1. Change `_write_sidecar` / `_emit_run_sidecars` to return structured
   write results with exact paths while preserving current bool semantics
   for callers or adding a compatibility wrapper.
2. Expose final renderer timeline entries after bridge insertion with
   enough fields for manifest usage rows: `clip_id`, role, segment,
   trim start/end, display start/end, source duration.
3. Make `_run_variant_loop` accumulate rendered-output facts instead of
   only logging them.
4. Only then add `promo/core/pipeline/run_manifest.py`.

Sprint 3 follow-up: the local run manifest module has now been added.

## Sources

- `promo/cli/compile_promo.py` lines 128-193, 205-304.
- `promo/core/pipeline/pipeline.py` lines 40-56, 89-262.
- `promo/core/pipeline/steps.py` lines 80-199, 202-322, 410-635, 643-726.
- `promo/core/pipeline/variant_loop.py` lines 48-306.
- `promo/core/render/remotion_renderer.py` lines 70-190, 223-490, 550-602.
- `promo/remotion/src/HotelPromo/index.tsx` lines 63-125, 169-210, 324-337.
- `promo/core/pipeline/sidecar_writer.py` lines 22-153.
- `promo/core/backend.py` lines 36-50, 56-109, 144-185, 196-220.
- `promo/core/config.py` lines 86-194.
- `promo/core/llm/gemini_client.py` lines 73-90.
