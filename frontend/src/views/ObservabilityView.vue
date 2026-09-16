<template>
  <div class="obs-page">
    <div class="obs-head">
      <h2 class="obs-title">观测</h2>
      <p class="obs-sub">
        「用量」看钱花在哪（token 计量），「反馈」看你改了 AI 哪些地方 —— 那正是它反复做不好的地方。
      </p>
    </div>

    <div class="obs-bar">
      <el-radio-group v-model="tab" size="small">
        <el-radio-button value="usage">用量统计</el-radio-button>
        <el-radio-button value="feedback">反馈回流</el-radio-button>
      </el-radio-group>

      <template v-if="tab === 'usage'">
        <el-select v-model="days" size="small" style="width: 130px" @change="loadUsage">
          <el-option :value="7" label="最近 7 天" />
          <el-option :value="30" label="最近 30 天" />
          <el-option :value="90" label="最近 90 天" />
        </el-select>
      </template>

      <el-checkbox v-if="hasProject" v-model="onlyCurrent" size="small" @change="reload">
        只看当前作品
      </el-checkbox>

      <el-button size="small" text @click="reload">刷新</el-button>
    </div>

    <!-- ───────── 用量 ───────── -->
    <template v-if="tab === 'usage'">
      <div class="obs-cards">
        <div class="obs-card">
          <div class="obs-card-num">{{ usage.overall?.calls ?? 0 }}</div>
          <div class="obs-card-label">调用次数</div>
        </div>
        <div class="obs-card">
          <div class="obs-card-num">{{ fmtNum(usage.overall?.total_tokens) }}</div>
          <div class="obs-card-label">总 token</div>
        </div>
        <div class="obs-card">
          <div class="obs-card-num">{{ fmtNum(usage.overall?.prompt_tokens) }}</div>
          <div class="obs-card-label">输入 token</div>
        </div>
        <div class="obs-card">
          <div class="obs-card-num">{{ fmtNum(usage.overall?.completion_tokens) }}</div>
          <div class="obs-card-label">输出 token</div>
        </div>
        <div v-if="hasCache" class="obs-card">
          <div class="obs-card-num">{{ pct(usage.overall?.cache_hit_rate) }}</div>
          <div class="obs-card-label">缓存命中率</div>
        </div>
      </div>

      <div v-if="hasCache" class="obs-tip">
        缓存命中的输入 token 单价低得多（DeepSeek 系等）。
        本区间：命中 <b>{{ fmtNum(usage.overall?.cache_hit_tokens) }}</b> /
        未命中 <b>{{ fmtNum(usage.overall?.cache_miss_tokens) }}</b>。
        命中率只统计**厂商确实回传了缓存拆分**的调用，未回传的不计入分母（不假装有数据）。
      </div>

      <div v-if="usage.overall?.estimated_calls" class="obs-tip">
        其中 {{ usage.overall.estimated_calls }} 次为**字符估算**（厂商流式未回流 usage），非精确计量。
      </div>
      <div v-if="usage.overall?.failed_calls" class="obs-tip is-warn">
        有 {{ usage.overall.failed_calls }} 次调用失败 —— 失败同样消耗 token（prompt 已发出）。
      </div>

      <el-empty v-if="!usage.overall?.calls" description="还没有用量记录（生成一次章节即产生）" />

      <template v-else>
        <h3 class="obs-h3">按模型</h3>
        <el-table :data="usage.by_model" size="small" border>
          <el-table-column prop="model_name" label="模型" min-width="180" />
          <el-table-column prop="calls" label="次数" width="80" align="right" />
          <el-table-column label="输入" width="110" align="right">
            <template #default="{ row }">{{ fmtNum(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column label="输出" width="110" align="right">
            <template #default="{ row }">{{ fmtNum(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column label="合计" width="110" align="right">
            <template #default="{ row }"><b>{{ fmtNum(row.total_tokens) }}</b></template>
          </el-table-column>
          <el-table-column v-if="hasCacheIn(usage.by_model)" label="缓存命中率" width="110" align="right">
            <template #default="{ row }">{{ pct(row.cache_hit_rate) }}</template>
          </el-table-column>
        </el-table>

        <h3 class="obs-h3">按场景</h3>
        <el-table :data="usage.by_scene" size="small" border>
          <el-table-column prop="scene" label="场景" min-width="160" />
          <el-table-column prop="calls" label="次数" width="80" align="right" />
          <el-table-column label="合计 token" width="130" align="right">
            <template #default="{ row }">{{ fmtNum(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column v-if="hasCacheIn(usage.by_scene)" label="缓存命中率" width="110" align="right">
            <template #default="{ row }">{{ pct(row.cache_hit_rate) }}</template>
          </el-table-column>
        </el-table>

        <h3 class="obs-h3">按天（最近 30 天）</h3>
        <el-table :data="usage.trend" size="small" border max-height="280">
          <el-table-column prop="date" label="日期" width="130" />
          <el-table-column prop="calls" label="次数" width="80" align="right" />
          <el-table-column label="token" align="right">
            <template #default="{ row }">{{ fmtNum(row.total_tokens) }}</template>
          </el-table-column>
        </el-table>
      </template>
    </template>

    <!-- ───────── 反馈 ───────── -->
    <template v-else>
      <div class="obs-cards">
        <div class="obs-card">
          <div class="obs-card-num">{{ fbStats.total ?? 0 }}</div>
          <div class="obs-card-label">改动记录</div>
        </div>
        <div class="obs-card">
          <div class="obs-card-num">{{ pct(fbStats.avg_similarity) }}</div>
          <div class="obs-card-label">平均相似度</div>
        </div>
        <div class="obs-card">
          <div class="obs-card-num">{{ pct(fbStats.avg_change_ratio) }}</div>
          <div class="obs-card-label">平均改动幅度</div>
        </div>
      </div>

      <div v-if="fbStats.avg_change_ratio != null" class="obs-tip">
        改动幅度长期偏高 = AI 出的稿子离「能直接用」的标准还差得远；改动集中在某类句子时，就是提示词该改的信号。
      </div>

      <el-empty
        v-if="!feedbacks.length"
        description="还没有反馈记录 —— 在「章节列表」里编辑正文并保存后即会自动记下"
      />

      <div v-else class="obs-list">
        <div v-for="f in feedbacks" :key="f.id" class="obs-item">
          <div class="obs-item-head">
            <span class="obs-item-time">{{ fmtTime(f.created_at) }}</span>
            <el-tag size="small" type="info" effect="plain">{{ kindLabel(f.kind) }}</el-tag>
            <span class="obs-chip">改动幅度 {{ pct(f.detail?.change_ratio) }}</span>
            <span class="obs-chip">相似度 {{ pct(f.detail?.similarity) }}</span>
            <span class="obs-chip">
              长度 {{ f.detail?.before_len }} → {{ f.detail?.after_len }}
              <span :class="(f.detail?.delta_len || 0) < 0 ? 'obs-dn' : 'obs-up'">
                ({{ (f.detail?.delta_len || 0) > 0 ? '+' : '' }}{{ f.detail?.delta_len }})
              </span>
            </span>
          </div>
          <div class="obs-diff">
            <div class="obs-diff-col">
              <div class="obs-diff-label">AI 原文（开头）</div>
              <pre class="obs-diff-body">{{ f.detail?.sample_before || '—' }}</pre>
            </div>
            <div class="obs-diff-col">
              <div class="obs-diff-label">你改后（开头）</div>
              <pre class="obs-diff-body">{{ f.detail?.sample_after || '—' }}</pre>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useProjectStore } from '@/store/project'
import { feedbackApi, usageApi } from '@/api/observability'

const store = useProjectStore()
const hasProject = computed(() => !!store.currentNovelId)

const tab = ref('usage')
const days = ref(30)
const onlyCurrent = ref(false)
const loading = ref(false)

const usage = ref({})
const fbStats = ref({})
const feedbacks = ref([])

const projectFilter = computed(() =>
  onlyCurrent.value && store.currentNovelId ? { project_id: store.currentNovelId } : {}
)

// 缓存拆分只有部分厂商回传（DeepSeek 系给 prompt_cache_hit_tokens，多数厂商不给）。
// 后端在无数据时返回 cache_hit_rate = None（刻意不假装有数据）→ 前端据此整块隐藏，
// 避免用户看到一个永久「—」的卡片还以为坏了（2026-09-15，docs/08-B3）。
const hasCache = computed(() => usage.value?.overall?.cache_hit_rate != null)

// 单张聚合表是否该显示缓存列：**任一行**有缓存数据才显示。
// 理由：若整表都是「—」，那一列纯属噪声；若部分行有数据，则保留该列（缺数据的行显「—」才有信息量）。
function hasCacheIn(rows) {
  return Array.isArray(rows) && rows.some((r) => r?.cache_hit_rate != null)
}

function fmtNum(n) {
  if (n === null || n === undefined) return '—'
  return Number(n).toLocaleString('zh-CN')
}
function pct(v) {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(1)}%`
}
function fmtTime(s) {
  if (!s) return '—'
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
const kindLabel = (k) => ({ chapter_edit: '正文改动', direction_rejected: '走向被拒' }[k] || k)

async function loadUsage() {
  try {
    usage.value = (await usageApi.summary({ days: days.value, ...projectFilter.value })) || {}
  } catch (e) {
    ElMessage.error('加载用量失败')
  }
}

async function loadFeedback() {
  try {
    const [stats, list] = await Promise.all([
      feedbackApi.stats(projectFilter.value),
      feedbackApi.list({ limit: 100, ...projectFilter.value }),
    ])
    fbStats.value = stats || {}
    feedbacks.value = list || []
  } catch (e) {
    ElMessage.error('加载反馈失败')
  }
}

async function reload() {
  loading.value = true
  try {
    if (tab.value === 'usage') await loadUsage()
    else await loadFeedback()
  } finally {
    loading.value = false
  }
}

// 切 tab 时按需加载（不重复拉）
watch(tab, (t) => {
  if (t === 'usage' && !usage.value.overall) loadUsage()
  if (t === 'feedback' && !feedbacks.value.length) loadFeedback()
})

onMounted(loadUsage)
</script>

<style scoped>
.obs-page {
  padding: 16px 20px;
}
.obs-head {
  margin-bottom: 12px;
}
.obs-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0 0 4px;
}
.obs-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 0;
  line-height: 1.6;
}
.obs-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.obs-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.obs-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--el-bg-color);
}
.obs-card-num {
  font-size: 20px;
  font-weight: 600;
  line-height: 1.3;
}
.obs-card-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.obs-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 12px;
  line-height: 1.6;
}
.obs-tip.is-warn {
  color: var(--el-color-warning);
}
.obs-h3 {
  font-size: 13px;
  font-weight: 600;
  margin: 16px 0 8px;
}
.obs-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.obs-item {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px 14px;
}
.obs-item-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.obs-item-time {
  font-size: 13px;
  font-weight: 500;
}
.obs-chip {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
}
.obs-dn {
  color: var(--el-color-danger);
}
.obs-up {
  color: var(--el-color-success);
}
.obs-diff {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
@media (max-width: 900px) {
  .obs-diff {
    grid-template-columns: 1fr;
  }
}
.obs-diff-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.obs-diff-body {
  margin: 0;
  padding: 8px 10px;
  max-height: 180px;
  overflow: auto;
  background: var(--el-fill-color-lighter);
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
}
</style>
