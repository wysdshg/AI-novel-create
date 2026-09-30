# 章节流式生成（W2）

> 状态: completed | 建立: 2026-09-30 | 更新: 2026-09-30 | 上次验证: 2026-09-10（e2e SSE 事件序列 start→context→refs→chunk×N→validate→saved→ingest→done 完整，3 轮；2500 字目标实测 2566~2805 字）

## 为什么做

一章两三千字，非流式要等一两分钟白屏；作者中途改主意也没法停。

## 解决什么

SSE 实时输出、可随时中断；篇规划批量、单章对话框、对话框内三个入口共用同一生成端点（行为一致，修一处全生效）。

## 怎么实现

- 统一 `generateChapterStream` 单端点；sse.js 统一 4 处手写 SSE 读取（状态校验+超时中止+GenerationStopped）
- 响应式补 `resp.ok`（此前 400/500 的 JSON 错误体被当 SSE 解析，错误被吞）
- sse_event() 收敛帧编码（ensure_ascii=False + \n\n 契约有护栏测试防回潮）

## 怎么扩展

- 新入口接生成：直接调同一端点，别自己开并行实现
- 模型选择走 model_crud.resolve_model()（绕过 active 校验的 bug 已修，别回退）

## 代码位置

- backend/app/routers/chapter.py；frontend/src/utils/sse.js、api/chapter.js

## 关联

- 上游 W1（商讨定剧情）/W8（上下文）；下游 W3（安全网）→ W5（落库）
