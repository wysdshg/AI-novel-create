# 写后自动沉淀（W6）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-10（真机：生成完 60s 内章级记忆落库；伏笔回注 15 单测 + 真机闭环：第1章抽 2 条伏笔 → 第2章上下文出现【伏笔状态】块）

## 为什么做

写完一章如果靠人工记"这章发生了什么/埋了什么伏笔"，写到第三十章就记不动了。

## 解决什么

每章写完自动抽取并落库：章级记忆、篇章摘要、伏笔动作（走向建议已按作者 2026-09-13 拍板默认下线，代码保留可恢复）。

## 怎么实现

- ingestion.py 分阶段流水线（含 2.6 伏笔回注、2.7 关系/势力回注、3.5 向量索引钩子）
- 摄取分阶段开关：memory.extract_enabled 等；extract=False 不是跳过，走规则兜底（fallback_reason 区分"主动省调用"与"LLM 失败"）
- 计量接入 ingest_extract/aggregate/stage 三场景（旁路设计绝不影响主链路）

## 怎么扩展

- 加新抽取键：EXTRACT_SYSTEM + _JSON_KEYS + normalize_extract + fallback 四处同步
- max_tokens 预算注意：加键后 2000 会截断 JSON → 整章白抽（已修为 2600~4096）

## 代码位置

- backend/app/services/ingestion.py；foreshadow_crud.sync_from_actions；relation_crud.sync_from_extract

## 关联

- 上游 W5；下游 W7（伏笔生命周期）、W8（下一章上下文）
