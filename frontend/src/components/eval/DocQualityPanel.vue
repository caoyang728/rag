<template>
  <div class="doc-panel-page">
    <!-- 工具栏：类型筛选 + 批量评估 -->
    <div class="eval-toolbar mb-3">
      <div class="doc-toolbar">
        <div class="flex gap-2 items-center">
          <el-select v-model="org.deptId" placeholder="全部部门" clearable style="width: 160px" @change="onDeptChange">
            <el-option v-for="d in org.departments" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="org.teamId" placeholder="全部团队" clearable style="width: 160px" :disabled="!org.deptId" @change="loadDocQuality">
            <el-option v-for="t in org.teamsOfDept" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
          <el-button type="primary" :loading="running" @click="runBatchDocEval">🔍 批量评估最近文档</el-button>
        </div>
        <span class="text-sub text-sm">{{ summaryText }}</span>
      </div>
    </div>

    <!-- KPI 卡片 -->
    <div class="kpi-grid mb-3">
      <div class="kpi-card"><div class="kpi-label">文档总数</div><div class="kpi-value">{{ summary.totalDocs }}</div></div>
      <div class="kpi-card"><div class="kpi-label">平均质量分</div><div class="kpi-value">{{ summary.avgScore }}</div></div>
      <div class="kpi-card kpi-highlight"><div class="kpi-label">优秀 (≥85)</div><div class="kpi-value">{{ dist.excellent }}</div></div>
      <div class="kpi-card kpi-good"><div class="kpi-label">良好 (≥70)</div><div class="kpi-value">{{ dist.good }}</div></div>
      <div class="kpi-card kpi-fair"><div class="kpi-label">及格 (≥50)</div><div class="kpi-value">{{ dist.fair }}</div></div>
      <div class="kpi-card kpi-poor"><div class="kpi-label">待改进 (&lt;50)</div><div class="kpi-value kpi-red">{{ dist.poor }}</div></div>
    </div>

    <!-- 文档质量分布 -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">文档质量分布</PanelHeader>
      <div class="eval-panel-body">
        <BarChart v-if="distData.length" :data="distData" :width="400" :height="140" :pad-left="20" :pad-bottom="24" :pad-top="20" :start-x="20" max-mode="sum" :value-text="distValueText" />
        <div v-else class="eval-empty"><div class="eval-empty-icon">📄</div><div>暂无数据，点击"批量评估"</div></div>
      </div>
    </div>

    <!-- 常见质量问题 -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">常见质量问题</PanelHeader>
      <div class="eval-panel-body">
        <div v-if="!commonIssues.length" class="eval-empty"><div class="eval-empty-icon">✨</div><div>暂无常见问题</div></div>
        <div v-for="(i, idx) in commonIssues" :key="idx" class="issue-item">
          <span class="issue-icon" :class="severityClass(i.severity)">{{ (i.type || '?')[0] }}</span>
          <div class="issue-content">{{ i.type || '未知问题' }}</div>
          <span class="issue-count">{{ i.count || 0 }} 次</span>
        </div>
      </div>
    </div>

    <!-- 文档质量报告 -->
    <div class="eval-panel doc-report-panel">
      <PanelHeader titleClass="eval-panel-title">文档质量报告</PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="rows" v-loading="loading" size="small">
          <el-table-column label="ID" width="60" prop="id" />
          <el-table-column label="文档" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.document_name || '' }}</template>
          </el-table-column>
          <el-table-column label="质量分" width="90" align="right">
            <template #default="{ row }"><el-tag :type="qualityTagType(row.quality_score)" size="small">{{ row.quality_score }}</el-tag></template>
          </el-table-column>
          <el-table-column label="解析状态" width="100">
            <template #default="{ row }"><span class="text-sub">{{ row.parse_status }}</span></template>
          </el-table-column>
          <el-table-column label="提取率" width="90" align="right">
            <template #default="{ row }"><span class="text-sub">{{ fmtPct(row.text_extraction_rate) }}</span></template>
          </el-table-column>
          <el-table-column label="切片数" width="80" prop="chunk_count" align="right" />
          <el-table-column label="平均大小" width="90" prop="avg_chunk_chars" align="right" />
          <el-table-column label="向量化率" width="90" align="right">
            <template #default="{ row }"><span class="text-sub">{{ fmtPct(row.embedding_success_rate) }}</span></template>
          </el-table-column>
          <el-table-column label="问题" min-width="140">
            <template #default="{ row }">
              <template v-if="(row.quality_issues || []).length">
                <el-tag v-for="(q, i) in row.quality_issues" :key="i" size="small" effect="plain" style="margin-right: 4px">{{ q.type }}</el-tag>
              </template>
              <span v-else class="text-sub">-</span>
            </template>
          </el-table-column>
          <el-table-column label="评估时间" width="150">
            <template #default="{ row }"><span class="text-sub">{{ row.evaluated_at ? formatDate(row.evaluated_at) : '-' }}</span></template>
          </el-table-column>
          <template #empty><el-empty description="暂无文档质量报告，点击「批量评估」" :image-size="70" /></template>
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
import BarChart from './BarChart.vue'
import PanelHeader from '../base/PanelHeader.vue'
import { useListLoader } from '../../composables/useListLoader'
import { useOrgFilter } from './useOrgFilter'
import { fmtPct, qualityTagType } from './constants'

/**
 * 文档质量 Tab（原 doc 面板）：文档质量汇总（KPI/分布/常见问题）+ 文档质量报告列表 + 批量评估
 */
const org = useOrgFilter()
const running = ref(false)

const summary = reactive({ totalDocs: 0, avgScore: 0 })
const dist = reactive({ excellent: 0, good: 0, fair: 0, poor: 0 })
const commonIssues = ref([])
const rows = ref([])

const summaryText = computed(() => `范围：${org.scopeText} · 共 ${summary.totalDocs} 个文档，平均质量分 ${summary.avgScore}`)

const distData = computed(() => [
  { label: '优秀', value: dist.excellent, color: '#10b981' },
  { label: '良好', value: dist.good, color: '#3b82f6' },
  { label: '及格', value: dist.fair, color: '#f59e0b' },
  { label: '待改进', value: dist.poor, color: '#ef4444' },
])
// 比例模式下柱顶显示"数量 (占比%)"
const distTotal = computed(() => distData.value.reduce((s, d) => s + d.value, 0))
const distValueText = v => `${v} (${distTotal.value > 0 ? (v / distTotal.value * 100).toFixed(0) : 0}%)`

function onDeptChange() {
  org.onDeptChange()
  loadDocQuality()
}

// severity 白名单,防止注入非法 CSS 类
function severityClass(s) {
  return { high: 'sev-high', mid: 'sev-mid', low: 'sev-low' }[s] || 'sev-low'
}

const { loading, load: loadDocQuality } = useListLoader(async () => {
  const params = new URLSearchParams()
  if (org.deptId.value) params.set('dept_id', org.deptId.value)
  if (org.teamId.value) params.set('team_id', org.teamId.value)
  const qs = params.toString() ? '?' + params.toString() : ''
  // 并行请求两个接口，减少等待时间
  const [data, sum] = await Promise.all([
    api.getJson(`/api/v1/analytics/doc-quality/reports/${qs}`),
    api.getJson(`/api/v1/analytics/doc-quality/${qs}`),
  ])
  summary.totalDocs = sum.total_docs || 0
  summary.avgScore = sum.avg_score || 0
  const d = sum.score_distribution || {}
  dist.excellent = d.excellent || 0
  dist.good = d.good || 0
  dist.fair = d.fair || 0
  dist.poor = d.poor || 0
  commonIssues.value = sum.common_issues || []
  rows.value = data.rows || []
}, { errorPrefix: '加载失败' })

async function runBatchDocEval() {
  ElMessage.info('正在评估最近文档质量...')
  running.value = true
  try {
    const result = await api.postJson('/api/v1/analytics/doc-quality/evaluate/', { days: 7 })
    const s = result.summary || {}
    ElMessage.success(`评估完成: ${s.evaluated} 个文档，平均分 ${s.avg_quality_score}`)
    loadDocQuality()
  } catch (e) {
    ElMessage.error('评估失败: ' + errMsg(e, '未知错误'))
  } finally {
    running.value = false
  }
}

onMounted(loadDocQuality)

defineExpose({ reload: loadDocQuality })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,内容超高时在容器内部上下滑动 */
.doc-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
}

.doc-panel-page .eval-toolbar,
.doc-panel-page .kpi-grid,
.doc-panel-page .eval-panel:not(.doc-report-panel) {
  flex-shrink: 0;
}

/* 报告面板占满剩余空间,最低高度保证表格可读,超出部分由容器滚动 */
.doc-report-panel {
  flex: 1;
  min-height: 200px;
  max-height: 400px;
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-3 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 表格占满面板剩余空间,行在表格内部滚动 */
.doc-report-panel .eval-panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.doc-report-panel .eval-panel-body .el-table {
  flex: 1;
  min-height: 0;
}

.doc-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.text-sub {
  color: var(--el-text-color-secondary, #6b7280);
}
</style>
