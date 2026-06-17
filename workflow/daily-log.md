# PGC Pipeline — Daily Log

The **history layer**: what happened, by day. `workflow/ROADMAP.md` holds the *position* (where we
are now); this holds the *trail*. Newest on top. **Deep per-day detail (commits, contracts) lives
in `docs/ROADMAP.md` §执行日志** — this file is the lean position-layer trail + decisions; it
points there for the heavy narrative.

> **Migration rule:** when a lane closes on `workflow/ROADMAP.md` (done → migrated out of the
> progress bar), its one-line summary lands here under that day's entry.

---

## Milestones (the spine — big closes, newest first)

- **2026-06-16** — 跨范式去重 recipe_input/H1 上线(056 开,end-to-end 通)+ 运营硬化四分支合入 `73eb804` + roadmap 补到今天 + roadmap-discipline skill 装上(跨仓 board)。
- **2026-06-15** — P3.5b 架构校准 + P4 测试健康完工;库存批 401 仗(换独立 key)。
- **2026-06-11** — 翻转二 cutover(packer 唯一引擎,Gemini #2 退役)+ P2 type 卡片化。

---

## 2026-06-16  (operator + reviewer 多 session)

### What happened
- 跨范式去重 recipe_input/H1:`feat/recipe-input-dedup` 合并+部署+生产实证;AIGC 拨 056;真候选 rfp2 指纹已算出 = end-to-end 通。两范式 DB 实证都在供 recipe_input(music_remix 1165/1204、PGC 162/187),1326 approved 行 0 重复指纹 / 0 跨范式双发。
- 运营硬化四分支(① preflight 活体探活 ② resume 接 tail_workers ④ cooldown 软化 + SKILL tail-workers 2→4)→ 3 个 fresh-context agent 独立复核 MERGEABLE 后合入 `73eb804`,186 tests green,VPS 已部署。
- 库存批 `stock_4x3_…232943Z` 12/12 干净(soft-cooldown 当场救场:`fresh_eligible=0` 全靠回退才跑起来 = 生产现证;tail-workers 真 3 路并发)。
- roadmap 补到今天(`docs/ROADMAP.md` §执行日志 + §当前排期 + 降过时旗);装 roadmap-discipline skill = 建 `workflow/CROSS-REPO.md` 跨仓 board + 标准 `workflow/ROADMAP.md` position 层。

### Decisions (with the why)
- recipe_input 走 vendor 复制不 import asset_platform — **why:** CONTRACT §3.5「producer 永不 import 平台内部」+ asset_platform 没打包;golden 钉死防漂移。
- 23505 宽接不收窄 — **why:** 这张表所有唯一冲突都是"已注册跳过"语义、无一致命 → 宽接是永久正确基线,精确收窄降为 P1g 可观测性。
- cooldown 软化(不硬踢)— **why:** cooldown 跨范式读共享账本,music_remix 一忙就饿死 PGC 选片;软化让优先 fresh、不够回退 cooled。
- tail-workers 默认 2→4 — **why:** 升级长尾有 45-50min 怪兽,2 路一只怪兽就饿死流水线;尾巴网络等待不抢 CPU。
- roadmap 不重复建、docs/ROADMAP.md 当详情层 — **why:** PGC 已有重 doc,标准 workflow/ROADMAP.md 当精简 position 层指向它,避免两份竞争。

### Next
- 🔐 轮换 key `de9214e4`(库存批跑完后)。
- 确认 AIGC P1e 唯一索引开没开(跟踪在 CROSS-REPO.md)。
- 判 P5(小旋钮 vs 大改),再决定做不做。

### Don't do (yet)
- 别从旧 worktree(<73eb804)跑生产批(recipe_input 空 → 056 拒)。
- 别为修 23505 日志去盲收窄索引名(等 P1g 抓到真错形状再说)。
- P5、120s、价格政策、mid-silence、720p脱离 — 全等触发/Leo 拍板,别现在动。

---

<!-- New days go ABOVE this line, newest on top. -->
