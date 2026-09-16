// 伏笔 API 封装（2026-09-15，docs/08-B1） —— 后端 /api/v1
// 字段：{id, description, buried_chapter, scene, trigger_condition, enabled,
//        activated_chapter, related_ids[], status}
// status: pending（埋下未收）/ active（已暗示）/ done（已回收）
import http from './http'

export const foreshadowApi = {
  // 列表（可按 status 过滤；不传则全部）
  list: (projectId, params = {}) =>
    http.get(`/projects/${projectId}/foreshadows`, { params }),

  // 只看未回收（pending + active）—— 写作前「哪些线还悬着」用这个
  active: (projectId) =>
    http.get(`/projects/${projectId}/foreshadows/active`),

  // 人工补录伏笔（detect 端点已下架为 501，这是唯一的非 AI 录入路径）
  create: (projectId, data) =>
    http.post(`/projects/${projectId}/foreshadows`, data),

  // 改描述/场景/触发条件/启用状态
  update: (projectId, id, data) =>
    http.put(`/projects/${projectId}/foreshadows/${id}`, data),

  remove: (projectId, id) =>
    http.delete(`/projects/${projectId}/foreshadows/${id}`),

  // 标记回收 → status 置 done（activated_chapter 走 **query 参数**，不是 body）
  activate: (projectId, id, activatedChapter) =>
    http.post(`/projects/${projectId}/foreshadows/${id}/activate`,
      null,
      { params: activatedChapter != null ? { activated_chapter: activatedChapter } : {} }),
}
