# PGC Pipeline — Daily Log

The **history layer**: what happened, by day. `workflow/ROADMAP.md` holds the *position* (where we
are now); this holds the *trail*. Newest on top. **Deep per-day detail (commits, contracts) lives
in `docs/ROADMAP.md` §执行日志** — this file is the lean position-layer trail + decisions; it
points there for the heavy narrative.

> **Migration rule:** when a lane closes on `workflow/ROADMAP.md` (done → migrated out of the
> progress bar), its one-line summary lands here under that day's entry.

---

## Milestones (the spine — big closes, newest first)

- **2026-07-01** — **hardening 会(全仓摸底)**:四路侦察(架构+死代码 / skill 无人值守缺口 / 6月commit史 / AIGC Main 参照)。关键判定:①单视频引擎已是无人值守级,人卡在边缘(拼命令/盯stall/薄池拍板)→ build list = 启动wrapper+看批watchdog;②查实 **AIGC Main 自己也无无人值守编排**(可借积木=receipt软锁/finalize真DB校验/退出码契约/cron三态wrapper,详 memory `aigc-unattended-reality`);③死代码仅三小堆(6个 research_*.py 草稿 + retrieval_dry_run + ~10 已合并分支),架构主体干净;④账面漂移回填(本文件 06-25/27 脊柱线补记 + db-first PLAN.md 入库 + roadmap 校准)。定位:**两线一闸** — 质量线(检索分诊,用已有 sidecar 数据)+ 硬化线(账面→wrapper→清扫);闸口=首批 armed 活体批(CPU 被占,等空)。
- **2026-06-27** — **DB-first 全库配片 armed 进生产标准**(main `d627ecc`):全局 Hungarian 指派(06-26 `6b02e17`)+ 全库选片拿掉 top-30 下载上限、download-after(`d85d321`);816 测试绿、三方独立评审、resume 安全、默认关=急停旗标。⚠️ **首批 watched 未跑**(claim-2 fail-loud 会拒覆盖缺口 POI,新行为待活体看)。前夜(06-26)swarm 16-agent 判定:近似去重仅占痛感 ~5%,**真开口 = 检索 relevance(70%)**(选中片余弦中位 0.305、18% beat <0.20;但低余弦≠病,待排序质量分诊)。
- **2026-06-25** — **视觉去重 armed 进生产标准**(main `9e16f64`):工单① packer DINOv2 视觉近重复软闸(`--near-dup-threshold 0.85`)+ 工单② 下载选片视觉 max-min(`--download-diversity`,resume 安全,含 candidate_only 接线修复 `2c55e88`);65s A/B:残留近重复对 1→0 / 3→0,~零相关性代价。余项=首批 armed 活体验 `visual_pool>0`。同期顺手:cdn-egress 换公链下载(`5ff48ff`)+ 旁白 EBU R128 loudnorm(`fff5a24`)。
- **2026-06-23** — **720→1080 切换 live + 实证**(main `2c03d9a`):min_width ≥1080 下限 + 完整-predicate 搁浅(102存活/27搁浅)+ resume 守卫 + L-001 默认安全 + footgun 堵 + SKILL 翻;live smoke(Club Wyndham 1×2)ffprobe 真1080/零WaveSpeed/render 5–6分 vs 16 → **71% 提速杠杆落地**。多轮 panel(3-agent ×2 + 跨仓对齐)拦掉裸切泄漏/指纹污染/静默烧钱。poi_description 桥 + A/B(事实正确性刹车)同期上线。首个 1080 生产批 `stock_1x2_…002353Z`(Club Wyndham,2/2 入库)顺手发现成片素材**近似重复** → 只读实验证 embedding 余弦能判近似(0.942=同镜头)→ **挂 P0 明日 #1**(packer MMR 去重,dry-run 先;详 ROADMAP)。另记毛病 A 清晰度(软-但-1080,无字段可判,跨仓 AIGC 活)。
- **2026-06-18** — cooldown 范式各算自己**上线**(branch,595 绿)+ arsenal 操作手册成文 + 渲染提速调研收口(只白嫖 swangle/concurrency,GPU 没用、渲小再升否)+ POI 软锁**建好 reviewed**(739 绿,加原子写中)+ POI 档案(hotel_description)跨仓对齐(一列,与 AIGC reviewer 双向 sync)。
- **2026-06-17** — roadmap-discipline 按更新版 skill 重建(Mermaid 版 workflow/ROADMAP.md)+ backlog 剪枝;cooldown 设计拍板=**改成范式各算自己**(待落地);reviewer 交接。
- **2026-06-16** — 跨范式去重 recipe_input/H1 上线(056 开,end-to-end 通)+ 运营硬化四分支合入 `73eb804` + roadmap 补到今天 + roadmap-discipline skill 装上(跨仓 board)。
- **2026-06-15** — P3.5b 架构校准 + P4 测试健康完工;库存批 401 仗(换独立 key)。
- **2026-06-11** — 翻转二 cutover(packer 唯一引擎,Gemini #2 退役)+ P2 type 卡片化。

---

## 2026-07-01  (reviewer/orchestrator session — hardening 会)

### What happened
- **四路并行侦察收齐**(架构+死代码 / skill 无人值守缺口 / commit 史 / AIGC Main 参照),结论见脊柱线。另放一个对抗式审计(6月各合并"宣称 vs 代码")在跑。
- **账面回填**(本 commit):06-25 / 06-27 两条脊柱线补记;`workflow/projects/db-first-assignment/PLAN.md` 从 untracked 入库(roadmap 指向它但一直没提交=丢失风险);roadmap 校准(Last-verified 重盖、两条"进行中"降级为实况)。
- 首批 armed 活体批(watched)**因 VPS CPU 被占推迟** — 设计已定:fresh-context agent 只拿自然语言订单+skill 跑标准随机路径 stockpile 4×3,边跑边写决策 journal(= skill 漏洞探针),一批验三样(DB-first watched / visual_pool>0 / 检索分诊底料)。第二批才做定向 `--batch` 实验(避开第一批 POI;此路径绕过 cooldown 且默认 30s,必须显式 `--target-duration-sec 65`)。

### Decisions (with the why)
- **分两批(标准随机批 → 定向实验批)** — why:一批一个变量,agent 卡壳时才分得清是 skill 漏洞还是点名路径怪癖;点名路径绕过 cooldown、有 30s 默认坑,不配当首批。
- **无人值守 build list 只先做 ①启动 wrapper ②看批 watchdog** — why:③负载感知 timeout ④薄池预声明策略等真遇到再说(简单优先);wrapper 与 roadmap 已挂的"常驻 deploy 简化"提案是同一件事,合并做。
- **检索 70% 大工程先不动,先做排序质量分诊** — why:低余弦(0.305 中位)可能是文风差的正常值(Leo 质疑成立);swarm 冲浪案例里排序把对的片排 0/1/2,败在旧贪心分配——而分配已被 Hungarian 修掉。分诊分清 (a)匹配弱 (b)分配吃片(已修) (c)真缺口,若 (b) 占大头则大工程直接降级。用 VPS 已有 `clip_assignments_*.json` 即可,不等 CPU。
- **聊天里禁用 mermaid** — Leo 终端只见源码;回复用 ASCII 字符画,mermaid 仅进 .md 文件。

### Next
- 硬化线:① wrapper(合并"常驻 deploy"提案)② 死代码清扫(6 research 草稿 + retrieval_dry_run + 已合并分支;sidecar 模块不动)。
- 质量线:检索排序分诊(VPS 轻活,不等 CPU)。
- 闸口:CPU 空 → 放 fresh-context agent 跑首批 armed 活体批。
- 待 Leo:`feat/arsenal-quality` 去留(1 ahead / 48 behind,最后动静 06-22;建议摘 doc 归档)。

### Don't do (yet)
- 检索大工程(richer 描述/CLIP 联合空间)— 分诊没出结果前不动。
- 第二批定向实验 — 首批没跑完不动。
- 别删 `clip_assignment_sidecar.py`(测试 replay 在用)。

### Later same day (22:44 — 分诊回收 + 三项 cross-check + 大账纠错)
- **对抗审计回收(6月合并宣称 vs 代码)**:零推翻,192 测试现场重跑全绿。两个真发现:①视觉去重闸门 fail-open 无覆盖率地板——armed 下上游指纹作业滞后会**静默解除且 receipt 仍显 armed**,DB-first 后它是唯一去重防线 → 修=覆盖率 <0.9 告警(小活,进硬化线);②POI 软锁只扫同 runs-root——per-batch worktree 布局下跨树互不可见 → 常驻 deploy 提案顺手治好(+1票)。次要:cdn-egress 空说明=虚惊(分支 commit 有全文+sha256 兜底);claim-2 爆炸半径=单条视频非整批。
- **检索分诊回收 + 本席 cross-check(判例卡数字+语义双核、receipt 逐一验)**:49 个弱 beat 分类 → **84% (d)假警报**(度量假象:1-2 词碎拍子中位 0.240 vs 10+词 0.438,r=0.438;营销抽象句无视觉指代物)/ ~9% (a)真排序失误(仅 1 例清晰:白天片配"天黑了"旁白,时间属性丢失)/ ~5% (b)且零旧-greedy 型(Hungarian 后没有饿死案例,只剩"一句拆两拍+素材只有一条"的结构性)/ 2% (c)缺口。后-flip 真实中位 **0.376**(swarm 的 0.305 已过时)、<0.20 仅 **7.6%**(vs 18%)。**判决:「检索 70% 大工程」证据不支持**;便宜替代=①修度量(碎拍子不计 KPI)②packer 允许 ≤2 词碎拍继承上一拍片(治 Card6/7 型)③时间/天气属性小探针④下架 off-brand 素材(no-trespassing 围栏那种)。待 regroup 裁决。
- **大账纠错(ground truth 推翻两小时前的本账)**:VPS `/home/deploy/pgc_runs/` 存在 **06-27/28 五个 armed 生产批**(batch3x3/gold1x3/batch5x3/gold_top7/gold_make5),receipt 逐一核:**63/63 complete、0 quarantine、db_first 全 true**;闸门真喂料(`visual_embeddings_attached` 82–117/条,那个 `visual_vectors_available 0` 是已退役工单② 的装饰字段=虚惊)。→ 两个 watched 义务**已事实闭环**;「首批 armed 未跑」为过时账面(本 roadmap 自己的宣称被沿袭)。⚠️ 待对:这五批是谁跑的(哪个 session)、为何未记账。
- **skill 探针批照常在跑**(fresh-context 操作员,journal 模式)——watched 义务虽已闭环,它的另一半价值(无人值守可跑性探针)不受影响。

---

## 2026-06-18  (reviewer + 多 worker session)

### What happened
- **cooldown 范式各算自己上线**:`batch_selection.py` 加 `.like("run_id","pgc_run_%")` 只数 PGC 自己 usage + 注释 + 测试钉死;595 单测绿;真库 fidelity 实测 A==B(53 POI,0 误伤)。commit 在 branch `feat/cooldown-paradigm-scope`(连 daily-log + arsenal 手册 + POI-lock 提案文档)。
- **arsenal 操作手册成文**:`promo/arsenal/README.md`(74→243),全景图 + 全参数表 + "千篇一律"诊断 + 文档-vs-现实(纠正 2 条:PROMO_PERSONA_SELECTOR 没接线、加时长档零路由)。
- **渲染提速调研收口**(3 worker 卡 + reviewer 审):单条 16 分 = 渲染 29% / 升级 71%;**只白嫖 swangle + concurrency**(待 VPS 闲实测);**GPU 没用**(主体是 OffthreadVideo,不吃 GPU,已验);**渲小再升(540)否**(Leo:比 720 源还低,WaveSpeed 补不回);并行化留到 1080 后(有同-POI staged-dir 互删坑);Lambda 等爆量。
- **POI 软锁建好 + reviewed**:in-progress POI lock(选片前硬排除未完成兄弟批认领的店),默认开。3 agent + reviewer 自审:逻辑过硬、4 判断点全对、739 绿;**拆穿一个 agent 误报**("60s 窗口挡不住 26s Sandpearl"——扫描在拉数据之后紧挨写,残留窗口亚秒级,原始事故会被挡)。在 branch `feat/in-progress-poi-lock`,**加原子写中**(对齐 music_remix `write_receipt_atomic`,抄漏了)未 commit。
- **POI 档案(hotel_description)跨仓对齐**:决定**一列自由文本**(不分 notable_details,避免必背清单→雷同);防重机制暂不上;契约只要"真实渠道好描述、不强求独特";与 AIGC reviewer 双向 sync——双方都发现"读-转发桥未写"(`_SNAPSHOT_FIELDS` 不含该列 + run_batch 零转发),白板 H2 已改准。

### Decisions (with the why)
- cooldown allowlist=`pgc_run_%`(非 denylist)— why:实证非 pgc_run_ 的全是 music_remix(含误导性 `eu_expl_720drain`),denylist 会错收。
- 渲小再升(540→1080)**否** — why:渲在 540 比 720 源还低,先自废源细节再让 WaveSpeed 补,一定更糊。
- POI 档案**一列** hotel_description — why:单列"要点"块会被模型当必背清单→过度依赖+同类店雷同;一段背景 prose 是参考不是考点。
- POI 软锁 fail-closed 无 TTL(沿用 06-17 决定)+ 加原子写 — why:原版 music_remix 本就原子写,PGC 抄漏了;补回=对齐,堵住并发半写读的最后小缝。

### Later same day (上线 + 速度实测)
- **cooldown + POI 软锁 + arsenal 手册 + 文档 全部并入 main**(快进 `ea23b09..190f227`,已推 origin,742 绿)。POI 软锁先过**真库对照烟测**(锁开两批不撞、锁关同 seed 撞=对照证明)才并。**未部署 VPS**(等下次跑批)。push/merge 自此归 reviewer(见 [[feedback_reviewer_owns_push]])。
- **速度实测收口**(worker 真跑 3 配置,详 `docs/research/render-speedup-2026-06.md`):现设置已最优,**main 一行未动**。swangle **否决**(2.2× 慢,推翻"白嫖"推理);conc 保持 6;**8 核并行被 ffmpeg ~5 线程地板堵死** → 真并行只能加核/换机。

### EOD 收口(2026-06-18)
- **部署完成**:开新 worktree `main_20260618T_cooldownlock @ f06d4b9`(含 73eb804 不回退)+ npm + .env + VPS 环境 31 测试绿。**下次跑批从它跑 → cooldown + 锁生效**。runbook 示例已改指向它(+红线警告,别从 .git 主仓旧 checkout 跑)。
- **清理**:本轮 3 分支删净(cooldown/poi-lock/render-speed)。"老 worktree" `main_20260608` 查实=**VPS .git 主仓(不能删)**,其内容=6-9 引擎旧草稿(实测无 origin 没有的活东西;1143 行未提交已存 patch 备查)。
- **PGC 状态 = 闲置**:手头无活;唯一开着的 = H2 hotel_description(AIGC 前置)。其余等 720→1080 / 加核(都 Leo 拍板)。

### Next
- **POI 软锁**:✅ 上线 + 部署 + 真库烟测 PASS,全收口。下次跑批从新 worktree 跑即生效。
- **速度**:VPS 闲时跑 swangle/concurrency 实测(worker 走正式 Protocol 真产视频观察)。
- **POI 档案**:AIGC 前置发列(contract PR §1.1+视图)→ 之后 PGC 写读-转发桥(`_SNAPSHOT_FIELDS`+run_batch 转发+`safe_substitute`)。PGC 不抢跑。
- ✅ **AIGC P1e 唯一索引 = 开了**(Leo 2026-06-18 确认)→ H1 跨范式去重端到端闭环(盖指纹+真拦+0 重复)。
- arsenal README 加"症状→抽屉"决策树(Mermaid)。
- ⚠️ 发现:`main_20260608T000000Z @ e3600e9` 是 6-7 岔出的**老 stale worktree**(18 老 commit + 1143 行未提交,非生产);runbook 示例却指向它 = 谁照抄跑批会用老码、recipe_input 空 → 056 拒。待 Leo 确认废弃后清 + 修 runbook 示例。生产实际从 per-deploy worktree(如 `73eb804`)跑。

### Don't do (yet)
- POI 档案桥别在 AIGC 发列前写(空跑)。
- 速度测试和 POI 锁真测别并行(污染计时)。
- 渲小再升别上;GPU 别租。

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
