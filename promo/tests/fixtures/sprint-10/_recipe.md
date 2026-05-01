# Sprint 10 C6 — Fixture recipe

Source of truth for the fixture-replay test at
`promo/tests/test_sprint_10_fixture_replay.py`. Reconstructed from Sprint
09b live renders so the test can prove Gemini #2 would satisfy the hard
constraint on the exact narrations that 09b shipped.

## Inputs (reconstructed from `output/sprint-09b/`)

### LPI v1

- Script text: extracted from `_run_lpi_65s.log` `Post-TTS tagged_text
  (variant 1):` block (the full-narration blob, not the truncated
  `Seg N:` log lines). Split into per-segment text by consuming the
  first N words per segment, where N comes from each entry's
  `word_count` in `Post-TTS segment_timestamps (variant 1):`.
- `pause_after_ms`: from `Pre-TTS pause_after_ms per segment (variant 1):`.
- `segment_timestamps`: from `Post-TTS segment_timestamps (variant 1):`.
- `word_timestamps`: synthetic uniform distribution — for each segment
  the N words are spread evenly between the segment's `start` and `end`
  timestamps. This is a deliberate simplification noted in the test
  module's docstring — the hard constraint depends on
  `display_span = word_end - word_start + pause_after_ms`, which is
  conserved under uniform spread (within-segment timing distribution
  does not affect whether the span fits the clip's usable footage).
- Clip pool + durations: loaded from
  `material/little-palm-island-resort/clips/` at test runtime via
  `_get_clip_duration` (ffprobe). NOT committed as a fixture.

### Jashita v1

Identical recipe against `_run_jashita_65s.log` and
`material/jashita-hotel-tulum/clips/`.

## Live capture: Gemini #2 response

One live Gemini #2 call per POI. The captured response is saved as
`gemini2_response_{poi}_v1.json` with its sha256 recorded in the test
module as `EXPECTED_HASH_LPI` / `EXPECTED_HASH_JASHITA` in
`promo/tests/test_sprint_10_fixture_replay.py`.

The live call is gated behind the `--live` flag on the
fixture-builder script (NOT on pytest — there is no `--live`
conftest option):

    python3 -m promo.tools.build_sprint_10_fixtures --live

Pass `--only lpi` or `--only jashita` to restrict to a single POI.
After recapture, update the matching `EXPECTED_HASH_*` constant in
the test module to the new sha256 emitted by the builder's log line
`Gemini #2 {poi} sha256: ...`.

By default the test uses the committed response and asserts the hash of
the committed file matches the constant — any drift of the captured
fixture is caught.

## Negative fixtures

Two mutations per POI, per Sprint 10a reflection 2a — guard against
Gemini #2 drift re-introducing either of the two silent-pass bugs the
10a audit caught:

- `gemini2_response_{poi}_v1_drop_segment.json` — one segment's phrases
  removed. Asserts L-001 guard
  (`_enforce_hard_constraint_and_enrich` missing-segment raise) fires.
- `gemini2_response_{poi}_v1_overlap.json` — two phrases' word-idx
  ranges overlap within one segment. Asserts L-003 guard (phrase-tiling
  raise) fires.

Both are derived programmatically from the happy-path capture rather
than hand-written, so they move with it if the happy-path fixture ever
gets re-captured.
