# LEARNING.md — 已完成改造的工作原理(Leo 的学习手册)

> 这本手册回答一个问题:**"我知道修好了,但它到底是怎么工作的?"**
> 每节 = 一句奶奶版 + 机制 + 代码在哪 + 怎么亲眼看到它工作。
> 配套:`ROADMAP-2026-06.md`(要做什么) / 本文件(已做的怎么运作)。

---

## 0. 一条视频的一生(先看懂全局)

```
run_batch(总管,一个进程)
 ├─ 选 POI(查 Supabase)→ 写 batch.json
 ├─ preflight 体检(autopilot 模式)
 └─ 对每条视频,依次:
     ├─ ① 渲染:起一个子进程跑 compile_promo
     │     (子进程内部:Gemini#1 写文案 → ElevenLabs 配音 → MiMo 分析素材
     │      → 确定性选片[切幻灯片→检索→排片员] → Remotion 渲染 → 写 manifest)
     └─ ② autopilot 尾巴(总管自己做):
           WaveSpeed 升级 → Drive 上传 → usage 写回 → release candidate 注册
```

关键认知:**①是子进程、②是总管进程**。receipt(签收单)由总管全程持有和落盘;子进程不碰 receipt,它的产出(视频文件 + manifest)由总管检查后记账。

实测时间账单(2026-06-10 smoke):渲染 ~450s,**upscale ~700s**,Drive 7s,usage 2s,release 0s。→ 尾巴比渲染还长,这就是"流水线化"排上日程的原因。

---

## 1. Receipt:实时签收单(你最想懂的,讲细一点)

**先看一个真实故事(2026-06-10 的 smoke,纸上实际发生的事)**:

| 时刻 | 工厂里发生了什么 | 纸上(RUN_RECEIPT.json)立刻变成什么 |
|---|---|---|
| 04:41:20 | 总管选好 2 家 POI,体检通过 | 文件诞生:两条视频都是 `planned`,`preflight: passed` |
| 04:41:43 | 视频1 开始渲染 | 视频1 → `rendering`,timings 记下开始时刻 |
| 04:46:59 | 视频1 渲染失败(没装依赖) | 视频1 → `render_failed`,退出码 1,timings 记下结束时刻 |
| 04:46:59 | 视频2 开始渲染 | 视频2 → `rendering` |
| 04:55:05 | 视频2 渲染成功 | 视频2 → `rendered_manifest_audited` |
| 05:06:46 | 视频2 升级完成 | `final_upscale` 格子 → verified(**单独写盘一次**) |
| 05:06:53 | Drive 传完 | `drive_upload` 格子 → verified(又写一次) |
| 05:06:55 | 账记完、候选注册完 | 视频2 → `complete` |

**思想实验**:假如 05:00 整栋楼断电——纸上停在"视频2 渲染成功、升级进行中"。重启后看纸就知道:视频1 要重做,视频2 的片子是好的、只欠后半段手续。**这就是"实时"的全部含义:任何瞬间打开这个文件,看到的就是工厂此刻的真相,因为每前进一小步就重写一次整张纸。**改造之前不是这样——以前是一条视频全办完才写一次,断电时纸上是几十分钟前的旧账。

**奶奶版**:快递员的签收单,每送一件当场打勾,卡车抛锚后看单子就知道从哪继续。

**它是什么**:`outputs/RUN_RECEIPT.json` 一个本地 JSON 文件。结构三层:

```
batch 层:  batch_id / created_at / request(当初的完整订单参数)/ preflight 结果
videos[]:  每条视频一条记录 ↓
  state:   一个状态字符串,随流程推进改写(见下)
  小格子:  render(含完整命令+退出码)/ manifest / manifest_audit /
           final_upscale / drive_upload / usage / release_candidate
  timings: 每阶段 started_at / finished_at / duration_sec
summary:   全批统计(写盘时自动重算)
```

**state 的一生**(`promo/core/run_receipt.py`):

```
planned → rendering → rendered_manifest_audited → complete
                ↘ render_failed                ↘ final_upscale_failed
                                               ↘ drive_upload_failed
                                               ↘ usage_writeback_failed
                                               ↘ release_candidate_failed_retryable
```

**什么时候写盘**(这是 2026-06 的核心改造):每次有事发生**立刻**写整份文件——
- 渲染前 `mark_rendering`、渲染后 `mark_render_result`(`run_batch.py` 主循环);
- 尾巴**每个子步骤完成后**各写一次(`_run_video_production_autopilot` 里每段结尾的 `write_run_receipt`)。以前是整条尾巴跑完才写一次 → 进程中途死掉,盘上是旧状态;现在盘上永远等于真实进度。
- 写盘函数 `write_run_receipt`(`run_receipt.py` 末尾):重算 summary → 整文件覆写。毫秒级,相对分钟级的步骤可忽略。

**timings 怎么记**:`mark_stage_started/finished`(`run_receipt.py`)往 `video["timings"][阶段名]` 写时间戳并算差值。渲染阶段藏在 `mark_rendering/mark_render_result` 里自动打点;尾巴四个阶段在 autopilot 链里手动包夹。

**怎么亲眼看**:VPS 上跑批时 `watch -n 5 'python3 -c "import json;r=json.load(open(\"RUN_RECEIPT.json\"));print([v[\"state\"] for v in r[\"videos\"]])"'` ——你会看到状态一格格往前爬。我昨天的监视器就是这么实现的。

---

## 2. Resume:停电重启按钮

**整个功能就是三句话**:拿起签收单,对每条视频只问一个问题——
1. "你 `complete` 了吗?" → 是 → **绕开,一根手指都不碰**;
2. "你的片子做好了、只是后半段手续(升级/上传/记账)没办完?" → 是 → **只补手续,不重做片子**;
3. 其他一切情况(没开始、做一半卡死、做坏了)→ **按纸上抄录的原配方重做**。

**为什么第 2 条敢"从手续的头上整段重办"而不怕重复?**因为后半段每个动作都长着"做过就认得"的脑子:升级成品已在且验收过 → 直接用不重付;Drive 上已有同名同大小文件 → 不重传;每笔账有唯一编号(从 manifest 内容算出的指纹)→ 记过的账自动跳过。所以不需要记"精确死在哪半步",整段重跑,已做的部分自然变成"白拿"。

**昨天的实战对照**:视频2 已 `complete` → 走第 1 条,resume 全程没碰它;视频1 是 `render_failed` → 走第 3 条,重放原命令重做。第 2 条(最值钱的"只补手续")昨天没机会上场——两条视频都没出现"片子好了手续断了"的状态,它目前只有单元测试背书,等下次真实出现再毕业。

**奶奶版**:看签收单,做完的绕开、做一半的从断点接着、坏的重做。

**决策表**(`run_receipt.plan_resume_action`):

| 盘上 state | 动作 | 原因 |
|---|---|---|
| `complete` | skip | done |
| `rendered_manifest_audited` + 四种尾部失败 | **tail-only** | 视频和 manifest 都在盘上,只补尾巴 |
| 其余(含卡死的 `rendering`) | re-render | 重放 receipt 里存的原命令(`video.render.command`) |

**为什么 tail-only 敢直接重跑整条尾巴**(不需要知道死在哪半步)——尾巴每步幂等:
- upscale:输出已存在且验证通过 → 复用不重付(`run_batch._apply_final_upscale_to_inventory` 开头的 existing 检查);
- Drive:按文件名+大小发现已存在 → 复用;
- usage:event_id = manifest 内容的确定性 sha256 → RPC 自动去重;
- release:先查再插只补缺。

**防双记的关键不变量**:tail-only **沿用原 manifest_id**。如果重渲,会 mint 新 manifest_id → 新 event_ids → 同一条视频 usage 记两次。所以"已写过 usage 的状态绝不重渲"。

**实战记录**:2026-06-10 首战 = 1 skip / 1 re-render,全部 complete。⚠️ tail-only 路径还没在生产击发过(只有单测),下次出现尾部失败状态就是它的毕业考。

代码:`run_batch.resume_batch` + CLI `--resume`。

---

## 3. WaveSpeed:私有保险库 + 订单号小本本

**奶奶版**:包裹放自家带锁仓库给店家限时钥匙;订单号记小本本,重试先问"我那单好了吗"。

机制(`promo/cli/wavespeed_upscale_once.py`):
1. master 上传到 Supabase Storage 私有桶 `pgc-upscale-staging`(按文件 sha256 命名)→ 生成限时签名 URL → 交给 WaveSpeed 拉取;
2. 提交后**立刻**把 prediction_id 按输入 sha256 落盘到 `<output>.wavespeed_state.json`;超时重试时:同输入 → 续 poll 老订单不重付;订单死了 → 重新提交;输入变了(重渲过)→ 忽略旧状态;
3. 成功后:删暂存对象、删状态文件,回执 JSON(prediction_id 等)被 `final_upscale.py` 捕获进 receipt。
4. `--source-host supabase` 在生产是强制的(fail-closed):凭证缺失直接失败,**绝不**静默退回公共文件床;anon key 被识别为"没有凭证"(它写不了 storage)。

---

## 4. Fail-fast:听到"没救了"立刻挂电话

**奶奶版**:对方说"你的地区不支持",再拨 14 次也是浪费。

机制(`promo/core/llm/retry.py`):`retry_with_backoff` 每次捕获异常先问分类器 `is_non_retryable_client_error`——HTTP 4xx(除 408/429)= 确定性失败,立即抛出;5xx/超时/断连 = 暂时性,继续退避重试。识别三种错误长相:状态码属性、google 风格 `"400 ..."` 前缀、adapter 风格 `"API error 401"`。四个 LLM 调用点(Gemini#1/#2、MiMo、embedding)自动生效。

## 5. TTS 重试:给配音也配上"占线重拨"

`narrate/tts_elevenlabs.py`:原来是全链唯一单次 `requests.post`(瞬时抖动 = 整条视频报废),现在套 `retry_with_backoff(max_retries=3)`;坏 key 走 §4 的分类器秒失败。

## 6. Preflight:开工前体检(两层)

`run_batch._autopilot_preflight`,渲染任何东西**之前**:
- 第一层:Drive uploader / Supabase client 能不能构造(凭证文件在不在);
- 第二层(深检):调用升级命令的 `--preflight` 模式——它会加载自己的 `--env` 文件,检查 WAVESPEED_API_KEY 和 storage 凭证。**连子命令私有配置文件里的缺失都能拦住**。
失败 → 整批退出,错误写进 `receipt["preflight"]`,零渲染零花费。

## 7. 音乐轮换

`music_library.select_tracks`:取**全部** ≥目标时长的曲子(以前只取最短 3 首),按批次种子洗牌;`run_batch.plan_batch_items` 按**全局序号**轮换(以前按 video_index → 每个 POI 的 video_001 永远同一首)。根因仍是曲库只有 7 首 ≥65s——加曲子后代码自动受益。

## 8. 分页排序(防数漏)

`batch_selection._fetch_all_rows` 翻页前强制 `ORDER BY` 唯一键(clips 按 asset_id、usage 按 event_id)。没有排序时 PostgREST 不保证页间稳定,并发写入会让某行"换页"被跳过 → cooldown 漏判 → 同 POI 三天发两条。

## 9. F3 split-repair(已退役,2026-06-11)

> **退役说明**:本节描述的拆分修补和它服务的 Gemini #2 选片链已于 2026-06-11 整体退役(A/B 裁决后按 Leo 决定跳过陪跑期直接删除,回滚靠 git revert)。替代者是确定性新链(切幻灯片 ≤4s → 检索 → 排片员),"句子太长素材盖不住"在新链里结构性罕见且会大声告警。以下保留作历史记录。

**奶奶版**:一句话要 7.6 秒画面、素材只有 7 秒?把这句话的画面在某个词处切开,前半用原素材、后半补一段没用过的。文案和配音一个字不动(拆的只是"画面在哪个词切换")。

机制(`promo/core/assign/clip_assignment_repair.py`):validator 报时长违规 → 在违规 phrase 里找离时间中点最近的词边界拆开 → 后半从未用素材里挑能盖住的 → 重过 validator,最多修 3 次;修不动才走昂贵的"重写文案"老路。结构性错误(重复/缺失)不修,照常抛。

**生产首秀**:2026-06-10,`7.63s vs 7.04s` 违规被拆分(词 51 处,clip 0056 留 2.58s + clip 0007 盖 5.06s),视频走完全链。grep 批次 log 里的 `F3 split-repair` 可统计触发率(首样本 2 条中 1 条)——这是决定 Gemini#2 去留的数据。

## 10. Revert CLI(退货机器)

`promo.cli.revert_usage`:默认只打印"将退什么"清单(零写入),`--execute` 才动手;usage 撤回走平台 RPC(账本重算非盲减);`--full-cleanup` 额外把候选标 `rejected`(永不删行);**distribution 已领走 → exit 2 拒绝操作,--execute 也无效**;Drive 文件永不碰。SKILL.md 已规定 agent 只许用它,禁止手写 SQL。

---

## 11. 速度问题:720p / ffmpeg / 过渡期经济学

当前每条视频:渲染 450s + **upscale 700s(过渡期税)** + 上传写回 10s。

- upscale 之所以强制,是因为素材库现在只有 720 宽素材(`transition_low_res_only`)。**过渡期结束(素材库有原生 1080)→ 政策回 best_available → 这 700s 和 WaveSpeed 费直接消失,整体变快不是变慢**;
- **720p 渲染 A/B** 只在过渡期有价值(渲染像素减半 + WaveSpeed 做真超分而非"精修拉伸像素"),过渡期结束即作废——所以它是低优先实验;
- **尾巴流水线化**(渲染下一条 ‖ 升级上一条)过渡期内收益最大(尾巴 700s 全部藏进渲染时间),已落地 → §12;
- **ffmpeg 换 Remotion** 是过渡期后的问题:那时渲染重新成为最大头。但 Remotion 承担字幕/排版,换引擎是大重写 → 先调研后决定,不预设结论。

---

## 12. 尾巴流水线化:厨师 + 送餐员

**奶奶版**:厨师(渲染)做完一道菜,以前要亲自骑车送外卖(upscale/Drive/记账),送完才做下一道。现在雇了送餐员:菜一出锅交给他,厨师立刻回灶台。做菜和送餐同时进行。

**关键认知**:送餐那 ~700s 里我们的 VPS 几乎是闲着的——出汗的是 WaveSpeed 的 GPU,我们只是隔几秒问"好了吗"。所以重叠几乎免费。

**节拍账**:串行 = 450+700 = 1150s/条;1 个送餐员 = max(450,700) = 700s;**2 个送餐员 = ≈450s(瓶颈回到做菜)**。3 个没用(700 < 2×450,两个刚好够);WaveSpeed 支持 100 并发,远未触顶。

**三条家规**(`promo/cli/run_batch.py`):
1. **同一家店的两单不能同时在路上**:同 POI 视频的 usage 必须按顺序记账。排单已改 POI round-robin(`plan_batch_items`,相邻 item 异 POI);渲染某条前若同 POI 尾巴在飞,先等它落地**再查 quarantine**——隔离语义逐字保留。
2. **送餐员有人数上限**(`--tail-workers`,默认 1):在飞尾巴满员时,厨师等一单落地再交下一单。任何时刻最多 1 渲染 + N 尾巴,内存有界。
3. **签收单一次一人拿笔**:receipt 写盘 + timings 插键在 `run_receipt` 里持模块锁;每个送餐员线程自带自己的 Drive/Supabase 钥匙(googleapiclient 非线程安全,不共享连接)。

**后悔药**:`--serial-tail`(= `--tail-workers 0`)一键回到老的串行流程,零代码改动。

**怎么亲眼看到**:跑批后看 receipt 的 timings——视频 2 的 `render.started_at` 早于视频 1 的 `final_upscale.finished_at`,就是重叠的铁证。批次总时长应 ≈ 首条渲染 + Σ max(渲染, 尾巴)。

**实战验证(2026-06-10 smoke)**:Lido 渲染开始时刻 = Westgate 尾巴开始时刻(20:41:04),并在其 upscale 结束前 4 分钟渲完;双 complete,全批 33 分钟(串行需 ~41)。实测渲染 466-597s(波动看素材数/文案长)、upscale 668-707s。

**出错怎么办**:送餐员线程崩了只记该条失败,整批继续;主进程崩了,签收单仍是每步实时落盘的,`--resume` 照常接管(尾巴半途死掉正好是 tail-only 路径的毕业考)。

---

## 13. 读账要设防(usage 窗口账本读方)

**奶奶版**:商店年年盘点,不是因为账一定错,是因为没人敢保证账永远对。

**本质**:`poi_asset_usage_events` 的窗口账本和素材实物之间**没有同步保证**。读账的人(packer 的窗口轮换)用之前先对照实物剪一刀——账上的窗口超出素材现长就钳掉/丢弃——这是一行成本的终身保险,防的是元数据错误、未来新管线、手工修数据。

**那一下(2026-06-10)**:查了真账——5,041 行里,可对照的 4,532 行**零条旧账**(其余 509 行的素材已退役/到使用上限,正常)。保险买在没着火的时候;**但也别把"可能着火"讲成"正在着火"**——Claude 这次自己犯过这个错(给钳制修复编了一个"素材会被重新编码变短"的不存在机制),已在代码注释和本手册纠正。

代码:`promo/core/assign/usage_windows.py`(fetch 钳制 + 空隙长度先钳后判,各一处,均有反例回归测试)。
