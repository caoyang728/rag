<template>
  <div class="page-container graph-page">
    <!-- ===== 页头 ===== -->
    <div class="page-header">
      <div>
        <div class="page-title">知识图谱</div>
        <div class="page-desc">浏览实体-关系图谱，语义检索实体并扩展邻居，查看社区摘要</div>
      </div>
      <!-- 社区检测：仅知识库管理员 / 超管可见（后端另有权限校验） -->
      <el-button v-if="userStore.hasAnyRole('super_admin', 'kb_admin')" size="small" @click="confirmDetect">🔁 重新检测社区</el-button>
    </div>

    <!-- ===== 内容区：tabs 撑满高度，面板内部滚动 ===== -->
    <div class="page-body tabs-fill">
    <!-- ===== Tab 切换 ===== -->
    <el-tabs v-model="activeTab">
      <!-- ============ 图谱浏览 ============ -->
      <el-tab-pane label="🕸️ 图谱浏览" name="graph">
        <div class="graph-toolbar">
          <el-input
            v-model="entityQuery"
            placeholder="🔍 语义检索实体，如：公司年度目标"
            clearable
            style="flex: 1; min-width: 240px"
            @keyup.enter="doEntitySearch"
            @clear="resetSearch"
          />
          <div class="toolbar-actions">
            <el-select v-model="entityTypeFilter" placeholder="全部类型" clearable style="width: 120px">
              <el-option v-for="opt in ENTITY_TYPE_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
            <el-button type="primary" @click="doEntitySearch">检索</el-button>
            <el-button @click="resetGraph">重置</el-button>
          </div>
        </div>

        <!-- 语义检索提示条：检索中/命中数/未找到/错误等状态反馈 -->
        <div v-if="searchHint" class="graph-search-hint">{{ searchHint }}</div>

        <!-- 检索结果条：一次检索命中的实体，点击可切换中心实体 -->
        <div v-if="searchResults.length" class="graph-search-results">
          <span
            v-for="(r, i) in searchResults"
            :key="r.entity_id || r.id"
            class="gsr-item"
            :class="{ 'gsr-active': i === activeResultIndex }"
            :title="searchResultsIsFallback ? '名称匹配' : `相似度 ${(r.score || 0).toFixed(2)}`"
            @click="switchCenter(r.entity_id || r.id, i)"
          >
            {{ r.name }}
            <span class="gsr-score">{{ typeLabel(r.type) }}{{ searchResultsIsFallback ? '' : ' · ' + (r.score || 0).toFixed(2) }}</span>
          </span>
        </div>

        <!-- 图谱内容排列：SVG 画布 + 实体详情面板 -->
        <div class="graph-layout">
          <div
            ref="svgWrapRef"
            class="graph-canvas-wrap"
            :class="{ 'graph-canvas-grabbing': isDragging }"
            @wheel.prevent="onSvgWheel"
            @mousedown="onSvgMouseDown"
          >
            <!-- 缩放控制按钮 -->
            <div class="graph-zoom-controls" v-if="graphNodes.length">
              <button class="zoom-btn" @click="zoomIn" title="放大">＋</button>
              <span class="zoom-label">{{ Math.round(graphScale * 100) }}%</span>
              <button class="zoom-btn" @click="zoomOut" title="缩小">－</button>
              <button class="zoom-btn zoom-btn-reset" @click="zoomReset" title="重置">↻</button>
            </div>
            <!-- 原生 SVG 力导向图：节点点击扩展邻居，悬停提示实体信息
                 viewBox 动态计算实现缩放，支持鼠标拖动平移 -->
            <svg
              v-if="graphNodes.length"
              class="graph-svg"
              :class="{ 'graph-svg-grabbing': isDragging }"
              :viewBox="graphViewBox"
            >
              <!-- 边：中心关联边加粗展示，便于追踪中心实体关系 -->
              <line
                v-for="e in graphEdgesWithCoords"
                :key="e.id"
                :x1="e.x1"
                :y1="e.y1"
                :x2="e.x2"
                :y2="e.y2"
                :stroke-width="e.isCenter ? edgeStrokeWidthCenter : edgeStrokeWidth"
                class="graph-edge"
              />
              <g
                v-for="n in graphNodes"
                :key="n.id"
                :transform="`translate(${n.x}, ${n.y})`"
                class="graph-node"
                @click.stop="onNodeClick(n)"
              >
                <circle
                  :r="nodeRadius(n)"
                  :fill="typeColor(n.type)"
                  :stroke="n.is_center ? '#f59e0b' : 'transparent'"
                  :stroke-width="n.is_center ? 2 : 0"
                  class="graph-node-circle"
                />
                <text
                  :y="nodeRadius(n) + Math.round(nodeLabelFontSize * 1.2)"
                  text-anchor="middle"
                  :font-size="nodeLabelFontSize"
                  class="graph-node-label"
                >{{ n.name }}</text>
                <title>{{ n.name }}（{{ typeLabel(n.type) }}）{{ n.description ? '：' + n.description : '' }}</title>
              </g>
            </svg>
            <!-- 空态：无图数据时的引导文案 -->
            <div v-else class="graph-empty">
              <div class="graph-empty-icon">🕸️</div>
              <div>输入关键词检索实体，或点击社区中的实体查看其关系子图</div>
            </div>
          </div>

          <!-- 实体详情面板（右侧固定宽度，与画布等高）
               始终显示面板，避免 v-show 切换导致 graph-svg 抖动 -->
          <div class="entity-panel">
            <div v-if="entityDetail" class="entity-panel-inner">
              <div class="entity-panel-title">
                {{ entityDetail.name }}
                <span class="tag tag-info">{{ entityDetail.type_label }}</span>
              </div>
              <div class="ep-meta">🕐 {{ formatDate(entityDetail.updated_at) }} · 📄 来源文档 {{ entityDetail.source_doc_count }} 篇（可见 {{ (entityDetail.source_docs || []).length }}）</div>
              <div class="ep-section">
                <div class="ep-section-title">描述</div>
                <div class="ep-desc">{{ entityDetail.description || '暂无' }}</div>
              </div>
              <div class="ep-section">
                <div class="ep-section-title">别名</div>
                <div v-if="entityDetail.aliases && entityDetail.aliases.length">
                  <span v-for="a in entityDetail.aliases" :key="a" class="tag">{{ a }}</span>
                </div>
                <span v-else class="text-sub">无</span>
              </div>
              <div class="ep-section">
                <div class="ep-section-title">可见来源文档</div>
                <ul class="ep-docs">
                  <template v-if="entityDetail.source_docs && entityDetail.source_docs.length">
                    <li v-for="doc in entityDetail.source_docs" :key="doc.title">📄 {{ doc.title }}</li>
                  </template>
                  <li v-else class="text-sub">无可显示来源文档</li>
                </ul>
              </div>
              <div class="ep-section ep-actions">
                <el-button size="small" type="primary" @click="expandNode(entityDetail.id)">＋ 展开邻居</el-button>
                <el-button size="small" @click="loadSubgraph(entityDetail.id, 2)">↻ 以此为中心</el-button>
              </div>
            </div>
            <div v-else class="ep-placeholder">
              <div class="ep-placeholder-icon">👆</div>
              <div class="ep-placeholder-text">点击节点查看详情</div>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ============ 社区列表 ============ -->
      <el-tab-pane label="🗂️ 社区列表" name="communities">
        <div class="graph-toolbar">
          <el-input
            v-model="communityQuery"
            placeholder="🔍 按主题/关键词搜索社区"
            clearable
            style="flex: 1; min-width: 240px"
            @keyup.enter="loadCommunities(1)"
            @clear="loadCommunities(1)"
          />
          <div class="toolbar-actions">
            <el-select v-model="communityLevelFilter" placeholder="全部粒度" clearable style="width: 130px" @change="loadCommunities(1)">
              <el-option label="细粒度（L0）" value="0" />
              <el-option label="中粒度（L1）" value="1" />
              <el-option label="粗粒度（L2）" value="2" />
            </el-select>
            <el-button type="primary" @click="loadCommunities(1)">查询</el-button>
          </div>
        </div>

        <div v-loading="communityLoading" class="community-list">
          <div v-if="!communityLoading && communities.length === 0" class="community-empty">
            <div class="community-empty-icon">🗂️</div>
            <div>暂无社区数据，可点击右上角"重新检测社区"生成</div>
          </div>
          <!-- 社区卡片：点击展开/收起社区内实体 -->
          <div
            v-for="c in communities"
            :key="c.id"
            class="community-card"
            @click="toggleCommunityDetail(c.id)"
          >
            <div class="community-card-head">
              <span class="community-card-topic">{{ c.topic || `社区 #${c.community_id}` }}</span>
              <span class="tag tag-info">L{{ c.level }}</span>
              <span class="badge badge-default">{{ c.entity_count }} 实体</span>
              <span class="community-card-time">🕐 {{ formatDate(c.updated_at) }}</span>
            </div>
            <div class="community-card-summary">{{ c.summary || '暂无摘要（触发社区检测后自动生成）' }}</div>
            <div class="community-card-meta">
              <span v-for="k in c.keywords || []" :key="k" class="tag">{{ k }}</span>
              <span class="text-sub">▾ 点击查看社区实体</span>
            </div>
            <!-- 社区实体（展开后展示，同一社区只请求一次，缓存于 communityDetailMap） -->
            <div v-if="expandedCommunityId === c.id" class="community-entities">
              <template v-if="communityDetailMap[c.id]">
                <div class="community-entities-title">社区实体（{{ (communityDetailMap[c.id].entities || []).length }}/{{ c.entity_count }}）</div>
                <div>
                  <span
                    v-for="e in communityDetailMap[c.id].entities"
                    :key="e.id"
                    class="community-entity-item"
                    :title="e.description || ''"
                    @click.stop="openEntityFromCommunity(e.id)"
                  >{{ e.name }} <span class="community-entity-type">{{ typeLabel(e.type) }}</span></span>
                  <span v-if="!(communityDetailMap[c.id].entities || []).length" class="text-sub">无可见实体</span>
                </div>
              </template>
              <div v-else class="community-loading">加载社区实体...</div>
            </div>
          </div>
        </div>
        <!-- 分页：后端按 page_size 切片；切换每页条数时重置回第 1 页 -->
        <AppPagination
          class="community-pagination"
          :total="communityTotal"
          :page-size="PAGE_SIZE"
          :page="communityPage"
          @page-change="onCommunityPageChange"
        />
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, errMsg } from '../utils/format'
import { useConfirm } from '../composables/useConfirm'
import AppPagination from '../components/base/AppPagination.vue'

const GRAPH_API = '/api/v1/graph'
const PAGE_SIZE = 20 // 后端分页默认每页 20 条

const userStore = useUserStore()
// 二次确认弹窗统一封装
const { confirm } = useConfirm()

const TYPE_LABELS = { PERSON: '人物', ORG: '组织', CONCEPT: '概念', TERM: '术语', PRODUCT: '产品' }
const TYPE_COLORS = { PERSON: '#f97316', ORG: '#3b82f6', CONCEPT: '#10b981', TERM: '#8b5cf6', PRODUCT: '#ec4899' }
// 实体类型选项：统一数据源，避免模板中硬编码导致维护两份类型列表
const ENTITY_TYPE_OPTIONS = Object.entries(TYPE_LABELS).map(([value, label]) => ({ label, value }))

// SVG 视口基准尺寸（小图使用，大图动态扩展）
const GRAPH_W_BASE = 900
const GRAPH_H_BASE = 600

/**
 * 缩放不变量计算：根据缩放级别计算视觉大小，并除以 scale 抵消 viewBox 放大。
 * 用于节点标签字体大小、边线条宽度等需要在任意缩放级别下保持屏幕视觉大小一致的属性。
 * @param {number} base - 基础视觉大小
 * @param {number} min - 最小视觉大小限制
 * @param {number} max - 最大视觉大小限制
 * @param {number} scale - 当前缩放级别
 * @returns {number} SVG 坐标系中的大小值
 */
function scaleInvariant(base, min, max, scale) {
  const visual = base * scale
  return Math.max(min, Math.min(max, visual)) / scale
}

/* ==========================================================
   图谱浏览状态
   ========================================================== */
const activeTab = ref('graph')
const entityQuery = ref('')
const entityTypeFilter = ref('')
const searchHint = ref('')
const searchResults = ref([])
const searchResultsIsFallback = ref(false)
const activeResultIndex = ref(0)

const svgWrapRef = ref(null)       // SVG 容器引用（缓存尺寸，避免高频事件 reflow）
const graphNodes = ref([])        // 节点数组（布局后含 x/y）
const graphEdges = ref([])        // 边数组
const graphCenterId = ref(null)   // 当前中心实体 id
const entityDetail = ref(null)    // 实体详情面板数据
const expandedSet = new Set()     // 已展开过邻居的节点 id（避免重复请求）
// SVG 缩放状态：通过 viewBox 控制缩放，节点大小保持不变
const graphScale = ref(1)
const GRAPH_SCALE_MIN = 0.3
const GRAPH_SCALE_MAX = 3
// 缩放中心点（相对于画布的坐标比例）
const graphPanX = ref(0.5)
const graphPanY = ref(0.5)
// 拖动状态
const isDragging = ref(false)
const dragStartX = ref(0)
const dragStartY = ref(0)
const dragStartPanX = ref(0)
const dragStartPanY = ref(0)
let _cachedSvgRect = null  // 拖动期间缓存 SVG 尺寸，避免 mousemove 高频 reflow
let _wheelRaf = null       // 滚轮缩放 requestAnimationFrame 句柄，用于节流
// id → 节点对象索引（非响应式，仅用于坐标/度数快速查找）
const nodeById = new Map()
// id → 边对象（非响应式，用于去重；渲染数组为 graphEdges）
const edgeById = new Map()

// 动态画布尺寸：根据节点数自动扩展，避免大量节点挤在一起
const graphW = computed(() => {
  const count = graphNodes.value.length
  if (count <= 50) return GRAPH_W_BASE
  // 节点数 >50 时按面积比例扩展（每 50 个节点增加约 22% 边长）
  const scale = Math.sqrt(count / 50)
  return Math.ceil(GRAPH_W_BASE * Math.max(1, scale))
})
const graphH = computed(() => {
  const count = graphNodes.value.length
  if (count <= 50) return GRAPH_H_BASE
  const scale = Math.sqrt(count / 50)
  return Math.ceil(GRAPH_H_BASE * Math.max(1, scale))
})

// 动态 viewBox：根据缩放级别和中心点计算，实现缩放时节点大小不变
const graphViewBox = computed(() => {
  const GW = graphW.value
  const GH = graphH.value
  const scale = graphScale.value
  // 缩放后的视口尺寸（缩放越大，视口越小，内容显示越大）
  const vw = GW / scale
  const vh = GH / scale
  // 视口左上角坐标（基于中心点比例）
  const cx = graphPanX.value * GW
  const cy = graphPanY.value * GH
  const vx = cx - vw / 2
  const vy = cy - vh / 2
  return `${vx} ${vy} ${vw} ${vh}`
})

// 节点标签字体大小：视觉大小 = base × scale，除以 scale 抵消 viewBox 放大
const nodeLabelFontSize = computed(() => scaleInvariant(12, 8, 11, graphScale.value || 1))

// 边线条宽度：视觉宽度 = base × scale，除以 scale 抵消 viewBox 放大
const edgeStrokeWidth = computed(() => scaleInvariant(1, 0.5, 2, graphScale.value || 1))
const edgeStrokeWidthCenter = computed(() => scaleInvariant(2, 1, 4, graphScale.value || 1))

/* ==========================================================
   社区列表状态
   ========================================================== */
const communityPage = ref(1)
const communityQuery = ref('')
const communityLevelFilter = ref('')
const communities = ref([])
const communityTotal = ref(0)
const communityLoading = ref(false)
const expandedCommunityId = ref(null)   // 当前展开的社区卡片 id
const communityDetailMap = ref({})      // 社区详情缓存（卡片展开只请求一次）

// 请求序号：防止快速筛选/翻页时旧响应后返回覆盖新状态
let communitySeq = 0
let searchSeq = 0      // 检索/子图请求序号：防快速连续操作时旧响应覆盖新状态
let detailSeq = 0      // 详情请求序号
let detectTimer = null  // 社区检测轮询定时器（卸载时清理）
let detectPollCount = 0  // 当前轮询次数
const DETECT_POLL_MAX = 10  // 最大轮询次数（每 3 秒一次，最多 30 秒）
const DETECT_POLL_INTERVAL = 3000  // 轮询间隔（毫秒）

function typeLabel(type) {
  return TYPE_LABELS[type] || type
}

function typeColor(type) {
  return TYPE_COLORS[type] || '#64748b'
}

/* ============ 语义检索实体 ============ */
async function doEntitySearch() {
  const q = entityQuery.value.trim()
  if (!q) {
    ElMessage.warning('请输入要检索的实体关键词')
    return
  }
  const seq = ++searchSeq
  const type = entityTypeFilter.value
  searchHint.value = '🔍 检索中...'
  searchResults.value = []

  try {
    const params = new URLSearchParams({ q, top_k: 10 })
    if (type) params.set('type', type)
    const data = await api.getJson(`${GRAPH_API}/entities/search/?${params.toString()}`)
    if (seq !== searchSeq) return
    const results = data.results || []

    if (!results.length) {
      // 语义检索无命中：回退到名称模糊检索，保证"实体存在但无向量"场景可用
      const fb = await nameSearchFallback(q, type)
      if (seq !== searchSeq) return
      if (!fb.length) {
        searchHint.value = `未找到与"${q}"匹配的实体`
        return
      }
      searchResults.value = fb
      searchResultsIsFallback.value = true
      activeResultIndex.value = 0
      loadSubgraph(fb[0].id)
      return
    }

    searchHint.value = `命中 ${results.length} 个实体，已渲染"${results[0].name}"的关系子图，点击其他结果可切换`
    searchResults.value = results
    searchResultsIsFallback.value = false
    activeResultIndex.value = 0
    loadSubgraph(results[0].entity_id || results[0].id)
  } catch (e) {
    if (seq !== searchSeq) return
    searchHint.value = `检索失败：${errMsg(e, '未知错误')}`
  }
}

async function nameSearchFallback(q, type) {
  const params = new URLSearchParams({ q, page_size: 10 })
  if (type) params.set('type', type)
  const data = await api.getJson(`${GRAPH_API}/entities/?${params.toString()}`)
  return (data.results || []).map(r => ({ id: r.id, name: r.name, type: r.type, description: r.description }))
}

function switchCenter(id, index) {
  activeResultIndex.value = index
  loadSubgraph(id)
}

/* ============ 子图加载 / 邻居扩展 ============ */
async function loadSubgraph(entityId, depth = 2) {
  const seq = ++searchSeq
  searchHint.value = '🕸️ 子图加载中...'
  try {
    const data = await api.getJson(`${GRAPH_API}/entities/${entityId}/neighbors/?depth=${depth}`)
    if (seq !== searchSeq) return
    const hadNodes = graphNodes.value.length > 0
    mergeSubgraph(data, true)
    // 有已有节点时用增量布局（复用坐标），无节点时用全量布局
    if (hadNodes) {
      await runIncrementalLayout(entityId)
    } else {
      await runLayout()
    }
    showEntityDetail(entityId)
  } catch (e) {
    if (seq !== searchSeq) return
    searchHint.value = `子图加载失败：${errMsg(e, '未知错误')}`
  }
}

async function expandNode(entityId) {
  if (expandedSet.has(entityId)) {
    // 已展开过：仅刷新详情面板
    showEntityDetail(entityId)
    return
  }
  expandedSet.add(entityId)
  const seq = ++searchSeq
  try {
    const data = await api.getJson(`${GRAPH_API}/entities/${entityId}/neighbors/?depth=1`)
    if (seq !== searchSeq) {
      expandedSet.delete(entityId)
      return
    }
    mergeSubgraph(data, false)
    // 新增了邻居节点：做增量布局，新节点围绕中心节点展开，不移动已有节点
    await runIncrementalLayout(entityId)
    showEntityDetail(entityId)
  } catch (e) {
    if (seq !== searchSeq) return
    expandedSet.delete(entityId)
    ElMessage.error('邻居扩展失败：' + errMsg(e, '未知错误'))
  }
}

function mergeSubgraph(data, isNewCenter) {
  const newNodes = data.nodes || []
  const newEdges = data.edges || []

  if (isNewCenter) {
    // 切换中心实体：保留旧图但更新中心标记（不清空，便于回溯）
    graphCenterId.value = data.center
  }
  newNodes.forEach(n => {
    const existing = nodeById.get(n.id)
    nodeById.set(n.id, {
      id: n.id,
      name: n.name,
      type: n.type,
      type_label: n.type_label,
      description: n.description,
      // 保留已有节点的坐标，避免重建对象后坐标丢失导致节点堆叠到原点
      x: existing && existing.x != null ? existing.x : undefined,
      y: existing && existing.y != null ? existing.y : undefined,
      is_center: isNewCenter ? n.is_center : (existing ? existing.is_center : false),
    })
  })
  // 新中心实体强制标记为中心
  if (isNewCenter && graphCenterId.value != null && nodeById.has(graphCenterId.value)) {
    nodeById.get(graphCenterId.value).is_center = true
  }
  // 边按 id 去重：多次扩展邻居会返回重复边，避免渲染重叠
  // 先写入 Map，最后一次性赋值 graphEdges，避免中间态逐条 push 触发 computed 重算
  newEdges.forEach(e => {
    if (!edgeById.has(e.id)) {
      edgeById.set(e.id, e)
    }
  })
  // 用去重后的 Map 值一次性重建节点和边数组（保证同一元素只渲染一次）
  graphEdges.value = [...edgeById.values()]
  graphNodes.value = [...nodeById.values()]
}

/* ============ 力导向布局（原生实现） ============
 * 公共模拟逻辑提取到 simulateForces，runLayout / runIncrementalLayout 各自负责初始化坐标和参数。
 * 斥力计算为 O(n²) + 距离剪枝（大图跳过远距离节点对），实际复杂度介于 O(n) ~ O(n²) 之间。
 */

/**
 * 力导向布局公共模拟：斥力 + 弹簧力 + 中心引力，迭代收敛后写入节点 x/y。
 * 使用 requestAnimationFrame 分帧执行，避免阻塞主线程导致 UI 卡顿。
 * @param {Array} nodes - 节点数组
 * @param {Array} edges - 边数组（已过滤无效端点）
 * @param {Map} pos - 节点 id → {x, y} 位置
 * @param {Map} vel - 节点 id → {x, y} 速度
 * @param {Object} opts - 布局参数：cx, cy, GW, GH, REPULSION, SPRING_LEN, SPRING_K, GRAVITY, DAMPING, ITER, [newIdSet]
 * @returns {Promise<void>} 布局完成后 resolve
 */
function simulateForces(nodes, edges, pos, vel, opts) {
  return new Promise(resolve => {
    const n = nodes.length
    const { cx, cy, GW, GH, REPULSION, SPRING_LEN, SPRING_K, GRAVITY, DAMPING, ITER, newIdSet } = opts

    const idx = new Map(nodes.map((node, i) => [node.id, i]))
    const adj = edges
      .map(e => ({ a: idx.get(e.source), b: idx.get(e.target) }))
      .filter(e => e.a !== undefined && e.b !== undefined)

    let iter = 0
    const BATCH_SIZE = 5 // 每帧执行的迭代轮数，平衡流畅度与布局速度
    const hasNewId = !!newIdSet

    function step() {
      const batchEnd = Math.min(iter + BATCH_SIZE, ITER)
      for (; iter < batchEnd; iter++) {
        // 斥力：O(n²) + 距离剪枝（大图跳过远距离节点对）
        for (let i = 0; i < n; i++) {
          const ni = nodes[i]
          const pi = pos.get(ni.id)
          const vi = vel.get(ni.id)
          for (let j = i + 1; j < n; j++) {
            const pj = pos.get(nodes[j].id)
            let dx = pi.x - pj.x
            let dy = pi.y - pj.y
            let d2 = dx * dx + dy * dy
            if (d2 < 1) {
              // 重叠节点添加微小随机偏移，避免斥力发散
              dx = Math.random() - 0.5
              dy = Math.random() - 0.5
              d2 = dx * dx + dy * dy || 1
            }
            if (n > 100 && d2 > 100000) continue
            const d = Math.sqrt(d2)
            const f = REPULSION / d2
            const fx = (dx / d) * f
            const fy = (dy / d) * f
            vi.x += fx
            vi.y += fy
            const vj = vel.get(nodes[j].id)
            vj.x -= fx
            vj.y -= fy
          }
        }
        // 弹簧力：边两端节点受力向平衡长度靠拢
        for (const { a, b } of adj) {
          const na = nodes[a]
          const nb = nodes[b]
          const pa = pos.get(na.id)
          const pb = pos.get(nb.id)
          const va = vel.get(na.id)
          const vb = vel.get(nb.id)
          const dx = pb.x - pa.x
          const dy = pb.y - pa.y
          const d = Math.sqrt(dx * dx + dy * dy) || 0.01
          const f = SPRING_K * (d - SPRING_LEN)
          const fx = (dx / d) * f
          const fy = (dy / d) * f
          va.x += fx
          va.y += fy
          vb.x -= fx
          vb.y -= fy
        }
        // 中心引力 + 阻尼：新节点引力更强（newIdSet），引导快速落位
        for (const node of nodes) {
          const p = pos.get(node.id)
          const v = vel.get(node.id)
          const g = hasNewId && newIdSet.has(node.id) ? 0.05 : GRAVITY
          v.x += (cx - p.x) * g
          v.y += (cy - p.y) * g
          v.x *= DAMPING
          v.y *= DAMPING
          p.x += v.x
          p.y += v.y
        }
      }

      if (iter < ITER) {
        requestAnimationFrame(step)
      } else {
        // 将模拟结果写回节点并夹紧到画布边界
        const pad = 34
        for (const node of nodes) {
          const p = pos.get(node.id)
          node.x = Math.max(pad, Math.min(GW - pad, p.x))
          node.y = Math.max(pad, Math.min(GH - pad, p.y))
        }
        resolve()
      }
    }

    requestAnimationFrame(step)
  })
}

// 增量布局：为新节点（无坐标）围绕中心节点分配初始位置，再做轻量迭代微调。
// 不瞬移已有节点到画布中心——否则中心节点会脱离邻居孤立显示（视觉上"没有连线"）。
// 使用异步布局避免阻塞主线程，返回 Promise 表示布局完成。
async function runIncrementalLayout(centerId) {
  const nodes = graphNodes.value
  const n = nodes.length
  if (n === 0) return
  const GW = graphW.value
  const GH = graphH.value
  const cx = GW / 2
  const cy = GH / 2

  // 中心节点位置：优先用其现有坐标（保持与邻居的连线）
  const centerNode = nodeById.get(centerId)
  const centerX = centerNode && centerNode.x != null ? centerNode.x : cx
  const centerY = centerNode && centerNode.y != null ? centerNode.y : cy

  // 为没有坐标的新节点分配初始位置：围绕中心节点环形分布，保证邻居展开后可见
  const newNodes = nodes.filter(node => node.x == null)
  const newIdSet = new Set(newNodes.map(node => node.id))
  const r0 = Math.max(80, Math.min(160, Math.sqrt(GW * GH / n) * 0.4))
  newNodes.forEach((node, i) => {
    const ang = (2 * Math.PI * i) / Math.max(newNodes.length, 1)
    node.x = centerX + r0 * Math.cos(ang)
    node.y = centerY + r0 * Math.sin(ang)
  })

  const pos = new Map()
  const vel = new Map()
  nodes.forEach(node => {
    pos.set(node.id, { x: node.x || cx, y: node.y || cy })
    vel.set(node.id, { x: 0, y: 0 })
  })

  // 增量布局：迭代次数少，靠已有位置快速收敛
  await simulateForces(nodes, graphEdges.value, pos, vel, {
    cx, cy, GW, GH,
    REPULSION: 3000 * Math.max(1, Math.sqrt(n / 50)),
    SPRING_LEN: Math.max(40, Math.min(150, Math.sqrt(GW * GH / n) * 0.4)),
    SPRING_K: 0.02,
    GRAVITY: 0.03,
    DAMPING: 0.6,
    ITER: n > 200 ? 30 : 60,
    newIdSet,
  })
}

// 全量布局（首次加载或重置后使用），异步执行避免阻塞主线程
async function runLayout() {
  const nodes = graphNodes.value
  const n = nodes.length
  if (n === 0) return
  const GW = graphW.value
  const GH = graphH.value
  const cx = GW / 2
  const cy = GH / 2
  const r = Math.min(GW, GH) / 2 - 80

  const pos = new Map()
  const vel = new Map()
  nodes.forEach((node, i) => {
    const ang = (2 * Math.PI * i) / n
    pos.set(node.id, { x: cx + r * Math.cos(ang), y: cy + r * Math.sin(ang) })
    vel.set(node.id, { x: 0, y: 0 })
  })

  // 动态参数：根据节点数自适应
  await simulateForces(nodes, graphEdges.value, pos, vel, {
    cx, cy, GW, GH,
    REPULSION: 5000 * Math.max(1, Math.sqrt(n / 50)),
    SPRING_LEN: Math.max(40, Math.min(150, Math.sqrt(GW * GH / n) * 0.4)),
    SPRING_K: n > 100 ? 0.015 : 0.02,
    GRAVITY: n > 100 ? 0.01 : 0.02,
    DAMPING: 0.6,
    // 大幅减少迭代次数：500+节点用更少迭代，靠增量布局微调
    ITER: n > 200 ? 80 : n > 50 ? 120 : 200,
  })
}

// 预计算边渲染数据：一次性算出端点坐标与是否关联中心节点，
// 避免模板中对每条边进行 4 次 Map 查找（节点不存在时回退到画布中心，避免 NaN）
const graphEdgesWithCoords = computed(() => {
  return graphEdges.value.map(e => {
    const s = nodeById.get(e.source)
    const t = nodeById.get(e.target)
    return {
      id: e.id,
      x1: s && s.x != null ? s.x : graphW.value / 2,
      y1: s && s.y != null ? s.y : graphH.value / 2,
      x2: t && t.x != null ? t.x : graphW.value / 2,
      y2: t && t.y != null ? t.y : graphH.value / 2,
      isCenter: e.source === graphCenterId.value || e.target === graphCenterId.value,
    }
  })
})

// 节点大小常量（视觉半径，单位 px，即屏幕上看到的实际大小）
// 中心节点的 1.7 倍放大同时作用于 base/min/max 三个值：
// 普通节点范围 [NODE_RADIUS_MIN, NODE_RADIUS_MAX]，中心节点范围自动为 1.7 倍。
const NODE_RADIUS_BASE = 12            // 普通节点基础视觉半径
const NODE_RADIUS_CENTER_FACTOR = 1.7  // 中心节点放大系数（1.7 倍）
const NODE_RADIUS_MIN = 10             // 普通节点最小视觉半径（缩小到极限时的下限）
const NODE_RADIUS_MAX = 100            // 普通节点最大视觉半径（放大到极限时的上限）

// 节点大小：视觉半径 = base × scale（随缩放线性变化）。
// 中心节点整条范围（base/min/max）都放大 1.7 倍，保证任意缩放级别下始终比其他节点大 1.7 倍。
// 由于 viewBox 会把 SVG 坐标放大 scale 倍，这里将 clamp 后的视觉半径除以 scale
// 得到 SVG 坐标半径，从而保证屏幕上看到的实际大小正好等于 clamp 后的视觉半径。
function nodeRadius(n) {
  const scale = graphScale.value || 1
  const factor = n.is_center ? NODE_RADIUS_CENTER_FACTOR : 1
  const base = NODE_RADIUS_BASE * factor
  const min = NODE_RADIUS_MIN * factor
  const max = NODE_RADIUS_MAX * factor
  const visual = base * scale
  const clamped = Math.max(min, Math.min(max, visual))
  // 除以 scale 抵消 viewBox 放大，屏幕视觉大小 = clamped
  return clamped / scale
}

// 点击节点：将该节点设为中心并展示详情，同时确保邻居已展开
function onNodeClick(n) {
  // 将点击的节点设为中心实体（更新中心标记和中心 id，不移动节点位置）
  if (graphCenterId.value !== n.id) {
    const prevCenter = graphCenterId.value
    graphCenterId.value = n.id
    if (prevCenter != null && nodeById.has(prevCenter)) {
      nodeById.get(prevCenter).is_center = false
    }
    nodeById.get(n.id).is_center = true
  }
  // 展开邻居（首次点击拉取邻居并做增量布局，让新增节点围绕中心显示）
  expandNode(n.id)
}

/* ============ 检索状态重置 ============ */
// 清空输入框时重置检索结果，不触发搜索请求
function resetSearch() {
  searchResults.value = []
  searchResultsIsFallback.value = false
  activeResultIndex.value = 0
  searchHint.value = ''
}

/* ============ SVG 缩放 ============ */
// 鼠标滚轮缩放：以鼠标位置为中心缩放，使用 requestAnimationFrame 节流避免高频重绘
function onSvgWheel(e) {
  // .prevent 修饰符已处理 preventDefault，无需重复调用
  if (_wheelRaf) return
  // 提前提取事件属性，避免闭包持有整个 MouseEvent（可能被浏览器回收或进入被动模式）
  const { clientX, clientY, deltaY } = e
  _wheelRaf = requestAnimationFrame(() => {
    _wheelRaf = null
    const svg = svgWrapRef.value && svgWrapRef.value.querySelector('svg')
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    const mx = (clientX - rect.left) / rect.width
    const my = (clientY - rect.top) / rect.height
    const delta = deltaY > 0 ? -0.12 : 0.12
    const newScale = Math.max(GRAPH_SCALE_MIN, Math.min(GRAPH_SCALE_MAX, graphScale.value + delta))
    // 平滑更新中心点（向鼠标位置偏移）
    const factor = 0.3
    graphPanX.value = graphPanX.value + (mx - graphPanX.value) * factor
    graphPanY.value = graphPanY.value + (my - graphPanY.value) * factor
    graphScale.value = newScale
  })
}

// 缩放按钮控制
function zoomIn() {
  graphScale.value = Math.min(GRAPH_SCALE_MAX, graphScale.value + 0.2)
}

function zoomOut() {
  graphScale.value = Math.max(GRAPH_SCALE_MIN, graphScale.value - 0.2)
}

function zoomReset() {
  graphScale.value = 1
  graphPanX.value = 0.5
  graphPanY.value = 0.5
}

/* ============ SVG 拖动 ============ */
// 鼠标按下：记录拖动起始位置，动态绑定 document 事件确保拖拽不丢失
function onSvgMouseDown(e) {
  // 仅响应左键，且不在节点上时才启动拖动
  if (e.button !== 0) return
  // 如果点击的是节点，不启动拖动（让节点点击事件处理）
  if (e.target.closest('.graph-node')) return
  isDragging.value = true
  dragStartX.value = e.clientX
  dragStartY.value = e.clientY
  dragStartPanX.value = graphPanX.value
  dragStartPanY.value = graphPanY.value
  // 缓存 SVG 尺寸，避免拖动期间 mousemove 高频触发 reflow
  if (svgWrapRef.value) {
    const svg = svgWrapRef.value.querySelector('svg')
    if (svg) _cachedSvgRect = svg.getBoundingClientRect()
  }
  // 绑定 document 级别事件，确保鼠标移出 SVG 区域后仍能响应拖拽
  document.addEventListener('mousemove', onSvgMouseMove)
  document.addEventListener('mouseup', onSvgMouseUp)
}

// 鼠标移动：更新 viewBox 中心点实现拖动效果
function onSvgMouseMove(e) {
  if (!isDragging.value || !_cachedSvgRect) return
  // 使用 mousedown 时缓存的 SVG 尺寸，避免每帧触发 reflow
  const dx = (e.clientX - dragStartX.value) / _cachedSvgRect.width
  const dy = (e.clientY - dragStartY.value) / _cachedSvgRect.height
  const scale = graphScale.value
  // 拖动方向与视口移动方向相反（鼠标向右拖，视图向左移）
  // 除以 scale 后拖动距离与鼠标物理位移 1:1 一致，任意缩放级别下手感相同
  graphPanX.value = Math.max(0, Math.min(1, dragStartPanX.value - dx / scale))
  graphPanY.value = Math.max(0, Math.min(1, dragStartPanY.value - dy / scale))
}

// 鼠标松开：结束拖动，解绑 document 事件
function onSvgMouseUp() {
  isDragging.value = false
  _cachedSvgRect = null
  document.removeEventListener('mousemove', onSvgMouseMove)
  document.removeEventListener('mouseup', onSvgMouseUp)
}

/* ============ 实体详情面板 ============ */
async function showEntityDetail(id) {
  const seq = ++detailSeq
  entityDetail.value = null
  try {
    const d = await api.getJson(`${GRAPH_API}/entities/${id}/`)
    if (seq !== detailSeq) return
    entityDetail.value = d
  } catch (e) {
    if (seq !== detailSeq) return
    entityDetail.value = null
    ElMessage.error('详情加载失败：' + errMsg(e, '未知错误'))
  }
}

function resetGraph() {
  graphNodes.value = []
  graphEdges.value = []
  nodeById.clear()
  edgeById.clear()
  expandedSet.clear()
  graphCenterId.value = null
  entityDetail.value = null
  searchResults.value = []
  searchHint.value = ''
  graphScale.value = 1    // 重置缩放
  graphPanX.value = 0.5   // 重置平移中心
  graphPanY.value = 0.5
}

/* ============ 社区列表 ============ */
// 社区列表翻页回调：el-pagination current-change 触发，loadCommunities 内部会同步更新当前页码
function onCommunityPageChange(p) {
  loadCommunities(p)
}

async function loadCommunities(page) {
  const seq = ++communitySeq
  communityPage.value = page
  communityLoading.value = true
  // 翻页/筛选时清理社区详情缓存，避免内存无限增长
  communityDetailMap.value = {}
  expandedCommunityId.value = null
  try {
    const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (communityLevelFilter.value) params.set('level', communityLevelFilter.value)
    const q = (communityQuery.value || '').trim()
    if (q) params.set('q', q)

    const data = await api.getJson(`${GRAPH_API}/communities/?${params.toString()}`)
    // 竞态检查：若有更新的请求已发出，丢弃本次结果
    if (seq !== communitySeq) return
    communities.value = data.results || []
    communityTotal.value = data.count || 0
  } catch (e) {
    if (seq !== communitySeq) return
    communities.value = []
    communityTotal.value = 0
    ElMessage.error('加载失败：' + errMsg(e, '未知错误'))
  } finally {
    if (seq === communitySeq) communityLoading.value = false
  }
}

async function toggleCommunityDetail(id) {
  // 同一社区只请求一次；再次点击收起
  if (expandedCommunityId.value === id) {
    expandedCommunityId.value = null
    return
  }
  expandedCommunityId.value = id
  if (communityDetailMap.value[id]) return
  try {
    const d = await api.getJson(`${GRAPH_API}/communities/${id}/`)
    // 整体替换触发响应式，确保模板可靠刷新；同时避免直接 addProperty 的隐式依赖
    communityDetailMap.value = { ...communityDetailMap.value, [id]: d }
  } catch (e) {
    // 请求失败：标记为空对象避免重复请求，显示无实体提示
    communityDetailMap.value = { ...communityDetailMap.value, [id]: { entities: [], error: true } }
    ElMessage.error('加载失败：' + errMsg(e, '未知错误'))
  }
}

function openEntityFromCommunity(id) {
  // 跳到图谱 Tab 并渲染该实体的关系子图
  activeTab.value = 'graph'
  loadSubgraph(id, 2)
}

/* ============ 手动触发社区检测 ============ */
function confirmDetect() {
  // 关键操作二次确认：将重建全部社区并调用 LLM 生成摘要，耗时较长
  confirm({
    message: '将重建全部社区并调用 LLM 生成摘要，耗时较长',
    title: '重新检测社区', confirmText: '确认提交', errorText: '提交失败',
  }, async () => {
    const res = await api.postJson(`${GRAPH_API}/communities/detect/`, {})
    ElMessage.success(res.detail || '任务已提交')
    // 启动轮询：每 3 秒刷新社区列表，最多 10 次（30 秒），直到社区数据更新
    startDetectPolling()
  })
}

// 社区检测轮询：定期刷新社区列表，直到检测完成或达到最大次数
function startDetectPolling() {
  // 清除旧轮询
  if (detectTimer) clearInterval(detectTimer)
  detectPollCount = 0
  const prevTotal = communityTotal.value
  detectTimer = setInterval(async () => {
    detectPollCount++
    await loadCommunities(1)
    // 检测完成：社区数量发生变化，或达到最大轮询次数
    if (communityTotal.value !== prevTotal || detectPollCount >= DETECT_POLL_MAX) {
      clearInterval(detectTimer)
      detectTimer = null
      if (detectPollCount >= DETECT_POLL_MAX && communityTotal.value === prevTotal) {
        ElMessage.info('社区检测仍在进行中，请稍后手动刷新查看')
      }
    }
  }, DETECT_POLL_INTERVAL)
}

/* ============ 页面初始化 ============ */
onMounted(() => {
  userStore.restore()
  loadCommunities(1)
})

onUnmounted(() => {
  // 清理社区检测轮询定时器
  if (detectTimer) clearInterval(detectTimer)
  // 清理可能残留的 document 事件监听器（拖拽中途卸载组件时）
  document.removeEventListener('mousemove', onSvgMouseMove)
  document.removeEventListener('mouseup', onSvgMouseUp)
})
</script>

<style scoped>

/* el-tabs 三件套（撑满 + 面板内部滚动 + pane flex 列）由全局 .tabs-fill 提供 */

/* ===== 工具栏 ===== */
.graph-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 4px 0 12px;
}

.toolbar-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* ===== 检索提示与结果条 ===== */
.graph-search-hint {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 8px;
}

.graph-search-results {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.gsr-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--app-border);
  border-radius: 16px;
  font-size: 13px;
  cursor: pointer;
  background: var(--app-card-bg);
  transition: all 0.15s;
}

.gsr-item:hover {
  border-color: #2563eb;
  color: #2563eb;
}

.gsr-item.gsr-active {
  border-color: #2563eb;
  background: #eff6ff;
  color: #2563eb;
}

.gsr-item .gsr-score {
  font-size: 11px;
  color: var(--app-text-sub);
}

/* ===== 图谱内容排列：SVG 画布 + 实体详情面板 =====
   撑满 tab 剩余高度；内容超高（如小屏）时在容器内滚动，不让整个 tab 滚动 */
.graph-layout {
  flex: 1;
  min-height: 0;
  display: flex;
  gap: 12px;
  align-items: stretch;
  overflow-y: auto;
}

.graph-canvas-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
  position: relative;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-card-bg);
  overflow: hidden;
  cursor: grab; /* 默认显示抓手图标，表示可拖动 */
}

/* 拖动中：抓手按下状态 */
.graph-canvas-grabbing,
.graph-svg-grabbing {
  cursor: grabbing !important;
}

/* 缩放控制按钮组（悬浮在图谱右上角） */
.graph-zoom-controls {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 4px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.zoom-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  border-radius: 4px;
  cursor: pointer;
  font-size: 16px;
  color: var(--app-text);
  transition: background 0.15s;
}

.zoom-btn:hover {
  background: var(--app-menu-hover);
}

.zoom-btn-reset {
  font-size: 14px;
  margin-left: 2px;
}

.zoom-label {
  font-size: 12px;
  color: var(--app-text-sub);
  min-width: 40px;
  text-align: center;
  user-select: none;
}

/* SVG 画布：随 graph-layout 剩余高度自适应（viewBox 等比缩放），
   不再固定 560px，避免小屏时被裁剪或整页滚动 */
.graph-svg {
  flex: 1;
  width: 100%;
  height: 100%;
  display: block;
  user-select: none; /* 防止拖动时选中文本 */
}

/* 边：灰线，中心关联边加粗 */
.graph-edge {
  stroke: var(--app-border);
}

/* 节点：手型指针表示可点击，拖动时保持 pointer 不变 */
.graph-node {
  cursor: pointer;
}

.graph-node-circle {
  transition: opacity 0.15s;
}

.graph-node:hover .graph-node-circle {
  opacity: 0.85;
}

.graph-node-label {
  fill: var(--app-text);
  pointer-events: none;
}

.graph-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--app-text-sub);
  font-size: 14px;
  padding: 60px 0;
}

.graph-empty-icon {
  font-size: 48px;
  margin-bottom: 12px;
}

/* 实体详情面板（右侧固定宽度，与画布等高，内容超高时面板内滚动） */
.entity-panel {
  width: 320px;
  flex-shrink: 0;
  min-height: 0;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-card-bg);
  padding: 14px;
  overflow-y: auto;
}

/* 面板占位符：未选中节点时显示引导提示 */
.ep-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 200px;
  color: var(--app-text-sub);
}

.ep-placeholder-icon {
  font-size: 32px;
  margin-bottom: 12px;
  opacity: 0.5;
}

.ep-placeholder-text {
  font-size: 13px;
}

.entity-panel-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
  word-break: break-all;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.tag {
  display: inline-block;
  background: var(--app-menu-hover);
  color: var(--app-text);
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 4px;
  margin-right: 6px;
  margin-bottom: 4px;
}

.tag-info {
  background: #eff6ff;
  color: #2563eb;
}

.ep-meta {
  font-size: 13px;
  color: var(--app-text-sub);
  margin-bottom: 10px;
}

.ep-section {
  margin-bottom: 12px;
}

.ep-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-sub);
  margin-bottom: 6px;
}

.ep-desc {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--app-text);
}

.ep-docs {
  list-style: none;
  padding: 0;
  margin: 0;
}

.ep-docs li {
  font-size: 13px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--app-border);
  word-break: break-all;
}

.ep-actions {
  display: flex;
  gap: 8px;
}

.text-sub {
  color: var(--app-text-sub);
  font-size: 13px;
}

/* ===== 社区列表：撑满 tab 剩余高度，社区卡片列表在容器内滚动，分页固定在底部 ===== */
.community-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-card-bg);
  padding: 14px 16px;
}

.community-empty {
  padding: 40px 0;
  text-align: center;
  color: var(--app-text-sub);
}

.community-empty-icon {
  font-size: 48px;
  margin-bottom: 8px;
}

.community-card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-card-bg);
  padding: 14px 16px;
  margin-bottom: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.community-card:hover {
  border-color: #2563eb;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.community-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.community-card-topic {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
}

.community-card-time {
  font-size: 12px;
  color: var(--app-text-sub);
}

.badge-default {
  background: var(--app-menu-hover);
  color: var(--app-text);
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 4px;
}

.community-card-summary {
  font-size: 13px;
  color: var(--app-text-sub);
  line-height: 1.6;
  margin-bottom: 8px;
}

.community-card-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--app-text-sub);
}

/* 社区详情：社区内实体列表 */
.community-entities {
  margin-top: 10px;
  border-top: 1px dashed var(--app-border);
  padding-top: 10px;
}

.community-entities-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-sub);
  margin-bottom: 8px;
}

.community-entity-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--app-border);
  border-radius: 16px;
  font-size: 13px;
  cursor: pointer;
  margin: 0 6px 6px 0;
  background: var(--app-card-bg);
  transition: all 0.15s;
}

.community-entity-item:hover {
  border-color: #2563eb;
  color: #2563eb;
}

.community-entity-type {
  font-size: 11px;
  color: var(--app-text-sub);
}

.community-loading {
  padding: 20px 0;
  text-align: center;
  color: var(--app-text-sub);
}

.community-pagination {
  margin-top: 12px;
  flex-shrink: 0;
  justify-content: flex-end;
}
</style>
