<template>
  <div class="route-panel-page">
    <!-- 工具栏：时间窗口 + 组织筛选(左) + 手动聚合(右) -->
    <div class="eval-toolbar mb-16">
      <div class="route-toolbar">
        <div class="filters">
          <el-select v-model="days" style="width: 110px" @change="loadRouteAnalysis">
            <el-option v-for="opt in options" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <el-select v-model="org.deptId" placeholder="全部部门" clearable style="width: 160px" @change="onDeptChange">
            <el-option v-for="d in org.departments" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="org.teamId" placeholder="全部团队" clearable style="width: 160px" :disabled="!org.deptId" @change="loadRouteAnalysis">
            <el-option v-for="t in org.teamsOfDept" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
          <el-button @click="loadRouteAnalysis">🔄 刷新</el-button>
          <span class="text-sub text-sm route-summary">{{ summaryText }}</span>
        </div>
        <div class="aggregate">
          <el-date-picker
            v-model="reportDate"
            type="date"
            value-format="YYYY-MM-DD"
            placeholder="选择回补日期"
            style="width: 150px"
            :clearable="true"
          />
          <el-button type="primary" :loading="aggregating" @click="runRouteAggregate">🗃️ 聚合路由数据</el-button>
        </div>
      </div>
    </div>

    <!-- KPI 卡片：总量 + 四层命中数(缺失层补 0) -->
    <div class="kpi-grid mb-16">
      <div class="kpi-card"><div class="kpi-label">路由请求数</div><div class="kpi-value">{{ kpi.total }}</div></div>
      <div class="kpi-card kpi-good"><div class="kpi-label">Wiki 直答</div><div class="kpi-value">{{ kpi.wiki }}</div></div>
      <div class="kpi-card"><div class="kpi-label">GraphRAG 局部</div><div class="kpi-value">{{ kpi.local }}</div></div>
      <div class="kpi-card kpi-highlight"><div class="kpi-label">GraphRAG 全局</div><div class="kpi-value">{{ kpi.global }}</div></div>
      <div class="kpi-card"><div class="kpi-label">RAG 兜底</div><div class="kpi-value">{{ kpi.rag }}</div></div>
    </div>

    <!-- 四层命中分布 -->
    <div class="eval-panel mb-16">
      <PanelHeader titleClass="eval-panel-title">
        四层路由命中分布
        <template #actions>
          <div class="text-sub text-sm">命中率 = 该层命中数 / 路由请求总数；质量分 = 该层已评估 QA 的 12 维均分</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="coverageRows" v-loading="loading" size="small">
          <el-table-column label="路由层级" min-width="130">
            <template #default="{ row }">
              <el-tag size="small" :style="routeTagStyle(row.route)">{{ routeLabel(row.route) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="命中数" width="90" prop="count" align="right" />
          <el-table-column label="命中率" width="100" align="right">
            <template #default="{ row }">{{ fmtPct(row.share) }}</template>
          </el-table-column>
          <el-table-column label="平均置信度" width="110" align="right">
            <template #default="{ row }">
              <!-- 平均置信度可能为 null(该层暂无聚合数据),空值显示 -- 避免 NaN% -->
              <span v-if="isFinite(Number(row.avg_confidence))">{{ fmtPct(row.avg_confidence) }}</span>
              <span v-else class="text-sub">--</span>
            </template>
          </el-table-column>
          <el-table-column label="平均延迟(ms)" width="110" align="right" prop="avg_latency_ms" />
          <el-table-column label="平均质量分" width="110" align="right">
            <template #default="{ row }">
              <el-tag v-if="hasQuality(row.avg_answer_quality)" :type="scoreTagType(row.avg_answer_quality)" size="small">{{ fmtPct(row.avg_answer_quality) }}</el-tag>
              <span v-else class="text-sub">--</span>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无数据,请先在上方点击「聚合路由数据」(或等待每日定时聚合)" :image-size="70" /></template>
        </el-table>
      </div>
    </div>

    <!-- 按天命中趋势(堆叠条) -->
    <div class="eval-panel mb-16">
      <PanelHeader titleClass="eval-panel-title">
        按天命中趋势
        <template #actions>
          <div class="text-sub text-sm">每天各层命中数堆叠展示,观察路由分流随内容更新的变化</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <div v-if="!trendRows.length" class="text-sub">暂无按天数据</div>
        <template v-else>
          <!-- 图例:层级颜色说明,顺序与 ROUTE_ORDER 一致 -->
          <div class="route-legend mb-8">
            <span v-for="(r, i) in ROUTE_ORDER" :key="r" class="route-legend-item">
              <span class="route-legend-dot" :style="{ background: ROUTE_COLOR[i] }"></span>{{ ROUTE_LABEL[r] }}
            </span>
          </div>
          <div class="route-trend-list">
            <div v-for="d in trendRows" :key="d.date" class="route-trend-row">
              <span class="route-trend-date">{{ d.date }}</span>
              <!-- 当日无任何命中时仅显示 0,不渲染空条 -->
              <template v-if="d.total > 0">
                <div class="route-trend-bar">
                  <!-- 堆叠条总宽 100%,每层占比 = 该层当日命中 / 当日总命中 -->
                  <span
                    v-for="seg in d.segments"
                    :key="seg.route"
                    class="route-trend-seg"
                    :style="{ width: seg.pct + '%', background: seg.color }"
                    :title="`${seg.label} ${seg.count}`"
                  >{{ seg.count }}</span>
                </div>
              </template>
              <span v-else class="text-sub text-sm">0</span>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- 各层回答质量对比 -->
    <div class="eval-panel route-quality-panel">
      <PanelHeader titleClass="eval-panel-title">
        各层回答质量对比
        <template #actions>
          <div class="text-sub text-sm">12 维按 4 大类聚合;无评估数据的层显示 "--"</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="qualityRows" v-loading="loading" size="small">
          <el-table-column label="路由层级" min-width="130">
            <template #default="{ row }">
              <el-tag size="small" :style="routeTagStyle(row.route)">{{ row.label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="整体均分" width="110" align="right">
            <template #default="{ row }">
              <el-tag v-if="row.overall !== null && row.overall !== undefined" :type="scoreTagType(row.overall)" size="small">{{ fmtPct(row.overall) }}</el-tag>
              <span v-else class="text-sub">--</span>
            </template>
          </el-table-column>
          <el-table-column v-for="g in GROUP_CELLS" :key="g.key" :label="g.label" width="100" align="right">
            <template #default="{ row }">
              <!-- 该层未评估该大类时显示 --,避免误导 -->
              <el-tag v-if="row.groups && row.groups[g.key] !== undefined && row.groups[g.key] !== null" :type="scoreTagType(row.groups[g.key])" size="small">{{ fmtPct(row.groups[g.key]) }}</el-tag>
              <span v-else class="text-sub">--</span>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无评估数据,在「回答质量」Tab 评估后此处展示各层质量对比" :image-size="70" /></template>
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { errMsg } from '../../utils/format'
import PanelHeader from '../base/PanelHeader.vue'
import { useListLoader } from '../../composables/useListLoader'
import { useTimeRange } from '../../composables/useTimeRange'
import { useOrgFilter } from './useOrgFilter'
import { ROUTE_COLOR, ROUTE_LABEL, ROUTE_ORDER, fmtPct, scoreTagType } from './constants'

/**
 * 路由分析 Tab（原 route 面板）：四层路由命中率 + 按天趋势 + 各层质量对比
 * - 三层路由 + RAG 兜底共 4 层,层级固定顺序与后端 ROUTE_ORDER 对齐,缺失层补 0
 * - 手动聚合可指定回补日期(留空聚合昨天),派发后 3 秒自动刷新
 */
const org = useOrgFilter()
const { days, options } = useTimeRange()
const reportDate = ref('')
const aggregating = ref(false)

// 质量对比表固定 4 大类(与后端 quality_by_route.groups 键对齐)
const GROUP_CELLS = [
  { key: 'retrieval', label: '检索' },
  { key: 'quality', label: '回答质量' },
  { key: 'safety', label: '安全' },
  { key: 'business', label: '业务' },
]

const kpi = reactive({ total: 0, wiki: 0, local: 0, global: 0, rag: 0 })
const coverageRows = ref([])
const trendRows = ref([])
const qualityRows = ref([])

const summaryText = computed(() => `窗口 ${days.value} 天 · 范围 ${org.scopeText} · 路由请求 ${kpi.total}`)

function onDeptChange() {
  org.onDeptChange()
  loadRouteAnalysis()
}

const { loading, load: loadRouteAnalysis } = useListLoader(async () => {
  const params = new URLSearchParams()
  params.set('days', days.value)
  if (org.deptId.value) params.set('dept_id', org.deptId.value)
  if (org.teamId.value) params.set('team_id', org.teamId.value)

  const data = await api.getJson('/api/v1/analytics/eval-dashboard/route-analysis/?' + params.toString())
  renderRouteKpi(data)
  renderRouteTrend(data)
  renderRouteQuality(data)
}, { errorPrefix: '路由分析加载失败' })

// KPI:总量 + 四层命中数(缺失层补 0)
function renderRouteKpi(data) {
  const byRoute = {}
  ;(data.coverage_by_route || []).forEach(r => { byRoute[r.route] = r.count })
  kpi.total = data.total || 0
  kpi.wiki = byRoute.wiki || 0
  kpi.local = byRoute.graphrag_local || 0
  kpi.global = byRoute.graphrag_global || 0
  kpi.rag = byRoute.rag || 0
  // 命中分布表与 KPI 同源,直接复用 coverage_by_route
  coverageRows.value = data.coverage_by_route || []
}

// 平均质量分判断:null/undefined/0 均视为无评估(旧逻辑:>0 才展示)
function hasQuality(v) {
  return v !== null && v !== undefined && Number(v) > 0
}

// 层级颜色 tag 样式(浅色底 + 主色文字,对应旧 .tag 内联着色)
function routeTagStyle(route) {
  const color = ROUTE_COLOR[ROUTE_ORDER.indexOf(route)] || '#6b7280'
  return { background: color + '22', color, borderColor: color + '33' }
}

function routeLabel(route) {
  return ROUTE_LABEL[route] || route
}

// 按天命中趋势:预计算每日各层占比,模板只做渲染(避免模板内重复计算)
function renderRouteTrend(data) {
  trendRows.value = (data.daily_trend || []).map(d => {
    const total = ROUTE_ORDER.reduce((s, r) => s + (d[r] || 0), 0)
    const segments = ROUTE_ORDER.map((r, i) => ({
      route: r,
      label: ROUTE_LABEL[r],
      color: ROUTE_COLOR[i],
      count: d[r] || 0,
      // 每层占比 = 该层当日命中 / 当日总命中
      pct: total > 0 ? Number((d[r] || 0) / total * 100).toFixed(2) : 0,
    }))
    return { date: d.date, total, segments }
  })
}

// 各层回答质量对比:只展示有质量数据的层,避免空层占位
function renderRouteQuality(data) {
  const qb = data.quality_by_route || {}
  const order = data.route_order || ROUTE_ORDER
  qualityRows.value = order
    .filter(r => qb[r] && qb[r].overall !== null && qb[r].overall !== undefined)
    .map(r => ({ route: r, label: ROUTE_LABEL[r] || r, overall: qb[r].overall, groups: qb[r].groups || {} }))
}

// 手动触发路由数据聚合(可指定回补日期,留空聚合昨天)
async function runRouteAggregate() {
  const body = reportDate.value ? { report_date: reportDate.value } : {}
  ElMessage.info(reportDate.value ? `正在聚合 ${reportDate.value} 的路由数据...` : '正在聚合昨天的路由数据...')
  aggregating.value = true
  try {
    await api.postJson('/api/v1/analytics/route-analysis/aggregate/', body)
    ElMessage.success('聚合已派发,稍后点击刷新查看结果')
    // 聚合为异步任务,3 秒后自动刷新一次
    setTimeout(() => loadRouteAnalysis(), 3000)
  } catch (e) {
    ElMessage.error('聚合派发失败: ' + errMsg(e, e))
  } finally {
    aggregating.value = false
  }
}

onMounted(loadRouteAnalysis)

defineExpose({ reload: loadRouteAnalysis })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,内容超高时在容器内部上下滑动 */
.route-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
}

.route-panel-page .eval-toolbar,
.route-panel-page .kpi-grid,
.route-panel-page .eval-panel:not(.route-quality-panel) {
  flex-shrink: 0;
}

.route-quality-panel {
  flex: 1;
  min-height: 200px; /* 最低高度:容器空间不足时保证表格可读,超出部分由容器滚动 */
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-16 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 表格占满面板剩余空间,行在表格内部滚动 */
.route-quality-panel .eval-panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.route-quality-panel .eval-panel-body .el-table {
  flex: 1;
  min-height: 0;
}

.route-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: nowrap;
}

.filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
  flex-wrap: wrap;
}

.aggregate {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.route-summary {
  white-space: nowrap;
  flex-shrink: 0;
  margin-left: 8px;
}

.mb-16 { margin-bottom: 16px; }
.mb-8 { margin-bottom: 8px; }

.text-sub { color: var(--el-text-color-secondary, #6b7280); }
</style>
