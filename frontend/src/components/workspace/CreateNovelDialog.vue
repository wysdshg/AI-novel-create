<template>
  <el-dialog
    :model-value="modelValue"
    title="新建小说"
    width="520px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
    @open="onOpen"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
      <el-form-item label="小说名称" prop="name">
        <el-input v-model="form.name" maxlength="120" placeholder="如：剑来青云" />
      </el-form-item>

      <el-form-item label="类型">
        <el-select v-model="form.genre" placeholder="选择类型（可选）" clearable style="width: 100%">
          <el-option v-for="g in genres" :key="g" :label="g" :value="g" />
        </el-select>
      </el-form-item>

      <el-form-item label="简介">
        <el-input
          v-model="form.summary"
          type="textarea"
          :rows="3"
          maxlength="2000"
          show-word-limit
          placeholder="一句话概括世界观、主线或卖点（可选）"
        />
      </el-form-item>

      <el-form-item label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option label="草稿" value="draft" />
          <el-option label="写作中" value="writing" />
          <el-option label="暂停" value="paused" />
          <el-option label="已完结" value="finished" />
        </el-select>
      </el-form-item>

      <!-- 挑选全局参考资料：复制进新小说作为它的参考文档 -->
      <el-form-item label="参考资料">
        <el-button text type="primary" @click="globalVisible = !globalVisible">
          {{ globalVisible ? '收起' : '挑选全局参考资料' }}
        </el-button>
      </el-form-item>

      <el-collapse-transition>
        <div v-if="globalVisible" class="cnd-global">
          <div v-if="globalLoading" class="cnd-global-hint">加载全局参考资料中…</div>
          <el-empty v-else-if="!globalList.length" :image-size="48" description="全局池暂无参考资料，可在「参考资料」页上传" />
          <el-checkbox-group v-else v-model="selectedGlobalIds" class="cnd-global-list">
            <el-checkbox v-for="g in globalList" :key="g.id" :value="g.id" class="cnd-global-item">
              <span class="cnd-global-name">{{ g.filename }}</span>
              <span class="cnd-global-meta">{{ formatSize(g.size) }}</span>
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="globalList.length" class="cnd-global-count">
            已选 {{ selectedGlobalIds.length }} / {{ globalList.length }}
          </div>
        </div>
      </el-collapse-transition>

      <!-- 挑选设定库：选择后该小说对话只注入选中的设定 -->
      <el-form-item label="设定选项">
        <el-button text type="primary" @click="settingVisible = !settingVisible">
          {{ settingVisible ? '收起' : '挑选设定模板' }}
        </el-button>
        <span v-if="selectedSettingIds.length" class="cnd-setting-hint">已选 {{ selectedSettingIds.length }} 项</span>
      </el-form-item>

      <el-collapse-transition>
        <div v-if="settingVisible" class="cnd-setting">
          <div v-if="settingLoading" class="cnd-setting-hint">加载设定模板中…</div>
          <el-empty v-else-if="!settingTemplateList.length" :image-size="48" description="暂无可选设定模板，请在「设定库 → 设定模板」中创建" />
          <el-checkbox-group v-else v-model="selectedSettingIds" class="cnd-setting-list">
            <el-checkbox v-for="s in settingTemplateList" :key="s.id" :value="s.id" class="cnd-setting-item">
              <span class="cnd-setting-name">{{ s.name }}</span>
              <el-tag size="small" effect="plain" class="cnd-setting-cat">{{ s.genre }}</el-tag>
            </el-checkbox>
          </el-checkbox-group>
          <div v-if="settingTemplateList.length" class="cnd-setting-count">
            已选 {{ selectedSettingIds.length }} / {{ settingTemplateList.length }}（不选则全量注入）
          </div>
        </div>
      </el-collapse-transition>
    </el-form>

    <template #footer>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="onSubmit">创建</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { projectApi } from '@/api/projects'
import { globalReferenceApi } from '@/api/reference'
import { settingTemplateApi } from '@/api/setting'
import { useProjectStore } from '@/store/project'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'created'])
const store = useProjectStore()

const genres = ['玄幻', '都市', '悬疑', '历史', '科幻', '言情', '武侠', '其他']
const formRef = ref(null)
const submitting = ref(false)

const form = reactive({
  name: '',
  genre: '',
  summary: '',
  status: 'draft',
})

const rules = {
  name: [{ required: true, message: '请输入小说名称', trigger: 'blur' }],
}

// 全局参考资料挑选
const globalVisible = ref(false)
const globalLoading = ref(false)
const globalList = ref([])
const selectedGlobalIds = ref([])

// 设定库挑选
const settingVisible = ref(false)
const settingLoading = ref(false)
const settingTemplateList = ref([])
const selectedSettingIds = ref([])

async function loadGlobalRefs() {
  globalLoading.value = true
  try {
    globalList.value = await globalReferenceApi.list()
  } catch {
    globalList.value = []
  } finally {
    globalLoading.value = false
  }
}

async function loadSettingTemplates() {
  settingLoading.value = true
  try {
    settingTemplateList.value = await settingTemplateApi.list({})
  } catch {
    settingTemplateList.value = []
  } finally {
    settingLoading.value = false
  }
}

const onOpen = () => {
  form.name = ''
  form.genre = ''
  form.summary = ''
  form.status = 'draft'
  selectedGlobalIds.value = []
  selectedSettingIds.value = []
  globalVisible.value = false
  settingVisible.value = false
  formRef.value?.clearValidate?.()
  loadGlobalRefs()
  loadSettingTemplates()
}

function formatSize(bytes) {
  if (!bytes) return '0 B'
  const k = 1024
  if (bytes < k) return bytes + ' B'
  if (bytes < k * k) return (bytes / k).toFixed(1) + ' KB'
  return (bytes / (k * k)).toFixed(1) + ' MB'
}

const onSubmit = async () => {
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    const payload = {
      name: form.name.trim(),
      genre: form.genre || null,
      summary: form.summary.trim() || null,
      status: form.status,
      setting_ids: selectedSettingIds.value.length ? selectedSettingIds.value : undefined,
    }
    const novel = await projectApi.create(payload)
    store.addNovel(novel)
    store.selectNovel(novel.id)

    // 将选中的全局参考资料复制进新小说
    if (selectedGlobalIds.value.length) {
      try {
        await globalReferenceApi.importToProject(novel.id, selectedGlobalIds.value)
      } catch (e) {
        ElMessage.warning('小说已创建，但参考资料复制失败：' + (e?.message || '未知错误'))
      }
    }

    ElMessage.success(`已创建《${novel.name}》`)
    emit('update:modelValue', false)
    emit('created', novel)
  } catch (e) {
    ElMessage.error('创建失败：' + (e?.message || '未知错误'))
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.cnd-global {
  margin: 0 0 4px 80px;
  padding: 12px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  background: #fafafa;
}
.cnd-global-hint { color: #909399; font-size: 13px; }
.cnd-global-list { display: flex; flex-direction: column; gap: 8px; max-height: 280px; overflow: auto; padding-right: 4px; }
.cnd-global-item { width: 100%; margin: 0 !important; padding: 6px 0; min-height: 32px; display: flex; align-items: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cnd-global-name { font-weight: 500; }
.cnd-global-meta { color: #909399; margin-left: 8px; font-size: 12px; }
.cnd-global-count { margin-top: 8px; color: #909399; font-size: 12px; text-align: right; }

.cnd-setting {
  margin: 0 0 4px 80px;
  padding: 12px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
  background: #fafcfc;
}
.cnd-setting-hint { color: #409eff; font-size: 12px; margin-left: 8px; }
.cnd-setting-list { display: flex; flex-direction: column; gap: 8px; max-height: 280px; overflow: auto; padding-right: 4px; }
.cnd-setting-item { width: 100%; margin: 0 !important; padding: 6px 0; min-height: 32px; display: flex; align-items: center; }
.cnd-setting-name { font-weight: 500; }
.cnd-setting-cat { margin-left: 8px; font-size: 11px; }
.cnd-setting-count { margin-top: 8px; color: #909399; font-size: 12px; text-align: right; }
</style>
