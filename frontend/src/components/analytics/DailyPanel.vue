<template>
  <div class="panel-body">
    <!-- 趋势区 + 摘要表合并为一张卡片，内部用分隔线间隔 -->
    <div class="app-card daily-card">
      <!-- 多日趋势区：标题 + 天数选择器 + 折线图 -->
      <div class="daily-trend-section">
        <div class="daily-chart-header">
          <div class="trend-head-title">📈 最近 {{ trendData.length }} 天趋势</div>
          <el-select v-model="trendDays" size="small" style="width: 80px" @change="loadDailyReport">
            <el-option label="7 天" :value="7" />
            <el-option label="14 天" :value="14" />
            <el-option label="30 天" :value="30" />
          </el-select>
        </div>
        <!-- QA次数/好评/差评 走左轴，准确率 走右轴；勾选状态由 dailyMetricVisible 恢复。
             图表撑满趋势区剩余高度（ECharts 随容器自适应） -->
        <div v-if="trendData.length > 1" class="trend-chart-box">
          <VChart :option="trendOption" :events="trendEvents" />
        </div>
        <div v-else class="chart-placeholder">{{ trendData.length === 0 ? '暂无数据' : '仅 1 天数据，暂无法绘制趋势图' }}</div>
      </div>

      <!-- 每日摘要对比：今日 vs 昨日 + 环比 -->
      <div class="daily-summary-section">
        <div class="summary-title">📅 每日摘要对比</div>
        <el-table :data="summaryRows" size="small" class="summary-table">
          <el-table-column label="指标" width="140">
            <template #default="{ row }">{{ row.label }}</template>
          </el-table-column>
          <el-table-column :label="`今日 (${todayDate})`" min-width="120">
            <template #default="{ row }">{{ row.todayText }}</template>
          </el-table-column>
          <el-table-column :label="`昨日 (${yesterdayDate})`" min-width="120">
            <template #default="{ row }">{{ row.yesterdayText }}</template>
          </el-table-column>
          <el-table-column label="环比" min-width="200">
            <template #default="{ row }">
              <span v-if="row.diff" class="text-sm" :class="row.diffCls">{{ row.diff }}</span>
              <span v-else class="text-sub">-</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { errMsg, fmtPct } from '../../utils/format'
import { useListLoader } from '../../composables/useListLoader'
import { useTheme } from '../../composables/useTheme'
import { buildTrendOption, chartThemeColors, trendLegendSelectChanged } from '../../utils/chart'
import VChart from '../base/VChart.vue'

/**
 * 日报详情 Tab：多日趋势折线图（QA/好评/差评/准确率）+ 今日 vs 昨日摘要对比（含环比）
 * 天数选择器独立于概览时间范围（forceDays 强制按 days 查询）；勾选状态内存保持，重载时恢复
 */
const props = defineProps({
  rootType: { type: String, default: '' }, // 当前根节点筛选（root_type）
})

const trendDays = ref(30)
const trendData = ref([])
const today = ref({})
const yesterday = ref({})
const todayDate = ref('-')
const yesterdayDate = ref('-')
// 日报趋势图的指标显示开关：勾选 checkbox 时更新开关并重渲染（与 ECharts 版一致）
const dailyMetricVisible = { qa: true, good: true, bad: true, accuracy: true }

const { load: loadDailyReport } = useListLoader(async () => {
  const rootType = props.rootType
  const rtQ = rootType ? `?root_type=${encodeURIComponent(rootType)}` : ''
  // 并行拉取日报对比数据和趋势数据，减少等待时间
  const [dailyData, trendResp] = await Promise.all([
    api.getJson('/api/v1/analytics/daily/' + rtQ),
    // 日报天数选择器独立于概览时间范围，forceDays 强制按 days 查询
    api.getJson(buildTrendUrl({ days: trendDays.value, forceDays: true, rootType })),
  ])

  today.value = dailyData.today || {}
  yesterday.value = dailyData.yesterday || {}
  todayDate.value = today.value.date || '-'
  yesterdayDate.value = yesterday.value.date || '-'
  // 缓存趋势数据，勾选指标时直接重渲染无需重新请求 API
  trendData.value = trendResp.trend || []
}, {
  // 失败时清空数据并提示；onError 存在时不会走 useListLoader 的默认提示
  onError: (e, { silent }) => {
    if (silent) return
    trendData.value = []
    today.value = {}
    yesterday.value = {}
    ElMessage.error('加载日报失败: ' + errMsg(e, '未知错误'))
  },
})

/** 构造趋势报表接口 URL：日报场景强制按 days 查询（不随概览 custom 状态漂移），追加 root_type */
function buildTrendUrl(opts = {}) {
  const url = `/api/v1/analytics/trend/?days=${opts.days || 30}`
  if (opts.rootType) return url + '&root_type=' + encodeURIComponent(opts.rootType)
  return url
}

/* ===== 多日趋势图 ===== */
const xLabel = t => String(t.date || '').slice(5)
const dailySeries = computed(() => [
  { key: 'qa', label: 'QA次数', color: '#2563eb', axis: 'left', visible: dailyMetricVisible.qa, get: t => t.qa_count || 0 },
  { key: 'good', label: '好评', color: '#059669', axis: 'left', visible: dailyMetricVisible.good, get: t => t.good || 0 },
  { key: 'bad', label: '差评', color: '#dc2626', axis: 'left', visible: dailyMetricVisible.bad, get: t => t.bad || 0 },
  { key: 'accuracy', label: '准确率', color: '#7c3aed', axis: 'right', dashed: true, visible: dailyMetricVisible.accuracy, get: t => (t.accuracy || 0) * 100 },
])
const dailyAxes = {
  // 左轴：计数值（QA/好评/差评），从 0 起算、不设上限；右轴：准确率百分比，0-100 封顶
  left: { toFixed: 0, includeZero: true, clampMin: 0, clampMax: null },
  right: { unit: '%', toFixed: 0, includeZero: false, clampMin: 0, clampMax: 100, defaultMin: 0, defaultMax: 100, minSpan: 5, padMin: 5, padMax: 5 },
}

const { isDark } = useTheme()
// 图表主题色：依赖 isDark，主题切换时重建（canvas 图表需取计算后的 CSS 变量色值）
const chartColors = computed(() => chartThemeColors(isDark.value))
// 趋势图 option：数据/勾选配置变化时自动重渲染
const trendOption = computed(() => buildTrendOption({
  series: dailySeries.value,
  data: trendData.value,
  axes: dailyAxes,
  xLabel,
  smooth: false,
  colors: chartColors.value,
}))
// 图例保护：至少保留一条指标线可见，避免图表空白
const trendEvents = { legendselectchanged: trendLegendSelectChanged }

/* ===== 摘要对比表 ===== */
/**
 * 环比差值计算：warn=true 表示"差评数"等反向指标（上升为红，下降为绿）；
 * 其他正向指标则上升绿下降红；今日/昨日同为空值时无环比
 */
function diffText(tVal, yVal, warn) {
  if (tVal == null || yVal == null || (yVal === 0 && tVal === 0)) return ''
  let delta = null
  let pct = null
  if (typeof tVal === 'number' && typeof yVal === 'number') {
    delta = tVal - yVal
    pct = yVal === 0 ? null : (delta / Math.abs(yVal)) * 100
  }
  if (delta === null) return ''
  const arrow = delta > 0 ? '▲' : delta < 0 ? '▼' : '·'
  let cls = ''
  if (delta !== 0) {
    if (warn) cls = delta > 0 ? 'diff-down' : 'diff-up'
    else cls = delta > 0 ? 'diff-up' : 'diff-down'
  }
  const pctStr = pct == null ? '—' : (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%'
  return { html: `${arrow} ${Math.abs(delta).toLocaleString()} (${pctStr})`, cls }
}

const summaryRows = computed(() => {
  const t = today.value
  const y = yesterday.value
  const fields = [
    { key: 'date', label: '日期', tf: v => v || '-', yf: v => v || '-' },
    { key: 'qa_count', label: 'QA 次数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true },
    { key: 'good', label: '好评数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true },
    { key: 'bad', label: '差评数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true, warn: true },
    { key: 'accuracy', label: '准确率', tf: v => fmtPct(v || 0, 2), yf: v => fmtPct(v || 0, 2), cmp: true },
  ]
  return fields.map(f => {
    const tv = t[f.key]
    const yv = y[f.key]
    const d = f.cmp ? diffText(tv, yv, f.warn) : null
    return {
      label: f.label,
      todayText: f.tf(tv),
      yesterdayText: f.yf(yv),
      diff: d ? d.html : '',
      diffCls: d ? d.cls : '',
    }
  })
})

// 根节点筛选变化：重载日报数据（reloadCurrentTab → loadDailyReport）
watch(() => props.rootType, () => loadDailyReport())

onMounted(loadDailyReport)

defineExpose({ reload: loadDailyReport })
</script>

<style scoped>
.panel-body {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 卡片：flex 列布局撑满剩余高度；内容超出时卡片内部滚动，避免页面级滚动条 */
.daily-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  margin-bottom: 0;
  overflow-y: auto;
}

/* 趋势区：占满卡片剩余高度 */
.daily-trend-section {
  flex: 1;
  min-height: 340px;
  display: flex;
  flex-direction: column;
  padding: 16px 16px 0;
  border-bottom: 1px solid var(--app-border);
}

.daily-chart-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  flex-shrink: 0;
}

.trend-head-title {
  font-size: 16px;
  font-weight: 600;
}

/* 图表容器：占满趋势区剩余高度，保留最小尺寸保证图表可读 */
.daily-trend-section .trend-chart-box {
  flex: 1;
  min-width: 320px;
  min-height: 220px;
}

/* 空数据/单点占位文案 */
.chart-placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--app-text-sub);
  font-size: 13px;
}

/* 摘要对比区：保持自然高度，不可压缩，保证表格完整可见 */
.daily-summary-section {
  flex-shrink: 0;
  padding: 6px 16px 14px;
}

.summary-title {
  font-size: 16px;
  font-weight: 600;
  margin: 10px 0 10px;
}

.summary-table {
  width: 100%;
}

/* 环比差值：绿=正向，红=反向（差评数等反向指标取反） */
.diff-up {
  color: #059669;
}

.diff-down {
  color: #dc2626;
}
</style>
