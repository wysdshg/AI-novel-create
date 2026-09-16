<template>
  <div class="fs-page">
    <div class="fs-head">
      <h2 class="fs-title">线索 / 伏笔</h2>
      <p class="fs-sub">
        AI 每写完一章会自动抽取伏笔动作（埋下 / 暗示 / 回收）并写进这张表，
        下一章生成时未回收的伏笔会自动注入上下文提醒 —— 也就是「你自己埋的线，AI 帮你记着」。
        这里可以人工补录、标记回收、清理误报。
      </p>
    </div>

    <div class="fs-bar">
      <el-radio-group v-model="statusFilter" size="small" @change="load">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button value="pending">未回收</el-radio-button>
        <el-radio-button value="done">已回收</el-radio-button>
      </el-radio-group>

      <el-input v-model="kw" size="small" placeholder="搜索描述 / 场景" clearable style="width: 220px" />

      <el-button size="small" type="primary" :disabled="!novelId" @click="openCreate">新建伏笔</el-button>
      <el-button size="small" text :disabled="!novelId" @click="load">刷新</el-button>
    </div>

    <div class="fs-cards">
      <div class="fs-card">
        <div class="fs-card-num">{{ total }}</div>
        <div class="fs-card-label">合计</div>
      </div>
      <div class="fs-card">
        <div class="fs-card-num is-warn">{{ nPending }}</div>
        <div class="fs-card-label">未回收（待兑现）</div>
      </div>
      <div class="fs-card">
        <div class="fs-card-num is-ok">{{ nDone }}</div>
        <div class="fs-card-label">已回收</div>
      </div>
    </div>

    <el-empty v-if="!loading && !filtered.length" :description="emptyText" />

    <el-table v-else v-loading="loading" :data="filtered" size="small" border>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small" effect="plain">
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="伏笔内容" min-width="320" show-overflow-tooltip />
      <el-table-column label="埋于" width="90" align="center">
        <template #default="{ row }">
          {{ row.buried_chapter != null ? `第 ${row.buried_chapter} 章` : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="回收于" width="90" align="center">
        <template #default="{ row }">
          {{ row.activated_chapter != null ? `第 ${row.activated_chapter} 章` : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="涉及场景" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ row.scene || '—' }}</template>
      </el-table-column>
      <el-table-column label="启用" width="70" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled !== false" size="small"
                     @change="(v) => toggleEnabled(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" align="right">
        <template #default="{ row }">
          <el-button v-if="row.status !== 'done'" size="small" text type="success"
                     @click="doActivate(row)">标记回收</el-button>
          <el-button size="small" text @click="openEdit(row)">编辑</el-button>
          <el-button size="small" text type="danger" @click="doRemove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlgVisible" :title="editing ? '编辑伏笔' : '新建伏笔'" width="560px">
      <el-form label-width="90px">
        <el-form-item label="内容" required>
          <el-input v-model="form.description" type="textarea" :rows="3"
                    placeholder="要埋的线是什么，用一句话说清" />
        </el-form-item>
        <el-form-item label="埋于章节">
          <el-input-number v-model="form.buried_chapter" :min="0" :controls="false"
                           placeholder="章号，可留空" style="width: 140px" />
        </el-form-item>
        <el-form-item label="涉及场景">
          <el-input v-model="form.scene" placeholder="如「第3章 崖底」" />
        </el-form-item>
        <el-form-item label="触发条件">
          <el-input v-model="form.trigger_condition" placeholder="什么情况下该回收它" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
          <span class="fs-hint">关掉后不再注入下一章上下文</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlgVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="doSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProjectStore } from '@/store/project'
import { foreshadowApi } from '@/api/foreshadow'

const store = useProjectStore()
const novelId = computed(() => store.currentNovelId)

const rows = ref([])
const loading = ref(false)
const statusFilter = ref('')
const kw = ref('')

const total = computed(() => rows.value.length)
const nPending = computed(() => rows.value.filter((r) => r.status !== 'done').length)
const nDone = computed(() => rows.value.filter((r) => r.status === 'done').length)

const filtered = computed(() => {
  const q = kw.value.trim()
  if (!q) return rows.value
  return rows.value.filter(
    (r) => (r.description || '').includes(q) || (r.scene || '').includes(q),
  )
})

const emptyText = computed(() => {
  // ⚠️ 区分「没选作品」与「选了但没伏笔」—— 两者提示必须不同，
  // 否则用户看到「这个作品还没有伏笔记录」会以为数据丢了（2026-09-15 真机验收抓出）
  if (!novelId.value) return '请先在左侧「当前小说列表」里选择一本小说'
  if (rows.value.length) return '没有匹配的伏笔（试试清空搜索）'
  return '这个作品还没有伏笔记录 —— 写一章后 AI 会自动抽取，也可以点「新建伏笔」手动补录'
})

const dlgVisible = ref(false)
const saving = ref(false)
const editing = ref(null)
const form = reactive({
  description: '', buried_chapter: null, scene: '', trigger_condition: '', enabled: true,
})

const statusLabel = (s) => ({ pending: '未回收', active: '已暗示', done: '已回收' }[s] || s || '—')
const statusType = (s) => ({ pending: 'warning', active: 'primary', done: 'success' }[s] || 'info')

async function load() {
  loading.value = true
  try {
    const r = await foreshadowApi.list(
      novelId.value, statusFilter.value ? { status: statusFilter.value } : {})
    rows.value = Array.isArray(r) ? r : (r?.items || [])
  } catch {
    /* 拦截器已报错 */
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, {
    description: '', buried_chapter: null, scene: '', trigger_condition: '', enabled: true,
  })
  dlgVisible.value = true
}

function openEdit(row) {
  editing.value = row
  Object.assign(form, {
    description: row.description || '',
    buried_chapter: row.buried_chapter ?? null,
    scene: row.scene || '',
    trigger_condition: row.trigger_condition || '',
    enabled: row.enabled !== false,
  })
  dlgVisible.value = true
}

async function doSave() {
  if (!form.description.trim()) {
    ElMessage.warning('伏笔内容不能为空')
    return
  }
  saving.value = true
  try {
    const payload = {
      description: form.description.trim(),
      buried_chapter: form.buried_chapter ?? null,
      scene: form.scene || null,
      trigger_condition: form.trigger_condition || null,
      enabled: form.enabled,
    }
    if (editing.value) {
      await foreshadowApi.update(novelId.value, editing.value.id, payload)
      ElMessage.success('已保存')
    } else {
      await foreshadowApi.create(novelId.value, payload)
      ElMessage.success('已新建')
    }
    dlgVisible.value = false
    await load()
  } catch {
    /* 拦截器已报错 */
  } finally {
    saving.value = false
  }
}

async function doActivate(row) {
  try {
    await foreshadowApi.activate(novelId.value, row.id)
    ElMessage.success('已标记回收')
    await load()
  } catch {
    /* 拦截器已报错 */
  }
}

async function toggleEnabled(row, val) {
  try {
    await foreshadowApi.update(novelId.value, row.id, { enabled: val })
    row.enabled = val
  } catch {
    /* 拦截器已报错 */
  }
}

async function doRemove(row) {
  try {
    await ElMessageBox.confirm(
      `删除后不可恢复，确认删除这条伏笔？\n\n${(row.description || '').slice(0, 60)}`,
      '删除伏笔',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await foreshadowApi.remove(novelId.value, row.id)
    ElMessage.success('已删除')
    await load()
  } catch {
    /* 拦截器已报错 */
  }
}

watch(novelId, (v) => { if (v) load() })
onMounted(() => { if (novelId.value) load() })
</script>

<style scoped>
.fs-page { padding: 16px 20px; }
.fs-head { margin-bottom: 12px; }
.fs-title { font-size: 16px; font-weight: 600; margin: 0 0 4px; }
.fs-sub { font-size: 12px; color: var(--el-text-color-secondary); margin: 0; line-height: 1.7; }
.fs-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.fs-cards {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 14px;
}
.fs-card { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 12px 14px; }
.fs-card-num { font-size: 20px; font-weight: 600; line-height: 1.3; }
.fs-card-num.is-warn { color: var(--el-color-warning); }
.fs-card-num.is-ok { color: var(--el-color-success); }
.fs-card-label { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
.fs-hint { font-size: 12px; color: var(--el-text-color-secondary); margin-left: 10px; }
</style>
