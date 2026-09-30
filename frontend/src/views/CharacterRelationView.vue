<template>
  <div class="cr">
    <div class="cr-toolbar">
      <div class="cr-title">
        关系网
        <span class="cr-novel">当前作品：{{ store.currentNovel?.name || '未选择' }}</span>
      </div>
      <div class="cr-actions">
        <el-tag v-if="characters.length" type="info" effect="plain">
          {{ characters.length }} 个角色 · {{ graphEdges.length }} 条关系
        </el-tag>
        <el-button :icon="RefreshRight" @click="relayout">重新布局</el-button>
        <el-button :icon="Share" @click="goCharacters">去角色库</el-button>
      </div>
    </div>

    <el-empty
      v-if="!loading && characters.length === 0"
      description="当前作品还没有角色，先在「角色库」里添加角色吧"
    >
      <el-button type="primary" :icon="Plus" @click="goCharacters">去角色库添加</el-button>
    </el-empty>

    <template v-else>
      <!-- 关系连线模式提示 -->
      <div v-if="linkingSourceId" class="cr-link-hint">
        请点击目标角色以建立关系（Esc 取消）
        <el-button text size="small" @click="cancelLink">取消</el-button>
      </div>

      <div class="cr-body" v-loading="loading">
        <div class="cr-canvas-wrap">
          <svg
            ref="svgEl"
            class="cr-svg"
            :class="{ grabbing: isDragging }"
            :viewBox="viewBoxStr"
            preserveAspectRatio="xMidYMid meet"
            @mousedown="onMouseDown"
            @mousemove="onMouseMove"
            @mouseup="onMouseUp"
            @mouseleave="onMouseUp"
            @wheel.prevent="onWheel"
            @click="onSvgBlankClick"
            @contextmenu.prevent="onSvgRightClick"
          >
            <defs>
              <pattern id="cr-dots" width="26" height="26" patternUnits="userSpaceOnUse">
                <circle cx="1.5" cy="1.5" r="1" fill="#e9edf2" />
              </pattern>
            </defs>

            <!-- 背景 -->
            <rect :x="vb.x" :y="vb.y" :width="vb.w" :height="vb.h" fill="#fbfcfe" />
            <rect :x="vb.x" :y="vb.y" :width="vb.w" :height="vb.h" fill="url(#cr-dots)" />

            <!-- 图内容：可平移/缩放 -->
            <g class="cr-content" :transform="contentTransform">
              <!-- 关系边 -->
              <g
                v-for="e in edges"
                :key="'e' + e.id"
                class="cr-edge"
                :class="{ active: isEdgeSelected(e.id) }"
                @click.stop="onEdgeClick(e)"
                @contextmenu.stop.prevent="onEdgeRightClick(e)"
              >
                <line
                  :x1="e.x1" :y1="e.y1" :x2="e.x2" :y2="e.y2"
                  :stroke="relColor(e.relation_type)"
                  :stroke-width="1.5 + e.strength / 20"
                  :stroke-opacity="isEdgeSelected(e.id) ? 1 : 0.72"
                />
                <polygon :points="e.arrow" :fill="relColor(e.relation_type)" />
                <text :x="e.mx" :y="e.my - 4" text-anchor="middle" class="cr-edge-label">
                  {{ e.relation_type }}
                </text>
              </g>

              <!-- 角色节点 -->
              <g
                v-for="n in characters"
                :key="n.id"
                class="cr-node"
                :class="{ active: isNodeSelected(n.id), dragging: draggingNodeId === n.id, linking: linkingSourceId === n.id }"
                :transform="`translate(${pos[n.id]?.x || 0},${pos[n.id]?.y || 0})`"
                @mousedown.stop="onNodeMouseDown($event, n)"
                @click.stop="onNodeClick(n)"
                @dblclick.stop="onNodeDblClick(n)"
                @contextmenu.stop.prevent="onNodeRightClick($event, n)"
              >
                <circle
                  :r="NODE_R"
                  :fill="roleColor(n.role_type)"
                  :stroke="isNodeSelected(n.id) ? '#303133' : '#fff'"
                  :stroke-width="isNodeSelected(n.id) ? 3 : 2"
                />
                <text :y="NODE_R + 16" text-anchor="middle" class="cr-node-label" :class="{ active: isNodeSelected(n.id) }">
                  {{ n.name }}
                </text>
              </g>
            </g>
          </svg>

          <!-- 视图控制浮层 -->
          <div class="cr-controls">
            <el-button-group>
              <el-button :icon="ZoomIn" title="放大" @click="zoomBy(1.2)" />
              <el-button :icon="ZoomOut" title="缩小" @click="zoomBy(1 / 1.2)" />
              <el-button :icon="RefreshRight" title="重置视图" @click="resetView" />
            </el-button-group>
            <div class="cr-zoom-info">{{ zoomPercent }}%</div>
          </div>

          <!-- 图例 -->
          <div class="cr-legend">
            <div class="cr-legend-title">地位</div>
            <div class="cr-legend-row" v-for="r in roleLegend" :key="r.label">
              <span class="cr-dot" :style="{ background: r.color }"></span>{{ r.label }}
            </div>
            <div class="cr-legend-title" style="margin-top: 6px">关系</div>
            <div class="cr-legend-row" v-for="r in relLegend" :key="r.label">
              <span class="cr-line" :style="{ background: r.color }"></span>{{ r.label }}
            </div>
          </div>

          <p class="cr-canvas-hint">
            提示：滚轮缩放，拖拽空白处平移；拖动角色可调整布局并自动保存。单击查看详情，双击编辑，右键角色可新建关系 / 编辑 / 删除，右键空白新建角色。
          </p>
        </div>

        <!-- 右侧详情面板 -->
        <div class="cr-detail" v-if="selectedNode">
          <div class="cr-detail-head">
            <span class="cr-detail-name">{{ selectedNode.name }}</span>
            <el-tag v-if="selectedNode.role_type" size="small" :type="roleTag(selectedNode.role_type)" effect="light">
              {{ selectedNode.role_type }}
            </el-tag>
          </div>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="性别">{{ selectedNode.gender || '—' }}</el-descriptions-item>
            <el-descriptions-item label="年龄">{{ selectedNode.age ?? '—' }}</el-descriptions-item>
            <el-descriptions-item label="当前等级">{{ selectedNode.current_level || '—' }}</el-descriptions-item>
          </el-descriptions>

          <div class="cr-detail-block" v-if="selectedNode.personality">
            <div class="cr-detail-sub">性格</div>
            <p class="cr-detail-text">{{ selectedNode.personality }}</p>
          </div>
          <div class="cr-detail-block" v-if="selectedNode.brief">
            <div class="cr-detail-sub">简介</div>
            <p class="cr-detail-text">{{ selectedNode.brief }}</p>
          </div>

          <div class="cr-detail-block" v-if="incidentRelations.length">
            <div class="cr-detail-sub">关系（{{ incidentRelations.length }}）</div>
            <div
              v-for="r in incidentRelations"
              :key="r.id"
              class="cr-rel-item"
              :class="{ active: isEdgeSelected(r.id) }"
              @click="selectEdge(r.id)"
            >
              <span class="cr-rel-dir">{{ r.dir }}</span>
              <span class="cr-rel-other">{{ r.other }}</span>
              <el-tag size="small" effect="plain" :color="relColor(r.type)" style="color:#fff;border:none">
                {{ r.type }}
              </el-tag>
              <span class="cr-rel-strength">强度 {{ r.strength }}</span>
            </div>
          </div>

          <div class="cr-detail-actions">
            <el-button text size="small" @click="selected.kind = null; selected.id = null">收起</el-button>
            <el-button type="primary" text size="small" :icon="Edit" @click="openEditCharacter(selectedNode)">编辑</el-button>
          </div>
        </div>

        <div class="cr-detail" v-else-if="selectedEdge">
          <div class="cr-detail-head">
            <span class="cr-detail-name">关系</span>
            <el-tag size="small" effect="light">{{ selectedEdge.relation_type }}</el-tag>
          </div>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="主体">{{ charName(selectedEdge.subject_id) }}</el-descriptions-item>
            <el-descriptions-item label="客体">{{ charName(selectedEdge.object_id) }}</el-descriptions-item>
            <el-descriptions-item label="强度">
              <el-progress :percentage="selectedEdge.strength" :stroke-width="10" />
            </el-descriptions-item>
          </el-descriptions>
          <div class="cr-detail-block" v-if="selectedEdge.note">
            <div class="cr-detail-sub">备注</div>
            <p class="cr-detail-text">{{ selectedEdge.note }}</p>
          </div>
          <div class="cr-detail-actions">
            <el-button text size="small" @click="selected.kind = null; selected.id = null">收起</el-button>
            <el-button type="primary" text size="small" :icon="Edit" @click="openEditRelation(selectedEdge)">编辑</el-button>
          </div>
        </div>

        <div class="cr-detail cr-detail-empty" v-else>
          <el-icon class="cr-detail-placeholder"><Share /></el-icon>
          <p>点击角色或关系查看详情</p>
        </div>
      </div>
    </template>

    <!-- 右键菜单 -->
    <div
      v-if="ctxMenu.visible"
      class="cr-ctx-mask"
      @click="closeCtxMenu"
      @contextmenu.prevent="closeCtxMenu"
    >
      <div class="cr-ctx" :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }">
        <div
          v-for="it in ctxMenu.items"
          :key="it.label"
          class="cr-ctx-item"
          :class="{ danger: it.danger }"
          @click="runCtx(it)"
        >{{ it.label }}</div>
      </div>
    </div>

    <!-- 角色编辑对话框 -->
    <el-dialog
      v-model="charDialogVisible"
      :title="charEditingId ? '编辑角色' : '新建角色'"
      width="760px"
      top="4vh"
      @closed="onCharClosed"
    >
      <CharacterForm v-if="charForm" v-model="charForm" />

      <!-- S3 修订历史（2026-09-17）：版本留痕 + AI 提案审批 + 一键回滚 -->
      <div v-if="charEditingId" class="cr-rev">
        <div class="cr-rev-head">
          <span class="cr-rev-title">修订历史</span>
          <el-tag v-if="pendingRevs.length" type="danger" size="small" effect="dark">
            {{ pendingRevs.length }} 条待审
          </el-tag>
          <span class="cr-rev-spacer" />
          <el-button size="small" text :loading="revLoading" @click="loadRevisions">刷新</el-button>
        </div>

        <el-empty
          v-if="!revisions.length"
          description="暂无修订（建卡 / 改卡 / 章节摄取都会自动留痕）"
          :image-size="50"
        />

        <div
          v-for="r in revisions"
          :key="r.id"
          class="cr-rev-item"
          :class="{ 'is-pending': r.status === 'pending' }"
        >
          <div class="cr-rev-line">
            <el-tag size="small" :type="revTagType(r)" effect="plain">{{ revLabel(r) }}</el-tag>
            <span class="cr-rev-time">{{ fmtRevTime(r.created_at) }}</span>
            <span v-if="r.chapter_no" class="cr-rev-ch">据第 {{ r.chapter_no }} 章</span>
            <span class="cr-rev-spacer" />
            <template v-if="r.status === 'pending'">
              <el-button size="small" type="primary" text @click="approveRev(r)">采纳</el-button>
              <el-button size="small" text @click="rejectRev(r)">驳回</el-button>
            </template>
            <el-button
              v-if="r.status === 'approved'"
              size="small" text
              @click="rollbackRev(r)"
            >回滚到此版</el-button>
          </div>

          <div v-if="r.note" class="cr-rev-note">{{ r.note }}</div>

          <div v-if="diffEntries(r).length" class="cr-rev-diff">
            <div v-for="d in diffEntries(r)" :key="d.k" class="cr-rev-diff-row">
              <b>{{ fieldZh(d.k) }}</b>：
              <span class="cr-rev-old">{{ d.from || '（空）' }}</span>
              <span class="cr-rev-arrow">→</span>
              <span class="cr-rev-new">{{ d.to || '（空）' }}</span>
            </div>
          </div>
          <div v-else class="cr-rev-note cr-rev-same">（与当前卡无字段差异）</div>
        </div>
      </div>
      <template #footer>
        <el-button v-if="charEditingId" type="danger" plain @click="deleteCharacter">删除</el-button>
        <span class="cr-dialog-spacer" />
        <el-button @click="charDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveCharacter">保存</el-button>
      </template>
    </el-dialog>

    <!-- 关系编辑对话框 -->
    <el-dialog
      v-model="relDialogVisible"
      :title="relEditingId ? '编辑关系' : '新建关系'"
      width="480px"
      @closed="onRelClosed"
    >
      <el-form :model="relForm" label-width="80px" v-if="relForm">
        <el-form-item label="主体"><el-input :value="charName(relForm.subject_id)" disabled /></el-form-item>
        <el-form-item label="客体"><el-input :value="charName(relForm.object_id)" disabled /></el-form-item>
        <el-form-item label="关系类型" required>
          <el-select
            v-model="relForm.relation_type"
            filterable allow-create default-first-option placeholder="选择或输入"
            style="width: 100%"
          >
            <el-option v-for="t in relTypes" :key="t" :label="t" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="关系强度">
          <el-slider v-model="relForm.strength" :min="0" :max="100" show-input />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="relForm.note" type="textarea" :rows="2" placeholder="特殊恩怨 / 细节" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button v-if="relEditingId" type="danger" plain @click="deleteRelation">删除</el-button>
        <span class="cr-dialog-spacer" />
        <el-button @click="relDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveRelation">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, Share, RefreshRight, ZoomIn, ZoomOut, Edit,
} from '@element-plus/icons-vue'
import { useProjectStore } from '@/store/project'
import { characterApi, relationApi } from '@/api/database'
import CharacterForm from '@/components/database/CharacterForm.vue'
import { useSvgViewport } from '@/composables/useSvgViewport'

const router = useRouter()
const store = useProjectStore()
const projectId = computed(() => store.currentNovelId)

const NODE_R = 22

// ---- 数据 ----
const characters = ref([])
const relations = ref([])
const loading = ref(false)

// 节点布局坐标（SVG 画布坐标系，可拖拽持久化）
const pos = reactive({})

// 视图变换（scale + translate + 拖拽平移 + 滚轮缩放 + 坐标换算）见下方 useSvgViewport
// （Phase 3.3 抽公共实现，与 WorldMapView 共用一份）

// 拖拽节点
const draggingNodeId = ref(null)
const nodeGrab = reactive({ dx: 0, dy: 0 })
const nodeMoved = ref(false)

// 选中
const selected = reactive({ kind: null, id: null })

// 关系连线模式
const linkingSourceId = ref(null)

// 对话框
const charDialogVisible = ref(false)
const charEditingId = ref(null)
const charForm = ref(null)
const relDialogVisible = ref(false)
const relEditingId = ref(null)
const relForm = ref(null)

// 右键菜单
const ctxMenu = reactive({ visible: false, x: 0, y: 0, items: [] })

// 画布尺寸（viewBox 直接取元素真实像素，消除 letterbox）
const svgEl = ref(null)
const svgSize = reactive({ w: 960, h: 640 })
let resizeObserver = null
function measureSvg() {
  const el = svgEl.value
  if (!el) return
  const r = el.getBoundingClientRect()
  if (r.width > 0 && r.height > 0) {
    svgSize.w = r.width
    svgSize.h = r.height
  }
}

// ---- 颜色 ----
const ROLE_COLORS = { '主角': '#f4a23c', '配角': '#409eff', '反派': '#9254de' }
function roleColor(r) { return ROLE_COLORS[r] || '#909399' }
const REL_COLORS = {
  '友好': '#67c23a', '敌对': '#f56c6c', '亲人': '#e6a23c', '师徒': '#409eff',
  '上下级': '#909399', '暧昧': '#f78989', '恋人': '#ff7eb3', '同盟': '#13c2c2', '其他': '#c0c4cc',
}
function relColor(t) { return REL_COLORS[t] || '#c0c4cc' }
const roleTag = (t) => ({ '主角': 'danger', '配角': 'warning', '反派': 'info' }[t] || 'info')
const roleLegend = [
  { label: '主角', color: '#f4a23c' },
  { label: '配角', color: '#409eff' },
  { label: '反派', color: '#9254de' },
  { label: '其他', color: '#909399' },
]
const relLegend = Object.keys(REL_COLORS).map((k) => ({ label: k, color: REL_COLORS[k] }))
const relTypes = Object.keys(REL_COLORS)

// ---- 派生数据 ----
const charIdSet = computed(() => new Set(characters.value.map((c) => c.id)))
const graphEdges = computed(() =>
  relations.value.filter((r) => charIdSet.value.has(r.subject_id) && charIdSet.value.has(r.object_id)),
)

function edgeGeom(e) {
  const a = pos[e.subject_id]
  const b = pos[e.object_id]
  if (!a || !b) return null
  const dx = b.x - a.x, dy = b.y - a.y
  const d = Math.hypot(dx, dy) || 1
  const ux = dx / d, uy = dy / d
  const x1 = a.x + ux * NODE_R, y1 = a.y + uy * NODE_R
  const x2 = b.x - ux * NODE_R, y2 = b.y - uy * NODE_R
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2
  const s = 9
  const bx = x2, by = y2
  const lx = bx - ux * s - uy * s * 0.55
  const ly = by - uy * s + ux * s * 0.55
  const rx = bx - ux * s + uy * s * 0.55
  const ry = by - uy * s - ux * s * 0.55
  return { x1, y1, x2, y2, mx, my, arrow: `${bx},${by} ${lx},${ly} ${rx},${ry}` }
}

const edges = computed(() =>
  graphEdges.value
    .map((e) => {
      const g = edgeGeom(e)
      return g ? { ...e, ...g } : null
    })
    .filter(Boolean),
)

const selectedNode = computed(() =>
  selected.kind === 'node' ? characters.value.find((c) => c.id === selected.id) || null : null,
)
const selectedEdge = computed(() =>
  selected.kind === 'edge' ? relations.value.find((r) => r.id === selected.id) || null : null,
)
const incidentRelations = computed(() => {
  if (!selectedNode.value) return []
  return relations.value
    .filter((r) => r.subject_id === selectedNode.value.id || r.object_id === selectedNode.value.id)
    .map((r) => {
      const isSub = r.subject_id === selectedNode.value.id
      const otherId = isSub ? r.object_id : r.subject_id
      return {
        id: r.id, type: r.relation_type, strength: r.strength,
        dir: isSub ? '→' : '←', other: charName(otherId),
      }
    })
})

function charName(id) { return characters.value.find((c) => c.id === id)?.name || '?' }
function isNodeSelected(id) { return selected.kind === 'node' && selected.id === id }
function isEdgeSelected(id) { return selected.kind === 'edge' && selected.id === id }
function selectEdge(id) { selected.kind = 'edge'; selected.id = id }

// ---- viewBox ----
const vb = computed(() => {
  const w = svgSize.w || 960
  const h = svgSize.h || 640
  return { x: 0, y: 0, w: Math.max(120, w), h: Math.max(120, h) }
})
const viewBoxStr = computed(() => `${vb.value.x} ${vb.value.y} ${vb.value.w} ${vb.value.h}`)

// 视图变换（scale + translate + 拖拽平移 + 滚轮缩放 + 坐标换算）
// Phase 3.3 抽到 useSvgViewport，与 WorldMapView 共用一份实现
const {
  view, isDragging,
  clientToSvg, screenToContent,
  zoomAt, zoomBy, resetView,
  startPan, movePan, endPan, onWheel,
  contentTransform, zoomPercent,
} = useSvgViewport(() => vb.value, { svgRef: svgEl })

function screenToGraph(clientX, clientY) {
  const p = screenToContent(clientX, clientY)
  return { x: p.x, y: p.y }
}

// ---- 力导向布局 ----
function runLayout(ignoreSaved) {
  const w = svgSize.w || 960, h = svgSize.h || 640
  const cx = w / 2, cy = h / 2
  const ids = characters.value.map((c) => c.id)
  const p = {}
  for (const c of characters.value) {
    if (!ignoreSaved && c.network_x != null && c.network_y != null) {
      p[c.id] = { x: c.network_x, y: c.network_y }
    }
  }
  const need = characters.value.filter((c) => !p[c.id])
  const R = Math.min(w, h) * 0.32
  need.forEach((c, i) => {
    const a = (i / Math.max(1, need.length)) * Math.PI * 2
    p[c.id] = { x: cx + R * Math.cos(a) + (Math.random() - 0.5) * 4, y: cy + R * Math.sin(a) + (Math.random() - 0.5) * 4 }
  })
  const n = ids.length
  const iters = 320
  for (let it = 0; it < iters; it++) {
    const disp = {}
    ids.forEach((id) => (disp[id] = { x: 0, y: 0 }))
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = p[ids[i]], b = p[ids[j]]
        let dx = a.x - b.x, dy = a.y - b.y
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = dx * dx + dy * dy + 0.01 }
        const d = Math.sqrt(d2)
        const rep = 12000 / d2
        const fx = (dx / d) * rep, fy = (dy / d) * rep
        disp[ids[i]].x += fx; disp[ids[i]].y += fy
        disp[ids[j]].x -= fx; disp[ids[j]].y -= fy
      }
    }
    for (const e of graphEdges.value) {
      const a = p[e.subject_id], b = p[e.object_id]
      if (!a || !b) continue
      let dx = a.x - b.x, dy = a.y - b.y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      const f = (d - 170) * 0.012
      const fx = (dx / d) * f, fy = (dy / d) * f
      disp[e.subject_id].x -= fx; disp[e.subject_id].y -= fy
      disp[e.object_id].x += fx; disp[e.object_id].y += fy
    }
    for (const id of ids) {
      disp[id].x += (cx - p[id].x) * 0.008
      disp[id].y += (cy - p[id].y) * 0.008
    }
    const t = Math.max(2, 28 * (1 - it / iters))
    for (const id of ids) {
      const dx = disp[id].x, dy = disp[id].y
      const d = Math.sqrt(dx * dx + dy * dy) || 1
      p[id].x += (dx / d) * Math.min(d, t)
      p[id].y += (dy / d) * Math.min(d, t)
    }
  }
  for (const id of ids) pos[id] = { x: Math.round(p[id].x), y: Math.round(p[id].y) }
}
function relayout() { runLayout(true); resetView() }

// ---- 加载 ----
async function load() {
  if (!projectId.value) return
  loading.value = true
  try {
    const [chars, rels] = await Promise.all([
      characterApi.list(projectId.value),
      relationApi.list(projectId.value),
    ])
    characters.value = Array.isArray(chars) ? chars : (chars.items || [])
    relations.value = Array.isArray(rels) ? rels : (rels.items || [])
    runLayout(false)
  } catch (e) {
    ElMessage.error('加载失败：' + (e?.message || '未知错误'))
  } finally {
    loading.value = false
  }
}

// ---- 平移 / 缩放（实现在 useSvgViewport）----
// 包装 startPan 以记录**客户端像素**起点，供 onSvgBlankClick 判定「是拖动还是点击」。
// ⚠️ 不能用 composable 的 dragStart（那是 viewBox/内容坐标），两者量纲不同：
// scale≈1 且 tx=0 时恰好接近，缩放平移后会误判（拖动被当成点击）。
const panClientStart = { x: 0, y: 0 }
function onMouseDown(e) {
  panClientStart.x = e.clientX
  panClientStart.y = e.clientY
  startPan(e)
}
function onMouseMove(e) {
  if (draggingNodeId.value) {
    const g = screenToGraph(e.clientX, e.clientY)
    pos[draggingNodeId.value] = { x: g.x - nodeGrab.dx, y: g.y - nodeGrab.dy }
    nodeMoved.value = true
    return
  }
  movePan(e)
}
async function onMouseUp() {
  if (draggingNodeId.value) {
    const id = draggingNodeId.value
    draggingNodeId.value = null
    if (nodeMoved.value) {
      const p = pos[id]
      try {
        await characterApi.update(projectId.value, id, {
          network_x: Math.round(p.x), network_y: Math.round(p.y),
        })
      } catch (err) {
        ElMessage.error('布局保存失败，已保留本次显示')
      }
    }
    nodeMoved.value = false
    return
  }
  endPan()
}

// ---- 节点交互 ----
function onNodeMouseDown(e, n) {
  if (e.button !== 0) return
  e.stopPropagation()
  if (linkingSourceId.value) {
    if (linkingSourceId.value === n.id) { cancelLink(); return }
    completeLink(n)
    return
  }
  draggingNodeId.value = n.id
  nodeMoved.value = false
  const g = screenToGraph(e.clientX, e.clientY)
  nodeGrab.dx = g.x - (pos[n.id]?.x || 0)
  nodeGrab.dy = g.y - (pos[n.id]?.y || 0)
}
function onNodeClick(n) {
  if (linkingSourceId.value) return
  selected.kind = 'node'; selected.id = n.id
}
function onNodeDblClick(n) { openEditCharacter(n) }
function onEdgeClick(e) { selected.kind = 'edge'; selected.id = e.id }

function onSvgBlankClick(e) {
  const moved = Math.hypot(e.clientX - panClientStart.x, e.clientY - panClientStart.y)
  if (moved > 4) return
  if (linkingSourceId.value) { cancelLink(); return }
  selected.kind = null; selected.id = null
}

// ---- 右键菜单 ----
function openCtx(e, items) {
  ctxMenu.x = Math.min(e.clientX, window.innerWidth - 160)
  ctxMenu.y = Math.min(e.clientY, window.innerHeight - 190)
  ctxMenu.items = items
  ctxMenu.visible = true
}
function closeCtxMenu() { ctxMenu.visible = false }
function runCtx(it) { closeCtxMenu(); it.handler() }

function onNodeRightClick(e, n) {
  openCtx(e, [
    { label: '新建关系', handler: () => startLink(n) },
    { label: '编辑角色', handler: () => openEditCharacter(n) },
    { label: '删除角色', danger: true, handler: () => confirmDeleteCharacter(n) },
  ])
}
function onEdgeRightClick(e) {
  openCtx(e, [
    { label: '编辑关系', handler: () => openEditRelation(e) },
    { label: '删除关系', danger: true, handler: () => confirmDeleteRelation(e) },
  ])
}
function onSvgRightClick(e) {
  if (linkingSourceId.value) { cancelLink(); return }
  openCtx(e, [
    { label: '新建角色', handler: () => openCreateCharacter() },
  ])
}

// ---- 关系连线模式 ----
function startLink(n) {
  linkingSourceId.value = n.id
  selected.kind = 'node'; selected.id = n.id
}
function cancelLink() { linkingSourceId.value = null }
function completeLink(target) {
  const source = linkingSourceId.value
  linkingSourceId.value = null
  if (source === target.id) return
  openCreateRelation(source, target.id)
}

// ---- 角色对话框 ----
function emptyChar() {
  return {
    name: '', role_type: null, age: null, gender: null, current_level: '',
    personality: '', background: '', talent: '', skills: [], relationship_network: [], brief: '',
  }
}
function openCreateCharacter() {
  charEditingId.value = null
  charForm.value = emptyChar()
  charDialogVisible.value = true
}
function openEditCharacter(n) {
  const c = characters.value.find((x) => x.id === n.id)
  if (!c) return
  charForm.value = {
    name: c.name, role_type: c.role_type, age: c.age, gender: c.gender,
    identity: c.identity, function: c.function,
    current_level: c.current_level, personality: c.personality, background: c.background,
    talent: c.talent, skills: c.skills ? [...c.skills] : [],
    relationship_network: c.relationship_network ? [...c.relationship_network] : [], brief: c.brief,
  }
  charEditingId.value = n.id
  charDialogVisible.value = true
  loadRevisions()
}

// ---------------------------------------------------------------------------
// S3 角色修订（2026-09-17）：历史留痕 / AI 提案审批 / 一键回滚
// ---------------------------------------------------------------------------
const revisions = ref([])
const revLoading = ref(false)
const pendingRevs = computed(() => revisions.value.filter((r) => r.status === 'pending'))

const _FIELD_ZH = {
  name: '姓名', role_type: '角色类型', gender: '性别', age: '年龄',
  personality: '性格', background: '背景', talent: '天赋',
  current_level: '等级/境界', skills: '技能', relationship_network: '关系网', brief: '简介',
}
function fieldZh(k) { return _FIELD_ZH[k] || k }

function revLabel(r) {
  const src = r.source === 'ai_extract' ? 'AI 提案' : r.source === 'rollback' ? '回滚' : '手动'
  const st = r.status === 'pending' ? '（待审）' : r.status === 'rejected' ? '（已驳回）' : ''
  return src + st
}
function revTagType(r) {
  return r.status === 'pending' ? 'danger' : r.status === 'approved' ? 'success' : 'info'
}
function fmtRevTime(s) {
  return (s || '').replace('T', ' ').slice(0, 16)
}
// 后端给的是 diff_vs_current: {字段: [当前值, 该版本值]}
function diffEntries(r) {
  const d = r.diff_vs_current || {}
  return Object.entries(d).map(([k, v]) => ({ k, from: v?.[0], to: v?.[1] }))
}

async function loadRevisions() {
  if (!projectId.value || !charEditingId.value) return
  revLoading.value = true
  try {
    const r = await characterApi.listRevisions(projectId.value, charEditingId.value)
    revisions.value = Array.isArray(r) ? r : (r?.items || [])
  } catch (e) {
    revisions.value = []
  } finally {
    revLoading.value = false
  }
}

// 采纳/回滚都会改变角色卡 → 重新拉角色列表 + 回填表单
async function _afterCardChanged(tip) {
  ElMessage.success(tip)
  await loadRevisions()
  try {
    const list = await characterApi.list(projectId.value)
    characters.value = Array.isArray(list) ? list : (list?.items || [])
    const fresh = characters.value.find((x) => x.id === charEditingId.value)
    if (fresh) openEditCharacter(fresh)
  } catch (e) { /* 列表刷新失败不影响审批结果 */ }
}

async function approveRev(r) {
  try {
    await characterApi.approveRevision(projectId.value, charEditingId.value, r.id)
    await _afterCardChanged('已采纳该修订，角色卡已更新')
  } catch (e) { /* 拦截器已提示 */ }
}
async function rejectRev(r) {
  try {
    await characterApi.rejectRevision(projectId.value, charEditingId.value, r.id)
    ElMessage.info('已驳回（角色卡未变）')
    loadRevisions()
  } catch (e) { /* 拦截器已提示 */ }
}
async function rollbackRev(r) {
  try {
    await ElMessageBox.confirm(
      `回滚到「${fmtRevTime(r.created_at)}」的版本？当前设定会被覆盖（回滚本身也会留痕）。`,
      '回滚确认', { type: 'warning' })
  } catch { return }
  try {
    await characterApi.rollbackRevision(projectId.value, charEditingId.value, r.id)
    await _afterCardChanged('已回滚到该版本')
  } catch (e) { /* 拦截器已提示 */ }
}
async function saveCharacter() {
  if (!charForm.value?.name?.trim()) { ElMessage.warning('请填写角色姓名'); return }
  const payload = {
    ...charForm.value,
    network_x: pos[charEditingId.value]?.x,
    network_y: pos[charEditingId.value]?.y,
  }
  try {
    if (charEditingId.value) {
      const upd = await characterApi.update(projectId.value, charEditingId.value, payload)
      const idx = characters.value.findIndex((c) => c.id === charEditingId.value)
      if (idx >= 0) characters.value[idx] = upd
      ElMessage.success('已更新')
    } else {
      const created = await characterApi.create(projectId.value, payload)
      characters.value.push(created)
      pos[created.id] = {
        x: (svgSize.w || 960) / 2 + (Math.random() - 0.5) * 60,
        y: (svgSize.h || 640) / 2 + (Math.random() - 0.5) * 60,
      }
      ElMessage.success('已创建')
    }
    charDialogVisible.value = false
  } catch (err) {
    ElMessage.error('保存失败：' + (err?.message || ''))
  }
}
async function confirmDeleteCharacter(n) {
  try {
    await ElMessageBox.confirm(`确认删除角色「${n.name}」？其关系也会一并移除。`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  await deleteCharacter()
}
async function deleteCharacter() {
  if (!charEditingId.value) return
  try {
    await characterApi.remove(projectId.value, charEditingId.value)
    characters.value = characters.value.filter((c) => c.id !== charEditingId.value)
    relations.value = relations.value.filter((r) => r.subject_id !== charEditingId.value && r.object_id !== charEditingId.value)
    if (selected.kind === 'node' && selected.id === charEditingId.value) { selected.kind = null; selected.id = null }
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error('删除失败')
  }
  charDialogVisible.value = false
}
function onCharClosed() { charEditingId.value = null; charForm.value = null }

// ---- 关系对话框 ----
function openCreateRelation(subjectId, objectId) {
  relEditingId.value = null
  relForm.value = { subject_id: subjectId, object_id: objectId, relation_type: '友好', strength: 50, note: '' }
  relDialogVisible.value = true
}
function openEditRelation(e) {
  relEditingId.value = e.id
  relForm.value = {
    subject_id: e.subject_id, object_id: e.object_id,
    relation_type: e.relation_type, strength: e.strength, note: e.note || '',
  }
  relDialogVisible.value = true
}
async function saveRelation() {
  if (!relForm.value?.relation_type) { ElMessage.warning('请选择关系类型'); return }
  const body = {
    relation_type: relForm.value.relation_type,
    strength: relForm.value.strength,
    note: relForm.value.note,
  }
  try {
    if (relEditingId.value) {
      const upd = await relationApi.update(projectId.value, relEditingId.value, body)
      const idx = relations.value.findIndex((r) => r.id === relEditingId.value)
      if (idx >= 0) relations.value[idx] = upd
      ElMessage.success('已更新')
    } else {
      const created = await relationApi.create(projectId.value, {
        subject_id: relForm.value.subject_id, object_id: relForm.value.object_id, ...body,
      })
      relations.value.push(created)
      ElMessage.success('已创建')
    }
    relDialogVisible.value = false
  } catch (err) {
    ElMessage.error('保存失败：' + (err?.message || ''))
  }
}
async function confirmDeleteRelation(e) {
  try {
    await ElMessageBox.confirm(`确认删除关系「${charName(e.subject_id)} → ${charName(e.object_id)}」？`, '删除确认', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  await deleteRelation()
}
async function deleteRelation() {
  if (!relEditingId.value) return
  try {
    await relationApi.remove(projectId.value, relEditingId.value)
    relations.value = relations.value.filter((r) => r.id !== relEditingId.value)
    if (selected.kind === 'edge' && selected.id === relEditingId.value) { selected.kind = null; selected.id = null }
    ElMessage.success('已删除')
  } catch (err) {
    ElMessage.error('删除失败')
  }
  relDialogVisible.value = false
}
function onRelClosed() { relEditingId.value = null; relForm.value = null }

function goCharacters() { router.push('/workspace/database') }

function onKey(e) {
  if (e.key === 'Escape') {
    if (linkingSourceId.value) cancelLink()
    closeCtxMenu()
  }
}

onMounted(() => {
  load()
  if (svgEl.value) {
    resizeObserver = new ResizeObserver(measureSvg)
    resizeObserver.observe(svgEl.value)
  }
  window.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  window.removeEventListener('keydown', onKey)
})
watch(projectId, () => { selected.kind = null; selected.id = null; load() })
</script>

<style scoped>
.cr { display: flex; flex-direction: column; height: 100%; box-sizing: border-box; padding: 4px 16px 16px; overflow: hidden; }
.cr-toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; flex: 0 0 auto; }
.cr-title { font-size: 18px; font-weight: 600; }
.cr-novel { font-size: 13px; font-weight: 400; color: #909399; margin-left: 10px; }
.cr-actions { display: flex; align-items: center; gap: 10px; }
.cr-link-hint {
  flex: 0 0 auto; margin-bottom: 10px; padding: 8px 14px; border-radius: 6px;
  background: #ecf5ff; color: #409eff; font-size: 13px; display: flex; align-items: center; gap: 10px;
}
.cr-body { display: flex; flex: 1; min-height: 0; gap: 12px; }
.cr-canvas-wrap { position: relative; flex: 1; min-height: 0; height: 100%; background: #fbfcfe; border-radius: 8px; overflow: hidden; }
.cr-svg { width: 100%; height: 100%; display: block; cursor: grab; user-select: none; }
.cr-svg.grabbing { cursor: grabbing; }
.cr-node { cursor: pointer; }
.cr-node-label { font-size: 13px; font-weight: 600; fill: #303133; paint-order: stroke; stroke: #fbfcfe; stroke-width: 3px; }
.cr-node-label.active { fill: #409eff; }
.cr-node.linking circle { stroke: #409eff; stroke-width: 3; stroke-dasharray: 4 3; }
.cr-edge-label { font-size: 11px; fill: #606266; paint-order: stroke; stroke: #fbfcfe; stroke-width: 3px; pointer-events: none; }
.cr-edge { cursor: pointer; }
.cr-edge.active line { stroke-opacity: 1; }

.cr-controls { position: absolute; left: 12px; bottom: 12px; display: flex; align-items: center; gap: 10px; }
.cr-zoom-info { font-size: 12px; color: #909399; background: #fff; padding: 2px 8px; border-radius: 4px; border: 1px solid var(--el-border-color-light); }

.cr-legend {
  position: absolute; right: 12px; top: 12px; background: #fff; border: 1px solid var(--el-border-color-light);
  border-radius: 6px; padding: 8px 10px; font-size: 12px; color: #606266;
}
.cr-legend-title { font-weight: 600; color: #303133; margin-bottom: 4px; }
.cr-legend-row { display: flex; align-items: center; gap: 6px; line-height: 20px; }
.cr-dot { width: 12px; height: 12px; border-radius: 50%; display: inline-block; }
.cr-line { width: 16px; height: 3px; border-radius: 2px; display: inline-block; }

.cr-canvas-hint { position: absolute; left: 12px; top: 12px; margin: 0; font-size: 12px; color: #909399; max-width: 60%; }

.cr-detail { width: 300px; flex: 0 0 300px; background: #fff; border: 1px solid var(--el-border-color-light); border-radius: 8px; padding: 14px; overflow: auto; }
.cr-detail-empty { display: flex; flex-direction: column; align-items: center; justify-content: center; color: #c0c4cc; }
.cr-detail-placeholder { font-size: 40px; margin-bottom: 8px; }
.cr-detail-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.cr-detail-name { font-size: 16px; font-weight: 600; }
.cr-detail-block { margin-top: 12px; }
.cr-detail-sub { font-size: 13px; font-weight: 600; color: #303133; margin-bottom: 6px; }
.cr-detail-text { font-size: 13px; color: #606266; line-height: 1.6; margin: 0; white-space: pre-wrap; }
.cr-rel-item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.cr-rel-item:hover { background: #f5f7fa; }
.cr-rel-item.active { background: #ecf5ff; }
.cr-rel-dir { color: #909399; font-weight: 600; }
.cr-rel-other { flex: 1; color: #303133; }
.cr-rel-strength { color: #909399; font-size: 12px; }
.cr-detail-actions { margin-top: 14px; display: flex; justify-content: flex-end; gap: 8px; }

.cr-ctx-mask { position: fixed; inset: 0; z-index: 3000; }
.cr-ctx { position: fixed; min-width: 130px; background: #fff; border: 1px solid var(--el-border-color-light); border-radius: 6px; box-shadow: 0 4px 16px rgba(0,0,0,0.12); padding: 4px; }
.cr-ctx-item { padding: 7px 12px; border-radius: 4px; font-size: 13px; cursor: pointer; color: #303133; }
.cr-ctx-item:hover { background: #f5f7fa; }
.cr-ctx-item.danger { color: #f56c6c; }
.cr-dialog-spacer { flex: 1; }

/* S3 修订历史（2026-09-17） */
.cr-rev {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color);
  max-height: 320px;
  overflow-y: auto;
}
.cr-rev-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.cr-rev-title { font-size: 13px; font-weight: 600; color: var(--el-text-color-primary); }
.cr-rev-spacer { flex: 1; }
.cr-rev-item {
  padding: 8px 10px;
  margin-bottom: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-blank);
}
.cr-rev-item.is-pending {
  border-color: var(--el-color-danger-light-5);
  background: var(--el-color-danger-light-9);
}
.cr-rev-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cr-rev-time { font-size: 12px; color: var(--el-text-color-secondary); }
.cr-rev-ch { font-size: 12px; color: var(--el-color-primary); }
.cr-rev-note { font-size: 12px; color: var(--el-text-color-regular); margin-top: 4px; }
.cr-rev-same { color: var(--el-text-color-placeholder); }
.cr-rev-diff { margin-top: 5px; }
.cr-rev-diff-row { font-size: 12px; line-height: 1.7; color: var(--el-text-color-regular); }
.cr-rev-old { color: var(--el-text-color-placeholder); text-decoration: line-through; }
.cr-rev-arrow { margin: 0 4px; color: var(--el-text-color-placeholder); }
.cr-rev-new { color: var(--el-color-primary); }
</style>
