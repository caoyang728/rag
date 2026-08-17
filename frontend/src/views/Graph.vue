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
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ============ 图谱浏览 ============ -->
      <el-tab-pane label="🕸️ 图谱浏览" name="graph">
        <div class="graph-toolbar">
          <el-input
            v-model="entityQuery"
            placeholder="🔍 语义检索实体，如：公司年度目标"
            clearable
            style="flex: 1; min-width: 240px"
            @keyup.enter="doEntitySearch"
            @clear="doEntitySearch"
          />
          <div class="toolbar-actions">
            <el-select v-model="entityTypeFilter" placeholder="全部类型" clearable style="width: 120px">
              <el-option label="人物" value="PERSON" />
              <el-option label="组织" value="ORG" />
              <el-option label="概念" value="CONCEPT" />
              <el-option label="术语" value="TERM" />
              <el-option label="产品" value="PRODUCT" />
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
          <div class="graph-canvas-wrap">
            <!-- 原生 SVG 力导向图：节点点击扩展邻居，悬停提示实体信息 -->
            <svg
              v-if="graphNodes.length"
              class="graph-svg"
              :viewBox="`0 0 ${GRAPH_W} ${GRAPH_H}`"
              @click="onSvgClick"
            >
              <!-- 边：中心关联边加粗展示，便于追踪中心实体关系 -->
              <line
                v-for="e in graphEdges"
                :key="e.id"
                :x1="nodeX(e.source)"
                :y1="nodeY(e.source)"
                :x2="nodeX(e.target)"
                :y2="nodeY(e.target)"
                :stroke-width="e.source === graphCenterId || e.target === graphCenterId ? 2 : 1"
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
                  :stroke-width="n.is_center ? 3 : 0"
                  class="graph-node-circle"
                />
                <text :y="nodeRadius(n) + 14" text-anchor="middle" class="graph-node-label">{{ n.name }}</text>
                <title>{{ n.name }}（{{ typeLabel(n.type) }}）{{ n.description ? '：' + n.description : '' }}</title>
              </g>
            </svg>
            <!-- 空态：无图数据时的引导文案 -->
            <div v-else class="graph-empty">
              <div class="graph-empty-icon">🕸️</div>
              <div>输入关键词检索实体，或点击社区中的实体查看其关系子图</div>
            </div>
          </div>

          <!-- 实体详情面板（右侧固定宽度，与画布等高） -->
          <div v-show="entityDetail" class="entity-panel">
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
            <div v-else class="text-sub ep-loading">加载中...</div>
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
import { onMounted, ref } from 'vue'
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

// SVG 视口尺寸（viewBox 固定，容器内等比缩放）
const GRAPH_W = 900
const GRAPH_H = 600

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

const graphNodes = ref([])        // 节点数组（布局后含 x/y）
const graphEdges = ref([])        // 边数组
const graphCenterId = ref(null)   // 当前中心实体 id
const entityDetail = ref(null)    // 实体详情面板数据
const expandedSet = new Set()     // 已展开过邻居的节点 id（避免重复请求）
// id → 节点对象索引（非响应式，仅用于坐标/度数快速查找）
const nodeById = new Map()
// id → 边对象（非响应式，用于去重；渲染数组为 graphEdges）
const edgeById = new Map()

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
  const type = entityTypeFilter.value
  searchHint.value = '🔍 检索中...'
  searchResults.value = []

  try {
    const params = new URLSearchParams({ q, top_k: 10 })
    if (type) params.set('type', type)
    const data = await api.getJson(`${GRAPH_API}/entities/search/?${params.toString()}`)
    const results = data.results || []

    if (!results.length) {
      // 语义检索无命中：回退到名称模糊检索，保证"实体存在但无向量"场景可用
      const fb = await nameSearchFallback(q, type)
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
  searchHint.value = '🕸️ 子图加载中...'
  try {
    const data = await api.getJson(`${GRAPH_API}/entities/${entityId}/neighbors/?depth=${depth}`)
    mergeSubgraph(data, true)
    runLayout()
    showEntityDetail(entityId)
  } catch (e) {
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
  try {
    const data = await api.getJson(`${GRAPH_API}/entities/${entityId}/neighbors/?depth=1`)
    mergeSubgraph(data, false)
    runLayout()
    showEntityDetail(entityId)
  } catch (e) {
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
      is_center: isNewCenter ? n.is_center : (existing ? existing.is_center : false),
    })
  })
  // 新中心实体强制标记为中心
  if (isNewCenter && graphCenterId.value != null && nodeById.has(graphCenterId.value)) {
    nodeById.get(graphCenterId.value).is_center = true
  }
  // 边按 id 去重：多次扩展邻居会返回重复边，避免渲染重叠
  newEdges.forEach(e => {
    if (!edgeById.has(e.id)) {
      edgeById.set(e.id, e)
      graphEdges.value.push(e)
    }
  })
  // 用去重后的 Map 值重建节点数组（保证同一节点只渲染一次）
  graphNodes.value = [...nodeById.values()]
}

/* ============ 力导向布局（原生实现，替代原 ECharts force 布局） ============
 * 斥力（所有节点对）+ 弹簧力（边）+ 中心引力，迭代收敛后写入节点 x/y。
 * 节点数通常为几十个，O(n²) 迭代在同步计算内可接受。
 */
function runLayout() {
  const nodes = graphNodes.value
  const n = nodes.length
  if (n === 0) return
  const pos = new Map()
  const vel = new Map()
  const cx = GRAPH_W / 2
  const cy = GRAPH_H / 2
  const r = Math.min(GRAPH_W, GRAPH_H) / 2 - 80
  // 初始位置：环形分布，避免随机初值导致收敛到局部重叠
  nodes.forEach((node, i) => {
    const ang = (2 * Math.PI * i) / n
    pos.set(node.id, { x: cx + r * Math.cos(ang), y: cy + r * Math.sin(ang) })
    vel.set(node.id, { x: 0, y: 0 })
  })
  const idx = new Map(nodes.map((node, i) => [node.id, i]))
  // 过滤掉端点不在图中的边（如删图后残留边）
  const adj = graphEdges.value
    .map(e => ({ a: idx.get(e.source), b: idx.get(e.target) }))
    .filter(e => e.a !== undefined && e.b !== undefined)

  const REPULSION = 5000   // 斥力系数
  const SPRING_LEN = 130   // 边理想长度
  const SPRING_K = 0.02    // 弹簧刚度
  const GRAVITY = 0.02     // 中心引力，防止整体漂移出画布
  const DAMPING = 0.6      // 速度阻尼
  const ITER = 260

  for (let iter = 0; iter < ITER; iter++) {
    // 斥力（所有节点对，力与距离平方成反比）
    for (let i = 0; i < n; i++) {
      const pi = pos.get(nodes[i].id)
      const vi = vel.get(nodes[i].id)
      for (let j = i + 1; j < n; j++) {
        const pj = pos.get(nodes[j].id)
        let dx = pi.x - pj.x
        let dy = pi.y - pj.y
        let d2 = dx * dx + dy * dy
        // 完全重叠时施加随机扰动，避免除零/死锁
        if (d2 < 1) {
          dx = Math.random() - 0.5
          dy = Math.random() - 0.5
          d2 = dx * dx + dy * dy || 1
        }
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
    // 弹簧力（边，趋向理想长度）
    for (const { a, b } of adj) {
      const pa = pos.get(nodes[a].id)
      const pb = pos.get(nodes[b].id)
      const dx = pb.x - pa.x
      const dy = pb.y - pa.y
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01
      const f = SPRING_K * (d - SPRING_LEN)
      const fx = (dx / d) * f
      const fy = (dy / d) * f
      const va = vel.get(nodes[a].id)
      const vb = vel.get(nodes[b].id)
      va.x += fx
      va.y += fy
      vb.x -= fx
      vb.y -= fy
    }
    // 中心引力 + 阻尼更新位置
    for (const node of nodes) {
      const p = pos.get(node.id)
      const v = vel.get(node.id)
      v.x += (cx - p.x) * GRAVITY
      v.y += (cy - p.y) * GRAVITY
      v.x *= DAMPING
      v.y *= DAMPING
      p.x += v.x
      p.y += v.y
    }
  }
  // 边界约束：节点不超出画布（留出标签空间）
  const pad = 34
  for (const node of nodes) {
    const p = pos.get(node.id)
    node.x = Math.max(pad, Math.min(GRAPH_W - pad, p.x))
    node.y = Math.max(pad, Math.min(GRAPH_H - pad, p.y))
  }
}

// SVG 渲染辅助：边端点坐标（节点不存在时回退到画布中心，避免 NaN）
function nodeX(id) {
  const n = nodeById.get(id)
  return n && n.x != null ? n.x : GRAPH_W / 2
}

function nodeY(id) {
  const n = nodeById.get(id)
  return n && n.y != null ? n.y : GRAPH_H / 2
}

// 节点大小：中心实体放大，其余按度数自适应
function nodeRadius(n) {
  if (n.is_center) return 26
  const degree = graphEdges.value.filter(e => e.source === n.id || e.target === n.id).length
  return 14 + Math.min(degree, 8) * 2
}

// 点击节点：展开其邻居并展示详情（已展开仅刷新详情）
function onNodeClick(n) {
  expandNode(n.id)
}

// 点击画布空白：无操作（保留当前图，便于浏览）
function onSvgClick() {}

/* ============ 实体详情面板 ============ */
async function showEntityDetail(id) {
  entityDetail.value = null
  try {
    const d = await api.getJson(`${GRAPH_API}/entities/${id}/`)
    entityDetail.value = d
  } catch (e) {
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
    // 翻页后收起已展开的社区，避免详情错位
    expandedCommunityId.value = null
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
    communityDetailMap.value[id] = d
  } catch (e) {
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
    // 异步任务完成后刷新社区列表
    setTimeout(() => loadCommunities(1), 5000)
  })
}

/* ============ 页面初始化 ============ */
onMounted(() => {
  userStore.restore()
  loadCommunities(1)
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
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-card-bg);
  overflow: hidden;
}

/* SVG 画布：随 graph-layout 剩余高度自适应（viewBox 等比缩放），
   不再固定 560px，避免小屏时被裁剪或整页滚动 */
.graph-svg {
  flex: 1;
  width: 100%;
  height: 100%;
  display: block;
}

/* 边：灰线，中心关联边加粗 */
.graph-edge {
  stroke: var(--app-border);
}

/* 节点 hover 放大提示（原生 title 已带实体信息，无需额外 tooltip 库） */
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
  font-size: 12px;
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

.ep-loading {
  text-align: center;
  padding: 40px 0;
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
