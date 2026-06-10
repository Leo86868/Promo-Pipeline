# PGC Pipeline 下一阶段路线图(2026-06)

> **新 session 自举(60 秒)**:① 读本文件(重点:§执行日志 最后一天 + §翻转二);② `git log --oneline -15` 看落了什么;③ auto-memory 会自动带上下文。**已完成勿重做**:第一二梯队全部落地;翻转一(轻量版)已实现并实战验证(per-step flush + timings + `--resume`),不需要再"实现翻转一"。**当前最大未做项**:尾巴流水线化(timings 数据已到手:upscale ~700s > render ~450s)和 翻转二(beat planner + packer,F3 触发率首样本 50%)。VPS 部署流程:push origin → 新 worktree → **必须 `cd promo/remotion && npm install`** → preflight → detached 启动。

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
- 远期(同翻转方向):MiMo 分析挪到资产入库时一次性做,视频生成时纯查表。

### 翻转三:反馈闭环(defer)

分发表现(播放/完播)回流 → join manifest → 变成选片权重 / script few-shot / 音乐规则。现在只需"不关门":manifest 已够 join,无动作。

---

## 2. 第一梯队:止损修复(全部 S,合计约 2-3 天)

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

1. **Receipt resume/top-up**(= 翻转一落地)——最大 scaling 阻塞,4 个扫描视角独立命中,receipt 自己在 `implementation_gaps` 里承认。
2. **Revert/smoke cleanup 产品化**:现在是 agent 对 prod 手写裸 SQL(SKILL.md:456-460),做成 dry-run 默认的 `promo.cli.revert_usage`。
3. **批次流水线化**(render CPU-bound ‖ autopilot 尾巴网络-bound,估省 25-40% wall-clock)——先做 #5 装表实测,再注意 `plan_batch_items` 需改 POI round-robin(同 POI 因 usage 顺序不能重叠)。
4. **720p 渲染 + 真超分 A/B**(一个下午):现在是 720 素材拉伸到 1080 再让 WaveSpeed"增强"已拉伸像素;改 720 渲染(像素减半更快)+ WaveSpeed 真 720→1080,看字幕是否劣化。
5. **S5 测试解耦**(见 BACKLOG.md):测试 monkeypatch 内部符号 → "改内部就崩测试",这是"以后自己改起来费劲"的真正元凶。

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
- 与 AIGC 侧核实闭环:usage events **已**持久化窗口四字段(3,343 行完整);PGC trim 恒 0(去重风险现在进行时);music remix 是无状态散列、跨批次确定性复读,非可抄先例 → PGC packer 将是窗口查询的家族标准。中台零改动。
- **部署 + 实战验证**:推送至 origin;VPS worktree `main_20260610T044026Z_hardened`(39caa85);storage 上传/签名/删除 live 往返通过;hardened smoke(2 POI × 1,production autopilot)双双 `complete`。
  - **split-repair 生产首秀**:7.63s vs 7.04s 违规被拆分修复,视频走完全链(首样本 2 条中 1 条触发——beat planner 重构的第一个硬数据点);
  - **resume 实战**:skip 与 re-render 路径验证通过(1 skip / 1 re-render → 全 complete);**tail-only 路径仅有单测覆盖,待下次真实尾部失败完成实战**;
  - **timings 第一课**:upscale ~700s **>** 渲染 ~430-490s,尾巴流水线化收益高于预估,优先级上调;
  - 部署教训:新 worktree 必须 `npm install`(promo/remotion/),应进部署清单。
- 当前状态:main = `39caa85` 已推送并部署;696 tests passed。
- 下一步候选(按数据就绪度):尾巴流水线化(timings 已到手)> 720p 真超分 A/B > beat planner + packer(F3 触发率持续积累中,窗口数据已就绪)。
