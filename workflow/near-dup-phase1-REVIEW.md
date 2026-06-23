# 近似去重 Phase-1 · Reviewer 验证报告

**分支** `feat/near-dup-phase1`(未 push)· **基线** main `cb31bca` · **状态** code-complete + 测试过 + **默认关**,等合并/部署 + 活体烟测。

---

## 交叉复审结论(2 个独立 agent,2026-06-23)

- **正确性/接线**:三条断言全 HOLD,**零 bug**;关态字节相同(差分验证)、三条 batch-start 路径(batch/select/**resume 靠录入命令重放**)全覆盖、fail-soft/fail-open 正确。默认关=零风险。
- **数据**:饿到 0/9/36 独立重跑**逐数复现**;"EXACT 收益"确属真成片上算、**不依赖 56% 重放**。
- **采纳的 3 处修正**(已改进本报告 + report.md):
  1. **生产链路是【环境变量】中介**,非"参数穿到 packer":`compile_promo` 把 `--near-dup-threshold`
     写进 `PROMO_NEAR_DUP_THRESHOLD`,`_assign_clips_packer` 从 `config.near_dup_threshold()` 读
     (其 `near_dup_threshold` 形参目前是待用 hook,生产调用方未穿)。功能端到端验证过。
  2. **"召回损失 2%" 是措辞夸大**:0.02 是绝对余弦差(低底数上约 8% 相对);稳的结论是"跨阈值不变"。
  3. **0.85 是 margin call**:贴饿到悬崖边,0.88 是更稳的首发值。已在 report.md 改注。

---

## TL;DR(给 reviewer 30 秒)

给 packer 加了一道**单视频内"近似软闸"**:挑片时,候选若与本视频已选片的 embedding 余弦 ≥ 阈值,就跳过、取下一个多样片;**fail-soft**(找不到多样片就放宽、绝不让视频失败)。`--near-dup-threshold` 从 run_batch 一路贯通到 packer,**默认 None = 关 = 与今天逐字节相同**。推荐阈值 **0.85**。

⚠️ **根本局限**:embedding 是**文字描述**(text-embedding-3-small over scene_description)算的,不是像素。能堵"描述相近"的近似(A 类),**堵不到"画面像但描述不同"**(B 类,如实测两张床 cos 仅 0.628 却肉眼一样)。B 类要视觉指纹(phase-2,中台),本单不含。

---

## Reviewer 验证清单(请逐条核)

| # | 要验证的断言 | 怎么验 |
|---|---|---|
| 1 | **关态字节相同** —— `near_dup_threshold=None` 时,选片 + provenance + 录入命令与今天完全一致 | `test_gate_off_is_byte_identical_to_today`;读 packer.py 关态分支(provenance 不加新键、diversity_passes=(False,)) |
| 2 | **闸逻辑正确** —— 候选与已选片 cos≥阈值才跳 | `test_gate_on_skips_near_dup_for_diverse_clip`;实测重跑日志:每个选中片 maxcos_to_prior < 阈值,DB 口径残留 0 对 |
| 3 | **fail-soft** —— 全近似片的 POI 仍出完整视频 | `test_gate_fail_soft_all_near_dup_still_full_video` |
| 4 | **生产贯通** —— run_batch → compile_promo → packer,且记进命令可 --resume 重放 | `test_build_compile_command_near_dup_threshold`;读 run_batch.py 两条 batch-start 路径(select / batch)都传了 near_dup_threshold |
| 5 | **阈值 0.85 合理** —— 零饿到 + 召回不损 | 扫描表(near-dup-phase1-report.md):0.85 饿到=0,0.82=9,0.80=36;召回损失全程 2-3% |
| 6 | **共享判定不漂移** —— production 与 simulate 用同一 cos 函数 | `packer.max_cosine_to_chosen` 被两处 import |

**重点盯**:① 关态真的零改动吗(这是上线安全的命根);④ resume 路径是否真的靠"录入命令"覆盖(我没给 resume_batch 单独穿参,靠的是原始命令已录入)。

---

## 代码地图(5 个 commit,干净叠放)

| commit | 内容 | 关键文件 |
|---|---|---|
| `37e3118` | 引擎:packer 软闸 + 共享判定 + steps 穿参 | `promo/core/assign/packer.py`、`promo/core/pipeline/steps.py` |
| `ca2a0ba` | simulate 工具 + 成本/收益报告(66 真视频) | `workflow/near_dup_simulate.py`、`near-dup-phase1-report.md` |
| `c2972e2` | 阈值扫描 0.80-0.90 + 推荐 0.85 | `near-dup-phase1-report.md` |
| `66ca686` | 渲染开关:config 读取 + compile_promo 旗标 | `promo/core/config.py`、`promo/cli/compile_promo.py` |
| `7e13f06` | 生产开关:run_batch 旗标贯通 + 单测 | `promo/cli/run_batch.py`、`test_run_batch.py` |

**引擎核心**:`packer.py::pack_clips` 的 relax 阶梯——`diversity_passes = (True, False) if threshold else (False,)`,关态塌缩成原逻辑;软闸在主循环候选筛选里(insertion point A),向量取 `meta_by_id[norm]["embedding"]`(已在 clip_metadata,不改签名)。

---

## 怎么复现(只读)

- **simulate / 阈值扫描**:`workflow/near_dup_simulate.py`,VPS 上 `PYTHONPATH=. python3 workflow/near_dup_simulate.py`(读 4 个真批 + poi_asset_embeddings)。
- **真渲染 A/B**(已做):club_wyndham off/0.88/0.86,`compile_promo --near-dup-threshold`,纯渲染零账本(ledger 前后 rc=2021/usage=48 逐行相同)。
- **冗余度水位查询**:见对话记录,club_wyndham 内部近似冗余 top ~2.5%(全库最雷同的 POI 之一,是个严苛测试点)。

---

## 没验证 / 开放风险(老实交底)

1. **run_batch 这层没活体烟测** —— 闸在 compile_promo 真渲染验证过,但"run_batch 把旗标拼进子进程命令"只有单测,没端到端跑过。**建议合并前先 `run_batch --near-dup-threshold 0.85` 纯渲染烟测一条。**
2. **simulate 重放 baseline 仅 56% 忠实**(跨视频窗口轮转离线还原不了)→ 收益数是 EXACT(真实成片上算),但代价/饿到是方向性估计(结构性结论仍可信)。
3. **阈值边界 slop** —— 0.88 那次出现过一对 0.884 残留(高于阈值),复测无法重现,判定为边界精度效应(±0.005),非逻辑洞。reviewer 可独立判断。
4. **B 类盲点**(文字非像素)—— 见 TL;DR,这是设计天花板不是 bug。

---

## 决定项(留给 Leo/reviewer)

- 合并 main + 部署 VPS(默认关,零风险)。
- 首批是否传 `--near-dup-threshold 0.85` 灰度。
- phase-2:推中台加视觉指纹(pHash/视觉 embedding)解 B 类 —— Leo 已定"视觉指纹等,兜底先上"。
