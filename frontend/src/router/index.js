import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '@/layout/MainLayout.vue'
import ChatView from '@/views/ChatView.vue'
import OverviewView from '@/views/OverviewView.vue'
import DatabaseView from '@/views/DatabaseView.vue'
import FactionView from '@/views/FactionView.vue'
import LocationView from '@/views/LocationView.vue'
import WorldMapView from '@/views/WorldMapView.vue'
import CharacterRelationView from '@/views/CharacterRelationView.vue'
import ForeshadowView from '@/views/ForeshadowView.vue'
import ChapterListView from '@/views/ChapterListView.vue'
import TemplateView from '@/views/TemplateView.vue'
import ModelConfigView from '@/views/ModelConfigView.vue'
import ReferenceView from '@/views/ReferenceView.vue'
import GlobalReferenceView from '@/views/GlobalReferenceView.vue'

// 本轮（用户大任务 1）新增的 3 个顶层视图
import SettingView from '@/views/SettingView.vue'         // 全局设定库
import CustomSkillView from '@/views/CustomSkillView.vue' // 全局写作 SKILL
import WorkflowView from '@/views/WorkflowView.vue'       // 全局工作流
import WorkflowEditorView from '@/views/WorkflowEditorView.vue' // 工作流画布编辑器
import SkillListView from '@/views/SkillListView.vue'     // 数据库·技能库（按小说隔离的角色技能表）
import EvalView from '@/views/EvalView.vue'               // Phase 4.1 生成评估（版本留档 / 打分 / 对比）
import ObservabilityView from '@/views/ObservabilityView.vue'  // Phase 4.2/4.3 观测（用量计量 + 反馈回流）
import PlanView from '@/views/PlanView.vue'                    // Phase 7.5 篇规划（计划/选角/引入单/连续性）

// =========================================================================
// 顶部 tab 系统（参考用户大任务截图）
//   一级 tab：「对话 / 概览 / 工作流 / 数据库 / 参考文档」5 个
//   二级 tab（数据库 激活时显示）：「角色库 / 关系网 / 技能库 / 势力库 / 世界地图」5 个
//   注：「参考资料 / 设定库 / 写作 SKILL」仅出现在左侧边栏，进入对应页面时隐藏顶部 tab（meta.hideTopNav）。
//
//   meta.tab 类型：
//     'parent'       一级 tab
//     'sub'          二级 tab（必填 meta.tabParent 对应一级名）
//     undefined / false  不显示在 tab 栏（如 hideTab）
//
//   后续新增模块只需添加一条路由 + 正确 meta，MainLayout 自动渲染——
//   这是 §可扩展性 设计的一部分。
// =========================================================================
const routes = [
  { path: '/', redirect: '/workspace/chat' },
  {
    path: '/workspace',
    component: MainLayout,
    redirect: '/workspace/chat',
    children: [
      // —— 一级 tab ——
      { path: 'chat',     name: 'chat',     component: ChatView,     meta: { title: '对话',     tab: 'parent' } },
      { path: 'overview', name: 'overview', component: OverviewView, meta: { title: '概览',     tab: 'parent' } },
      { path: 'workflow', name: 'workflow', component: WorkflowView, meta: { title: '工作流',   tab: 'parent' } },
      { path: 'workflow-editor/:id', name: 'workflow-editor', component: WorkflowEditorView, meta: { title: '工作流编辑', hideTopNav: true } },
      { path: 'database', name: 'database', redirect: { name: 'database-character' }, meta: { title: '数据库', tab: 'parent' } },
      { path: 'reference', name: 'reference', component: ReferenceView, meta: { title: '参考文档', tab: 'parent' } },
      // 「参考资料」只放在左侧边栏，不出现在顶部 tab（见 #2/#7）
      { path: 'global-reference', name: 'global-reference', component: GlobalReferenceView, meta: { title: '参考资料', hideTopNav: true } },

      // —— 数据库 子 tab ——（order 决定左右顺序，与用户图一致）
      { path: 'database-character', name: 'database-character', component: DatabaseView,
        meta: { title: '角色库',   tab: 'sub', tabParent: 'database', order: 1 } },
      { path: 'character-relation', name: 'character-relation', component: CharacterRelationView,
        meta: { title: '关系网',   tab: 'sub', tabParent: 'database', order: 2 } },
      { path: 'database-skill',    name: 'database-skill',     component: SkillListView,
        meta: { title: '技能库',   tab: 'sub', tabParent: 'database', order: 3 } },
      { path: 'faction',           name: 'faction',            component: FactionView,
        meta: { title: '势力库',   tab: 'sub', tabParent: 'database', order: 4 } },
      { path: 'world-map',         name: 'world-map',          component: WorldMapView,
        meta: { title: '世界地图', tab: 'sub', tabParent: 'database', order: 5 } },

      // —— 其他视图（保留可达，但不展示在顶部 tab，便于侧栏 / 深链）——
      { path: 'location',    name: 'location',    component: LocationView,    meta: { title: '地点库',     hideTab: true } },
      // 「配置对话」(config-chat) 原页面已于 2026-09-10 删除（Phase 3.1 死代码清理）：
      // 该页 316 行但侧栏无入口、长期不可达；其核心能力（/角色 /地点 等斜杠指令 +
      // 自然语言入库）已由 ChatInput.vue 承载（同样调 commandApi + parseSlashCommands），
      // 故删除的是不可达的重复 UI，能力未丢。历史版本见 git bd74768。
      { path: 'foreshadow',  name: 'foreshadow',  component: ForeshadowView,  meta: { title: '线索 / 伏笔', hideTab: true } },
      // Phase 4.1 生成评估：章节的生成版本留档 / 打分 / 对比。
      // hideTopNav → 不占顶部 tab，靠左侧栏「生成评估」入口进入（入口见 layout/MainLayout.vue）
      { path: 'eval', name: 'eval', component: EvalView, meta: { title: '生成评估', hideTopNav: true } },
      // Phase 4.2/4.3 观测：用量计量 + 反馈回流（同样靠左侧栏入口进入）
      { path: 'observability', name: 'observability', component: ObservabilityView,
        meta: { title: '观测', hideTopNav: true } },
      // Phase 7.5 篇规划：计划表格 + 选角 + 引入单确认 + 交接警告。
      // hideTab → 不占顶部 tab，靠左侧栏「篇规划」入口进入（入口见 layout/MainLayout.vue，
      // ⚠️ 零入口病两连教训：路由必须配可达入口）
      { path: 'plan', name: 'plan', component: PlanView,
        meta: { title: '篇规划', hideTab: true } },
      // 走向推荐（direction）已随模块删除：ingestion 每章推的走向卡片走商讨面板，
      // 不设独立表/页面（2026-09-10，见 docs/03 §1）。
      { path: 'chapters',    name: 'chapters',    component: ChapterListView, meta: { title: '章节列表',   hideTab: true } },
      { path: 'template',    name: 'template',    component: TemplateView,    meta: { title: '套路模板',   hideTab: true } },
      // 设定库 / 写作 SKILL 与左侧边栏入口同级，进入后不显示顶部 tab（见 #7）
      { path: 'setting',      name: 'setting',      component: SettingView,      meta: { title: '设定库',     hideTopNav: true } },
      { path: 'custom-skill', name: 'custom-skill', component: CustomSkillView, meta: { title: '写作 SKILL', hideTopNav: true } },
      // 模型配置从左侧栏进入，不在顶部标签中展示
      { path: 'model-config', name: 'model-config', component: ModelConfigView, meta: { title: '模型配置', hideTab: true } },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
