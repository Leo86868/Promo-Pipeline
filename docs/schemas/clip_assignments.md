# `clip_assignments_<slug>_<dur>s.json`

Per-variant Gemini #2 phrase-to-clip assignment output. Frozen at sidecar-write time so a later run can replay the assignments (fixture-replay tests) or attribute a quality regression to specific retrieval / inventory state.

## Filename template

`clip_assignments_<sanitized_poi_slug>_<round(duration_sec)>s.json`

- `<sanitized_poi_slug>`: lowercase-underscore form produced by `promo.core.sanitize_poi_name` (e.g. `hotel_xcaret_arte`). NOT the hyphenated material-directory slug.
- `<round(duration_sec)>`: integer seconds (e.g. `65`).
- Collision-bumped variants (`...-2.json`, `...-3.json`, ...) are produced by `_write_sidecar` when the base name already exists.

## Producer / consumers

- Producer: `promo/cli/compile_promo.py` — `full_pipeline` writes the assignment record + retrieval provenance via `_write_sidecar`.
- Consumer: `promo/core/assign/clip_assigner.py::load_latest_clip_assignments` — returns the variants list. Tolerates both the bare-list shape (legacy) and the wrapped-dict shape documented below.

Cross-reference: `architecture.md` "Sidecar inventory" table for the canonical producer/consumer pairing.

## Payload shape

```json
{
  "retrieval_active": true,
  "embedded_pool_size": 26,
  "reduced_pool_size": 14,
  "mimo_prompt_sha1": "abcdef12",
  "fallback_reason": null,
  "retrieval_contract": "soft_hint",
  "variants": [
    {
      "variant_index": 1,
      "variant_status": "rendered",
      "assignments": [
        {
          "segment": 1,
          "clip_id": "0042",
          "start_word_idx": 0,
          "end_word_idx": 4,
          "trim_start": 0.0,
          "display_span_sec": 2.35,
          "source_duration_sec": 5.12
        }
      ]
    }
  ]
}
```

## Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `retrieval_active` | `bool` | `true` iff the embedding-retrieval closure was wired AND the embedding sidecar loaded. |
| `embedded_pool_size` | `int` | Size of the embedded metadata list after `attach_embeddings_to_metadata`; `0` when retrieval inactive. |
| `reduced_pool_size` | `int` | Size of the inventory actually passed to Gemini #2. Equals the full pool when retrieval is inactive OR any fallback fired. |
| `mimo_prompt_sha1` | `str \| null` | 8-hex SHA1 segment of the MiMo prompt (matches the `.mimo_cache/` + `.embedding_cache/` filename-suffix key). `null` when retrieval inactive. |
| `fallback_reason` | `str \| null` | One of `null` (clean retrieval), `"no_sidecar"` (embedding_cache_dir threaded but sidecar missing), `"h2_union_shortfall"` (`union_size < len(narration_queries)` at k=6), `"m4_attach_shrinkage"` (sidecar-embedded pool < MiMo metadata pool), `"retrieval_exception"` (closure raised — exception swallowed by `assign_clips_with_f3_retry`'s defensive wrap, full pool used). |
| `retrieval_contract` | `str` | Literal `"soft_hint"` on every run. Declares that retrieval is advisory, not strict (see "Soft hint contract" below). Seeded by `_empty_retrieval_provenance`; never mutated downstream. |

## Soft hint contract

The `retrieval_contract` field documents a runtime invariant: **retrieval is a soft hint, not a strict gate**. Two consequences when reading this sidecar:

1. The retrieved subset (`reduced_pool_size` clips) is what Gemini #2 *sees* in its prompt — but its reply is NOT rejected when it names a `clip_id` from outside that subset. `assign_clips_with_f3_retry` and `_enforce_hard_constraint_and_enrich` carry no `clip_id in retrieved_ids` guard; the assigner accepts any valid `clip_id` from the full MiMo pool.
2. The four `fallback_reason` codes (`no_sidecar`, `m4_attach_shrinkage`, `h2_union_shortfall`, `retrieval_exception`) encode cases where retrieval did not reach Gemini #2 with a narrowed pool — the full `clips_metadata` was passed instead. A non-null `fallback_reason` plus `retrieval_active: true` therefore means "retrieval was wired but the hint was empty"; in that state the run is mathematically equivalent to a no-retrieval run.

A future "strict retrieval" mode would surface as a different `retrieval_contract` value (e.g. `"strict"`). Today there is exactly one value and the sidecar always emits it.

## Variants list

Each `variants[i]`:

| Field | Type | Meaning |
|---|---|---|
| `variant_index` | `int` | 1-based rank; matches `generate_script_variants` output. |
| `variant_status` | `str` | `"rendered"` — success-gated (pre-F3-fail variants never appear). |
| `assignments` | `list[dict]` | Enriched per-phrase clip assignments from `_enforce_hard_constraint_and_enrich`. |

Each `assignments[j]`:

| Field | Type | Meaning |
|---|---|---|
| `segment` | `int` | 1-based segment index from Gemini #1. |
| `clip_id` | `str` | 4-digit zero-padded clip id (e.g. `"0042"`). |
| `start_word_idx` | `int` | Index into the variant's `word_timestamps` where the phrase starts. |
| `end_word_idx` | `int` | Inclusive end index. |
| `trim_start` | `float` | Seconds into the clip where playback starts. |
| `display_span_sec` | `float` | Measured display span in assigner space. |
| `source_duration_sec` | `float` | Full clip source duration from ffprobe. |

## Backward compatibility

Older payloads stored the variants list bare:

```json
[
  {"variant_index": 1, "variant_status": "rendered", "assignments": [...]}
]
```

`load_latest_clip_assignments` unwraps via `payload.get("variants")` when the payload is a dict, returns the list directly when the payload is a list. Both shapes yield the same `list[variant]` to downstream readers; the provenance fields are consumed out-of-band (human review + future analysis tooling).

## Notes

- Fields use the `display_span_sec` value measured in **assigner space** (ceiling = `narration_end`). The renderer extends the last clip past `narration_end`; that buffer is renderer-space territory and is NOT captured here. See `architecture.md` "Two-space model".
- The provenance fields are **last-successful-variant-wins**, not strictly last-invocation-wins: `compile_promo.full_pipeline` only updates `run_retrieval_provenance` inside the variant loop's try-success branch. If variant N raises `ClipAssignmentError` after variant 1 succeeded, variant 1's provenance lingers in the written sidecar. All variants in a single `compile_promo` run share the same `embedding_cache_dir` + MiMo prompt so this rarely matters, but anyone debugging a per-variant regression should consult the per-variant log lines for the true story.
