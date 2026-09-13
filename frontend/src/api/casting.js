// 角色选角 API 封装（Phase 7.3 后端 + 7.3.5 回归材料） —— 后端 /api/v1
// 选角结果唯一真相源是 plan_castings 表；手改标 manual，重算不覆盖。
// 分数为显式余弦（0~1），后端阈值 MIN_SCORE=0.58，低于阈值的槽位不会自动匹配。
import http from './http'

export const castingApi = {
  // 读该篇选角结果（列表：slot/slot_desc/character_id/character_name/score/source/needs_reentry_note）
  list: (projectId, articleId) =>
    http.get(`/projects/${projectId}/articles/${articleId}/casting`),

  // 重算选角（向量匹配；manual 槽位不覆盖）
  recompute: (projectId, articleId) =>
    http.post(`/projects/${projectId}/articles/${articleId}/casting/recompute`),

  // 作者手改某槽位选角：{ slot, character_id }（character_id=null 清空）
  set: (projectId, articleId, slot, characterId) =>
    http.put(`/projects/${projectId}/articles/${articleId}/casting`,
             { slot, character_id: characterId }),

  // 派生刷新角色出场台账（last_seen / appearance_count，纯统计）
  refreshAppearances: (projectId) =>
    http.post(`/projects/${projectId}/characters/refresh-appearances`),

  // 回归理由材料包（伏笔 → 世界线 → 兜底三级优先）
  reentryMaterial: (projectId, characterId) =>
    http.get(`/projects/${projectId}/characters/${characterId}/reentry-material`),
}
