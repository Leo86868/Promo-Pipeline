# DB-First Assignment — 实现规格(供 Leo 审核)

合并两个独立设计(Plan + advisor)的对齐结论。fork 采用 **A-minimal**(每变体下载、TTS 不前移)。
**不重写** packer 引擎(`_global_assign`/`_solve_assignment`),但 claim-1/2 + near-dup 语义是**在现有 global assignment 上补成本项 + 硬校验 = 改 packer 行为**(措辞别说"不动")。所有改动 flag-gated,默认 = 今天。

## 数据流 before → after
```
现在(下载先于匹配):
  ready_assets(全库,已在内存,pipeline.py:390)
   → 脚本生成
   → candidate_asset_ids_for_download 选30/35(②多样性)
   → fetch_candidate_clips 下载30
   → ★clips_metadata 被过滤成30(pipeline.py:~552)★ ← 30上限在这产生
   → 每变体:TTS → assign(只在30里)→ 下载好的渲染

变后(匹配先于下载,A-minimal):
  ready_assets(全库)+ inline 文本embedding
   → 脚本生成
   → 每变体:TTS → assign【在全库metadata上】(_global_assign 看全库)
            → 算 reserve → 【此刻才下载 assigned ∪ reserve】→ 渲染
   ↑ 不再有"过滤成30";下载从"匹配前门槛"挪到"每变体匹配后"
```

## 文件改动清单(逐条可核)

### 1. `promo/core/config.py`
- 新增 `db_first_assignment_enabled()` 读 `PROMO_DB_FIRST_ASSIGNMENT`(默认 False),镜像现有 `global_assignment_enabled()`(:207)。
- 新增 `bridge_reserve_count()` 读 `PROMO_BRIDGE_RESERVE_COUNT`(默认 0 → 代码用 `max(8, ceil(0.5×beats))`)。

### 2. `promo/core/assets/retrieval.py`
- `clip_metadata_from_ready_assets`(:239,现在**丢掉了** embedding)→ 加可选 `with_embedding=True`,
  设 `m["embedding"] = list(asset.embedding_vector)`(向量已在内存)。
  目的:让下游 `_assign_clips_packer` 走 **inline 分支**(steps.py:772),文本embedding **零额外DB读**。
- ②(`_diverse_download_asset_ids`/`download_diversity_enabled`/`_fetch_visual_vectors_if_armed`)标注 `# RETIRE with download-first`,**本期不删**(后续清理)。

### 3. `promo/core/assign/packer.py`(claim-1/2 + reserve,引擎逻辑不动)
- **claim-1(窗口新鲜度软项)**:`_global_assign`(:239)新增入参 `used_windows`+`clip_to_asset`+`spans`;
  对每个 (beat,clip) 算 `free_windows(duration, used, min_len_sec=span)`,**无空闲窗口** → `base[bi,j] += GLOBAL_STALE_WINDOW_PENALTY`(新常量 ~0.10,同余弦刻度;0.05相邻/0.50近重复)。
  **保持 SOFT,永不 `_INFEASIBLE`**(窗口耗尽是软契约,packer.py:19-24)。经 `_pack_clips_global` 透传。
- **claim-2(太短 fail-loud)**:`_solve_assignment` 兜底(:233)现在会塞"最便宜未用列"哪怕盖不住。
  改:`_pack_clips_global` solve 后**逐beat断言** `durations[pick]+TOL >= span`,否则 `raise ClipAssignmentError`(同 greedy:512 / global len-check:601 的 fail-loud 契约)。加回归测试。
- **新增** `select_bridge_reserve(clip_metadata, clip_durations, assigned_clip_ids, used_windows, count) -> list[str]`:
  从全库未指派可覆盖片里选 `count` 条,**criteria=最低usage_count → 最长duration**(桥是无字幕填充,不看相关性;同 retrieval.py:423-430 既有排序)。`_pack_clips_global`/`pack_clips` 把 `reserve_clip_ids` 放进 provenance。

### 4. `promo/core/pipeline/steps.py`
- `_assign_clips_packer`(:699):嵌入/视觉/窗口 fetch 已 keyed by `clip_to_asset`(:604/:651/:846)→ 喂全库 metadata 即全库富集,**无新查询形状**。
- 只把 `pack_provenance["reserve_clip_ids"]` 透传进返回的 provenance。**无签名改动**。

### 5. `promo/core/pipeline/variant_loop.py`
- `_run_variant_loop` **保签名**;DB-first 时:assign 后、`build_props_from_script` 前,新 helper
  `_materialize_assigned_clips(backend, poi_name, variant_tmp_dir, assignments, reserve_ids, clip_to_asset)`:
  `assigned ∪ reserve` 的 asset_id → `backend.fetch_candidate_clips(...)`(已存在,:217)→ 得**本变体** `variant_clip_paths`。
  下游 `build_props`(:229)/`stage_media`(:260)改用 `variant_clip_paths`(不再是 run 级空 `clip_paths`)。

### 6. `promo/core/pipeline/pipeline.py`
- `candidate_only_mode` 分支:DB-first 旗标开 → **跳过** :540-557 的"下载+过滤成30",
  保留全库 `clips_metadata`/`clip_durations`,传 `clip_paths={}` 给 `_run_variant_loop`(它每变体重建),传 `db_first=True`。
- 旗标关 → 现有路径**逐字节不变**。

### 7. `promo/core/render/remotion_renderer.py`
- **不改**。桥池继续扫 `clip_paths` 找未用片(:382-441);因为现在下载 `assigned ∪ reserve`,未用的那部分**就是 reserve**。

## 旗标 / rollout
- `PROMO_DB_FIRST_ASSIGNMENT`(默认关=今天)。**DB-first 隐含 global_assignment=True**(全库的价值在global才最大;greedy全库仍会strand)。
- claim-1/2 在 packer 引擎里,由 `PROMO_GLOBAL_ASSIGNMENT` 管,两条路径都受益,**独立测**。
- parity:旗标关 → sidecar 与当前 main 逐字节相同(回归锁)。

## 测试用例(具体断言)
- 单元 `select_bridge_reserve`:确定性 count/选择(最低usage→最长duration)。
- 单元 claim-1:存在"等余弦但更新鲜"的片时,pick 切换到新鲜那条。
- 单元 claim-2:某 beat 无可覆盖未用片 → **raise**,不出片。
- 集成(真门):假 backend `ready_assets_for_retrieval` 返回 >35 片(带embedding+视觉+窗口)+ `fetch_candidate_clips` 记录被请求的 asset_id。断言:
  (a) 指派**考虑了全库**(相关性排名 >35 的片能被指派);
  (b) `fetch_candidate_clips` 只被 `assigned ∪ reserve` 调用(不是②的30);
  (c) 桥池非空。
- parity:旗标关 = 当前 main sidecar 逐字节相同。
- 渲染验门:真POI跑通,`pool_exhaustion_hard_fails==0`,sidecar 里 `download_asset_ids ⊆ assigned ∪ reserve` 且含一个 rank>30 的片(证明30上限拿掉了)。

## 风险 + 缓解(两个设计都点的)
- 🔴 **桥接 reserve 太小** → `FreezeWouldOccurError`(以前下30碰巧有~15富余当桥)。
  缓解:reserve 保守 `max(8, 0.5×beats)` + env 可调 + 渲染验盯 `pool_exhaustion_hard_fails`。
- 🔴 **claim-2 把"静默用略短片"变硬报错** → 暴露以前被掩盖的真覆盖缺口。
  缓解:flag-gated + 第一批 armed 盯住,把它当"以前就有、现在被照出来的"问题处理。
- 🟡 **全库 `fetch_used_windows`**:现在读全库窗口(非~30)→ 确认是单次 `.in_()` 批读(usage_windows.py 是)、非 N+1;大POI上测延迟。
- 🟡 **A-minimal 的重复下载**:同片被多变体用 → 下多次(有界)。本期接受;后续可加 asset_id 级缓存(= B 的并集下载,留作 future)。

## 范围边界
- **本期做**:DB-first reorder(A-minimal)+ reserve + claim-1/2 + 旗标 + 测试 + 渲染验。
- **本期不做(future)**:删 ②(等 DB-first 转默认后)/ B 的并集下载与 TTS 抽出(egress 优化)/ 删 `_filter_candidate_metadata`。

## ⚠️ codex 审核补充(开工前必补的硬约束)
> 主体方案对(A-minimal 拍板),但原 PLAN 偏渲染正确性,漏了【生产证据链 + 批处理恢复】。开工单前必须把这4条写死。

1. **manifest / usage writeback 回传(最大遗漏)** —— A-minimal 在 `variant_loop` 里【按变体】下载,
   则 `pipeline.py` 末尾建 `run_manifest` / usage writeback 时,run级 `clip_paths`/`shared_assets_for_manifest` 可能是空或非最终集。
   **硬约束**:每个变体下载后的 `materialized clip_paths + shared_assets` **必须回传/累积到 `full_pipeline`**,
   用于 `run_manifest` 和 usage writeback。否则片能渲,但 `usage_events`/`asset_snapshot`/`release_candidate` 证据链丢 asset_id = 生产事故。
   验收:DB-first 跑出的 manifest 里 asset_id 集合 == assigned ∪ reserve(下载集),usage_events 行数/asset 对得上。

2. **near-dup 语义必须明确(DB-first 停用②后,它是唯一防线)** —— DB-first gate 掉 ②(上游去近重复那层),
   则 global 的近重复惩罚成为**唯一**视觉去重。当前 global 只看**相邻 beat**(claim-3),会漏"隔一个镜头"的视觉重复。
   **硬约束**:DB-first arm 前,把 global near-dup 惩罚**恢复成"跟所有已选片比"(greedy 的 all-prior 语义)**,
   否则等于把刚 armed 的视觉去重削回去。且**不再叫"global optimal"**(加惩罚后是启发式)。

3. **run_batch / receipt / resume 进计划** —— 生产走 batch runner,`PROMO_DB_FIRST_ASSIGNMENT` 不能只飘在 env。
   **硬约束**:把 DB-first 状态**记进 RUN_RECEIPT / manifest**,`resume` 时按**同一策略**恢复(同 `--download-diversity` 的 resume-safe 做法)。
   否则一次 run 可能前半段旧策略、后半段新策略。

4. **"Packer 不动"措辞已纠**(见顶部)——准确说法:不重写引擎,只补成本项 + 硬校验。

5. **reserve 验收**(再强调):`pool_exhaustion_hard_fails == 0` 是渲染验的硬门;桥池失败 = reserve 给少了。

## 工作量
**Medium**(A-minimal)。~6-9 处编辑,跨 4 文件 + 测试;无新子系统(DB读/下载器/渲染桥都已存在)。
(B 的 efficient fork = Large,差额全在"TTS前移";本期不取。)
