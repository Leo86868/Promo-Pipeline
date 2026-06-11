# PGC Pipeline — 当前进度

**最后更新**: 2026-05-03
**当前 main HEAD**: `7647404` (S2c merged) = **v0.5 — BASELINE COMPLETE 🎉**
**GitHub**: https://github.com/Leo86868/Promo-Pipeline

---

## 你现在停在哪儿

**baseline 完工**。整套 v0 → v0.5 的拆分 + 清理 + 修 bug 全部 landed + push 上 GitHub。

```
✅ v0 cut             从 promo-lab 抠出来
✅ S0 cold-start      3 段视频证明能用
✅ S0.5 修 bug        per-variant WPM + 文件名
✅ S1 删死代码        -221 行
✅ S2a 拆 tts_engine  1170 → 364 行 (-69%)
✅ S2b 拆 clip_assigner 891 → 279 行 (-69%)
✅ S2c 拆 script_gen  779 → 515 行 (-34%, 撞测试耦合上限)

★ baseline 完成 ★
```

**你现在可以拿这个 repo 日常做视频**——不需要再等任何 sprint。

---

## 你回来时第 1 件事（取决于你想做啥）

| 想干啥 | 该做啥 |
|---|---|
| 用一下，看看实际怎样 | 直接用 — `compile_promo --poi "..." --local-clips ...`。需要 BGM 路径 + 真材料库（在 promo-lab/material/ 那边）。|
| 觉得操作时哪里不爽 | 跟 orchestrator 说 — 我们开一个针对性的 mini-sprint 解决 |
| 想做 S3（看砍掉的功能要不要回流）| 跟 orchestrator 说 "开 S3" |
| 想做 S3.5（arsenal 完整性审计） | 跟 orchestrator 说 "开 S3.5" |

---

## 剩下可选的 sprint

```
⏳ S3   Tier-2 回流研究            半天聊，不写代码
⏳ S3.5 Arsenal 完整性审计 ★ NEW   半天-1 天，把"伪装成代码的数据"挪进 arsenal
```

**两个都不强制**。原因详见下面的"为啥可以现在停"。

### S3 是啥

v0 cut 时砍掉了一堆功能：
- BB-browser 自动抓 POI 图片
- KIE / Seedance 自动生成视频片段
- WaveSpeed 1080p 升清
- curate_and_generate 流水线
- Sprint workflow 工具链

S3 = 跟 orchestrator 聊 → 看哪些你日常用得上 → 决定要不要回流（开新 sprint 实施）。

### S3.5 是啥（你 2026-05-03 自己发现的 gap）

v0 把 4 个数据库 externalize 到 arsenal/ 了（system_prompts / voices / personas / script_skeletons），但**漏了几个**：
- `HOOK_TECHNIQUES` —— 6 个开场风格，写死在 Python 里
- `_DEFAULT_PERSONA_PATH` —— 默认人设路径
- `DEFAULT_TARGET_COVERAGE` / `PER_GAP_CAP_MS` / 等"政策性数字"

S3.5 = 系统性扫一遍 → 把"操作员该能调的"全部挪进 arsenal → 让你看到 `arsenal/` 就**等于**"我能调的全部"。

详细 backlog: `memory/project_pgc_arsenal_completeness_backlog.md`

---

## 为啥可以现在停

1. **main 永远稳定** —— v0.5 已 push GitHub，回来无缝接上
2. **真用一阵子** —— 真做几个视频会暴露真实需求，比预想更准
3. **6 个 sprint 跑下来脑子需要消化** —— 强行连 S3 / S3.5 不会更高效

等你哪天发现"诶 X 没了不方便" 或 "想加新 hook 风格但要改代码烦死了"——那时候**就是**开下个 sprint 的最佳时机。

---

## 工作流程（每个 sprint 都一样，记着以备复用）

```
我（orchestrator / promo-lab）写 prompt
   ↓
你贴给 UFO（worktree session）
   ↓
UFO 跑命令 / 改代码 / 跑测试
   ↓
UFO 贴回执给你
   ↓
你转回执给我
   ↓
我审 → 判 PASS / FAIL
   ↓
PASS → UFO 顺手 merge + push
   ↓
进下一个 sprint
```

---

## 关键路径速查

| 项 | 路径 / 值 |
|---|---|
| Main checkout | `/Users/leowu/pgc-pipeline` |
| Worktree (UFO 在这) | `/Users/leowu/.superset/worktrees/pgc-pipeline/cleaning-up-a-messy-repo-and-establishing-a-proper` |
| Main HEAD | `7647404` (S2c merged, v0.5 baseline) |
| GitHub remote | https://github.com/Leo86868/Promo-Pipeline |
| v0 cut 合同 | `/Users/leowu/promo-lab/workflow/projects/pgc-pipeline/sprints/v0-extraction-contract.md` |
| 真测素材库 | `/Users/leowu/promo-lab/material/ocean-key-resort-spa/clips/` (Ocean Key, baseline POI) |
| 历史 sprint branches（forensics）| `s1-pruning` / `s2a-tts-engine-split` / `s2b-clip-assigner-split` / `s2c-script-generator-split` 都未删 |

---

## 已存的 Memory（你回来不用再问我）

记忆在 `/Users/leowu/.claude/projects/-Users-leowu-promo-lab/memory/`，自动读。重要的几条：

- **`feedback_split_principle_api_service`** — 你的"API service 1 输入 1 输出 + isolated"原则
- **`feedback_executor_critique_beats_orchestrator`** — UFO 反驳 orchestrator 时倾向信 UFO
- **`feedback_atomic_commit_no_orphans`** — 删/移代码时同 commit 清掉 self-introduced orphans
- **`feedback_operator_lets_ufo_handle_merges`** — sprint PASS 后默认让 UFO 顺手做 merge + push
- **`project_pgc_test_health_backlog`** — 测试结构耦合问题（在 S2c 又被证实为真），post-S3 audit 候选
- **`project_pgc_arsenal_completeness_backlog`** — S3.5 sprint 议程，2026-05-03 发现的 gap

---

## 你随时可以问的问题

回来忘了在哪 → 把这个文件给 orchestrator 看，再说一句"我们停在哪儿"——能立刻接上下文。

想中途换方向（不做某个 sprint / 跳过 / 加新的）随时说。roadmap 是给你服务的。

---

*这个文件是 orchestrator 帮你存档用的，不是 v0 contract 一类的合同。每次 sprint 关门会顺手更新。*
