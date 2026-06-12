# 愿景稿:目录终态 + 一条视频的一生(数据流)

> 状态:讨论定稿(Leo + orchestrator,2026-06-12)。
> 归宿:P3 根目录瘦身时,数据流图正式并入根 `architecture.md`、
> 目录导览并入 `README.md`,本草稿随之删除。
> 在那之前,这里就是这两张图的家。

---

## 一、目录终态(P3 完工后的样子)

```
pgc-pipeline/
├── README.md            ← 30 秒入门:这是什么、怎么发一批
├── architecture.md      ← 工程总图 + 数据流图的正式家
├── pyproject.toml · LICENSE · .env(.example)
│
├── promo/
│   ├── arsenal/         ←【Leo 的控制面板】品味全在这
│   │   ├── README.md       旋钮索引:"想调X→改Y"
│   │   ├── script_skeletons/   每 type 一张全息卡(字数/节奏/门槛)
│   │   └── personas/ · voices/ · script_hooks.yaml
│   ├── core/            ←【法律与机器】按工序分舱
│   │   ├── script/ narrate/ analyze/ assign/ render/ pipeline/
│   │   └── llm/ + model_adapters/(供应商通讯录:换模型在此/env)
│   ├── cli/             ← 命令入口(run_batch / compile / revert…)
│   └── tests/           ← 输入/输出契约测试(P4 后纯净版)
│
├── docs/                ← 文档馆(P3 把根上五份 .md 收进来)
│   ├── ROADMAP.md(含「当前排期」) · LEARNING.md · BACKLOG.md
│   ├── operations/(runbook · 生产契约) · schemas/
├── runs/                ← 运行时数据归一(gitignored)
├── scripts/ · ci/
└── .codex/skills/       ← operator skill(自然语言 → 生产命令的翻译官)
```

**三权分立**:品味归 arsenal(卡片),法律归 core(代码),数据归中台(表)。

**明文约定(故意与姊妹 repo 不同)**:本 repo **没有 `archive/legacy/` 冷藏区**。
退役代码直接删除,git 历史就是冷藏室(例:Gemini #2 链 = `git revert 1f28902`
即解冻)。理由:本 repo 的主要读者是 agent,死代码即误导源。请勿好心引入冷藏目录。

---

## 二、一条视频的一生(数据流,对齐姊妹 repo 的"素材的一生")

```
 中台(AIGC repo 的表)             PGC(本 repo:工序 → 目录)
─────────────────────────────────────────────────────────────────
 poi_asset_pois 店档案 ────选店──→ ① 排单 + 签收单诞生      cli/run_batch
 (未来:档案袋四列在此)                ↓
 poi_asset_valid_clips 素材 ─────→ ② 视觉简报(全量分类摘要) core/pipeline
                                      ↓
                                   ③ 写稿(骰子+发牌)→ 字数闸 core/script
                                      ↓
                                   ④ 配音 → 每字时刻表        core/narrate
                                      ↓
 poi_asset_embeddings 向量 ──────→ ⑤ 切幻灯片→检索→排片员    core/assign
 poi_asset_usage_events 账本 ────→    (窗口轮换在此翻账,fail-closed)
                                      ↓ 验证器(法律关卡)
                                   ⑥ 渲染 → master + manifest core/render
                                      ↓ 尾巴(与下一条的①-⑥并行流水)
                                   ⑦ WaveSpeed 升级 → Drive 上传
 poi_asset_usage_events ←──记账─── ⑧ usage 写回(窗口四字段)
 release_candidates    ←──上架─── ⑨ 候选注册  ✅ 一条视频完工
─────────────────────────────────────────────────────────────────
 distribution_status(分发侧)·····→ 翻转三:表现回流(远期的桥)
```

**读图法**:左列全是中台的表、右列全是本 repo 的目录——两个 repo 的每一条
接缝都应有 `CONTRACT.md` 条目背书;新增任何左右连线 = 先开契约 PR。

---

## 三、差异机制速查(同店多条视频为什么不雷同)

| 层 | 机制 | 内容 |
|---|---|---|
| 固定层 | 卡片 + 品牌 | 五段结构、字数、节奏、persona 口吻 |
| 发牌层 | 确定性轮换 | 声音、音乐、hook 牌(P2 第 5 步接 seed)、素材窗口 |
| 骰子层 | temperature 0.85 | 每个句子、每个卖点的取舍 |
