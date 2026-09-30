<template>
  <div class="it">
    <div class="it-toolbar">
      <div class="it-title">
        资料库 · 物品库
        <span class="it-novel">当前作品：{{ store.currentNovel?.name || '未选择' }}</span>
      </div>
      <div class="it-toolbar-actions">
        <el-input
          v-model="keyword"
          placeholder="搜索：物品名 / 标签"
          clearable
          style="width: 220px"
          @input="load"
          @clear="load"
        />
        <el-button :icon="RefreshRight" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建物品</el-button>
      </div>
    </div>

    <el-card v-if="!projectId" shadow="never" class="it-empty">
      <el-alert type="info" :closable="false" title="请先在左侧选择或新建一本小说" />
    </el-card>

    <el-card v-else shadow="never" class="it-card">
      <el-table
        :data="filteredList"
        v-loading="loading"
        empty-text="暂无物品，点击右上角新建（章节摄取的 AI 抽取也会自动落库）"
        border stripe
      >
        <el-table-column prop="name" label="物品名" min-width="150" fixed />
        <el-table-column label="类别" width="100">
          <template #default="{ row }">{{ row.category || '—' }}</template>
        </el-table-column>
        <el-table-column label="唯一" width="70" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_unique" size="small" type="warning" effect="light">唯一</el-tag>
            <span v-else class="it-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">{{ row.status || '—' }}</template>
        </el-table-column>
        <el-table-column label="简介" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.summary || '—' }}</template>
        </el-table-column>
        <el-table-column label="标签" min-width="140">
          <template #default="{ row }">
            <el-tag v-for="t in row.tags || []" :key="t" size="small" effect="plain" style="margin-right:4px">{{ t }}</el-tag>
            <span v-if="!(row.tags || []).length" class="it-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="90" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.ai_generated" size="small" effect="plain">AI</el-tag>
            <span v-else class="it-muted">手动</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑物品' : '新建物品'"
      width="680px"
      top="6vh"
      @closed="resetForm"
    >
      <el-form :model="form" label-width="100px">
        <el-form-item label="物品名" required>
          <el-input v-model="form.name" maxlength="80" show-word-limit />
        </el-form-item>
        <el-form-item label="类别">
          <el-select v-model="form.category" allow-create filterable default-first-option placeholder="法宝 / 丹药 / 材料 / 其他" style="width:100%">
            <el-option label="法宝" value="法宝" />
            <el-option label="丹药" value="丹药" />
            <el-option label="材料" value="材料" />
            <el-option label="功法" value="功法" />
            <el-option label="信物" value="信物" />
            <el-option label="其他" value="其他" />
          </el-select>
        </el-form-item>
        <el-form-item label="唯一物">
          <el-switch v-model="form.is_unique" />
          <span class="it-hint">剧情核心物品（如主角金手指）勾选</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-input v-model="form.status" placeholder="完好 / 受损 / 已失去" />
        </el-form-item>
        <el-form-item label="简介">
          <el-input v-model="form.summary" type="textarea" :rows="2" placeholder="一句话概括（用于列表与 AI 注入的短描述）" />
        </el-form-item>
        <el-form-item label="详细描述">
          <el-input v-model="form.full_desc" type="textarea" :rows="5" placeholder="外观、来历、能力、限制……能写进描述的都写这里" />
        </el-form-item>
        <el-form-item label="标签">
          <el-select
            v-model="form.tags" multiple filterable allow-create default-first-option
            placeholder="输入后回车（如：法宝 / 剧情核心 / 可成长）" style="width:100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import { useProjectStore } from '@/store/project'
import { itemApi } from '@/api/database'

const store = useProjectStore()
const projectId = computed(() => store.currentNovelId)
const list = ref([])
const loading = ref(false)
const keyword = ref('')

const dialogVisible = ref(false)
const editingId = ref(null)
const emptyForm = () => ({
  name: '', category: '', is_unique: false, status: '完好',
  summary: '', full_desc: '', tags: [],
})
const form = reactive(emptyForm())

const filteredList = computed(() => {
  if (!keyword.value) return list.value
  const k = keyword.value.toLowerCase()
  return list.value.filter((s) =>
    (s.name || '').toLowerCase().includes(k) ||
    (s.tags || []).some((t) => String(t).toLowerCase().includes(k)))
})

async function load() {
  if (!projectId.value) { list.value = []; return }
  loading.value = true
  try {
    list.value = await itemApi.list(projectId.value)
  } catch (e) {
    list.value = []
  } finally {
    loading.value = false
  }
}

function resetForm() { Object.assign(form, emptyForm()); editingId.value = null }
function openCreate() { resetForm(); dialogVisible.value = true }
function openEdit(row) {
  Object.assign(form, {
    name: row.name, category: row.category || '', is_unique: !!row.is_unique,
    status: row.status || '完好', summary: row.summary || '', full_desc: row.full_desc || '',
    tags: row.tags ? [...row.tags] : [],
  })
  editingId.value = row.id
  dialogVisible.value = true
}

async function save() {
  if (!form.name || !form.name.trim()) { ElMessage.warning('请填写物品名'); return }
  if (editingId.value) {
    await itemApi.update(projectId.value, editingId.value, form)
    ElMessage.success('已更新')
  } else {
    await itemApi.create(projectId.value, form)
    ElMessage.success('已创建')
  }
  dialogVisible.value = false
  load()
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确认删除物品「${row.name}」？`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  await itemApi.remove(projectId.value, row.id)
  ElMessage.success('已删除')
  load()
}

onMounted(load)
watch(projectId, load)
</script>

<style scoped>
.it { padding: 4px; }
.it-toolbar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 14px; flex-wrap: wrap; gap: 10px;
}
.it-title { font-size: 16px; font-weight: 600; display: flex; align-items: baseline; gap: 10px; }
.it-novel { font-size: 13px; color: #909399; font-weight: 400; }
.it-toolbar-actions { display: flex; gap: 10px; align-items: center; }
.it-card, .it-empty { margin-bottom: 14px; }
.it-muted { color: #c0c4cc; }
.it-hint { font-size: 12px; color: #909399; margin-left: 10px; }
</style>
