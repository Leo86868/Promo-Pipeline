# PGC Pipeline — Forward Roadmap

**Protocol: see the `roadmap-discipline` skill** (3-layer division · single-writer · done-migrate-out · event-triggered update · milestones-not-PRs).
This file = the **position layer** (coarse, stable). PR-level detail → `gh pr list`; ground truth → the shared Supabase (`release_candidates` / `poi_asset_usage_events`) + VPS worktree deploy state; **deep detail / design contracts / full execution log → `docs/ROADMAP.md`** (the heavy doc); history trail → `workflow/daily-log.md`.
**Last verified:** 2026-06-16 (scribe checked vs Supabase `release_candidates` + main HEAD).

---

## In plain words: what stage is this in

PGC's video-gen **engine is built and mature** — the deterministic packer (翻转二) is the sole
selection engine; the receipt/resume state machine, tail pipelining, and type-card system all
landed and are live. The **product main-line has one clear next station (P5)**; everything after it
is **decision-gated** (waits on Leo's product calls), so it's normal that there's no obvious
"engineering push point" — the wheel is built, the steering is Leo's. Recent weeks' work was
**off the product P-line**: a cross-paradigm dedup integration with AIGC + operational hardening,
all merged and live as of `ac62df1`.

## Progress bar (milestones not PRs; for the PRs → `gh pr list`)

```
产品主线 (P-roadmap)   ⏳   at P5 门口 — P3/P3.5/P3.5b/P4 全✅;P5(TTS静音)未进
跨范式去重 H1          🟡   PGC侧完工+生产实证;AIGC P1e唯一索引 ⚠️待确认开没开
运营硬化               ✅   preflight探活/resume提速/cooldown软化/tail-workers4 → 合入 73eb804
库存生产               ⏳   720-only过渡政策 + 强制升级;持续补库存(soft-cooldown已救饿死)

Recently closed (done → migrate out; detail → docs/ROADMAP.md §执行日志 / gh):
  P4测试健康 ✅   P3.5b架构校准 ✅   recipe_input(PGC) ✅   soft-cooldown ✅   tail-workers 2→4 ✅

Active lanes: 3   ← ⏳/🟡 行数(产品主线 · 去重H1 · 库存生产)
```

## Layered backlog

**This week (in flight / next):**
- 🔐 轮换暴露的 key `de9214e4`(库存批跑完后)— red-line。
- P1e 唯一索引确认(AIGC 侧;跟踪在 `workflow/CROSS-REPO.md`)。
- P5 判断:用今天的真静音数据判"降 pause_cap 小旋钮 vs ffmpeg 静音清理大改"——先判再做。

**Queued (want to, no slot this week):**
- P5 本体(TTS segment 间静音清理,§4a;翻转二已落地,word_timestamps 耦合自动安全)。
- 跨范式 cooldown 设计拍板(PGC 选片该不该跨范式数 usage)— Leo 决定。

**Triggered (build only when the condition fires):** see Triggers below.

## Live red-lines (only those bound to an in-flight lane)

- **056 已开 → 任何 PGC 批必须从 `73eb804`+ 代码跑**;从旧 worktree(recipe_input 为空)跑 → RC 插入被 056 拒(fail-loud)。
- 🔐 暴露 key `de9214e4`(对话明文泄露过)未轮换 — 用着先,尽快换。
- 720-only 是过渡税:每条都得走 WaveSpeed 升级(花钱 + 升级长尾堵车);素材库原生 1080 后作废。

## Cross-repo (PGC ↔ AIGC asset_platform)

- Handoff board: see `workflow/CROSS-REPO.md`. Iron rule: board ≠ lock; correctness lives in code + the shared `release_candidates` DB constraints (056 trigger / P1e unique index).

## Triggers (build only at the condition; don't build now)

| Deferred item | Graduation trigger |
|---|---|
| 720p 真超分 A/B → 脱离 720-only | 素材库出现足量原生 1080(查库判时机) |
| 120s type | 先与 AIGC 对资产门槛(~90-100) |
| 价格政策 / POI 档案袋 | Leo 拍板(禁价 or 喂真实档案);纯 arsenal 一行 |
| 停顿中切镜(mid-silence bridges) | Leo 看片决定(口味题) |
| ffmpeg vs Remotion 调研 | upscale 消失后渲染重新成大头时 |
| 翻转三(分发数据回流) | 远期;manifest 钩子已留好 |

## History pointers (done; detail → docs/ROADMAP.md §执行日志 / git / daily-log)

- **2026-06-16 ✅**: 跨范式去重 recipe_input/H1 上线(056 开、end-to-end 通)+ 运营硬化四分支合入 `73eb804`(186 tests green)。详见 `docs/ROADMAP.md` §执行日志 2026-06-15→16。
- **2026-06-15 ✅**: P3.5b 架构圣经校准 + P4 测试健康。
- **2026-06-11 ✅**: 翻转二 cutover(packer 唯一引擎,Gemini #2 退役 `1f28902`)+ P2 type 卡片化。
- 更早(止损/翻转一/尾巴流水线)→ `docs/ROADMAP.md` §执行日志。

## projects directory

Active project detail → `workflow/projects/<name>/`(已有 pgc-batch-production / shared-poi-asset-library 等);this file holds only the global position layer.
