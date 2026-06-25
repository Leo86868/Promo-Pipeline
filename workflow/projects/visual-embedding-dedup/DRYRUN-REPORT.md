# 工单② STEP-1 — 视觉多样选片 dry-run（只测量，不碰生产）

**日期** 2026-06-25 · **分支** `feat/visual-diversity-select`（未 push）
**目的** 给 Leo 数字定"考虑窗口"，再写 flag-gated 生产路径。
**结论** **窗口 = 45**：15 个真 POI 全部 → 0 视觉近重复 + 30/30 不同簇，保留 ~92–97% 相关性质量。

---

## 一句话
今天下载的 30 条里有 **1–9 对视觉近重复**、只有 **22–29 个真正不同的画面簇**（名额浪费在重复上）。换成"相关性种子 + 视觉 max-min、窗口=45"，**15 个 POI 无一例外** → 0 近重复 + 30 满簇，下载数仍 30（egress 不变），**相关性只丢 ~5%**。

## 方法（真相关性 · 在 VPS 上跑，零代理）
> 上一版本地用"质心代理"近似相关性（OpenRouter key 本地 401 + 本地无 DB）。Leo 要真召回数 → **搬到 VPS**：
- **相关性 = 真生产相关性**：每个 POI 取它**最近一次真生产**的 `clip_assignments_*.json → shared_asset_retrieval.queries`（=那条片真实脚本的旁白段落），用**生产 OpenRouter key 现场嵌入**，对当前可选池算 `max(cosine)`。**不是质心，是真脚本 query。**
- **基线 relevance-30** = 按真相关性取前 30（=今天"纯相关性"选法）。
- **maxmin@W** = 相关性 top-W 当考虑池 → 贪心最远点选 30，**种子=相关性#1**（PGC 适配，不是 prototype 的 medoid）。算法见 `prototype/select_diverse.py`。
- **白化** = 对 top-60 工作池 mean-center → L2（handoff 消费侧白化）；所有选法同一白化空间，公平比。
- **指标**：近重复对(≥0.85/≥0.92 白化余弦) · worst(集合内最大余弦) · 不同簇(单链@0.85) · deepest(摸到第几名相关性) · top15(前15相关留几个) · **rel%(=选中集相关性之和 / relevance-30 之和 = 真召回保留率)**。
- harness：`dryrun/vps-window-sweep.py`（VPS 跑，**只读**，不碰 `retrieval.py`）。15 POI 覆盖海滩/沙漠/牧场/树屋/水疗/城市/lodge + 含 Leo 点名的 Huatulco + Bonnet Creek + 一个小池(43)。

## 结果（15 真 POI，真相关性）

| POI（池） | 选法 | 近重复≥0.85 | ≥0.92 | worst | 簇/30 | 摸到 | top15留 | rel%留 |
|---|---|---|---|---|---|---|---|---|
| **Huatulco** [硬] (88) | relevance-30 | **3** | 1 | 0.949 | 27 | 30 | 15 | 100% |
| | **maxmin@45** | **0** | 0 | 0.599 | **30** | 45 | 8 | 97% |
| **Bonnet Creek** [硬] (74) | relevance-30 | **4** | 2 | 0.982 | 26 | 30 | 15 | 100% |
| | **maxmin@45** | **0** | 0 | 0.666 | **30** | 45 | 9 | 97% |
| Sandpearl (115) | relevance-30 / **@45** | 6 / **0** | 2/0 | 0.950/0.717 | 25/**30** | – | 15/8 | 100/**96%** |
| Bolt Farm Treehouse (128) | relevance-30 / **@45** | 5 / **0** | 2/0 | 0.939/0.712 | 25/**30** | – | 15/9 | 100/**94%** |
| Ranch at Rock Creek (121) | relevance-30 / **@45** | 4 / **0** | 3/0 | 0.944/0.612 | 26/**30** | – | 15/10 | 100/**97%** |
| Ambiente Sedona [沙漠] (88) | relevance-30 / **@45** | 5 / **0** | 1/0 | 0.986/0.714 | 26/**30** | – | 15/11 | 100/**97%** |
| Tu Tu Tun Lodge [小池] (43) | relevance-30 / **@45** | 3 / **0** | 1/0 | 0.921/0.461 | 27/**30** | – | 15/10 | 100/**92%** |
| Marriott Marquis [城市] (78) | relevance-30 / **@45** | 1 / **0** | 0/0 | 0.865/0.502 | 29/**30** | – | 15/11 | 100/**97%** |
| Little Palm Island [海滩] (101) | relevance-30 / **@45** | 3 / **0** | 0/0 | 0.918/0.560 | 27/**30** | – | 15/10 | 100/**95%** |
| Southall Farm & Inn (128) | relevance-30 / **@45** | 8 / **0** | 3/0 | 0.965/0.625 | 23/**30** | – | 15/9 | 100/**94%** |
| CIVANA Wellness (96) | relevance-30 / **@45** | 2 / **0** | 1/0 | 0.954/0.698 | 28/**30** | – | 15/12 | 100/**97%** |
| The Cliffs Hotel & Spa (90) | relevance-30 / **@45** | 3 / **0** | 1/0 | 0.942/0.596 | 27/**30** | – | 15/8 | 100/**94%** |
| Lido Beach Resort (70) | relevance-30 / **@45** | 6 / **0** | 4/0 | 0.988/0.710 | 24/**30** | – | 15/7 | 100/**95%** |
| Turquoise Place [小] (51) | relevance-30 / **@45** | 6 / **0** | 2/0 | 0.978/0.613 | 24/**30** | – | 15/9 | 100/**92%** |
| Blue Hills Ranch (88) | relevance-30 / **@45** | **9** | 5 | 0.988 | 22 | 30 | 15 | 100% |
| | **maxmin@45** | **0** | 0 | 0.600 | **30** | 45 | 8 | **96%** |

### 三窗口汇总（15 POI 平均）
| 窗口 | 平均近重复对 | 平均簇/30 | 平均 worst | 平均 top15留 | 0近重复的 POI 数 |
|---|---|---|---|---|---|
| maxmin@35 | 1.0 | 29.1 | 0.818 | 12.5 | **8/15**（清不干净） |
| **maxmin@45** | **0.0** | **30.0** | 0.626 | 9.3 | **15/15** ✅ |
| maxmin@60 | 0.0 | 30.0 | 0.506 | 6.9 | 15/15（相关性丢更多） |

## 读数
1. **今天的视觉浪费是真的**（真相关性下，非代理）：基线 1–9 对近重复、平均 ~4.5 对、worst 0.86–0.99（=两条几乎同一镜头同时进池）、22–29 簇。最惨 Blue Hills Ranch（9 对、5 对近乎同一、只 22 簇）；连最干净的城市 Marriott 也有 1 对。
2. **@35 窗口太窄**：15 个里只有 8 个清干净，硬池（Southall/Blue Hills/Turquoise）还剩 3–4 对。
3. **@45 是拐点**：**15/15 全部** → 0 近重复 + 30/30 满簇，worst 掉到 0.46–0.72。**真召回代价小：保留 92–97% 相关性质量**（前15相关留 8–12）。
4. **@60 边际收益小、代价大**：worst 再降一点点，但相关性保留掉到 84–95%、前15相关只留 5–9 → 割相关性换不到多少多样性。小池尤其亏（Turquoise @60 只剩 84%）。
5. **小池优雅**：Tu Tu Tun(43)/Turquoise(51) @45 仍 0 近重复 + 30 簇（摸到池底）；**从不变差**（仍下 30）。

## 建议（数字一致，可锁）
**窗口 = 45** · 近重复阈值 **0.85**（同①；0.92 留给"只删同一镜头"的保守口味）。
理由:@45 是唯一"全清近重复 + 满簇 + 相关性只丢 ~5%"的点;@35 清不干净、@60 多割相关性没多换多样性。

## egress / 边界
- 下载数恒 30 ✅（只换"挑哪 30"）。读向量在下载前、是 DB 读、**不是 egress** ✅。
- 类别软上限 v1 不开 ✅ / per-frame 不碰 ✅ / 默认关 flag ✅（生产路径还没写）。

## 复跑
`dryrun/vps-window-sweep.py` → `scp` 到 VPS → `cd /home/deploy/promo-pipeline-readiness && python3 vps-window-sweep.py`（用其 `.env`：live OpenRouter + SUPABASE）。只读，无写库，不动 `retrieval.py`。
（`dryrun/window-sweep.sql` = 早期本地质心代理版，已被本真相关性 VPS 版取代，留档。）

## STEP-2 — flag-gated 生产路径已写 + 接线证据（窗口 45 已锁）

落点 `retrieval.py::candidate_asset_ids_for_download`（纯函数,向量+相关性在 `pipeline.py` 取好传进来）。
flag `PROMO_DOWNLOAD_DIVERSITY`（config `download_diversity_enabled()`,**默认关**,未自动 arm）。

**接线证据**(把**真的修改后模块**载到 VPS,对真生产数据跑;`dryrun/vps-wiring-evidence.py`,只读):

| POI | flag关==已部署函数 | #关 | #开 | 今天近重复 | 开后近重复 | 与今天重合 |
|---|---|---|---|---|---|---|
| Secrets Huatulco | **True** | 30 | 30 | 1 | **0** | 9/30 |
| Club Wyndham Bonnet Creek | **True** | 30 | 30 | 5 | **0** | 17/30 |
| Sandpearl Resort | **True** | 30 | 30 | 4 | **0** | 15/30 |
| Tu Tu Tun Lodge [小] | **True** | 30 | 30 | 0 | **0** | 6/30 |

- **默认关 = 逐字节同已部署函数**(4/4 `True`,直接比对 VPS 上线代码)✅
- **下载数恒 30**(关/开都 30 → 零额外 egress)✅
- **开 → 0 近重复**(我独立度量 + 函数自报 `residual=0` 双证)✅

**接线时抓到一个真 bug(已修)**:窗口原本取"全部 text-ready 的相关 top-45",但 Huatulco 相关 top 里 23/45 是 visual-**pending** → 只剩 22 个可比 → max-min 没法铺开 → 仍剩 3 对近重复。**修正**:窗口取"**visual-ready** 的相关 top-45"(=dry-run 实际用的池),pending 片 fail-open 仅作相关性兜底回填。修后 Huatulco/Tu Tu Tun 全 0。
**附带提醒**:armed 偏好 visual-ready 片;visual 覆盖还没满的 POI(AIGC 回填进行中),少数高相关但 visual-pending 的片 armed 时不下载 → 等覆盖补齐自然消失。

## arm 与否 = 等渲染 before/after,Leo 定
代码默认关、未自动 arm。要 arm 一批做渲染 before/after,设 `PROMO_DOWNLOAD_DIVERSITY=1` 即可。
