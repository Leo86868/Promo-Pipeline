# promo/core/llm/ — Gemini SDK quarantine + LLM utilities

The single network quarantine lane for Gemini calls. `gemini_client.py` is the **only** allowed `import google.generativeai` site in the repo (Pluggability Charter Rule 1); every other production file talks to Gemini through helpers re-exported here. The two utility modules (`retry.py`, `json_response.py`) are LLM-call companions consumed by Gemini- and OpenRouter-touching seams across the pipeline.

## Files (inventory)

| File | Role |
|---|---|
| `__init__.py` | Empty (no re-exports — consumers import each helper directly). |
| `gemini_client.py` | Gemini SDK quarantine. `configure_gemini()` (thread-safe one-time init), `reset_for_tests()` (clears the `_configured` flag), `resolve_gemini_model(log_context=...)` (returns a configured `GenerativeModel`), `GeminiModel` type alias. |
| `retry.py` | `retry_with_backoff(func, max_retries, base_delay, max_delay, exceptions)` — exponential-backoff retry helper. Doubles delay per attempt, capped at `max_delay`. |
| `json_response.py` | `parse_json_response(text)` — strict dict-shape JSON parser. Strips ` ```json ... ``` ` fences; raises `ValueError` on non-dict top-level shapes. |

## How they wire together

**Cross-file seams (verified consumer set):**

- `gemini_client.resolve_gemini_model` consumed by `script/script_generator` (Gemini #1) and `assign/clip_assignment_gemini` (Gemini #2). The `log_context` tag distinguishes the two call sites in captured logs. `script/script_gemini_caller` uses the `GeminiModel` type alias for parameter typing.
- `retry.retry_with_backoff` consumed by 4 sites: `analyze/clip_analyzer` (MiMo via OpenRouter), `script/script_gemini_caller` (Gemini #1), `assign/clip_assignment_gemini` (Gemini #2), and `assign/clip_embedder` (OpenRouter embeddings). Each caller picks its own retry budget (typically 2-3 retries, 2-3s base delay).
- `json_response.parse_json_response` consumed by `script/script_gemini_caller` only. The Gemini #2 site (`assign/clip_assignment_gemini._parse_gemini2_json`) has its own parser because that prompt emits a top-level list — see BACKLOG S6 for the unification proposal.
- `GEMINI_API_KEY` routes through `config.gemini_api_key()` (typed resolver, fail-fast). `GEMINI_MODEL` is the documented carve-out from Pluggability Charter Rule 2: read directly via `os.getenv("GEMINI_MODEL", "gemini-2.5-pro")` inside `gemini_client` because the module sits one import-cycle hop away from `config`.

**Invariants:**

- **Single quarantine site** — no other module is allowed to `import google.generativeai`. New consumers route through `resolve_gemini_model()` plus the `GeminiModel` type alias.
- **Module-global `_configured` is test-isolated** — `reset_for_tests()` clears the flag so a test rotating an API key sees a fresh `genai.configure(...)`. Production code MUST NOT call it. Pattern matches the test-isolation requirement called out in the project's Filter Quality S2.5 lessons (module-global cache/config flags need a `reset_for_tests()` helper in the same module).
- **Empty / whitespace api_key fails loud** — `configure_gemini("")` raises `ValueError` instead of silently no-op'ing into a configured-with-empty-key state. The typed resolver in `config.py` is the source of truth for "GEMINI_API_KEY is missing"; this helper trusts that output.
- **`parse_json_response` forbids top-level lists by design** — catches Gemini drifts that would silently parse but break downstream typed consumers. The Gemini #2 parser (assign-side) has the inverse contract because that prompt explicitly asks for a list.
