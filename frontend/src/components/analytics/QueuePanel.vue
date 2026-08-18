<template>
  <div class="panel-body">
    <!-- 实时状态 + 历史趋势合并为一张卡片，内部用分隔线间隔 -->
    <div class="app-card queue-card">
      <!-- 实时快照区：顶部固定 + 与历史趋势区分隔 -->
      <div class="queue-snapshot-section">
        <div class="snapshot-head">
          <span class="snapshot-title">📡 实时队列状态</span>
          <div class="snapshot-right">
            <span class="text-sub">历史窗口：</span>
            <el-select v-model="queueHours" size="small" style="width: 130px" @change="loadQueueDepth">
              <el-option label="最近 1 小时" :value="1" />
              <el-option label="最近 6 小时" :value="6" />
              <el-option label="最近 24 小时" :value="24" />
              <el-option label="最近 3 天" :value="72" />
              <el-option label="最近 7 天" :value="168" />
            </el-select>
          </div>
        </div>
        <el-table :data="currentRows" size="small" class="queue-table">
          <el-table-column label="队列名" min-width="140" prop="name" />
          <el-table-column label="等待任务数" width="110">
            <template #default="{ row }">
              <span :class="row.size > 1000 ? 'cell-danger' : 'cell-success'">{{ row.size.toLocaleString() }}</span>
            </template>
          </el-table-column>
          <el-table-column label="已排队" width="90">
            <template #default="{ row }">{{ row.queued }}</template>
          </el-table-column>
          <el-table-column label="运行中" width="90">
            <template #default="{ row }">{{ row.active }}</template>
          </el-table-column>
          <el-table-column label="空闲 Worker" width="110">
            <template #default="{ row }">{{ row.idle }}</template>
          </el-table-column>
          <el-table-column label="失败" width="90">
            <template #default="{ row }">{{ row.failed }}</template>
          </el-table-column>
          <template #empty><el-empty description="当前无队列数据（Celery Worker 未启动？）" :image-size="50" /></template>
        </el-table>
      </div>
      <!-- 历史趋势区：占满卡片剩余高度 -->
      <div class="queue-history-section">
        <div class="history-title">📈 队列深度历史趋势</div>
        <!-- 队列集合动态变化，series 由队列名动态生成；空/单点展示占位文案。
             图表撑满历史趋势区剩余高度（ECharts 随容器自适应） -->
        <div v-if="chartData.length > 1" class="trend-chart-box">
          <VChart :option="trendOption" :events="trendEvents" />
        </div>
        <div v-else class="chart-placeholder">{{ chartData.length === 0 ? '暂无历史数据（需要等待至少 1 个 5 分钟周期）' : '历史数据不足，至少需要 2 个时间槽' }}</div>
      </div>
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
import { buildTrendOption, chartThemeColors, trendLegendSelectChanged } from '../../utils/chart'
import VChart from '../base/VChart.vue'

/**
 * 队列深度 Tab：实时快照（各 Celery 队列当前状态）+ 指定时间窗口的历史深度趋势折线图
 * 队列名是动态的（Worker 启停会增减队列），series 按每次响应的队列集合重建
 */
const queueHours = ref(24)
const currentRows = ref([])
const history = ref([])

const { load: loadQueueDepth } = useListLoader(async () => {
  const data = await api.getJson(`/api/v1/analytics/queue-depth/?hours=${queueHours.value}`)

  // 1. 当前实时快照：队列大小超过 1000 视为危险，用红/绿色切换
  const cur = data.current || {}
  currentRows.value = Object.keys(cur).map(q => {
    const d = cur[q] || {}
    return {
      name: q,
      size: d.size || d.length || 0,
      queued: d.queued != null ? d.queued : '-',
      active: d.active != null ? d.active : '-',
      idle: d.idle != null ? d.idle : '-',
      failed: d.failed != null ? d.failed : '-',
    }
  })
  // 2. 历史趋势数据
  history.value = data.history || []
}, {
  // 失败时清空数据并提示；onError 存在时不会走 useListLoader 的默认提示
  onError: (e, { silent }) => {
    if (silent) return
    currentRows.value = []
    history.value = []
    ElMessage.error('加载队列深度失败: ' + errMsg(e, '未知错误'))
  },
})

/* ===== 历史趋势：组装 (bucket, queue) → 总深度（queued + active） ===== */
// minute_bucket 为 YYYYMMDDHHmm（本地时间），X 轴标签 slice 成 HH:MM
const xLabel = t => String(t.date).slice(8, 10) + ':' + String(t.date).slice(10, 12)
const buckets = computed(() => [...new Set(history.value.map(h => h.minute_bucket))].sort())
const queueNames = computed(() => [...new Set(history.value.map(h => h.queue_name))].sort())
// 先构造 (bucket, queue) → 深度的 Map 建索引，后续组装数据点直接查 Map，避免双重循环 .find() 的 O(B*Q*H)
const depthMap = computed(() => {
  const map = new Map()
  history.value.forEach(h => {
    map.set(`${h.minute_bucket}||${h.queue_name}`, (h.queued_size || 0) + (h.active_size || 0))
  })
  return map
})
// 每个时间槽一个数据点，字段按队列名取值（TrendChart 默认读 t[key]）
const chartData = computed(() => buckets.value.map(b => {
  const point = { date: b }
  queueNames.value.forEach(q => { point[q] = depthMap.value.get(`${b}||${q}`) || 0 })
  return point
}))
const palette = ['#2563eb', '#059669', '#f59e0b', '#dc2626', '#7c3aed', '#0891b2', '#db2777']
const chartSeries = computed(() => queueNames.value.map((q, i) => ({
  key: q, label: q, color: palette[i % palette.length], axis: 'left',
})))
// 计数轴：从 0 起算、不设上限（队列深度可能远超 100），刻度取整数
const queueAxes = {
  left: { toFixed: 0, includeZero: true, clampMin: 0, clampMax: null, minSpan: 1, padMin: 1, padMax: 1 },
}

const { isDark } = useTheme()
// 图表主题色：依赖 isDark，主题切换时重建（canvas 图表需取计算后的 CSS 变量色值）
const chartColors = computed(() => chartThemeColors(isDark.value))
// 趋势图 option：队列集合/数据变化时自动重渲染
const trendOption = computed(() => buildTrendOption({
  series: chartSeries.value,
  data: chartData.value,
  axes: queueAxes,
  xLabel,
  smooth: true,
  colors: chartColors.value,
}))
// 图例保护：至少保留一条指标线可见，避免图表空白
const trendEvents = { legendselectchanged: trendLegendSelectChanged }

onMounted(loadQueueDepth)

defineExpose({ reload: loadQueueDepth })
</script>

<style scoped>
.panel-body {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 卡片：flex 列布局撑满剩余高度；内容超出时卡片内部滚动，避免页面级滚动条 */
.queue-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  margin-bottom: 0;
  overflow-y: auto;
}

/* 实时快照区 */
.queue-snapshot-section {
  flex-shrink: 0;
  padding: 12px 16px;
  border-bottom: 1px solid var(--app-border);
}

.snapshot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.snapshot-title {
  font-size: 16px;
  font-weight: 600;
}

.snapshot-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.queue-table {
  width: 100%;
}

/* 队列大小超过 1000 视为危险 */
.cell-danger {
  color: #dc2626;
  font-weight: 600;
}

.cell-success {
  color: #059669;
  font-weight: 600;
}

/* 历史趋势区：占满卡片剩余高度，保留最小高度避免图表过矮 */
.queue-history-section {
  flex: 1;
  min-height: 320px;
  display: flex;
  flex-direction: column;
  padding: 12px 16px;
}

.history-title {
  flex-shrink: 0;
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 10px;
}

/* 图表容器：占满历史趋势区剩余高度，保留最小尺寸保证图表可读 */
.queue-history-section .trend-chart-box {
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
</style>
