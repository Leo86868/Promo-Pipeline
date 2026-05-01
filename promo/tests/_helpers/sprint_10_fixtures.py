"""One-off fixture builder for Sprint 10 C6 — reconstructs script fixtures
from Sprint 09b log artifacts and (with ``--live``) captures one Gemini #2
response per POI.

Running without ``--live`` rebuilds the script fixtures from logs only,
skipping the API call. Running with ``--live`` ALSO hits Gemini #2 once
per POI and writes the happy-path + two derived negative fixtures.

Usage:
    python3 -m promo.tests._helpers.sprint_10_fixtures                # scripts only
    python3 -m promo.tests._helpers.sprint_10_fixtures --live         # + Gemini #2
    python3 -m promo.tests._helpers.sprint_10_fixtures --live --only lpi
"""
# dev utility

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "promo" / "tests" / "fixtures" / "sprint-10"

POI_CONFIG = {
    "lpi": {
        "poi_name": "Little Palm Island Resort",
        "location": "Little Torch Key, Florida",
        "target_duration_sec": 65.0,
        "log_path": REPO_ROOT / "output" / "sprint-09b" / "_run_lpi_65s.log",
        "clips_dir": REPO_ROOT / "material" / "little-palm-island-resort" / "clips",
        "script_fixture": "lpi_v1_script.json",
        "gemini_fixture": "gemini2_response_lpi_v1.json",
        "drop_fixture": "gemini2_response_lpi_v1_drop_segment.json",
        "overlap_fixture": "gemini2_response_lpi_v1_overlap.json",
    },
    "jashita": {
        "poi_name": "Jashita Hotel Tulum",
        "location": "Soliman Bay, Tulum, Mexico",
        "target_duration_sec": 65.0,
        "log_path": REPO_ROOT / "output" / "sprint-09b" / "_run_jashita_65s.log",
        "clips_dir": REPO_ROOT / "material" / "jashita-hotel-tulum" / "clips",
        "script_fixture": "jashita_v1_script.json",
        "gemini_fixture": "gemini2_response_jashita_v1.json",
        "drop_fixture": "gemini2_response_jashita_v1_drop_segment.json",
        "overlap_fixture": "gemini2_response_jashita_v1_overlap.json",
    },
}


_TAGGED_HEADER = "Post-TTS tagged_text (variant 1):"
_PAUSE_RE = re.compile(r"Pre-TTS pause_after_ms per segment \(variant 1\): (\[.*\])")
_SEGTS_RE = re.compile(r"Post-TTS segment_timestamps \(variant 1\): (\[.*\])")


def _extract_from_log(log_path: Path) -> dict:
    """Parse the Sprint 09b run log for the variant-1 inputs we need."""
    lines = log_path.read_text().splitlines()
    pauses: list[int] | None = None
    segment_ts: list[dict] | None = None
    tagged_text_lines: list[str] = []
    in_tagged = False
    for i, ln in enumerate(lines):
        if in_tagged:
            # tagged_text continues until the next timestamped log line.
            if re.match(r"\d{2}:\d{2}:\d{2}\s+[A-Za-z_.]+\s+(INFO|WARNING|ERROR)", ln):
                in_tagged = False
            else:
                tagged_text_lines.append(ln)
                continue
        m = _PAUSE_RE.search(ln)
        if m and pauses is None:
            pauses = json.loads(m.group(1))
            continue
        m = _SEGTS_RE.search(ln)
        if m and segment_ts is None:
            # The log serializes python dicts (single quotes) — eval-safe
            # replacement to JSON.
            segment_ts = json.loads(m.group(1).replace("'", '"'))
            continue
        if _TAGGED_HEADER in ln and not tagged_text_lines:
            in_tagged = True
    if pauses is None or segment_ts is None or not tagged_text_lines:
        raise RuntimeError(
            f"Could not extract variant-1 inputs from {log_path}: "
            f"pauses={pauses is not None}, seg_ts={segment_ts is not None}, "
            f"tagged_len={len(tagged_text_lines)}"
        )
    tagged_text = " ".join(
        ln.strip() for ln in tagged_text_lines if ln.strip()
    )
    return {
        "pauses": pauses,
        "segment_timestamps": segment_ts,
        "tagged_text": tagged_text,
    }


def _split_text_by_word_count(text: str, counts: list[int]) -> list[str]:
    """Consume ``counts[i]`` whitespace-delimited words per segment, in
    order, from the full ``text`` blob."""
    words = text.split()
    segments: list[str] = []
    cursor = 0
    for n in counts:
        chunk = words[cursor:cursor + n]
        segments.append(" ".join(chunk))
        cursor += n
    if cursor < len(words):
        # Trailing words — append to the last segment so nothing is lost.
        tail = words[cursor:]
        if segments:
            segments[-1] = (segments[-1] + " " + " ".join(tail)).strip()
        else:
            segments.append(" ".join(tail))
    return segments


def _build_script_fixture(poi_key: str, extracted: dict, config: dict) -> dict:
    counts = [int(s["word_count"]) for s in extracted["segment_timestamps"]]
    segment_texts = _split_text_by_word_count(extracted["tagged_text"], counts)
    segments: list[dict] = []
    for i, (text, pause_ms, ts) in enumerate(
        zip(segment_texts, extracted["pauses"], extracted["segment_timestamps"]),
        start=1,
    ):
        segments.append({
            "segment": i,
            "text": text,
            "pause_after_ms": int(pause_ms),
            "word_count": int(ts["word_count"]),
            "start_sec": float(ts["start"]),
            "end_sec": float(ts["end"]),
        })
    return {
        "poi_key": poi_key,
        "poi_name": config["poi_name"],
        "location": config["location"],
        "target_duration_sec": config["target_duration_sec"],
        "format_mode": "long",
        "variant_index": 1,
        "segments": segments,
    }


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _synthetic_word_timestamps(script_fixture: dict) -> list[dict]:
    """Generate the synthetic uniform word_timestamps the fixture-replay
    test uses. Same function as the test's helper — re-used here so the
    live Gemini #2 prompt sees the same numbers the test will replay.
    """
    wts: list[dict] = []
    for seg in script_fixture["segments"]:
        n = int(seg["word_count"])
        if n == 0:
            continue
        start = float(seg["start_sec"])
        end = float(seg["end_sec"])
        span = max(end - start, 1e-3)
        per_word = span / n
        words = seg["text"].split()
        if len(words) < n:
            words = words + [""] * (n - len(words))
        for i in range(n):
            w_start = start + i * per_word
            w_end = start + (i + 1) * per_word
            wts.append({
                "word": words[i],
                "start": round(w_start, 3),
                "end": round(w_end, 3),
            })
    return wts


def _load_clip_metadata(clips_dir: Path) -> list[dict]:
    """Minimal MiMo-free metadata: id + source_duration_sec. Matches the
    subset Gemini #2 actually reads.
    """
    from promo.core.render.remotion_renderer import get_clip_duration
    entries: list[dict] = []
    for p in sorted(clips_dir.glob("*.mp4")):
        m = re.search(r"(\d{4})", p.name)
        if not m:
            continue
        clip_id = m.group(1)
        entries.append({
            "id": clip_id,
            "category": "unknown",
            "scene_description": "",
            "main_subject": "",
            "shot_size": "",
            "source_duration_sec": get_clip_duration(str(p)),
        })
    return entries


def _live_capture(poi_key: str, config: dict) -> None:
    """Run ONE Gemini #2 call and commit its response as the happy-path
    fixture. Derives drop-segment + overlap negative fixtures from it.
    """
    from promo.core.assign.clip_assigner import (
        _build_gemini2_prompt, _call_gemini2,
    )
    from promo.core.format_profiles import get_promo_format_profile

    script_path = FIXTURES_DIR / config["script_fixture"]
    script_fixture = json.loads(script_path.read_text())
    word_ts = _synthetic_word_timestamps(script_fixture)
    pauses = [int(s["pause_after_ms"]) for s in script_fixture["segments"]]
    clips_meta = _load_clip_metadata(config["clips_dir"])
    profile = get_promo_format_profile(script_fixture["target_duration_sec"])

    prompt = _build_gemini2_prompt(
        script={
            "poi_name": script_fixture["poi_name"],
            "location": script_fixture["location"],
            "target_duration_sec": script_fixture["target_duration_sec"],
            "segments": script_fixture["segments"],
        },
        word_timestamps=word_ts,
        pause_after_ms_per_segment=pauses,
        clips_metadata=clips_meta,
        variant_index=1,
        profile=profile,
    )
    logger.info("Gemini #2 prompt for %s: %d chars", poi_key, len(prompt))
    response = _call_gemini2(prompt)
    logger.info("Gemini #2 response for %s: %d phrases", poi_key, len(response))

    # Happy-path fixture
    _write_json(FIXTURES_DIR / config["gemini_fixture"], response)
    canonical = json.dumps(response, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    logger.info("Gemini #2 %s sha256: %s", poi_key, digest)

    # Negative fixture 1: drop all phrases from segment 2 (L-001 guard).
    # If Gemini didn't emit multiple segments, skip — the test module will.
    segments_present = sorted({int(r.get("segment", 0)) for r in response})
    if len(segments_present) >= 2:
        drop_target = segments_present[1]
        mutated_drop = [r for r in response if int(r.get("segment", 0)) != drop_target]
        _write_json(FIXTURES_DIR / config["drop_fixture"], {
            "dropped_segment": drop_target,
            "assignments": mutated_drop,
        })

    # Negative fixture 2: overlap two phrases within a segment (L-003 guard).
    # Find the first segment with ≥2 phrases and collapse phrase-2's
    # start_word_idx to phrase-1's start_word_idx — creating an overlap.
    by_segment: dict[int, list[dict]] = {}
    for r in response:
        by_segment.setdefault(int(r.get("segment", 0)), []).append(r)
    overlap_source = None
    for seg_idx, phrases in sorted(by_segment.items()):
        if len(phrases) >= 2:
            overlap_source = (seg_idx, phrases[0], phrases[1])
            break
    if overlap_source:
        seg_idx, p1, p2 = overlap_source
        mutated_overlap = [dict(r) for r in response]
        for entry in mutated_overlap:
            if (
                int(entry.get("segment", 0)) == seg_idx
                and entry.get("clip_id") == p2.get("clip_id")
            ):
                # Make phrase-2 start BEFORE phrase-1's end → overlap.
                entry["start_word_idx"] = int(p1["start_word_idx"])
                break
        _write_json(FIXTURES_DIR / config["overlap_fixture"], {
            "overlap_segment": seg_idx,
            "assignments": mutated_overlap,
        })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Hit Gemini #2 once per POI and write the response fixture")
    parser.add_argument("--only", choices=list(POI_CONFIG.keys()),
                        help="Restrict to a single POI")
    args = parser.parse_args()

    from promo.core.logging_config import configure_logging
    configure_logging()

    pois = [args.only] if args.only else list(POI_CONFIG)
    for poi_key in pois:
        config = POI_CONFIG[poi_key]
        logger.info("=== %s ===", poi_key)
        extracted = _extract_from_log(config["log_path"])
        fixture = _build_script_fixture(poi_key, extracted, config)
        _write_json(FIXTURES_DIR / config["script_fixture"], fixture)
        logger.info("Wrote %s (%d segments, %d total words)",
                    config["script_fixture"],
                    len(fixture["segments"]),
                    sum(int(s["word_count"]) for s in fixture["segments"]))

        if args.live:
            if not os.getenv("GEMINI_API_KEY"):
                raise RuntimeError("GEMINI_API_KEY must be set for --live capture")
            _live_capture(poi_key, config)


if __name__ == "__main__":
    main()
