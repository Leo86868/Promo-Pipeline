#!/usr/bin/env python3
"""CLI — generate narration from a script JSON via the dual-backend TTS engine.

Usage:
    python3 -m promo.cli.generate_narration \\
        --script-json path/to/script.json \\
        [--voice jarnathan] \\
        [--output-dir output/]

Also reachable via the back-compat shim at
``python3 -m promo.core.narrate.tts_engine generate ...`` for symmetry
with the AC-16 ``list`` invocation.
"""

import argparse
import json
import os
from typing import Optional

from dotenv import load_dotenv


def main(argv: Optional[list[str]] = None) -> None:
    load_dotenv()

    from promo.core.logging_config import configure_logging
    configure_logging()

    from promo.core.narrate.tts_engine import VOICE_CATALOG, generate_narration

    parser = argparse.ArgumentParser(
        description="Generate hotel narration via the dual-backend TTS engine "
                    "(ElevenLabs + Gemini 3.1 Flash)."
    )
    parser.add_argument("--script-json", required=True, help="Path to script JSON")
    parser.add_argument(
        "--voice", default="jarnathan",
        help="Voice key from VOICE_CATALOG (e.g. kore for Gemini, jarnathan/hope/heather "
        "for ElevenLabs) or a raw ElevenLabs voice_id",
    )
    parser.add_argument("--output-dir", default=None, help="Output directory")

    args = parser.parse_args(argv)

    with open(args.script_json) as f:
        script = json.load(f)

    segments = script.get("segments", [])
    voice_key = args.voice
    voice_id = None
    if voice_key not in VOICE_CATALOG:
        # Treat as a raw voice_id.
        voice_id = voice_key
        voice_key = "custom"

    result = generate_narration(
        segments=segments,
        voice_id=voice_id,
        voice_key=voice_key if voice_id is None else "jarnathan",
        output_dir=args.output_dir,
    )

    print("\n" + "=" * 60)
    print("NARRATION GENERATED")
    print(f"Audio: {result['audio_path']}")
    print(f"Duration: {result['duration']:.1f}s")
    print(f"Words: {len(result['word_timestamps'])}")
    print(f"Segments: {len(result['segment_timestamps'])}")
    print("=" * 60)

    print("\nSegment timestamps:")
    for st in result["segment_timestamps"]:
        print(f"  Seg {st['segment']}: {st['start']:.2f}s - {st['end']:.2f}s ({st['duration']:.1f}s)")

    print("\nWord timestamps (first 20):")
    for wt in result["word_timestamps"][:20]:
        print(f"  [{wt['start']:.2f}-{wt['end']:.2f}] {wt['word']}")
    if len(result["word_timestamps"]) > 20:
        print(f"  ... ({len(result['word_timestamps']) - 20} more)")

    ts_path = os.path.join(os.path.dirname(result["audio_path"]), "timestamps.json")
    with open(ts_path, "w") as f:
        json.dump({
            "word_timestamps": result["word_timestamps"],
            "segment_timestamps": result["segment_timestamps"],
            "duration": result["duration"],
        }, f, indent=2)
    print(f"\nTimestamps saved: {ts_path}")


if __name__ == "__main__":
    main()
