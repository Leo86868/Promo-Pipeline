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
     │      → Gemini#2 选片 → Remotion 渲染 → 写 manifest)
     └─ ② autopilot 尾巴(总管自己做):
           WaveSpeed 升级 → Drive 上传 → usage 写回 → release candidate 注册
```

关键认知:**①是子进程、②是总管进程**。receipt(签收单)由总管全程持有和落盘;子进程不碰 receipt,它的产出(视频文件 + manifest)由总管检查后记账。

实测时间账单(2026-06-10 smoke):渲染 ~450s,**upscale ~700s**,Drive 7s,usage 2s,release 0s。→ 尾巴比渲染还长,这就是"流水线化"排上日程的原因。

---

## 1. Receipt:实时签收单(你最想懂的,讲细一点)

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

## 9. F3 split-repair(句子太长就拆两半)

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
- **尾巴流水线化**(渲染下一条 ‖ 升级上一条)过渡期内收益最大(尾巴 700s 全部藏进渲染时间),数据已实测,这是下一个吞吐项;
- **ffmpeg 换 Remotion** 是过渡期后的问题:那时渲染重新成为最大头。但 Remotion 承担字幕/排版,换引擎是大重写 → 先调研后决定,不预设结论。
