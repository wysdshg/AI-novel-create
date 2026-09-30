# 关系层数据落地（A7·已落地）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-10-01 | 验证: 2026-10-01 端到端真跑通过

## 为什么做（问题是什么）

GraphRAG 代码链路正常，但【关系网】【相关技能】【相关物品】【相关势力】【相关地点】5 个注入块因边表无归属边而恒空（角色-角色关系边已随旧表迁移落库，缺的是 skill/item 归属边——AI 抽的实体全部 owner_id=None）。

## 拍板口径（2026-10-01 用户拍板）

「每章写完 AI 抽实体时**顺带判定关系连边**」——不是机械同章共现，AI 直接判定"谁持有/谁掌握"。

## 落地实现（2026-10-01）

1. 抽取 prompt：`new_entities` 的 item/skill 加 `owner` 字段（须出自 characters，无主就空）
2. `normalize_extract` 保留 owner 字段（此前会被洗掉）
3. `entity_relation_crud.link_owner_from_extract`：双防线连边（① owner 须在本章 characters 名单；② 名字须精确解析到库内角色），新建与重名两分支都连（重摄取可为存量孤儿补边），幂等由 upsert_edge 保证
4. `item_crud/skill_crud.sync_from_extract` 接入（边类型：持有物品/掌握技能），stats 加 `linked` 计数
5. `ingestion` 2.7 节调用点传 `chapter_characters` + `chapter_no`

## 怎么验证（已验证 ✅）

- 单测 +4（连边成功/重名补边/双防线拒边/normalize 保留 owner），全量 **551 passed**
- 端到端真跑：重摄取「原神启动」第 1 章 → `items linked=1` → GraphRAG【相关物品】块非空（灰石：陈峰从药渣中捡到的五块温热石头…）、关系网新增 `陈峰—[持有物品]→灰石`、注入 278→383 字符

## 存量回填策略

重摄取幂等自动补边（重名跳过分支也连边），随写作自然积累；无需专门跑批。
库内 20 条已删项目孤儿边无害（assemble 按 project_id 过滤），暂不清理。

## 代码位置

- backend/app/services/entity_relation_crud.py（link_owner_from_extract）
- backend/app/services/item_crud.py / skill_crud.py（sync_from_extract 连边）
- backend/app/services/ingestion.py（prompt + normalize + 调用点）

## 关联

- A3（GraphRAG 读侧已通）、A5④（关系网图谱有数据了）、A6（端到端验收）
