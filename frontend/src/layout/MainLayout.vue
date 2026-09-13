<template>
  <el-container class="na-layout">
    <!-- 左侧：侧栏（侧边功能 + 小说 / 对话树） -->
    <el-aside width="248px" class="na-aside">
      <!--
        logo：纯原生 <a href> + @click 拦截走 SPA 推送，双保险。
        用 router-link 在某些 Edge 缓存/扩展场景下点击事件不触发；
        这里锚点 href 直跳 /workspace/chat 即便 router 没接管也是原生跳转。
      -->
      <a href="/workspace/chat" class="na-logo" title="返回首页（欢迎页）" @click.prevent="onLogoClick">
        网络小说助手
      </a>

      <!-- 侧栏上方：核心操作 + 全局入口 -->
      <div class="na-aside-actions">
        <el-button type="primary" class="na-new-novel" @click="onCreateNovel">
          <el-icon><Plus /></el-icon> 新建小说
        </el-button>
        <!-- 参考图 1：参考资料 / 设定库 / SKILL 三个全局入口 -->
        <el-button
          v-for="entry in globalEntries"
          :key="entry.name"
          class="na-entry-btn"
          :class="{ 'is-active': isEntryActive(entry.name) }"
          plain
          @click="goEntry(entry)"
        >
          <el-icon><component :is="entry.icon" /></el-icon>
          {{ entry.label }}
        </el-button>
      </div>

      <!--
        中间滚动区：把「当前小说列表标题 + 树 + 对话列表」打包成一个整体，
        高度 = 100vh − (logo + 3入口 + 模型配置) 的剩余空间；超出时整段自滚。
        模型配置在 .na-aside-footer 永远钉在底部；logo+3入口在顶部固定。
      -->
      <div class="na-aside-middle">
        <div class="na-section-title">当前小说列表</div>
        <div class="na-tree-scroll">
          <el-tree
            class="na-tree"
            :data="novelTree"
            node-key="id"
            :props="{ label: 'label', children: 'children' }"
            :expand-on-click-node="false"
            default-expand-all
            highlight-current
            @node-click="onNodeClick"
          >
            <template #default="{ data, node }">
              <span class="na-tree-node" :class="`is-${data.type}`">
                <el-icon v-if="data.type === 'novel'"><Files /></el-icon>
                <el-icon v-else-if="data.type === 'volume'"><Notebook /></el-icon>
                <el-icon v-else-if="data.type === 'article'"><Collection /></el-icon>
                <el-icon v-else-if="data.type === 'chapter'"><Document /></el-icon>
                <span class="na-tree-label">{{ data.label }}</span>
                <span class="na-tree-actions">
                  <el-button
                    v-if="['novel','volume','article'].includes(data.type)"
                    class="na-tree-action"
                    size="small"
                    text
                    title="添加子节点"
                    @click.stop="onAddChild(data)"
                  >
                    <el-icon><Plus /></el-icon>
                  </el-button>
                  <el-button
                    v-if="['novel','volume','article','chapter'].includes(data.type)"
                    class="na-tree-action"
                    size="small"
                    text
                    type="danger"
                    :title="`删除${typeLabel(data.type)}`"
                    @click.stop="onDeleteNode(data)"
                  >
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </span>
              </span>
            </template>
          </el-tree>
        </div>

        <!-- 对话列表：豆包式独立对话，时间戳命名（M-D-H-MI） -->
        <div class="na-conv-section">
          <div class="na-section-title">
            <span>对话列表</span>
            <el-button
              class="na-conv-add"
              size="small"
              text
              type="primary"
              title="新建对话"
              @click="onNewConversation"
            >
              <el-icon><Plus /></el-icon> 新建
            </el-button>
          </div>
          <ul class="na-conv-list">
            <li
              v-for="c in store.conversations"
              :key="c.id"
              class="na-conv-item"
              :class="{ 'is-active': c.id === store.currentConversationId }"
              @click="onSelectConversation(c.id)"
            >
              <el-icon class="na-conv-icon"><ChatLineRound /></el-icon>
              <span class="na-conv-label">{{ c.title }}</span>
              <el-button
                class="na-conv-del"
                size="small"
                text
                type="danger"
                title="删除对话"
                @click.stop="onDeleteConversation(c)"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </li>
            <li v-if="!store.conversations.length" class="na-conv-empty">
              暂无对话，点击右上「新建」开启第一个对话
            </li>
          </ul>
        </div>
      </div>

      <div class="na-aside-footer">
        <router-link to="/workspace/model-config" class="na-model-link">
          <el-icon><Setting /></el-icon> 模型配置
        </router-link>
      </div>
    </el-aside>

      <!-- 右侧：顶部 nav + 子 nav + 内容 -->
    <el-container>
      <!-- 顶部一级 tab：对话 / 概览 / 工作流 / 数据库 / 参考文档（红高亮当前） -->
      <!-- 参考资料 / 设定库 / 写作 SKILL 等「与左侧栏同级」的页面不显示顶部 tab（meta.hideTopNav） -->
      <el-header v-if="showTopNav" class="na-header">
        <el-tabs
          :model-value="activeParentName"
          class="na-tabs"
          active-color="#ff4d4f"
          @tab-change="onParentTabChange"
        >
          <el-tab-pane
            v-for="t in parentTabs"
            :key="t.name"
            :name="t.name"
            :label="t.title"
          />
        </el-tabs>
      </el-header>

      <!-- 二级 sub tab（仅在数据库 parent 激活时显示） -->
      <div v-if="showTopNav && activeParentName === 'database'" class="na-subtab-row">
        <div class="na-subtab-inner">
          <span
            v-for="t in databaseSubTabs"
            :key="t.name"
            class="na-subtab"
            :class="{ 'is-active': t.name === activeRouteName }"
            @click="goSub(t)"
          >
            {{ t.title }}
          </span>
        </div>
      </div>

      <el-main class="na-main">
        <router-view />
      </el-main>
    </el-container>

    <!-- 新建小说弹窗 -->
    <CreateNovelDialog v-model="createVisible" />
  </el-container>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, Files, Delete, Setting, Document, Notebook, Collection,
  ChatLineRound, DataAnalysis, TrendCharts, Memo,
} from '@element-plus/icons-vue'
import { useProjectStore } from '@/store/project'
import { volumeApi } from '@/api/volume'
import { articleApi } from '@/api/article'
import { chapterApi } from '@/api/chapter'
import CreateNovelDialog from '@/components/workspace/CreateNovelDialog.vue'

const route = useRoute()
const router = useRouter()
const store = useProjectStore()

// 「新建小说」弹窗显隐控制
const createVisible = ref(false)

// 顶部 tab 是否显示：参考资料 / 设定库 / 写作 SKILL 等「与左侧栏同级」页面隐藏
const showTopNav = computed(() => !route.meta?.hideTopNav)

// 侧栏上方「全局入口」：参考资料 / 设定库 / SKILL —— 跳到对应页面
const globalEntries = [
  { label: '参考资料', icon: Document, name: 'global-reference' },
  { label: '设定库',   icon: Collection, name: 'setting' },
  { label: 'SKILL',    icon: Notebook, name: 'custom-skill' },
  // Phase 4.1：生成评估（版本留档 / 打分 / 对比）—— 必须有侧栏入口，
  // 否则路由存在但用户点不到（本项目已两次踩过「零入口」的坑，见 06 手册）
  { label: '生成评估', icon: DataAnalysis, name: 'eval' },
  // Phase 4.2/4.3：观测（用量计量 + 反馈回流）
  { label: '观测', icon: TrendCharts, name: 'observability' },
  // Phase 7.5：篇规划（计划表格 / 选角 / 引入单 / 交接警告）—— 同上，必须有可达入口
  { label: '篇规划', icon: Memo, name: 'plan' },
]
function goEntry(entry) {
  router.push({ name: entry.name })
}
// 左侧栏全局入口高亮：当前路由与该入口同名时加红高亮
const isEntryActive = (name) => route.name === name

// —— 自适应顶部 nav：所有 meta.tab='parent' 的一级 tab ——
const parentTabs = computed(() =>
  router
    .getRoutes()
    .filter((r) => r.path.startsWith('/workspace/') && r.meta?.tab === 'parent')
    .map((r) => ({ name: r.name, title: r.meta.title }))
)
// —— 数据库 sub tab：所有 meta.tab='sub' 且 tabParent==='database' 的二级，按 meta.order 排序 ——
const databaseSubTabs = computed(() =>
  router
    .getRoutes()
    .filter((r) => r.path.startsWith('/workspace/') && r.meta?.tab === 'sub' && r.meta?.tabParent === 'database')
    .map((r) => ({ name: r.name, title: r.meta.title, order: r.meta?.order ?? 99 }))
    .sort((a, b) => a.order - b.order)
)

// 当前路由名（用于数据库 subtab 高亮）
const activeRouteName = computed(() => route.name)
const activeParentName = computed(() => {
  const r = router.getRoutes().find((rt) => rt.name === route.name)
  if (!r) return route.name
  if (r.meta?.tab === 'sub') return r.meta.tabParent
  return route.name
})

const onParentTabChange = (name) => {
  const cur = router.getRoutes().find((rt) => rt.name === route.name)
  if (name === 'database' && cur?.meta?.tab === 'sub' && cur.meta?.tabParent === 'database') {
    return
  }
  router.push({ name })
}

const goSub = (t) => router.push({ name: t.name })

// —— 4 级树：小说 → 卷 → 篇 → 章 ——
// 从 store.structure 派生（每次 selectNovel / loadStructure 后自动刷新）
const novelTree = computed(() =>
  store.novels.map((n) => {
    const volumes = (store.structure.volumes || []).filter((v) => v.project_id === n.id)
    return {
      id: n.id,
      label: n.name,
      type: 'novel',
      nodeKey: n.id,
      children: volumes.map((v) => ({
        id: v.id,
        label: v.name,
        type: 'volume',
        projectId: n.id,
        nodeKey: v.id,
        children: (v.articles || []).map((a) => ({
          id: a.id,
          label: a.name,
          type: 'article',
          projectId: n.id,
          volumeId: v.id,
          nodeKey: a.id,
          children: (a.chapters || []).map((c) => ({
            id: c.id,
            label: c.title || `第${c.chapter_no}章`,
            type: 'chapter',
            projectId: n.id,
            articleId: a.id,
            nodeKey: c.id,
          })),
        })),
      })),
    }
  })
)

const typeLabel = (t) => ({ novel: '小说', volume: '卷', article: '篇', chapter: '章' }[t] || t)

const onNodeClick = (data) => {
  if (data.type === 'novel') {
    store.selectNovel(data.id)
  } else if (data.type === 'chapter') {
    // ★ 确保先选中所属小说，否则 currentNovelId 为空 → 消息误走全局线程
    if (data.projectId && data.projectId !== store.currentNovelId) {
      store.selectNovel(data.projectId)
    }
    store.selectChapter(data.id)
    router.push({ name: 'chat' })
  } else if (data.type === 'volume') {
    if (data.projectId && data.projectId !== store.currentNovelId) {
      store.selectNovel(data.projectId)
    }
    store.selectVolume(data.id)
  } else if (data.type === 'article') {
    if (data.projectId && data.projectId !== store.currentNovelId) {
      store.selectNovel(data.projectId)
    }
    store.selectArticle(data.id)
  }
}

// 添加子节点：弹 ElMessageBox.prompt 输入名，依层级调对应 API
const onAddChild = async (data) => {
  const childType = data.type === 'novel' ? 'volume' : data.type === 'volume' ? 'article' : 'chapter'
  const defaultName = {
    volume: '第一卷',
    article: '第一篇',
    chapter: '第一章',
  }[childType]
  let value = ''
  try {
    const r = await ElMessageBox.prompt(
      `请输入新${typeLabel(childType)}的名称`,
      `在「${data.label}」下添加${typeLabel(childType)}`,
      {
        confirmButtonText: '创建',
        cancelButtonText: '取消',
        inputPlaceholder: defaultName,
        inputValue: defaultName,
      }
    )
    value = r?.value?.trim()
  } catch {
    return
  }
  if (!value) {
    ElMessage.warning('名称不能为空')
    return
  }
  try {
    if (childType === 'volume') {
      await volumeApi.create(data.id, { name: value })
    } else if (childType === 'article') {
      await articleApi.create(data.projectId, data.id, { name: value })
    } else if (childType === 'chapter') {
      // 章需要有 chapter_no；按该篇下已有章数 +1
      const existing = (data.children || []).length
      await articleApi.createChapter(data.id, { name: value, chapter_no: existing + 1, title: value })
    }
    await store.loadStructure(store.currentNovelId)
    ElMessage.success(`已创建${typeLabel(childType)}「${value}」`)
  } catch (e) {
    ElMessage.error('创建失败：' + (e?.message || '未知错误'))
  }
}

// 删除节点：按类型 dispatch，二次确认
const onDeleteNode = async (data) => {
  const typeText = typeLabel(data.type)
  const cascade = {
    novel: '该作品下的角色、章节、伏笔等所有资料将一并删除',
    volume: '将同时删除该卷下所有篇与章',
    article: '将同时删除该篇下所有章',
    chapter: '',
  }[data.type]
  try {
    await ElMessageBox.confirm(
      `确定要删除${typeText}「${data.label}」吗？${cascade ? '(' + cascade + ')' : ''}且不可恢复。`,
      `删除${typeText}`,
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        confirmButtonClass: 'el-button--danger',
      }
    )
  } catch {
    return
  }
  try {
    if (data.type === 'novel') {
      await store.deleteNovel(data.id)
    } else if (data.type === 'volume') {
      await volumeApi.remove(data.projectId, data.id)
    } else if (data.type === 'article') {
      await articleApi.remove(data.projectId, data.id)
    } else if (data.type === 'chapter') {
      await chapterApi.remove(data.projectId, data.id)
    }
    await store.loadStructure(store.currentNovelId)
    ElMessage.success(`已删除${typeText}「${data.label}」`)
  } catch (e) {
    ElMessage.error('删除失败：' + (e?.message || '未知错误'))
  }
}

const onCreateNovel = () => { createVisible.value = true }

// —— 顶部 logo 点击：SPA 跳首页；如已在 /workspace/chat 则强制刷新欢迎态 ——
const onLogoClick = (ev) => {
  // 已经 preventDefault 住了原生 href，但万一 router 推送失败也兜底
  const target = '/workspace/chat'
  if (route.path === target) {
    // 同路径：用 forceRefresh 重置 store 欢迎态上下文
    router.go(0)
    return
  }
  router.push(target).catch(() => {
    // router 推送失败（如页面在外部状态）→ 回退到原生导航
    window.location.href = target
  })
}

// —— 对话列表（豆包式时间戳命名会话） ——
const onNewConversation = () => {
  const conv = store.addConversation(store.currentNovelId || '')
  ElMessage.success(`已新建对话「${conv.title}」`)
  router.push({ name: 'chat' })
}
const onSelectConversation = (id) => {
  store.selectConversation(id)
  router.push({ name: 'chat' })
}
const onDeleteConversation = async (c) => {
  try {
    await ElMessageBox.confirm(
      `删除对话「${c.title}」？该对话的历史消息将一并删除，无法恢复。`,
      '删除对话',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' }
    )
  } catch { return }
  store.removeConversation(c.id)
  ElMessage.success(`已删除对话「${c.title}」`)
}

// 应用启动后从后端拉取作品列表与首本结构
onMounted(() => {
  store.hydrateConversations()
  store.loadNovels()
    .then(() => {
      // 预加载第一本小说的 4 级结构，让侧栏树默认展开；
      // 仅加载结构、不调用 selectNovel，故 currentNovelId 保持空 → 欢迎页仍是落地页。
      if (store.novels.length) store.loadStructure(store.novels[0].id)
    })
    .catch(() => ElMessage.warning('作品列表加载失败，请确认后端已启动'))
})
</script>

<style scoped>
.na-layout { height: 100vh; }
.na-aside {
  background: #1f2d3d;
  color: #fff;
  display: flex;
  flex-direction: column;
  /* 整侧栏自身不再 overflow-y:auto——交给 .na-aside-middle 内部滚，
     整侧栏高度由 .na-layout { height:100vh } 锁死 = 视口高度。 */
}
.na-aside::-webkit-scrollbar { width: 6px; }
.na-aside::-webkit-scrollbar-thumb { background: #3e5163; border-radius: 3px; }
.na-aside::-webkit-scrollbar-track { background: transparent; }
.na-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 60px;
  text-align: center;
  font-weight: 600;
  font-size: 16px;
  color: #fff;
  text-decoration: none;
  border-bottom: 1px solid #2c3e50;
  flex-shrink: 0;
  transition: background .15s, color .15s;
}
.na-logo:hover { background: #2c3e50; color: #fff; }
.na-logo.router-link-active { background: #243342; color: #ff4d4f; }
/* 侧栏上方操作区：新建小说 + 参考资料 + 设定库 + SKILL */
.na-aside-actions {
  padding: 12px 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}
.na-new-novel { width: 100%; }
.na-entry-btn {
  width: 100%;
  margin: 0;
  background: #2c3e50;
  border-color: #2c3e50;
  color: #fff;
}
.na-entry-btn:hover { background: #34495e; border-color: #34495e; }
.na-entry-btn.is-active { background: #ff4d4f; border-color: #ff4d4f; color: #fff; }
.na-entry-btn.is-active:hover { background: #e63a3c; border-color: #e63a3c; }

.na-section-title {
  padding: 8px 16px;
  font-size: 12px;
  color: #8a9bb0;
  letter-spacing: 1px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
}
.na-conv-add {
  padding: 2px 6px;
  font-size: 11px;
  color: #409eff !important;
}
/* 对话列表区段：按内容紧贴树下面；不再 max-height（由 .na-aside-middle 整体滚）。 */
.na-conv-section {
  flex: 0 0 auto;
  display: block;
  border-top: 1px solid #2c3e50;
  padding-bottom: 4px;
}
.na-conv-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.na-conv-list::-webkit-scrollbar { width: 6px; }
.na-conv-list::-webkit-scrollbar-thumb { background: #3e5163; border-radius: 3px; }
.na-conv-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  cursor: pointer;
  color: #c0c4cc;
  border-left: 3px solid transparent;
  transition: background .15s, color .15s;
}
.na-conv-item:hover { background: #2d3f53; color: #fff; }
.na-conv-item.is-active {
  background: #2f4b66;
  color: #fff;
  border-left-color: #ff4d4f;
}
.na-conv-icon { font-size: 14px; flex-shrink: 0; }
.na-conv-label {
  flex: 1;
  min-width: 0;          /* 同树修复：长对话标题也不撑开、不溢出 */
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.na-conv-del {
  padding: 2px !important;
  margin: 0;
  opacity: 0;
  transition: opacity .15s;
}
.na-conv-item:hover .na-conv-del { opacity: 1; }
.na-conv-empty {
  padding: 12px 16px;
  font-size: 11px;
  color: #6c7e92;
  text-align: center;
  font-style: italic;
  list-style: none;
}
/* 侧栏中段：占满 logo+3入口 与 模型配置 之间的剩余高度，整段自滚。
   min-height:0 必加，否则 flex 子项不会收缩，整侧栏会超出视口被截。 */
.na-aside-middle {
  flex: 1 1 0;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
}
.na-aside-middle::-webkit-scrollbar { width: 6px; }
.na-aside-middle::-webkit-scrollbar-thumb { background: #3e5163; border-radius: 3px; }
.na-aside-middle::-webkit-scrollbar-track { background: transparent; }
/* 小说树：在 .na-aside-middle 内部按内容自然展开，超出时由 middle 自己滚；
   这里不再设 max-height —— middle 接管滚动后这里的 max-height 会与父级重叠。 */
.na-tree-scroll {
  flex: 0 0 auto;
  display: block;
  min-height: 0;
}
.na-tree {
  background: transparent;
  /* 不设 max-height，由外层 .na-aside-middle 统一滚动 */
  overflow-y: visible;
}
.na-tree :deep(.el-tree-node__content) {
  background: transparent;
  color: #c0c4cc;
  height: 34px;
  overflow: hidden;       /* 防止长小说名把操作按钮挤出侧栏 */
}
.na-tree :deep(.el-tree-node__content:hover) { background: #2d3f53; }
.na-tree :deep(.el-tree-node.is-current > .el-tree-node__content) { background: #2f4b66; color: #fff; }
.na-tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  overflow: hidden;       /* 兜底：长名撑不开时也不让按钮溢出侧栏 */
}
.na-tree-node.is-novel .na-tree-label { color: #fff; font-weight: 600; }
.na-tree-node.is-volume .na-tree-label { color: #cdd6e4; font-weight: 600; }
.na-tree-node.is-article .na-tree-label { color: #c0c4cc; }
.na-tree-node.is-chapter .na-tree-label { color: #9aa4b2; }
.na-tree-label {
  flex: 1;
  min-width: 0;          /* 关键：让 flex 子项可收缩到比内容更窄，ellipsis 才能生效 */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.na-tree-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: 4px;
  opacity: 0;
  transition: opacity .15s;
}
.na-tree-node:hover .na-tree-actions { opacity: 1; }
.na-tree-action { padding: 2px !important; margin: 0; }
.na-aside-footer {
  border-top: 1px solid #2c3e50;
  padding: 12px 16px;
  flex: 0 0 auto;
  /* 完全按内容紧贴上面对话列表，模型配置不再被强制贴底——
     这样用户看到的就是「logo+3入口+树+对话列表+模型配置」从上到下贴紧堆叠 */
}
.na-model-link {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #c0c4cc;
  text-decoration: none;
}
.na-model-link:hover { color: #fff; }

/* 右侧 nav：顶部一行一级 tab + 数据库次级子 tab */
.na-header {
  display: flex;
  align-items: center;
  background: #fff;
  border-bottom: 1px solid var(--el-border-color-light);
  padding: 0 16px;
  height: 44px;
}
.na-tabs { width: 100%; }
.na-tabs :deep(.el-tabs__header) { margin: 0; }
.na-tabs :deep(.el-tabs__item) { font-size: 14px; }

/* 数据库 sub tab 行 —— 自定义样式以呈现「红高亮」效果（参考用户图） */
.na-subtab-row {
  background: #fff;
  border-bottom: 1px solid var(--el-border-color-light);
  padding: 8px 16px;
}
.na-subtab-inner {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.na-subtab {
  display: inline-block;
  padding: 6px 16px;
  font-size: 13px;
  border-radius: 4px;
  cursor: pointer;
  user-select: none;
  color: #606266;
  background: transparent;
  border: 1px solid transparent;
  transition: background .15s, color .15s;
}
.na-subtab:hover { background: #f0f2f5; }
.na-subtab.is-active {
  background: #ff4d4f;        /* 红 —— 与用户图 4/5 一致 */
  color: #fff;
  font-weight: 600;
}

.na-main { padding: 16px; background: #f5f7fa; }
</style>
