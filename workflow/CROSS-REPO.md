# Cross-repo handoff board (PGC ↔ AIGC asset_platform)

**This is a thin pointer, NOT the source of truth.** Correctness lives in code + the shared
`release_candidates` / `poi_asset_*` tables (fail-loud 056 trigger, the P1e UNIQUE index, CI).
This board is just a convenience sticky for "who takes the next handoff, and did they verify it."
**The shared issue/board is NOT a lock.** Protocol: the `roadmap-discipline` skill.

- **Ground truth** = the shared Supabase (`release_candidates`, `poi_asset_usage_events`) + each
  repo's deploy state. No real DB check → no tick.
- **Position layer** = PGC `docs/ROADMAP.md` §当前排期 (this board is referenced from there).
- Done handoffs → PGC `docs/ROADMAP.md` §执行日志. This file keeps only **in-flight**.

---

## In-flight handoffs

### 跨范式内容去重:recipe_input → 056 触发器 → P1e 唯一索引 (H1)

Goal: PGC (`pgc_65s`) + AIGC (`aigc_music_remix`) both publish into the shared
`release_candidates`; identical visual content must never be double-published. Each paradigm
supplies `recipe_input` (ordered `source_content_hash`, music+trim excluded); the DB computes
`recipe_fingerprint`; a UNIQUE index structurally blocks duplicates.

- [x] **PGC**: RC insert supplies `recipe_input`; never sets `recipe_fingerprint`; 23505 broad
      per-row tolerance.  owner=PGC → merged+deployed `73eb804`, live-verified (real candidate
      `manifest:manifest_1a5253cb…:variant:1` carries 23-hash recipe_input + rfp2 fingerprint) ✓
- [x] **AIGC**: `music_remix` (path B) also supplies `recipe_input`.  owner=AIGC → DB-confirmed
      1165/1204 (97%) non-NULL ✓ (the "is music_remix deployed?" worry is resolved)
- [x] **AIGC**: 056 `BEFORE INSERT` trigger computes `recipe_fingerprint` from recipe_input.
      owner=AIGC → ON (Leo flipped it); both paradigms' approved rows carry rfp2 fingerprints ✓
- [ ] **AIGC**: P1e partial UNIQUE `(poi_id, recipe_fingerprint) WHERE recipe_fingerprint IS NOT
      NULL AND status <> 'rejected'` = the actual "block duplicate" enforcement.
      owner=AIGC  ⚠️ **待确认开没开** (after: backfill clears existing dups; both factories green ✓)
      — until P1e is on, fingerprints compute but nothing is rejected = measuring, not yet blocking.
- [ ] **两仓 (PGC + AIGC)**: P1g — insert a real duplicate, capture the actual postgrest 23505
      error shape (.code? index name in .message/.details?), THEN narrow each side's detection
      precisely.  owner=PGC+AIGC  (downgraded to OBSERVABILITY — on this table all unique
      violations are "already-registered, skip" semantics, so broad-accept is the permanent-safe
      baseline; narrowing only buys honest log labels, not control flow. Not urgent.)

- **Acceptance** (= deploy + real DB check): both paradigms write recipe_input (✓ DB) · 056
  computes fingerprint (✓) · P1e enforces uniqueness (⚠️ confirm) · among approved+fingerprinted
  rows: 0 duplicate `(poi_id, recipe_fingerprint)` and 0 same-fingerprint-cross-paradigm
  (✓ measured 2026-06-16: 1326 approved, 0 dup, 0 cross-paradigm double-publish).
- **Deploy gate (out-of-order = fail-loud)**: 056 enforcement MUST come AFTER both factories
  supply recipe_input — else the non-supplying side's RC inserts are rejected fail-loud.
  ✓ satisfied (both supply before 056 went on).

---

### POI 事实描述通道:poi_asset_pois.hotel_description → 脚本事实地基 (H2)

Goal: 给每个 POI 补一份**事实性描述**(这地方是什么 / 在哪 / 特色·设施·调性),让 PGC
脚本生成有事实可依,而不是靠大模型训练记忆猜(现状:脚本里的数字/价格全是模型瞎编)。
Brief(AIGC 起草)→ `workflow/projects/poi-dimension-info/brief-for-pgc.md`。

- **Scope decided (Leo 2026-06-18): ONE column `hotel_description`(自由文本)** —— 不分
  `notable_details`(单独"要点"块会变必背清单→过度依赖+复述,反增雷同);将来真出现特别
  notable 的情况再加二列。防重机制(变体轮换/跨视频惩罚)暂不上。
- [ ] **AIGC**: 加 `hotel_description` 到 `poi_asset_pois` + 投影进契约视图
  `poi_asset_valid_clips`(契约级 §1.1 改动 → contract PR 先行 + 通知两仓);在
  onboarding 建档(操作员/AI 起草皆可),回填现有 ~31 活跃 POI。料来自真实渠道(Booking 等);
  **不强求"独特",别加严约束**;provenance(记来源)可选、非必须。owner=AIGC
- [ ] **PGC**: 写"读-转发桥"(AIGC reviewer 2026-06-18 独立审 + PGC 复核确认)——
  ① `hotel_description` 加进读适配器 `_SNAPSHOT_FIELDS`(`poi_asset_valid_clips.py:33`,
  现在到 file_size_bytes 为止,会把该列丢弃);② 从 POI 摘要一路转发
  `run_batch → compile_promo → pipeline`(`run_batch.py` 现在零引用、根本不传)。
  ⚠️ 注意:prompt 模板侧已接到深层(`script_prompt_builder.py:378` 的
  `$hotel_description_block` 有内容才插),**但桥没写 = 即便 AIGC 发了列,脚本仍拿空串、
  DESCRIPTION 块不亮**(别误判"已通")。顺手:builder 用 `Template.substitute`(:386),
  写桥时改 `safe_substitute` 防将来漏 key KeyError。prompt 维持现有"一个 fact/脚本"框架。
  owner=PGC(前置依赖 AIGC 先发列;PGC 不抢跑)。
- **Acceptance**: 活跃 POI 的 `hotel_description` 有真实内容 → PGC 脚本 DESCRIPTION 块点亮、
  数字有据可依(real DB check:视图能读到非空 hotel_description)。
- Read path: PGC 走 `poi_asset_valid_clips` 视图(事实跟 clip 行重复,PGC 去重到 POI 级)。

---

## Adjacent (PGC-side, informed by cross-repo but NOT a handoff)

- **Cross-paradigm cooldown design call (Leo)**: PGC's selection cooldown reads the SHARED
  `poi_asset_usage_events` with NO paradigm filter → music_remix's production cools down PGC's POI
  pool (measured 2026-06-16: 162 POIs cooled, 145 by music_remix → `fresh_eligible=0` on a real
  batch). `soft-cooldown` (merged `73eb804`) fixed the starvation (prefer fresh, fall back to
  cooled). Open Leo decision: should PGC cooldown stay cross-paradigm, or count only PGC's own
  usage? PGC-internal code change either way; not an AIGC handoff.
