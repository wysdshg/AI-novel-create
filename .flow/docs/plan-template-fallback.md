# 无口述时模板检索兜底（F9·待拍板）

> 状态: pending_decision | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: —（2026-09-20 实测确认：search("")→0 命中，search("修仙 灵石")→vector 命中 4，等用户拍板）

## 为什么做（问题是什么）

作者不写「剧情口述」（hint 空）时：q_list 为空 → 关键词兜底 → **0 命中** → 计划 origin='free'、每行 template_ref='无模板·自由设计'。**75 条模板对这类作者完全不可见**。

## 争议点

search() 本身正常（有 query 就能命中），问题只在**查询串的来源**。要拍板的是兜底查询用什么：

- 方案甲：篇名/卷概要当 query
- 方案乙：篇内角色列表当 query
- 方案丙：两者拼接（信息最全，可能引入噪声）

## 怎么验证

造一个不写口述的计划 → origin 不再是 'free' → template_ref 有真实模板 → 命中节拍与篇名相关。

## 代码位置

- backend/app/services/plan_crud.py（q_list 组装处）；plot_template_crud.search

## 关联

- 上游 F3（模板库）；下游 M7/P1（篇规划生成的模板检索）
