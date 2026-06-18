# Proposal (for reviewer 审核): add an in-progress POI soft-lock to PGC

**Author:** production session, 2026-06-17. **Status:** feasibility report, NOT implemented.
**Trigger:** an accidental 2-batch parallel PGC run (`stock_5x3_…212011Z` + `stock_3x5_…211945Z`,
launched ~26s apart) both selected **Sandpearl** → render-staging race (survived on luck) +
usage-cap TOCTOU (14 Sandpearl assets reached 4 uses vs the 3-use cap, bounded +1).

## Goal
Give PGC the same "don't pick a POI another running batch already claimed" guard that
**music_remix** already has, so staggered-start concurrent batches stop colliding on the same POI.

## Current state (verified by grep 2026-06-17)
- **PGC has NO such lock.** No `collect_in_progress_poi_ids`, no `plan_batch`, no `batch_planner.py`.
  Selection (`promo/core/batch_selection.py`) only reads the Supabase usage ledger for cooldown —
  blind to in-flight sibling batches.
- **music_remix HAS it:** `video_paradigms/music_remix/receipt.py:55 collect_in_progress_poi_ids`
  (scans staging-dir sibling receipts, claims POIs of any non-`completed` receipt),
  `batch_planner.py:259-273` skip (reason `in_progress_lock`), default-on `scripts/plan_batch.py:54-61`.

## Difficulty: SMALL (~1–2h incl. tests). Why it's easy here
1. **The seam already exists.** `batch_selection.py` already computes a POI-id set and threads it
   into selection: `fetch_recent_usage_poi_ids(...)` → `summarize_pois(..., cooldown_poi_ids=...)`
   (lines 162 / 216-302). An in-progress set plugs into the same shape — except it's a **hard
   exclude** (skip), not the soft cooldown flag.
2. **The "whiteboard" is already written early.** `prepare_selected_batch` writes
   `selection_summary.json` listing the selected POIs at `promo/cli/run_batch.py:304` — BEFORE any
   render. So a sibling batch starting seconds later can read it. (Today's 26s gap → A's summary was
   almost certainly on disk; B just had no code to read it.)

## Proposed implementation (for the reviewer to weigh)
1. New `collect_in_progress_poi_ids(runs_root, *, exclude_dir)` in `batch_selection.py`: glob
   `runs_root/*/selection_summary.json` (and/or `RUN_RECEIPT.json`), and for any batch **not fully
   complete**, collect its selected `poi_id`s into a set. Exclude the current run's own dir.
2. Wire into the selection call in `run_batch.py` as a **hard exclusion** applied before the random
   pick (distinct from soft cooldown). `runs_root` = `dirname(output_dir)` by default, overridable.
3. Default-ON flag `--in-progress-lock / --no-in-progress-lock` (mirror music_remix default-on).
4. Test: pin (a) a sibling non-complete summary's POIs are excluded, (b) a fully-complete sibling
   releases its POIs, (c) the current run's own dir is ignored, (d) flag off = no exclusion.

## ⚠️ Honest scope — what this does NOT fix (it's 1 of 3 layers)
- **Same-instant start still races.** It's a soft lock: if two batches both finish selection before
  either flushes `selection_summary.json`, they see empty dirs and the lock no-ops. Bounded by how
  fast selection flushes (~seconds), not eliminated.
- **Does NOT fix the usage-cap TOCTOU.** That race is at the shared Supabase ledger, independent of
  worktree/receipt. Even with this lock, two batches on the SAME POI would still overshoot the
  3-use cap. (This lock's whole point is to stop them sharing a POI in the first place — so in
  practice it also removes the cap race *as long as it doesn't no-op*.)
- **Does NOT fix near-dup** (Jaccard ~0.5–0.99, different fingerprints, visually similar) — no layer
  guards that today. This is the standing CLAUDE.md concurrency red line.
- **Stale-receipt starvation (design decision needed):** fail-closed means a crashed/non-complete
  old batch claims its POIs forever until resumed or cleaned. PGC has `--resume`, so "crashed batch
  still holds its POIs" is arguably *correct* (resume it). But reviewer should decide: time-bound
  the scan (ignore receipts older than N h), require a live process, or rely on operator cleanup.

## The 3-layer picture (so the reviewer sees where this sits)
| layer | guards | music_remix | PGC today | PGC after this |
|---|---|---|---|---|
| ① in-progress soft-lock | same-POI, staggered start | ✅ | ❌ | ✅ |
| ② DB hard net (057 partial unique + 056 trigger) | exact twins → 23505 skip | ✅ | ✅ | ✅ |
| ③ near-dup gate | visually-similar non-twins | ❌ | ❌ | ❌ |

## Recommendation
Worth doing IF Leo wants to run PGC batches in parallel routinely — cheap parity, closes the most
common (staggered-start) overlap. It is **necessary but not sufficient** for "safe parallel": full
safety still wants **one git worktree per concurrent batch** (isolates render staging) **+ disjoint
POI sets** (which also moots layers ① and the cap race). If parallel stays rare/accidental,
operator-enforced disjoint POIs alone is the cheaper path and this lock is optional insurance.
