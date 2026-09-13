// 篇规划 API 封装（Phase 7.2 后端 + 7.3.5 引入单） —— 后端 /api/v1
// 计划行字段：{no, beat, summary, new_chars[], recall_chars[], target_words, hook, template_ref}
import http from './http'

export const planApi = {
  // 生成本篇章计划（hint=作者口述最高优先级；n_chapters 2~40；force_free 跳过模板检索）
  generate: (projectId, articleId, data) =>
    http.post(`/projects/${projectId}/articles/${articleId}/plan/generate`, data),

  // 读当前计划（没有返回 null；含 carryover / reentry_materials 连续性数据）
  get: (projectId, articleId) =>
    http.get(`/projects/${projectId}/articles/${articleId}/plan`),

  // 保存行级编辑（整表提交）
  save: (projectId, articleId, lines, notes) =>
    http.put(`/projects/${projectId}/articles/${articleId}/plan`, { lines, notes }),

  // AI 只改一行（其余行原样）
  refineLine: (projectId, articleId, lineNo, instruction) =>
    http.post(`/projects/${projectId}/articles/${articleId}/plan/refine-line`,
              { line_no: lineNo, instruction }),

  // 拍板确认（draft → confirmed）
  confirm: (projectId, articleId) =>
    http.post(`/projects/${projectId}/articles/${articleId}/plan/confirm`),

  // —— 7.3.5 新角色引入单 ——
  // 引入单列表（含 confirmed/dismissed —— 作者的决策历史要可见）
  listPlannedChars: (projectId, articleId) =>
    http.get(`/projects/${projectId}/articles/${articleId}/planned-chars`),

  // 调整引入单：{ slot?, slot_desc?, first_appearance?, status? }（pending|dismissed）
  updatePlannedChar: (projectId, articleId, pcId, data) =>
    http.put(`/projects/${projectId}/articles/${articleId}/planned-chars/${pcId}`, data),

  // 确认建卡进角色库：{ role_type?, personality?, background?, talent?, current_level?, brief? }
  confirmPlannedChar: (projectId, articleId, pcId, data) =>
    http.post(`/projects/${projectId}/articles/${articleId}/planned-chars/${pcId}/confirm`, data),
}
