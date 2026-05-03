#!/usr/bin/env python3
"""CLI — list available TTS voices from VOICE_CATALOG.

Usage:
    python3 -m promo.cli.list_voices

Also reachable via the back-compat shim at
``python3 -m promo.core.narrate.tts_engine list`` (arsenal-externalization
contract AC-16).
"""

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()

    from promo.core.logging_config import configure_logging
    configure_logging()

    from promo.core.narrate.tts_engine import VOICE_CATALOG

    print("\nVoice Catalog (ElevenLabs + Gemini 3.1 Flash):")
    print("=" * 60)
    for key, voice in VOICE_CATALOG.items():
        print(f"  {key}: {voice['name']} ({voice['gender']}, {voice['age']}, {voice['accent']})")
        print(f"    {voice['description']}")
        print(f"    voice_id: {voice['id']}")
        print()


if __name__ == "__main__":
    main()
