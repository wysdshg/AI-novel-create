<template>
  <div class="cl">
    <div class="cl-toolbar">
      <div class="cl-title">
        章节列表
        <span class="cl-novel">当前作品：{{ store.currentNovel?.name || '未选择' }}</span>
      </div>
      <div class="cl-actions">
        <el-input
          v-model="keyword"
          class="cl-search"
          placeholder="搜索标题 / 正文"
          clearable
          :prefix-icon="Search"
        />
        <el-radio-group v-model="statusFilter" size="small">
          <el-radio-button label="all">全部</el-radio-button>
          <el-radio-button label="drafted">已写</el-radio-button>
          <el-radio-button label="empty">草稿</el-radio-button>
        </el-radio-group>
        <!-- 导出（08-B2③）：list 接口本就返回完整 content，前端直接拼 txt，零后端改动 -->
        <el-dropdown @command="onExport" :disabled="!projectId">
          <el-button :disabled="!projectId">
            导出<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="selected" :disabled="!selected.length">
                导出选中章节（{{ selected.length }}）
              </el-dropdown-item>
              <el-dropdown-item command="all">导出全部章节（{{ allChapters.length }}）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button
          type="danger"
          plain
          :disabled="!selected.length"
          @click="removeSelected"
        >
          删除选中（{{ selected.length }}）
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="!projectId"
      type="info"
      :closable="false"
      show-icon
      title="请先在左侧选择一本小说"
      description="章节列表按作品隔离——选定作品后这里会列出它的全部卷 / 篇 / 章。"
    />

    <el-card v-else shadow="never" class="cl-card">
      <!-- 树形表格：卷 → 篇 → 章，直接复用 store.structure（与侧栏同一数据源，不重复请求） -->
      <el-table
        ref="tableRef"
        :data="rows"
        v-loading="loading"
        row-key="id"
        :tree-props="{ children: 'children' }"
        default-expand-all
        empty-text="暂无章节：可在左侧树里给「篇」添加章，或到对话页生成"
        border
        stripe
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="46" :selectable="(r) => r.type === 'chapter'" />
        <el-table-column label="标题" min-width="280">
          <template #default="{ row }">
            <span class="cl-name" :class="`is-${row.type}`">
              <el-icon v-if="row.type === 'volume'"><Notebook /></el-icon>
              <el-icon v-else-if="row.type === 'article'"><Collection /></el-icon>
              <el-icon v-else><Document /></el-icon>
              {{ row.label }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="层" width="70">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ typeLabel(row.type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="chapter_no" label="章号" width="80" align="center">
          <template #default="{ row }">
            <span v-if="row.type === 'chapter'">{{ row.chapter_no ?? '—' }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="字数" width="100" align="right">
          <template #default="{ row }">
            <span v-if="row.type === 'chapter'">{{ (row.word_count || 0).toLocaleString() }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.type === 'chapter'" :type="row.word_count ? 'success' : 'info'" size="small" effect="light">
              {{ row.word_count ? '已写' : '草稿' }}
            </el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.note">{{ row.note }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="160">
          <template #default="{ row }">
            <span v-if="row.updated_at">{{ fmtTime(row.updated_at) }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <template v-if="row.type === 'chapter'">
              <el-button size="small" text type="primary" @click="openChapter(row)">进入</el-button>
              <el-button size="small" text type="primary" @click="openContent(row)">正文</el-button>
              <el-button size="small" text type="primary" @click="rename(row)">改名</el-button>
              <el-button size="small" text type="danger" @click="removeOne(row)">删除</el-button>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 正文查看 / 编辑（Phase 4.3 的前提：没有编辑入口，反馈就无从产生） -->
    <el-drawer v-model="contentDrawer" size="640px" :title="contentTitle" destroy-on-close>
      <div v-loading="contentLoading" class="ct-wrap">
        <el-alert
          v-if="contentChanged"
          type="info"
          :closable="false"
          show-icon
          title="已修改"
          description="保存后系统会记下你改了哪些 —— 这些正是 AI 反复做不好的地方"
          style="margin-bottom: 10px"
        />
        <el-input
          v-model="contentDraft"
          type="textarea"
          :autosize="{ minRows: 18, maxRows: 30 }"
          placeholder="该章还没有正文"
        />
        <div class="ct-foot">
          <span class="ct-count">{{ (contentDraft || '').length }} 字</span>
          <el-button size="small" @click="contentDrawer = false">取消</el-button>
          <el-button size="small" type="primary" :disabled="!contentChanged" @click="saveContent">
            保存
          </el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, Collection, Notebook, Search, ArrowDown } from '@element-plus/icons-vue'
import { useProjectStore } from '@/store/project'
import { chapterApi } from '@/api/chapter'

const store = useProjectStore()
const router = useRouter()
const projectId = computed(() => store.currentNovelId)

const tableRef = ref(null)
const loading = ref(false)
const keyword = ref('')
const statusFilter = ref('all')
const selected = ref([])

const typeLabel = (t) => ({ volume: '卷', article: '篇', chapter: '章' }[t] || t)

function fmtTime(s) {
  if (!s) return '—'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// 把 store.structure（卷→篇→章）摊平成树表行；过滤时保留命中节点的祖先链
const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const shouldKeepChapter = (c) => {
    if (statusFilter.value === 'drafted' && !c.word_count) return false
    if (statusFilter.value === 'empty' && c.word_count) return false
    if (!kw) return true
    return (
      (c.title || '').toLowerCase().includes(kw) ||
      (c.content || '').toLowerCase().includes(kw)
    )
  }
  const out = []
  for (const v of store.structure.volumes || []) {
    const artRows = []
    for (const a of v.articles || []) {
      const kids = (a.chapters || [])
        .filter(shouldKeepChapter)
        .map((c) => ({
          id: c.id,
          type: 'chapter',
          label: c.title || `第${c.chapter_no}章`,
          chapter_no: c.chapter_no,
          word_count: c.word_count,
          note: c.note,
          updated_at: c.updated_at,
          content: c.content,
          article_id: a.id,
        }))
      // 有过滤条件时，把没有命中章节的篇整条隐藏，避免空壳
      if (kids.length || !kw && statusFilter.value === 'all') {
        artRows.push({ id: a.id, type: 'article', label: a.name || '未命名篇', children: kids })
      }
    }
    if (artRows.length || (!kw && statusFilter.value === 'all')) {
      out.push({ id: v.id, type: 'volume', label: v.name || '未命名卷', children: artRows })
    }
  }
  return out
})

function onSelectionChange(sel) {
  selected.value = sel
}

// ── 导出 txt（08-B2③）────────────────────────────────────────────
// list 接口（_chapter_to_dict）本就返回完整 content → 前端直接拼文件，零后端改动。
// 「全部」从 store.structure 原始数据取（**无视搜索/状态筛选**，导出的是真全本，
// 而不是「当前筛选后的子集」——后者容易让人以为导出了全本）。

const allChapters = computed(() => {
  const out = []
  for (const v of store.structure.volumes || []) {
    for (const a of v.articles || []) for (const c of a.chapters || []) out.push(c)
  }
  return out
})

function _today() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`
}

function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function fmtChapters(chs) {
  return [...chs]
    .sort((a, b) => (a.chapter_no || 0) - (b.chapter_no || 0))
    .map((c) => `第${c.chapter_no}章 ${c.title || ''}\n\n${(c.content || '').trim() || '（本章暂无正文）'}`)
    .join('\n\n\n')
}

function onExport(cmd) {
  const name = store.currentNovel?.name || '未命名'
  if (cmd === 'selected') {
    const chs = selected.value.filter((r) => r.type === 'chapter')
    if (!chs.length) return
    downloadText(`${name}-选中${chs.length}章-${_today()}.txt`, fmtChapters(chs))
    ElMessage.success(`已导出 ${chs.length} 章`)
    return
  }
  const chs = allChapters.value
  if (!chs.length) {
    ElMessage.warning('当前作品还没有章节')
    return
  }
  downloadText(`${name}-全本${chs.length}章-${_today()}.txt`, fmtChapters(chs))
  ElMessage.success(`已导出全部 ${chs.length} 章`)
}

// 进入该章工作区（对话页：对话 + 正文 + 走向）
function openChapter(row) {
  store.selectChapter(row.id)
  router.push({ name: 'chat' })
}

// ── 正文查看 / 编辑（Phase 4.3 的前提：没有编辑入口，反馈就无从产生）──
const contentDrawer = ref(false)
const contentLoading = ref(false)
const contentTitle = ref('正文')
const contentDraft = ref('')
const contentOriginal = ref('')
const contentChapterId = ref('')
const contentChanged = computed(() => contentDraft.value !== contentOriginal.value)

async function openContent(row) {
  contentChapterId.value = row.id
  contentTitle.value = `${row.label} · 正文`
  contentDraft.value = ''
  contentOriginal.value = ''
  contentDrawer.value = true
  contentLoading.value = true
  try {
    const d = await chapterApi.get(projectId.value, row.id)
    const text = d?.content || ''
    contentDraft.value = text
    contentOriginal.value = text
  } catch (e) {
    ElMessage.error('加载正文失败')
  } finally {
    contentLoading.value = false
  }
}

async function saveContent() {
  try {
    // 后端会对比「AI 原文」与本次提交，自动记一笔反馈（Phase 4.3）
    await chapterApi.update(projectId.value, contentChapterId.value, { content: contentDraft.value })
    ElMessage.success('已保存')
    contentDrawer.value = false
    await store.loadStructure()   // 字数 / 状态列跟着刷新
  } catch (e) {
    ElMessage.error('保存失败')
  }
}

async function rename(row) {
  try {
    const { value } = await ElMessageBox.prompt('新的章节标题', '重命名', {
      inputValue: row.label,
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValidator: (v) => (v && v.trim() ? true : '标题不能为空'),
    })
    await chapterApi.update(projectId.value, row.id, { title: value.trim() })
    ElMessage.success('已更新标题')
    await store.loadStructure()
  } catch (e) {
    if (e !== 'cancel' && e?.action !== 'cancel') console.error(e)
  }
}

async function removeOne(row) {
  try {
    await ElMessageBox.confirm(`确认删除「${row.label}」？该章的正文与章级记忆会一并清除。`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
    await chapterApi.remove(projectId.value, row.id)
    ElMessage.success('已删除')
    if (store.currentChapterId === row.id) store.selectChapter('')
    await store.loadStructure()
  } catch (e) {
    if (e !== 'cancel' && e?.action !== 'cancel') console.error(e)
  }
}

async function removeSelected() {
  const targets = selected.value.filter((r) => r.type === 'chapter')
  if (!targets.length) return
  try {
    await ElMessageBox.confirm(
      `确认删除选中的 ${targets.length} 章？正文与章级记忆会一并清除，不可撤销。`,
      '批量删除确认',
      { type: 'warning', confirmButtonText: '全部删除', cancelButtonText: '取消' },
    )
  } catch (e) {
    if (e === 'cancel' || e?.action === 'cancel') return
    throw e
  }
  let okCount = 0
  const failed = []
  // 逐条删除（后端为单条 DELETE 接口），单条失败不影响其余
  for (const t of targets) {
    try {
      await chapterApi.remove(projectId.value, t.id)
      okCount++
      if (store.currentChapterId === t.id) store.selectChapter('')
    } catch (e) {
      failed.push(t.label)
      console.error('删除章节失败', t.label, e)
    }
  }
  if (okCount) ElMessage.success(`已删除 ${okCount} 章`)
  if (failed.length) ElMessage.error(`以下章节删除失败：${failed.join('、')}`)
  tableRef.value?.clearSelection()
  await store.loadStructure()
}

async function refresh() {
  if (!projectId.value) return
  loading.value = true
  try {
    // 数据源统一走 store.structure —— 侧栏树 / 生章后会自动同步，避免两处各自请求导致不一致
    await store.loadStructure(projectId.value)
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
watch(projectId, refresh)

// 侧栏可能触发生章/删章，结构变化时同步刷新一次（loadStructure 会重建对象引用）
watch(() => store.structure.volumes, () => { /* 依赖 structure 即可，无需额外请求 */ }, { deep: false })

// 键盘：Esc 清空筛选
function onKeydown(e) {
  if (e.key === 'Escape') {
    keyword.value = ''
    statusFilter.value = 'all'
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))
</script>

<style scoped>
.ct-wrap {
  display: flex;
  flex-direction: column;
}
.ct-foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 12px;
}
.ct-count {
  margin-right: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.cl { padding: 4px 16px 16px; }
.cl-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-bottom: 14px; flex-wrap: wrap;
}
.cl-title { font-size: 18px; font-weight: 600; }
.cl-novel { font-size: 13px; font-weight: 400; color: #909399; margin-left: 10px; }
.cl-actions { display: flex; align-items: center; gap: 10px; }
.cl-search { width: 220px; }
.cl-name { display: inline-flex; align-items: center; gap: 6px; }
.cl-name.is-volume { font-weight: 600; }
.cl-name.is-article { font-weight: 500; }
.muted { color: #c0c4cc; }
</style>
