# 角色连续性 B 档（P3）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-30（pytest 全量单测复验 542/542，含 test_continuity.py 22 例；端到端效果待真实长篇检验）

## 为什么做

换篇/隔了很多章再出现的角色最容易"失忆"或凭空冒出来；新角色一下子涌进来也接不住。

## 解决什么

三件事：新角色引入有单子（作者确认才建卡）、隔篇回归有交接提醒、老角色回归有理由材料。

## 怎么实现

- 新角色引入链路：计划 new_chars 落 plan_chars pending 引入单（≤3/篇、必绑功能位但不自动猜绑定），作者确认建卡回链 character_id，重落只清 pending
- 篇间交接差集：定位上次写作位置取前 3 条 characters 并集为遗留名单，差集=遗留−(召回∪新角色∪选角)，**警告非报错**（防自指）
- 回归理由材料包：确定性预取三级 priority（伏笔未回收优先 → 缺席期世界线事件 → 纯新编兜底）
- _finalize_plan 统一生成尾部，deepcopy 断共享引用；级联删除三入口全接入

## 怎么扩展

- 单篇新角色限额：普通 8 / 舞台切换篇 12（设计铁律 6）
- 刻意隐藏身份允许代称但须标 name_known=false（铁律 5）

## 代码位置

- backend/app/services/plan_crud.py（plan_chars/carryover/reentry）；casting_crud.reentry_material

## 关联

- 上游 P1；下游 P4（连续性警告区/引入单面板）
