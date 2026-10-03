# 阶段A·前端分批接入（A5）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-10-01 | 上次验证: 2026-10-01（A5 四批全部用户实拍核对验收；悬案结案：09-20 文档"1/4 批"过时）

## 为什么做

后端表和 GraphRAG 都好了，但前端还没有入口看/改这些新字段——"零入口病"是本项目反复踩的坑。

## 解决什么

分 4 批把阶段A的新能力接进前端：①角色库新字段 ②势力/地点新字段表单 ③物品/技能页 ④关系网图谱。

## 怎么实现

- ① 角色库 ✅：CharacterForm 增「身份地位/作用」两字段（09-21）
- ③ 物品/技能页：ItemListView 已挂路由 database-item（09-21）、SkillListView 挂 database-skill；**页面验收待确认**（文档 09-25 仍标"剩余③"，代码与文档有出入，以验收为准）
- GraphRAG 已接入上下文构建器（开关 retrieval.graph_rag，两处调用点）
- 每批独立验收；收尾必须确认可达入口

## 怎么扩展

- 剩余：②势力/地点新字段表单、④关系网图谱（entity_relations 有数据才有意义，先看 A7）

## 代码位置

- frontend/src/views/ItemListView.vue、SkillListView.vue、CharacterForm 组件

## 关联

- 上游 A1~A4；下游 A6（收口）；被 A7 阻塞④
