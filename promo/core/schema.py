"""Cross-module TypedDict schema for pgc-pipeline payloads.

Sprint 14 item (f) — introduces typed shapes for dict payloads that flow
between the six named core modules (``clip_assigner``, ``script_generator``,
``tts_engine``, ``remotion_renderer``, ``clip_embedder``, ``clip_retriever``).

These TypedDicts replace bare ``list[dict]`` / ``dict[str, Any]`` return
types on public function signatures. Behavior is byte-identical — these
are annotation-only; Python does not enforce TypedDicts at runtime.

Fields marked ``NotRequired`` are present in some payloads and absent in
others depending on the call site (e.g. fixture vs full pipeline). Fields
marked required must be present in every payload of that type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NotRequired, TypedDict


# ---------------------------------------------------------------------------
#  Frozen dataclasses shared across format / script modules
#
#  Sprint Arsenal Externalization (Commit 0) — relocated here from
#  :mod:`promo.core.format_profiles` (``SegmentPlan``, ``PromoFormatProfile``)
#  and :mod:`promo.core.script.script_generator` (``NarratorPersona``) so
#  :mod:`promo.core.arsenal_loader` (added in Commit 1) can return typed
#  values without recreating an import cycle through the originating
#  modules. 0-behaviour-change move; the originating modules now re-export
#  these names from here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentPlan:
    label: str
    approx_words: int
    min_clips: int
    max_clips: int
    guidance: str
    preferred_categories: tuple[str, ...] = ()

    @property
    def clip_range(self) -> tuple[int, int]:
        """The ``(min_clips, max_clips)`` tuple the Gemini #2 prompt
        and clip_assigner enforcement layer consume.

        Exposed as a property so callers can unpack it without reaching
        into each field separately and so the pair stays in lockstep —
        changes to either bound flow through here without a shotgun
        edit at every call site.
        """
        return (self.min_clips, self.max_clips)

    @property
    def clip_range_display(self) -> str:
        """Human-readable range rendered into the Gemini prompt."""
        if self.min_clips == self.max_clips:
            noun = "clip" if self.min_clips == 1 else "clips"
            return f"{self.min_clips} {noun}"
        return f"{self.min_clips}-{self.max_clips} clips"


@dataclass(frozen=True)
class PromoFormatProfile:
    mode: str
    target_duration_sec: int
    duration_label: str
    segment_count: int
    total_words_min: int
    total_words_max: int
    per_segment_min: int
    per_segment_max: int
    min_clip_pool_size: int
    recommended_clip_pool_size: int
    min_effective_wpm: int
    max_effective_wpm: int
    max_narration_ratio: float
    segment_plans: tuple[SegmentPlan, ...]
    # Sprint Arsenal Externalization (Commit 6a) — skeleton-owned
    # per-mode strings that previously lived as inline conditional
    # branches in `script_generator._build_prompt`. ``sentence_rule``
    # fills the `$sentence_rule` slot of `gemini1_script_v1.md`;
    # ``extra_rules`` is joined with `"\n- "` (caller-side) and dropped
    # into `$extra_rules_block` — empty tuple = empty block. The
    # consumer (script_generator) starts reading these in Commit 6b;
    # in Commit 6a they are dead-read placeholders, so the defaults
    # below absorb any pre-Commit-6a code path that constructs a
    # `PromoFormatProfile` without supplying them. Tuple is the frozen-
    # dataclass-friendly form (vs `list`) per Sprint 16 convention.
    sentence_rule: str = ""
    extra_rules: tuple[str, ...] = ()

    @property
    def total_clips_min(self) -> int:
        return sum(sp.min_clips for sp in self.segment_plans)

    @property
    def total_clips_max(self) -> int:
        return sum(sp.max_clips for sp in self.segment_plans)


@dataclass
class NarratorPersona:
    id: str
    display_name: str
    perspective: str
    wpm: int
    voice_id: str
    system_prompt: str
    tone_keywords: list[str] = field(default_factory=list)
    forbidden_phrases: list[str] = field(default_factory=list)
    forbidden_openers: list[str] = field(default_factory=list)
    example_scripts: list[dict] = field(default_factory=list)
    pause_guidelines: str = ""
    # Sprint TTS-Migration Phase 4: optional per-backend binding. Gemini
    # TTS reads ``gemini.voice`` + ``gemini.style_prompt`` at dispatch time.
    # Older personas without this block keep working — default is empty.
    gemini: dict = field(default_factory=dict)


class WordTimestamp(TypedDict):
    """Per-word alignment returned by ElevenLabs native alignment OR MMS_FA.

    Both backends emit the same shape; ``tts_engine`` guarantees parity.
    """

    word: str
    start: float
    end: float


class SegmentTimestamp(TypedDict):
    """Per-segment alignment derived from word timestamps by
    ``tts_engine._back_allocate_timestamps``."""

    segment: int
    start: float
    end: float
    duration: float
    word_count: NotRequired[int]


class ClipMetadata(TypedDict):
    """Per-clip metadata combining POI-onboarding fields with MiMo V2 Omni analysis.

    ``id`` / ``category`` / ``scene_description`` are required across the
    pipeline; the motion / shot metadata is populated by ``clip_analyzer``
    during MiMo analysis and may be absent in fixture-replay paths.
    ``embedding`` + ``embedding_input`` are added by
    ``clip_embedder.attach_embeddings_to_metadata`` when Sprint 12a/12b
    retrieval is active; absent on the MiMo-only path.
    """

    id: str
    category: str
    scene_description: str
    source_duration_sec: NotRequired[float]
    shot_size: NotRequired[str]
    main_subject: NotRequired[str]
    dominant_motion_phase: NotRequired[str]
    embedding: NotRequired[list[float]]
    embedding_input: NotRequired[str]


class ClipAssignment(TypedDict):
    """Gemini #2 per-phrase clip assignment, post-enrichment.

    Shape matches ``docs/schemas/clip_assignments.md``. Written by
    ``clip_assigner._enforce_hard_constraint_and_enrich``; consumed by
    ``remotion_renderer._bind_clips_to_narration``.
    """

    segment: int
    clip_id: str
    start_word_idx: int
    end_word_idx: int
    trim_start: float
    display_span_sec: float
    source_duration_sec: float


class ScriptSegment(TypedDict):
    """One narration segment emitted by Gemini #1.

    ``text`` + ``pause_weight`` are Gemini #1's output contract.
    ``pause_after_ms`` is added by ``pause_budget.assign_pause_budgets``
    after TTS calibration. ``word_count`` is an author-declared field that
    may drift from the actual tokenization of ``text`` (see
    ``clip_assigner.py`` commentary).
    """

    text: str
    pause_weight: int
    pause_after_ms: NotRequired[int]
    word_count: NotRequired[int]


class Script(TypedDict):
    """Full script payload emitted by ``script_generator.generate_script_variants``.

    ``segments`` is a list of ``ScriptSegment`` — NOT ``list[dict]`` —
    per pre-planner amendment to Sprint 14 item (f).
    """

    segments: list[ScriptSegment]
    total_words: int
    format_mode: str
    target_duration_sec: float
    persona_id: NotRequired[str]
    poi_name: NotRequired[str]
    location: NotRequired[str]
    variant_index: NotRequired[int]
    hook_technique: NotRequired[str]
    unique_detail: NotRequired[str]


class Narration(TypedDict):
    """Return shape of ``tts_engine.generate_narration``.

    The 7 required keys are guaranteed-present by the dispatch seam contract
    (Sprint TTS-Migration N3) — both the ElevenLabs and Gemini backends
    return the same 7-key core, byte-identical shape. The NotRequired keys
    are telemetry / provenance fields populated by the orchestrator.
    """

    audio_path: str
    word_timestamps: list[WordTimestamp]
    segment_timestamps: list[SegmentTimestamp]
    duration: float
    voice_id: str
    voice_key: str | None
    tagged_text: str
    pure_spoken_sec: NotRequired[float]
    inter_segment_silence_sec: NotRequired[float]
    measured_wpm_spoken: NotRequired[float | None]
    backend: NotRequired[str]
    tts_speed: NotRequired[float]
    batch_count: NotRequired[int]
