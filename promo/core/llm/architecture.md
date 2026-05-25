# promo/core/llm/ — Gemini SDK quarantine + LLM utilities

The single SDK quarantine lane for Gemini calls. `gemini_client.py` is the **only** allowed `import google.generativeai` site in the repo (Pluggability Charter Rule 1); production model-call sites reach it through `promo.core.model_adapters.gemini`. The two utility modules (`retry.py`, `json_response.py`) are LLM-call companions consumed by Gemini- and OpenRouter-touching seams across the pipeline.

> **Read upstream first:** [`README.md`](../../../README.md) → [`promo/core/architecture.md`](../architecture.md) (defines Pluggability Charter, retry helper, Gemini #1/#2). This doc covers the cross-cutting `llm/` folder.

## Vocabulary (new terms in this doc)

- **quarantine lane** — a code architecture pattern where access to a vendor SDK (here: `google.generativeai`) is confined to a single file. Consumers call helpers re-exported by that file; the SDK's import surface never spreads into the rest of the codebase, so swapping providers later is a single-file change.
- **`_configured` flag** — a module-global guard inside `gemini_client.py` that prevents double-initialization of the Gemini SDK across threads. Threading lock + flag ensure exactly one `genai.configure(...)` call per process.

## Files (inventory)

| File | I/O surface |
|---|---|
| `__init__.py` | Empty (no re-exports — consumers import each helper directly). |
| `gemini_client.py` | **Provides:** `configure_gemini(api_key)` (thread-safe one-time init via `_configured` flag + threading lock), `reset_for_tests()` (clears the flag for test isolation), `resolve_gemini_model(log_context=...)` (returns a configured `GenerativeModel`), `GeminiModel` type alias. **In:** API key (from `config.gemini_api_key()`); optional `log_context` for log disambiguation. **Out:** `GenerativeModel` instance. **Side:** the only `import google.generativeai` site in the repo; reads `GEMINI_MODEL` via `os.getenv` (the documented carve-out from Pluggability Charter Rule 2). **Raises:** `ValueError` on empty/whitespace api_key. **Consumers:** `model_adapters/gemini`. |
| `retry.py` | **Provides:** `retry_with_backoff(func, max_retries=3, base_delay=1.0, max_delay=60.0, exceptions=(Exception,))` — wraps any callable and retries on the listed exceptions with exponential backoff (delay doubles per attempt, capped at `max_delay`). **In:** the callable + retry policy parameters. **Out:** the callable's return value. **Side:** sleeps between attempts; logs each attempt. **Raises:** the last caught exception after `max_retries` failures. **Consumers (4 sites):** `analyze/clip_analyzer` (MiMo via OpenRouter), `script/script_gemini_caller` (Gemini #1), `assign/clip_assignment_gemini` (Gemini #2), `assign/clip_embedder` (OpenRouter embeddings). |
| `json_response.py` | **Provides:** `parse_json_response(text)` — strict dict-shape JSON parser for AI text responses. **In:** raw response text. **Out:** parsed `dict`. **Side:** strips ` ```json ... ``` ` fences before parsing; pure otherwise. **Raises:** `ValueError` on JSON decode failure or non-dict top-level shape. **Consumers:** `script/script_gemini_caller` only. The Gemini #2 site has its own list-parser (`_parse_gemini2_json` in `assign/clip_assignment_gemini`) because that prompt emits a top-level list, which this helper rejects by design — see [`BACKLOG.md`](../../../BACKLOG.md) for the unification proposal. |

## How they wire together

**Cross-file seams:**

- `GEMINI_API_KEY` routes through `config.gemini_api_key()` (typed resolver, fail-fast). `GEMINI_MODEL` is the documented carve-out from Pluggability Charter Rule 2: read directly via `os.getenv("GEMINI_MODEL", "gemini-2.5-pro")` inside `gemini_client` because that module sits one import-cycle hop away from `config`.
- `model_adapters/gemini` re-exports `GeminiModel` / `resolve_gemini_model` and owns the SDK call wrapper used by script and assignment stages.

**Invariants:**

- **Single quarantine site** — no other module is allowed to `import google.generativeai`. New consumers route through `model_adapters/gemini`.
- **`_configured` flag is test-isolated** — `reset_for_tests()` clears the flag so a test rotating an API key sees a fresh `genai.configure(...)`. Production code MUST NOT call it. Pattern: any module-global cache/config flag in this codebase ships with a `reset_for_tests()` helper in the same module.
- **Empty / whitespace api_key fails loud** — `configure_gemini("")` raises `ValueError` instead of silently no-op'ing into a configured-with-empty-key state. The typed resolver in `config.py` is the source of truth for "GEMINI_API_KEY is missing"; this helper trusts that output.
- **`parse_json_response` forbids top-level lists by design** — catches Gemini drifts that would silently parse but break downstream typed consumers. The Gemini #2 parser (assign-side) has the inverse contract because that prompt explicitly asks for a list.
