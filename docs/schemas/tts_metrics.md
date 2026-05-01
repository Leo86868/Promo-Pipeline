# `tts_metrics_<slug>_<dur>s.json`

Per-variant TTS observability metrics. Written this run, read on the NEXT run
to bootstrap the per-POI WPM self-calibration loop.

## Filename template

`tts_metrics_<sanitized_poi_slug>_<round(duration_sec)>s.json`

Collision-bumped variants land alongside the base file as
`..._<dur>s-2.json`, `..._<dur>s-3.json`, … (producer: `_write_sidecar`).

## Producer / consumer

- Producer: `promo/cli/compile_promo.py::full_pipeline` — appends one
  row per rendered variant to the `tts_metrics` accumulator and writes
  the accumulator via `_write_sidecar` at run-end (success-gated, NOT
  per-variant).
- Consumer: `promo/core/script/pause_budget.py::load_calibrated_wpm` — the NEXT
  run's bootstrap reader averages measured_wpm values across variants
  stored here for the same POI/duration key.

Cross-reference: `architecture.md` "Sidecar inventory" table.

## Payload shape

```json
[
  {
    "variant_index": 1,
    "voice_key": "kore",
    "voice_backend": "gemini",
    "measured_wpm": 144.6,
    "narration_coverage": 0.920,
    "duration_sec": 60.0,
    "last_word_end": 59.4,
    "segments_count": 4,
    "total_words": 145
  }
]
```

## Fields

| Field | Type | Meaning |
|---|---|---|
| `variant_index` | `int` | 1-based rank from `generate_script_variants`. |
| `voice_key` | `str` | VOICE_CATALOG key in effect for this variant (e.g. `"kore"`, `"jarnathan"`). |
| `voice_backend` | `str` | `"gemini"` or `"elevenlabs"` — the TTS dispatch seam decided by VOICE_CATALOG. |
| `measured_wpm` | `float` | Per-variant WPM derived from `word_timestamps` + `last_word_end`. |
| `narration_coverage` | `float` | `last_word_end / target_duration_sec`; the fraction of the target the narration actually fills. |
| `duration_sec` | `float` | Target duration this run was compiled for (the `--target-duration-sec` CLI value). |
| `last_word_end` | `float` | Timestamp (seconds) of the last TTS word end. Used with duration_sec for coverage math. |
| `segments_count` | `int` | Number of narration segments (Gemini #1 output). |
| `total_words` | `int` | Word count across all segments. |

## Notes

- **WPM is backend-dependent**. Gemini Kore averages ~145 WPM; ElevenLabs
  jarnathan averages ~195 WPM. `load_calibrated_wpm` averages across
  variants stored in this sidecar for the same POI/duration pair — so a
  sidecar mixing backends will produce a blended bootstrap. The calibration
  reader does NOT currently discriminate by `voice_backend`; per-voice
  calibration is a known gap (trigger condition: observed WPM variance > 15%
  across voice × text).
- The sidecar is written as a JSON **list** of per-variant rows. Aborted
  variants (F3 second-fail, render fail, pool-exhaustion abort) do NOT
  appear — only successfully rendered variants are recorded.
