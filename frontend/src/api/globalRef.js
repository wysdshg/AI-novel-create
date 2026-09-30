/** 全局物品/技能库 API（/global-ref，E3 管理页用）。 */
import http from './http'

function clean(params) {
  const out = {}
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') out[k] = v
  }
  return out
}

export const globalRefApi = {
  meta: () => http.get('/global-ref/meta'),
  listItems: (params = {}) => http.get('/global-ref/items', { params: clean(params) }),
  listSkills: (params = {}) => http.get('/global-ref/skills', { params: clean(params) }),
  createItem: (data) => http.post('/global-ref/items', data),
  createSkill: (data) => http.post('/global-ref/skills', data),
  updateItem: (id, data) => http.put(`/global-ref/items/${id}`, data),
  updateSkill: (id, data) => http.put(`/global-ref/skills/${id}`, data),
}
