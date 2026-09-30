// 设定库 API 封装（全局共享） —— 后端 /api/v1/settings
// CRUD 模式参照 src/api/database.js 的 crud() 工厂，保留 list/create/update/remove 命名。
import http from './http'

function buildSearch(params = {}) {
  // 移除 undefined 以免发成 ?category=undefined
  const out = {}
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') out[k] = v
  }
  return out
}

export const settingApi = {
  list: (params = {}) => http.get('/settings', { params: buildSearch(params) }),
  get: (id) => http.get(`/settings/${id}`),
  create: (data) => http.post('/settings', data),
  update: (id, data) => http.put(`/settings/${id}`, data),
  remove: (id) => http.delete(`/settings/${id}`),
  // 便捷方法：创建小说时挑选模板
  templates: () => http.get('/settings', { params: { template_only: true } }),
}

// 设定模板 API（2026-09-26：按题材一套一套的单文档模板）—— 后端 /api/v1/setting-templates
export const settingTemplateApi = {
  list: (params = {}) => http.get('/setting-templates', { params: buildSearch(params) }),
  get: (id) => http.get(`/setting-templates/${id}`),
  create: (data) => http.post('/setting-templates', data),
  update: (id, data) => http.put(`/setting-templates/${id}`, data),
  remove: (id) => http.delete(`/setting-templates/${id}`),
}
