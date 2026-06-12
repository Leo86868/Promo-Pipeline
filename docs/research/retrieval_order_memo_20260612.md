# 备忘录:脚本先行 vs 素材先行 —— 外部调研 + 第一性原理分析

**给 Leo / 2026-06-12 / 纯探索(fresh-context agent),未改任何代码**
**配套实验**:同日 script-only A/B(现状 3 范文 vs 零范文,各 5 篇)证明删范文不治相似——B 臂收敛 4-gram 16 个 > A 臂 10 个,只是口头禅从范文 DNA 换成模型 DNA("two and a half acres" 4/5 篇)。真元凶 = 恒定输入 + 采样必然收敛。

---

## 〇、一句话结论

**"脚本先写、素材后配"这个顺序本身没选错,真正的病根是"每条视频喂给 Gemini 的简报(brief)一模一样"。** 如果只改一件事:**给每条视频随机抽一个不同的素材子集来做简报(按品类分层抽样 + 按历史使用次数降权),而不是每次都把全库 99 条都告诉 Gemini。**

## 一、奶奶版比喻

厨师(Gemini)给同一家酒店写菜单。现在每次都把**整个冰箱的全部存货清单**给他看——清单不变,厨师每次都想到"有火锅料,来个火锅吧",三桌菜三次火锅,而且端上来的是**同一包**(库里只有一包)。改法不是让厨师后写菜单,而是**每次只给他看冰箱里随机一半的格子**。

## 二、外部调研

- **MoneyPrinterTurbo**(源码 `app/services/llm.py` generate_terms / `app/services/video.py` combine_videos):脚本先行 → 生成搜索词 → Pexels 拉素材;片段与句子不做语义匹配,防重复仅 `_prioritize_unique_source_clips`。**多样性来自 Pexels 近乎无限的库。**
- **ShortGPT**(`shortGPT/engine/content_video_engine.py`):脚本先行,逐段(带时间戳)生成搜索词,`used_links` 仅防单视频内重复。**跨视频重复完全不管——库无限。**
- **Write-A-Video**(SIGGRAPH Asia 2019):固定个人库 + 文本先行 + 语义检索——我们架构的学术祖师爷,但只做单条视频。
- **Transcript to Video**(Adobe, arXiv 2107.11851)、**B-Script**(CHI 2019):同为 transcript 先行。
- **AutoCut**(快手+清华, arXiv 2603.28366):**唯一双模式工业系统**。script-driven(给定脚本选片)与 footage-driven(先圈定本条视频的素材,再让脚本贴着写,句数=片数)。后者是"素材先行"的工业先例。

**三个关键观察**:① 开源工具的脚本先行成立于"素材空间无界";我们固定 99 条小库,brief 是接地的必需品,于是"brief 喂什么"成了全管道熵的总闸门。② AutoCut footage-driven 的精髓 = 接地集合从"全库"换成"本条视频圈定子集"。③ **没有任何被调研系统解决"同一固定库批量出片的跨视频场景重复"——没有现成轮子。**

## 三、第一性原理

```
全库 brief(每条视频恒定)→ Gemini 脚本(唯一熵源)→ 句子检索(argmax)
→ beat 切分(确定性)→ packer(确定性,仅窗口轮换)
```

检索是 argmax、下游全确定性——全管道的熵只有 Gemini 采样一处,而其输入每次完全相同。输入相同 → 输出分布相同 → 相似脚本 → 相似 query → 同一批片。**few-shot 修复治"说法雷同",治不了"题材必然撞车":孤品场景(唯一的 fire-pit 片)只要被提到就必然召回。** 多样性应注入在"最上游的、之后被确定性放大的输入"——brief 本身。

## 四、四个备选

| 方案 | 买到什么 | 风险 | 难度 |
|---|---|---|---|
| (a) 素材先行:分层+usage 降权抽子集,brief 与检索池都限子集 | 孤品撞车概率 ~100%→抽样率;三层同时散开;长尾曝光上升 | 抽掉最佳片(解法:每类保底 1-2 条);越界幻觉反而被低分暴露(fail-loud) | 低-中:brief 前加纯函数 sampler + 检索 id 过滤 |
| (b) 交错式:大纲→逐段检索→对着结果定稿 | 接地最紧 | **字数控制报废**(160 词全局预算被五段漂移叠加打碎);LLM 调用 ×3-6;且不自动解决跨视频重复 | 高,不推荐 |
| (c) 只轮换 brief,检索仍全库 | 题材散开(~80% 收益) | 幻觉/泛化措辞仍可能召回孤品 | **最低:一个 sampler,检索一行不动** |
| (d) MMR/DPP 多样性抽样(子集更均匀)、packer 跨视频软惩罚 | 备件 | packer 层补丁对抗上游已收敛的 query——治标,不推荐 | — |

## 五、建议

1. **P1 最小刀 = (c)**:只轮换 brief。验收:同 POI 跨视频 clip 重复率显著下降;字数分布不退化。
2. **不够再升 (a)**:检索池限子集 + "子集内最高分仍低于阈值"大声告警。
3. **脚本先行骨架不要推倒**(160 词预算唯一控制点;全部同行佐证;AutoCut 反例细看其实是 (a))。
4. few-shot 修复继续做(治措辞),但天花板明确:brief 不轮换,题材撞车有结构性下限。

### Sources

- https://github.com/harry0703/MoneyPrinterTurbo (`app/services/llm.py`, `app/services/video.py`)
- https://github.com/RayVentura/ShortGPT (`shortGPT/engine/content_video_engine.py`)
- AutoCut: arXiv 2603.28366(script-driven / footage-driven 双模式定义)
- Write-A-Video: SIGGRAPH Asia 2019, ACM TOG / http://miaowang.me/write-a-video/
- Transcript to Video: arXiv 2107.11851 (ACM MM 2022)
- B-Script: CHI 2019
