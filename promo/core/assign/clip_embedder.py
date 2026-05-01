"""Sprint 12a — embedding index for MiMo clip metadata.

Produces a per-POI sidecar at
``material/<slug>/.embedding_cache/text-embedding-3-small-1536-<mimo_prompt_sha1>-v<composition_version>.json``
over the concatenated MiMo fields (``"<scene_description> | <category>"``,
composition v1 — see ``compose_embedding_text``).

The embedding model is OpenAI's ``text-embedding-3-small`` (1536-dim),
accessed via OpenRouter's OpenAI-compatible embeddings endpoint
(``https://openrouter.ai/api/v1/embeddings``, model-id
``openai/text-embedding-3-small``). This mirrors ``clip_analyzer``'s
existing OpenRouter pattern and lets us reuse the ``OPENROUTER_API_KEY``
rather than introduce a second provider key. Vectors are byte-identical
to OpenAI-direct; pricing is the same ($0.02/1M tokens).

Four-axis cache filename invalidation:
  - ``model``   (``text-embedding-3-small``)    — constant per sprint
  - ``dim``     (``1536``)                       — constant per sprint
  - ``mimo_prompt_sha1``                         — bumped by clip_analyzer's
    ``_cache_version_suffix(_ANALYSIS_PROMPT, PROMO_CLIP_MODEL)``; changes
    whenever MiMo prompt or model changes, invalidating in lockstep with
    the MiMo cache
  - ``composition_version`` (``COMPOSITION_VERSION``) — manually bumped when
    ``compose_embedding_text`` changes; protects against silent drift when
    the embedding formula is edited without a model/prompt change

Writes are atomic (``os.replace``). Re-running over the same POI with new
clips only embeds the new clips (incremental). Transient 5xx / timeout is
absorbed by ``retry_with_backoff`` (same helper the rest of the pipeline
uses).

Sprint 12a is zero-integration: this module is called ONLY by
``promo/cli/build_embedding_index.py`` and unit tests. The assigner
wiring lands in Sprint 12b via ``clip_retriever.union_of_top_k``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Optional

from typing import cast

from promo.core.llm.retry import retry_with_backoff
from promo.core.schema import ClipMetadata

logger = logging.getLogger(__name__)


EMBEDDING_MODEL = "text-embedding-3-small"
# Model ID sent to OpenRouter's embeddings endpoint. OpenAI-provider models
# are namespaced as ``openai/<model>``. The sidecar filename uses the bare
# ``EMBEDDING_MODEL`` (no slash, cleaner path). The vectors are identical —
# OpenRouter proxies through to OpenAI at the same pricing.
EMBEDDING_MODEL_API_ID = "openai/text-embedding-3-small"
EMBEDDING_DIM = 1536
EMBEDDING_API_URL = "https://openrouter.ai/api/v1/embeddings"
CACHE_DIR_NAME = ".embedding_cache"

# Bump manually when ``compose_embedding_text`` changes. The MiMo-prompt SHA1
# digests only the MiMo prompt + model (not this module's composition
# function), so a formula change would otherwise write new vectors under the
# old filename — a silent-drift risk. This 4th axis makes the invalidation
# explicit: any composition change gets a fresh sidecar filename, and the
# old one is orphaned rather than partially-overwritten.
# v1 (Sprint 12a): ``"<scene_description> | <category>"``
#   — dominant_motion_phase dropped after advisor logical-correctness review
#     (temporal metadata, not content; ~0 semantic overlap with phrase queries).
COMPOSITION_VERSION = 1


def _get_api_key() -> str:
    from promo.core.config import openrouter_api_key

    return openrouter_api_key()


def current_mimo_prompt_sha1() -> str:
    """Return the 8-hex MiMo-cache version suffix for the active prompt + model.

    Imported lazily so tests can monkeypatch ``clip_analyzer._ANALYSIS_PROMPT``
    or ``PROMO_CLIP_MODEL`` and see a fresh digest.
    """
    from promo.core.analyze import clip_analyzer
    from promo.core.config import clip_model as _clip_model
    resolved_model = _clip_model()
    return clip_analyzer._cache_version_suffix(
        clip_analyzer._ANALYSIS_PROMPT, resolved_model,
    )


def sidecar_filename(
    mimo_prompt_sha1: str, composition_version: int = COMPOSITION_VERSION,
) -> str:
    return (
        f"{EMBEDDING_MODEL}-{EMBEDDING_DIM}-{mimo_prompt_sha1}"
        f"-v{composition_version}.json"
    )


def sidecar_path(
    cache_dir: str,
    mimo_prompt_sha1: str,
    composition_version: int = COMPOSITION_VERSION,
) -> str:
    return os.path.join(
        cache_dir, sidecar_filename(mimo_prompt_sha1, composition_version),
    )


def compose_embedding_text(clip: ClipMetadata) -> str:
    """Concatenate the MiMo fields that drive retrieval similarity.

    Composition v1: ``"<scene_description> | <category>"``.
    ``dominant_motion_phase`` is intentionally excluded — it is timeline
    metadata (which third of the clip has peak motion), not content, and
    adding the bare token "early|middle|late" dilutes the vector against
    narration phrases that never reference it. If this formula changes,
    bump ``COMPOSITION_VERSION`` in the same edit.
    """
    sd = (clip.get("scene_description") or "").strip()
    cat = (clip.get("category") or "").strip()
    return f"{sd} | {cat}"


def _load_sidecar(path: str) -> Optional[dict]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_sidecar(path: str, payload: dict) -> None:
    """Atomic write: tmp in the same dir, then ``os.replace``."""
    cache_dir = os.path.dirname(path)
    os.makedirs(cache_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="embed.", suffix=".json.tmp", dir=cache_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _post_embeddings(inputs: list[str], api_key: str) -> list[list[float]]:
    """Single HTTP call to OpenRouter's OpenAI-compatible embeddings endpoint.

    OpenRouter batches natively in the same shape OpenAI does. The extra
    ``HTTP-Referer`` / ``X-OpenRouter-Title`` headers mirror
    ``clip_analyzer._call_openrouter`` — they're free-form provenance
    strings OpenRouter uses for attribution but doesn't validate.
    """
    import requests

    from promo.core.config import openrouter_http_referer

    response = requests.post(
        EMBEDDING_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": openrouter_http_referer(),
            "X-OpenRouter-Title": "pgc-pipeline",
        },
        json={"model": EMBEDDING_MODEL_API_ID, "input": inputs},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    items = data.get("data", [])
    if len(items) != len(inputs):
        raise RuntimeError(
            f"Embeddings API returned {len(items)} vectors for {len(inputs)} inputs"
        )
    # Strict index lookup: the OpenAI-shape API contract guarantees an
    # "index" field on every item. Using .get("index", 0) would silently
    # collapse partial responses onto position 0 and misalign embeddings
    # with clips one layer up. Raise loudly if the contract ever slips.
    items_sorted = sorted(items, key=lambda it: it["index"])
    return [it["embedding"] for it in items_sorted]


def embed_texts(inputs: list[str]) -> list[list[float]]:
    """Embed raw strings. Used both internally and by ``clip_retriever`` for queries."""
    if not inputs:
        return []
    api_key = _get_api_key()

    def _call() -> list[list[float]]:
        return _post_embeddings(inputs, api_key)

    vectors = retry_with_backoff(_call, max_retries=3, base_delay=3.0)
    for vec in vectors:
        if len(vec) != EMBEDDING_DIM:
            raise RuntimeError(
                f"Unexpected embedding dim {len(vec)} (expected {EMBEDDING_DIM})"
            )
    return vectors


def embed_clips_for_poi(
    clip_metadata: list[ClipMetadata],
    cache_dir: str,
    *,
    mimo_prompt_sha1: Optional[str] = None,
    composition_version: int = COMPOSITION_VERSION,
) -> dict[str, object]:
    """Embed (or read cached) MiMo-describable clips for one POI.

    ``clip_metadata`` — list of dicts with at least ``id``, ``scene_description``,
    ``category`` (shape produced by ``clip_analyzer.analyze_clips``).

    ``cache_dir`` — target directory; conventionally
    ``material/<slug>/.embedding_cache/``. Created if absent.

    ``mimo_prompt_sha1`` / ``composition_version`` — injection points for
    tests. Production callers pass ``None`` / defaults; the suffixes are
    computed from the active ``clip_analyzer._ANALYSIS_PROMPT`` + resolved
    model + the module-level ``COMPOSITION_VERSION``.

    Sidecar payload shape per clip: ``{"vector": [floats], "input": str}``.
    Persisting ``input`` is the audit trail — given a sidecar alone you can
    always reconstruct what text was fed to OpenAI without re-deriving it
    from the current composition function (which may have drifted).

    Returns a dict with:
      - ``model``, ``dim``, ``mimo_prompt_sha1``, ``composition_version``, ``embeddings``
      - ``stats``: ``{clips_embedded, cache_hits, incremental}``
      - ``sidecar_path``: absolute path to the sidecar on disk
    """
    if not clip_metadata:
        raise ValueError("clip_metadata is empty — nothing to embed")

    # Dedupe the incoming batch against itself. Same-id duplicates would
    # otherwise waste an OpenRouter token AND silently overwrite each
    # other inside the merge loop (the second entry's vector would win,
    # first entry's work lost + stats["clips_embedded"] over-counts by
    # the duplicate count). Current production producers
    # (clip_analyzer.analyze_clips — returns a sorted list from a dict-
    # keyed iterator; _collect_clip_paths — guards with `cid not in
    # clip_paths`) cannot emit duplicates, but hand-crafted clip_metadata
    # from tests or future library callers (e.g. Sprint 12b integration)
    # can. Match backend.py:268-273 collision-log precedent: keep first,
    # warn on each subsequent collision. Sprint 12a audit finding M-1.
    seen_ids: set[str] = set()
    deduped: list[ClipMetadata] = []
    for clip in clip_metadata:
        cid = str(clip["id"])
        if cid in seen_ids:
            logger.warning(
                "Duplicate clip_id in embed batch: '%s' already seen, skipping",
                cid,
            )
            continue
        seen_ids.add(cid)
        deduped.append(clip)
    clip_metadata = deduped

    sha1 = mimo_prompt_sha1 or current_mimo_prompt_sha1()
    path = sidecar_path(cache_dir, sha1, composition_version)
    existing = _load_sidecar(path) or {}
    existing_emb: dict[str, dict] = existing.get("embeddings", {}) or {}

    # Sprint 17 B: reuse decision must compare the cached input text
    # against the freshly-composed text. Sidecar filename axes
    # (model, dim, mimo_prompt_sha1, composition_version) only catch
    # global drift; per-clip content edits to the upstream MiMo cache
    # (the operator surgical-edit pathway documented in
    # feedback_mimo_luxury_bias) leave the filename unchanged, so a
    # `cid in existing_emb`-only check would silently serve the
    # pre-edit vector forever. WARNING + re-embed on mismatch.
    to_embed: list[ClipMetadata] = []
    for clip in clip_metadata:
        cid = str(clip["id"])
        composed = compose_embedding_text(clip)
        existing_entry = existing_emb.get(cid)
        if existing_entry is None:
            to_embed.append(clip)
            continue
        cached_input = existing_entry.get("input") if isinstance(existing_entry, dict) else None
        if cached_input != composed:
            # Future-tense — the API call (embed_texts) happens later in
            # this function. Past-tense "re-embedding" overstates the
            # outcome if embed_texts raises after retries are exhausted.
            logger.warning(
                "Embedding cache stale input for clip_id '%s' "
                "(input mismatch — cached=%r, composed=%r); scheduling re-embed",
                cid,
                (cached_input[:80] + "...") if isinstance(cached_input, str) and len(cached_input) > 80 else cached_input,
                (composed[:80] + "...") if len(composed) > 80 else composed,
            )
            to_embed.append(clip)

    new_embeddings: dict[str, dict] = {}
    if to_embed:
        inputs = [compose_embedding_text(clip) for clip in to_embed]
        vectors = embed_texts(inputs)
        for clip, vec, text in zip(to_embed, vectors, inputs):
            new_embeddings[str(clip["id"])] = {"vector": vec, "input": text}

    merged: dict[str, dict] = dict(existing_emb)
    merged.update(new_embeddings)

    payload = {
        "model": EMBEDDING_MODEL,
        "dim": EMBEDDING_DIM,
        "mimo_prompt_sha1": sha1,
        "composition_version": composition_version,
        "embeddings": merged,
    }

    if new_embeddings or not existing:
        _save_sidecar(path, payload)

    clips_embedded_this_run = len(to_embed)
    cache_hits = len(clip_metadata) - clips_embedded_this_run
    incremental = clips_embedded_this_run if (existing_emb and to_embed) else 0

    return {
        **payload,
        "stats": {
            "clips_embedded": clips_embedded_this_run,
            "cache_hits": cache_hits,
            "incremental": incremental,
        },
        "sidecar_path": path,
    }


def load_embeddings_for_poi(
    cache_dir: str,
    *,
    mimo_prompt_sha1: Optional[str] = None,
    composition_version: int = COMPOSITION_VERSION,
) -> Optional[dict]:
    """Return the full sidecar payload for ``cache_dir`` at the current version
    tuple, or ``None`` when no sidecar exists.

    Consumer of the sidecar — Sprint 12b's ``clip_retriever`` path reads
    this to populate ``clip_metadata`` with ``embedding`` fields before
    calling ``top_k`` / ``union_of_top_k``. See ``attach_embeddings_to_metadata``
    for the canonical merge.
    """
    sha1 = mimo_prompt_sha1 or current_mimo_prompt_sha1()
    path = sidecar_path(cache_dir, sha1, composition_version)
    return _load_sidecar(path)


def attach_embeddings_to_metadata(
    clip_metadata: list[ClipMetadata],
    sidecar_payload: dict,
) -> tuple[list[ClipMetadata], list[str]]:
    """Join a ``clip_analyzer.analyze_clips`` result with its embedding sidecar.

    Returns ``(attached, dropped_ids)``:
      * ``attached`` — new list of dicts each carrying the original MiMo
        fields plus ``embedding`` (the 1536-D vector) and ``embedding_input``
        (the text fed to OpenAI).
      * ``dropped_ids`` — list of clip_ids present in ``clip_metadata`` but
        missing from the sidecar, in the original ``clip_metadata`` order.

    Clips missing from the sidecar are NOT included in ``attached`` — Sprint
    12b's ``union_of_top_k`` consumer requires every clip in the attached
    list to have an ``embedding``, so silent inclusion of un-embedded clips
    would raise inside the retriever. Prefer an explicit re-run of
    ``embed_clips_for_poi`` over half-indexed metadata.

    Sprint 13 AC18 (D-002): the previous per-clip-dropped WARNING was
    emitted here as a side effect; the caller in
    ``compile_promo._step_assign_clips`` now emits a single unified WARNING
    that folds the dropped_ids list together with the counts + fallback-
    reason context so operators do not cross-reference two log timestamps
    to diagnose a retrieval regression.
    """
    embeddings = sidecar_payload.get("embeddings", {}) or {}
    attached: list[ClipMetadata] = []
    dropped_ids: list[str] = []
    for clip in clip_metadata:
        cid = str(clip["id"])
        entry = embeddings.get(cid)
        if entry is None:
            dropped_ids.append(cid)
            continue
        merged = dict(clip)
        merged["embedding"] = entry["vector"]
        merged["embedding_input"] = entry["input"]
        attached.append(cast(ClipMetadata, merged))
    return attached, dropped_ids
