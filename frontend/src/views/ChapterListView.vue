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
    <el-drawer v-model="contentDrawer" size="640px" :title="contentTitle" destroy-on-close @closed="onDrawerClosed">
      <div v-loading="contentLoading || polishLoading" class="ct-wrap">
        <!-- ── AI 润色模式 ────────────────────────────── -->
        <template v-if="polishStep === 'select'">
          <el-alert
            type="info" :closable="false" show-icon style="margin-bottom: 10px"
            title="AI 润色：勾选要打磨的段落"
            description="只优化选中的段落，其余原样保留；可全选。走魔搭开思考（选中全章约 10~20 分钟，选得少更快）。"
          />
          <div class="pl-bar">
            <el-checkbox v-model="polishAllChecked" @change="onPolishAll">全选</el-checkbox>
            <span class="pl-n">共 {{ polishParas.length }} 段 · 已选 {{ polishSelCount }} 段</span>
            <el-button size="small" @click="polishStep = ''">退出润色</el-button>
            <el-button size="small" type="primary" :disabled="!polishSelCount" @click="startPolish">
              开始润色（{{ polishSelCount }} 段）
            </el-button>
          </div>
          <div class="pl-list">
            <div v-for="(p, i) in polishParas" :key="i" class="pl-item">
              <el-checkbox v-model="p.checked" />
              <span class="pl-idx">{{ i + 1 }}</span>
              <span class="pl-len">{{ p.text.length }}字</span>
              <span class="pl-text" :title="p.text">{{ p.text.slice(0, 60) }}</span>
            </div>
          </div>
        </template>

        <template v-else-if="polishStep === 'running'">
          <el-alert
            type="warning" :closable="false" show-icon style="margin-bottom: 10px"
            title="魔搭开思考打磨中（一次请求，全章约 10~20 分钟）"
            description="可以关掉抽屉去干别的，任务在后端不会中断；回来点「查看结果」。"
          />
          <div class="pl-bar">
            <span class="pl-n">已等待 {{ Math.floor(polishWaited / 60) }} 分 {{ polishWaited % 60 }} 秒</span>
            <el-button size="small" @click="polishStep = ''">先离开</el-button>
            <el-button size="small" type="primary" @click="pollOnce">查看结果</el-button>
          </div>
        </template>

        <template v-else-if="polishStep === 'review'">
          <el-alert
            type="success" :closable="false" show-icon style="margin-bottom: 10px"
            :title="`打磨完成：${(polishResult.items || []).length} 段，已采纳 ${acceptedCount} 段`"
            description="逐段对比。采纳会替换正文对应段落（已实时写入下方编辑器）；未采纳的保持原样。改完记得回编辑模式点「保存」。"
          />
          <div class="pl-bar">
            <el-button size="small" type="primary" @click="acceptAll">全部采纳</el-button>
            <el-button size="small" type="success" :disabled="!acceptedCount" @click="finishPolish">
              完成（已应用 {{ acceptedCount }} 段）
            </el-button>
            <el-button size="small" @click="polishStep = 'select'">返回选段</el-button>
          </div>
          <div class="pl-list">
            <div v-for="it in polishResult.items" :key="it.idx" class="pl-cmp">
              <div class="pl-cmp-head">
                <span class="pl-idx">{{ it.idx }}</span>
                <span class="pl-meta">重合 {{ it.ov_self }} · 参考 {{ it.ov_ref_max }}</span>
                <el-tag v-if="accepted[it.idx] === it.polished" size="small" type="success">已采纳</el-tag>
                <el-tag v-else-if="accepted[it.idx] === it.original" size="small" type="info">保留原文</el-tag>
                <el-button size="small" type="primary" plain @click="acceptOne(it)">采纳</el-button>
                <el-button size="small" @click="keepOne(it)">保留原文</el-button>
              </div>
              <div class="pl-old">{{ it.original }}</div>
              <div class="pl-new">{{ it.polished }}</div>
            </div>
          </div>
        </template>

        <!-- ── 编辑模式 ──────────────────────────────── -->
        <template v-else>
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
            <el-button size="small" :disabled="!contentDraft" @click="openPolish">AI 润色</el-button>
            <el-button size="small" @click="contentDrawer = false">取消</el-button>
            <el-button size="small" type="primary" :disabled="!contentChanged" @click="saveContent">
              保存
            </el-button>
          </div>
        </template>
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
import { chapterApi, polishApi } from '@/api/chapter'

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

// ── AI 润色（2026-09-21）：勾选段落 → 一次请求打包 → 魔搭开思考 → 对照采纳 ──
// 拆段规则必须与后端 chapter_paras 完全一致（短行并入上一段），否则段号会错位。
const polishStep = ref('')            // '' | select | running | review
const polishLoading = ref(false)
const polishParas = ref([])           // [{ text, checked }] —— 段号 = 下标 + 1（与后端一致）
const polishTaskId = ref('')
const polishWaited = ref(0)
const polishResult = ref(null)
const accepted = ref({})              // idx -> 采纳的文本
let polishStartAt = 0
let pollTimer = null

const polishSelCount = computed(() => polishParas.value.filter((p) => p.checked).length)
const polishAllChecked = computed({
  get: () => polishParas.value.length > 0 && polishParas.value.every((p) => p.checked),
  set: (v) => polishParas.value.forEach((p) => { p.checked = v }),
})
const acceptedCount = computed(() => Object.keys(accepted.value).length)

function onPolishAll(v) {
  polishParas.value.forEach((p) => { p.checked = v })
}

function splitParasLocal(text) {
  const out = []
  for (const line of text.split(/\r?\n/)) {
    const s = line.trim()
    if (!s) continue
    if (s.length < 12 && out.length) { out[out.length - 1] += s; continue }
    if (s.length < 12) continue
    out.push(s)
  }
  return out
}

function openPolish() {
  const paras = splitParasLocal(contentDraft.value || '')
  if (!paras.length) { ElMessage.warning('本章没有可润色的正文'); return }
  polishParas.value = paras.map((text) => ({ text, checked: true }))   // 默认全选
  polishResult.value = null
  accepted.value = {}
  polishStep.value = 'select'
}

function onDrawerClosed() {
  clearInterval(pollTimer)
  if (polishStep.value === 'running') polishStep.value = ''   // 任务在后端不中断，重开可重试
}

function stopPoll() { clearInterval(pollTimer); pollTimer = null }

async function startPolish() {
  const ids = polishParas.value.map((p, i) => (p.checked ? i + 1 : 0)).filter(Boolean)
  if (!ids.length) return
  polishLoading.value = true
  try {
    const r = await polishApi.start(projectId.value, contentChapterId.value, {
      para_ids: ids, provider: 'ms', thinking: true, top_k: 5,
    })
    polishTaskId.value = r.task_id
    polishStartAt = Date.now()
    polishWaited.value = 0
    polishStep.value = 'running'
    stopPoll()
    pollTimer = setInterval(() => pollOnce(), 5000)
  } catch (e) {
    ElMessage.error('启动润色失败')
  } finally {
    polishLoading.value = false
  }
}

async function pollOnce() {
  try {
    const s = await polishApi.get(projectId.value, contentChapterId.value, polishTaskId.value)
    polishWaited.value = Math.floor((Date.now() - polishStartAt) / 1000)
    if (s.status === 'done') {
      stopPoll()
      polishResult.value = s.result
      accepted.value = {}
      polishStep.value = 'review'
    } else if (s.status === 'error') {
      stopPoll()
      ElMessage.error('润色失败：' + (s.error || '').slice(0, 140))
      polishStep.value = 'select'
    }
  } catch (e) { /* 网络抖动忽略，下一轮再试 */ }
}

function applyAccepted() {
  const paras = polishParas.value.map((p) => p.text)
  for (const [k, v] of Object.entries(accepted.value)) {
    const i = Number(k) - 1
    paras[i] = v
    if (polishParas.value[i]) polishParas.value[i].text = v
  }
  contentDraft.value = paras.join('\n\n')   // 每段一行、段间空行（与正文原格式一致）
}

function acceptOne(it) {
  accepted.value[it.idx] = it.polished
  applyAccepted()
}
function keepOne(it) {
  accepted.value[it.idx] = it.original
  applyAccepted()
}
function acceptAll() {
  (polishResult.value.items || []).forEach((it) => { accepted.value[it.idx] = it.polished })
  applyAccepted()
}
function finishPolish() {
  applyAccepted()
  polishStep.value = ''
  ElMessage.success(`已应用 ${acceptedCount.value} 段修改 —— 记得点「保存」写回后端`)
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
/* ── AI 润色面板 ── */
.pl-bar {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 10px; flex-wrap: wrap;
}
.pl-n { font-size: 12px; color: #909399; margin-right: auto; }
.pl-list {
  max-height: 520px; overflow: auto;
  border: 1px solid #ebeef5; border-radius: 6px;
}
.pl-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-bottom: 1px solid #f2f6fc;
}
.pl-idx { font-size: 12px; color: #409eff; min-width: 30px; font-weight: 600; }
.pl-len { font-size: 11px; color: #c0c4cc; min-width: 46px; }
.pl-text {
  font-size: 13px; color: #606266; flex: 1;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pl-cmp { border: 1px solid #ebeef5; border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
.pl-cmp-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.pl-meta { font-size: 11px; color: #909399; margin-right: auto; }
.pl-old {
  font-size: 12.5px; color: #909399; background: #f5f7fa;
  border-radius: 6px; padding: 8px 10px; margin-bottom: 6px; line-height: 1.8;
}
.pl-new { font-size: 13.5px; line-height: 1.9; }
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
