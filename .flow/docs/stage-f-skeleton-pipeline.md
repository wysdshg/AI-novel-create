# 阶段F·原子骨架管线（M3）

> 状态: in_progress | 建立: 2026-09-30 | 更新: 2026-10-04 | 上次验证: 2026-10-04（pytest 全量 681 passed；DATA01 清洗后复检 48063 文件 A/B/C 全归零 + verify 核账通过）

## 为什么做

旧模板是"整体向量聚类"出来的，无法回答"这一环还能怎么走"。权威设计在 docs/10。

## 解决什么

从七本书切出原子事件，跨书归并成 75 条通用剧情骨架；写新篇时按环节给走法参考。

## 怎么实现

- F1 归并判据定稿（动态 min 0.65/0.7/0.85，单测 17 例锁死）
- F2 七书 591 单弧 → 75 组骨架（cut_atoms / merge_arcs_crossbook）
- F3 骨架入模板库（前端可见），旧 164 条 draft 归档；三路向量回填完毕
- F4 组装演示 ✅ 10-03 关账（f4_assemble.py 零 LLM，4 张样张用户拍板，`outputs/f4_poc/`）
- F8 角色模板 ✅ 10-03 全线关账（265 条现役，fillFromArchetype 验收过）
- F9 四分支检索兜底 ✅ 10-03 全线关账（F95+F9a+F9b+F9c，详见 `.flow/docs/plan-template-fallback.md`）
- DATA01 素材体检与清洗 ✅ 10-04 关账（39295 动作：A 38568 改写 + B 294 + C 433 隔离，复检归零，详见 `.flow/docs/data-clean-narrative.md`）——**扩容前置债已清**
- 权威设计 docs/10（§11 九项拍板 + 进度表）

## 怎么扩展

- 剩余：F5 核心飘标根治（87.5% 飘标率）、F6 逆天邪神续跑放量、F7 扩章——**等用户定扩容题材方向**
- 下一站 P3：组装结果注入上下文实测生成一章（F4 样张素材现成）

## 代码位置

- scripts/cut_atoms_from_chapters.py、scripts/merge_arcs_crossbook.py、scripts/label_skeleton_casts.py

## 关联

- 下游 M7/P1（篇规划的模板检索用这批骨架）；F8 影响选角/人设起草
