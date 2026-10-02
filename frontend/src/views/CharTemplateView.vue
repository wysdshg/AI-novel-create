<template>
  <div class="tpl-page">
    <div class="gr-subnav">
      <router-link :to="{ name: 'template' }" class="gr-tab">情节模板库</router-link>
      <router-link :to="{ name: 'template-char' }" class="gr-tab on">人物模板库</router-link>
      <router-link :to="{ name: 'global-ref', query: { tab: 'item' } }" class="gr-tab">物品库</router-link>
      <router-link :to="{ name: 'global-ref', query: { tab: 'skill' } }" class="gr-tab">技能库</router-link>
    </div>

    <div class="ctp-head">
      <div>
        <h2 class="tp-title">人物模板库</h2>
        <p class="tp-sub">
          从三本书 1636 个分档角色里提炼的人物功能模板（匿名化后入库）：
          12 维性格刻度 + 腔调记忆点 + 行为/关系模式。建卡时「功能位参考」自动从这里召回。
        </p>
      </div>
      <div class="tp-stats"><span class="tp-stat">共 <b>{{ list.length }}</b> 个</span></div>
    </div>

    <div class="tp-bar">
      <el-input v-model="keyword" class="tp-search" placeholder="按名称 / 功能位 / 描述过滤，如「隐忍」「师者」「反派」" clearable />
      <el-select v-model="bookFilter" placeholder="来源书" clearable style="width: 140px">
        <el-option v-for="b in allBooks" :key="b" :label="b" :value="b" />
      </el-select>
      <el-select v-model="gradeFilter" placeholder="档位" clearable style="width: 120px">
        <el-option v-for="g in [5, 4, 3, 2, 1]" :key="g" :label="`档${g} ${GRADE_NAME[g]}`" :value="g" />
      </el-select>
      <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 110px">
        <el-option label="现役" value="active" />
        <el-option label="已归档" value="archived" />
        <el-option label="草稿" value="draft" />
      </el-select>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-empty v-if="loading" description="加载中…" />
    <el-empty v-else-if="!shown.length" description="没有匹配的模板" />

    <div v-else class="ctp-grid">
      <div v-for="t in shown" :key="t.id" class="ctp-card" @click="openDetail(t)">
        <div class="ctp-card-head">
          <span class="ctp-name">{{ t.name }}</span>
          <span class="ctp-grade">{{ gradeOf(t) }}</span>
        </div>
        <p class="ctp-desc">{{ t.logline || cast(t).desc }}</p>
        <div class="ctp-chips">
          <span class="ctp-chip ctp-chip-slot">{{ cast(t).slot }}</span>
          <span class="ctp-chip">{{ cast(t).mode }}</span>
          <span class="ctp-chip" v-if="t.source_stats?.book">{{ t.source_stats.book }}</span>
          <span class="ctp-chip ctp-chip-dim" v-for="tr in topTraits(t)" :key="tr.k"
                :class="{ neg: tr.v < 0 }">{{ tr.label }} {{ tr.v > 0 ? '+' : '' }}{{ tr.v }}</span>
        </div>
        <div class="ctp-memo" v-if="cast(t).voice?.记忆点">💭 {{ cast(t).voice.记忆点 }}</div>
        <div class="ctp-src">原型：{{ t.source_stats?.source_name || '—' }}<span
          v-if="t.source_stats?.member_count > 1">（{{ t.source_stats.member_count }} 人聚合）</span></div>
      </div>
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawer" :title="detail?.name || '模板详情'" size="640px" destroy-on-close>
      <div v-if="detail" class="ctp-detail">
        <div class="ctp-d-row">
          <el-tag size="small" effect="plain" type="warning">{{ cast(detail).slot }}</el-tag>
          <el-tag size="small" effect="plain">{{ cast(detail).mode }}</el-tag>
          <el-tag v-for="g in detail.genre_tags || []" :key="g" size="small" effect="plain" type="info">{{ g }}</el-tag>
          <el-tag size="small" :type="detail.status === 'active' ? 'success' : 'info'" effect="plain">
            {{ detail.status === 'active' ? '现役' : detail.status === 'archived' ? '已归档' : '草稿' }}
          </el-tag>
        </div>

        <p class="ctp-d-desc">{{ cast(detail).desc }}</p>
        <p class="ctp-d-ranks" v-if="ranksText(detail)">位阶：{{ ranksText(detail) }}</p>

        <el-divider content-position="left">12 维性格刻度</el-divider>
        <div v-for="tr in allTraits(detail)" :key="tr.k" class="ctp-trait">
          <span class="ctp-trait-label">{{ tr.label }}</span>
          <div class="ctp-trait-bar">
            <div class="ctp-trait-fill" :class="{ neg: tr.v < 0 }"
                 :style="tr.v ? { left: tr.v < 0 ? 50 - Math.abs(tr.v) * 5 + '%' : '50%', width: Math.abs(tr.v) * 5 + '%' } : {}" />
            <div class="ctp-trait-zero" />
          </div>
          <span class="ctp-trait-val" :class="{ z: !tr.v }">{{ tr.v > 0 ? '+' + tr.v : tr.v }}</span>
        </div>
        <div v-if="basisText(detail)" class="ctp-basis">依据：{{ basisText(detail) }}</div>

        <template v-if="cast(detail).voice">
          <el-divider content-position="left">腔调</el-divider>
          <div class="ctp-voice">
            <p v-if="cast(detail).voice.语域"><b>语域</b>：{{ cast(detail).voice.语域 }}</p>
            <p v-if="cast(detail).voice.幽默类型"><b>幽默</b>：{{ cast(detail).voice.幽默类型 }}</p>
            <p v-if="cast(detail).voice.攻防模式"><b>攻防</b>：{{ cast(detail).voice.攻防模式 }}</p>
            <p v-if="cast(detail).voice.注意力偏向"><b>注意力</b>：{{ cast(detail).voice.注意力偏向 }}</p>
            <p v-if="cast(detail).voice.记忆点"><b>记忆点</b>：{{ cast(detail).voice.记忆点 }}</p>
          </div>
        </template>

        <el-divider content-position="left">行为模式</el-divider>
        <ul class="ctp-list">
          <li v-for="(p, i) in cast(detail).behavior_patterns || []" :key="i">{{ p }}</li>
          <li v-if="!(cast(detail).behavior_patterns || []).length" class="ctp-none">未观测</li>
        </ul>

        <el-divider content-position="left">关系模式（对类不对人）</el-divider>
        <ul class="ctp-list">
          <template v-for="(h, i) in cast(detail).relation_patterns || []" :key="i">
            <li v-if="typeof h === 'object'"><b>{{ h.target }}</b>：{{ h.pattern }}</li>
            <li v-else>{{ h }}</li>
          </template>
          <li v-if="!(cast(detail).relation_patterns || []).length" class="ctp-none">未观测</li>
        </ul>

        <el-divider content-position="left">来源</el-divider>
        <p class="ctp-src-line">
          原型：{{ detail.source_stats?.source_name || '—' }}
          <span v-if="detail.source_stats?.book">（{{ detail.source_stats.book }}·档{{ detail.source_stats.grade }}）</span>
          <span v-if="detail.source_stats?.member_count > 1">｜{{ detail.source_stats.member_count }} 人聚合</span>
        </p>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { plotTemplateApi } from '@/api/plotTemplate'

const GRADE_NAME = { 5: '全书级', 4: '小说级', 3: '卷级', 2: '篇章级', 1: '断续配角' }
const TRAIT_LABEL = {
  altruism: '利他↔自私', honor: '信义↔背信', mercy: '仁慈↔狠辣', resolve: '坚毅↔易摧',
  decisiveness: '果决↔犹豫', discipline: '自律↔放纵', risk: '冒险↔稳健', rationality: '理性↔冲动',
  guile: '城府↔直率', idealism: '理想↔务实', warmth: '热忱↔冷漠', dominance: '强势↔随和',
}

const list = ref([])
const loading = ref(false)
const keyword = ref('')
const bookFilter = ref('')
const gradeFilter = ref(null)
const statusFilter = ref('active')
const drawer = ref(false)
const detail = ref(null)

const cast = (t) => (t.structure?.cast || [])[0] || {}
const gradeOf = (t) => {
  const g = t.source_stats?.grade ?? (t.genre_tags || []).find((x) => /^档\d$/.test(x))
  return g !== undefined && g !== null ? `档${String(g).replace('档', '')}` : '—'
}
const topTraits = (t) => {
  const tr = cast(t).traits || {}
  return Object.entries(tr)
    .filter(([, v]) => Number(v))
    .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
    .slice(0, 4)
    .map(([k, v]) => ({ k, v: Number(v), label: TRAIT_LABEL[k] || k }))
}
const allTraits = (t) => {
  const tr = cast(t).traits || {}
  return Object.keys(TRAIT_LABEL).map((k) => ({ k, v: Number(tr[k] || 0), label: TRAIT_LABEL[k] }))
}
const ranksText = (t) => {
  const r = cast(t).ranks
  return Array.isArray(r) ? r.filter(Boolean).join(' / ') : (r || '')
}
const basisText = (t) => {
  const b = cast(t).trait_basis || {}
  const parts = Object.entries(b).map(([k, v]) => `${TRAIT_LABEL[k] || k}：${v}`)
  return parts.join('；')
}

const allBooks = computed(() => [...new Set(list.value.map((t) => t.source_stats?.book).filter(Boolean))])
const shown = computed(() => {
  const kw = keyword.value.trim()
  return list.value.filter((t) => {
    if (bookFilter.value && t.source_stats?.book !== bookFilter.value) return false
    if (gradeFilter.value && Number(t.source_stats?.grade) !== Number(gradeFilter.value)) return false
    if (statusFilter.value && (t.status || 'active') !== statusFilter.value) return false
    if (kw) {
      const c = cast(t)
      const blob = `${t.name} ${c.slot} ${c.desc} ${t.logline || ''} ${c.voice?.记忆点 || ''} ${(c.behavior_patterns || []).join(' ')}`
      if (!blob.includes(kw)) return false
    }
    return true
  })
})

async function load() {
  loading.value = true
  try {
    // scale=character 的模板全部拉回，状态在前端过滤（量大但轻）
    const r = await plotTemplateApi.list({ scale: 'character' })
    const data = r?.data ?? r
    list.value = Array.isArray(data) ? data : (data?.items || data || [])
  } finally {
    loading.value = false
  }
}
function openDetail(t) {
  detail.value = t
  drawer.value = true
}
onMounted(load)
</script>

<style scoped>
.tpl-page { padding: 16px 22px 40px; max-width: 1280px; margin: 0 auto; }
.gr-subnav { display: flex; gap: 8px; margin-bottom: 14px; }
.gr-tab { padding: 6px 16px; border-radius: 999px; background: #eef1f6; color: #4b5563; font-size: 13px; text-decoration: none; }
.gr-tab.on { background: #2563eb; color: #fff; font-weight: 600; }
.ctp-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 10px; }
.tp-title { font-size: 20px; margin: 0 0 4px; }
.tp-sub { color: #6b7280; font-size: 12.5px; margin: 0; max-width: 760px; line-height: 1.6; }
.tp-stat { color: #6b7280; font-size: 13px; }
.tp-stat b { color: #111; font-size: 16px; }
.tp-bar { display: flex; gap: 8px; margin: 10px 0 14px; flex-wrap: wrap; }
.tp-search { width: 320px; }
.ctp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(295px, 1fr)); gap: 12px; }
.ctp-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; cursor: pointer; transition: box-shadow .15s; display: flex; flex-direction: column; gap: 7px; }
.ctp-card:hover { box-shadow: 0 4px 14px rgba(0, 0, 0, .08); }
.ctp-card-head { display: flex; align-items: center; gap: 8px; }
.ctp-name { font-weight: 700; font-size: 15px; }
.ctp-grade { margin-left: auto; background: #ecfdf5; color: #15803d; border-radius: 999px; padding: 1px 9px; font-size: 11px; }
.ctp-desc { color: #374151; font-size: 12.5px; margin: 0; line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.ctp-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.ctp-chip { background: #f3f4f6; color: #4b5563; border-radius: 4px; padding: 1px 7px; font-size: 11px; }
.ctp-chip-slot { background: #dbeafe; color: #1d4ed8; }
.ctp-chip-dim { background: #eff6ff; color: #1e40af; }
.ctp-chip-dim.neg { background: #fff7ed; color: #c2410c; }
.ctp-memo { font-size: 12px; color: #92400e; background: #fffbeb; border-radius: 6px; padding: 3px 8px; }
.ctp-src { font-size: 11.5px; color: #9ca3af; margin-top: auto; }
.ctp-detail { font-size: 13px; }
.ctp-d-row { display: flex; gap: 6px; flex-wrap: wrap; }
.ctp-d-desc { color: #111827; line-height: 1.7; }
.ctp-d-ranks { color: #6b7280; }
.ctp-trait { display: flex; align-items: center; gap: 8px; margin: 5px 0; }
.ctp-trait-label { width: 96px; color: #374151; font-size: 12px; }
.ctp-trait-bar { flex: 1; background: #e5e7eb; border-radius: 3px; height: 12px; position: relative; }
.ctp-trait-fill { position: absolute; top: 0; bottom: 0; background: #3b82f6; border-radius: 3px; }
.ctp-trait-fill.neg { background: #f59e0b; }
.ctp-trait-zero { position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: #9ca3af; }
.ctp-trait-val { width: 34px; text-align: right; font-weight: 600; }
.ctp-trait-val.z { color: #9ca3af; font-weight: 400; }
.ctp-basis { color: #374151; font-size: 12px; background: #f9fafb; border-radius: 6px; padding: 6px 10px; margin-top: 6px; line-height: 1.6; }
.ctp-voice p { margin: 3px 0; color: #374151; }
.ctp-list { margin: 4px 0; padding-left: 18px; color: #374151; line-height: 1.7; }
.ctp-none { color: #9ca3af; }
.ctp-src-line { color: #6b7280; }
</style>
