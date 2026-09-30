# 质量评估与观测（M6）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-16（计量 8 单测 + 真机 command/discussion 两路记账；Playwright 缓存卡片 3 组真机）

## 为什么做

"改了配置之后生成到底变好还是变坏"此前只能凭感觉；token 成本是黑箱；作者改稿的反馈信号被丢弃。

## 解决什么

- 生成自动留档版本（配置快照+正文+指标），可打分、可对比
- token 用量按模型/场景/日期统计，缓存命中拆分（真实/估算如实区分）
- 作者改正文自动捕获为反馈样本（avg_change_ratio 最该盯）

## 怎么实现

- Phase 4.1/4.2/4.3：chapter_variants / eval_records / llm_usage_logs / feedback_records 四张表
- 计量覆盖五类场景：章节生成、商讨、写后摄取（3 场景）、指令解析、离线管线（14 调用点）；旁路设计绝不影响主链路
- 前端 EvalView + ObservabilityView（含缓存命中率卡片，None 时整块隐藏）

## 怎么扩展

- 加新场景计量：走 usage_crud.record_usage，显式传 vendor/model
- 估算与真实必须区分（取不到 usage 返回 None，不硬造 0）

## 代码位置

- frontend/src/views/EvalView.vue、ObservabilityView.vue；backend/app/services/usage_crud.py

## 关联

- 上游 M1（所有场景的调用都汇到这里）；W5（版本留档在落库时发生）
