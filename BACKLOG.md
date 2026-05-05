# BACKLOG

Findings deferred to future sprints, captured here so doc-writing and code-reading don't lose them. The four buckets correspond to expected sprint trajectories. "Other" holds items that don't fit S4/S5/S6 or are awaiting triage.

Each item is tagged with confidence:

- **Likely** — high confidence the item belongs in this sprint.
- **Tentative** — judgment call; reclassify when the right home becomes clearer.
- **Unclassified** — discovery is real but the right sprint is unclear; needs operator triage.

Cross-references to the auto-memory system are noted where relevant. Memory files remain canonical for catalog-style findings (e.g. test-coupling sites); BACKLOG.md is the in-repo entry point.

---

## S4 — Arsenal-bound (extend operator-facing data libraries)

- **`HOOK_TECHNIQUES` externalization** — *Likely* — `promo/core/script/script_prompt_builder.py:HOOK_TECHNIQUES` is a 6-string list hard-coded in Python. Belongs alongside `arsenal/personas/` and `arsenal/script_skeletons/` so the operator can rotate hooks without a Python edit.

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
