import http from './http'
import { readSseStream } from '@/utils/sse'

// 模块2/3：章节生成 + 剧情商讨（需求 2、3、6）
export const chapterApi = {
  list: (projectId, params) => http.get(`/projects/${projectId}/chapters`, { params }),
  get: (projectId, id) => http.get(`/projects/${projectId}/chapters/${id}`),
  create: (projectId, data) => http.post(`/projects/${projectId}/chapters`, data),
  update: (projectId, id, data) => http.put(`/projects/${projectId}/chapters/${id}`, data),
  remove: (projectId, id) => http.delete(`/projects/${projectId}/chapters/${id}`),
}

// 段落写法打磨（2026-09-21）：勾选段落 → 一次请求打包 + 各自 top-5 参考 → 魔搭开思考。
// 后台任务 + 轮询（E10：同步响应会被 vite 代理 300s 空闲上限掐掉）。
export const polishApi = {
  start: (projectId, chapterId, data) =>
    http.post(`/projects/${projectId}/chapters/${chapterId}/polish`, data),
  get: (projectId, chapterId, taskId) =>
    http.get(`/projects/${projectId}/chapters/${chapterId}/polish/${taskId}`),
}

// 流式单章生成（SSE，§3.3）。用 fetch 读取事件流，回调 onEvent。
// 支持传入 { signal: AbortSignal, modelId } 实现手动停止并指定模型；中止后抛 GenerationStopped 错误。
export async function generateChapterStream(projectId, body, onEvent, { signal, modelId } = {}) {
  if (modelId) body.model_id = modelId
  const resp = await fetch(`/api/v1/projects/${projectId}/chapters/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  // 状态校验 / 读流 / 超时中止语义统一在 readSseStream（Phase 3.3 抽公共实现）：
  // ⚠️ 不校验 HTTP 状态会把 400/500 的 JSON 错误体当 SSE 解析 → 前端只看到"完全没有输出"。
  // 典型场景：漏传 article_id 后端返回 400「生成章节必须指定所属篇」。
  await readSseStream(resp, onEvent, {
    signal,
    errorPrefix: '章节生成接口',
    stoppedMessage: '生成已停止',
  })
}
