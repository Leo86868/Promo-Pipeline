"""Shared exception types for the promo pipeline.

Sprint 09a introduced these to replace silent fallbacks with loud failures
in paths that previously logged-and-continued into surfaces the operator
can't recover from (freeze-prone renders, silent BGM fallback).
"""

from __future__ import annotations


class FreezeWouldOccurError(RuntimeError):
    """Raised when clip binding cannot cover the narration timeline without
    letting a clip freeze past its source footage and the unused clip pool
    is exhausted.

    Sprint 09a M-006 — operator directive: the pipeline must never render
    a video with freeze. The prior Sprint 08.5 behavior logged
    ``freeze will occur`` and continued into a freeze-prone render; this
    error replaces that path. Callers that can recover (rerun with a
    larger pool, pick a different POI, adjust target duration) may catch
    it; ``full_pipeline`` surfaces it by returning ``False`` without
    writing an mp4.
    """


class NoSuitableBGMError(RuntimeError):
    """Raised when ``_discover_bgm_files`` cannot find any BGM track
    meeting the minimum-duration constraint and no explicit BGM was
    provided by the operator.

    Sprint 09a M-005 — the prior silent fallback to the unfiltered pool
    defeated the filter's purpose (the short BGM the filter rejected
    would still get picked and leave a silent tail). Callers surface
    this by returning ``False`` instead of rendering a silent-tail video.
    """


class ClipAssignmentError(RuntimeError):
    """Raised by ``clip_assigner.assign_clips`` when Gemini #2's output
    violates the hard constraint ``source_duration - trim_start ≥
    display_span_sec`` for any phrase (TOL=0.05s).

    Sprint 10 C2 — the two-pass Gemini architecture's correctness guarantee:
    Gemini #2 must never emit an assignment that would freeze its clip
    before the narration phrase finishes. On violation, this error is
    raised naming the first failing segment + phrase indices and the
    arithmetic shortfall so the F3 retry can issue a targeted
    "tighten segment X" hint to Gemini #1.

    Attributes:
        segment_index: 1-indexed segment number that failed.
        phrase_index: 1-indexed phrase number within the segment that failed.
        required_span: The phrase's computed ``display_span_sec``.
        actual_max_usable: ``source_duration - trim_start`` for the offending
            assignment.
        clip_id: The clip_id Gemini #2 assigned to the failing phrase.
    """

    def __init__(
        self,
        segment_index: int,
        phrase_index: int,
        required_span: float,
        actual_max_usable: float,
        clip_id: str,
    ):
        self.segment_index = segment_index
        self.phrase_index = phrase_index
        self.required_span = float(required_span)
        self.actual_max_usable = float(actual_max_usable)
        self.clip_id = clip_id
        super().__init__(
            f"segment {segment_index} phrase {phrase_index} (clip {clip_id}) "
            f"requires {required_span:.2f}s visual but usable footage is "
            f"{actual_max_usable:.2f}s (shortfall "
            f"{required_span - actual_max_usable:.2f}s)"
        )


class ForcedAlignmentError(RuntimeError):
    """Raised by ``forced_aligner.align_words`` when MMS_FA cannot confidently
    align a script token against the input audio.

    Sprint TTS-Migration Phase 3 (AC7) — the L-003 guard lifted from the
    spike: when a token's avg forced-alignment score falls below the
    confidence threshold, contraction of the output list is forbidden and
    this error is raised naming the offending token and its index in
    ``script_tokens``. Callers that cannot recover surface it by returning
    ``False`` / aborting the narration; no silent drop.

    Attributes:
        token: The offending script token (preserves caller's original form).
        position: 0-indexed position of the token within the caller's
            ``script_tokens`` list.
        reason: One-line diagnostic — either "avg score {score:.3f} below
            threshold {thr:.2f}" or "MMS_FA returned empty span list" /
            "aligner raised: {exc}" depending on failure mode.
    """

    def __init__(self, token: str, position: int, reason: str):
        self.token = token
        self.position = position
        self.reason = reason
        super().__init__(
            f"MMS_FA could not align token {token!r} at position {position}: {reason}"
        )


class MimoAnalysisError(RuntimeError):
    """Raised when MiMo clip analysis fails for any clip after the
    retry budget is exhausted.

    Sprint 09b C4 (Codex #6) — the prior Sprint 08 behavior caught all
    exceptions in ``analyze_single_clip`` and substituted a stub
    ``{"scene_description": "analysis failed"}`` entry so the pipeline
    continued with an empty description on the failing clip. Gemini
    would then assign the unlabeled clip to a narration phrase and the
    render would ship with a clip whose content the script didn't
    actually match.

    After Sprint 09b C4, ``analyze_single_clip`` raises this error on
    final failure (after the retry budget is exhausted) and
    ``analyze_clips`` propagates it — the first failure aborts the
    concurrent pool and the pipeline exits non-zero with an actionable
    error message naming the clip.

    Paired with a retry budget bump from ``max_retries=1`` to
    ``max_retries=3`` in the same commit, so transient OpenRouter 5xx
    /timeouts are absorbed before the strict raise fires.
    """

    def __init__(self, clip_id: str, clip_path: str, cause: Exception | None = None):
        self.clip_id = clip_id
        self.clip_path = clip_path
        self.cause = cause
        cause_text = f": {cause}" if cause is not None else ""
        super().__init__(
            f"MiMo analysis failed for clip {clip_id} ({clip_path}){cause_text}"
        )
