"""Narration script validation — 4-gate quality system.

Gate 0: Soft normalization (``normalize_script``) — strip forbidden openers,
        trim trailing sentences when the word count overshoots the profile
        max. Runs BEFORE Gate 1 so the validator never has to hard-reject
        on cosmetic/arithmetic rules a regex or trim can fix.
Gate 1: Structural validation (instant, free)
Gate 2: LLM quality scoring (handled in script_generator.py)
Gate 3: Deduplication check
Gate 4: Pacing validation

Soft-normalize policy (Sprint 08): hard-reject only on structural invariants
(duplicate clips, missing required fields, clip_id not in inventory,
per-segment word count bands, clip count range). Word-count overflow, word-
count under-range, and forbidden openers are normalized in-place or logged
as warnings. See ``<operator-memory-root>/feedback_gates_normalize_not_reject.md``.

Usage:
    from promo.core.script.script_validator import (
        normalize_script, validate_structural, validate_pacing,
    )

    normalize_script(script_dict, profile=..., persona=...)
    validate_structural(script_dict, persona, valid_clip_ids, profile=...)
    validate_pacing(script_dict, target_duration=30.0, wpm=130)
"""

import logging
import re

logger = logging.getLogger(__name__)

from promo.core.format_profiles import PromoFormatProfile, get_promo_format_profile


class ValidationError(Exception):
    """Raised when a script fails validation."""
    pass


# ---------------------------------------------------------------------------
#  Gate 0: Soft normalization (runs BEFORE validate_structural)
# ---------------------------------------------------------------------------

# Sprint 09b C5: aggressive pruning per operator directive. The Sprint 08
# set of five openers was AI-authored bloat — `welcome`, `experience`, and
# `introducing` can legitimately start real travel copy, so hard-stripping
# them was cutting good scripts alongside bad. Keep only the two clearest
# AI-tells: `imagine` (the model's default "let me paint a picture" crutch)
# and `discover` (brochure voice). Widen only when a real failure demands it.
FORBIDDEN_OPENERS = {"imagine", "discover"}

# Matches a trailing terminator (.!?) possibly followed by a closing quote.
_SENTENCE_TERMINATOR = re.compile(r'[.!?]["\')\]]?\s*$')


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences on ``.!?`` boundaries, preserving the
    terminator. Returns a list of non-empty sentence strings with their
    trailing whitespace trimmed.
    """
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _strip_forbidden_opener(segment: dict) -> bool:
    """If the segment's first word is a forbidden opener, strip it in-place
    and re-capitalize the next word. Returns True when a strip happened."""
    text = (segment.get("text") or "").strip()
    if not text:
        return False
    first_word = text.split()[0].lower().rstrip(".,!?;:")
    if first_word not in FORBIDDEN_OPENERS:
        return False
    # Remove the opener token and its trailing punctuation, then re-capitalize.
    remainder = re.sub(r'^\S+[\s,;:]*', '', text, count=1).lstrip()
    if not remainder:
        return False
    remainder = remainder[0].upper() + remainder[1:]
    segment["text"] = remainder
    logger.info(
        "Normalized forbidden opener in segment %s: '%s' stripped",
        segment.get("segment", "?"), first_word,
    )
    return True


def _word_count(text: str) -> int:
    return len(text.split()) if text else 0


def _trim_overflow(
    segments: list[dict], max_words: int, min_words: int,
) -> None:
    """Trim trailing sentences from the LAST segment until total words ≤ max_words.

    Trims by sentence boundary (not word) so the narration always ends on
    sentence-terminating punctuation. Safety clause: if the trim would leave
    ``total < min_words``, the overshoot is kept and a warning is logged —
    better a slightly long script than trimmed-into-garbage.
    """
    total = sum(_word_count(s.get("text", "")) for s in segments)
    if total <= max_words:
        return
    # Work on the last segment (HIGHLIGHTS/CLOSE territory where trim impact is lowest).
    last = segments[-1]
    sentences = _split_sentences(last.get("text", ""))
    if len(sentences) <= 1:
        logger.warning(
            "Normalize: word count %d exceeds max %d but last segment has only %d sentence(s); "
            "keeping overshoot (trimming would destroy segment integrity).",
            total, max_words, len(sentences),
        )
        return
    kept = list(sentences)
    dropped = 0
    while len(kept) > 1:
        candidate_text = " ".join(kept[:-1]).strip()
        candidate_total = (
            sum(_word_count(s.get("text", "")) for s in segments[:-1])
            + _word_count(candidate_text)
        )
        if candidate_total < min_words:
            break
        kept.pop()
        dropped += 1
        if candidate_total <= max_words:
            last["text"] = candidate_text
            last["word_count"] = _word_count(candidate_text)
            logger.info(
                "Normalized word count %d → %d (dropped %d trailing sentence%s from last segment)",
                total, candidate_total, dropped, "s" if dropped != 1 else "",
            )
            return
        # else continue trimming
    # Exited loop without a successful trim: either trim would drop below min_words
    # or ran out of sentences.
    logger.warning(
        "Normalize: overshoot %d > max %d but cannot trim further without falling "
        "below min %d. Keeping original; let pause budget absorb shorter duration.",
        total, max_words, min_words,
    )


def normalize_script(
    script: dict,
    profile: PromoFormatProfile | None = None,
    persona=None,
) -> dict:
    """Run Gate 0 soft normalization in-place.

    - Strips forbidden openers (segment-by-segment); re-capitalizes remainder.
    - Trims trailing sentences from the last segment when total words overshoot
      ``profile.total_words_max``; safety-clause keeps the overshoot when a
      trim would drop the total below ``profile.total_words_min``.
    - Emits a warning when total words fall below ``profile.total_words_min``
      without padding — the pause budget math downstream will absorb the
      shorter duration.

    ``persona`` is accepted for signature symmetry with ``validate_structural``
    but is not currently used here. Returns the mutated script dict.
    """
    profile = profile or get_promo_format_profile(30.0)
    segments = script.get("segments")
    if not segments or not isinstance(segments, list):
        return script

    for seg in segments:
        _strip_forbidden_opener(seg)

    # Recount after opener strip.
    for seg in segments:
        seg["word_count"] = _word_count(seg.get("text", ""))

    _trim_overflow(segments, profile.total_words_max, profile.total_words_min)

    # Re-tally and flag under-range.
    total = sum(_word_count(s.get("text", "")) for s in segments)
    for seg in segments:
        seg["word_count"] = _word_count(seg.get("text", ""))
    script["total_words"] = total
    if total < profile.total_words_min:
        # LONG hard-gates the floor so the retry loop gets another draw.
        # SHORT keeps normalize-not-reject — SHORT has no tail-safety floor
        # and the pause budget math absorbs the shorter narration fine.
        if profile.mode == "long":
            raise ValidationError(
                f"total words {total} below LONG floor "
                f"{profile.total_words_min} — retrying for a longer draw"
            )
        logger.warning(
            "Normalize: total words %d below profile min %d. Proceeding without "
            "padding — pause budget math will absorb the shorter duration.",
            total, profile.total_words_min,
        )
    return script


# ---------------------------------------------------------------------------
#  Gate 1: Structural Validation
# ---------------------------------------------------------------------------

# Canonical AI-tell stopwords.
#
# Sprint 08 cut the AI-authored 30+ list down to 8. Sprint 09b C5 goes
# further: the remaining 8 still included words that appear in real
# travel writing (`paradise`, `curated`, `seamlessly`, `bespoke`,
# `unleash`). Operator directive — we do not yet know which words truly
# identify AI output; give Gemini room. Keep only the four with the
# strongest generated-text signal (`nestled`, `tapestry`, `delve` are
# notorious; `bespoke` borderline but kept as a canary). Re-widen
# only when a real failure demonstrates a specific word needs banning.
#
# Mirrors promo/personas/third_person_promo.yaml `forbidden_phrases`
# (words-slice). Keep in sync manually; no structural import between
# validator and persona by design.
BANNED_WORDS = {
    "nestled",
    "tapestry",
    "delve",
    "bespoke",
}

# Sprint 09b C5: also pruned. `welcome to`, `discover the`, `experience
# the`, `more than just`, `redefine` are too generic — real copy uses
# them. Keep only the two unambiguous template cliches.
BANNED_PHRASES = [
    "imagine a place",
    "where luxury meets",
]


def _check_banned_words(text: str) -> list[str]:
    """Check for banned words/phrases in text. Returns list of violations."""
    lower = text.lower()
    violations = []

    for phrase in BANNED_PHRASES:
        if phrase in lower:
            violations.append(f"banned phrase: '{phrase}'")

    for word in BANNED_WORDS:
        # Match whole word only (not substrings)
        if re.search(rf'\b{re.escape(word)}\b', lower):
            # Skip if already caught as a phrase
            if not any(word in v for v in violations):
                violations.append(f"banned word: '{word}'")

    return violations


def validate_structural(
    script: dict,
    persona=None,
    valid_clip_ids: set = None,
    profile: PromoFormatProfile | None = None,
) -> None:
    """Run Gate 1 structural validation. Raises ValidationError on failure.

    Checks:
    - Expected segment count for the chosen format
    - Total word count in the profile range
    - Per-segment word count in the profile range
    - No banned words or phrases
    - No forbidden openers
    - Each segment has a 'clips' array with 1-4 entries
    - All clip_ids exist in valid set (if provided)
    - No clip reuse across segments
    - Total clips in the profile range
    - Segments have required fields
    """
    profile = profile or get_promo_format_profile(30.0)
    errors = []

    # Check segments exist
    segments = script.get("segments")
    if not segments or not isinstance(segments, list):
        raise ValidationError("Missing or invalid 'segments' field")

    # Check segment count
    if len(segments) != profile.segment_count:
        errors.append(f"expected {profile.segment_count} segments, got {len(segments)}")

    # Check required fields per segment
    for i, seg in enumerate(segments):
        for fld in ("segment", "text"):
            if fld not in seg:
                errors.append(f"segment {i+1} missing field: '{fld}'")
        # Must have 'clips' array (new format) or 'clip_id' (legacy single-clip)
        if "clips" not in seg and "clip_id" not in seg:
            errors.append(f"segment {i+1} missing 'clips' array")

    if errors:
        raise ValidationError("; ".join(errors))

    # Normalize legacy single-clip format to multi-clip
    for seg in segments:
        if "clips" not in seg and "clip_id" in seg:
            seg["clips"] = [{"clip_id": seg.pop("clip_id"), "cut_after": ""}]

    # Recount words (don't trust LLM's count)
    for seg in segments:
        actual_wc = len(seg["text"].split())
        seg["word_count"] = actual_wc  # Fix in place

    total_words = sum(s["word_count"] for s in segments)
    script["total_words"] = total_words  # Fix in place

    # Total word count is owned by normalize_script (Gate 0) under the
    # normalize-not-reject policy. Under-range is acceptable — the pause
    # budget math absorbs the shorter narration. Overshoots are trimmed in
    # Gate 0 before reaching here.
    if total_words < profile.total_words_min:
        logger.info(
            "Total words %d below profile min %d (accepted — normalize-not-reject policy)",
            total_words, profile.total_words_min,
        )

    for seg in segments:
        wc = seg["word_count"]
        if wc < profile.per_segment_min or wc > profile.per_segment_max:
            errors.append(
                f"segment {seg['segment']} has {wc} words "
                f"(need {profile.per_segment_min}-{profile.per_segment_max})"
            )

    # Clips-per-segment checks
    all_used_clips = []
    for seg in segments:
        clips = seg.get("clips", [])
        seg_num = seg.get("segment", "?")
        if not clips:
            errors.append(f"segment {seg_num} has no clips")
            continue
        if len(clips) > 4:
            errors.append(f"segment {seg_num} has {len(clips)} clips (max 4)")
        for c in clips:
            cid = str(c.get("clip_id", ""))
            all_used_clips.append(cid)

    # Total clips check
    total_clips = len(all_used_clips)
    script["total_clips"] = total_clips
    if total_clips < profile.total_clips_min or total_clips > profile.total_clips_max:
        errors.append(
            f"total clips {total_clips} outside range "
            f"{profile.total_clips_min}-{profile.total_clips_max}"
        )

    # Check for duplicate clip usage across all segments
    if len(all_used_clips) != len(set(all_used_clips)):
        seen = set()
        dupes = []
        for c in all_used_clips:
            if c in seen:
                dupes.append(c)
            seen.add(c)
        errors.append(f"duplicate clip usage: {dupes}")

    # Banned words check (structural hard-reject kept — see BANNED_WORDS surgery in Sprint 08)
    full_text = " ".join(s["text"] for s in segments)
    banned_violations = _check_banned_words(full_text)
    errors.extend(banned_violations)

    # Forbidden-opener enforcement moved to Gate 0 (normalize_script).

    # Clip ID validation against inventory
    if valid_clip_ids:
        for cid in all_used_clips:
            if cid not in valid_clip_ids:
                errors.append(
                    f"invalid clip_id '{cid}' (valid: {sorted(valid_clip_ids)})"
                )

    if errors:
        raise ValidationError("; ".join(errors))

    logger.debug(
        "Structural validation passed: %d words, %d segments, %d clips",
        total_words, profile.segment_count, total_clips,
    )


# ---------------------------------------------------------------------------
#  Sprint 10: Pass-1 (script-only) validation
# ---------------------------------------------------------------------------
#
# Gemini #1 writes only narration text + pause_weight. Clip assignment is
# the deterministic assign stage's job (``packer`` + ``clip_assignment_validator``).
# ``validate_script_only`` is the pass-1 gate — it checks text shape and
# pause_weight validity and deliberately does NOT look at clips / clip_id /
# cut_after. Callers still run ``normalize_script`` (Gate 0) first.

def validate_script_only(
    script: dict,
    persona=None,
    profile: PromoFormatProfile | None = None,
) -> None:
    """Pass-1 validator for Sprint 10 two-pass Gemini.

    Raises ``ValidationError`` on:
      - missing / non-list ``segments``
      - wrong segment count vs. profile
      - per-segment ``word_count`` outside the profile's per-segment band
      - ``pause_weight`` missing or not in ``{1, 2, 3}`` on any NON-last
        segment (the last segment's ``pause_weight`` is ignored — the
        Gemini #1 prompt explicitly tells the model so)
      - banned words / phrases anywhere in the narration

    Does NOT check clip fields. Mutates the script dict in place to refresh
    per-segment ``word_count`` and ``total_words`` (same behavior as the
    pre-Sprint-10 ``validate_structural``).
    """
    profile = profile or get_promo_format_profile(30.0)
    errors: list[str] = []

    segments = script.get("segments")
    if not segments or not isinstance(segments, list):
        raise ValidationError("Missing or invalid 'segments' field")

    if len(segments) != profile.segment_count:
        errors.append(f"expected {profile.segment_count} segments, got {len(segments)}")

    for i, seg in enumerate(segments):
        for fld in ("segment", "text"):
            if fld not in seg:
                errors.append(f"segment {i + 1} missing field: '{fld}'")

        text = seg.get("text", "") or ""
        actual_wc = len(text.split())
        seg["word_count"] = actual_wc
        if actual_wc < profile.per_segment_min or actual_wc > profile.per_segment_max:
            seg_num = seg.get("segment", i + 1)
            errors.append(
                f"segment {seg_num} has {actual_wc} words "
                f"(need {profile.per_segment_min}-{profile.per_segment_max})"
            )

        is_last = (i == len(segments) - 1)
        if not is_last:
            pw = seg.get("pause_weight")
            if pw not in (1, 2, 3):
                seg_num = seg.get("segment", i + 1)
                errors.append(
                    f"segment {seg_num} pause_weight {pw!r} invalid; must be 1, 2, or 3"
                )

    total_words = sum(s.get("word_count", 0) for s in segments)
    script["total_words"] = total_words

    if total_words < profile.total_words_min:
        logger.info(
            "Total words %d below profile min %d (accepted — normalize-not-reject policy)",
            total_words, profile.total_words_min,
        )

    full_text = " ".join(s.get("text", "") for s in segments)
    errors.extend(_check_banned_words(full_text))

    if errors:
        raise ValidationError("; ".join(errors))

    logger.debug(
        "Pass-1 (script-only) validation passed: %d words, %d segments",
        total_words, profile.segment_count,
    )


# ---------------------------------------------------------------------------
#  Gate 4: Pacing Validation
# ---------------------------------------------------------------------------

def validate_pacing(
    script: dict,
    target_duration: float = 30.0,
    wpm: int = 130,
    profile: PromoFormatProfile | None = None,
) -> list[str]:
    """Validate pacing constraints. Returns list of warnings (not errors).

    Checks:
    - Effective narration density is in the profile's acceptable range
    - Total narration leaves some silence
    - Word count generally decreases across segments (soft check)
    """
    profile = profile or get_promo_format_profile(target_duration)
    target_duration = float(target_duration or profile.target_duration_sec)
    warnings = []
    segments = script.get("segments", [])
    total_words = sum(s.get("word_count", 0) for s in segments)

    # Estimated narration duration at given WPM
    narration_seconds = (total_words / wpm) * 60
    narration_ratio = narration_seconds / target_duration if target_duration > 0 else 1.0

    effective_wpm = (total_words / target_duration) * 60 if target_duration > 0 else 0

    if effective_wpm < profile.min_effective_wpm:
        warnings.append(
            f"pacing too slow: {effective_wpm:.0f} WPM "
            f"(target {profile.min_effective_wpm}-{profile.max_effective_wpm} for {profile.mode})"
        )
    elif effective_wpm > profile.max_effective_wpm:
        warnings.append(
            f"pacing too fast: {effective_wpm:.0f} WPM "
            f"(target {profile.min_effective_wpm}-{profile.max_effective_wpm} for {profile.mode})"
        )

    if narration_ratio > profile.max_narration_ratio:
        warnings.append(
            f"narration fills {narration_ratio*100:.0f}% of video "
            f"(max {profile.max_narration_ratio*100:.0f}% for {profile.mode})"
        )

    # Soft check: word count should generally decrease
    word_counts = [s.get("word_count", 0) for s in segments]
    if len(word_counts) == 4:
        if word_counts[3] > word_counts[0] + 3:
            warnings.append(
                f"segment 4 ({word_counts[3]}w) is longer than segment 1 ({word_counts[0]}w) "
                "— emotional arc suggests decreasing word count"
            )

    for w in warnings:
        logger.info("Pacing warning: %s", w)

    return warnings
