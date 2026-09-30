# 阶段E·全局条目库/尺度库（M4）

> 状态: in_progress | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-30（pytest 全量 547/547 通过，含 E4 块级单测 3 例 + E3 单测 2 例；真库 build_chapter_messages 实测 E4 注入块可达）

## 为什么做

AI 造物品/技能爱硬造怪词（"伐骨丹""青云术"），且每本书重复建"洗髓丹"条目。

## 解决什么

- 条目库：装类型惯例词（筑基丹/灵石/御剑术），让 AI 优先用通用词、抽取可归一
- 尺度库：装类别级设计尺度，让 AI 会造新东西而不是千篇一律

## 怎么实现

- E1 口径 2026-09-25 拍板（G1~G7，"≥2 本即收"+人工复核；教训：扫描是初筛不是裁决，见 docs/09 §7.5）
- E2 四表 + global_ref_crud（别名归一/硬过滤/统计），种子 46 词条 + 6 尺度，说明全自写 ≤50 字
  - 09-30 查库更正：库内实有物品 610（丹药173/材料146/武器102/法器89/灵石货币31/符箓28/防具20/信物11/境界10）+ 技能 237（阵法163/攻击技29/功法25…），全 active——批量扩充未回写过文档
- E3 前置已通：09-30 实测 normalize_lookup("洗髓丹"/"灵石"/"御剑术") 全命中，仅差在 ingestion.py 落库前接线
- **E3 ✅（09-30）**：`item_crud / skill_crud` 的 `sync_from_extract` 落库前接 `normalize_lookup`——命中惯例词（主名/别名、只认 active）不建本地条目，统计增 `normalized`；disabled 条目照常建。单测 +2，全量 544 passed。代码位置：`backend/app/services/item_crud.py`（sync_from_extract）、`skill_crud.py` 同构
- **E4 ✅（09-30）**：`global_ref_crud.build_injection_block`（候选按类别只给名字每类≤8 + 类别尺度 + 使用要求；题材家族映射 玄幻/武侠→仙侠池）接入 `builder.py` system 拼装；回滚开关 `app_configs globalref.inject_on_generate` 默认开。块级单测 3 例，全量 547 passed；真库实测注入块可达（约 +1500 字）。**生成效果属主观体验，待作者真机写章确认**
- E5 前端 GlobalRefView 已挂路由 global-ref（09-26，侧栏入口待确认）
- 侵权三条纪律强制：只存名词+一句话说明、不收录独创专名、描述重写不保留原文

## 怎么扩展

- E1~E4 全 ✅，只剩 E5 前端管理页侧栏验收（in_progress）
- E3/E4 都有回滚口径：E3 无开关（纯落库前过滤，影响仅限新建条目）；E4 关 `globalref.inject_on_generate` 即恢复原提示词
- 候选名单每类 ≤8 是体积权衡；条目库扩到千级后可考虑按本章要点挑相关类别（需在 builder 拿到 hint 后再取，当前为静态块）

## 代码位置

- backend/app/services/global_ref_crud.py、backend/app/routers/global_ref.py、frontend/src/views/GlobalRefView.vue

## 关联

- 下游 M2/A4（抽取归一要用条目库）；M1/W8（写作注入）
