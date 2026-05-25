"""Gemini #2 client — prompt assembly + API call + JSON parse.

Extracted from ``clip_assigner.py`` (Sprint S2b). The three pieces are
fused into one file because they live and die together: the prompt
builder feeds exactly one consumer (``_call_gemini2``), and the parser
exists to consume that call's response. Splitting them across files
would add an inter-file seam without buying change-locality, since a
prompt-wording change typically also tunes ``max_output_tokens`` /
sampling temp.

Sprint Arsenal Externalization (Commit 4): the Gemini #2 prompt body
lives in ``promo/arsenal/system_prompts/gemini2_assign_v1.md``; this
module formats the per-call substitutions and calls ``string.Template``.
"""

from __future__ import annotations

import json
import re
from string import Template
from typing import Any

from promo.core import arsenal_loader
from promo.core.llm.retry import retry_with_backoff
from promo.core.model_adapters.gemini import (
    generate_content_text,
    resolve_gemini_model,
)
from promo.core.format_profiles import PromoFormatProfile
from promo.core.schema import (
    ClipAssignment,
    ClipMetadata,
    Script,
    WordTimestamp,
)
from promo.core.script.script_prompt_builder import format_clip_inventory


# ---------------------------------------------------------------------------
#  Gemini #2 prompt
# ---------------------------------------------------------------------------

def _format_phrase_timing_block(
    script: Script,
    word_timestamps: list[WordTimestamp],
    pause_after_ms_per_segment: list[int],
    profile: PromoFormatProfile,
) -> str:
    """Render the narration-with-timing block Gemini #2 reads to plan
    phrase boundaries. Word indices here are the same GLOBAL indices
    Gemini #2 must echo back in its response.
    """
    lines: list[str] = []
    cursor = 0
    segments = script.get("segments", [])
    for seg_i, seg in enumerate(segments):
        text = seg.get("text", "") or ""
        seg_words = text.split()
        seg_pos = seg_i  # 0-indexed
        seg_num = seg.get("segment", seg_i + 1)
        pause_ms = 0
        if seg_pos < len(pause_after_ms_per_segment):
            raw_pause = pause_after_ms_per_segment[seg_pos]
            try:
                pause_ms = int(raw_pause) if raw_pause is not None else 0
            except (TypeError, ValueError):
                pause_ms = 0
        # Per-segment phrase count range from profile. Neither IndexError
        # (script has more segments than the profile declares) nor
        # AttributeError (SegmentPlan drifted off its ``clip_range``
        # contract) is silently absorbed: either is a real data
        # mismatch that would render a misleading "1-4 clips" prompt
        # while Gemini #2 is planning against different bounds. Sprint
        # 10b post-close audit-fix 2026-04-18 closed the AttributeError
        # axis; this block also raises on IndexError for the same
        # reason — letting the caller see the failure surface instead
        # of a silent fallback.
        clip_range_display = profile.segment_plans[seg_pos].clip_range_display

        is_last_segment = (seg_pos == len(segments) - 1)
        trailing_desc = (
            f"{pause_ms}ms pause follows" if (pause_ms > 0 and not is_last_segment)
            else "no trailing pause"
        )
        lines.append(
            f"Segment {seg_num} ({clip_range_display}; {trailing_desc}):"
        )
        for word_i, word in enumerate(seg_words):
            global_idx = cursor + word_i
            if global_idx < len(word_timestamps):
                wt = word_timestamps[global_idx]
                start = float(wt.get("start", 0.0))
                end = float(wt.get("end", 0.0))
                lines.append(
                    f"  [{global_idx}] {word!r:<20s} start={start:.3f} end={end:.3f}"
                )
            else:
                # Defensive — TTS did not produce a timestamp for this index.
                lines.append(f"  [{global_idx}] {word!r} (no timestamp)")
        cursor += len(seg_words)
    return "\n".join(lines)


def _build_gemini2_prompt(
    script: Script,
    word_timestamps: list[WordTimestamp],
    pause_after_ms_per_segment: list[int],
    clips_metadata: list[ClipMetadata],
    variant_index: int,
    profile: PromoFormatProfile,
    *,
    target_duration_sec: float | None = None,
) -> str:
    """Construct the Gemini #2 clip-assignment prompt.

    Audit-fix L-011 (2026-04-18): ``target_duration_sec`` is accepted
    explicitly so the caller (``assign_clips``) can pass the same value
    it already resolved for profile selection, avoiding a redundant
    re-derivation that could silently diverge if the defaults differ.
    Falls back to ``script["target_duration_sec"] or 65.0`` for
    backwards compat with any caller that doesn't pass it.
    """
    poi_name = script.get("poi_name", "?")
    location = script.get("location", "?")
    if target_duration_sec is None:
        target_duration_sec = float(script.get("target_duration_sec") or 65.0)
    else:
        target_duration_sec = float(target_duration_sec)
    timing_block = _format_phrase_timing_block(
        script, word_timestamps, pause_after_ms_per_segment, profile,
    )
    inventory = format_clip_inventory(clips_metadata, duration_precision=2)

    # Precomputed for prompt clarity — eliminates Gemini arithmetic slips
    # on the last-phrase edge case. Sprint 10.5 C1.2: the last phrase's
    # ceiling is narration_end, not final_display_end; the renderer's
    # bridge mechanism handles the target-extension buffer.
    narration_end = (
        float(word_timestamps[-1].get("end", 0.0)) if word_timestamps else 0.0
    )
    pool_max_source_dur = max(
        (float(c.get("source_duration_sec") or 0.0) for c in clips_metadata),
        default=0.0,
    )

    segments = script.get("segments", [])
    seg_range_lines: list[str] = []
    for seg_i, seg in enumerate(segments):
        # Neither IndexError nor AttributeError caught — see the twin
        # block in :func:`_format_phrase_timing_block` for the rationale
        # (Sprint 10b post-close audit-fix 2026-04-18).
        lo, hi = profile.segment_plans[seg_i].clip_range
        seg_range_lines.append(
            f"  Segment {seg.get('segment', seg_i + 1)}: {lo}-{hi} phrases (and thus {lo}-{hi} clips)"
        )
    seg_ranges = "\n".join(seg_range_lines)

    # Per-segment global word-index boundaries — tells Gemini exactly
    # which global word indices belong to each segment so peek-ahead
    # across segment boundaries is unambiguous.
    # Audit-fix L-008 (2026-04-18): use len(text.split()) — the same
    # source the enforcer's ``segment_word_ranges`` uses
    # (``_enforce_hard_constraint_and_enrich`` line ~191) — so the
    # prompt's boundary table cannot silently diverge from what the
    # enforcer validates against. seg["word_count"] is an author-declared
    # field that may drift from the actual tokenization of seg["text"].
    seg_word_boundaries: list[str] = []
    cursor = 0
    for seg_i, seg in enumerate(segments):
        text = seg.get("text") or ""
        n = len(text.split())
        if n <= 0:
            continue
        seg_word_boundaries.append(
            f"  Segment {seg.get('segment', seg_i + 1)}: words [{cursor}..{cursor + n - 1}]"
        )
        cursor += n
    seg_word_boundaries_str = "\n".join(seg_word_boundaries)

    # Sprint Arsenal Externalization (Commit 4): the prompt body lives in
    # ``promo/arsenal/system_prompts/gemini2_assign_v1.md``. Caller
    # pre-formats the 3 floats (`target_duration_sec`, `narration_end`,
    # `pool_max_source_dur`) so the MD template can stay flat
    # ``$identifier`` substitution — `string.Template` doesn't honour
    # f-string `:.3f` formatting natively. Sprint 10b post-close audit
    # arithmetic-slip regression test pins the 5 invariant substrings
    # below (PRECOMPUTED CONSTANTS / ARITHMETIC CHECK / "the last
    # phrase's constraint uses the last word's end, NOT
    # target_duration_sec") — these came across byte-identically in the
    # MD migration.
    template = Template(arsenal_loader.load_system_prompt("gemini2_assign"))
    return template.substitute(
        poi_name=poi_name,
        location=location,
        target_duration_sec=f"{target_duration_sec:.3f}",
        narration_end=f"{narration_end:.3f}",
        pool_max_source_dur=f"{pool_max_source_dur:.3f}",
        seg_word_boundaries_str=seg_word_boundaries_str,
        timing_block=timing_block,
        variant_index=variant_index,
        seg_ranges=seg_ranges,
        inventory=inventory,
    )


# ---------------------------------------------------------------------------
#  Gemini #2 response parsing + API call
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", re.DOTALL)


def _parse_gemini2_json(text: str) -> list[ClipAssignment]:
    """Parse Gemini #2's response text as JSON. The expected shape is a
    top-level list of phrase-assignment dicts; some Gemini runs wrap the
    list under a key (``assignments`` / ``phrases`` / ``clips``), which
    is unwrapped here. Strips ```json``` fences first.

    Raises :class:`ValueError` on malformed JSON or unrecognizable shape.
    Distinct from :func:`promo.core.llm.json_response.parse_json_response` because
    that helper forbids top-level lists by design.
    """
    cleaned = text.strip()
    match = _FENCE_RE.match(cleaned)
    if match:
        cleaned = match.group(1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Gemini #2 JSON parse failure: {exc}. "
            f"Response (first 200 chars): {text[:200]!r}"
        ) from exc
    if isinstance(parsed, dict):
        for candidate_key in ("assignments", "phrases", "clips"):
            val = parsed.get(candidate_key)
            if isinstance(val, list):
                return val
        raise ValueError(
            f"Gemini #2 returned a dict without a recognizable list key; "
            f"keys={list(parsed.keys())}"
        )
    if not isinstance(parsed, list):
        raise ValueError(
            f"Gemini #2 response is not a list: {type(parsed).__name__}"
        )
    return parsed


def _call_gemini2(prompt: str) -> list[ClipAssignment]:
    """Call Gemini with the clip-assignment prompt. Returns parsed JSON.

    Separated out for test-patchability (tests patch this at the module
    level to inject fixture responses without touching the real API).
    """
    model = resolve_gemini_model(log_context="Gemini #2")

    def _call() -> Any:
        text = generate_content_text(
            model,
            prompt,
            generation_config={  # type: ignore[arg-type]
                "temperature": 0.35,
                "top_p": 0.9,
                # Raised from 10000 — Sprint 10.5 C1.1 prompt-hardening adds
                # a precomputed-constants block + segment-boundary table +
                # arithmetic-check step that makes Gemini 2.5-pro do more
                # thinking before emitting JSON. Observed first re-capture
                # crashed with finish_reason=2 (MAX_TOKENS). Per the
                # project's "Gemini token budget not a constraint" memory.
                "max_output_tokens": 32000,
            },
        )
        return _parse_gemini2_json(text)

    return retry_with_backoff(_call, max_retries=2, base_delay=2.0)
