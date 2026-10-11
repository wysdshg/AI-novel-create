// 角色模板库 API 封装 —— 后端 /api/v1/character-templates（P-CHARSPLIT 拆表，2026-10-12）
//
// 独立门牌（用户拍板）：角色模板与剧情弧模板物理分查，门牌本身防错——
// 从本路由拿不到剧情弧，也无需再传 scale 参数。
// 数据形态与拆表前同构（structure.cast[0] 由后端从展开列合成），视图层处理零改动。
import http from './http'

function clean(params = {}) {
  const out = {}
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') out[k] = v
  }
  return out
}

export const charTemplateApi = {
  /** 列表：可按 status('active'|'draft'|'reviewed'|'archived') 过滤 */
  list: (params = {}) => http.get('/character-templates', { params: clean(params) }),

  get: (id) => http.get(`/character-templates/${id}`),

  /** 入参二选一：cast_slot（structure.cast[0] 同构）或整包 structure */
  create: (data) => http.post('/character-templates', data),

  /** 只改状态（B16 同款，卡片快审用） */
  review: (id, status) => http.post(`/character-templates/${id}/review`, { status }),

  update: (id, data) => http.put(`/character-templates/${id}`, data),

  remove: (id) => http.delete(`/character-templates/${id}`),
}

export default charTemplateApi
