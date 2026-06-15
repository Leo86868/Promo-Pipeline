# BACKLOG

Findings deferred to future sprints, captured here so doc-writing and code-reading don't lose them. The four buckets correspond to expected sprint trajectories. "Other" holds items that don't fit S4/S5/S6 or are awaiting triage.

Each item is tagged with confidence:

- **Likely** — high confidence the item belongs in this sprint.
- **Tentative** — judgment call; reclassify when the right home becomes clearer.
- **Unclassified** — discovery is real but the right sprint is unclear; needs operator triage.

Cross-references to the auto-memory system are noted where relevant. Memory files remain canonical for catalog-style findings (e.g. test-coupling sites); BACKLOG.md is the in-repo entry point.

---

## S4 — Arsenal-bound (extend operator-facing data libraries)


*See `project_pgc_arsenal_completeness_backlog` memory for pre-existing items to fold into this section at S4 kick-off.*

---

## S5 — Test-rewrite (lift tests off internal-symbol monkey-patching)

- **Migrate facade-internal patches to DI or `autospec=True` attribute paths** — *Likely* — three S2 module-split sprints (S2a `tts_engine`, S2b `clip_assigner`, S2c `script_generator`) all collapsed bigger refactors into facade re-export patterns because tests patch internal symbols by bare name. Once tests stop reaching for internals, the facades can either stay or merge entry points back without breakage.
- **Broader coupling sweep beyond the three S2 facades** — *Tentative* — `promo/core/pipeline/` helpers (`_step_assign_clips` etc.) are likely also patched by name; sweep at S5 kick-off.

*See `project_pgc_test_health_backlog` memory for the S2c finding catalog (12 patch sites enumerated).*

---

## S6 — Tier-2 product (post-MVP polish, internal cleanups, naming)

- **Duplicate `_DEFAULT_PERSONA_PATH`** — *Likely* — defined in both `promo/core/script/script_prompt_builder.py` and `promo/core/selection/persona_selectors.py`. Drift risk; consolidate.
- **Memory-file paths leaked into source docstrings** — *Likely* — `promo/core/script/script_validator.py:16`, `promo/core/script/pause_budget.py:9`, `promo/core/script/script_gemini_caller.py:13-14` reference `<operator-memory-root>/...` paths or memory keys readers cannot resolve. Inline the rationale or remove.
- **`_parse_gemini2_json` vs `llm/json_response.parse_json_response`** — *Tentative* — the assign-side parser is distinct only because Gemini #2 emits a top-level list; could unify with an `expect_list: bool` parameter on the LLM helper.

---

## Other — uncategorized / pending triage

- **`clip_assignment_validator.py:301` bare `assert`** — *Unclassified* — `assert flat_entries[emission_pos] is entry, ...` could be stripped under `python -O`. Safer as an explicit `RuntimeError`. (Line reference updated post-S2b: the assert moved with `_enforce_hard_constraint_and_enrich` from `clip_assigner.py` into the extracted validator module.)
- **`selection/__init__.py:3` references "Shape B layout"** — *Unclassified* — convention isn't defined locally. Either link to the definition or drop the term.
- **`format_profiles.py` triggers I/O at module-import** — *Unclassified — minor* — `FORMAT_TEMPLATES = arsenal_loader.load_format_templates()` runs on first `import`. Surfaced now in the umbrella `promo/core/architecture.md` row for `format_profiles.py`; could also be added to the file's own docstring.
- **Repeated facade re-export pattern** — *Unclassified — overlaps with S5* — `clip_assigner.py`, `tts_engine.py`, `script_generator.py` all use the same shape, all because of the test patch surface. Once S5 lands, reconsider whether all three can collapse.

### From P4-health VPS smoke (2026-06-14, non-blocking)

- **~28 stale "Gemini #2" human-name strings in promo/core** — *DONE 2026-06-15 (`0498294`)* — P3.5-4's symbol grep missed the human-name token. Reworded all live/retirement references in promo/core (`git grep "Gemini #2" promo/core` = 0) EXCEPT the four stage-subfolder bibles, spun out as **P3.5b** (below).
- **Word floor 150→145** — *DONE 2026-06-15 (`38ad7a2`) — behavioral* — `long_65s.yaml` `total_words_min` 150→145 + pinned tests; verify redraw-friction drop on the next production batch.
- **Render concurrency default 2→6** — *DONE 2026-06-15 (`d2034da`)* — `PROMO_RENDER_CONCURRENCY` default raised; 8-core VPS was leaving ~6 idle. Long-term render lever is still the ffmpeg-vs-Remotion swap (ROADMAP §6).
- **P3.5b — stage-subfolder architecture bibles** — *DONE 2026-06-15 (`5f5f9a6..2cbcf30`)* — `promo/core/{pipeline,render,narrate,script}/architecture.md` recalibrated to packer reality against `assign/architecture.md` + real code (4 independent commits; reviewer double-pass — hard acceptance + fresh-agent symbol-vs-code audit, 0 new errors). Root/umbrella retirement/rollback context kept by design.

### Stale code-comment residue (deleted-engine, doc-only, batch with the Gemini-#2 sweep)

Fresh-agent symbol audit (P3.5b) caught 2 more stale comments the grep token set missed (behavior-correct, cosmetic — fold into a future comment-cleanup pass):

- **`script_validator.py:8`** — comment "Gate 2: LLM quality scoring" describes a gate that is no longer present in current code (the script_generator vocab already notes Gate 2 is historical).
- **`variant_loop.py:194`** — log string "Sprint 10 F3 retry" — the F3 retry path is retired; the abort log shouldn't reference it.
- *(optional doc-completeness)* `pipeline/architecture.md` could add a row listing the three currently-undocumented modules touched by the pipeline: `poi_asset_valid_clips`, `release_handoff`, `run_manifest`.

### SKILL.md NL-acceptance minor holes (2026-06-15, from darwin blind-operator test)

The de-drift SKILL passed blind-operator acceptance (0 wrong commands, values from the right source); 3 real holes were fixed (E5 classification / C2 length / E3 top-up). 3 minor holes deferred (smoother, not blocking — asking is the safe current behavior):

- **No `--videos-per-poi` default** — *Tentative* — "make 4 POI" (count omitted) forces the operator to ask every time. Could define a default (or document "if omitted, ask"). Asking is safe; this is friction only.
- **"渲染快点" has no operational mapping** — *Tentative* — SKILL correctly points render speed to `config.py` (no hardcoded number) but gives no guidance on which knob to bump or a safe ceiling on the shared VPS. Could add: "faster = raise `PROMO_RENDER_CONCURRENCY`, check `uptime`/AIGC load first."
- **720-transition phase has no in-doc status check** — *Tentative* — the transition flags are conditional ("while the phase is active") but the SKILL gives no way to check whether the 720 pool is still being drained; the operator must know externally. Could add a status pointer.
