# PGC Pipeline 下一阶段路线图(2026-06)

> **角色(roadmap-discipline)**:本文件 = **详情层**(设计契约 §翻转二 + 全量 §执行日志 + 深度 §当前排期)。精简 **position 层**(进度条/lanes/红线/触发器)→ `workflow/ROADMAP.md`;跨仓 handoff board → `workflow/CROSS-REPO.md`;历史 trail → `workflow/daily-log.md`。两边里程碑同步,正确性永远在代码/DB。

> **新 session 自举(60 秒)**:① 读本文件(重点:§当前排期 + §执行日志 最后一天);② `git log --oneline -15` 看落了什么;③ auto-memory 会自动带上下文;④ **已完成项的工作原理在 `LEARNING.md`(同 docs/ 目录)**(给 Leo 的学习手册;P2 的 type 卡片体系在 §14)。**已完成勿重做**(均已实战验证):第一二梯队、翻转一(receipt 状态机 + `--resume`)、尾巴流水线化(`--tail-workers`,双批 6 分/条实测)、**翻转二全链 + Gemini #2 遗产退役**(排片员是唯一选片引擎,回滚 = `git revert 1f28902`)、P1 范文冲突修复、**P2 type 卡片化七步**(性格进 skeleton YAML、路由查表、hook 发牌接 per-video seed、范文法规)。**下一步看 §当前排期**。VPS 部署流程:push origin → 新 worktree → **必须 `cd promo/remotion && npm install`** → preflight → detached 启动。

**写作日期**: 2026-06-09
**当前 main HEAD**: `4453d78`(F3 split-repair 止血已落地)
**来源**: 37-agent 全库深扫(31 条发现全部经独立对抗复核确认)+ 三轮架构讨论
**读者**: Leo + 任何接手的 agent(Codex / Claude / 人)

---

## 0. TL;DR

- **不做大重写**。现有 core 的"复杂"大半是生产事故换来的修复(validator 里每个分支都带审计编号),重写会丢掉这些知识。
- 取而代之的纪律:**过手即终态**——路线图每碰一个区域,就把它留成终态质量(新模块 + 测试 + 该目录 architecture.md 更新)。走完路线,最烂的两个区域(`assign/` 的 Gemini #2 面、`run_batch` 的 autopilot 尾巴)自然就是全新的了。
- 顺序:**第一梯队止损(约 2-3 天)→ receipt 状态机化 + resume → beat planner 替换 Gemini #2(先 A/B 后切换)**。
- 反馈闭环(分发数据回流)P0 低,放最后;manifest 已记录每条视频的 script/clips/音乐,钩子已留好,无需现在动。

---

## 当前排期(2026-06-16 刷新)

**已完成(真实环境体检通过 2026-06-14,P4-health 2×3 smoke 全链绿)**:
- ✅ **P3 根目录瘦身**:5 份 .md → docs/(改名 ROADMAP)、README 目录树、registry 唯一换模型入口、自举路径+memory 指针同步(C1/C2/C4);
- ✅ **P3.5 架构圣经重写**:root `architecture.md` 全量重写到 packer 现实 + 数据流图上墙 + 焚草稿;连带 umbrella `core/architecture.md`、`llm/architecture.md`、README 校准;《退役档案》留回滚记录;
- ✅ **P3.5b stage 子目录圣经校准**:`pipeline/render/narrate/script/architecture.md` 把当现役写的 Gemini #2 + F3 改到 packer 现实(退役/回滚语境保留);4 本独立 commit、reviewer 双层过(硬验收 + fresh-agent 逐符号对码,零新错)(2026-06-15,`5f5f9a6..2cbcf30`);
- ✅ **P4 测试健康**:assert-weld → 验产物;私有内脏焊点 → stub 真边界+跑真逻辑+验输出(test_compile_promo / clip_analyzer / wavespeed,变异检验防假绿);协作者隔离 🟡17 + IO seam 🟢5 + crux 40 私有边界包装按裁定**留**;项目级测试约定写进 LEARNING §15。

**已完成(2026-06-16,主线外 — 跨范式去重 + 运营硬化,生产实证)**:
- ✅ **跨范式内容去重(recipe_input / H1)**:RC insert 供有序 `source_content_hash`(排音乐排 trim),DB **056 触发器**算 `recipe_fingerprint`(PGC 不碰);vendor 逐字节复制平台 oracle + golden 钉死防漂移、23505 宽接+诚实日志逐条跳(`release_candidates` 上所有唯一冲突皆"已注册跳过"语义、无一致命 → 宽接是永久正确基线,精确收窄降为 P1g 可观测性)。`feat/recipe-input-dedup` 合并部署上线,**056 已开、真候选 rfp2 指纹已算出 = end-to-end 通**。边界守法同 usage 账本(供数据/不算指纹/不 import 平台内部);vendor 复制代价(改 rfp 要重 vendor+重钉 golden)记账。**解了 line 189「去重风险现在进行时」的 cross-paradigm 那层。**
- ✅ **运营硬化四分支**(main=`73eb804`,三分支 fresh-context agent 独立复核 MERGEABLE 后合,186 tests green):① **preflight 活体探活**(死 key 开跑前就响,不再渲 8 分钟才炸)· ② **resume 接 tail_workers**(补救也并发,抽 `_AutopilotTailPipeline` 两路共享、同 POI 守门保留)· ④ **cooldown 软化**(跨范式 usage 不再硬饿死 PGC 选片;优先 fresh、不够回退 cooled,资产门槛仍硬;某批 `fresh_eligible=0` 全靠回退才跑起来 = 生产现证)· **SKILL tail-workers 默认 2→4**(治升级长尾:多数 ~700s 但有 45–50min「怪兽」,2 路一只怪兽就饿死流水线)。

**主线(按序)**:
1. ⏸ **P5 TTS 静音清理**(去 segment 间 ffmpeg 静音拼接,§4a)— Leo 2026-06-17 **倾向暂不做**(数据显示降 pause_cap 小旋钮可能就够);不再作为"下一关"。当前 PGC 处成熟期,无硬性工程主线项,详见 `workflow/ROADMAP.md`。

**等信号灯(有外部输入才动)**:
- **价格政策 / POI 档案袋**:`hotel_description`/`notable_details` 通道常年为空、价格数字来自模型记忆未核实(LEARNING §14)——等 Leo 拍板(禁提价格 or 喂真实档案),纯 arsenal 一行;
- **120s type**:开卡前先与 AIGC 对资产门槛(~90-100);
- **停顿中切镜(mid-silence bridges)**:口味题,等 Leo 看片决定;
- **720p 真超分 A/B**:过渡期税,素材库原生 1080 后作废,只在过渡期拖长时才值得;
- **ffmpeg vs Remotion 调研**:upscale 消失后渲染重新成为大头时再启动。

**远期**:翻转三(分发数据回流;manifest 钩子已留好,不关门即可)。

**跨仓协调(PGC ↔ AIGC asset_platform)**:in-flight handoff board → `workflow/CROSS-REPO.md`(当前主线 = 跨范式去重 recipe_input→056→P1e;铁律:board ≠ 锁,正确性在 release_candidates DB 约束)。协议 = `roadmap-discipline` skill。

**三块常转仪表(每个生产批顺手读)**:
1. **字数摩擦率**:LONG 下限已降 150→145(`38ad7a2`)——P4-health smoke 见 3/6 条 <150 重抽(全恢复,终稿 152-160);下个生产批验 <145 重抽率下降;
2. **F3 基线**:新配方 F3=0(批次 1 0/6)持续确认;
3. **素材消耗**:packer provenance 的 `beat_count`/`unique_clip_count`(22-24 段/条)与窗口耗尽率;新增看 `assigned_hook` 是否随规范序号走牌(P2 step 5 的生产验证)。

---

## 1. 宏观方向:三个翻转

### 翻转一(轻量版):receipt 就是状态机,不引入 Supabase

病根:resume 缺失、crash 状态模糊、串行、无时间戳、usage 双记——是同一个架构决定(batch 进程 + 局部状态)的五个症状。

**不用** Supabase 状态表(单 VPS 单操作员用不上多机协调,徒增依赖)。轻量版三件事:

1. **每个子步骤完成立刻 flush receipt**(现在是整条 autopilot 链跑完才写盘)。写盘毫秒级 vs 步骤分钟级,无感知开销。receipt 的子步骤字段结构(`render/manifest_audit/drive_upload/usage/release_candidate`)已存在,只是写盘时机和时间戳要补。
2. **每步幂等**——大部分已成立:usage event id 是确定性 sha256、Drive 按文件名+大小复用、RC 注册先 preflight。唯一破口:重跑 render 会 mint 新 manifest_id(uuid4)→ 新 event_ids → 同一视频 usage 双记。resume 必须跳过已 render 的视频,而不是重渲。
3. **`run_batch --resume <receipt>`**:读 receipt、跳过完成项、从每条视频卡住的子步骤继续。约 200-400 行。

> 比喻:快递员的签收单,从"跑完全程回站才打勾"改成"每送一件当场打勾"。卡车抛锚后换车,看单子就知道从哪继续。

### 翻转二:资产预制 + 确定性组装(替换 Gemini #2)

目标链路:**script → TTS → beat 切分(确定性)→ embedding 检索 → packer 排片 → 渲染**。

- **Beat 切分**:TTS 之后(正好是现在 Gemini #2 的位置),拿 word timestamps + 标点/分句边界,把旁白切成 ≤4s 的 visual beats。纯代码,无 LLM。Beat 短 → 任何 5s/8s clip 都盖得住 → `ClipAssignmentError` 结构性消失。
- **检索**:每个 beat 文本 embed 一次(批量调用),对该 POI 全部 active clips 的预存 embedding 算 cosine(基建已存在:Sprint 12b 的 `clip_embedder`/`clip_retriever` + Supabase `poi_asset_embeddings`),返回排序候选。
- **Packer(排片员)**:一两百行规则代码,从候选里选第一个过家规的:① 用过的 clip 不再用;② 时长盖得住 beat;③ 相邻 beat 惩罚同类目/同场景(防连续两个泳池镜头);④ **窗口轮换(防 TikTok 去重,优先级最高的新规则)**——同一 clip 跨视频复用时,避开 usage 账本里已记录的源窗口;⑤ trim_start 在未用窗口内用 MiMo 已标注的 `dominant_motion_phase` 选精彩段(只当平局裁判,不得违反 ④)。刚落地的 `clip_assignment_repair.py` 是它的雏形。

#### 窗口轮换的事实基础(2026-06-10 与 AIGC 侧核实完毕)

- **PGC 现状(警报)**:Gemini #2 实践中永远选 `trim_start=0`(prompt 只有约束算术,trim=0 永远最安全),bridge 硬编码 0(`remotion_renderer.py:328,437`)。即同一 clip 用进 3 条视频时,3 次展示的都是开头几秒——**去重风险不是未来时,是现在进行时**。数据无 bug(`_maybe_float` 缺失→NULL,非 0),账本如实记录了退化行为。
- **中台已就绪,零改动**:`poi_asset_usage_events` 已持久化 `trim_start_sec / display_start_sec / display_end_sec / source_duration_sec`(RPC 见 AIGC repo migration 036),3,343 行完整无 NULL。源窗口 = `[trim_start_sec, trim_start_sec + (display_end_sec − display_start_sec))`。不加衍生列,需要时建 view。
- **music remix 不是可抄的先例**:其 trim 是无状态散列(`basis = ((variant−1)×7 + occurrence×3) mod 11`),不查历史不做批内协调——批内"不重叠"只是概率撒点,**跨批次是确定性复读**(同输入→同窗口)。交叉验证:mod 11 理论 trim=0 占比 ~9%,实测 8%,账本可信再次坐实。
- **设计责任**:PGC packer 将是这四个字段的**第一个读方**,中台没有现成查询约定;做好后即全家族标准,music remix 反向换装同一套(届时通知 AIGC 侧)。
- **切换条件**:先拿 2-3 个 POI 做同 script A/B(Gemini #2 版 vs retrieval 版各渲一遍,肉眼比),赢了再全量切。检索质量上限 = MiMo scene_description 质量,A/B 就是在验这个。
- **数据闸门**:在那之前,看 batch log 里 `F3 split-repair` WARNING 的频率——频繁 = 换掉 Gemini #2 的硬证据;罕见 = 大改降优先级。

#### 设计契约(2026-06-10 与 Leo 四板拍定,可以开工)

1. **A/B 用同文案**:给 `compile_promo` 加"回放已录 script"输入(~80 行,additive),同文案两臂各渲一版,差异 100% 归因选片;
2. **窗口账本读取 fail-closed**:生产模式下查 `poi_asset_usage_events` 窗口失败 → 重试耗尽即该视频失败(resume 捞),**绝不**静默降级裸播。依据:读写同一 Supabase、同级重试,真失败率 ≈ usage 写回失败率(年级别罕见),产量代价趋近零;dev 无凭证 → 空历史 + WARNING + provenance 标记;
3. **小店问题已被选店门槛规避**(50+10×extra ≫ ~22 beats/条):planner 仍留两行自适应保险(beat 数 > 素材数时自动拉长 beat),仅护住手搓 batch 绕过门槛的边角;
4. **切换闸门(三条与)**:① 盲测 3 店 Leo 肉眼判 packer ≥2 不输;② F3 有效触发率持续偏高(06-09 后样本:4 条 1-2 触发,06-10 topup 批进行中又 +2 触发,趋势 ~30%);③ A/B 批 packer 臂失败率 ≤ Gemini #2 臂、节拍不慢(packer 无 LLM 调用,天然占优)。

复审修正(2026-06-10,Leo 方向 + agent 复核,均已实现):
5. **B1 切分语意优先**(替代"时间格子+标点微调"):先按标点/分句切,<2s 碎句软合并(合并不得突破 4s 硬顶——地板软、天花板硬),单句 >4s 才强制补刀。代价已具名:beat 数上升 → 每视频 clip 消耗上升 → 素材 3 次上限吃得更快;packer provenance 记 `beat_count`/`unique_clip_count`,生产数据盯消耗速度;
6. **窗口耗尽 = 软偏好,使用次数 = 唯一硬门**(防僵尸素材条款):资格唯一硬门是中台 `poi_asset_valid_clips` 的 <3 次规则(上游过滤);排片员对"窗口用尽"只降级(队尾 → 重叠最少旧窗口 + provenance 标记),绝不当成资格拒绝。两套规则各管各:次数管"能不能上场",窗口管"上场露哪截"。
- 实施顺序:B1 beat planner(纯函数)→ B2 检索 per-beat 排序 → B3 usage_windows 读方(家族标准,文档化查询规范,完工通知 AIGC 侧)→ B4 packer → B5 `PROMO_CLIP_ASSIGNER` 开关集成 → B6 A/B。
- 远期(同翻转方向):MiMo 分析挪到资产入库时一次性做,视频生成时纯查表。

### 翻转三:反馈闭环(defer)

分发表现(播放/完播)回流 → join manifest → 变成选片权重 / script few-shot / 音乐规则。现在只需"不关门":manifest 已够 join,无动作。

---

## 2. 第一梯队:止损修复 — ✅ 全部完成(2026-06-09 落地,06-10 生产实证)

> 下表保留当时的问题诊断作历史记录;**每项怎么修的、修完怎么运作,看 `LEARNING.md` 对应章节**(#1→§3、#2→§4、#3→§6、#4→§5、#5→§1、#6→§7、#7→§8)。

| # | 修复 | 证据 | 收益 |
|---|------|------|------|
| 1 | **WaveSpeed 链路**:master 现在传免费匿名公共文件床(uguu→tmpfiles→litterbox)拿 URL 给 WaveSpeed(`promo/cli/wavespeed_upscale_once.py:83-95`);超时重试会重新提交全新付费 prediction(`run_batch.py:478-489`) | 全链最贵一步挂在最不可靠依赖上;未发布 master 公网可下载 1-24h | 换 Drive/Supabase 签名 URL + 持久化 prediction_id 续 poll。省重复付费、消掉整视频报废的失败模式、关掉泄露面 |
| 2 | **Fail-fast**:`llm/retry.py` 对一切 Exception 重试;真实事故:`400 User location` 烧了每视频 15 次 Gemini 调用 × 整批。正确的 4xx 分类器已存在(`run_batch._is_retryable_autopilot_error`)只是没接到 LLM 重试上 | `batch_run.log` 实锤 | 致命配置错误秒级浮出而不是磨几十分钟 |
| 3 | **Autopilot preflight**:Drive OAuth/Supabase/WaveSpeed env 现在是第一条视频渲染完才惰性初始化(`run_batch.py:538-541`),坏凭证 = 整批渲染白做 | | 开跑前 30 行预检 |
| 4 | **ElevenLabs TTS 加重试**:关键路径上唯一零 HTTP 重试的外部调用(单次 `requests.post`),一个瞬时 429 = 整条视频报废 | `tts_elevenlabs.py` | 与其他 API 对齐(2-3 次退避重试) |
| 5 | **Receipt 时间戳 + 步进落盘**:每 state transition 加 `started_at/finished_at` | `run_receipt.py:243-251` 只写 state | 一切提速决策的前提;也是翻转一的第一块砖 |
| 6 | **BGM 轮换 + MiMo 字段透传**(质量两连击,零成本):① 音乐查询是 `order=duration_sec.asc&limit=3`,全产品线 72+ 视频循环同 3 首曲子(empirically 确认跨批次同 music_id);genre/bpm/tags 查了没人消费(`music_library.py:146-155`)→ 加 seeded 轮换;② MiMo 分析出的 `camera_motion`/`dominant_motion_phase` 被 `format_clip_inventory` 丢弃(`script_prompt_builder.py:126-144`),Gemini #2 盲选 trim_start → 透传进 prompt | | 整个 catalog 的听感多样性 + 每个画面窗口质量 |
| 7 | **一行修复**:`batch_selection.py:56-71` 的 `.range()` 分页无 `.order()`,并发写入可静默跳行 → cooldown 漏判 | | 加 `.order()` |

## 3. 第二梯队(M)

1. ✅ **Receipt resume/top-up**(= 翻转一落地)——已完成并实战验证(skip/re-render 路径;tail-only 待真实尾部失败时毕业考)。原理:`LEARNING.md` §2。
2. ✅ **Revert/smoke cleanup 产品化**:`promo.cli.revert_usage` 已落地(dry-run 默认 / distribution 一票否决)。原理:`LEARNING.md` §10。
3. ✅ **批次流水线化**(2026-06-10 落地,待 VPS 实证):渲染 N+1 与尾巴 N 重叠,`--tail-workers`(默认 1,2 = 双送餐员、节拍≈渲染时长)/`--serial-tail` 回退开关;`plan_batch_items` 已改 POI round-robin。原理:`LEARNING.md` §12。WaveSpeed 已确认支持 100 并发 prediction。
4. **720p 渲染 + 真超分 A/B**(低优先,过渡期限定):现状是 720 素材拉伸到 1080 再让 WaveSpeed"精修拉伸像素";实验 = 720 渲染(像素减半)+ WaveSpeed 真 720→1080。**注意经济学**:upscale 这 700s 是"过渡期税",素材库有原生 1080 后强制 upscale 自动消失、整体变快——这个实验届时作废,所以只在过渡期还要持续很久时才值得做。详见 `LEARNING.md` §11。
5. **S5 测试解耦**(见 BACKLOG.md):测试 monkeypatch 内部符号 → "改内部就崩测试",这是"以后自己改起来费劲"的真正元凶。
6. **ffmpeg vs Remotion 调研**(过渡期后的问题):upscale 消失后渲染(450s)重新成为最大头,届时换引擎才真正值钱;但 Remotion 承担字幕/排版,迁移是大重写 → 先调研收益与成本,不预设结论。

---

## 4. "想改 X → 去动 Y" 导航

### 4a. TTS 停顿(想去掉 segment 间静音)

现状:`pause_weight >= 2` 的 segment 边界切成多个 ElevenLabs batch(`narrate/tts_batch_planner.py:30-68`),batch 之间用 ffmpeg 生成精确时长静音 MP3 拼接(`narrate/tts_engine.py:238-257`);静音时长由 `script/pause_budget.py:217-327` 计算。

要去掉静音 / 整段一次 TTS:
- 改 `plan_tts_batches()`(全并一个 batch)+ 删 `tts_engine.py:241-256` 静音插入段;
- 时间戳回填(`tts_assembly.py:181-294`)自动退化,不用改;
- **唯一要小心的耦合**:word_timestamps 里包含静音,下游 display-span 数学(assign/)直接读它。好消息:翻转二的 beat planner 同样从 word_timestamps 算 span,所以**先做翻转二再改静音,耦合自动安全**。

### 4b. Arsenal 导航

实际只有 5 类,全部 load-bearing,无死货(`promo/arsenal/README.md` 讲了怎么扩展,缺的是 feature→file 索引):

| 想控制什么 | 改哪个文件 |
|---|---|
| Gemini #1 写 script 的总 prompt | `arsenal/system_prompts/gemini1_script_v1.md` |
| 旁白人设/语气/禁用词/few-shot 例子 | `arsenal/personas/third_person_promo.yaml` |
| 段数/字数/每段任务(HOOK/ARRIVAL/...) | `arsenal/script_skeletons/{long_65s,short_30s}.yaml` |
| 开头 hook 的 6 种套路 | `arsenal/script_hooks.yaml` |
| 声音目录 | `arsenal/voices/catalog.yaml` |
| TTS 语速等 | 代码里:`narrate/tts_elevenlabs.py:44` `VOICE_SETTINGS` |

### 4c. Persona / Format / Script 雷同

**Script 听起来都一样的 7 个原因(按影响排序)**:

| 原因 | 位置 | 解法 |
|---|---|---|
| 默认锁单 persona | `selection/persona_selectors.py:42,60` | **零代码**:加第二个 persona YAML + `PROMO_PERSONA_SELECTOR=random` |
| 每次同 4 个 few-shot 例子 | `personas/third_person_promo.yaml:76-113` | 例子轮换(小代码改动) |
| hook 6 选 1 强制轮换,6 条后精确重复 | `script_prompt_builder.py:82` + prompt 里 "do not use a different one" | 加 hook 条目 / 放宽措辞 |
| 同一 format 结构(永远 HOOK→ARRIVAL→LIVE IT→HIGHLIGHTS→CLOSE) | `format_profiles.py:45-55` | 加 skeleton YAML + `PROMO_FORMAT_SELECTOR=random` |
| best-of-N 实为 first-valid-wins,无打分 | `script_generator.py:280-291`(docstring 撒谎说 highest-scoring) | 生成 3 个有效候选 + 轻量 judge 挑 hook 最强的 |
| 固定 temperature 0.85 | `script_gemini_caller.py:35` | 按 variant 扫 temperature |
| 每段任务指令逐字相同 | `script_prompt_builder.py:257-264` | 跟 format 多样化一起做 |

**加 persona / format 的成本**(已验证):丢一个 YAML 进对应目录即可,selector 自动发现,**零 Python 改动**(format 若引入新时长档需在 `format_profiles.get_promo_format_profile` 加一行路由)。

**"更 agentic"的路径**:先把上表的零代码项用起来(persona×format×hook 组合已能产生大量多样性);真要 agentic,插槽在 `script_generator` 的候选循环——把"生成→验证→接受"改成"生成 N→judge 打分→挑选",这正是 first-valid-wins 修复的自然延伸。

---

## 5. 完整发现清单去哪看

31 条发现(含每条 file:line 证据、影响估算、独立复核的 corrections)的扫描属于 2026-06-09 session;高 ROI 项已全部收编进上文。被复核压级的代表:Remotion per-render bundle(实测有 92MB 持久缓存,真实节省仅 ~5-15s/视频)、Drive↔DB reconciler(单操作员 + fail-closed 插入,不急)。

**纪律备忘**:
1. 过手即终态:碰哪个模块,留下新模块 + 测试 + 该目录 `architecture.md` 更新;
2. 外壳接口冻结:SKILL.md 用法、CLI 参数、Supabase schema、receipt 字段、Drive 目录——改 core 不改契约;
3. 一切提速/换模型决策先看数据(receipt 时间戳、`F3 split-repair` 频率),不拍脑袋。

---

## 执行日志

### 2026-06-09

- `4453d78` F3 split-repair 止血落地(时长违规 phrase 词边界拆分,替代 script+TTS 重生成)。
- 37-agent 全库深扫 → 31 条发现全部独立复核,本路线图据此成稿。
- 第一梯队全部落地:`3c97dd1` WaveSpeed 私有桶+prediction 续传;`e80316d` 4xx fail-fast + TTS 重试;`af45192` 分页稳定排序;`8e1323d` preflight + 阶段时间戳 + 每步落盘 + 音乐全池轮换。

### 2026-06-10

- `2951adc` `run_batch --resume`(skip/tail-only/re-render 三分;tail 沿用原 manifest 防 usage 双记);`f6e72b3` review 加固(验证器查时长+音轨、resume 策略推导、真 preflight、upscale 回执留痕);`39caa85` `promo.cli.revert_usage`(dry-run 默认,distribution 一票否决,候选只标 rejected 永不删)。
- 与 AIGC 侧核实闭环:usage events **已**持久化窗口四字段(3,343 行完整);PGC trim 恒 0(去重风险现在进行时);music remix 是无状态散列、跨批次确定性复读,非可抄先例 → PGC packer 将是窗口查询的家族标准。中台零改动。 **[已解决 2026-06-16:within-PGC 由 packer 窗口轮换解(翻转二上线);cross-paradigm 由 recipe_input/056 解,见 §执行日志 2026-06-15→16 + §当前排期 ✅。]**
- **部署 + 实战验证**:推送至 origin;VPS worktree `main_20260610T044026Z_hardened`(39caa85);storage 上传/签名/删除 live 往返通过;hardened smoke(2 POI × 1,production autopilot)双双 `complete`。
  - **split-repair 生产首秀**:7.63s vs 7.04s 违规被拆分修复,视频走完全链(首样本 2 条中 1 条触发——beat planner 重构的第一个硬数据点);
  - **resume 实战**:skip 与 re-render 路径验证通过(1 skip / 1 re-render → 全 complete);**tail-only 路径仅有单测覆盖,待下次真实尾部失败完成实战**;
  - **timings 第一课**:upscale ~700s **>** 渲染 ~430-490s,尾巴流水线化收益高于预估,优先级上调;
  - 部署教训:新 worktree 必须 `npm install`(promo/remotion/),应进部署清单。
- 当前状态:main = `39caa85` 已推送并部署;696 tests passed。

### 2026-06-11

- **翻转二 B1-B6 全部建成并通过三轮对抗审查**(全程藏在 `PROMO_CLIP_ASSIGNER` 开关后,默认 gemini2):B1 语意优先切分(标点成刀、<2s 软合并、4s 硬顶、安全带最大余数法);B3 账本读方(过期窗口钳制);B4 排片员;B5 接线;B6 文案留底+回放(`PROMO_REPLAY_SCRIPT`)。审查共抓 2+3+2 个 blocking 全修复:安全带数学、账本假窗口、超长 beat 静默、回放掉 word_count 标签(B 臂多灌 5.6s 静音、考试作废级)、回放开关泄漏(批次入口双拒绝闸 + `replay_source` 留痕)。763 tests。
- **wpm 根因**(`8973d91`):persona 声明 140 wpm vs 实测 177-182 → 文案被掐在 131 词、塞 14-17s 静音 = F3 肥停顿与无聊长镜头的共同病根。字数 155-170、停顿 cap 3s、wpm 175。Leo 耳测已认证。**F3 旧账分代解释:批次 1 新配方 F3 = 0/5。**
- **架构裁决落槌**:Gemini #2 vs 排片员不再由 F3 统计决定——五条架构论证(账本盲区/同源信息/免疫系统成本/工程四项/叙事规则已显式化)定案排片员胜;**A/B 降级为质检员**(验 MiMo 描述质量够不够),已解锁待渲。
- **中台契约**:AIGC-Main 合并 `asset_platform/CONTRACT.md` v1——PGC 全部平台依赖(valid_clips 列、embedding v1 四元组、存储路径模板、两个 usage RPC、release_candidates 键)成为书面承诺,破坏性变更先 PR 后通知;新增平台依赖须先走 contract PR;`usage_windows.py` 被定为**全家族窗口查询标准**(music remix 将换装);PGC 合规,唯一过渡项 = LocalBackend 本地缓存(已在退役计划内)。
- 下一拍:A/B 考试(规程写入 SKILL.md)→ Leo 判卷 → 切换决策。

- 下一步候选(按数据就绪度):尾巴流水线化(timings 已到手)> 720p 真超分 A/B > beat planner + packer(F3 触发率持续积累中,窗口数据已就绪)。

### 2026-06-10(续)

- **尾巴流水线化落地**(方案 A,Leo 拍板"开并行接口,为日产 200 条做准备"):
  - `plan_batch_items` 改 POI round-robin(相邻 item 异 POI,渲染永不等同 POI 尾巴);
  - 主循环改两级流水:渲染(主线程)‖ 尾巴(`--tail-workers` 线程池,默认 1);同 POI 守门(渲染前 join 该 POI 在飞尾巴后再查 quarantine);槽位节流(在飞尾巴 ≤ workers);`finally` 排空在飞尾巴;
  - 并发安全:`run_receipt` 写盘 + timings 插键持模块锁;`final_upscale` 子字典改整体重赋值;每个 worker 线程自建 Drive/Supabase client(googleapiclient 非线程安全);
  - `--serial-tail`(= `--tail-workers 0`)一键回老路;`--tail-workers 2` 时节拍 ≈ 渲染时长(700 < 2×450,2 个够,WaveSpeed 100 并发上限远未触及);
  - 5 个新单测(round-robin / 重叠实证 / 同 POI 串行 / 双 worker 并发 / 尾巴线程崩溃隔离),线程测试 20 连跑无 flake;701 tests passed。
  - **VPS 实证完毕**(smoke `tail_pipeline_smoke_20260610T1`,2 POI × 1,双 complete,全批 33 分钟):重叠铁证 = Lido 渲染 20:41:04 开始,恰为 Westgate 尾巴起点,且在其 upscale 结束(20:52:51)前渲完。实测:渲染 597/466s、upscale 707/668s、Drive 9/6s、usage 1s。节拍结论:串行 ~21 分/条 → 1 worker ~12 分/条 → 2 workers ~9 分/条(≈160 条/天单机上限;日产 200 需渲染提速,过渡期后再议)。待办:`--tail-workers 2` 在下一次生产批验证。
  - **复审修复**(外部 review 抓到两条,均确认属实):① 音乐/种子从执行序号改回**规范序号**(POI-major)——执行序号下,POI 数是曲库数(现 7 首)整数倍时,同 POI 所有视频钉死同一首歌(2026-06-09 轮换修复的孪生回归);规范序号同时保住种子跨版本可比性。② `--serial-tail` 帮助文案改实:行为等价(每视频参数逐字节同老版),但执行顺序无条件 round-robin。新增"整除现形"回归测试;702 tests passed。
- 翻转二(beat planner + packer)方案已与 Leo 对齐,关乎核心、先讨论后动工;A/B 闸门不变。
- **补齐双批收官(22:33)**:topup_gap3(Saint Vincent、Broadmoor × 3)+ topup_gap2(Mount Princeton、Universal Cabana Bay、Westgate × 2)**双批并行 12/12 complete 零失败**,5 家店全部到 3 条 approved(分发软门槛)。**8 核双批极限吞吐首份实测:12 条 / 72 分钟 = 6 分钟/条(理论 ~240 条/天)**;渲染 466-774s(双批分核,均值 ~570s,符合预期);upscale 454-741s,**4 路并发无排队膨胀**;音乐实证:5 店每店曲目互不重复(canonical 序号修复有效)。F3 again +2(样本 ~16 条,趋势 ~20-25%)。
- **翻转二 B1-B5 全部落地**(`535625c`→`e598802`,744 tests):B1 语意优先 beat planner(复审后翻新:标点成刀、<2s 软合并、>4s 补刀)、B2 per-beat 排序检索、B3 usage_windows 账本读方(家族标准,fail-closed)、B4 packer(五家规+防僵尸条款)、B5 `PROMO_CLIP_ASSIGNER` 开关接入热路径(默认 gemini2,生产无感)。**剩 B6**:compile_promo 回放 script 输入 + 同文案 A/B(2-3 店)。

### 2026-06-11(裁决日)

- B6 落地(script 随 sidecar 留底 + `PROMO_REPLAY_SCRIPT` 回放,复审两 bug——掉 word_count 标签、粘性开关——均修复:`ab417a2`);packer embeddings 三级阶梯补齐生产形态(`78267e9`);A/B 流程写进 SKILL.md(`84ca7fb`)。
- 批次 1(2 店 × 3,新节奏+双送餐员):6/6 complete(1 条文案五抽低于 155 词下限 → resume 重渲一次过;字数闸摩擦率 1/12 观察中);**新配方 F3 = 0/6**(干净基线坐实旧触发率主因是过期 wpm)。
- **A/B 首考(Little Palm Island,同 158 词文案)**:B 臂全链首次实战一次过——22 beats(1 张 4.12s 超顶如实告警)、22 段不重复素材、真实账本加载、窗口零耗尽、零 LLM 调用、选片 31 秒。素材消耗 22 vs 17(+30%,语意切分的已知代价)。
- **Leo 裁决:B 胜,packer 上岗**;随后 Leo 决定**跳过陪跑期,当天直接退役遗产**(理由:双线并存的导航税 > git revert 的 15 分钟回滚成本;agent 操作的 codebase 死代码即误导源)。
- **遗产大扫除(同日)**:删除 Gemini #2 链全家(`clip_assignment_gemini/clip_assigner/clip_assignment_repair` + 两个 prompt md + F3 regen + tighten_hint 管道 + `union_of_top_k`/`top_k` + `PROMO_CLIP_ASSIGNER` 开关);`_step_assign_clips` 坍缩为单引擎直通。**测试按"输入/输出契约"哲学重整**:95 个老链/monkeypatch-内部齿轮测试退役,新增 9 个验证器直调契约测试(`test_clip_assignment_validator.py`),两个端到端测试的假零件挪到新接缝;668 tests passed。回滚程序:`git revert` 删除 commit + 重部署 worktree(写进 SKILL.md)。镜头节奏旋钮 = `beat_planner.DEFAULT_MAX/MIN_BEAT_SEC`(4.0/2.0),Leo 想调时可挪进 skeleton YAML。根目录顺手清掉缓存目录与过期 zip;文档归并(根级 5 份 .md)另行讨论。
- **处女航(maiden_packer_3x1)**:纯新引擎首个生产批,3/3 complete。引擎零失分(packer 三上三中、账本加载、零耗尽);Zion 首跑死于字数闸(5 抽全落 150-153)→ 杠挪 155→150(`d9b6c78`)→ resume 一发过(2 skip/1 re-render)。**窗口轮换双向实证**:新店 trim=0(无账可避,正确);老店(Little Palm B 臂)10 个非零 trim 与账本合并窗口逐笔吻合(3.47/4.42/2.39/2.35/3.69)。注意:即便 150 杠,Zion 仍抽了 146/149 两次才过——素材薄的店笔锋聚在 146-153,若再现预算耗尽考虑 145。**正常批量已解锁**;生产 worktree = main_20260611T_soleengine @ d9b6c78。打包缺口:31 家店全部 ≥3 approved,零欠账。
- **字数摩擦根因(Leo 的 conflicting-inputs 直觉命中)**:prompt 说"瞄 160 词",但唯一的 format=long 范文只有 143 词(低于下限!),另三篇 55-69 词的短范文进一步下拽——模型模仿范文胜过服从数字。修复(`81799ce`,纯 arsenal 零代码):Soneva 扩到 158 词 + 两篇真实成片(Secrets 158 / Nemacolin 160)入库为 long 范文。**迭代方法论**:文案行为不对先查 arsenal 信号一致性(指令 vs 范文 vs 配速带),再动代码。
- **P2 方向已与 Leo 对齐(待新 session 实施)**:type 个性全部进 skeleton YAML(节奏 beat min/max、停顿上限、资产门槛),时长/身份路由数据化——加 type = 丢一张 YAML + 补该 format 范文,零 Python;operator 自然语言说"做 type B",skill 按卡片找配方。P3 根目录瘦身排后。

### 2026-06-11(P2 落地,七步七 commit)

- **第 1-3 步(`b8b71ec`→`6dda9a9`→`de15a7a`,orchestrator 复验放行)**:卡片扩 schema(`description`+`pacing`+`assets`,现值回填,loader 必填拒载)→ 路由"时长→卡"精确查表(一时长一张卡,撞车加载时报错;**有意的行为变化**:未知时长从静默落 long 改为大声报错列牌堆)→ 消费端(beat_planner/packer/pause_budget/选店门槛)改读卡,五个代码常量删净 + 签名守卫防默认值复活。旋钮依据(4s 顶的素材物理、3s 停顿帽的 7000→3000 历史)全部迁到卡上注释。
- **第 5 步(`30b1dd6`,先于第 4 步提交,因两步同文件、保 commit 原子)**:hook 发牌接 per-video seed。根因 = 批次架构(每视频独立 compile + `--n-variants 1` → variant 序号恒 1,永远发第一张牌;first-valid-wins 是 variant 内候选层,另一层楼)。新 `--hook-seed` 通道 = `(base_seed or 0) + 规范序号`(音乐惯例,生产 receipt seed:None 也照转;`--seed` 的选择器熵语义不动)。provenance 拆 `assigned_hook`(发的牌)/`self_reported_hook`(Gemini 自报)——回归测试只认 assigned 随 seed 轮换。
- **第 4 步(`3235c76`)**:范文法规。`format_examples` 的静默跨 format fallback(143 词事故的温床)改为大声报错;persona 服务的每个 format ≥2 篇打标范文;生产 persona 双卡钉绿;范文"迁入卡片"之路注释预留。
- **第 6 步(`5721108`)**:arsenal README 旋钮索引 + LEARNING §14(三抽屉/三层差异/简报链路 + 两条 Leo 批注口径 + 事实通道防失传:hotel_description/notable_details 空通道、价格来自模型记忆、政策待拍板)。
- **第 7 步**:三块仪表写进 §当前排期(字数摩擦毕业考 / F3 基线 / 素材消耗+走牌验证),下一生产批顺手读。
- 全程 693 tests passed(668 起步,+25);每步独立 commit,行为不变步骤有回归钉。

### 2026-06-13(V1/V2 范文/采样验证收官)

- 两批无升级验证批跑测后**全量 revert**(revert 机器实弹 ×2 均验证干净:usage→0、RC→rejected、distribution 未碰)。V1 修包(`610804b`→`11da0ac`):真 bug = hook 卡被 `n_variants>1` 挡住、生产 `--n-variants 1` 永不注入 → 修(注入即断言 prompt 含卡);CLOSE 反说教规则 + 两条长范文结尾重写。V2 简报采样器(`f1a2601`)收货留用(零运行成本、微助益)。裁决:服牌 1/6→6/6、banned 短语 5/6→0/6、首次零低抽字数;采样器**非**干净赢,差异随库存余量放大(假设待 cheap 实验)。详见 memory `project_v_series_findings`。

### 2026-06-15→16(库存批仗 + 跨范式去重上线 + 运营硬化 + roadmap 补记)

- **库存批 401 仗**:首批 stock_4x3 死在 WaveSpeed 升级 401 → 退货干净(fail-closed 守住、零真成本)。根因 = PGC 与 AIGC 共用 key、AIGC 轮换后 PGC 用着死 key(**非抢账号,401≠429**);装独立新 key `de9214e4`(⚠️ 对话明文暴露过、**待轮换**)、活体探活过。重跑 **12/12 干净**(SKILL 补 `--tail-workers 2`,纯一句话实测通过)。详见 memory `project_wavespeed_401_and_preflight_gap`。
- **跨范式内容去重 recipe_input/H1 上线**(见 §当前排期 ✅):`feat/recipe-input-dedup`(`0793918`+`711e4ad`)合并 + 部署 + AIGC 拨 056 + 真候选 rfp2 指纹已算出 = **end-to-end 通**。设计经 PGC↔AIGC 来回对抗审收敛(PGC 审出 AIGC「立刻收窄索引名」过早会埋「认不出真错」雷;AIGC 反认自己 #300 对称偏严)。AIGC 侧待:P1e 唯一索引(真"拦重复")、P1g 两仓活体抓真 23505 形状。
- **运营硬化四分支合入** main=`73eb804`(186 tests green):① preflight 活体探活 ② resume 接 tail_workers ④ cooldown 软化 + SKILL tail-workers 2→4。三分支 fresh-context agent 独立复核 MERGEABLE 后才合;all merges conflict-free。
- **跨范式 cooldown 发现(待议设计点)**:PGC 选片 cooldown 读共享 usage 账本【跨范式无 paradigm 过滤】→ music_remix 一忙就把 PGC 选片池冷干(实测 162 POI 冷、145 由 music_remix)。soft-cooldown 软化解了"饿死",但"PGC 选片 cooldown 该不该跨范式 vs 各算自己"是 Leo 待拍的设计点。
- **欠债**:🔐 key `de9214e4` 轮换(库存批跑完后);P1g 两仓活体验真 23505 形状(已降为可观测性,不急)。
- **状态**:main=`73eb804`、产品 P 主线坐标不变(P5 仍下一关,今天的活全在主线外)。VPS 生产 worktree 已部署 `73eb804`。
