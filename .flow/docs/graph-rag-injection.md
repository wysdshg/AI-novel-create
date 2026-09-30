# GraphRAG 注入协议（A3）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-30（pytest 全量单测复验 542/542，含本功能 test_graph_rag_a3.py 4 例：分块/权重排序确定性/死亡降级/名字解析）

## 为什么做

写章提到某角色时，AI 需要"这个人和谁什么关系、会什么技能、有什么东西"，靠人工喂不现实。

## 解决什么

按角色名自动查出关系网/技能/物品/势力/地点，组装成上下文块注入写作与商讨。

## 怎么实现

- resolve_seeds 按名字五表解析种子 → assemble 一跳 entity_relations，按 relation_types.weight 确定性排序 → 分块组装
- 死亡实体只注入「名字+关系+已死亡」，人物卡字段不出现
- 端点 POST /projects/{pid}/graph-rag/assemble；builder 两处调用点接入，开关 retrieval.graph_rag

## 怎么扩展

- 🔴 关系边表 0 条 → 5 个注入块恒空（A7 待拍板，见 relation-layer-decision.md）
- 加新注入块：在 assemble 里加分块，保持确定性排序

## 代码位置

- backend/app/services/graph_rag.py；backend/app/routers/graph_rag.py

## 关联

- 上游 A1/A2（表与双写）；下游 M1/W8（注入消费方）；阻塞 A7
