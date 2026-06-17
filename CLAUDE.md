# PGC Pipeline

Roadmap discipline: `workflow/ROADMAP.md` is the position layer (milestones not PRs, single
writer, verify-before-write, done-migrate-out). Full protocol + scaffolding: the
`roadmap-discipline` skill.

## Repo-specific

- Cross-repo (PGC ↔ AIGC asset_platform) handoffs → `workflow/CROSS-REPO.md`. History trail → `workflow/daily-log.md`.
- Deep detail / design contracts (§翻转二) / full execution log → `docs/ROADMAP.md` (the heavy doc; `workflow/ROADMAP.md` points to it). New-session 60-sec bootstrap lives in its header.
- **Operating red-lines:** 056 enforcement is on → every PGC batch MUST run from main `73eb804`+ (an older worktree writes recipe_input=NULL → its RC inserts get rejected fail-loud). 720-only = transition tax (every video upscaled via WaveSpeed; goes away when the asset library is native 1080).
