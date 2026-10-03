# 阶段A·数据模型大更新（M2）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-10-01 | 上次验证: 2026-10-01（阶段A 全模块关闭：A6 脚本验收对账 15=15、A7 端到端真跑 linked=1/GraphRAG 块非空、A5 四批用户实拍核对）

## 为什么做

实体库原来只有角色/势力/地点，AI 认识不了"物品/技能"，也没有可查询的关系网，写作时带不出"谁和谁什么关系"。

## 解决什么

给 AI 补齐五类实体的认识与检索：物品、技能、实体关系边、等级标准。写章提到某角色时自动带出关系网/技能/物品/势力/地点。

## 怎么实现

- A1 建 4 张新表 + 老表加列，迁移脚本幂等、跨进程复核
- A2 写路径双写（新旧两套表同时写，读侧未切前不丢数据）
- A3 GraphRAG 注入协议（graph_rag.py，死亡实体降级）
- A4 AI 抽取扩到 item/skill，标 ai_generated 落库
- A5 前端分 4 批接入（进行中，见 stage-a-frontend.md）

## 怎么扩展

- 双写过渡期读侧仍读旧表；A6 收口确认后才可弃读老列
- 🔴 A7 未拍板：关系边表 0 条，5 个注入块恒空（见 relation-layer-decision.md）

## 代码位置

- backend/app/models/orm.py、backend/app/services/graph_rag.py、entity_relation_crud.py、item_crud.py

## 关联

- 子节点 A0~A7、T1~T4；下游 M1（注入）；阻塞项 A7
