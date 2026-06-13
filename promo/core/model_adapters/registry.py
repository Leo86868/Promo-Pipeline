"""Model contacts book — the single place to swap provider/model identifiers.

Change a model here (or via the env override noted alongside) and every
consumer follows. No model-name string literal should live outside this module.

Env overrides:
  - MIMO_CLIP_MODEL   ← PROMO_CLIP_MODEL   (clip analysis)
  - GEMINI_TEXT_MODEL ← GEMINI_MODEL       (Gemini #1 script generation)
"""

# MiMo clip analysis (via OpenRouter)
MIMO_CLIP_MODEL = "xiaomi/mimo-v2-omni"

# OpenRouter — shared base + text embeddings
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_TITLE = "pgc-pipeline"

OPENROUTER_EMBEDDING_MODEL = "text-embedding-3-small"
OPENROUTER_EMBEDDING_MODEL_API_ID = "openai/text-embedding-3-small"
OPENROUTER_EMBEDDING_DIM = 1536
OPENROUTER_EMBEDDING_API_URL = f"{OPENROUTER_BASE_URL}/embeddings"

# Gemini text — script generation (Gemini #1)
GEMINI_TEXT_MODEL = "gemini-2.5-pro"

# Gemini TTS — narration synthesis
GEMINI_TTS_PRIMARY_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_TTS_FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# ElevenLabs TTS
ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
ELEVENLABS_API_BASE = "https://api.elevenlabs.io"
