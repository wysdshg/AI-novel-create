# 核心写作闭环（M1）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-10（test_full_chain.py e2e 全链路 3 轮 + 质量基线 8/8 全绿）

## 为什么做

作者要的是"能一直写下去"：AI 写下一章时必须记得前文、接住伏笔，否则每章都要人工喂设定。

## 解决什么

一条从商谈到落库的完整链路：剧情商讨 → 流式生成 → 安全网把关 → 写后自动沉淀 → 下一章自动带上前情/伏笔。篇规划批量生成也复用这条链的生成端点。

## 怎么实现

- 商讨两阶段按需加载（先报资料清单，加载后再答，常识问题短路）
- 生成统一走 `generateChapterStream` 单端点，SSE 流式
- 写后摄取 `ingestion.py` 抽章级记忆/伏笔动作，60s 内落库
- 上下文装配 `builder.py`：记忆+设定+伏笔+Hybrid 检索+GraphRAG 实体关系图

## 怎么扩展

- 加注入块改 `builder.py`（有预算裁剪，P_CRITICAL 永不裁）
- 增强能力失败必须降级不阻断主流程（设计铁律 15）
- 摄取各阶段有开关（`memory.extract_enabled` 等）

## 代码位置

- backend/app/core/context/builder.py（上下文装配）
- backend/app/services/ingestion.py（写后沉淀）
- backend/app/routers/chapter.py（生成端点）

## 关联

- 子节点 W0~W9；上游 M7（篇规划批量生成复用本端点）；下游 M6（留档/计量/反馈）
