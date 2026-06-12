# AI 腔观察名单(watchlist — 只记录,不执法)

**政策(09b C5 同款)**:这份清单本身**不进** persona 的 `forbidden_phrases`。
谁在生产脚本里被抓到现行(高频出现),谁才晋升进禁用词——抓现行的尺子是
4-gram 频率分析(对批次 sidecar 的 `variants[].script.segments[].text` 跑,
方法见 `docs/research/retrieval_order_memo_20260612.md` 配套实验)。

**为什么不整包进禁用**:50 行 "please avoid" 会稀释 prompt 注意力;我们有
validator 硬卡 + 重试,执法应该挂在被证实的违例上,不挂在想象的违例上。

## 一、已抓现行(晋升候选,等下一次 4-gram 复查确认后提请 Leo)

来自 2026-06-11/12 生产批与 A/B 实验的实测高频:

- `right on the sand`(生产批 4/6 条;A/B 两臂都出现)— 模型自带口头禅
- `you find your spot (by)`(生产批 3/6)
- `it's a place that (doesn't)`(收尾公式,5/6 条)— 已改为 CLOSE guidance
  禁令 + 范文收尾同步修正(2026-06-12),下批验证,若仍出现再进禁用词
- `two and a half acres`(零范文实验 4/5 篇)— 简报显著性偏食的产物,
  根治靠 brief sampler(V2),不适合进禁用词(它是真实事实)

## 二、外部清单(观察项——尚未在我们的产出里抓到现行)

英文 AI-tell(boringmarketer DR gist / create-viral-content):

- delve / unlock / unleash / game-changer / landscape /
  "In today's fast-paced world" / elevate / nestled / boasts /
  "whether you're X or Y" / tapestry / oasis of calm
- 结构 tell:每段长度雷同、破折号滥用、三连排比成瘾

中文生态(douyin-creator-toolkit 去AI味 Agent,翻译保留原意):

- 过度整齐、过度总结、明显 AI 套话;缺少情绪起伏和停顿感

两条万能人检(给 Leo 看片/读稿时用):

1. "Would you actually say this to a friend?"
2. "Read aloud — if you stumble, readers will too."

## 三、用法

```bash
# 对一个批次的 sidecar 跑 4-gram 频率(≥3/批 视为现行)
# 参考实现:docs/research/ 配套实验脚本的 ngrams() 归一化
#(注意弯引号 ' 要先归一,否则漏报)
```

晋升流程:抓到现行 → 报 Leo 决策卡 → 批准后写进
`personas/third_person_promo.yaml` 的 `forbidden_phrases`。
