# PGC 消费交接 — 视觉嵌入(去重 / 多样性选片)

**给 PGC(Leo86868/Promo-Pipeline)reviewer。** 视觉嵌入回填已完成 + 验过,数据可用了。这份是"怎么消费"的完整交接。**AIGC 只造向量;选片/阈值归 PGC(在你们 repo 做)。**

## 现在有什么(live + 完整 + 验过)
```
表 poi_asset_visual_embeddings —— DINOv2-base 均值视觉向量,一片一行
全量回填:14,944 个 1080p clip 全做完(0 缺 / 0 脏 / 0 分裂;随机10个recompute cosine=1.0验过)
向量 = 768维 · L2归一 · 【生向量(未白化)】 · recipe_version='v1-visual-mean'
```

## 怎么读(只走契约视图,别直查底表)
```
读 poi_asset_valid_clips 视图(契约唯一读面),它已加了两列:
  visual_embedding_status  —— gate 在这:='ready' 才有视觉向量
  visual_recipe_version    —— 'v1-visual-mean'
向量本体在 poi_asset_visual_embeddings,按 asset_id join 取 embedding_vector
🔴 分模态就绪:别把文字的 embedding_status='ready' 当视觉就绪;视觉去重只认 visual_embedding_status='ready'
   回填窗口/新片可能 visual=pending → 这是正常中间态,你侧 fail-open(跳过视觉去重、别崩)
```

## 怎么算"像不像"
```
两条片的相似度 = 两个向量的 cosine(向量已L2归一,点积即cosine)
向量是【生的】→ 如要更准可在【你侧 query 时】做白化(mean-center against POI池);平台永不持久化白化值
  → 这样你能重调阈值/白化而不用 AIGC 重回填
```

## 两个用法 + 参考实现
```
① 去重/选多样:同一 POI 内,别把"太像"的片一起堆(max-min 选片把近重复挤掉)
② "给我选 N 个互不近似的片":max-min 跳着选,选中集任意两条相似度低
参考实现(直接抄逻辑):AIGC repo prototype/select_diverse.py(已验:Huatulco 158→选30含8不同泳池、最像0.53)
```

## "太像"阈值(你们拍,可后定)
```
0.92 = 只算"同一镜头"太像(保守)
0.85 = 连"同地点不同镜头"也算太像(更激进铺开)
建议先拿几个 POI 的样片肉眼核一遍再定线。这是消费侧审美,AIGC 不替你定。
```

## 边界 / 注意
- **per-frame 帧级向量 = 阶段二**(将来"精确删同一条素材"才需要;现在均值够"选多样")。要时跟 AIGC 提,从留存源片重抽即可。
- **"视觉相似≠功能类别"**:DINOv2 偶尔觉得"健身房≈带海景的房间"(都是落地玻璃)。纯视觉 max-min 若要保证"类别覆盖"(必有房间+必有健身房),在你侧选片层加类别约束。
- **新片覆盖**:今天 14,944 已覆盖;以后新 onboard 的片由 AIGC 的增量入口接上(进行中),你侧只管 gate visual_status=ready。

## 跨仓
PGC 在自己 repo 建消费(读向量 + max-min + 阈值);AIGC 拥有向量层。有缺口/要 per-frame/阈值疑问 → 走 CROSS-REPO issue。
