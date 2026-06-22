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
- [x] **AIGC**: P1e partial UNIQUE `(poi_id, recipe_fingerprint) WHERE recipe_fingerprint IS NOT
      NULL AND status <> 'rejected'` = the actual "block duplicate" enforcement.
      owner=AIGC → **ON, confirmed by Leo 2026-06-18.** Fingerprints now both compute (056) AND
      block (P1e) = real cross-paradigm dedup enforced end-to-end.
- [ ] **两仓 (PGC + AIGC)**: P1g — insert a real duplicate, capture the actual postgrest 23505
      error shape (.code? index name in .message/.details?), THEN narrow each side's detection
      precisely.  owner=PGC+AIGC  (downgraded to OBSERVABILITY — on this table all unique
      violations are "already-registered, skip" semantics, so broad-accept is the permanent-safe
      baseline; narrowing only buys honest log labels, not control flow. Not urgent.)

- **Acceptance** (= deploy + real DB check): both paradigms write recipe_input (✓ DB) · 056
  computes fingerprint (✓) · P1e enforces uniqueness (✓ Leo confirmed 2026-06-18) · among approved+fingerprinted
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
- [x] **AIGC**: 列名 = **`poi_description`**(非 hotel_description),已加进 `poi_asset_pois`
  + 投影进 `poi_asset_valid_clips`;310 个活跃 POI 已填(grouped 事实卡片,~2700-5000 字)。
  ⚠️ 视图上只见 118 个非空(其余应为"无有效片段"的 POI,PGC 本就用不到)——可顺手对一句。owner=AIGC ✓
- [x] **PGC**: 读-转发桥**已上线 main `67aa7e7`**(2026-06-22)。**纠正 06-18 的设计**:经 3-agent
  panel 复核,描述走 **raw_row / 管道 B(照抄 `location`),绝不进 `_SNAPSHOT_FIELDS`**——后者喂
  recipe 指纹,塞进去会污染去重(panel 拦下了这个雷)。6 处接线 + compile_promo 的死线
  (`full_pipeline(hotel_description=args.poi_description)`)+ fail-loud 哨兵(列缺失硬停、值 NULL 省略)
  + 每批非空计数。750 测试绿。owner=PGC ✓
- **A/B 实证(5 POI × 2 版,2026-06-22):正面、价值是"事实正确性的刹车"**——不喂卡片模型理直气壮
  瞎编具体数字(180万加仑水族馆/泳池1870年凿/42种菜,全假);喂卡片编造消失、换真事实、还纠正了瞎编。
  对外营销物料里瞎编=负债。**判定:值得全量(下批带上即生效,NULL 容忍)。**
- [ ] **AIGC(下一棒)**: A/B 既已正面 → **把 onboarding 自动生成 `poi_description` 接上**(保准确),
  让新 onboard 的 POI 也带卡片。这是这条价值链的根。owner=AIGC
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
