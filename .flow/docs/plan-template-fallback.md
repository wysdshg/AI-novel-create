# 无口述时模板检索兜底 + 显式选模板（F9·方案定稿待实施）

> 状态: planned | 建立: 2026-09-30 | 更新: 2026-10-03 | 上次验证: 2026-10-03（F95 骨架池恢复后 search 冒烟：命中全为 arc 骨架、beats=6；恢复脚本 scripts/restore_v3_skeletons_20261003.py）

## 为什么做（问题是什么）

1. 作者不写「剧情口述」（hint 空）时：q_list 为空 → 关键词兜底 → **0 命中** → 计划 origin='free'、每行 template_ref='无模板·自由设计'。模板库对这类作者完全不可见。
2. **2026-10-03 新发现**：10-02 f8_p3_swap 用 retire --status active 一刀切，把 100 条 v3 情节骨架误归档（用户意图只下角色模板）——篇规划模板参考池被清空，**有口述也检索不到骨架**（实测命中的全是 scale=character 角色模板，beats=0 注入不出节拍）。已由 F95 修复（100 条骨架 active + 三路向量 1738 块重建）。
3. 顺带查明 TemplateView 三个显示 bug：statusText 三元链兜底把 active 显示成"已归档"、来源读 source_stats.book_names 而 F8 数据键是 book、粒度 chip 无"角色模板"；且模板库页不按 scale 过滤导致 265 条角色模板混显（同表是设计，缺过滤是缺陷）。归 DEV-F9a 修复。

## 定稿方案（2026-10-03 用户拍板，四分支）

前端生成表单加「参考模板」可搜索下拉（默认"自动匹配"，列 active 骨架）：

| 场景 | 行为 |
|---|---|
| 有选+有口述 | 选中模板锁定注入 + 标注「作者指定」；可选点「AI 增强口述」（qwen3-8B 关思考）→ 增强口述**填进口述框作者可改** → 用增强口述再检索几个候选补充（选中模板保送候选集，不与候选打架） |
| 有选+无口述 | 选中模板锁定注入；可选点「AI 帮我写口述」（8B 依据模板+卷概要+前情生成）→ 填框可改 |
| 无选+有口述 | 现状不变：口述检索 top-4 骨架 |
| 无选+无口述 | 多路兜底检索（卷概要截 200 字 + 篇名 + 活跃角色前 8 名，search() 原生多路 RRF）→ 空/挂才随机抽 4~5 骨架并入规划 prompt 让 LLM 挑（**灵感触发器定位**，随机名单记 raw_ai） |

**关键取舍记录**：
- AI 增强/生成口述的产物**必须落在口述框可见可改**（不是隐藏管道）——同时治"口述简陋"痛点；
- 增强调用用 qwen3-8B 关思考（短输出任务，零成本快返回）；不预支大模型调用；
- 「作者指定」标注解决候选打架：作者选择的优先级最高；
- 随机 = 灵感触发器（用户定位），不追求可复现，但名单记账；
- 题材过滤暂不做（全池修仙系无区分度），**入库第一本异题材书时**给 genre_tags 打题材标签后一并上；
- 篇规划检索必须加 `scale='arc'` 过滤（DEV-F9a），角色模板留给建卡链（char_archetype 路）。

## 怎么验证

1. 不写口述生成计划 → origin='template'、template_ref 有真实骨架、命中的 beats 与卷概要相关；raw_ai 留兜底 query 账
2. 显式选模板生成 → 计划参考该骨架（模板名进 prompt/或 template_ref 指向）；「AI 增强口述」产物在口述框可见可改
3. force_free=True（自由规划开关）→ 仍完全跳过检索
4. 有口述 → 行为与现状一致（回归）
5. 检索故障（停网关）→ 随机降级触发且名单进 raw_ai，不阻断生成

## 代码位置

- backend/app/services/plan_crud.py（`_templates_for_plan` / `_plan_prompt` / `generate_plan`）
- backend/app/services/plot_template_crud.py（search 多路 query）
- frontend/src/views/PlanView.vue（生成表单 + 口述框）
- backend/app/routers/plans.py（generate 入参）

## 关联

- 前置 F95（骨架池修复，✅ 2026-10-03）；上游 F3（模板库）；下游 M7/P1（篇规划生成）
- 实施派单：DEV-F9a（scale 过滤 + TemplateView 显示修复）→ DEV-F9b（四分支主体）→ QA
