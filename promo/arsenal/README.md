# Arsenal — operator-facing libraries

The 4 sub-directories plus `script_hooks.yaml` under `promo/arsenal/` are the libraries the operator extends over time. Logic stays in Python; data leaves Python. Every consumer (`clip_analyzer`, `script_generator`, `tts_engine`, `format_profiles`) reads through `promo/core/arsenal_loader.py` — never directly.

## 旋钮索引 — "想控制什么 → 改哪里" (P2)

| 想控制什么 | 改哪里 | 备注 |
|---|---|---|
| 镜头平均长度(beat 上下限) | 该 type 的 `script_skeletons/*.yaml` → `pacing.beat_min_sec` / `beat_max_sec` | 4s 顶有素材物理依据,卡上注释写明 |
| 单个停顿封顶 | 同上 → `pacing.pause_cap_ms` | 7000→3000 的历史也在卡上 |
| 选店素材门槛(50+10×extra) | 同上 → `assets.base_min` / `per_extra` | |
| 段数 / 字数 / 每段任务 | 同上 → `segment_count` / `total_words_*` / `segment_plans` | |
| 旁白人设 / 语气 / 禁用词 / 范文 | `personas/*.yaml` | 范文法规:每个 served format ≥2 篇打标范文 |
| 开头 hook 套路 | `script_hooks.yaml` | 发牌按 per-video `--hook-seed` 轮换(P2 step 5);sidecar 记 `assigned_hook` vs `self_reported_hook` |
| 声音目录 | `voices/catalog.yaml` | |
| Gemini #1 总 prompt | `system_prompts/gemini1_script_v1.md` | |
| TTS 语速等引擎参数 | 代码:`narrate/tts_elevenlabs.py` `VOICE_SETTINGS` | 尚未数据化 |
| persona / format 随机化 | env:`PROMO_PERSONA_SELECTOR` / `PROMO_FORMAT_SELECTOR` | 多丢一张 YAML 即可被选中 |

## What goes here

| Library | Contents | Loaded by |
|---|---|---|
| `system_prompts/*_v1.md` | The 2 LLM prompts: `mimo_clip_analysis`, `gemini1_script` (`gemini1_f3_retry` / `gemini2_assign` retired 2026-06-11 with the Gemini #2 chain). | `arsenal_loader.load_system_prompt(name)` |
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

## How to add a format template (= add a type, P2)

1. Drop a `*.yaml` in `script_skeletons/`. The YAML must declare a unique `mode` field — that becomes the dispatch key — and a unique `target_duration_sec` (duration is the routing key; two cards on one duration fail loudly at load).
2. Required fields: every field of `promo.core.schema.PromoFormatProfile` (`mode`, `target_duration_sec`, `duration_label`, `segment_count`, `total_words_min/max`, `per_segment_min/max`, `min/recommended_clip_pool_size`, `min/max_effective_wpm`, `max_narration_ratio`, `segment_plans`) plus the skeleton-owned fields:
   - `description` (str) — operator-facing one-liner; the brief entry for "make type B".
   - `pacing` (`beat_min_sec` / `beat_max_sec` / `pause_cap_ms`) — the card IS the source; no code defaults exist to fall back to.
   - `assets` (`base_min` / `per_extra`) — POI selection floor.
   - `sentence_rule` (str) — fills `$sentence_rule` in `gemini1_script_v1.md`. The per-mode RULES bullet for sentence length / cadence.
   - `extra_rules` (list[str]) — joined with `"\n- "` and dropped into `$extra_rules_block`. Empty list = empty block.
3. **范文法规**: add 2-3 examples tagged `format: <your mode>` to the persona's `example_scripts`, each INSIDE the card's word range — the model imitates examples over instruction numbers, and the prompt builder refuses to borrow another format's examples.
4. `arsenal_loader.load_format_templates()` picks up the new YAML on next module-import. No Python edit. Re-validate `promo/tests/test_selection.py` random-distribution tests if your new mode key would shift the seeded sample. New duration tiers may also need an asset-floor conversation with the AIGC side first (e.g. 120s ≈ 90-100 floor).

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
