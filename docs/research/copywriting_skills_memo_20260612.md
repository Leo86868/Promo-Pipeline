# 备忘录:GitHub 上的"爆款文案神器"到底值不值得抄?

**给 Leo · 2026-06-12 · fresh-context agent 调研:英文 8 源 + 中文 8 源,全部读 prompt 原文非 README**

## 一句话结论

Leo 的直觉对:星数最高的生产工具(MoneyPrinterTurbo ~86k★)脚本 prompt 仅 ~10 行,零"爆款工程";花哨多步框架的价值浓缩后就是十几句好话。我们的架构(arsenal + validator + retry)比所有公开 prompt 包多一样它们没有的东西——**强制执行**。该抄的是几张小卡片,不是整套流程。

## 按机制分桶

- **(a) 钩子分类表/开头公式 — 最值得抄**。两个语言生态唯一共识:钩子按命名类型写。最干净的源:claude-vibes scriptwriting-methodology(7 行钩子表带例句)、claude-youtube(~164★,每钩子带 must-not:"Curiosity-Gap 绝不能开头给答案"、"Story 必须 in medias res")、douyin-creator-toolkit(~593★,五型:结果反差/认知冲突/利益点/痛点/悬念)、LangGPT《流量黑客》标题五型。零调用纯文本。
- **(b) 节拍模板**:我们 5 段 skeleton(字数+片源类别映射)比所有公开模板细,**已覆盖且领先**。
- **(c) 人设卡**:已覆盖,质量在外面平均线之上。
- **(d) 自评重写循环 — 吹最响证据最薄**。五人格攻击循环 = 每条 2-5 次额外调用;"49% 提升"全部不可核实;中文生态根本没人真做出打分→重写循环。我们已有 validate+retry 硬卡 + 休眠 best-of-N 槽。**不进口**。
- **(e) 受众研究链**:解决冷启动;我们产品/受众/痛点恒定,**纯表演,不采纳**。
- **(f) 违禁词/去AI味清单 — 第二值得抄**。boringmarketer gist(AI-tell:delve/unlock/unleash/game-changer + "Read aloud, if you stumble readers will too")、去AI味 Agent("避免过度整齐、过度总结,保留情绪起伏")。当**观察名单**不当采购清单——谁被生产日志抓现行谁才进 BANNED(09b C5 政策)。
- **视觉接地**:全网没有任何包有 clip-inventory 硬约束——我们独有护城河。

## 裁决(质量÷复杂度排名)

0. **前置:验证 hook 注入 no-op 线头**(已由 orchestrator 验真,见决策卡/执行日志)。
1. **CLOSE 段加"反鸡汤"半句**(ShortGPT:"DO NOT end with a moral conclusion — just stop"):点名后禁总结性升华,落在具体画面即停。纯 YAML 一行。
2. **6 张裸 hook 标签升级成 hook 卡**:标签 + 一行手法 + 一条 must-not(素材现成)。`script_hooks.yaml` 结构化 + prompt builder ~5-10 行。
3. **外部 AI-tell 清单当观察名单**:合并成 watchlist,定期 grep 生产脚本,抓现行才进 BANNED_WORDS。
4. (可选零成本)prompt 末尾一句自检:"Read each line as if saying it to a friend; cut any line you would not say out loud."

**明确不采纳**:受众研究链、五人格自评循环(将来真要质量裁判 = 激活已有 best-of-N 槽 + 单调用小裁判)、算法迎合类许愿、伪精确自打分、整套 AIDA/PAS 换骨(降级)。

## 十句好话(全部调研浓缩)

1. "DO NOT end with a moral conclusion — just stop when it makes sense."(ShortGPT)
2. "Curiosity-gap 钩子绝不能在开头给出答案。"(claude-youtube)
3. "Story 开场必须 in medias res,禁止 'Let me tell you about a time when…'"(同上)
4. "结尾镜像开头,做无缝循环回放。"(claude-youtube shorts.md)
5. "Would you actually say this to a friend? Read aloud — if you stumble, readers will too."(boringmarketer)
6. AI-tell 清单:delve/unlock/unleash/game-changer/landscape/"In today's fast-paced world" + 段长雷同等结构 tell。
7. "避免过度整齐、过度总结和明显 AI 套话,保留情绪起伏和停顿感。"(douyin-creator-toolkit)
8. 钩子五型:结果反差/认知冲突/利益点/痛点/悬念。(douyin-creator-toolkit)
9. 标题五型:恐吓/数字/圈层/情绪/强利益。(LangGPT 流量黑客)
10. "黄金三秒" + 钩子▸痛点▸方案▸召唤。(已由 skeleton 实现,留作对照)

## 出处

MoneyPrinterTurbo(harry0703)、ShortGPT(RayVentura)、claude-vibes(mike-coulbourn)、boringmarketer DR gist、claude-youtube(AgriciDaniel)、create-viral-content(aaaronmiller)、LangGPT(langgptai ~12.2k★)、douyin-creator-toolkit(lid664951-crypto)、XHS-CopyCat(bantang0820)、qiaomu-prompts(joeseesun);负面对照:lijigang/prompts、anthropics/skills 均无文案 skill。
