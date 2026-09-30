<template>
  <div class="sv">
    <div class="sv-toolbar">
      <div class="sv-title">
        设定库
        <span class="sv-sub">设定模板按题材整套管理 · 新建小说时挑一套即可</span>
      </div>
      <div class="sv-toolbar-actions">
        <el-button
          v-if="activeTab === 'template'"
          type="primary" :icon="Plus" @click="openCreate"
        >新建设定模板</el-button>
      </div>
    </div>

    <!-- 子标签页切换 -->
    <div class="sv-tabs">
      <span
        class="sv-tab"
        :class="{ 'sv-tab-active': activeTab === 'template' }"
        @click="activeTab = 'template'"
      >设定模板</span>
      <span
        v-if="hasNovel"
        class="sv-tab"
        :class="{ 'sv-tab-active': activeTab === 'novel' }"
        @click="switchToNovelTab"
      >本小说设定库</span>
      <span
        class="sv-tab"
        :class="{ 'sv-tab-active': activeTab === 'legacy' }"
        @click="activeTab = 'legacy'"
      >旧版单条设定</span>
    </div>

    <!-- ====== 设定模板（默认视图，按题材一套一套） ====== -->
    <template v-if="activeTab === 'template'">
      <div class="sv-tpl-filter">
        <el-input
          v-model="tplKeyword" placeholder="搜索模板名 / 说明" clearable
          style="width: 240px" @input="loadTemplates" @clear="loadTemplates"
        />
      </div>
      <div v-loading="tplLoading" class="sv-tpl-grid">
        <el-card
          v-for="t in tplList" :key="t.id"
          shadow="hover" class="sv-tpl-card"
        >
          <div class="sv-tpl-head">
            <span class="sv-tpl-name">{{ t.name }}</span>
            <el-tag size="small" effect="plain">{{ t.genre }}</el-tag>
          </div>
          <div class="sv-tpl-summary">{{ t.summary || '（无说明）' }}</div>
          <div class="sv-tpl-meta">
            <span>{{ t.content_chars }} 字</span>
            <span>{{ (t.updated_at || '').slice(0, 10) }}</span>
          </div>
          <div class="sv-tpl-actions">
            <el-button size="small" text type="primary" @click="openTplView(t)">查看</el-button>
            <el-button size="small" text type="primary" @click="openTplEdit(t)">编辑</el-button>
            <el-button size="small" text type="danger" @click="removeTpl(t)">删除</el-button>
          </div>
        </el-card>
        <el-empty
          v-if="!tplLoading && !tplList.length"
          description="还没有设定模板，点右上角「新建设定模板」创建一套"
          style="grid-column: 1 / -1"
        />
      </div>
    </template>

    <!-- ====== 本小说设定库 ====== -->
    <template v-if="activeTab === 'novel'">
      <el-card shadow="never" class="sv-card">
        <div class="sv-novel-header">
          <span>当前小说：{{ novelName }}</span>
          <el-button size="small" @click="openNovelSettingEditor">修改设定库</el-button>
        </div>
        <div v-if="novelSettings.length" class="sv-novel-list">
          <div v-for="s in novelSettings" :key="s.id" class="sv-novel-item">
            <span class="sv-novel-name">{{ s.name }}</span>
            <el-tag size="small" effect="plain">{{ s.category }}</el-tag>
            <span class="sv-novel-desc">{{ (s.description || '').slice(0, 80) }}{{ (s.description || '').length > 80 ? '…' : '' }}</span>
          </div>
        </div>
        <el-empty v-else :image-size="60" description="本小说未选择任何设定，对话时将全量注入所有全局设定" />
      </el-card>
    </template>

    <!-- ====== 旧版单条设定（只读留存，不再供新建小说挑选） ====== -->
    <template v-if="activeTab === 'legacy'">
      <el-alert type="info" :closable="false" class="sv-legacy-alert"
                title="旧版单条设定已整合进「设定模板」，此处仅留存查看；新建小说请在「设定模板」中挑选。" />
      <el-card shadow="never" class="sv-card">
        <div class="sv-legacy-toolbar">
          <el-input
            v-model="keyword" placeholder="搜索：名称 / 描述" clearable
            style="width: 240px" @input="load" @clear="load"
          />
        </div>
        <el-table :data="list" v-loading="loading" empty-text="无设定" border stripe size="small">
          <el-table-column prop="name" label="名称" min-width="180" fixed />
          <el-table-column label="分类" width="90">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.category }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="360" show-overflow-tooltip>
            <template #default="{ row }">{{ row.description || '—' }}</template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>

    <!-- 设定模板：查看 / 新建 / 编辑 共用弹窗 -->
    <el-dialog
      v-model="tplDialog.visible"
      :title="tplDialog.view ? `设定模板：${tplDialog.form.name}` : (tplDialog.editingId ? '编辑设定模板' : '新建设定模板')"
      width="860px" top="4vh" destroy-on-close
    >
      <el-form :model="tplDialog.form" label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="tplDialog.form.name" maxlength="120" :disabled="tplDialog.view" />
        </el-form-item>
        <el-form-item label="适用题材">
          <el-select v-model="tplDialog.form.genre" style="width: 200px" :disabled="tplDialog.view"
                     allow-create filterable placeholder="如 玄幻 / 架空历史 / 高武">
            <el-option v-for="g in genreOptions" :key="g" :label="g" :value="g" />
          </el-select>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="tplDialog.form.summary" maxlength="500" :disabled="tplDialog.view"
                    placeholder="一句话说明这套模板覆盖什么（列表页展示）" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input
            v-model="tplDialog.form.content" type="textarea" :rows="20"
            :disabled="tplDialog.view"
            placeholder="Markdown 单文档：境界 / 货币 / 体系 / 规则 / 物价… 分节组织"
            class="sv-tpl-content"
          />
          <div class="sv-hint">{{ (tplDialog.form.content || '').length }} 字 · 支持 Markdown</div>
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="tplDialog.tagsText" :disabled="tplDialog.view"
                    placeholder="逗号分隔，例如：玄幻,修仙,境界" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tplDialog.visible = false">{{ tplDialog.view ? '关闭' : '取消' }}</el-button>
        <el-button v-if="!tplDialog.view" type="primary" :loading="tplDialog.saving" @click="saveTpl">保存</el-button>
      </template>
    </el-dialog>

    <!-- 本小说设定库编辑弹窗 -->
    <el-dialog
      v-model="novelDialogVisible"
      title="修改本小说设定库"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="sv-novel-edit-hint">
        勾选的设定模板/设定将在本小说对话中注入。不勾选任何项 = 全量注入所有全局设定。
      </div>
      <div v-if="novelEditLoading" class="sv-novel-hint" style="text-align:center;padding:20px 0">加载中…</div>
      <el-checkbox-group v-else v-model="novelSelectedIds" class="sv-novel-edit-list">
        <el-checkbox
          v-for="s in selectablePool" :key="s.id" :value="s.id" class="sv-novel-edit-item"
        >
          <span class="sv-novel-name">{{ s.name }}</span>
          <el-tag size="small" effect="plain">{{ s.category }}</el-tag>
          <span class="sv-novel-meta">{{ s.meta }}</span>
        </el-checkbox>
      </el-checkbox-group>
      <div v-if="selectablePool.length" class="sv-novel-count">
        已选 {{ novelSelectedIds.length }} / {{ selectablePool.length }}
      </div>
      <template #footer>
        <el-button @click="novelDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="novelSaving" @click="saveNovelSettings">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { settingApi, settingTemplateApi } from '@/api/setting'
import { projectApi } from '@/api/projects'
import { useProjectStore } from '@/store/project'

const store = useProjectStore()
const hasNovel = computed(() => !!store.currentNovelId)
const novelName = computed(() => store.currentNovelName || '未选择')

// ====== 设定模板（主视图） ======
const activeTab = ref('template')
const tplKeyword = ref('')
const tplList = ref([])
const tplLoading = ref(false)
const genreOptions = ['玄幻', '架空历史', '高武', '都市', '科幻', '通用']

const tplDialog = reactive({
  visible: false, saving: false, view: false, editingId: null,
  form: { name: '', genre: '通用', summary: '', content: '' },
  tagsText: '',
})

async function loadTemplates() {
  tplLoading.value = true
  try {
    tplList.value = await settingTemplateApi.list({ q: tplKeyword.value })
  } finally {
    tplLoading.value = false
  }
}

function openTplView(row) {
  openTplDialog(row, true)
}

function openTplEdit(row) {
  openTplDialog(row, false)
}

async function openTplDialog(row, view) {
  tplDialog.view = view
  tplDialog.editingId = view ? null : row.id
  // 列表行不带正文，查看/编辑都需拉详情
  const full = await settingTemplateApi.get(row.id)
  tplDialog.form = {
    name: full.name, genre: full.genre || '通用',
    summary: full.summary || '', content: full.content || '',
  }
  tplDialog.tagsText = (full.tags || []).join(',')
  tplDialog.visible = true
}

function openCreate() {
  tplDialog.view = false
  tplDialog.editingId = null
  tplDialog.form = { name: '', genre: '通用', summary: '', content: '' }
  tplDialog.tagsText = ''
  tplDialog.visible = true
}

async function saveTpl() {
  const f = tplDialog.form
  if (!f.name?.trim()) { ElMessage.warning('请填写名称'); return }
  if (!f.content?.trim()) { ElMessage.warning('正文不能为空'); return }
  const tags = tplDialog.tagsText.split(',').map((s) => s.trim()).filter(Boolean)
  tplDialog.saving = true
  try {
    if (tplDialog.editingId) {
      await settingTemplateApi.update(tplDialog.editingId, { ...f, tags })
      ElMessage.success('已更新')
    } else {
      await settingTemplateApi.create({ ...f, tags })
      ElMessage.success('已创建')
    }
    tplDialog.visible = false
    await loadTemplates()
  } catch (e) {
    ElMessage.error('保存失败：' + (e?.message || '未知错误'))
  } finally {
    tplDialog.saving = false
  }
}

async function removeTpl(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除设定模板「${row.name}」？此操作不可撤销。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch { return }
  await settingTemplateApi.remove(row.id)
  ElMessage.success('已删除')
  loadTemplates()
}

// ====== 旧版单条设定（只读留存） ======
const keyword = ref('')
const list = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    list.value = await settingApi.list({ keyword: keyword.value })
  } finally {
    loading.value = false
  }
}

// ====== 本小说设定库 ======
const novelSettings = ref([])
const novelDialogVisible = ref(false)
const novelEditLoading = ref(false)
const novelSaving = ref(false)
const selectablePool = ref([])    // 设定模板 + 旧模板 单条，统一 {id,name,category,meta,description}
const novelSelectedIds = ref([])

async function switchToNovelTab() {
  activeTab.value = 'novel'
  await loadNovelSettings()
}

function tplToPoolItem(t) {
  return {
    id: t.id, name: t.name, category: '设定模板',
    meta: `${t.genre || '通用'} · ${t.content_chars} 字`,
    description: t.summary || '',
  }
}

function legacyToPoolItem(s) {
  return {
    id: s.id, name: s.name, category: s.category,
    meta: `${(s.levels || []).length} 级`,
    description: s.description || '',
  }
}

async function loadSelectablePool() {
  // 设定模板（新）+ 旧 is_template 单条，合并供勾选
  const [tpls, legacy] = await Promise.all([
    settingTemplateApi.list({}),
    settingApi.list({ template_only: true }),
  ])
  selectablePool.value = [
    ...tpls.map(tplToPoolItem),
    ...legacy.map(legacyToPoolItem),
  ]
}

async function loadNovelSettings() {
  if (!store.currentNovelId) return
  try {
    const res = await projectApi.getSettings(store.currentNovelId)
    const ids = res.setting_ids || []
    if (!ids.length) { novelSettings.value = []; return }
    await loadSelectablePool()
    novelSettings.value = selectablePool.value.filter((s) => ids.includes(s.id))
  } catch {
    novelSettings.value = []
  }
}

async function openNovelSettingEditor() {
  if (!store.currentNovelId) return
  novelDialogVisible.value = true
  novelEditLoading.value = true
  try {
    const [settingRes] = await Promise.all([
      projectApi.getSettings(store.currentNovelId),
      loadSelectablePool(),
    ])
    novelSelectedIds.value = settingRes.setting_ids || []
  } catch (e) {
    ElMessage.error('加载失败：' + (e?.message || '未知错误'))
  } finally {
    novelEditLoading.value = false
  }
}

async function saveNovelSettings() {
  if (!store.currentNovelId) return
  novelSaving.value = true
  try {
    await projectApi.updateSettings(store.currentNovelId, novelSelectedIds.value)
    ElMessage.success('已更新本小说设定库')
    novelDialogVisible.value = false
    await loadNovelSettings()
  } catch (e) {
    ElMessage.error('保存失败：' + (e?.message || '未知错误'))
  } finally {
    novelSaving.value = false
  }
}

watch(() => store.currentNovelId, () => {
  activeTab.value = hasNovel.value ? 'novel' : 'template'
  if (hasNovel.value) loadNovelSettings()
})

onMounted(() => {
  loadTemplates()
  load()
})
</script>

<style scoped>
.sv { padding: 4px; }
.sv-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
  flex-wrap: wrap;
  gap: 10px;
}
.sv-title { font-size: 16px; font-weight: 600; display: flex; align-items: baseline; gap: 10px; }
.sv-sub { font-size: 13px; color: #909399; font-weight: 400; }
.sv-toolbar-actions { display: flex; gap: 10px; align-items: center; }
.sv-card { margin-bottom: 14px; }
.sv-hint { font-size: 12px; color: #909399; margin-top: 4px; }
.muted { color: #c0c4cc; }

/* 子标签页 */
.sv-tabs {
  display: flex;
  gap: 0;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.sv-tab {
  padding: 8px 18px;
  cursor: pointer;
  font-size: 14px;
  color: var(--el-text-color-regular);
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
}
.sv-tab:hover { color: var(--el-color-primary); }
.sv-tab-active {
  color: #ff4d4f;
  border-bottom-color: #ff4d4f;
  font-weight: 600;
}

/* 设定模板卡片网格 */
.sv-tpl-filter { margin-bottom: 12px; }
.sv-tpl-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.sv-tpl-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.sv-tpl-name { font-size: 15px; font-weight: 600; }
.sv-tpl-summary {
  color: #606266; font-size: 13px; min-height: 38px; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.sv-tpl-meta { display: flex; gap: 12px; color: #909399; font-size: 12px; margin: 6px 0; }
.sv-tpl-actions { display: flex; gap: 4px; }
.sv-tpl-content :deep(textarea) { font-family: Consolas, Menlo, monospace; font-size: 13px; line-height: 1.6; }

/* 旧版单条设定 */
.sv-legacy-alert { margin-bottom: 12px; }
.sv-legacy-toolbar { margin-bottom: 10px; }

/* 本小说设定库列表 */
.sv-novel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  font-weight: 500;
}
.sv-novel-list { display: flex; flex-direction: column; gap: 8px; }
.sv-novel-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: #fafafa;
  border-radius: 6px;
}
.sv-novel-name { font-weight: 500; min-width: 160px; }
.sv-novel-desc { color: #909399; font-size: 13px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 编辑弹窗 */
.sv-novel-edit-hint { color: #909399; font-size: 13px; margin-bottom: 10px; }
.sv-novel-edit-list { display: flex; flex-direction: column; gap: 6px; max-height: 320px; overflow-y: auto; }
.sv-novel-edit-item { width: 100%; margin: 0 !important; align-items: center; height: auto; }
.sv-novel-meta { color: #909399; font-size: 12px; margin-left: auto; }
.sv-novel-count { margin-top: 10px; text-align: right; color: #909399; font-size: 12px; }
</style>
