"""Provider and model identifiers used by the current pipeline."""

MIMO_CLIP_MODEL = "xiaomi/mimo-v2-omni"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_TITLE = "pgc-pipeline"

OPENROUTER_EMBEDDING_MODEL = "text-embedding-3-small"
OPENROUTER_EMBEDDING_MODEL_API_ID = "openai/text-embedding-3-small"
OPENROUTER_EMBEDDING_DIM = 1536
OPENROUTER_EMBEDDING_API_URL = f"{OPENROUTER_BASE_URL}/embeddings"

GEMINI_TTS_PRIMARY_MODEL = "gemini-3.1-flash-tts-preview"
GEMINI_TTS_FALLBACK_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_TTS_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
ELEVENLABS_API_BASE = "https://api.elevenlabs.io"
