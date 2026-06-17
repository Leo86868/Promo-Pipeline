# PGC Pipeline — Daily Log

The **history layer**: what happened, by day. `workflow/ROADMAP.md` holds the *position* (where we
are now); this holds the *trail*. Newest on top. **Deep per-day detail (commits, contracts) lives
in `docs/ROADMAP.md` §执行日志** — this file is the lean position-layer trail + decisions; it
points there for the heavy narrative.

> **Migration rule:** when a lane closes on `workflow/ROADMAP.md` (done → migrated out of the
> progress bar), its one-line summary lands here under that day's entry.

---

## Milestones (the spine — big closes, newest first)

- **2026-06-17** — roadmap-discipline 按更新版 skill 重建(Mermaid 版 workflow/ROADMAP.md)+ backlog 剪枝;cooldown 设计拍板=**改成范式各算自己**(待落地);reviewer 交接。
- **2026-06-16** — 跨范式去重 recipe_input/H1 上线(056 开,end-to-end 通)+ 运营硬化四分支合入 `73eb804` + roadmap 补到今天 + roadmap-discipline skill 装上(跨仓 board)。
- **2026-06-15** — P3.5b 架构校准 + P4 测试健康完工;库存批 401 仗(换独立 key)。
- **2026-06-11** — 翻转二 cutover(packer 唯一引擎,Gemini #2 退役)+ P2 type 卡片化。

---

## 2026-06-17  (reviewer 交接点)

### What happened
- roadmap-discipline skill 更新了(6 文件重写,加 Mermaid)→ 按新版重建 `workflow/ROADMAP.md`(旅程弧 + 历史时间线 Mermaid + 排期/排队/触发卡片);`CLAUDE.md` 指针改回模版原话 + repo-specific 另起;daily-log/CROSS-REPO 模板没变、原样留。
- backlog 剪枝:**删** 停顿切镜(纯锦上添花);**P5 撤出活跃位置**(Leo 倾向不做 → 旅程图 ▶now=成熟期,docs §当前排期 标 ⏸ 暂不做、不再是"下一关");**120s** 改成"加新视频类型(type)"一眼懂;**价格政策** 改成"POI 档案信息"+ 标边界。
- 讨论清:PGC 处**成熟期**——引擎建完、库存满(169 approved)、fresh POI 池≈干;真瓶颈在上游(POI 供给/脱 720),不在 PGC。PGC 自己能推的只有两个 ⭐(速度/并行、输出调优);判断:**输出调优 ROI > 速度**(POI 受限时,提速=更快重复老店)。

### Decisions (with the why)
- **cooldown 改成范式各算自己(不跨范式)** — why:跨范式 cooldown 让 music_remix 一忙就把 PGC 选片池冷干(实测 145 POI 被它冷);soft-cooldown 是创可贴,paradigm-scope 才是根治。**⚠️ 待落地**(见 Next)。
- key `de9214e4` **不轮换** — Leo 接受 transcript 暴露面(风险有限)。
- roadmap 不重复建:`workflow/ROADMAP.md`=精简 position 层,`docs/ROADMAP.md`=详情/设计/全历史层(被指向)。

### Next (teed up for the new reviewer)
- ✅ **DONE 2026-06-17(branch `feat/cooldown-paradigm-scope`)cooldown paradigm-scope**:`batch_selection.py::fetch_recent_usage_poi_ids` 加 `.like("run_id","pgc_run_%")` 只数 PGC 自己 usage + 注释 + 测试钉死;595 单测绿。**交接里"music_remix=`music_remix_*`"的假设不准** → 实证(14749 events):非 `pgc_run_` 的全是 music_remix,含 **`eu_expl_720drain_*`(看着像 720 榨干,其实是 music_remix,产物在 `video_paradigms/music_remix/`、16s)**。`pgc_run_%` allowlist 真库 fidelity 实测 A==B(53 POI,0 误伤),`_` 是 LIKE 通配但当下数据无误伤。⚠️ soft-cooldown 保留(互补)。**未做端到端:VPS 还是旧码,需部署后真跑一批验选片。**
- 🔎 **P1e 行为级实证(2026-06-17)**:`release_candidates` 1352 条带指纹非 rejected 行 = **0 重复**(现在没双发)。但"索引物理装没装"PostgREST 读不到目录 = **仍需 AIGC 一句确认**(行为干净 ≠ 索引一定在)。
- 两个 ⭐ 待办(速度/并行、输出调优)等 Leo 想推时开;荐先输出调优(先摸清现状)。

### Don't do (yet)
- 别重新加回 停顿切镜 / P5(已是 Leo 决定撤的)。
- 别从 <`73eb804` 的旧 worktree 跑生产批(recipe_input 空 → 056 拒)。
- 别轮换 key(Leo 决定不换)。
- cooldown 落地时别盲收窄 23505 索引名(等 P1g 抓真错形状)——这是另一条线。

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
