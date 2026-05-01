# `.embedding_cache/<model>-<dim>-<mimo_prompt_sha1>-v<N>.json`

OpenAI `text-embedding-3-small` vectors over MiMo scene_description +
category, per POI. One sidecar per POI (not per clip). Populated by an
offline embedding harness; consumed by the retrieval closure inside
`compile_promo._step_assign_clips`.

## Filename template

`material/<poi-slug>/.embedding_cache/<model>-<dim>-<mimo_prompt_sha1>-v<composition_version>.json`

- `<model>`: hardcoded `text-embedding-3-small`.
- `<dim>`: hardcoded `1536` (matches the model's native dimension).
- `<mimo_prompt_sha1>`: 8-hex — SAME key as the `.mimo_cache/` version
  suffix, so any MiMo prompt/model change invalidates embeddings in
  lockstep.
- `<composition_version>`: manually-bumped integer in
  `promo/core/assign/clip_embedder.py::COMPOSITION_VERSION`. Bumps when
  `compose_embedding_text()` changes how the per-clip embedding input
  is assembled.

Example: `text-embedding-3-small-1536-abcdef12-v1.json`

## Producer / consumer

- Producer: `promo/cli/build_embedding_index.py` — dev utility,
  one invocation per POI. Writes via
  `promo/core/assign/clip_embedder.py::_save_sidecar` (atomic `os.replace`).
- Consumer: `promo/core/assign/clip_embedder.py::load_embeddings_for_poi` +
  `attach_embeddings_to_metadata` — integration site inside
  `compile_promo._step_assign_clips`.

Cross-reference: `architecture.md` "Sidecar inventory" + "Pool conventions"
for the four-axis invalidation rule.

## Payload shape

```json
{
  "model": "text-embedding-3-small",
  "dim": 1536,
  "mimo_prompt_sha1": "abcdef12",
  "composition_version": 1,
  "embeddings": {
    "0042": {
      "vector": [0.024, -0.011, 0.093, ...],
      "input": "infinity pool at sunset with palm trees | pool"
    }
  }
}
```

## Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `model` | `str` | Embedding model id (matches `<model>` in the filename). |
| `dim` | `int` | Vector dimension (matches `<dim>`). |
| `mimo_prompt_sha1` | `str` | 8-hex SHA1 of the MiMo `(_ANALYSIS_PROMPT + PROMO_CLIP_MODEL)` pair at production time. |
| `composition_version` | `int` | Version of `compose_embedding_text` used to build each `input` string. |
| `embeddings` | `dict[str, dict]` | Map from `clip_id` to `{vector, input}`. |

## Per-clip entry

| Field | Type | Meaning |
|---|---|---|
| `vector` | `list[float]` | The 1536-D embedding returned by OpenAI. |
| `input` | `str` | The raw text fed to the embedding API. Preserved so the embedding input is auditable without re-deriving from the MiMo sidecar. |

## Four-axis invalidation

Any change along any of the four axes produces a fresh filename rather
than silently overwriting:

1. **Model**: change the model id (e.g. `text-embedding-3-large`) → new
   filename prefix.
2. **Dim**: change the requested dimension → new filename.
3. **MiMo prompt SHA1**: change `_ANALYSIS_PROMPT` OR `PROMO_CLIP_MODEL`
   → `.mimo_cache/` suffix bumps → this filename's `<mimo_prompt_sha1>`
   segment bumps in lockstep.
4. **Composition version**: change `compose_embedding_text` → bump
   `COMPOSITION_VERSION` manually in the module.

Readers glob by the current four-axis key and miss stale entries — no
manual cache purge required.

## Retrieval integration

`attach_embeddings_to_metadata(clip_metadata, sidecar_payload)` merges
the `embeddings` map onto MiMo metadata and returns
`(attached, dropped_ids)`. Clips present in `clip_metadata` but missing
from the sidecar surface as `dropped_ids` and emit a unified WARNING at
the `compile_promo` caller. Any shrinkage triggers the
`m4_attach_shrinkage` fallback recorded in the clip_assignments
sidecar's `fallback_reason` field (see `clip_assignments.md`).

## Notes

- The sidecar directory is gitignored. Populate with
  `python3 -m promo.cli.build_embedding_index --poi <slug>` before
  the first compile_promo run on a new POI (or rely on the on-demand
  build that compile_promo performs automatically).
- A missing sidecar on a compile_promo run is NOT fatal —
  `fallback_reason="no_sidecar"` fires and Gemini #2 receives the full
  MiMo pool (no-retrieval behavior).
