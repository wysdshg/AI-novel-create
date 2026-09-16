<template>
  <div class="tpl-page">
    <div class="tp-head">
      <div>
        <h2 class="tp-title">情节模板库</h2>
        <p class="tp-sub">
          从 7 本小说 2453 章里凝练出的结构化套路（阶段 → 节拍 → 多书走法）。
          写新篇时按需检索：卡文就搜一句模糊口述，看看别的书在同一个节拍上怎么走。
        </p>
      </div>
      <div class="tp-stats">
        <span class="tp-stat">共 <b>{{ total }}</b> 个</span>
        <span v-if="statusFilter" class="tp-stat">{{ statusText(statusFilter) }} <b>{{ list.length }}</b></span>
      </div>
    </div>

    <!-- 工具条：检索 + 过滤 -->
    <div class="tp-bar">
      <el-input
        v-model="keyword"
        class="tp-search"
        placeholder="模糊口述检索，如「既像学院大比又像秘境寻宝」"
        clearable
        @keyup.enter="doSearch"
      >
        <template #append>
          <el-button :loading="searching" @click="doSearch">检索</el-button>
        </template>
      </el-input>

      <el-select v-model="scaleFilter" placeholder="粒度" clearable style="width: 110px" @change="loadList">
        <el-option label="故事弧" value="arc" />
        <el-option label="情节段" value="segment" />
      </el-select>

      <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 120px" @change="loadList">
        <el-option label="已审阅" value="reviewed" />
        <el-option label="草稿" value="draft" />
        <el-option label="已归档" value="archived" />
      </el-select>

      <el-select v-model="tagFilter" placeholder="题材" clearable style="width: 130px" @change="onTagChange">
        <el-option v-for="t in allTags" :key="t" :label="t" :value="t" />
      </el-select>

      <el-button v-if="isSearchMode" @click="exitSearch">退出检索</el-button>
      <el-button :loading="loading" @click="loadList">刷新</el-button>
    </div>

    <el-alert
      v-if="isSearchMode"
      class="tp-alert"
      :type="searchMode === 'fallback_tags' ? 'warning' : 'success'"
      :closable="false"
      show-icon
    >
      <template #title>
        检索结果 {{ list.length }} 条
        <span v-if="searchMode === 'fallback_tags'">（向量不可用，已回退关键词匹配）</span>
        <span v-else>（向量语义检索 + 命中节拍）</span>
      </template>
    </el-alert>

    <el-empty v-if="loading" description="加载中…" />
    <el-empty
      v-else-if="!list.length"
      :description="isSearchMode ? '没有匹配的模板，换个说法试试' : '模板库为空'"
    />

    <div v-else class="tp-grid">
      <div v-for="t in list" :key="t.id" class="tp-card" @click="openDetail(t)">
        <div class="tp-card-head">
          <span class="tp-name">{{ t.name }}</span>
          <el-tag :type="statusType(t.status)" size="small" effect="plain">
            {{ statusText(t.status) }}
          </el-tag>
        </div>

        <p v-if="t.logline" class="tp-logline">{{ t.logline }}</p>

        <div class="tp-tags">
          <el-tag
            v-for="g in (t.genre_tags || []).slice(0, 4)"
            :key="g"
            size="small"
            effect="plain"
            type="info"
          >
            {{ g }}
          </el-tag>
        </div>

        <div class="tp-meta">
          <span class="tp-chip">{{ t.scale === 'arc' ? '故事弧' : '情节段' }}</span>
          <span class="tp-chip">{{ (t.structure?.phases || []).length }} 阶段</span>
          <span class="tp-chip">{{ beatCount(t) }} 节拍</span>
          <span class="tp-chip">{{ (t.structure?.cast || []).length }} 角色位</span>
        </div>

        <div class="tp-src">
          来源：{{ (t.source_stats?.book_names || []).join('、') || '—' }}
          <span v-if="t.source_stats?.books" class="tp-arcs">（{{ t.source_stats.books }} 书）</span>
        </div>

        <div v-if="t.matched_beats?.length" class="tp-matched">
          <div class="tp-matched-title">命中节拍</div>
          <div v-for="(b, i) in t.matched_beats.slice(0, 3)" :key="i" class="tp-matched-item">
            <b>{{ b.phase }} · {{ b.beat }}</b>
            <span v-if="b.variants?.length" class="tp-matched-how">—— {{ b.variants[0].how }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawer" :title="detail?.name || '模板详情'" size="620px" destroy-on-close>
      <div v-if="detail" class="tp-detail">
        <div class="tp-d-row">
          <el-tag :type="statusType(detail.status)" size="small" effect="plain">
            {{ statusText(detail.status) }}
          </el-tag>
          <el-tag size="small" effect="plain" type="info">
            {{ detail.scale === 'arc' ? '故事弧' : '情节段' }}
          </el-tag>
          <el-tag v-for="g in detail.genre_tags || []" :key="g" size="small" effect="plain">
            {{ g }}
          </el-tag>
        </div>

        <p v-if="detail.logline" class="tp-d-logline">{{ detail.logline }}</p>

        <div class="tp-d-actions">
          <el-select v-model="detail.status" size="small" style="width: 120px" @change="changeStatus">
            <el-option label="已审阅" value="reviewed" />
            <el-option label="草稿" value="draft" />
            <el-option label="已归档" value="archived" />
          </el-select>
          <el-button size="small" type="danger" plain @click="removeTemplate">删除</el-button>
        </div>

        <el-divider content-position="left">结构（阶段 → 节拍 → 多书走法）</el-divider>

        <el-empty
          v-if="!(detail.structure?.phases || []).length"
          description="该模板没有结构数据"
          :image-size="60"
        />
        <div v-for="(ph, pi) in detail.structure?.phases || []" :key="pi" class="tp-phase">
          <div class="tp-phase-name">{{ ph.phase }}</div>
          <div v-for="(b, bi) in ph.beats || []" :key="bi" class="tp-beat">
            <div class="tp-beat-name">{{ b.beat }}</div>
            <div v-for="(v, vi) in b.variants || []" :key="vi" class="tp-variant">
              <span class="tp-variant-src">{{ v.src }}</span>
              <span class="tp-variant-how">{{ v.how }}</span>
            </div>
            <div v-if="!b.variants?.length" class="tp-none">（无走法记录）</div>
          </div>
        </div>

        <el-divider content-position="left">角色功能位（cast）</el-divider>
        <div v-if="!(detail.structure?.cast || []).length" class="tp-none">无</div>
        <div v-for="(c, ci) in detail.structure?.cast || []" :key="ci" class="tp-cast">
          <div class="tp-cast-head">
            <b>{{ c.slot }}</b>
            <el-tag size="small" effect="plain" type="warning">{{ c.mode }}</el-tag>
          </div>
          <div class="tp-cast-desc">{{ c.desc }}</div>
          <div class="tp-cast-srcs">
            出处：{{ (c.srcs || []).map((s) => `${s.book}·${s.alias}`).join('；') || '—' }}
          </div>
        </div>

        <template v-if="(detail.pitfalls || []).length">
          <el-divider content-position="left">易踩的坑</el-divider>
          <ul class="tp-list">
            <li v-for="(p, i) in detail.pitfalls" :key="i">{{ p }}</li>
          </ul>
        </template>

        <template v-if="detail.rhythm">
          <el-divider content-position="left">节奏</el-divider>
          <p class="tp-text">{{ detail.rhythm }}</p>
        </template>

        <template v-if="(detail.source_stats?.arc_refs || []).length">
          <el-divider content-position="left">来源弧（可追溯）</el-divider>
          <ul class="tp-list">
            <li v-for="(r, i) in detail.source_stats.arc_refs" :key="i">{{ r }}</li>
          </ul>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { plotTemplateApi } from '@/api/plotTemplate'

const loading = ref(false)
const searching = ref(false)
const list = ref([])
const total = ref(0)

const keyword = ref('')
const scaleFilter = ref('')
const statusFilter = ref('')
const tagFilter = ref('')
const isSearchMode = ref(false)
const searchMode = ref('')

const drawer = ref(false)
const detail = ref(null)

// 题材标签从当前列表里现取（后端没有单独的 tag 字典端点；反正一次 list 就够）
const allTags = computed(() => {
  const s = new Set()
  for (const t of list.value) for (const g of t.genre_tags || []) s.add(g)
  return [...s].sort()
})

function beatCount(t) {
  let n = 0
  for (const ph of t.structure?.phases || []) n += (ph.beats || []).length
  return n
}
function statusType(s) {
  return s === 'reviewed' ? 'success' : s === 'draft' ? 'warning' : 'info'
}
function statusText(s) {
  return s === 'reviewed' ? '已审阅' : s === 'draft' ? '草稿' : '已归档'
}

async function loadList() {
  loading.value = true
  isSearchMode.value = false
  try {
    const data = await plotTemplateApi.list({
      scale: scaleFilter.value,
      status: statusFilter.value,
    })
    let rows = Array.isArray(data) ? data : data?.items || []
    if (tagFilter.value) {
      rows = rows.filter((t) => (t.genre_tags || []).includes(tagFilter.value))
    }
    list.value = rows
    total.value = rows.length
  } catch (e) {
    // http 拦截器已弹错误提示；这里只需保证 UI 不残留脏数据
    list.value = []
  } finally {
    loading.value = false
  }
}

// 切题材算「本地过滤」——不能在 loadList 里带 tagFilter 判断，否则清空题材时
// 会因为 allTags 依赖当前 list 而把已选中的标签选项弄丢（鸡生蛋问题）
function onTagChange() {
  if (isSearchMode.value) {
    exitSearch()
  } else {
    loadList()
  }
}

async function doSearch() {
  const q = keyword.value.trim()
  if (!q) {
    ElMessage.info('先输入一句描述再检索')
    return
  }
  searching.value = true
  try {
    const data = await plotTemplateApi.search({
      query: q,
      scale: scaleFilter.value || null,
      topK: 12,
    })
    searchMode.value = data?.mode || ''
    list.value = data?.items || []
    isSearchMode.value = true
    if (!list.value.length) ElMessage.info('没有匹配的模板')
  } catch (e) {
    list.value = []
  } finally {
    searching.value = false
  }
}

function exitSearch() {
  keyword.value = ''
  loadList()
}

function openDetail(t) {
  // 列表项（尤其来自 search 的）字段可能不全 → 拉一次详情拿完整 structure
  detail.value = { ...t }
  drawer.value = true
  plotTemplateApi
    .get(t.id)
    .then((full) => {
      detail.value = { ...full, matched_beats: t.matched_beats }
    })
    .catch(() => {})
}

async function changeStatus(val) {
  try {
    await plotTemplateApi.update(detail.value.id, { ...detail.value, status: val })
    ElMessage.success('状态已更新')
    loadList()
  } catch (e) {
    // 拦截器已提示
  }
}

async function removeTemplate() {
  try {
    await ElMessageBox.confirm(
      `确定删除模板「${detail.value.name}」？会连带清掉它的向量。`,
      '删除确认',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await plotTemplateApi.remove(detail.value.id)
    ElMessage.success('已删除')
    drawer.value = false
    loadList()
  } catch (e) {
    // 拦截器已提示
  }
}

onMounted(loadList)
</script>

<style scoped>
.tpl-page {
  padding: 16px 20px;
}
.tp-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}
.tp-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 4px;
}
.tp-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 0;
  line-height: 1.6;
  max-width: 720px;
}
.tp-stats {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  white-space: nowrap;
}
.tp-stat {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.tp-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.tp-search {
  width: 380px;
}
.tp-alert {
  margin-bottom: 12px;
}
.tp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.tp-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--el-bg-color);
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.tp-card:hover {
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 2px 10px rgb(0 0 0 / 6%);
}
.tp-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.tp-name {
  font-size: 14px;
  font-weight: 600;
}
.tp-logline {
  font-size: 12px;
  color: var(--el-text-color-regular);
  margin: 8px 0 0;
  line-height: 1.7;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.tp-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 8px;
}
.tp-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.tp-chip {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
}
.tp-src {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 8px;
}
.tp-arcs {
  color: var(--el-text-color-placeholder);
}
.tp-matched {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.tp-matched-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-color-primary);
  margin-bottom: 4px;
}
.tp-matched-item {
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.6;
}
.tp-matched-how {
  color: var(--el-text-color-secondary);
}
.tp-d-row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.tp-d-logline {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.8;
  margin: 10px 0 0;
}
.tp-d-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}
.tp-phase {
  margin-bottom: 14px;
}
.tp-phase-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-color-primary);
  padding: 2px 0 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 8px;
}
.tp-beat {
  margin: 0 0 10px 8px;
}
.tp-beat-name {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 4px;
}
.tp-variant {
  font-size: 12px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  padding-left: 10px;
}
.tp-variant-src {
  display: inline-block;
  min-width: 62px;
  color: var(--el-color-warning);
  font-weight: 500;
}
.tp-variant-how {
  color: var(--el-text-color-regular);
}
.tp-cast {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.tp-cast-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.tp-cast-desc {
  font-size: 12px;
  color: var(--el-text-color-regular);
  line-height: 1.7;
  margin-top: 4px;
}
.tp-cast-srcs {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 4px;
}
.tp-none {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
.tp-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}
.tp-text {
  font-size: 13px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  margin: 0;
}
</style>
