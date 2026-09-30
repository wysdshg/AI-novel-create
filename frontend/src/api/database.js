import http from './http'

// 模块1：分作品资料库（需求 1、4、10）。所有路径含 projectId。
// 四个子资源：characters / skills / relations / factions，CRUD 形态一致。
function crud(prefix) {
  return {
    list: (projectId, params) => http.get(`/projects/${projectId}/${prefix}`, { params }),
    get: (projectId, id) => http.get(`/projects/${projectId}/${prefix}/${id}`),
    create: (projectId, data) => http.post(`/projects/${projectId}/${prefix}`, data),
    update: (projectId, id, data) => http.put(`/projects/${projectId}/${prefix}/${id}`, data),
    remove: (projectId, id) => http.delete(`/projects/${projectId}/${prefix}/${id}`),
  }
}

export const characterApi = {
  ...crud('characters'),
  // —— S3 角色修订（2026-09-17）：版本历史 / AI 改动审批 / 回滚 ——
  listRevisions: (projectId, cid) =>
    http.get(`/projects/${projectId}/characters/${cid}/revisions`),
  proposeRevision: (projectId, cid, data) =>
    http.post(`/projects/${projectId}/characters/${cid}/revisions`, data),
  approveRevision: (projectId, cid, rid) =>
    http.post(`/projects/${projectId}/characters/${cid}/revisions/${rid}/approve`),
  rejectRevision: (projectId, cid, rid) =>
    http.post(`/projects/${projectId}/characters/${cid}/revisions/${rid}/reject`),
  rollbackRevision: (projectId, cid, rid) =>
    http.post(`/projects/${projectId}/characters/${cid}/revisions/${rid}/rollback`),
}
export const skillApi = crud('skills')
export const relationApi = crud('relations')
export const factionApi = crud('factions')
export const itemApi = crud('items')   // 2026-09-21：A5 批次③（物品库，表在 A1 已建、AI 抽取已落库）
export const locationApi = {
  ...crud('locations'),
  geoRelations: (projectId, id) => http.get(`/projects/${projectId}/locations/${id}/geo-relations`),
}

// 设定强制校验（需求 4）——已下架：后端恒返回空 issues 属「假绿灯」，
// 现改为显式 501（见 routers/database.py:validate_settings）。前端无任何调用方，
// 故不再保留封装，避免日后有人误用又踩回假绿灯。
// 注：生成链路里的 AI 味检测走 SSE `validate` 事件，与这里无关，仍然可用。

// 自然语言指令自动入库（需求 10）
export const commandApi = {
  run: (projectId, data) => http.post(`/projects/${projectId}/command`, data),
}
