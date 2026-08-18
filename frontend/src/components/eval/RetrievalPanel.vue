<template>
  <div class="retrieval-panel-page">
    <!-- 工具栏：选择测试集 + 执行评估 -->
    <div class="eval-toolbar mb-3">
      <div class="retrieval-toolbar">
        <div class="flex gap-2 items-center">
          <el-select v-model="datasetId" placeholder="选择测试集" clearable style="width: 200px" @change="onDatasetChange">
            <el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-button type="primary" :loading="running" @click="runRetrievalEval">🚀 执行检索评估</el-button>
          <span class="text-sm text-sub" style="white-space: nowrap">建议部署前执行，用于量化版本对比</span>
        </div>
      </div>
    </div>

    <!-- KPI 卡片（有报告才展示） -->
    <div v-if="hasReport" class="kpi-grid mb-3">
      <div class="kpi-card"><div class="kpi-label">Recall@5</div><div class="kpi-value" :class="kpiClass(kpi.recallAt5)">{{ fmtPct(kpi.recallAt5) }}</div></div>
      <div class="kpi-card"><div class="kpi-label">Recall@10</div><div class="kpi-value" :class="kpiClass(kpi.recallAt10)">{{ fmtPct(kpi.recallAt10) }}</div></div>
      <div class="kpi-card"><div class="kpi-label">Recall@20</div><div class="kpi-value" :class="kpiClass(kpi.recallAt20)">{{ fmtPct(kpi.recallAt20) }}</div></div>
      <div class="kpi-card"><div class="kpi-label">MRR</div><div class="kpi-value" :class="kpiClass(kpi.mrr)">{{ fmtPct(kpi.mrr) }}</div></div>
      <div class="kpi-card"><div class="kpi-label">NDCG@10</div><div class="kpi-value" :class="kpiClass(kpi.ndcg)">{{ fmtPct(kpi.ndcg) }}</div></div>
      <div class="kpi-card"><div class="kpi-label">问题命中率</div><div class="kpi-value" :class="kpiClass(kpi.hitRate)">{{ fmtPct(kpi.hitRate) }}</div></div>
    </div>

    <!-- 各阶段增益分析 -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">各阶段增益分析</PanelHeader>
      <div class="eval-panel-body">
        <div v-if="gainData.length" class="bar-chart-box">
          <VChart :option="barOption" />
        </div>
        <div v-else class="eval-empty"><div class="eval-empty-icon">🔍</div><div>暂无评估报告，选择测试集后点击"执行检索评估"</div></div>
        <div class="text-sm text-sub mt-2">向量 vs BM25 vs 混合(RRF) vs Rerank 各阶段 Recall@10 对比</div>
      </div>
    </div>

    <!-- 历史评估报告 -->
    <div class="eval-panel eval-panel-scroll">
      <PanelHeader titleClass="eval-panel-title">历史评估报告</PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="reports" v-loading="loading" size="small">
          <el-table-column label="ID" width="70" prop="id" />
          <el-table-column label="测试集" width="90" prop="dataset_id" />
          <el-table-column label="R@5" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.recall_at_5)" size="small">{{ fmtPct(row.recall_at_5) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="R@10" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.recall_at_10)" size="small">{{ fmtPct(row.recall_at_10) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="R@20" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.recall_at_20)" size="small">{{ fmtPct(row.recall_at_20) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="MRR" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.mrr)" size="small">{{ fmtPct(row.mrr) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="NDCG@10" width="100" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.ndcg_at_10)" size="small">{{ fmtPct(row.ndcg_at_10) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="时间" min-width="150">
            <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
          </el-table-column>
          <template #empty><el-empty description="暂无评估报告，选择测试集后点击「执行检索评估」" :image-size="70" /></template>
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import PanelHeader from '../base/PanelHeader.vue'
import { useTheme } from '../../composables/useTheme'
import { buildBarOption, chartThemeColors } from '../../utils/chart'
import VChart from '../base/VChart.vue'
import { useListLoader } from '../../composables/useListLoader'
import { fmtPct, kpiClass, scoreTagType } from './constants'

/**
 * 检索评估 Tab（原 retrieval 面板）：选择测试集执行离线检索评估 + 查看历史报告
 * 离线评估用于部署前量化版本对比（Recall@5/10/20、MRR、NDCG@10、问题命中率）
 */
const datasets = ref([])
const datasetId = ref('') // 测试集下拉联动:按选中测试集过滤历史报告(不选则展示全部)
const reports = ref([])
const running = ref(false)

// 最新报告 KPI（数值化兜底,避免 questions_with_hits/total_questions 为 undefined 时得到 NaN）
const kpi = reactive({ recallAt5: null, recallAt10: null, recallAt20: null, mrr: null, ndcg: null, hitRate: null })

const hasReport = computed(() => kpi.recallAt10 !== null)
const gainData = computed(() => {
  if (!hasReport.value) return []
  const latest = reports.value[0]
  if (!latest) return []
  return [
    { label: '向量', value: Number(latest.vector_recall_at_10) || 0, color: '#3b82f6' },
    { label: 'BM25', value: Number(latest.bm25_recall_at_10) || 0, color: '#8b5cf6' },
    { label: '混合', value: Number(latest.hybrid_recall_at_10) || 0, color: '#06b6d4' },
    { label: 'Rerank', value: Number(latest.rerank_recall_at_10) || 0, color: '#10b981' },
  ]
})

const { isDark } = useTheme()
// 图表主题色：依赖 isDark，主题切换时重建（canvas 图表需取计算后的 CSS 变量色值）
const chartColors = computed(() => chartThemeColors(isDark.value))
// 增益柱状图 option：数值刻度模式（Y 轴百分比网格），柱顶展示 fmtPct
const barOption = computed(() => buildBarOption({
  data: gainData.value,
  valueText: fmtPct,
  maxMode: 'value',
  colors: chartColors.value,
}))

async function loadDatasetsOptions() {
  try {
    const data = await api.getJson('/api/v1/analytics/golden-datasets/')
    datasets.value = data.rows || []
  } catch (e) {
    ElMessage.error('加载测试集失败: ' + errMsg(e, '未知错误'))
  }
}

const { loading, load: loadRetrievalReports } = useListLoader(async () => {
  const url = datasetId.value
    ? '/api/v1/analytics/eval/retrieval-reports/?dataset_id=' + encodeURIComponent(datasetId.value)
    : '/api/v1/analytics/eval/retrieval-reports/'
  const data = await api.getJson(url)
  const rows = data.rows || []
  reports.value = rows

  // 展示最新报告的 KPI（带语义着色）
  const latest = rows[0]
  if (latest) {
    const totalQ = Number(latest.total_questions) || 0
    const hitQ = Number(latest.questions_with_hits) || 0
    kpi.recallAt5 = latest.recall_at_5
    kpi.recallAt10 = latest.recall_at_10
    kpi.recallAt20 = latest.recall_at_20
    kpi.mrr = latest.mrr
    kpi.ndcg = latest.ndcg_at_10
    kpi.hitRate = totalQ > 0 ? (hitQ / totalQ) : 0
  } else {
    kpi.recallAt5 = kpi.recallAt10 = kpi.recallAt20 = kpi.mrr = kpi.ndcg = kpi.hitRate = null
  }
}, { errorPrefix: '加载失败' })

function onDatasetChange() {
  loadRetrievalReports()
}

async function runRetrievalEval() {
  if (!datasetId.value) { ElMessage.error('请选择测试集'); return }
  ElMessage.info('正在执行评估，可能需要几分钟...')
  running.value = true
  try {
    const result = await api.postJson('/api/v1/analytics/eval/retrieval/', { dataset_id: parseInt(datasetId.value) })
    ElMessage.success(`评估完成: Recall@10=${fmtPct(result.recall_at_10)}`)
    loadRetrievalReports()
  } catch (e) {
    ElMessage.error('评估失败: ' + errMsg(e, '未知错误'))
  } finally {
    running.value = false
  }
}

function reload() {
  loadDatasetsOptions()
  loadRetrievalReports()
}

onMounted(reload)

defineExpose({ reload })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,内容超高时在容器内部上下滑动 */
.retrieval-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
}

.retrieval-panel-page .eval-toolbar,
.retrieval-panel-page .kpi-grid,
.retrieval-panel-page .eval-panel:not(.eval-panel-scroll) {
  flex-shrink: 0;
}

/* 显式标记滚动目标：el-dialog 会在面板内渲染 el-overlay 兄弟节点,
   不能依赖 :last-of-type 判定 */
.retrieval-panel-page .eval-panel-scroll {
  flex: 1;
  min-height: 200px; /* 最低高度:容器空间不足时保证表格可读,超出部分由容器滚动 */
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-3 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 滚动下移到表格内部：标题固定,表格占满剩余空间,行在表内滚动 */
.retrieval-panel-page .eval-panel-scroll .eval-panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.retrieval-panel-page .eval-panel-scroll .eval-panel-body .el-table {
  flex: 1;
  min-height: 0;
}

.retrieval-toolbar {
  display: flex;
  align-items: center;
}

/* 增益柱状图容器：固定高度（ECharts 需显式容器尺寸） */
.bar-chart-box {
  height: 220px;
  width: 100%;
}

.text-sub {
  color: var(--el-text-color-secondary, #6b7280);
}
</style>
