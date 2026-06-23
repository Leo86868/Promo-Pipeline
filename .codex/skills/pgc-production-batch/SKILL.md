---
name: pgc-production-batch
description: Use for PGC production autopilot, review batches, top-ups, usage writeback/reverts, Drive handoff, release_candidates registration, freshness checks, and RUN_RECEIPT-backed recovery for Supabase POI asset videos. Triggers on "make 15 POIs", "batch PGC videos", "review first", "write usage", "top up", "revert usage", "production PGC run", "Drive staging", or "release candidate".
---

# PGC Production Batch

Repo skill version: 2026-06-08 production-autopilot contract. This repo copy is
the source of truth for the PGC production workflow. The installed copy under
`~/.codex/skills` should be a symlink or installed copy of this folder; refresh
it with `scripts/install_repo_skills.sh` after workflow changes.

Read `docs/operations/pgc_production_contract.md` when implementing or auditing
behavior. Read `docs/operations/pgc_daily_runbook.md` when explaining the flow
to Leo in non-code terms.

## Target Default

- A normal request such as "make 15 POIs, 3 videos each" means production
  autopilot, not preview-first mode.
- Actual live production and live smoke runs should execute on the VPS
  production worktree, not the local Mac worktree. The VPS has the intended
  compute and production env. Local runs are for code work, dry/read-only
  preflight, and human review artifact inspection.
- The VPS is shared with AIGC/asset-platform jobs. For large batches, inspect
  `uptime` and the top CPU processes first. If heavy AIGC backfill or ffmpeg
  compression is already running, report that the batch may be slow; do not
  kill or pause those jobs unless Leo explicitly asks.
- If a session starts locally and Leo asks for live production, run it on the
  VPS or stop and report that the active shell is not the production runtime.
- Do not stop mid-run for routine production. Render, audit, write usage, and
  register handoff per successful video when the required repo/runtime support
  exists.
- Manual review is an explicit override, e.g. "review first" or "put videos in
  Downloads for review". In review mode, do not write usage or create
  `release_candidates` until Leo explicitly approves the next step.
- Do not add arbitrary large-batch confirmation thresholds. Trust the requested
  batch size unless required inputs are missing or eligibility is insufficient.
- Do not auto top-up failed videos. Report the shortfall; run a top-up batch
  only when Leo asks.

## Batch Modes — Stockpile vs Healthcheck

Every batch is one of two modes. Pick it from Leo's words, not from extra
operator-supplied numbers.

- **Stockpile batch** (default — "补库存", "make N POIs", "production run"):
  real inventory. `--production-autopilot` → real Drive upload + usage
  writeback + `release_candidates`. **Never reverted** — these are real
  products. Standard source/upscale config is the 720→1080 flip (native ≥1080
  `min_width` + upscale dismantled; see Source Width Policy) — there is no
  WaveSpeed spend to "not disable" anymore.

- **Healthcheck / test batch** ("体检", "smoke", "test the chain"): proves the
  live chain end-to-end without shipping product. Runs the SAME standard
  command as a stockpile batch — the difference is NOT a flag (post-flip the
  standard command already carries `--final-upscale-provider disabled`, and
  there is no WaveSpeed spend either way). A healthcheck STILL does real Drive
  upload + usage writeback + `release_candidates` — it is NOT no-publish — so it
  MUST be reverted when done, via FULL cleanup (collect each
  video's `manifest_id` from the receipt — the CLI keys on manifest-id, there
  is no batch-id flag):

  ```bash
  python3 -m promo.cli.revert_usage \
    --manifest-id <id> [--manifest-id <id> ...] --full-cleanup --execute
  ```

  Full cleanup (not usage-only) is mandatory — otherwise an `approved`
  release_candidate stays distributable in `release_unassigned_candidates`.
  See "Reverts And Smoke Cleanup" for the dry-run/checkpoint/verify contract.

  **Coverage caveat (dead-key blind spot) — now ROLLBACK-scoped:** a batch that
  does not upscale never authenticates the WaveSpeed key, so it passes GREEN
  even with a dead or rotated key (the 2026-06-15 stranded-key incident).
  Post-flip the standard flow upscales nothing at all, so the WaveSpeed key is
  exercised ONLY on the 720 rollback path — this blind spot is no longer a daily
  concern, it matters only if/when you re-arm upscale for rollback.

## Natural-language → command

The skill translates Leo's request into flags; the operator supplies NO numeric
values beyond POI-count and videos-per-poi (which Leo states). Every other
value — word floor, pacing, render concurrency, model ids, the asset floor —
reads from its single source (arsenal card / `config.py` / `registry.py`); the
operator types none of them.

"make 4 POIs, 3 videos each, for stockpile":

```bash
python3 -m promo.cli.run_batch \
  --select-random-pois --poi-count 4 --videos-per-poi 3 \
  --output-dir <run_dir> \
  --supabase-music-library --production-autopilot --tail-workers 4
  # + the 720→1080 flip flags (now standard — see Source Width Policy)
```

"health-check the chain, 2 POIs × 3" → the same command PLUS
`--final-upscale-provider disabled`, then the mandatory full-cleanup revert
(see Batch Modes).

"shorter scripts" / "字数短一点" is NOT a per-batch flag. Video length is a
PARADIGM choice — the `short_30s` vs `long_65s` arsenal card — and the word
band lives on the chosen card (`total_words_min` / `total_words_max`). So
translate "短一点" to a question, never a silent change: "切到 30s 档
(`short_30s` card) for this batch, or persistently lower the 65s card's
`total_words_min/max`?" A persistent word change = edit the card (single
source); there is no run_batch knob to shorten just one batch.

## Current Implementation Boundary

This repo currently has local manifests, usage preview/writeback helpers with
manifest audit and post-write verification, a local release handoff exporter,
`release_candidates` registration with post-insert verification, read-only random
POI selection via `promo.cli.select_batch_pois`, manifest-backed Drive staging
inventory via `promo.cli.prepare_drive_staging`, and render plus manifest-audit
`RUN_RECEIPT.json` emission from `promo.cli.run_batch`. It also has explicit
OAuth Drive upload via `promo.cli.upload_drive_staging`; uploads are private by
default. `promo.cli.run_batch --select-random-pois --production-autopilot` can
select eligible POIs, write `selection_summary.json` and `batch.json`, then
process each audit-passed video through private Drive upload, usage
writeback/verification, `release_candidates` registration/verification, POI
quarantine on usage failure, source-width transition filtering, fail-closed
final-upscale gating before Drive handoff, per-step receipt flushes with stage
timings, and an autopilot preflight that validates Drive/Supabase/upscale
configuration before rendering anything.

Interrupted or partially-failed batches resume with ONE command (2026-06-10):

```bash
python3 -m promo.cli.run_batch --resume <output_root>/RUN_RECEIPT.json
```

Per-video state decides the cheapest safe recovery: `complete` videos are
skipped; tail failures (`final_upscale_failed`, `drive_upload_failed`,
`usage_writeback_failed`, `release_candidate_failed_retryable`, or a crash
right after audit) re-run only the autopilot tail against the ORIGINAL
manifest — no re-render, no duplicate usage events, and an already-verified
upscale output is reused instead of re-paying WaveSpeed; everything else
(stuck `rendering`, `render_failed`, manifest problems, quarantine skips)
re-renders by replaying the recorded command. Quarantined POIs get one fresh
chance per resume; the cleared list is archived under `resume_history`. Do
NOT hand-build top-up batch JSONs for partial failures anymore — resume the
receipt.

Disambiguate "top up" before acting — the two readings diverge: recovering the
SAME failed slots = `--resume <receipt>` (same manifest, no duplicate usage,
cheapest per-video recovery); producing N BRAND-NEW videos = a fresh small
stockpile batch. When Leo says "补N条" / "top up the N that failed", confirm
which he means; do not silently pick one. (Top-up only when Leo asks — never
auto.) The remaining future autopilot work is live smoke hardening.

When a target behavior is not implemented yet, say so and do not fake it with
unsafe ad hoc live writes. Use the safest current workflow and report the gap.

## Ownership

PGC owns:

- video generation;
- manifest receipt and manifest audit;
- usage writeback to `public.poi_asset_usage_events`;
- durable finished-video registration in `public.release_candidates` once a
  durable `drive:<file_id>` URI exists;
- `RUN_RECEIPT.json` as the batch-level order record.

AIGC/zhongtai owns:

- account assignment;
- distribution and publishing;
- `public.distribution_status`;
- platform metrics.

PGC must not write account distribution state.

## Selection Defaults

- Select POIs randomly with equal weight among eligible POIs. Do not weight by
  active asset count; coverage is the goal.
- Default cooldown is global 3 days by POI, configurable when Leo asks.
- If Leo requests a classification (e.g. gold-tier, a region), filter with the
  paired flags `--classification-field <field> --classification-value <value>`
  — supported by BOTH `select_batch_pois` (read-only) and `run_batch`, and they
  must be passed together. VERIFIED read-only 2026-06-15:
  `--classification-field poi_type --classification-value gold` works and
  returns ONLY `poi_type='gold'` POIs. The real `poi_asset_pois` classification
  fields are `poi_type` (values `gold` / `exploration`) and `region`
  (e.g. `na`). Do NOT invent a field name — only `poi_type` / `region`
  are verified; for any other classification, verify it exists with a read-only
  `select_batch_pois` first. If the requested field/value does not exist the
  selector returns no/too-few POIs — fail clearly, do not silently fall back to
  all POIs.
- If not enough POIs pass filters, stop before production and ask Leo whether to
  run the smaller count or wait for zhongtai to add assets.
- Use `python3 -m promo.cli.run_batch --select-random-pois` for normal
  selection plus production runs. Use `promo.cli.select_batch_pois` only for
  manual preflight/debug.

## Active Asset Rule

The asset platform owns which assets are active/eligible and the usage cap.
The PGC paradigm owns how many active assets are enough for the requested video
format.
Selection must apply this threshold to candidate-ready assets, not just raw
active clips. For the current shared-asset path, candidate-ready means the asset
has a ready embedding (model = `registry.OPENROUTER_EMBEDDING_MODEL`, in
`promo/core/model_adapters/registry.py`) for the active composition version
and can enter semantic retrieval.

For `pgc_65s`, the floor is a FORMULA over the type card's asset knobs. The
SKILL states the rule; the numbers live on the card (single source — do not
copy them here):

```text
required_active_assets = assets.base_min + assets.per_extra * extra_variations
extra_variations       = max(videos_per_poi - 1, 0)
```

`assets.base_min` / `assets.per_extra` live in the arsenal type card
`promo/arsenal/script_skeletons/long_65s.yaml`; the formula is applied in
`promo/core/run_receipt.py::required_active_assets()`. Worked example (derived
from the current card): 3 videos/POI → `base_min + 2 × per_extra`
embedding-ready, policy-matching active assets. Future paradigms (`pgc_120s`)
carry their own card.

## Source Width Policy

PGC uses the shared asset `width` field as the main source-resolution selector
for vertical assets. Do not key this policy off `height`.

**Standard (since the 2026-06-22 flip): `min_width` at a ≥1080 floor**, with
final-video upscale dismantled. Finals use only native ≥1080 source; POIs
without enough qualifying clips are fail-loud STRANDED at selection. `min_width`
is a TRUE floor (no upper bound, so native 1440/2160 also pass):

```text
--source-resolution-policy-mode min_width
--source-target-width 1080
--final-upscale-provider disabled
```

**Rollback only — `transition_low_res_only` at 720:** the original transition
policy that eats old 720-width clips and upscales them to 1080. Retained but NOT
the default; use it only to fall back to the 720 pool (and then upscale is
required again — see below). Note `width_band` is a separate, symmetric
±tolerance band (NOT a floor) — do not reach for it as a ≥1080 selector; that is
what `min_width` is for.

```text
--source-resolution-policy-mode transition_low_res_only
--source-target-width 720
--source-width-tolerance-px 40
```

The aspect-ratio band that defines a vertical asset is NOT operator-typed — it
lives as code defaults `DEFAULT_ASPECT_RATIO_MIN` / `DEFAULT_ASPECT_RATIO_MAX`
in `promo/core/source_resolution_policy.py`; do not copy the numbers here.

Whichever policy is active must be applied both at POI eligibility time and at
retrieval/download time. Retrieval fallback or reserve padding must not widen
back to mixed-resolution assets. The candidate-ready asset floor (above) still
applies after the width filter.

Final-video upscale is keyed to the policy: under the standard `min_width` flow
it is OFF (sources are already ≥1080); under the `transition_low_res_only`
rollback it is REQUIRED for production handoff. The mechanism is detachable and
kept dormant, not deleted — see the upscale gate behavior under "Per-Video
Production Order".

PGC must not mutate asset-platform quality fields.

## Per-Video Production Order

Target order for each production video:

```text
render MP4
-> audit manifest
-> if final_upscale_required: apply and verify final-video WaveSpeed upscale
-> upload final MP4 to durable storage, currently Drive
-> write usage from manifest
-> verify usage writeback
-> insert release_candidates row
-> verify release_candidates row
-> update RUN_RECEIPT.json
```

Do not create `release_candidates` for local paths, VPS temp paths, or Downloads
paths. `source_output_uri` must be durable, currently `drive:<file_id>`.

Do not write usage before durable upload succeeds. If upload fails, no usage was
spent and no release candidate exists.

If `final_upscale_policy.required = true`, fail closed before Drive upload,
usage writeback, and `release_candidates` unless the final-video upscale was
actually applied and verified. `release_candidates.source_output_uri` must point
to the upscaled Drive URI. Usage still comes from the original manifest because
the asset timeline did not change.

Verified means ffprobe reports the actual final MP4 dimensions match the
configured upscale target (defaults `target_width` / `target_height` in
`promo/core/final_upscale.py`); do not copy the number here.

Current repo support can run selection plus the per-video production order:

```bash
ssh vps
# Deploy a FRESH worktree at the current main HEAD — do NOT pin a sha:
#   git -C <repo> fetch origin && git worktree add <new_worktree> origin/main
#   cd <new_worktree>/promo/remotion && npm install   # required (deploy-checklist)
#   then run the autopilot preflight before the live batch
cd <latest main worktree under /home/deploy/pgc_batch_worktrees/>
set -a
. ./.env
set +a
python3 -m promo.cli.run_batch \
  --select-random-pois \
  --poi-count "$poi_count" \
  --videos-per-poi "$videos_per_poi" \
  --output-dir "$output_dir" \
  --supabase-music-library \
  --production-autopilot \
  --tail-workers 4
```

**720→1080 flip (2026-06-22 — now STANDARD).** Append these to the command above
— finals use native ≥1080 source and the upscale is dismantled:

```bash
  --source-resolution-policy-mode min_width \
  --source-target-width 1080 \
  --final-upscale-provider disabled
```

`min_width` is a TRUE ≥1080 floor (no upper bound, so native 1440/2160 also pass).
POIs without enough native ≥1080 clips are fail-loud STRANDED at selection
(`insufficient_source_resolution_assets`) — never silently soft. **No
`PGC_WAVESPEED_UPSCALE_COMMAND` needed** (upscale dismantled; the WaveSpeed CLI is
kept dormant for rollback). Per-video wall-clock drops ~16→~5 min.

Legacy 720 transition (rollback only): `--source-resolution-policy-mode
transition_low_res_only --source-target-width 720 --source-width-tolerance-px 40`
+ set `PGC_WAVESPEED_UPSCALE_COMMAND` to the `wavespeed_upscale_once` runner.

The env file must provide `WAVESPEED_API_KEY`, and the process env (or the same
env file) must provide `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` — the
WaveSpeed source upload stages the master in a private Supabase Storage bucket
behind a time-limited signed URL (2026-06-09 fix; unreleased masters must NOT
transit public temp hosts). `--source-host supabase` is REQUIRED in production:
it fails closed when Supabase credentials are missing instead of silently
falling back to public temp hosts (`auto`/`temp` are for local smoke only).
If this command is missing, production autopilot must fail before
Drive/usage/release rather than hand off a raw low-res-source render.

This is the current structured production path. The skill translates Leo's
natural-language request into these flags; the repo does not parse English.
The explicit `--production-autopilot` flag avoids accidental live Drive/Supabase
writes from old render-only commands.

Tail pipelining (2026-06-10): by default the autopilot tail of video N
(upscale/Drive/usage/release) runs on a worker thread while video N+1 renders;
batch items are POI-round-robin ordered because same-POI videos must never
overlap (usage-event ordering). `--tail-workers 4` runs up to 4 concurrent
tails (WaveSpeed allows 100 concurrent predictions). 4 (not 2) because upscale
is the long pole and varies wildly — most ~700s but some "monster" upscales run
45–50 min; with only 2 lanes one monster starves the pipeline and renders stall
waiting for a tail slot. More lanes are cheap (tails are network-bound on
WaveSpeed polling, not local CPU). `--serial-tail` (or `--tail-workers 0`) is
the rollback switch: strictly serial render→tail per video. Per-video
voice/music/seed assignments are order-independent (keyed to the canonical
POI-major ordinal, byte-identical to the pre-pipelining mapping for the same
seed); the POI-round-robin EXECUTION order itself is unconditional.

Render speed knobs are repo config DEFAULTS, not natural-language policy — and
NOT copied here (copies drift — a render-concurrency value once recorded in
this doc had already gone stale against the code). Each resolves in
`promo/core/config.py` and is overridable by env var; read the current default
from config.py, never record a number here:

- concurrency    → `config.py::render_concurrency()`   · env `PROMO_RENDER_CONCURRENCY`
- x264 preset    → `config.py::render_x264_preset()`   · env `PROMO_RENDER_X264_PRESET`
- CRF            → `config.py::render_crf()`           · env `PROMO_RENDER_CRF`
- render timeout → `config.py::render_timeout_sec()`   · env `PROMO_RENDER_TIMEOUT_SEC`

Use these only to tune Remotion's current renderer. A future ffmpeg-only
renderer would be a separate implementation path because it must preserve
captions, CTA, audio mix, timeline entries, and manifest/usage semantics.

For repair/debug against a known POI list, `run_batch --batch "$batch_json"` is
still supported.

## Clip assigner (settled 2026-06-11)

The deterministic packer is the only clip-assignment engine. The Gemini #2
chain (assignment prompt + F3 script-regen + split-repair) and the
`PROMO_CLIP_ASSIGNER` switch were DELETED (`1f28902`); the same-script A/B that
chose the packer is closed (no A/B to run anymore — a leftover
`PROMO_CLIP_ASSIGNER` .env line is a harmless no-op, remove it when touching
.env). Rollback if ever needed: `git revert 1f28902` + redeploy a worktree
(~15 min). The replay mechanism survives — `PROMO_REPLAY_SCRIPT=<clip_assignments_*.json>`
re-renders a recorded script (`config.py::PROMO_REPLAY_SCRIPT`);
`run_batch`/`resume` refuse to start while it is set, so a forgotten var can't
corrupt a batch.

## Approved Existing Masters Handoff

When Leo says something like "continue the approved batch handoff" for existing
PGC masters, do not require him to recite the workflow. Ask only for the missing
artifact path if it was not provided. The normal artifact is an inventory JSON.

Supported inventory shapes:

- `pgc_drive_staging_inventory`: already normalized for Drive upload.
- `final_masters_inventory`: approved/upscaled masters with
  `upscaled_mp4_path`, `run_manifest_path`, `manifest_id`, `run_id`,
  `source_video_key`, POI fields, and music fields. Normalize this into a
  `pgc_drive_staging_inventory` before upload; use `upscaled_mp4_path` as the
  final `local_output_path`, not the pre-upscale render.

Safe order for approved existing masters:

```text
preflight inventory and MP4 paths
-> audit every run_manifest_path
-> check Supabase for existing usage/release candidate duplicates
-> upload approved masters to private neutral Drive staging
-> generate handoff items / release handoff JSON with drive:<file_id> URIs
-> write usage from manifests and verify
-> register release_candidates and verify
-> stop before distribution_status
```

Do not write usage or `release_candidates` for local Downloads paths or VPS temp
paths. Durable media must be `drive:<file_id>`. Do not write
`distribution_status`; AIGC/zhongtai owns distribution after
`release_candidates`.

The manual repair/debug path can prepare/audit the Drive staging inventory:

```bash
python3 -m promo.cli.prepare_drive_staging \
  --receipt "$run_receipt_json" \
  --output "$inventory_json" \
```

Then upload staged MP4s to Drive with the same OAuth credential shape as AIGC
Main:

```bash
python3 -m promo.cli.upload_drive_staging \
  --inventory "$inventory_json" \
  --output "$uploaded_inventory_json" \
  --handoff-items-output "$handoff_items_json"
```

This uses only manifest-audit-passed videos from the receipt. It uploads into
`AIGC Production Masters/<paradigm>/<date>/<batch_id>/` unless a parent folder
id/name override is provided. Do not use AIGC's public-share upload helper for
PGC final masters.

Once usage has been written and verified, current repo support can explicitly
register approved handoff rows:

```bash
python3 -m promo.cli.register_release_candidates \
  --handoff "$release_handoff_json" \
  --execute
```

Without `--execute`, this command is a dry run. Do not run `--execute` in review
mode unless Leo explicitly approved release registration.

## Manifest Audit

Usage writeback is derived from the manifest. No valid manifest means no usage
writeback and no release candidate.

Before trusting a rendered video, verify:

- manifest exists and has `manifest_id`, `run_id`, POI id/name;
- exactly one rendered output exists for the video slot;
- output has `music_label`;
- every timeline entry has `asset_id` and `occurrence_id`;
- every used `asset_id` exists in `asset_snapshot`;
- `bridge_tail` entries also carry `asset_id`;
- usage events can be derived and event IDs are unique.

Current repo support:

```bash
python3 -m promo.cli.audit_run_manifest "$manifest_path"
```

`promo.cli.usage_events_writeback --execute` runs this production audit before
calling Supabase. If audit fails, it prints the audit JSON and does not write
usage.

## Failure Policy

| Failure | Required behavior |
| --- | --- |
| Render/vendor/transient failure | Retry in code when safe; after retry budget, mark video failed and continue. |
| Manifest audit failure | Do not write usage; do not create release candidate; mark video unsafe and continue. |
| Drive upload failure | Retry in code; after retry budget, do not write usage or release candidate; mark failed handoff and continue. |
| Usage writeback/verification failure | Retry in code; if still failed or unclear, quarantine that POI and do not produce more videos for it. Continue other POIs only if asset usage is POI-isolated; otherwise stop the batch. |
| `release_candidates` insert/verification failure | Retry in code; if still failed, mark handoff failed/retryable and continue. Usage is already correct. |
| Not enough eligible POIs | Stop before production and ask Leo whether to run fewer or wait. |

Retries belong in repo/runtime code, not improvised agent loops. The skill sets
policy; CLIs should implement bounded retries and verification.

## RUN_RECEIPT.json

Every production run must write a batch receipt. Future sessions resume from
the receipt, not chat history.

Minimum fields:

- `batch_id`;
- `paradigm`, e.g. `pgc_65s`;
- request config: requested POIs, videos per POI, filters, cooldown,
  active-asset formula;
- selected/skipped POIs;
- per-video state: render, manifest, Drive upload, usage, release candidate;
- manifest IDs and output URIs;
- usage event counts and verification status;
- release candidate IDs/status when created;
- quarantined POIs;
- summary counts and top-up recommendation.

Do not add a Supabase batch registry now. Use JSON receipts for the batch-level
"order"; keep asset truth in `poi_asset_usage_events`, finished-video truth in
`release_candidates`, and distribution truth in `distribution_status`.

## Review Mode

Review mode is explicit. In review mode:

- produce MP4s/manifests/sidecars;
- copy review artifacts to Downloads if requested;
- generate usage preview and audit summary;
- do not write usage;
- do not upload/register release candidates unless Leo later approves that step.

Clear approval examples:

```text
write usage
approve these for production
register these release candidates
top up the missing videos
```

Ambiguous phrases like "looks good" or "continue" are not enough for live state
changes in review mode.

## Reverts And Smoke Cleanup

First classify Leo's intent. There are two different revert scopes:

- Usage-only revert: remove asset usage ledger rows and recompute asset
  counters, while leaving any finished-video handoff state untouched.
- Full smoke/test cleanup: remove asset usage ledger rows, recompute asset
  counters, and withdraw the matching `release_candidates` row so the test
  video no longer appears as a distributable PGC product.

If Leo says "revert usage", treat it as usage-only unless he also says "clean
up the smoke/test", "remove it from PGC products", "make today only the real
batch", or otherwise indicates full cleanup.

If `source_batch_id`, receipt path, output directory, Drive folder name, or
other run identifier contains `smoke` or `live_smoke`, default to preparing a
full smoke/test cleanup preview instead of silently treating the request as
usage-only. Still stop for explicit approval before updating usage rows or
marking any release candidate `rejected`.

Always inspect usage ledger state and release candidate state separately. Usage
rows being zero does not prove a smoke/test cleanup is complete; an approved
linked `release_candidates` row can still appear in
`release_unassigned_candidates` and remain distributable.

Reverts run through the repo CLI (2026-06-10) — do NOT hand-write SQL against
production. The CLI encodes this section's full contract and is dry-run by
default:

Usage-only revert:

1. Preview (no writes):

```bash
python3 -m promo.cli.revert_usage --manifest-id <manifest_id>
```

   The JSON report shows usage event count, affected asset count, usage-role
   breakdown, linked release candidate statuses, and a warning if an approved
   candidate would remain visible to `release_unassigned_candidates`.
2. 🔴 CHECKPOINT · STOP, show Leo the preview, wait for explicit approval.
3. Apply with `--execute` (same command plus the flag). The CLI calls
   `rpc_revert_poi_asset_usage_manifests` — the platform RPC removes the
   manifest's usage rows and recomputes asset counters from the remaining
   ledger (never blind subtraction) — then verifies rows are zero. Exit code
   0 = verified, 1 = verification failed (report it, do not improvise).

Full smoke/test cleanup:

1. Preview (no writes):

```bash
python3 -m promo.cli.revert_usage --manifest-id <manifest_id> --full-cleanup
```

   Adds the `distribution_status` check. If any distribution row already
   claims a linked candidate (or the check itself cannot resolve), the CLI
   exits 2 and refuses to touch release state EVEN WITH `--execute` — report
   to Leo and coordinate with zhongtai before doing anything else.
2. 🔴 CHECKPOINT · STOP, show Leo the preview, wait for explicit approval.
3. Apply with `--execute`. The CLI reverts usage (skipping it when rows are
   already zero — zero usage does NOT short-circuit the candidate
   withdrawal), marks linked candidates `status='rejected'` (rows are never
   deleted; the audit trail survives), and verifies: usage rows zero,
   candidates rejected, and candidates absent from
   `release_unassigned_candidates`.

The CLI never deletes `release_candidates` rows and never touches Drive
files. Do not delete the Drive file unless Leo separately asks for media
cleanup.

## Danger List

- Do not write usage from an invalid manifest.
- Do not register `release_candidates` without a durable `drive:<file_id>`.
- Do not write `distribution_status` from PGC.
- Do not silently relax POI filters or classification requirements.
- Do not auto top-up unless Leo asks.
- Do not use local Downloads or VPS temp paths as final media URIs.
- Do not continue producing a quarantined POI.
- Do not revert by manual row deletion or count subtraction.
- Do not leave a smoke/test `release_candidates.status = 'approved'` row after
  Leo asks for full cleanup.
- Do not delete release candidate rows for smoke cleanup; mark them `rejected`.
- Do not modify `distribution_status` from PGC cleanup.

## Final Report

Always finish with:

- requested/succeeded/failed video counts;
- selected/skipped POIs and shortage if any;
- quarantined POIs;
- receipt path;
- usage writeback summary;
- release candidate summary;
- review package path only when review/download was requested;
- exact next action if top-up, revert, or handoff repair is needed.

For revert or smoke cleanup close-outs, always include:

- manifest IDs checked;
- usage rows found and usage rows reverted;
- linked release candidate ID, status before, and status after;
- whether `release_unassigned_candidates` was checked;
- whether any `distribution_status` rows caused cleanup to stop;
- whether the remaining state is usage-only reverted or fully removed from PGC
  product candidates.
