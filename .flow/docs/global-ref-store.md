# 全局条目库/尺度库四表（E2）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-30（pytest 全量单测复验 542/542，含 test_global_ref.py 8/8；09-25 e3_apply_gate 只读核账 46 active / 0 disabled）

## 为什么做

AI 造物品爱硬造怪词、每本书重复建"洗髓丹"条目；需要全库统一的"类型惯例词"和"怎么造新东西"的尺度。

## 解决什么

条目库（global_items/global_skills：名称+类别+一句话说明+别名）+ 尺度库（global_item_scales/global_skill_scales：类别/功能位/强度/代价/叙事位置）四张表一套 CRUD。

## 怎么实现

- global_ref_crud：幂等 add、normalize_lookup 别名归一（SQLite JSON ensure_ascii 坑 → active 行 Python 过滤）、inject_* 硬过滤、stats
- 种子灌库 46 词条（35 物品 + 11 技能）+ 6 尺度，brief 全自写 ≤50 字
- 口径 E1 已拍板（G1~G7）：「≥2 本即收」，扫描只作初筛、淘汰须人工复核（8 个不达标词人工复核后全部恢复）

## 怎么扩展

- 下游还没接：E3 抽取归一化（抽到惯例词指向条目）、E4 写作注入（尺度+候选）
- 侵权三条纪律是强制的：不存原文片段、独创专名不收录、描述一律重写

## 代码位置

- backend/app/services/global_ref_crud.py、backend/scripts/seed_global_ref.py

## 关联

- 图节点：E1（口径）→ E2（本功能）→ GE1 数据库节点（global_items/global_skills/global_item_scales/global_skill_scales 四表，GT1~GT4）
- 下游 E3/E4（还没接：抽取归一化、写作注入）、E5（前端 GlobalRefView）；M2/A4（抽取归一）
