# 视觉嵌入去重 / 多样性选片 — 项目入口

**一句话:** 给每条素材片算一个"长相指纹"(DINOv2 视觉向量),让下游能**按画面**去重 + 选多样,取代之前用错的"文字描述向量"。

**状态(2026-06-24):** 技术调研 + 两轮实验**已完成、验证通过**。下一步 = 搭地基(加库列 + 回填 15k 1080p 素材)。**还没动生产、没动库。**

---

## 谁该看哪个文件

| 你是… | 看这个 |
|---|---|
| 想知道**结论 + 数字**(选哪个模型、存什么、多少钱) | [`ROUND2-REPORT.md`](ROUND2-REPORT.md) ← 最新、最全 |
| 想知道**为什么选 DINOv2 不选 CLIP**、技术路线怎么定的 | [`FINDINGS-route-and-ab-design.md`](FINDINGS-route-and-ab-design.md) |
| 想看**第一轮**(发现文字向量瞎了)的来龙去脉 | [`ROUND1-RESULTS-huatulco.md`](ROUND1-RESULTS-huatulco.md) |
| **PGC / 任何消费方**:想知道**怎么用这列向量选片** | [`prototype/select_diverse.py`](prototype/select_diverse.py) ← 干净参考实现,numpy 即可读 |
| 想**复现实验**(自己跑几个 POI 看效果) | `prototype/` 四个脚本(见下) |
| 当初的工单 | [`WORKER-TICKET.md`](WORKER-TICKET.md) |

---

## 锁定的配方(v1)

```
每条片 → ffmpeg 抽帧(1fps,min4/max16,掐首尾0.25s)
       → DINOv2-base 每帧 CLS token → L2 归一化
       → 片向量 = 帧均值(mean)     ← 一条片存 1 个 768 维向量
比对/选片时(消费侧):可选 mean-center 白化拉大区分度
```

**为什么是这些(都用真数据验过,见 ROUND2-REPORT):**
- **DINOv2 不是 CLIP** —— CLIP 看"内容是什么"(会把不同泳池都当一样),DINOv2 看"长什么样"。
- **存均值(一段)不是每帧(八段)** —— v1 做"选多样",均值够;"精确删同一条素材"才需每帧,那是以后的事。
- **1fps 不是 2fps** —— 实测零损失,省一半。
- **只回填 1080p**(14,944 条 / 377GB);720p 的 9,119 条跳过(本就不用)。

---

## 这次验证了什么(给没空读全文的)

1. **文字向量确实瞎**:同 POI 内文字向量余弦是 0.15–0.96 一坨云,跟画面对不上。
2. **DINOv2 + 均值能干净分出"太像 vs 不同"**(选多样所需),验证通过。
3. **max-min 选片真管用**:
   - Huatulco(158 条,159 对近重复泳池)选 30 → 只含 4 个**不同**泳池,重复堆被压成代表,最像一对 0.53。
   - Villa D'Este(43 条,贫瘠)选 30 → 优雅降级,最像一对 0.55,不硬凑重复。
   - 图见 `round2-charts/{huatulco,villadeste}_pick30_inorder.png`。
4. **成本/速率**:算力非瓶颈(Mac 4 worker ~20 分钟跑完全库);真成本 = egress ~$11 一次性 + GPU ~$1–5。

---

## 边界:谁干什么(重要,别越界)

```
我们(asset 平台)= 造向量          PGC / 消费方 = 用向量
  · 加 visual_embedding_vector 列     · max-min 选片(select_diverse.py)
  · 回填 15k 存量 + 新片入库自动算     · 定"太像"阈值
  · 交付 = 那一列向量                  · 去重 / 多样性 packer gate
```

`prototype/select_diverse.py` 是给消费侧的**参考**,证明"这列向量能用";真正怎么选、阈值划哪,是消费方的事。

---

## 还没定的(等拍板)

- **"太像"阈值**:参考线已量出(同镜头≈0.94 / 同地异镜≈0.87 / 不同≈0.28)。划 0.92(只砍同镜头)还是 0.85(连同地异镜也砍)——消费侧审美判断,转告 PGC。
- **地基 5 步**(见 ROUND2-REPORT 末):① CONTRACT 变更 PR ② 加 vector(768) 列 ③ 回填 worker(分片/幂等/先 dry-run 5 个)④ asset-enrichment skill 加视觉 embed 步 ⑤ 选片层归 PGC。

---

## prototype/ 里有什么

| 文件 | 干嘛 | 注意 |
|---|---|---|
| `select_diverse.py` | **消费侧参考**:max-min 选片 + 近重复对 + 白化,numpy 即可 | production 拿去改的就是这个 |
| `download_pois.py` | 从 poi-assets 桶按 POI 下原片(curl,读 .env) | 实验用 |
| `build_vectors_v2.py` | 抽帧 + DINOv2 → 存均值向量 + 每帧(.npz) | 锁定配方;路径硬编码,复现时改 |
| `analyze_v2.py` | 分离测试(均值/每帧/白化)+ max-min 选片图 | 复现实验结论 |
| `export_review_v2.py` | 把对照视频导成 small folders 给人肉眼核 | 实验用 |

> 这些是**原型/参考**,不是 production 代码(路径硬编码、给实验用)。production 回填 worker 另写,逻辑参照 `build_vectors_v2.py` 的配方 + `select_diverse.py` 的消费方式。

> 实验产出的**对照视频**(4 POI × 太像/多样/边界/选30)当时在本地 `~/Downloads/visual-dedup-review/`,体积大没进 repo;要重看跑 `export_review_v2.py`。
