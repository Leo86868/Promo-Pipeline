# promo/core/model_adapters/ — external model boundary

This folder is the small cabinet for provider-specific SDK / HTTP mechanics.
Pipeline stages keep their own business logic and call these adapters when
they need an external model.

## Files

| File | I/O surface |
|---|---|
| `registry.py` | Provider IDs, model IDs, base URLs, and output formats used by the current pipeline. No env reads. |
| `gemini.py` | Gemini text-generation surface. The actual `google.generativeai` import stays quarantined in `promo.core.llm.gemini_client`. |
| `openrouter.py` | OpenRouter chat-completion and embedding HTTP requests. Reads OpenRouter config through `promo.core.config`. |
| `tts.py` | Gemini TTS and ElevenLabs HTTP requests. Reads provider keys through `promo.core.config`. |

## Rule

Stage modules may keep prompt construction, response validation, caching, and
retry policy. Provider URLs, headers, API-key plumbing, and SDK call mechanics
belong here.
