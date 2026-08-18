<template>
  <div class="panel-body">
    <!-- 概览卡片：今日实时 + 时间范围工具栏 + 趋势图合并为一张卡，内部用分隔线间隔 -->
    <div class="app-card overview-card">
      <!-- 今日实时：Redis 秒级计数，固定展示今日累计；卡片窄化排成一行，超出宽度横向滑动 -->
      <div class="overview-realtime">
        <div class="realtime-head">
          <div class="realtime-title">🚀 今日实时</div>
          <el-tag v-if="freshnessTag" :type="freshnessTag.type" size="small" effect="light">{{ freshnessTag.text }}</el-tag>
        </div>
        <div class="realtime-strip">
          <div v-for="c in realtimeCards" :key="c.key" class="kpi-card">
            <div class="kpi-label">{{ c.label }}</div>
            <div class="kpi-value" :style="{ color: c.color }">{{ c.text }}</div>
            <div class="kpi-compare" :class="c.compareCls" :title="c.compareTitle">{{ c.compareText }}</div>
          </div>
        </div>
      </div>
      <!-- 趋势图工具栏：标题 + 时间范围按钮组 + 导出报表 -->
      <div class="overview-toolbar">
        <div class="trend-title">📈 指标趋势（{{ trendTitleLabel }}）</div>
        <div class="toolbar-right">
          <el-radio-group v-model="timeRange" size="small" @change="onTimeRangeChange">
            <el-radio-button value="7d">近7天</el-radio-button>
            <el-radio-button value="30d">近30天</el-radio-button>
            <el-radio-button value="90d">近90天</el-radio-button>
            <el-radio-button value="custom">自定义</el-radio-button>
          </el-radio-group>
          <el-button size="small" @click="exportReport">📥 导出报表</el-button>
        </div>
      </div>
      <!-- 趋势折线图：指标分轴（计数/满意率%/耗时 ms），撑满卡片剩余高度，图例多选/自适应由 ECharts 处理 -->
      <div class="trend-section">
        <div v-if="trendData.length > 1" class="trend-chart-box">
          <VChart :option="trendOption" :events="trendEvents" />
        </div>
        <!-- 空数据 / 仅 1 天数据：不绘制图表，直接展示文案（与原版占位一致） -->
        <div v-else class="chart-placeholder">{{ trendData.length === 0 ? '暂无数据' : '仅 1 天数据，暂无法绘制趋势图' }}</div>
      </div>
    </div>

    <!-- 自定义日期范围弹窗：开始/结束日期横向双列 -->
    <el-dialog v-model="customDateVisible" title="自定义日期范围" width="480px" :close-on-click-modal="false">
      <div class="date-range-row">
        <div class="date-item">
          <div class="date-label">开始日期</div>
          <el-date-picker v-model="customStart" type="date" value-format="YYYY-MM-DD" :clearable="false" :disabled-date="d => d > todayDate" style="width: 100%" />
        </div>
        <div class="date-item">
          <div class="date-label">结束日期</div>
          <el-date-picker v-model="customEnd" type="date" value-format="YYYY-MM-DD" :clearable="false" :disabled-date="d => d > todayDate" style="width: 100%" />
        </div>
      </div>
      <template #footer>
        <el-button @click="customDateVisible = false">取消</el-button>
        <el-button type="primary" @click="applyCustomDate">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { errMsg } from '../../utils/format'
import { exportCsv } from '../../utils/download'
import { useListLoader } from '../../composables/useListLoader'
import { useTheme } from '../../composables/useTheme'
import { buildTrendOption, chartThemeColors, trendLegendSelectChanged } from '../../utils/chart'
import VChart from '../base/VChart.vue'

/**
 * 概览 Tab：今日实时（Redis 快照 + 5 分钟轮询）+ 指标趋势折线图 + CSV 导出
 * 根节点筛选变化时由父组件调用 reload()；切回概览时调用 activate()（立即刷新并恢复轮询）
 */
const props = defineProps({
  rootType: { type: String, default: '' }, // 当前根节点筛选（root_type）
})

/* ===== 今日实时 ===== */
const REALTIME_POLL_INTERVAL = 5 * 60 * 1000 // 5 分钟，与后端 flush_realtime_metrics 周期对齐
let rtTimer = null
let activated = false // 当前是否处于概览 Tab（回前台时据此判断是否刷新）
const freshnessTag = ref(null)
const realtimeCards = ref([])

// 实时指标加载：轮询与手动刷新可能并发，useListLoader 内部请求序号保证只采用最新响应；
// 轮询走 { silent: true }，失败时静默保留上一次已渲染的数据，避免卡片闪烁或清空
const { load: loadRealtimeStrip } = useListLoader(async () => {
  const data = await api.getJson('/api/v1/analytics/realtime/')

  // 数据新鲜度徽标：实时指标每 5 分钟由 flush_realtime_metrics 更新时间戳，10 分钟内视为新鲜
  const freshness = data.last_flush_at ? Math.floor(Date.now() / 1000 - data.last_flush_at) : null
  const isFresh = freshness != null && freshness < 600
  if (freshness == null) freshnessTag.value = { type: 'warning', text: '尚未同步' }
  else if (isFresh) freshnessTag.value = { type: 'success', text: `数据新鲜（${freshness}s 前同步）` }
  else freshnessTag.value = { type: 'danger', text: `数据陈旧（${freshness}s 未同步）` }

  // 今日实时卡片：cur 为原始数值用于同比计算，positive 表示"上涨是否符合业务预期"
  // （缓存命中/正常请求上涨为佳；Token/费用/LLM 错误上涨为劣，用于对比行着色）
  const cards = [
    { label: '今日 QA 总数', key: 'total_qa', cur: data.total_qa || 0, color: 'var(--app-text)', positive: true, fmt: v => v.toLocaleString() },
    { label: '缓存命中', key: 'cache_hits', cur: data.cache_hits || 0, color: '#059669', positive: true, fmt: v => v.toLocaleString() },
    { label: '正常请求', key: 'normal_qa', cur: data.normal_qa || 0, color: '#2563eb', positive: true, fmt: v => v.toLocaleString() },
    { label: 'LLM 错误', key: 'llm_errors', cur: data.llm_errors || 0, color: data.llm_errors > 0 ? '#dc2626' : '#059669', positive: false, fmt: v => v.toLocaleString() },
    { label: '今日 Prompt Token', key: 'tokens_prompt', cur: data.tokens_prompt || 0, color: '#7c3aed', positive: false, fmt: v => v.toLocaleString() },
    { label: '今日 Completion Token', key: 'tokens_completion', cur: data.tokens_completion || 0, color: '#7c3aed', positive: false, fmt: v => v.toLocaleString() },
    { label: '今日预估费用', key: 'cost_estimate', cur: data.cost_estimate || 0, color: '#dc2626', positive: false, fmt: v => '¥ ' + v.toFixed(4) },
  ]
  realtimeCards.value = cards.map(c => {
    const compare = buildRealtimeCompare(c, data.yesterday || {})
    return {
      ...c,
      text: c.fmt(c.cur),
      compareText: compare.text,
      compareCls: compare.cls,
      compareTitle: compare.title,
    }
  })
}, {
  // 手动刷新失败时清空卡片并提示；轮询失败静默保留旧数据
  onError: (e, { silent }) => {
    if (silent) return
    realtimeCards.value = []
    ElMessage.error('加载实时指标失败: ' + errMsg(e, '未知错误'))
  },
})

/**
 * 生成实时卡片同比对比行（今日 vs 昨日同时段）
 *  - 无昨日数据时显示"暂无对比"；持平显示"持平"；否则按涨跌方向 + 差值 + 百分比展示，
 *    颜色按指标业务预期着色（positive=true 表示上涨符合预期 → 涨绿跌红，反之相反）
 */
function buildRealtimeCompare(c, yesterday) {
  const yVal = (yesterday && typeof yesterday[c.key] === 'number') ? yesterday[c.key] : null
  if (yVal == null) return { text: '暂无对比', cls: 'text-sub', title: '' }
  const diff = c.cur - yVal
  if (Math.abs(diff) < 1e-9) return { text: '持平', cls: '', title: '对比昨日同时段' }
  const up = diff > 0
  const good = up === c.positive
  const absDiff = Math.abs(diff)
  // Token/费用为小数时保留 4 位，计数类整数直接千分位
  const diffStr = Number.isInteger(absDiff) ? absDiff.toLocaleString() : absDiff.toFixed(4)
  const pct = yVal > 0 ? ` (${((absDiff / yVal) * 100).toFixed(1)}%)` : ''
  return { text: `${up ? '▲' : '▼'} ${diffStr}${pct}`, cls: good ? 'up' : 'down', title: '对比昨日同时段' }
}

// 轮询仅概览 Tab 激活时运行；切走/页面隐藏时暂停，回到概览/页面时立即刷新并恢复
function startPolling() {
  stopPolling()
  rtTimer = setInterval(() => loadRealtimeStrip(true), REALTIME_POLL_INTERVAL)
}
function stopPolling() {
  if (rtTimer) { clearInterval(rtTimer); rtTimer = null }
}
function activate() {
  activated = true
  loadRealtimeStrip({ silent: true })
  startPolling()
}
function deactivate() {
  activated = false
  stopPolling()
}

// 页面切后台暂停轮询，回前台且处于概览 Tab 时立即刷新并恢复
function onVisibilityChange() {
  if (document.hidden) {
    stopPolling()
  } else if (activated) {
    loadRealtimeStrip(true)
    startPolling()
  }
}

/* ===== 指标趋势 ===== */
const trendData = ref([])
const timeRange = ref('7d')
let prevRange = '7d' // 上一次非 custom 的时间范围，点"自定义"时用于恢复选中态
const customStart = ref('')
const customEnd = ref('')
const customDateVisible = ref(false)
const todayDate = new Date()

const trendTitleLabel = computed(() => {
  if (timeRange.value === 'custom' && customStart.value && customEnd.value) {
    return `${customStart.value} ~ ${customEnd.value}`
  }
  return { '7d': '近 7 天', '30d': '近 30 天', '90d': '近 90 天', custom: '自定义' }[timeRange.value] || '近 7 天'
})

/**
 * 构造趋势报表接口 URL：自定义范围走 start_date/end_date，否则按当前时间范围换算 days，
 * 统一追加 root_type 过滤（与 exportReport 复用）
 */
function buildTrendUrl(opts = {}) {
  const rootType = opts.rootType !== undefined ? opts.rootType : props.rootType
  let url
  if (!opts.forceDays && timeRange.value === 'custom' && customStart.value && customEnd.value) {
    url = `/api/v1/analytics/trend/?start_date=${customStart.value}&end_date=${customEnd.value}`
  } else {
    const days = opts.days || (timeRange.value === '7d' ? 7 : (timeRange.value === '30d' ? 30 : 90))
    url = `/api/v1/analytics/trend/?days=${days}`
  }
  if (rootType) url += '&root_type=' + encodeURIComponent(rootType)
  return url
}

// 趋势加载：时间范围/根节点快速切换时 useListLoader 内部请求序号保证只采用最新响应
const { load: loadTrend } = useListLoader(async () => {
  const data = await api.getJson(buildTrendUrl())
  trendData.value = data.trend || []
}, {
  // 加载失败时清空趋势，避免展示过期数据
  onError: (e) => {
    trendData.value = []
    ElMessage.error('加载趋势数据失败: ' + errMsg(e, '未知错误'))
  },
})

// 指标分轴：左轴=计数类（问答/缓存/好评/差评/活跃用户），右轴=满意率百分比，
// 耗时类（首字耗时/整体耗时）走独立 time 轴（单位 ms），避免与 % 混轴导致刻度单位错乱；耗时类默认不勾选
const trendSeries = [
  { key: 'qa', label: '总问答数', color: '#2563eb', axis: 'left', get: t => t.qa_count || 0 },
  { key: 'cache', label: '缓存命中', color: '#059669', axis: 'left', get: t => t.cache_hit_count || 0 },
  { key: 'good', label: '好评', color: '#16a34a', axis: 'left', visible: false, get: t => t.good || 0 },
  { key: 'bad', label: '差评', color: '#dc2626', axis: 'left', visible: false, get: t => t.bad || 0 },
  { key: 'active', label: '活跃用户', color: '#0891b2', axis: 'left', visible: false, get: t => t.active_users || 0 },
  { key: 'accuracy', label: '满意率', color: '#7c3aed', axis: 'right', dashed: true, get: t => (t.accuracy || 0) * 100 },
  { key: 'ttft', label: '首字耗时', color: '#a16207', axis: 'time', visible: false, get: t => t.avg_ttft_ms || 0 },
  { key: 'total', label: '整体耗时', color: '#ef4444', axis: 'time', visible: false, get: t => t.avg_total_ms || 0 },
]
const trendAxes = {
  // 左轴：计数类从 0 起算、不设上限，刻度取整数
  left: { toFixed: 0, includeZero: true, clampMin: 0, clampMax: null, minSpan: 1, padMin: 1, padMax: 1 },
  // 右轴：满意率百分比（0-100%），耗时类不再共用该轴
  right: { toFixed: 1, unit: '%', minSpan: 0.1 },
  // 耗时轴：毫秒刻度，从 0 起算、不设上限；pad/minSpan 保证小波动也有刻度跨度
  time: { toFixed: 0, unit: 'ms', includeZero: true, clampMin: 0, clampMax: null, minSpan: 100, padMin: 100, padMax: 100 },
}

const { isDark } = useTheme()
// 图表主题色：依赖 isDark，主题切换时重建（canvas 图表需取计算后的 CSS 变量色值）
const chartColors = computed(() => chartThemeColors(isDark.value))
// 趋势图 option：由 series/axes/data 翻译为 echarts 配置，数据与主题变化时自动重渲染
const trendOption = computed(() => buildTrendOption({
  series: trendSeries,
  data: trendData.value,
  axes: trendAxes,
  smooth: false,
  colors: chartColors.value,
}))
// 图例保护：至少保留一条指标线可见，避免图表空白
const trendEvents = { legendselectchanged: trendLegendSelectChanged }

/* ---- 时间范围切换 ---- */
function onTimeRangeChange(val) {
  // 点"自定义"时打开日期弹窗并恢复之前的时间范围，避免出现空范围选中态
  if (val === 'custom') {
    openCustomDate()
    timeRange.value = prevRange
    return
  }
  prevRange = val
  loadTrend()
}

// 打开自定义日期弹窗：默认预填近 7 天 ~ 今天（历史已有选择则沿用）
function openCustomDate() {
  if (!customStart.value || !customEnd.value) {
    customStart.value = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10)
    customEnd.value = new Date().toISOString().slice(0, 10)
  }
  customDateVisible.value = true
}

function applyCustomDate() {
  const start = customStart.value
  const end = customEnd.value
  if (!start || !end) { ElMessage.error('请选择开始日期和结束日期'); return }
  if (start > end) { ElMessage.error('开始日期不能晚于结束日期'); return }
  customDateVisible.value = false
  timeRange.value = 'custom'
  loadTrend()
  ElMessage.success(`已切换至自定义范围：${start} ~ ${end}`)
}

/* ---- 导出报表（CSV） ---- */
// 防 CSV 公式注入：单元格以 = + - @ \t \r 开头时前置单引号，避免 Excel 打开时被当作公式执行
function csvCell(v) {
  const s = String(v ?? '')
  return /^[=+\-@\t\r]/.test(s) ? "'" + s : s
}

async function exportReport() {
  try {
    const data = await api.getJson(buildTrendUrl())
    // exportCsv 自动加 UTF-8 BOM（EF BB BF），解决 Excel 打开中文乱码
    let csv = '日期,问答数,好评数,差评数,准确率(%),平均耗时(ms)\n'
    // 后端 TrendReportView 返回 avg_total_ms（非缓存命中的整体总耗时），并非 avg_latency_ms；
    // accuracy 可能缺失，先归一为 0 再乘 100，避免 (undefined*100).toFixed 输出 "NaN"
    ;(data.trend || []).forEach(t => {
      csv += [csvCell(t.date), csvCell(t.qa_count), csvCell(t.good), csvCell(t.bad),
        csvCell(((t.accuracy || 0) * 100).toFixed(2)), csvCell(t.avg_total_ms || 0)].join(',') + '\n'
    })
    exportCsv(`报表_${new Date().toISOString().slice(0, 10)}.csv`, csv)
    ElMessage.success('报表已导出')
  } catch (e) {
    ElMessage.error('导出失败: ' + errMsg(e, '未知错误'))
  }
}

/* ===== 根节点筛选联动：只影响趋势图（reloadCurrentTab → loadTrend） ===== */
watch(() => props.rootType, () => loadTrend())

onMounted(() => {
  loadRealtimeStrip()
  loadTrend()
  startPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

// 供父组件控制：activate=切回概览立即刷新+恢复轮询；deactivate=切走停止轮询；reload=根节点变化重载趋势
defineExpose({
  activate,
  deactivate,
  reload() {
    loadTrend()
    loadRealtimeStrip()
  },
})
</script>

<style scoped>
.panel-body {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 概览卡片：flex 列布局撑满剩余高度，内部由实时区/工具栏/趋势图分区，不超出屏幕；
   app-card 全局 margin-bottom 在此多余，置 0 避免底部空隙 */
.overview-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  margin-bottom: 0;
  overflow: hidden;
}

/* 今日实时区块：标题 + 新鲜度徽标 + 横向滚动计数条 */
.overview-realtime {
  flex-shrink: 0;
  padding: 14px 16px 0;
  border-bottom: 1px solid var(--app-border);
}

.realtime-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.realtime-title {
  font-size: 16px;
  font-weight: 600;
}

.realtime-strip {
  display: flex;
  align-items: stretch;
  gap: 10px;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 12px 0 14px;
  scrollbar-width: thin;
}

.kpi-card {
  flex: 0 0 auto;
  min-width: 132px;
  padding: 12px 14px;
  background: var(--app-menu-hover);
  border: 1px solid var(--app-border);
  border-radius: 8px;
}

.kpi-label {
  font-size: 12px;
  color: var(--app-text-sub);
  white-space: nowrap;
}

.kpi-value {
  font-size: 20px;
  font-weight: 600;
  margin-top: 4px;
  color: var(--app-text);
}

/* 实时卡片同比对比行：今日 vs 昨日同时段，绿=符合预期方向，红=反预期 */
.kpi-compare {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.kpi-compare.up { color: #059669; }
.kpi-compare.down { color: #dc2626; }
.kpi-compare.text-sub { color: var(--app-text-sub); }

/* 趋势工具栏：标题左 / 时间范围 + 导出右 */
.overview-toolbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 16px;
  border-bottom: 1px solid var(--app-border);
}

.trend-title {
  font-size: 15px;
  font-weight: 500;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 趋势图区：占满卡片剩余高度 */
.trend-section {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 12px 16px 16px;
}

/* 图表容器：占满剩余空间，保留最小尺寸保证窄/矮容器中图表可读（原 fill 模式语义） */
.trend-chart-box {
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

/* 自定义日期弹窗：横向双列 */
.date-range-row {
  display: flex;
  gap: 20px;
}

.date-item {
  flex: 1;
}

.date-label {
  margin-bottom: 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
}
</style>
