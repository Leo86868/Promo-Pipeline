# P1g — P1e really blocks + the exact 23505 shape (live-captured)

**Date:** 2026-06-23 · **Branch:** `chore/p1g-probe` · **DB:** shared prod `vuqpbtuobfbztbmqucbc`

## TL;DR (decision card)

- **Real?** Yes — we *watched* P1e reject a duplicate. It is no longer "should be
  blocking" (two indirect signals: AIGC says it's on + "0 dupes online"). We
  forced a real collision and caught the `23505`.
- **How bad / what changes?** Nothing is broken. PGC's matcher
  (`getattr(exc,"code")=="23505"`) is **correct** against the real shape.
- **Fix?** Do **NOT** narrow the catch (narrowing would be a regression — see
  below). Only honesty hygiene: de-hedged the stale "(P1g pending)" comment/log
  and added a pinned test that guards the `.code` dependency.
- **Who?** PGC side done on this branch. AIGC: nothing to do; the error shape is
  identical cross-repo (one sync note at the bottom).

## What we did (controlled, net-zero write to a shared prod table)

We did **not** insert two synthetic rows. We collided **one** probe insert
against an **already-committed** production row — so **zero rows ever persist**
(the single insert is rejected by P1e and rolled back; no cleanup step needed).
This also proves P1e rejects against *real stored state*, not a row we just made.

- Target row already in prod: `poi_id=poi_15d874e8787d`,
  `recipe_fingerprint=rfp2:bccd5f3b…` (verified its `recipe_input` re-hashes to
  the stored fingerprint, so the trigger reproduces the same fingerprint).
- Probe insert: same `poi_id` + that exact `recipe_input` + `status='approved'`
  + a **distinct** `source_pipeline/source_video_key` (`p1g_probe` /
  `p1g_probe_collision_20260623`) so the *only* index that can fire is P1e, not
  `release_candidates_source_unique`.
- Wrapped in a plpgsql `EXCEPTION WHEN unique_violation` block: on the expected
  collision the insert auto-rolls-back; if it had **succeeded** (= P1e not
  enforcing) the block deletes the row in-txn and returns `p1e_fired=false` to
  fail loud.

**Net-zero proof:** probe-row count `before = 0`, `after = 0`.

## The exact `23505` shape (captured live)

```json
{
  "p1e_fired": true,
  "sqlstate": "23505",
  "constraint_name": "uq_release_candidates_poi_recipe_fingerprint",
  "message": "duplicate key value violates unique constraint \"uq_release_candidates_poi_recipe_fingerprint\"",
  "detail":  "Key (poi_id, recipe_fingerprint)=(poi_15d874e8787d, rfp2:bccd5f3b…) already exists.",
  "hint": ""
}
```

How this maps to the **postgrest `APIError`** PGC actually catches (postgrest-py
2.28.2 — `APIError.__init__` reads each field straight from PostgREST's JSON body):

| question | answer |
|---|---|
| `.code` | `"23505"` — PostgREST puts the Postgres SQLSTATE here. **This is the field PGC's matcher reads.** |
| index name → `.message` or `.details`? | **`.message`** — `...unique constraint "uq_release_candidates_poi_recipe_fingerprint"` |
| colliding key → ? | **`.details`** — `Key (poi_id, recipe_fingerprint)=(…) already exists.` |
| `.hint` | empty / `null` |
| `.json()` / `._raw_error` | the full dict above |

So `_is_unique_violation`'s `getattr(exc,"code") == "23505"` **works**: the whole
matcher hangs on PostgREST exposing the SQLSTATE in `code`, which it does.

## Judgment: narrow the catch, or keep it broad?

**Keep it broad. Narrowing the control flow would be a regression.**

Two unique indexes on `release_candidates` can raise `23505` on insert:

1. `uq_release_candidates_poi_recipe_fingerprint` (P1e) — content dedup.
2. `release_candidates_source_unique` `(source_pipeline, source_video_key)` —
   already pre-filtered by `register_*`'s preflight, but possible under a race.

(The `candidate_id` PK is `gen_random_uuid()` — effectively never collides.)

Both legitimately mean the same thing: **"already registered → skip this row,
keep the rest."** If we narrowed the catch to P1e-only, a benign
`source_unique` race would stop being a skip and become a **whole-batch abort**.
That's strictly worse. The broad `23505→skip` is the correct **permanent
baseline**, not a placeholder.

The only honest improvement P1g unlocks is the **log label**: the operator log
already renders `error=%s` (the full APIError, which names the constraint), so
the specific index is *already* visible. We removed the stale "exact constraint
not yet distinguished (P1g)" hedge and stated that both indexes mean skip.

## Code changes on this branch (surgical)

- `promo/core/release_candidates.py`
  - `_is_unique_violation` docstring: recorded the P1g live verification (SQLSTATE
    in `.code`, index in `.message`, key in `.details`).
  - per-row warning comment/text: dropped the "(P1g pending) not yet
    distinguished" hedge; documented why the broad catch is kept on purpose.
  - **No control-flow change.**
- `promo/tests/unit/pipeline/test_release_candidates.py`
  - `test_is_unique_violation_matches_real_postgrest_shape` — builds a **real**
    `postgrest.exceptions.APIError` from the captured body and asserts the matcher
    fires and the index/key are recoverable. This is the **pin**: a future
    postgrest upgrade that moves the SQLSTATE out of `.code` breaks loudly here
    instead of silently letting real collisions abort batches.
  - `pytest promo/tests/unit/pipeline/test_release_candidates.py` → **14 passed**.

## Sync note for AIGC (P1g is a two-repo item)

The error shape is **cross-repo identical** — it's whatever PostgREST returns for
a `23505`, independent of which paradigm inserted. Concretely: SQLSTATE `23505`
in the JSON `code` field; constraint name `uq_release_candidates_poi_recipe_fingerprint`
in `message`; `Key (poi_id, recipe_fingerprint)=(…) already exists.` in `details`.
music_remix's **text-scan** matcher (vs PGC's `.code` check) will also match —
the index name is reliably present in `message`. No AIGC change required; if AIGC
ever wants to switch from text-scan to a code check, `.code == "23505"` is the
verified, more robust key.
```
