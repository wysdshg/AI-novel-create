# 阶段E·全局条目库/尺度库（M4）

> 状态: in_progress | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-30（pytest 全量单测复验 542/542 通过，含 test_global_ref.py 8/8；09-25 e3_apply_gate 只读核账 46 active / 0 disabled）

## 为什么做

AI 造物品/技能爱硬造怪词（"伐骨丹""青云术"），且每本书重复建"洗髓丹"条目。

## 解决什么

- 条目库：装类型惯例词（筑基丹/灵石/御剑术），让 AI 优先用通用词、抽取可归一
- 尺度库：装类别级设计尺度，让 AI 会造新东西而不是千篇一律

## 怎么实现

- E1 口径 2026-09-25 拍板（G1~G7，"≥2 本即收"+人工复核；教训：扫描是初筛不是裁决，见 docs/09 §7.5）
- E2 四表 + global_ref_crud（别名归一/硬过滤/统计），种子 46 词条 + 6 尺度，说明全自写 ≤50 字
- E5 前端 GlobalRefView 已挂路由 global-ref（09-26，侧栏入口待确认）
- 侵权三条纪律强制：只存名词+一句话说明、不收录独创专名、描述重写不保留原文

## 怎么扩展

- 剩 E3（抽取归一化）、E4（写作注入）——都是 to_plan，等排期

## 代码位置

- backend/app/services/global_ref_crud.py、backend/app/routers/global_ref.py、frontend/src/views/GlobalRefView.vue

## 关联

- 下游 M2/A4（抽取归一要用条目库）；M1/W8（写作注入）
