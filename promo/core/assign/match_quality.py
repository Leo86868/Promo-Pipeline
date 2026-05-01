"""Keyword-overlap observability for clip-narration matching.

Sprint 08 emits a ``match_quality.json`` sidecar per render so Sprint 09
(embedding-based RAG matching) has a concrete baseline to diff against.
Overlap is a naive but consistent proxy: fraction of narration-phrase
words that appear in the clip's scene_description, after lowercasing and
stopword removal.

No new dependency — stopword list is an inline constant here.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ~40 high-frequency English stopwords — enough to strip the noise from
# both narration copy and MiMo's scene_description text without pulling
# in NLTK. If this list ever needs to expand, do it inline here.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at",
    "be", "been", "but", "by",
    "do", "does", "did",
    "for", "from",
    "has", "have", "had", "he", "her", "his",
    "i", "if", "in", "into", "is", "it", "its",
    "me", "my",
    "of", "on", "or", "our",
    "she",
    "that", "the", "their", "them", "they", "this", "to",
    "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "will", "with",
    "you", "your", "yours",
    "also", "just", "not", "no", "than", "then", "there", "these",
    "up", "down", "off",
})

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> list[str]:
    lower = (text or "").lower()
    return [t for t in _TOKEN_RE.findall(lower) if t and t not in _STOPWORDS]


def compute_overlap_score(narration_phrase: str, scene_description: str) -> float:
    """Return ``|narration ∩ scene| / |narration|`` over content tokens.

    Returns 0.0 when the narration tokenizes to nothing (so the metric is
    defined even on empty or all-stopword input).
    """
    narration_tokens = set(_tokenize(narration_phrase))
    scene_tokens = set(_tokenize(scene_description))
    if not narration_tokens:
        return 0.0
    hits = len(narration_tokens & scene_tokens)
    return round(hits / len(narration_tokens), 3)


def build_match_quality_entries(
    assignments: list[dict],
    clips_metadata: list[dict],
    word_timestamps: list[dict],
    variant_index: int | None = None,
) -> list[dict]:
    """Return per-phrase observability entries for one variant's assignments.

    Sprint 10 C5: ``assignments`` is the word-idx per-phrase list produced
    by :func:`promo.core.assign.clip_assigner.assign_clips`. Each entry carries
    ``segment``, ``clip_id``, ``start_word_idx``, and ``end_word_idx``;
    the narration phrase is reconstructed by slicing ``word_timestamps``
    on those indices. The pre-Sprint-10 ``_split_by_cut_after`` helper
    retired with this signature change.

    Entry shape (unchanged from Sprint 08 AC20):
        {variant_index, segment_idx, clip_id, narration_phrase,
         scene_description, overlap_score, picked_category}
    """
    # Drop entries whose id field is absent or empty so the `""` key
    # cannot collide with an assignment clip_id that lstrips down to
    # "" (logic-auditor L-002 fix: previously `clip_id="0000"` aliased
    # to the missing-id sentinel entry).
    meta_by_id: dict[str, dict] = {}
    for m in clips_metadata:
        key = str(m.get("id", ""))
        if key:
            meta_by_id[key] = m
    entries: list[dict] = []
    n_words = len(word_timestamps)
    for a in assignments:
        seg_idx = int(a.get("segment") or 0)
        clip_id = str(a.get("clip_id") or "")
        try:
            start_idx = int(a.get("start_word_idx", 0))
            end_idx = int(a.get("end_word_idx", -1))
        except (TypeError, ValueError):
            start_idx, end_idx = 0, -1
        if start_idx < 0 or end_idx < start_idx or end_idx >= n_words:
            phrase = ""
        else:
            phrase = " ".join(
                str(wt.get("word", "")) for wt in word_timestamps[start_idx:end_idx + 1]
            ).strip()

        # Allow lookup by either the raw id Gemini emitted or its
        # zero-padded / zero-stripped forms — all three are plausible
        # shapes on real sidecars. The fallback is skipped when the
        # stripped form is empty to avoid the "" collision noted above.
        stripped = clip_id.lstrip("0")
        meta = (
            meta_by_id.get(clip_id)
            or (meta_by_id.get(stripped) if stripped else None)
            or meta_by_id.get(clip_id.zfill(4))
            or {}
        )
        scene = meta.get("scene_description") or ""
        category = meta.get("category") or "unknown"
        entry = {
            "segment_idx": seg_idx,
            "clip_id": clip_id,
            "narration_phrase": phrase,
            "scene_description": scene,
            "overlap_score": compute_overlap_score(phrase, scene),
            "picked_category": category,
        }
        if variant_index is not None:
            entry["variant_index"] = variant_index
        entries.append(entry)
    return entries
