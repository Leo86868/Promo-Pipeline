# PGC Pipeline

Standalone narrated promo-video pipeline. Inputs a directory of clip mp4s for one POI, outputs a 30–65 second vertical promo (1080×1920) with synthesized narration, on-screen captions, background music, and a Remotion render.

The repo is local-first. The shortest path is:

```bash
python3 -m promo.cli.compile_promo \
  --poi "Hotel Name" \
  --local-clips material/<slug>/clips \
  --target-duration-sec 30 \
  --n-variants 1 \
  --output-dir output/run-1
```

## 4-stage pipeline

```
clips/                        → MiMo V2 Omni            → scene descriptions per clip
                                (clip_analyzer)
                                                        → Gemini #1                → narration text + pause tiers
                                                          (script_generator)
                                                                                   → ElevenLabs TTS  /  Gemini Kore TTS    → narration.mp3 + word_timestamps
                                                                                     (tts_engine; MMS_FA forced-aligns the Gemini path)
                                                                                                                            → Gemini #2                → per-phrase clip_id + trim_start
                                                                                                                              (clip_assigner; soft-hint embedding retrieval)
                                                                                                                                                       → Remotion                → final MP4
                                                                                                                                                         (remotion_renderer)
```

**Where to read next:**

- **Run it** → this README has everything you need.
- **Understand it** → start with [`promo/core/architecture.md`](promo/core/architecture.md) — folder-level navigator that threads the 8 subfolders + 8 root modules in plain English.
- **Change internals** → [`architecture.md`](architecture.md) — engineer-facing project bible (two-space invariant, sidecar producer/consumer table, LLM quarantine charter, module graph, extension points).

## Install

Prerequisites:

- Python ≥ 3.11
- Node.js ≥ 18 + `npm`
- `ffmpeg` on `$PATH`
- ~1.2 GB MMS_FA bundle lazy-downloaded to `~/.cache/torch/hub/checkpoints/` on the first Gemini-TTS run

```bash
# Python
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.lock        # if present; otherwise -r requirements.txt
pip install -e .

# Remotion (one-time)
( cd promo/remotion && npm install )
```

## Vendor credentials

Three keys are required (`.env` or shell exports). See `.env.example` for the canonical list and optional knobs.

| Variable | Used by |
|---|---|
| `OPENROUTER_API_KEY` | MiMo clip analysis + OpenAI text-embedding-3-small |
| `GEMINI_API_KEY` | Gemini #1 (script) + Gemini #2 (clip assignment) + Gemini TTS path |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS path (only required if a configured voice routes there) |

Optional: `GEMINI_MODEL`, `PROMO_CLIP_MODEL`, `PROMO_RENDER_CONCURRENCY`, `PROMO_FORMAT_SELECTOR`, `PROMO_DEFAULT_DURATION_SEC`, `PROMO_DEFAULT_VARIANTS`, `PROMO_DEFAULT_SCRIPT_CANDIDATES`.

## CLI surface

| Command | Purpose |
|---|---|
| `python3 -m promo.cli.compile_promo` | End-to-end: clips → narration → assignment → MP4. Primary entry point. |
| `python3 -m promo.cli.select_batch_pois` | Read-only Supabase POI selector: random eligible POIs, cooldown, active-asset threshold, batch JSON output. |
| `python3 -m promo.cli.prepare_drive_staging` | Builds manifest-backed Drive staging inventory and handoff items from raw Drive file IDs. Does not upload. |
| `python3 -m promo.cli.usage_events_writeback` | Explicit usage-event dry run/writeback. With `--execute`, verifies rows in `poi_asset_usage_events` after the RPC. |
| `python3 -m promo.cli.smoke_local_render` | Minimal local-render smoke (no vendor calls). `--dry-run` skips `ffmpeg`. |
| `python3 -m promo.cli.build_embedding_index` | Optional warm-up: pre-compute per-POI embedding sidecar so retrieval narrows Gemini #2's clip pool. Skip on first run — the pipeline degrades gracefully to full-pool. |
| `python3 -m promo.cli.render_architecture` | Re-renders `architecture.md` to a local `architecture.html` with Mermaid diagrams (gitignored). |

## Production operations

Daily Supabase-backed PGC batch work is governed by the repo-local skill at
`.codex/skills/pgc-production-batch/SKILL.md`, the human runbook at
`docs/operations/pgc_daily_runbook.md`, and the production contract at
`docs/operations/pgc_production_contract.md`.

After changing the skill, refresh the installed Codex copy with:

```bash
scripts/install_repo_skills.sh
```

## POI material convention

Operator-supplied clip pools live under `material/<slug>/clips/` and are gitignored (only `material/README.md` + `material/.gitkeep` are tracked).

```
material/
└── <slug>/                            # lowercase + hyphens
    ├── clips/                         # mp4s; consumed by --local-clips
    ├── .mimo_cache/                   # auto-managed by clip_analyzer
    └── .embedding_cache/              # auto-managed by clip_embedder + build_embedding_index
```

`<slug>` is `promo.core.sanitize_poi_name(display_name)` — the conversion from `--poi "Hotel Xcaret Arte"` to `hotel-xcaret-arte` is automatic; the `--local-clips` argument is the operator-supplied pool path.

## Arsenal — the 4 operator libraries

`promo/arsenal/` holds the data the operator extends over time. Logic stays in Python; data leaves Python. Every consumer reads through `promo.core.arsenal_loader`.

| Library | What it holds | Recipe |
|---|---|---|
| `arsenal/system_prompts/*.md` | The 4 LLM prompts (MiMo, Gemini #1, F3-retry, Gemini #2). | Bump `_v2.md` to invalidate caches; never edit `_v1.md` in place. |
| `arsenal/voices/catalog.yaml` | Voice catalog (Gemini Kore + ElevenLabs voices). | Append a new top-level key; `backend` field selects dispatch. |
| `arsenal/personas/*.yaml` | Narrator personas — voice / tone / perspective only. | Drop a YAML; `RandomPersonaSelector` picks it up automatically. |
| `arsenal/script_skeletons/*.yaml` | Promo format templates (`short_30s`, `long_65s`, …). | Drop a YAML with a unique `mode`; `arsenal_loader.load_format_templates()` picks it up. |

See `promo/arsenal/README.md` for the full extension recipes.

## Tests

```bash
python3 -m pytest -m "not live" -q
```

Live-only tests (real vendor calls) are gated behind the `live` marker. The non-live suite is what `ci/handoff_check.sh` runs.

## CI gate

```bash
./ci/handoff_check.sh                  # pytest + smoke (no vendors)
./ci/handoff_check.sh --full           # + packaging dry-run + live compile_promo
```

`--full` requires a populated `.env` and an active POI under `material/<slug>/clips/`. Edit `ACTIVE_POI` / `ACTIVE_SLUG` in `ci/handoff_check.sh` to point at whatever pool you currently have.
