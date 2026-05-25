# Arsenal — operator-facing libraries

The 4 sub-directories plus `script_hooks.yaml` under `promo/arsenal/` are the libraries the operator extends over time. Logic stays in Python; data leaves Python. Every consumer (`clip_analyzer`, `script_generator`, `clip_assigner`, `tts_engine`, `format_profiles`) reads through `promo/core/arsenal_loader.py` — never directly.

## What goes here

| Library | Contents | Loaded by |
|---|---|---|
| `system_prompts/*_v1.md` | The 4 LLM prompts: `mimo_clip_analysis`, `gemini1_script`, `gemini1_f3_retry`, `gemini2_assign`. | `arsenal_loader.load_system_prompt(name)` |
| `voices/catalog.yaml` | Voice catalog: `kore` (Gemini), `jarnathan`/`hope`/`heather` (ElevenLabs). | `arsenal_loader.load_voice_catalog()` |
| `personas/*.yaml` | Narrator personas — voice / tone / perspective only. | `arsenal_loader.load_persona(name_or_path)` |
| `script_skeletons/*.yaml` | Promo format templates (currently `short_30s`, `long_65s`). Each YAML constructs one `PromoFormatProfile`. | `arsenal_loader.load_format_template(key)` / `load_format_templates()` |
| `script_hooks.yaml` | Ordered hook-technique seeds for multi-variant script diversity. | `arsenal_loader.load_script_hooks()` |

## How to add a voice

1. Open `voices/catalog.yaml`. Append a new top-level key.
2. Required fields: `id`, `name`, `gender`, `age`, `accent`, `description`, `backend` (`gemini` or `elevenlabs`). Optional: `style_prompt` (Gemini-only directorial instruction).
3. Position matters — `compile_promo._resolve_voice_keys` reads dict order for variant rotation when `--voice` is unset. Gemini entries should stay first to preserve the default rotation contract.

## How to add a persona

1. Drop a `*.yaml` in `personas/`. Fields: `id`, `display_name`, `perspective`, `wpm`, `voice_id`, `system_prompt`, `tone_keywords`, `forbidden_phrases`, `forbidden_openers`, `pause_guidelines`, `example_scripts`, optional `gemini.{voice, style_prompt, default_tags}`.
2. Persona is voice / tone / perspective ONLY. Format-specific rules (sentence length, segment count, "minimum N words") belong in script skeletons, not personas. Sprint Arsenal Externalization Commit 6c removed 4 historical bullets from `third_person_promo.yaml` for exactly this reason.
3. `RandomPersonaSelector` becomes observable when ≥2 persona YAMLs ship in this directory; the selector resolves YAMLs by file path.

## How to add a format template

1. Drop a `*.yaml` in `script_skeletons/`. The YAML must declare a unique `mode` field — that becomes the dispatch key.
2. Required fields: every field of `promo.core.schema.PromoFormatProfile` (`mode`, `target_duration_sec`, `duration_label`, `segment_count`, `total_words_min/max`, `per_segment_min/max`, `min/recommended_clip_pool_size`, `min/max_effective_wpm`, `max_narration_ratio`, `segment_plans`) plus the 2 skeleton-owned fields:
   - `sentence_rule` (str) — fills `$sentence_rule` in `gemini1_script_v1.md`. The per-mode RULES bullet for sentence length / cadence.
   - `extra_rules` (list[str]) — joined with `"\n- "` and dropped into `$extra_rules_block`. Empty list = empty block.
3. `arsenal_loader.load_format_templates()` picks up the new YAML on next module-import. No Python edit. Re-validate `promo/tests/test_selection.py` random-distribution tests if your new mode key would shift the seeded sample.

## How to edit hook techniques

1. Open `script_hooks.yaml`.
2. Edit the `hook_techniques` list. Order matters — variants rotate through the list by index.
3. Keep values short because the hook label is inserted directly into the Gemini #1 variant note.

## How to bump a prompt version

When you intentionally change a system prompt body:

1. Save the new text as `*_v2.md` (or `_v3.md`, etc.) — keep the old `*_v1.md` alongside.
2. Update `_KNOWN_SYSTEM_PROMPTS` in `arsenal_loader.py` if you're adding a brand-new prompt; existing prompts auto-rev when their consumer references the new name.
3. Update the consumer call: `load_system_prompt("foo")` → `load_system_prompt("foo")` continues to load `foo_v1.md` because the loader appends `_v1.md` (today). For a v2-and-onward path you would extend the loader to dispatch on a registry — that change is out of scope for the v1-only sprint.

**MiMo prompt versioning is load-bearing**: the `_cache_version_suffix` is a SHA1 of (prompt + model). Any byte change in `mimo_clip_analysis_v1.md` invalidates every existing `material/<slug>/.mimo_cache/<hash>-<suffix>.json` — across operator's POI set that means hundreds of OpenRouter calls + significant wallclock + cost on the next compile. Bumping the version is the correct way to opt into that invalidation; editing the v1 file in place is a footgun.

## Cache key invariants

- `mimo_clip_analysis_v1.md`: byte-length 1397 (1391 chars; 3 em dashes × 2 extra UTF-8 bytes each), no trailing newline. Pinned by `test_clip_analyzer.py::TestSprintArsenalExternalizationMimoPrompt`.
- `gemini2_assign_v1.md`: contains the verbatim 5 two-space-model substrings. Pinned by `test_clip_assigner.py::TestSprintArsenalExternalizationGemini2Template`.
- `gemini1_script_v1.md`: literal `$1,900` survives substitution as `$$1,900` → `$1,900`. Pinned by `test_script_generator.py::TestSprintArsenalExternalizationGemini1Template`.
