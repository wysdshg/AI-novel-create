// 情节模板库 API 封装 —— 后端 /api/v1/plot-templates（Phase 7.1）
//
// 与 setting.js 保持同风格：list/get/create/update/remove + 特有的 search。
//
// 为什么 search 是 POST：模板检索是**语义检索**（bge-m3 向量 + 多查询 RRF 融合），
// 查询串可能很长（"既像学院大比又像秘境寻宝"）且可传多个查询，放 body 比 query 干净；
// 且向量不可用时后端会自动回退关键词匹配（返回 mode=fallback_tags），前端无需感知。
import http from './http'

// 剔除 undefined/null/''，避免发成 ?status=undefined
function clean(params = {}) {
  const out = {}
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') out[k] = v
  }
  return out
}

export const plotTemplateApi = {
  /** 列表：可按 scale('arc'|'segment') / status('draft'|'reviewed'|'archived') 过滤 */
  list: (params = {}) => http.get('/plot-templates', { params: clean(params) }),

  get: (id) => http.get(`/plot-templates/${id}`),

  create: (data) => http.post('/plot-templates', data),

  update: (id, data) => http.put(`/plot-templates/${id}`, data),

  remove: (id) => http.delete(`/plot-templates/${id}`),

  /**
   * 检索主入口。返回 { mode, queries, items }，item 中额外的 matched_beats 是命中的节拍。
   * @param {string} query   一句模糊口述
   * @param {string[]} queries 显式多查询（走 multi-query RRF）
   * @param {string} scale   可选过滤
   * @param {string[]} tags  可选标签过滤
   * @param {number} topK
   */
  search: ({ query = '', queries = null, scale = null, tags = null, topK = 8 } = {}) =>
    http.post('/plot-templates/search', {
      query,
      queries,
      scale,
      tags,
      top_k: topK,
    }),
}

export default plotTemplateApi
