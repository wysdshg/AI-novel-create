<template>
  <div class="plan-page">
    <!-- 页头 -->
    <div class="pv-head">
      <h2 class="pv-title">篇规划</h2>
      <p class="pv-sub">
        以「篇」（8~15 章）为单位做结构规划：模板检索生成计划 → 向量选角 →
        新角色引入单确认 → 篇间交接检查，拍板后逐章生成时按行注入任务。
      </p>
    </div>

    <!-- 选择条 -->
    <div class="pv-bar">
      <el-select
        v-model="novelId"
        placeholder="选择作品"
        style="width: 220px"
        @change="onNovelChange"
      >
        <el-option v-for="n in store.novels" :key="n.id" :label="n.name" :value="n.id" />
      </el-select>
      <el-select
        v-model="articleId"
        placeholder="选择篇"
        style="width: 260px"
        :disabled="!novelId"
        @change="loadAll"
      >
        <el-option v-for="a in articleOptions" :key="a.id" :label="a.label" :value="a.id" />
      </el-select>

      <el-button type="primary" :disabled="!novelId || !articleId" @click="genVisible = true">
        <el-icon><MagicStick /></el-icon> 生成计划
      </el-button>

      <template v-if="plan">
        <el-tag :type="plan.status === 'confirmed' ? 'success' : 'info'" effect="plain">
          {{ plan.status === 'confirmed' ? '已拍板' : '草稿' }}
        </el-tag>
        <el-tag v-for="t in plan.template_names" :key="t" effect="plain" type="warning" size="small">
          {{ t }}
        </el-tag>
        <span v-if="plan.origin" class="pv-origin">{{ originLabel(plan.origin) }}</span>
      </template>
    </div>

    <!-- 空态 -->
    <el-empty v-if="!novelId" description="先在左侧选择一本小说（或在此选择）" />
    <el-empty v-else-if="!articleOptions.length" description="当前作品还没有「篇」，先在侧栏的小说下添加" />
    <el-empty v-else-if="!articleId" description="选择一个篇，查看 / 生成它的章节计划" />
    <el-empty v-else-if="!plan" description="该篇还没有计划 —— 点上方「生成计划」开始">
      <el-button type="primary" @click="genVisible = true">生成计划</el-button>
    </el-empty>

    <template v-else>
      <!-- 连续性警告（7.3.5） -->
      <el-alert
        v-if="carryoverNames.length"
        type="warning"
        :closable="false"
        class="pv-alert"
      >
        <template #title>
          篇间交接：这些角色在上篇末尾在场，本篇计划里没有任何交代 ——
          建议三选一：交代离场 / 安排出场 / 确认忽略
        </template>
        <div class="pv-carry-list">
          <el-tag
            v-for="name in carryoverNames"
            :key="name"
            type="warning"
            effect="light"
            class="pv-carry-tag"
          >
            {{ name }}
            <span v-if="carryoverChapters[name] != null" class="pv-carry-ch">
              最后出场：第{{ carryoverChapters[name] }}章
            </span>
          </el-tag>
        </div>
      </el-alert>

      <!-- 回归理由材料包 -->
      <el-collapse v-if="reentryList.length" class="pv-reentry">
        <el-collapse-item :title="`回归理由材料包（${reentryList.length} 个蛰伏/离场角色被本篇召回）`">
          <div v-for="m in reentryList" :key="m.character_id" class="pv-reentry-card">
            <div class="pv-reentry-head">
              <b>{{ m.name }}</b>
              <el-tag size="small" :type="priorityType(m.priority)" effect="plain">
                {{ priorityLabel(m.priority) }}
              </el-tag>
              <span v-if="m.last_seen_chapter != null" class="pv-reentry-meta">
                最后出场：第{{ m.last_seen_chapter }}章
              </span>
            </div>
            <ul v-if="(m.foreshadows || []).length" class="pv-reentry-list">
              <li v-for="(f, i) in m.foreshadows" :key="'f' + i">
                【未回收伏笔】{{ f.description || f.summary || JSON.stringify(f) }}
              </li>
            </ul>
            <ul v-if="(m.world_events || []).length" class="pv-reentry-list">
              <li v-for="(w, i) in m.world_events" :key="'w' + i">
                【缺席期事件】{{ w.summary || JSON.stringify(w) }}
              </li>
            </ul>
            <div v-if="!(m.foreshadows || []).length && !(m.world_events || []).length" class="pv-reentry-none">
              暂无现成材料 —— 需要纯新编回归理由（质量最差，建议补伏笔后重算）
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>

      <!-- 计划表格 -->
      <div class="pv-section">
        <div class="pv-section-head">
          <h3 class="pv-section-title">章节计划（{{ plan.lines.length }} 行）</h3>
          <div class="pv-section-actions">
            <el-button
              :disabled="plan.status === 'confirmed'"
              @click="savePlan"
            >
              <el-icon><Check /></el-icon> 保存修改
            </el-button>
            <el-button
              type="primary"
              :disabled="plan.status === 'confirmed'"
              @click="confirmPlan"
            >
              <el-icon><Stamp /></el-icon> 拍板确认
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="droppedTips.length"
          type="info"
          :closable="true"
          class="pv-alert"
        >
          <template #title>
            超过每篇 3 个新角色限额，以下引入单被剔除：{{ droppedTips.join('、') }}
          </template>
        </el-alert>

        <el-table :data="rows" border stripe class="pv-table" size="small">
          <el-table-column label="#" width="46" align="center">
            <template #default="{ row }">{{ row.no }}</template>
          </el-table-column>
          <el-table-column label="节拍" width="120">
            <template #default="{ row }">
              <el-input v-model="row.beat" size="small" :disabled="locked" />
            </template>
          </el-table-column>
          <el-table-column label="剧情概要" min-width="260">
            <template #default="{ row }">
              <el-input
                v-model="row.summary"
                size="small"
                type="textarea"
                :rows="2"
                autosize
                :disabled="locked"
              />
            </template>
          </el-table-column>
          <el-table-column label="召回角色" width="150">
            <template #default="{ row }">
              <div class="pv-tags">
                <el-tag v-for="c in splitNames(row._recall)" :key="c" size="small" effect="plain">
                  {{ c }}
                </el-tag>
              </div>
              <el-input
                v-model="row._recall"
                size="small"
                placeholder="逗号分隔"
                :disabled="locked"
              />
            </template>
          </el-table-column>
          <el-table-column label="新角色" width="150">
            <template #default="{ row }">
              <div class="pv-tags">
                <el-tag v-for="c in splitNames(row._new)" :key="c" size="small" type="warning" effect="plain">
                  {{ c }}
                </el-tag>
              </div>
              <el-input
                v-model="row._new"
                size="small"
                placeholder="逗号分隔"
                :disabled="locked"
              />
            </template>
          </el-table-column>
          <el-table-column label="字数" width="90">
            <template #default="{ row }">
              <el-input-number v-model="row.target_words" size="small" :min="200" :step="100"
                               controls-position="right" :disabled="locked" style="width: 100%" />
            </template>
          </el-table-column>
          <el-table-column label="钩子" min-width="140">
            <template #default="{ row }">
              <el-input v-model="row.hook" size="small" :disabled="locked" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110" align="center" fixed="right">
            <template #default="{ row }">
              <el-button size="small" text type="primary" :loading="row._refining"
                         @click="refineRow(row)">
                AI 改这行
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 选角面板（7.3） -->
      <div class="pv-section">
        <div class="pv-section-head">
          <h3 class="pv-section-title">角色选角（{{ castings.length }} 个槽位）</h3>
          <div class="pv-section-actions">
            <el-button size="small" :loading="recomputing" @click="recomputeCasting">
              <el-icon><Refresh /></el-icon> 重算选角
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="unmatchedSlots.length"
          type="info"
          :closable="false"
          class="pv-alert"
        >
          <template #title>
            {{ unmatchedSlots.length }} 个槽位没有匹配到角色（{{ unmatchedSlots.join('、') }}）——
            可在下方手动指定，或与「新角色引入单」关联（引入单是来补这些空位的）
          </template>
        </el-alert>

        <div class="pv-cast-grid">
          <div
            v-for="c in castings"
            :key="c.id"
            class="pv-cast-card"
            :class="{ 'is-empty': !c.character_id, 'is-manual': c.source === 'manual' }"
          >
            <div class="pv-cast-slot">
              <b>{{ c.slot }}</b>
              <el-tag v-if="c.source === 'manual'" size="small" type="success" effect="plain">手改</el-tag>
              <el-tag v-if="c.needs_reentry_note" size="small" type="warning" effect="plain">
                需回归理由
              </el-tag>
            </div>
            <div class="pv-cast-desc">{{ c.slot_desc || '—' }}</div>
            <div class="pv-cast-role">
              <template v-if="c.character_name">
                <span class="pv-cast-name">{{ c.character_name }}</span>
                <el-button size="small" text type="danger" @click="clearSlot(c)">清空</el-button>
              </template>
              <span v-else class="pv-cast-name is-none">未匹配</span>
            </div>
            <div v-if="c.score != null" class="pv-cast-score">
              <div class="pv-cast-score-bar">
                <div class="pv-cast-score-fill" :style="scoreStyle(c.score)"></div>
                <div class="pv-cast-score-thresh" title="自动匹配阈值 0.58"></div>
              </div>
              <span class="pv-cast-score-num">{{ c.score.toFixed(3) }}</span>
            </div>
            <el-select
              :model-value="c.character_id || ''"
              size="small"
              filterable
              placeholder="手动指定角色"
              class="pv-cast-select"
              @change="(cid) => setSlot(c, cid)"
            >
              <el-option label="（清空槽位）" value="" />
              <el-option v-for="ch in characters" :key="ch.id" :label="ch.name" :value="ch.id" />
            </el-select>
          </div>
          <el-empty v-if="!castings.length" description="没有选角记录（老模板没有 cast 槽位，或计划生成时未匹配）" />
        </div>
      </div>

      <!-- 新角色引入单面板（7.3.5） -->
      <div class="pv-section">
        <div class="pv-section-head">
          <h3 class="pv-section-title">新角色引入单（{{ plannedChars.length }} 条）</h3>
          <el-radio-group v-model="pcFilter" size="small">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="pending">待确认</el-radio-button>
            <el-radio-button value="confirmed">已建卡</el-radio-button>
            <el-radio-button value="dismissed">已忽略</el-radio-button>
          </el-radio-group>
        </div>
        <el-alert
          v-if="unboundPlanned.length"
          type="warning"
          :closable="false"
          class="pv-alert"
        >
          <template #title>
            {{ unboundPlanned.length }} 条引入单未绑定功能位 —— 绑定后才知道「这个新角色是来干什么的」
          </template>
        </el-alert>

        <el-table :data="filteredPlanned" border size="small" class="pv-table">
          <el-table-column label="角色名" width="130">
            <template #default="{ row }">
              <b>{{ row.name }}</b>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="pcStatusType(row.status)" effect="plain">
                {{ pcStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="绑定功能位" min-width="180">
            <template #default="{ row }">
              <el-select
                :model-value="row.slot || ''"
                size="small"
                :disabled="row.status !== 'pending'"
                placeholder="选择槽位（或留空）"
                @change="(slot) => bindSlot(row, slot)"
              >
                <el-option label="（不绑定）" value="" />
                <el-option v-for="s in slotOptions" :key="s" :label="s" :value="s" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="首登场" width="110" align="center">
            <template #default="{ row }">
              <span>第 {{ row.first_appearance }} 行</span>
              <el-tooltip content="篇内行号（该章尚未写出，无全局章号）；建卡后真实出场章由写后摄取派生" placement="top">
                <el-icon class="pv-q"><QuestionFilled /></el-icon>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column label="角色卡回链" width="110" align="center">
            <template #default="{ row }">
              <span v-if="row.character_id" class="pv-linked">已建卡</span>
              <span v-else class="pv-unlinked">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170" align="center" fixed="right">
            <template #default="{ row }">
              <template v-if="row.status === 'pending'">
                <el-button size="small" type="primary" @click="openConfirmChar(row)">确认建卡</el-button>
                <el-button size="small" text type="info" @click="dismissPlanned(row)">忽略</el-button>
              </template>
              <el-button
                v-else-if="row.status === 'dismissed'"
                size="small"
                text
                @click="restorePlanned(row)"
              >
                恢复待确认
              </el-button>
              <span v-else class="pv-linked">—</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>

    <!-- 生成计划弹窗 -->
    <el-dialog v-model="genVisible" title="生成本篇章计划" width="520px">
      <el-form label-width="90px">
        <el-form-item label="口述要求">
          <el-input v-model="genForm.hint" type="textarea" :rows="3"
                    placeholder="可选。最高优先级，例如：主角在这篇要突破，反派第一次正面出场" />
        </el-form-item>
        <el-form-item label="章数">
          <el-input-number v-model="genForm.n_chapters" :min="2" :max="40" />
        </el-form-item>
        <el-form-item label="跳过模板">
          <el-switch v-model="genForm.force_free" />
          <span class="pv-gen-tip">开启后不做模板检索，完全自由规划</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="genVisible = false">取消</el-button>
        <el-button type="primary" :loading="generating" @click="doGenerate">
          {{ generating ? '生成中（模型规划，约 1~2 分钟）…' : '开始生成' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 确认建卡弹窗 -->
    <el-dialog v-model="confirmCharVisible" title="确认引入 → 建卡进角色库" width="520px">
      <div class="pv-confirm-name">
        新角色：<b>{{ confirmingRow?.name }}</b>
        <template v-if="confirmingRow?.slot">（{{ confirmingRow.slot }}）</template>
      </div>
      <el-form label-width="90px">
        <el-form-item label="角色类型">
          <el-input v-model="confirmForm.role_type" placeholder="配角 / 反派 / 引路人…（默认配角）" />
        </el-form-item>
        <el-form-item label="性格">
          <el-input v-model="confirmForm.personality" placeholder="可选" />
        </el-form-item>
        <el-form-item label="背景">
          <el-input v-model="confirmForm.background" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
        <el-form-item label="简介">
          <el-input
            v-model="confirmForm.brief"
            type="textarea" :rows="2"
            :placeholder="confirmingRow?.slot ? `补「${confirmingRow.slot}」功能位的新角色` : '可选'"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="confirmCharVisible = false">取消</el-button>
        <el-button type="primary" :loading="confirming" @click="doConfirmChar">确认建卡</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProjectStore } from '@/store/project'
import { planApi } from '@/api/plan'
import { castingApi } from '@/api/casting'
import { characterApi } from '@/api/database'

const store = useProjectStore()

// —— 选择状态（与 store 双向同步：进页面带出侧栏已选的小说/篇） ——
const novelId = ref(store.currentNovelId || '')
const articleId = ref(store.currentArticle?.id || '')

const plan = ref(null)
const castings = ref([])
const plannedChars = ref([])
const characters = ref([])          // 本书角色（手改选角 / 确认建卡回显用）
const pcFilter = ref('all')

const generating = ref(false)
const recomputing = ref(false)
const confirming = ref(false)
const genVisible = ref(false)
const confirmCharVisible = ref(false)
const confirmingRow = ref(null)
const droppedTips = ref([])

const genForm = reactive({ hint: '', n_chapters: 8, force_free: false })
const confirmForm = reactive({ role_type: '', personality: '', background: '', brief: '' })

const MIN_SCORE = 0.58   // 与后端 casting_crud 同源（标定表 outputs/选角阈值标定.md）

const locked = computed(() => plan.value?.status === 'confirmed')

// 篇选项：当前小说结构下的所有篇（跨卷）
const articleOptions = computed(() => {
  const out = []
  for (const vol of store.structure.volumes || []) {
    for (const a of vol.articles || []) out.push({ id: a.id, label: `${vol.name} / ${a.name}` })
  }
  return out
})

// —— 连续性数据（7.3.5，plan JSON 里带出） ——
const carryoverNames = computed(() => plan.value?.carryover?.carryover_names || [])
const carryoverChapters = computed(() => {
  const map = {}
  for (const c of plan.value?.carryover?.last_chapters || []) {
    if (c?.name) map[c.name] = c.chapter_no
  }
  return map
})
const reentryList = computed(() => plan.value?.reentry_materials || [])

// —— casting 派生 ——
const unmatchedSlots = computed(() =>
  castings.value.filter((c) => !c.character_id).map((c) => c.slot)
)
const slotOptions = computed(() => castings.value.map((c) => c.slot))
const unboundPlanned = computed(() =>
  plannedChars.value.filter((p) => p.status === 'pending' && !p.slot)
)
const filteredPlanned = computed(() =>
  pcFilter.value === 'all'
    ? plannedChars.value
    : plannedChars.value.filter((p) => p.status === pcFilter.value)
)

// —— 计划行编辑态（deep copy + 逗号分隔辅助字段） ——
const rows = ref([])
function hydrateRows(lines) {
  rows.value = (lines || []).map((l) => ({
    ...l,
    _new: (l.new_chars || []).join('，'),
    _recall: (l.recall_chars || []).join('，'),
    _refining: false,
  }))
}
function collectRows() {
  return rows.value.map((r) => ({
    no: r.no,
    beat: (r.beat || '').trim(),
    summary: (r.summary || '').trim(),
    new_chars: splitNames(r._new),
    recall_chars: splitNames(r._recall),
    target_words: r.target_words || 2000,
    hook: (r.hook || '').trim(),
    template_ref: r.template_ref || '',
  }))
}
const splitNames = (s) =>
  String(s || '').split(/[，,、]/).map((x) => x.trim()).filter(Boolean)

// —— 加载 ——
async function loadAll() {
  if (!novelId.value || !articleId.value) {
    plan.value = null
    castings.value = []
    plannedChars.value = []
    return
  }
  try {
    const [p, c, pc, ch] = await Promise.all([
      planApi.get(novelId.value, articleId.value),
      castingApi.list(novelId.value, articleId.value),
      planApi.listPlannedChars(novelId.value, articleId.value),
      characterApi.list(novelId.value),
    ])
    plan.value = p
    hydrateRows(p?.lines)
    castings.value = Array.isArray(c) ? c : (c?.castings || [])
    plannedChars.value = Array.isArray(pc) ? pc : (pc?.items || [])
    characters.value = Array.isArray(ch) ? ch : (ch?.items || [])
    droppedTips.value = []
  } catch { /* 拦截器已报错 */ }
}

async function onNovelChange(id) {
  if (id !== store.currentNovelId) {
    await store.selectNovel(id)
  }
  articleId.value = ''
  plan.value = null
  castings.value = []
  plannedChars.value = []
}

onMounted(() => {
  // 侧栏已选小说时确保结构已加载（刷新页面后 store 从 localStorage 恢复，结构可能还没拉）
  if (novelId.value) {
    store.loadStructure(novelId.value).finally(() => {
      if (articleId.value) loadAll()
    })
  }
})

// —— 生成 ——
async function doGenerate() {
  generating.value = true
  try {
    await planApi.generate(novelId.value, articleId.value, {
      hint: genForm.hint,
      n_chapters: genForm.n_chapters,
      force_free: genForm.force_free,
    })
    genVisible.value = false
    genForm.hint = ''
    await loadAll()
    ElMessage.success('计划已生成 —— 记得检查选角与引入单后拍板')
  } catch { /* 拦截器已报错 */ } finally {
    generating.value = false
  }
}

// —— 行级编辑 / 拍板 ——
async function savePlan() {
  try {
    const r = await planApi.save(novelId.value, articleId.value, collectRows(), plan.value.notes)
    droppedTips.value = r?.new_chars_dropped || []
    await loadAll()
    ElMessage.success('已保存')
  } catch { /* 拦截器已报错 */ }
}

async function confirmPlan() {
  try {
    await ElMessageBox.confirm(
      '拍板后计划进入 confirmed 状态，逐章生成时按行注入任务；表格将锁定（需再改动请另起计划）。',
      '拍板确认',
      { type: 'warning', confirmButtonText: '拍板', cancelButtonText: '再看看' }
    )
  } catch { return }
  try {
    await planApi.confirm(novelId.value, articleId.value)
    await loadAll()
    ElMessage.success('已拍板')
  } catch { /* 拦截器已报错 */ }
}

async function refineRow(row) {
  let instruction = ''
  try {
    const r = await ElMessageBox.prompt(
      `对第 ${row.no} 行的修改要求（只改这一行，其余行原样）`,
      'AI 改这行',
      { inputPlaceholder: '例：把冲突强度加大，加入反派第一次正面出手', inputPattern: /\S+/,
        inputErrorMessage: '要求不能为空' }
    )
    instruction = r?.value?.trim()
  } catch { return }
  row._refining = true
  try {
    const res = await planApi.refineLine(novelId.value, articleId.value, row.no, instruction)
    const nl = res?.line || {}
    row.beat = nl.beat || row.beat
    row.summary = nl.summary || row.summary
    row.hook = nl.hook || row.hook
    row.target_words = nl.target_words || row.target_words
    row._new = (nl.new_chars || []).join('，')
    row._recall = (nl.recall_chars || []).join('，')
    droppedTips.value = res?.new_chars_dropped || []
    if (droppedTips.value.length) ElMessage.warning('该行新角色超出限额被剔除，见顶部提示')
    ElMessage.success(`第 ${row.no} 行已按要求修改`)
  } catch { /* 拦截器已报错 */ } finally {
    row._refining = false
  }
}

// —— 选角 ——
async function recomputeCasting() {
  recomputing.value = true
  try {
    const r = await castingApi.recompute(novelId.value, articleId.value)
    castings.value = Array.isArray(r) ? r : (r?.castings || [])
    ElMessage.success('选角已重算（manual 槽位未被覆盖）')
  } catch { /* 拦截器已报错 */ } finally {
    recomputing.value = false
  }
}

async function setSlot(c, characterId) {
  try {
    const r = await castingApi.set(novelId.value, articleId.value, c.slot, characterId || null)
    const updated = r
    const i = castings.value.findIndex((x) => x.id === c.id)
    if (i >= 0) castings.value[i] = { ...castings.value[i], ...(updated || {}) }
    ElMessage.success('已手改（重算不会覆盖）')
  } catch { /* 拦截器已报错 */ }
}

async function clearSlot(c) {
  await setSlot(c, null)
}

function scoreStyle(score) {
  const pct = Math.max(0, Math.min(1, score)) * 100
  return {
    width: pct + '%',
    background: score >= MIN_SCORE ? '#67c23a' : '#e6a23c',
  }
}

// —— 引入单 ——
async function bindSlot(row, slot) {
  try {
    const r = await planApi.updatePlannedChar(novelId.value, articleId.value, row.id, { slot: slot || null })
    Object.assign(row, r || {})
    ElMessage.success(slot ? `已绑定「${slot}」` : '已解除绑定')
  } catch { /* 拦截器已报错 */ }
}

async function dismissPlanned(row) {
  try {
    await planApi.updatePlannedChar(novelId.value, articleId.value, row.id, { status: 'dismissed' })
    row.status = 'dismissed'
    ElMessage.success('已忽略（可在「已忽略」里恢复）')
  } catch { /* 拦截器已报错 */ }
}

async function restorePlanned(row) {
  try {
    await planApi.updatePlannedChar(novelId.value, articleId.value, row.id, { status: 'pending' })
    row.status = 'pending'
  } catch { /* 拦截器已报错 */ }
}

function openConfirmChar(row) {
  confirmingRow.value = row
  confirmForm.role_type = ''
  confirmForm.personality = ''
  confirmForm.background = ''
  confirmForm.brief = ''
  confirmCharVisible.value = true
}

async function doConfirmChar() {
  confirming.value = true
  try {
    const r = await planApi.confirmPlannedChar(
      novelId.value, articleId.value, confirmingRow.value.id, { ...confirmForm })
    confirmingRow.value.status = 'confirmed'
    confirmingRow.value.character_id = r?.character_id || confirmingRow.value.character_id
    confirmCharVisible.value = false
    if (r?.skipped) {
      ElMessage.info('同名角色已存在：直接回链，不重复建卡')
    } else {
      ElMessage.success(`「${confirmingRow.value.name}」已建卡进角色库`)
    }
    // 建卡后角色列表多了新人 → 刷新（选角下拉要用）
    characterApi.list(novelId.value).then((ch) => {
      characters.value = Array.isArray(ch) ? ch : (ch?.items || [])
    }).catch(() => {})
  } catch { /* 拦截器已报错 */ } finally {
    confirming.value = false
  }
}

// —— 展示辅助 ——
const originLabel = (o) => ({ template: '模板规划', hybrid: '模板+自由', free: '自由规划' }[o] || o)
const priorityLabel = (p) => ({ foreshadow: '伏笔回收', world_events: '缺席期事件', fallback: '纯新编' }[p] || p || '—')
const priorityType = (p) => ({ foreshadow: 'success', world_events: 'primary', fallback: 'info' }[p] || 'info')
const pcStatusLabel = (s) => ({ pending: '待确认', confirmed: '已建卡', dismissed: '已忽略' }[s] || s)
const pcStatusType = (s) => ({ pending: 'warning', confirmed: 'success', dismissed: 'info' }[s] || 'info')
</script>

<style scoped>
.plan-page { max-width: 1280px; margin: 0 auto; }
.pv-head { margin-bottom: 12px; }
.pv-title { margin: 0 0 4px; font-size: 20px; }
.pv-sub { margin: 0; color: #909399; font-size: 13px; line-height: 1.6; }

.pv-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
.pv-origin { color: #909399; font-size: 12px; }

.pv-alert { margin-bottom: 12px; }
.pv-carry-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.pv-carry-tag { display: inline-flex; align-items: center; gap: 6px; }
.pv-carry-ch { color: #b88230; font-size: 11px; }

.pv-reentry { margin-bottom: 12px; }
.pv-reentry-card { padding: 8px 4px; border-bottom: 1px dashed var(--el-border-color-lighter); }
.pv-reentry-card:last-child { border-bottom: none; }
.pv-reentry-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.pv-reentry-meta { color: #909399; font-size: 12px; }
.pv-reentry-list { margin: 4px 0; padding-left: 18px; color: #606266; font-size: 13px; line-height: 1.7; }
.pv-reentry-none { color: #c0c4cc; font-size: 12px; font-style: italic; }

.pv-section { margin-top: 20px; }
.pv-section-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px;
}
.pv-section-title { margin: 0; font-size: 15px; }
.pv-section-actions { display: flex; gap: 8px; }

.pv-table { width: 100%; }
.pv-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 4px; min-height: 18px; }

/* 选角卡片 */
.pv-cast-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.pv-cast-card {
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
  padding: 12px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pv-cast-card.is-empty { border-style: dashed; background: #fafafa; }
.pv-cast-card.is-manual { border-color: #b3e19d; }
.pv-cast-slot { display: flex; align-items: center; gap: 6px; }
.pv-cast-desc {
  color: #909399; font-size: 12px; line-height: 1.5;
  min-height: 18px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.pv-cast-role { display: flex; align-items: center; justify-content: space-between; }
.pv-cast-name { font-size: 14px; }
.pv-cast-name.is-none { color: #c0c4cc; font-style: italic; }
.pv-cast-score { display: flex; align-items: center; gap: 8px; }
.pv-cast-score-bar {
  position: relative; flex: 1; height: 8px;
  background: #f0f2f5; border-radius: 4px; overflow: hidden;
}
.pv-cast-score-fill { height: 100%; border-radius: 4px; transition: width .3s; }
.pv-cast-score-thresh {
  position: absolute; left: 58%; top: -2px; bottom: -2px; width: 2px;
  background: #f56c6c; opacity: .55;
}
.pv-cast-score-num { font-size: 12px; color: #606266; width: 44px; text-align: right; }
.pv-cast-select { width: 100%; }

/* 引入单 */
.pv-q { color: #c0c4cc; margin-left: 4px; cursor: help; }
.pv-linked { color: #67c23a; font-size: 12px; }
.pv-unlinked { color: #c0c4cc; }

/* 生成 / 确认弹窗 */
.pv-gen-tip { margin-left: 10px; color: #c0c4cc; font-size: 12px; }
.pv-confirm-name { margin-bottom: 12px; color: #606266; }
</style>
