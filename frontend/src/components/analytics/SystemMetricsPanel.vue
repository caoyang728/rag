<template>
  <div class="panel-body">
    <div class="app-card system-card">
      <!-- 卡片头部：标题 + 日期选择 -->
      <div class="system-card-header">
        <div class="card-title-text">📅 历史性能指标报表（每日预计算，凌晨 2 点生成）</div>
        <div class="header-right">
          <span class="text-sub">日期：</span>
          <el-date-picker v-model="reportDate" type="date" value-format="YYYY-MM-DD" :clearable="false"
            :disabled-date="d => d > maxDate" style="width: 160px" @change="loadSystemMetrics" />
        </div>
      </div>

      <!-- 空态：该日期报表尚未生成 -->
      <div v-if="emptyMessage" class="card-empty">
        <div class="empty-emoji">📅</div>
        <div class="empty-title">{{ emptyMessage }}</div>
        <div class="text-sub">报表日期：{{ reportDate || '-' }}（请等待凌晨聚合任务完成或切换到其他日期）</div>
      </div>

      <template v-else>
        <!-- 1. KPI 层：QA 规模 + 比率，横向一排、宽度不足时横向滑动 -->
        <div class="system-section">
          <div class="section-title">📊 关键指标</div>
          <div class="kpi-grid">
            <div v-for="k in kpiCards" :key="k.label" class="kpi-card">
              <div class="kpi-label">{{ k.label }}</div>
              <div class="kpi-value" :style="{ color: k.color }">{{ k.value }}</div>
            </div>
          </div>
        </div>

        <!-- 2. 响应及缓存耗时 / Token 成本：左右并排（耗时卡占 2/3，Token 占 1/3） -->
        <div class="system-section">
          <div class="perf-grid">
            <!-- 左卡：5 个维度列（总延迟/LLM/检索/TTFB/缓存命中），每列上下展示 P50/P95/P99；
                 无数据（null/0）显示 "/"，避免 0 被误读为真实延迟 -->
            <div class="perf-sub-card">
              <div class="sub-card-title">⚡ 响应及缓存耗时（ms）</div>
              <div class="latency-grid">
                <div v-for="col in latencyCols" :key="col.name" class="latency-col">
                  <div class="latency-col-title">{{ col.name }}</div>
                  <div class="latency-row"><span>P50</span><b>{{ msNum(col.p50) }}</b></div>
                  <div class="latency-row"><span>P95</span><b>{{ msNum(col.p95) }}</b></div>
                  <div class="latency-row"><span>P99</span><b>{{ msNum(col.p99) }}</b></div>
                </div>
              </div>
            </div>
            <!-- 右卡：Token 与成本（Prompt / Completion / 费用），费用红色强调 -->
            <div class="perf-sub-card">
              <div class="sub-card-title">🪙 Token 成本</div>
              <div class="token-rows">
                <div class="token-row"><span>Prompt Token</span><b>{{ num(data.total_tokens_prompt) }}</b></div>
                <div class="token-row"><span>Completion Token</span><b>{{ num(data.total_tokens_completion) }}</b></div>
                <div class="token-row"><span>预估费用（¥）</span><b class="cost">¥ {{ (data.total_cost || 0).toFixed(4) }}</b></div>
              </div>
            </div>
          </div>
        </div>

        <!-- 3. 延迟与错误分布：左右两个子面板 -->
        <div class="system-section last">
          <div class="section-title">📊 延迟与错误分布</div>
          <div class="dist-grid">
            <!-- 延迟直方图：后端100ms细粒度桶由 mergeHistogramBuckets 智能合并为 6~12 个可读区间 -->
            <div class="sub-panel">
              <div class="sub-panel-title">⚡ 延迟分布</div>
              <div v-if="histOption" class="chart-box">
                <VChart :option="histOption" />
              </div>
              <div v-else class="empty">暂无分布数据</div>
            </div>
            <!-- 错误分布：水平条形图，按次数降序 -->
            <div class="sub-panel">
              <div class="sub-panel-title">❌ 错误分布</div>
              <div v-if="errOption" class="chart-box">
                <VChart :option="errOption" />
              </div>
              <div v-else class="empty">暂无错误数据 🎉</div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { errMsg } from '../../utils/format'
import { useListLoader } from '../../composables/useListLoader'
import { useTheme } from '../../composables/useTheme'
import { buildHistogramOption, buildErrorDistOption, chartThemeColors } from '../../utils/chart'
import VChart from '../base/VChart.vue'

/**
 * 历史指标 Tab：某日期（默认昨日）的 P50/P95/P99 / 缓存命中率 / 失败率 / Token / 延迟与错误分布
 * 数据由每日凌晨 2 点预计算；每次切换 Tab 由父组件调用 reload() 重新加载
 */
const reportDate = ref('') // 报表日期（YYYY-MM-DD）
const maxDate = new Date(Date.now() - 86400000) // 系统报表通常昨日已就绪，不允许选未来日期
const data = ref({})
const emptyMessage = ref('')

const { load: loadSystemMetrics } = useListLoader(async () => {
  let url = '/api/v1/analytics/system-metrics/'
  if (reportDate.value) url += '?date=' + encodeURIComponent(reportDate.value)
  const res = await api.getJson(url)
  data.value = res
  emptyMessage.value = res.available ? '' : (res.message || '暂无数据')
}, {
  // 失败时设置错误占位文案并提示；onError 存在时不会走 useListLoader 的默认提示
  onError: (e, { silent }) => {
    if (silent) return
    emptyMessage.value = '加载系统指标失败'
    ElMessage.error('加载系统指标失败: ' + errMsg(e, '未知错误'))
  },
})

/* ===== KPI 层 ===== */
const kpiCards = computed(() => {
  const d = data.value
  return [
    { label: '总 QA 数', value: num(d.total_qa), color: 'var(--app-text)' },
    { label: '正常请求数', value: num(d.normal_qa_count), color: '#2563eb' },
    { label: '缓存命中数', value: num(d.cache_hit_count), color: '#059669' },
    { label: '缓存命中率', value: pct(d.cache_hit_rate), color: '#059669' },
    // LLM 成功率低于 90% 标红，超时/Embedding 错误率超过 1% 标红
    { label: 'LLM 成功率', value: pct(d.llm_success_rate), color: (d.llm_success_rate || 0) < 0.9 ? '#dc2626' : '#059669' },
    { label: 'LLM 超时率', value: pct(d.llm_timeout_rate), color: (d.llm_timeout_rate || 0) > 0.01 ? '#dc2626' : '#f59e0b' },
    { label: 'Embedding 错误率', value: pct(d.embedding_error_rate), color: (d.embedding_error_rate || 0) > 0.01 ? '#dc2626' : '#f59e0b' },
    { label: '平均 Token/s', value: num(d.avg_tokens_per_second), color: '#7c3aed' },
  ]
})

/* ===== 延迟分位数 / Token ===== */
// 无数据（null/0）显示 "/"，避免 0 被误读为真实延迟
function msNum(v) {
  return (!v) ? '/' : Number(v).toLocaleString()
}
const latencyCols = computed(() => {
  const d = data.value
  return [
    { name: '总延迟', p50: d.p50_latency_total, p95: d.p95_latency_total, p99: d.p99_latency_total },
    { name: 'LLM', p50: d.p50_latency_llm, p95: d.p95_latency_llm, p99: d.p99_latency_llm },
    { name: '检索', p50: d.p50_latency_retrieval, p95: d.p95_latency_retrieval, p99: d.p99_latency_retrieval },
    { name: 'TTFB', p50: d.p50_ttfb, p95: d.p95_ttfb, p99: d.p99_ttfb },
    { name: '缓存命中', p50: d.cache_hit_p50_latency, p95: d.cache_hit_p95_latency, p99: d.cache_hit_p99_latency },
  ]
})

/* ===== 延迟直方图 & 错误分布（ECharts）===== */
const { isDark } = useTheme()
const chartColors = computed(() => chartThemeColors(isDark.value))
// 延迟直方图 option：后端100ms桶由 mergeHistogramBuckets 智能合并为 6~12 个可读区间
const histOption = computed(() => buildHistogramOption({
  rawHist: data.value.latency_histogram,
  colors: chartColors.value,
}))
// 错误分布 option：水平条形图，按次数降序
const errOption = computed(() => buildErrorDistOption({
  errDist: data.value.error_distribution,
  colors: chartColors.value,
}))

/* ===== 数值格式化 ===== */
function num(v) {
  return (v || 0).toLocaleString()
}
function pct(v) {
  // 0~1 比例 → 百分比：复用 utils/format 的 fmtPct（缺失按 0 处理保持原展示）
  return fmtPct(v || 0, 2)
}

onMounted(() => {
  // 默认日期预填昨日（系统报表通常已就绪）
  reportDate.value = new Date(Date.now() - 86400000).toISOString().slice(0, 10)
  loadSystemMetrics()
})

defineExpose({ reload: loadSystemMetrics })
</script>

<style scoped>
.panel-body {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 卡片：flex 列布局撑满剩余高度；内容超出时卡片内部滚动，避免页面级滚动条 */
.system-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  margin-bottom: 0;
  overflow-y: auto;
}

/* 卡片头部：标题 + 日期选择，与下方内容用分隔线区分 */
.system-card-header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--app-border);
}

.card-title-text {
  font-size: 16px;
  font-weight: 600;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 区块：纵向分隔线间隔 */
.system-section {
  flex-shrink: 0;
  padding: 12px 16px 14px;
  border-bottom: 1px solid var(--app-border);
}

.system-section.last {
  /* 最后一块占满卡片剩余高度，保留最小高度避免图表区过矮 */
  flex: 1;
  min-height: 320px;
  display: flex;
  flex-direction: column;
  border-bottom: none;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 10px;
}

/* 空态 */
.card-empty {
  padding: 40px;
  text-align: center;
}

.empty-emoji {
  font-size: 48px;
  margin-bottom: 16px;
}

.empty-title {
  font-size: 16px;
  font-weight: 500;
  margin-bottom: 8px;
}

/* KPI 卡：横向一排，宽度不足时横向滑动 */
.kpi-grid {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  overflow-y: hidden;
  padding-bottom: 2px;
}

.kpi-card {
  flex: 0 0 auto;
  min-width: 130px;
  padding: 10px 12px;
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

/* 响应耗时（2fr）与 Token（1fr）并排 */
.perf-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 12px;
}

.perf-sub-card {
  background: var(--app-menu-hover);
  padding: 12px 14px;
  border-radius: 6px;
  min-width: 0;
}

.sub-card-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
}

/* 分位数延迟：每维度一列，列内上下展示 P50/P95/P99 */
.latency-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 4px 12px;
}

.latency-col-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-sub);
  margin-bottom: 6px;
  white-space: nowrap;
}

.latency-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 0;
  font-size: 13px;
}

.latency-row + .latency-row {
  border-top: 1px dashed var(--app-border);
}

.latency-row span {
  color: var(--app-text-sub);
  min-width: 26px;
}

.latency-row b {
  font-weight: 600;
  white-space: nowrap;
}

/* Token 与成本：纵向三行，费用红色强调 */
.token-rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.token-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 7px 0;
  font-size: 13px;
}

.token-row + .token-row {
  border-top: 1px dashed var(--app-border);
}

.token-row span {
  color: var(--app-text-sub);
}

.token-row b {
  font-weight: 600;
  white-space: nowrap;
}

.token-row b.cost {
  color: #dc2626;
}

/* 延迟与错误分布子面板：grid 随区块剩余高度拉伸，左右面板等高 */
.dist-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.sub-panel {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--app-border);
  border-radius: 6px;
  background: var(--app-menu-hover);
  padding: 12px 14px;
  min-width: 0;
}

.sub-panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text-sub);
  margin-bottom: 8px;
}

.empty {
  padding: 30px 0;
  text-align: center;
  color: var(--app-text-sub);
  font-size: 13px;
}

/* 图表容器：ECharts 需显式容器尺寸 */
.chart-box {
  flex: 1;
  min-height: 200px;
  width: 100%;
}
</style>
