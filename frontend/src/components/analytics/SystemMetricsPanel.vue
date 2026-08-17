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
            <!-- 延迟直方图：100ms 等宽桶（后端 build_latency_histogram 生成），div 柱状图，桶多时柱自动变细 -->
            <div class="sub-panel">
              <div class="sub-panel-title">⚡ 延迟分布（ms）</div>
              <div v-if="histKeys.length === 0" class="empty">暂无分布数据</div>
              <div v-else class="hist-chart">
                <!-- 柱区：柱高按最大桶归一，柱从底部向上生长 -->
                <div class="hist-bars">
                  <div v-for="(k, i) in histKeys" :key="k" class="hist-col"
                    :style="{ height: histBarHeight(hist[k]) + '%', background: histColor }"
                    :title="`${k} ms：${Number(hist[k]).toLocaleString()} 条（${histPct(hist[k])}%）`"></div>
                </div>
                <!-- 分类标签：与柱一一对应，标明每个柱子的延迟范围；桶多时自动省略号，hover 看完整范围 -->
                <div class="hist-labels">
                  <div v-for="(k, i) in histKeys" :key="'l' + k" class="hist-label" :title="k + ' ms'">{{ k }}</div>
                </div>
              </div>
            </div>
            <!-- 错误分布：红色系进度条，按次数降序 -->
            <div class="sub-panel">
              <div class="sub-panel-title">❌ 错误分布</div>
              <div v-if="errKeys.length === 0" class="empty">暂无错误数据 🎉</div>
              <div v-else>
                <div v-for="k in errKeys" :key="k" class="hist-row">
                  <span class="hist-label-err">{{ k || 'unknown' }}</span>
                  <div class="hist-track-err">
                    <div class="hist-bar-err" :style="{ width: errPct(errDist[k]) + '%' }"></div>
                  </div>
                  <span class="hist-value">{{ errDist[k] }} ({{ errPct(errDist[k]) }}%)</span>
                </div>
              </div>
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
import { errMsg, fmtPct } from '../../utils/format'
import { useListLoader } from '../../composables/useListLoader'

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

/* ===== 延迟直方图（div 柱状图）与错误分布 ===== */
const hist = computed(() => data.value.latency_histogram || {})
const histTotal = computed(() => histKeys.value.reduce((s, k) => s + (hist.value[k] || 0), 0))
// 桶按键值（毫秒）升序排列
const histKeys = computed(() => Object.keys(hist.value).sort((a, b) => parseInt(a, 10) - parseInt(b, 10)))
const histColor = '#2563eb'
// 柱高按最大桶归一为百分比；桶多时柱自动变细（flex 均分宽度）
function histBarHeight(v) {
  const max = Math.max(1, ...histKeys.value.map(k => hist.value[k] || 0))
  return Math.max(1, (v || 0) / max * 100)
}
function histPct(v) {
  return histTotal.value ? ((v || 0) / histTotal.value * 100).toFixed(1) : '0.0'
}

const errDist = computed(() => data.value.error_distribution || {})
// 错误分布按次数降序排列
const errKeys = computed(() => Object.keys(errDist.value).sort((a, b) => (errDist.value[b] || 0) - (errDist.value[a] || 0)))
const errTotal = computed(() => errKeys.value.reduce((s, k) => s + (errDist.value[k] || 0), 0) || 1)
function errPct(v) {
  return (v / errTotal.value * 100).toFixed(1)
}

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

/* 延迟直方图：纵向布局，上为柱区（flex 柱状图，柱从底部向上生长），下为分类标签行；
   柱区随面板剩余高度伸缩，保留最小高度避免直方图过矮 */
.hist-chart {
  flex: 1;
  min-height: 200px;
  display: flex;
  flex-direction: column;
}

.hist-bars {
  flex: 1;
  display: flex;
  align-items: flex-end;
  gap: 1px;
  min-height: 0;
  padding-top: 8px;
}

.hist-col {
  flex: 1;
  min-width: 1px;
  border-radius: 2px 2px 0 0;
  cursor: pointer;
  transition: filter .15s;
}

.hist-col:hover {
  filter: brightness(0.9);
}

/* 分类标签行：与柱一一对应，桶多时溢出省略，避免标签相互挤压重叠 */
.hist-labels {
  display: flex;
  gap: 1px;
  padding-top: 4px;
}

.hist-label {
  flex: 1;
  min-width: 0;
  font-size: 10px;
  line-height: 14px;
  color: var(--app-text-sub);
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 错误分布行：[标签 | 进度条 | 数值] */
.hist-row {
  margin: 6px 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.hist-label-err {
  width: 160px;
  text-align: right;
  color: #dc2626;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hist-track-err {
  flex: 1;
  height: 20px;
  border-radius: 4px;
  overflow: hidden;
  max-width: 480px;
  background: var(--el-color-error-light-9, #fef2f2);
}

.hist-bar-err {
  height: 100%;
  background: #dc2626;
  transition: width .3s ease;
}

.hist-value {
  width: 90px;
  text-align: right;
  font-size: 12px;
  color: var(--app-text);
  white-space: nowrap;
}
</style>
