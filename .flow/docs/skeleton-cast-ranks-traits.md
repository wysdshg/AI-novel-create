# 骨架角色槽位补全（F8·待拍板）

> 状态: pending_decision | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: —（2026-09-20 八问核查实测确认，等用户拍板重跑成本）

## 为什么做（问题是什么）

75 条 active 骨架的 **361 个 cast 槽位全缺 ranks/traits**（旧 164 条有 506/498）。

## 争议点/后果链

- archetype_text() 少「位阶：」「性格：」两段 → char_archetype 867 块语义无性格
- → plans.py 建卡「功能位参考」返回空
- → PlanView.fillFromArchetype() 的 personality / background 自动起草**双双失效**
- 根因：label_skeleton_casts.py 的 prompt 与 validate() 压根没要求这两个字段

## 候选方案

改 prompt + validate 加必填校验 → 重跑 75 条（约 75 次 LLM 调用）→ 回填三路向量。要拍板的是**重跑时机与成本**（可等 F5 飘标根治一起跑，省一轮）。

## 怎么验证

重跑后抽查：ranks/traits 非空率 100% → 建卡「功能位参考」返回非空 → 人设自动起草出内容。

## 代码位置

- scripts/label_skeleton_casts.py；backend/app/services/plot_template_crud.py（archetype_text）

## 关联

- 上游 F3（入库时发现）；下游 M7/P3（人设起草）、三路向量回填
