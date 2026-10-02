<template>
  <div class="gr-page">
    <!-- 子导航：与情节模板库同层的三个库（唯一的 tab 层，勿再加 el-tabs） -->
    <div class="gr-subnav">
      <router-link :to="{ name: 'template' }" class="gr-tab">情节模板库</router-link>
      <router-link :to="{ name: 'template-char' }" class="gr-tab">人物模板库</router-link>
      <router-link :to="{ name: 'global-ref', query: { tab: 'item' } }"
                   class="gr-tab" :class="{ on: active === 'item' }">物品库</router-link>
      <router-link :to="{ name: 'global-ref', query: { tab: 'skill' } }"
                   class="gr-tab" :class="{ on: active === 'skill' }">技能库</router-link>
    </div>

    <div class="gr-head">
      <div>
        <h2 class="gr-title">{{ active === 'item' ? '物品库' : '技能库' }}</h2>
        <p class="gr-sub">
          全局类型惯例词（E1 口径）：写作注入 + 抽取归一化都用它。
          描述不限长度（≤500 字，注入自动取前 50 字短版）；下架（disabled）后注入与归一化立即不可见。
        </p>
      </div>
      <div class="gr-stats">
        <span class="gr-stat">共 <b>{{ filtered.length }}</b> 条</span>
        <span class="gr-stat">启用 <b>{{ countBy('active') }}</b></span>
        <span class="gr-stat">下架 <b>{{ countBy('disabled') }}</b></span>
      </div>
    </div>

    <el-alert v-if="loadError" type="error" :closable="false" class="gr-alert">
      <template #title>
        加载失败：{{ loadError }}
        <el-button link type="primary" @click="load">重试</el-button>
      </template>
    </el-alert>

    <div class="gr-bar">
      <el-input v-model="kw" class="gr-search" clearable
                :placeholder="active === 'item' ? '搜名称 / 简述，如「疗伤」' : '搜名称 / 简述，如「剑诀」'" />
      <el-select v-model="catFilter" class="gr-filter" clearable placeholder="类目">
        <el-option v-for="c in cats" :key="c" :label="c" :value="c" />
      </el-select>
      <el-select v-model="statusFilter" class="gr-filter narrow" clearable placeholder="状态">
        <el-option label="启用" value="active" />
        <el-option label="下架" value="disabled" />
      </el-select>
      <el-button type="primary" plain @click="openCreate">新增{{ kindLabel }}</el-button>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-table :data="filtered" size="small" border stripe
              max-height="calc(100vh - 320px)" v-loading="loading">
      <el-table-column prop="name" label="名称" width="130" fixed />
      <el-table-column prop="category" label="类目" width="90" />
      <el-table-column label="描述" min-width="420" show-overflow-tooltip>
        <template #default="{ row }">{{ row.full_desc || row.brief }}</template>
      </el-table-column>
      <el-table-column prop="genre" label="题材池" width="80" />
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">
            {{ row.status === 'active' ? '启用' : '下架' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="110">
        <template #default="{ row }">{{ shortTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 编辑 / 新增弹窗 -->
    <el-dialog v-model="dlg.show" :title="dlg.create ? `新增${kindLabel}` : `编辑：${dlg.form.name}`"
               width="520px" destroy-on-close>
      <el-form label-width="72px">
        <el-form-item label="名称">
          <el-input v-model="dlg.form.name" maxlength="60" show-word-limit />
        </el-form-item>
        <el-form-item label="类目">
          <el-select v-model="dlg.form.category" placeholder="选类目" style="width:100%">
            <el-option v-for="c in cats" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="dlg.form.desc" type="textarea" :rows="5"
                    :maxlength="500" show-word-limit
                    placeholder="通用描述，想写多长写多长（≤500 字，建议 60~120 字）" />
        </el-form-item>
        <el-form-item label="题材池">
          <el-select v-model="dlg.form.genre" style="width:100%">
            <el-option v-for="g in genres" :key="g" :label="g" :value="g" />
          </el-select>
        </el-form-item>
        <el-form-item label="别名">
          <el-input v-model="dlg.aliasesText" placeholder="逗号分隔，可空" />
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="dlg.form.status" active-value="active" inactive-value="disabled"
                     active-text="启用" inactive-text="下架" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.show = false">取消</el-button>
        <el-button type="primary" :loading="dlg.saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { globalRefApi } from '@/api/globalRef'

const route = useRoute()
const router = useRouter()

const active = ref(route.query.tab === 'skill' ? 'skill' : 'item')
const loading = ref(false)
const loadError = ref('')
const meta = ref({})
const rows = ref([])
const kw = ref('')
const catFilter = ref('')
const statusFilter = ref('')

const dlg = reactive({
  show: false, saving: false, create: false, id: null,
  form: { name: '', category: '', desc: '', genre: '通用', status: 'active' },
  aliasesText: '',
})

const kind = computed(() => active.value)
const kindLabel = computed(() => (active.value === 'item' ? '物品' : '技能'))
const cats = computed(() =>
  active.value === 'item'
    ? (meta.value.item_categories || [])
    : (meta.value.skill_categories || []))
const genres = computed(() => meta.value.genres || [])

const filtered = computed(() => {
  const k = kw.value.trim().toLowerCase()
  return rows.value.filter((r) => {
    if (catFilter.value && r.category !== catFilter.value) return false
    if (statusFilter.value && r.status !== statusFilter.value) return false
    if (k) {
      const hay = (r.name + '\n' + r.brief + '\n' + (r.full_desc || '')
        + '\n' + (r.aliases || []).join('\n')).toLowerCase()
      if (!hay.includes(k)) return false
    }
    return true
  })
})

function countBy(status) {
  return rows.value.filter((r) => r.status === status).length
}

function shortTime(iso) {
  if (!iso) return ''
  return iso.slice(0, 10)
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    // 注意：http.js 拦截器已解包统一信封，res 直接就是 list[dict]
    const fn = kind.value === 'item'
      ? globalRefApi.listItems : globalRefApi.listSkills
    const res = await fn({ limit: 2000 })
    rows.value = Array.isArray(res) ? res : (res?.items || [])
  } catch (e) {
    loadError.value = e?.response?.data?.message || e.message || '网络错误'
  } finally {
    loading.value = false
  }
}

function openEdit(row) {
  dlg.create = false
  dlg.id = row.id
  dlg.form = {
    name: row.name, category: row.category,
    desc: row.full_desc || row.brief || '',
    genre: row.genre, status: row.status,
  }
  dlg.aliasesText = (row.aliases || []).join('，')
  dlg.show = true
}

function openCreate() {
  dlg.create = true
  dlg.id = null
  dlg.form = {
    name: '', category: cats.value[0] || '', desc: '',
    genre: '通用', status: 'active',
  }
  dlg.aliasesText = ''
  dlg.show = true
}

async function save() {
  const f = { ...dlg.form }
  if (!f.name.trim()) { ElMessage.warning('名称不能为空'); return }
  if (!f.category) { ElMessage.warning('请选类目'); return }
  const desc = (f.desc || '').trim()
  if (!desc) { ElMessage.warning('描述不能为空'); return }
  const aliases = dlg.aliasesText.split(/[，,]/).map((s) => s.trim()).filter(Boolean)
  // full_desc 存全文；brief 自动取前 50 字（写作注入用短版，无需手填）
  const payload = {
    name: f.name.trim(), category: f.category, genre: f.genre,
    status: f.status, aliases,
    full_desc: desc, brief: desc.slice(0, 50),
  }
  dlg.saving = true
  try {
    if (dlg.create) {
      await (kind.value === 'item'
        ? globalRefApi.createItem({ kind: kind.value, ...payload })
        : globalRefApi.createSkill({ kind: kind.value, ...payload }))
    } else {
      await (kind.value === 'item'
        ? globalRefApi.updateItem(dlg.id, payload)
        : globalRefApi.updateSkill(dlg.id, payload))
    }
    ElMessage.success(dlg.create ? '已新增' : '已保存')
    dlg.show = false
    await load()
  } catch (e) {
    ElMessage.error('保存失败：' + (e?.response?.data?.message || e.message))
  } finally {
    dlg.saving = false
  }
}

watch(() => route.query.tab, (t) => {
  const next = t === 'skill' ? 'skill' : 'item'
  if (next !== active.value) {
    active.value = next
    catFilter.value = ''
    kw.value = ''
    load()
  }
})

onMounted(async () => {
  try {
    const res = await globalRefApi.meta()
    meta.value = res || {}
  } catch { /* meta 拉不到就用默认 50 上限 */ }
  await load()
})
</script>

<style scoped>
.gr-page { padding: 18px 22px; }
.gr-subnav { display: flex; gap: 4px; margin-bottom: 14px; }
.gr-tab {
  padding: 7px 18px; border: 1px solid #e3e6ea; border-bottom: none;
  border-radius: 8px 8px 0 0; color: #6a737d; text-decoration: none;
  background: #f0f2f5; font-size: 14px;
}
.gr-tab.on { background: #fff; color: #24292f; font-weight: 700; }
.gr-head { display: flex; justify-content: space-between; align-items: flex-start; }
.gr-title { margin: 0 0 4px; font-size: 20px; }
.gr-sub { margin: 0; color: #6a737d; font-size: 13px; }
.gr-stats { display: flex; gap: 14px; color: #6a737d; font-size: 13px; }
.gr-stat b { color: #24292f; }
.gr-alert { margin-bottom: 12px; }
.gr-bar { display: flex; gap: 10px; margin-bottom: 12px; }
.gr-search { width: 260px; }
.gr-filter { width: 140px; }
.gr-filter.narrow { width: 110px; }
</style>
