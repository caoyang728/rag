<template>
  <div class="coverage-panel-page">
    <!-- 工具栏：时间窗口 + 刷新/生成 -->
    <div class="eval-toolbar mb-3">
      <div class="coverage-toolbar">
        <div class="flex gap-2 items-center">
          <el-select v-model="days" style="width: 130px" @change="loadCoverage">
            <el-option v-for="opt in options" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <el-button type="primary" :loading="loading" @click="loadCoverage">🔄 刷新</el-button>
          <el-button :loading="generating" @click="generateCoverage">📊 生成报告</el-button>
        </div>
      </div>
    </div>

    <!-- KPI 卡片 -->
    <div class="kpi-grid mb-3">
      <div class="kpi-card"><div class="kpi-label">热门问题覆盖率</div><div class="kpi-value" :class="kpiClass(cov.rate)">{{ fmtPct(cov.rate) }}</div></div>
      <div class="kpi-card"><div class="kpi-label">热门查询总数</div><div class="kpi-value">{{ cov.total }}</div></div>
      <div class="kpi-card"><div class="kpi-label">已覆盖查询</div><div class="kpi-value">{{ cov.covered }}</div></div>
      <div class="kpi-card"><div class="kpi-label">未覆盖查询</div><div class="kpi-value kpi-red">{{ cov.uncovered }}</div></div>
      <div class="kpi-card"><div class="kpi-label">知识空白数</div><div class="kpi-value kpi-red">{{ gapCount }}</div></div>
      <div class="kpi-card"><div class="kpi-label">重复切片率</div><div class="kpi-value" :class="kpiClass(dupRate)">{{ fmtPct(dupRate) }}</div></div>
    </div>

    <!-- 知识空白查询 -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">知识空白查询</PanelHeader>
      <div class="eval-panel-body">
        <div v-if="!gaps.length" class="eval-empty"><div class="eval-empty-icon">✅</div><div>暂无知识空白</div></div>
        <div v-for="(g, i) in gaps" :key="i" class="gap-card">
          <div class="gap-question">{{ g.query }}</div>
          <div class="gap-meta">
            <span>📊 出现 {{ Number(g.count) || 0 }} 次</span>
            <span>💡 {{ g.suggestion || '' }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 部门/团队知识覆盖 -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">部门/团队知识覆盖</PanelHeader>
      <div class="eval-panel-body">
        <div v-if="!domainList.length" class="eval-empty"><div class="eval-empty-icon">📊</div><div>暂无数据，请先上传文档到对应部门/团队</div></div>
        <div v-for="(d, i) in domainList" :key="i" class="coverage-dept-group">
          <!-- 后端返回的部分字段为中文 key（如「占比」），统一数值化兜底，避免 undefined/NaN 显示 -->
          <div class="coverage-row coverage-row-dept">
            <div class="coverage-name"><strong>🏢 {{ d.name }}</strong></div>
            <div class="coverage-bar">
              <div class="coverage-bar-fill" :style="{ width: deptPct(d) + '%' }" :data-value="deptPct(d) + '%'"></div>
            </div>
            <div class="coverage-rate">{{ num(d.doc_count) }} 文档 · <span :class="hitColor(d.query_hit_rate)">命中率 {{ (num(d.query_hit_rate) * 100).toFixed(1) }}%</span></div>
          </div>
          <div v-for="[teamName, teamData] in d.teams || []" :key="teamName" class="coverage-row coverage-row-team">
            <div class="coverage-name">└ {{ teamName }}</div>
            <div class="coverage-bar">
              <div class="coverage-bar-fill" :style="{ width: teamPct(d, teamData) + '%' }" :data-value="teamPct(d, teamData) + '%'"></div>
            </div>
            <div class="coverage-rate">{{ num(teamData && teamData.doc_count) }} 文档 · {{ num(teamData && teamData.chunk_count) }} 切片</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 历史报告 -->
    <div class="eval-panel coverage-report-panel">
      <PanelHeader titleClass="eval-panel-title">
        历史报告
        <template #actions>
          <el-button size="small" @click="loadCoverageReports">🔄 刷新</el-button>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="reports" v-loading="reportLoading" size="small">
          <el-table-column label="ID" width="60" prop="id" />
          <el-table-column label="报告日期" width="110" prop="report_date" />
          <el-table-column label="覆盖率" width="100" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.hot_query_coverage_rate)" size="small">{{ fmtPct(row.hot_query_coverage_rate) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="查询数" width="90" align="right">
            <template #default="{ row }">{{ row.total_hot_queries || 0 }}</template>
          </el-table-column>
          <el-table-column label="知识空白" width="90" align="right">
            <template #default="{ row }">{{ row.gap_count || 0 }}</template>
          </el-table-column>
          <el-table-column label="重复率" width="90" align="right">
            <template #default="{ row }"><span class="text-sub">{{ fmtPct(row.duplicate_chunk_rate) }}</span></template>
          </el-table-column>
          <el-table-column label="生成时间" min-width="150">
            <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="downloadCoverageReport(row.id)">📥 下载</el-button>
              <el-button link type="danger" size="small" @click="confirmDeleteCoverageReport(row.id)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无历史报告，点击「生成报告」创建" :image-size="70" /></template>
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import { downloadBlob } from '../../utils/download'
import PanelHeader from '../base/PanelHeader.vue'
import { useConfirm } from '../../composables/useConfirm'
import { useListLoader } from '../../composables/useListLoader'
import { TIME_RANGE_OPTIONS, useTimeRange } from '../../composables/useTimeRange'
import { fmtPct, kpiClass, scoreTagType } from './constants'

/**
 * 覆盖率 Tab（原 coverage 面板）：热门问题覆盖 + 知识空白 + 部门/团队覆盖 + 历史报告
 * 报告支持 Excel 导出下载（blob + Content-Disposition 文件名解析）
 */
// 覆盖面板只展示 7/30 两档时间窗口，取通用三档的前两档避免文案重复定义
const { days, options } = useTimeRange(TIME_RANGE_OPTIONS.slice(0, 2))
const { confirm } = useConfirm()
const generating = ref(false)

const cov = reactive({ rate: null, total: 0, covered: 0, uncovered: 0 })
const gapCount = ref(0)
const dupRate = ref(0)
const gaps = ref([])
const domainList = ref([])

// 数值化兜底（后端某些字段可能缺失或为字符串）
const num = v => Number(v) || 0

const { loading, load: loadCoverage } = useListLoader(async () => {
  const data = await api.getJson(`/api/v1/analytics/coverage/?days=${days.value}`)
  const c = data.coverage || {}
  const dup = data.duplicates || {}
  cov.rate = c.hot_query_coverage_rate
  cov.total = c.total_hot_queries || 0
  cov.covered = c.covered_queries || 0
  cov.uncovered = c.uncovered_queries || 0
  gapCount.value = data.gap_count || 0
  dupRate.value = dup.duplicate_rate || 0
  gaps.value = data.gaps || []
  const domain = data.domain || {}
  domainList.value = domain.domain_coverage || []

  // 加载历史报告列表
  loadCoverageReports()
}, { errorPrefix: '加载失败' })

// 部门占比（0~1 → 百分比宽度）
function deptPct(d) {
  return (num(d['占比']) * 100).toFixed(1)
}

// 团队占比 = 团队文档数 / 部门文档数（部门文档数为 0 时按 0 处理,避免除零）
function teamPct(d, teamData) {
  const docCount = num(d.doc_count)
  if (docCount <= 0) return '0.0'
  return (num(teamData && teamData.doc_count) / docCount * 100).toFixed(1)
}

function hitColor(hitRate) {
  const rate = num(hitRate)
  if (rate >= 0.8) return 'kpi-green'
  if (rate >= 0.6) return 'kpi-orange'
  return 'kpi-red'
}

async function generateCoverage() {
  ElMessage.info('正在生成覆盖率报告...')
  generating.value = true
  try {
    const result = await api.postJson('/api/v1/analytics/coverage/generate/', { days: parseInt(days.value) })
    ElMessage.success(`报告已生成 (ID: ${result.report_id})`)
    // loadCoverage 内部已调用 loadCoverageReports(),无需重复请求
    loadCoverage()
  } catch (e) {
    ElMessage.error('生成失败: ' + errMsg(e, '未知错误'))
  } finally {
    generating.value = false
  }
}

/* ===== 历史报告 ===== */
const reports = ref([])
const { loading: reportLoading, load: loadCoverageReports } = useListLoader(async () => {
  const data = await api.getJson('/api/v1/analytics/coverage/reports/')
  reports.value = data.rows || []
}, { errorPrefix: '加载报告列表失败' })

/** 下载覆盖率报告为 Excel:拿到 Response 后转 blob 触发浏览器下载 */
async function downloadCoverageReport(id) {
  try {
    const resp = await api.get(`/api/v1/analytics/coverage/reports/${id}/export/`)
    if (!resp.ok) throw new Error(`下载失败: ${resp.status}`)
    // 从响应头提取文件名，兼容 RFC 5987 (filename*=UTF-8''xxx) 与传统 filename="xxx"
    // 优先匹配 filename* 编码格式（支持中文等非 ASCII 字符），回退到普通 filename
    const disp = resp.headers.get('Content-Disposition') || ''
    let filename = ''
    const starMatch = disp.match(/filename\*=([^;]+)/i)
    if (starMatch) {
      // 格式: UTF-8''xxx%20yyy
      const raw = starMatch[1].trim().replace(/^UTF-8''/i, '')
      try {
        filename = decodeURIComponent(raw)
      } catch (e) {
        filename = raw
      }
    } else {
      const match = disp.match(/filename="?([^";]+)"?/i)
      if (match) filename = match[1]
    }
    if (!filename) filename = `coverage_report_${id}.xlsx`
    const blob = await resp.blob()
    downloadBlob(blob, filename)
  } catch (e) {
    ElMessage.error('下载失败: ' + errMsg(e, '未知错误'))
  }
}

async function confirmDeleteCoverageReport(id) {
  await confirm(
    { message: '删除后不可恢复', title: '删除报告', confirmText: '确认删除', errorText: '删除失败' },
    async () => {
      await api.delete(`/api/v1/analytics/coverage/reports/${id}/`)
      ElMessage.success('删除成功')
      loadCoverageReports()
    },
  )
}

function reload() {
  loadCoverage()
  loadCoverageReports()
}

onMounted(reload)

defineExpose({ reload })
</script>

<style scoped>
/* 内容超高时在面板容器内部滑动,避免历史报告面板超出父级 */
.coverage-panel-page {
  height: 100%;
  overflow-y: auto;
}

.coverage-toolbar {
  display: flex;
  align-items: center;
}

/* 历史报告是最后一个面板,补底部间距与同级 mb-3 对齐 */
.coverage-report-panel {
  margin-bottom: 16px;
}

.text-sub {
  color: var(--el-text-color-secondary, #6b7280);
}
</style>
