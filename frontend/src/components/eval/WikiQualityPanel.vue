<template>
  <div class="wiki-panel-page">
    <!-- 工具栏：筛选 + 批量评估 -->
    <div class="eval-toolbar mb-3">
      <div class="wiki-toolbar">
        <div class="flex gap-2 items-center">
          <el-select v-model="days" style="width: 120px" @change="loadWikiQuality">
            <el-option v-for="opt in options" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <el-select v-model="dim" style="width: 130px" @change="loadWikiQuality">
            <el-option label="全部维度" value="" />
            <el-option label="忠实度" value="faithfulness" />
            <el-option label="完整性" value="completeness" />
          </el-select>
          <el-select v-model="status" style="width: 120px" @change="loadWikiQuality">
            <el-option label="全部状态" value="" />
            <el-option label="已完成" value="completed" />
            <el-option label="失败" value="failed" />
          </el-select>
          <el-button type="primary" :loading="running" @click="runWikiQualityEval">🔍 批量评估</el-button>
        </div>
        <span class="text-sub text-sm">{{ summaryText }}</span>
      </div>
    </div>

    <!-- KPI 卡片 -->
    <div class="kpi-grid mb-3">
      <div class="kpi-card"><div class="kpi-label">评估页面数</div><div class="kpi-value">{{ s.pagesEvaluated }}</div></div>
      <div class="kpi-card kpi-good"><div class="kpi-label">平均忠实度</div><div class="kpi-value">{{ s.avgFaithfulness }}</div></div>
      <div class="kpi-card kpi-highlight"><div class="kpi-label">平均完整性</div><div class="kpi-value">{{ s.avgCompleteness }}</div></div>
      <div class="kpi-card kpi-red"><div class="kpi-label">失败页面</div><div class="kpi-value kpi-red">{{ s.failedPages }}</div></div>
    </div>

    <!-- 页面质量明细 -->
    <div class="eval-panel eval-panel-scroll">
      <PanelHeader titleClass="eval-panel-title">
        Wiki 页面质量明细
        <template #actions>
          <div class="text-sub text-sm">忠实度 = 内容忠于源文档(无幻觉)；完整性 = 覆盖源文档关键要点；点击"查看理由"看评估依据</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="rows" v-loading="loading" size="small">
          <el-table-column label="页面 ID" width="90" prop="page_id" />
          <el-table-column label="标题" min-width="220" prop="title" show-overflow-tooltip />
          <el-table-column label="忠实度" width="110">
            <template #default="{ row }"><ScoreCell :info="row.scores && row.scores.faithfulness" /></template>
          </el-table-column>
          <el-table-column label="完整性" width="110">
            <template #default="{ row }"><ScoreCell :info="row.scores && row.scores.completeness" /></template>
          </el-table-column>
          <el-table-column label="更新时间" width="150">
            <template #default="{ row }"><span class="text-sub">{{ row.updatedAt }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="showWikiDetail(row.page_id, row.title)">查看理由</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无评估数据，点击右上角「批量评估」或等待每日定时评估" :image-size="70" /></template>
        </el-table>
      </div>
    </div>

    <!-- Dialog: Wiki 页面质量详情 -->
    <el-dialog :title="'Wiki 页面质量 · ' + detailTitle" v-model="detailVisible" width="640px" top="6vh" :close-on-click-modal="false">
      <div v-if="detailLoading" class="text-loading">加载中...</div>
      <div v-else-if="detailError" class="detail-error">{{ detailError }}</div>
      <template v-else>
        <div v-for="d in ['faithfulness', 'completeness']" :key="d" class="attr-section">
          <div class="attr-section-title">
            {{ WIKI_DIM_LABEL[d] || d }}
            <template v-if="detailScores[d]">
              ·
              <el-tag v-if="detailScores[d].status === 'failed'" type="danger" size="small">失败</el-tag>
              <el-tag v-else :type="scoreTagType(detailScores[d].score)" size="small">{{ fmtPct(detailScores[d].score) }}</el-tag>
            </template>
          </div>
          <template v-if="detailScores[d]">
            <!-- 失败维度显示红色错误信息,完成维度显示评估理由 -->
            <div v-if="detailScores[d].status === 'failed'" class="wiki-error">{{ detailScores[d].error_message || '评估失败' }}</div>
            <div v-else class="text-sm text-sub">{{ detailScores[d].reason || '无理由' }}</div>
            <div class="text-sm text-sub mt-2">更新时间 {{ formatDate(detailScores[d].updated_at) }}</div>
          </template>
          <div v-else class="text-sub">未评估</div>
        </div>
      </template>
      <template #footer>
        <el-button type="primary" @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, h, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import PanelHeader from '../base/PanelHeader.vue'
import { useListLoader } from '../../composables/useListLoader'
import { useTimeRange } from '../../composables/useTimeRange'
import { fmtPct, scoreTagType, WIKI_DIM_LABEL } from './constants'

/**
 * Wiki 页面质量 Tab（原 wiki 面板）：忠实度/完整性两个维度的页面质量评估
 * - 批量评估异步派发,LLM 评估耗时较长,派发后延迟刷新列表
 * - 详情弹窗按 page_id 精确查询该页两个维度的完整评估记录
 */
const { days, options } = useTimeRange()
const dim = ref('')
const status = ref('')
const running = ref(false)

const rows = ref([])
const s = ref({ pagesEvaluated: 0, avgFaithfulness: '--', avgCompleteness: '--', failedPages: 0 })
const total = ref(0)

const summaryText = computed(() => {
  if (total.value === null || total.value === undefined) return ''
  const dimText = dim.value === '' ? '全部' : (WIKI_DIM_LABEL[dim.value] || dim.value)
  const statusText = status.value === '' ? '全部' : (status.value === 'completed' ? '已完成' : '失败')
  return `窗口 ${days.value} 天 · 维度 ${dimText} · 状态 ${statusText} · 共 ${total.value} 页`
})

const { loading, load: loadWikiQuality } = useListLoader(async () => {
  const params = new URLSearchParams()
  params.set('days', days.value)
  if (dim.value) params.set('dimension', dim.value)
  if (status.value) params.set('status', status.value)
  const qs = '?' + params.toString()

  const data = await api.getJson('/api/v1/analytics/wiki-quality/' + qs)
  total.value = data.total
  const sum = data.summary || {}
  s.value = {
    pagesEvaluated: sum.pages_evaluated || 0,
    avgFaithfulness: sum.avg_faithfulness !== undefined ? fmtPct(sum.avg_faithfulness) : '--',
    avgCompleteness: sum.avg_completeness !== undefined ? fmtPct(sum.avg_completeness) : '--',
    failedPages: sum.failed_pages || 0,
  }
  // 更新时间取两个维度中较新的
  rows.value = (data.rows || []).map(r => {
    const fa = r.scores && r.scores.faithfulness
    const co = r.scores && r.scores.completeness
    const updatedMs = Math.max(
      fa && fa.updated_at ? new Date(fa.updated_at).getTime() : 0,
      co && co.updated_at ? new Date(co.updated_at).getTime() : 0
    )
    return { ...r, updatedAt: updatedMs ? formatDate(new Date(updatedMs).toISOString()) : '--' }
  })
}, { errorPrefix: 'Wiki 质量加载失败' })

/* ===== 详情弹窗 ===== */
// 请求序号守卫:连续查看不同页面时,旧响应后返回不覆盖新弹窗内容
let wikiDetailSeq = 0
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detailTitle = ref('')
const detailScores = ref({})

async function showWikiDetail(pageId, title) {
  const mySeq = ++wikiDetailSeq
  detailTitle.value = title
  detailVisible.value = true
  detailLoading.value = true
  detailError.value = ''
  detailScores.value = {}
  try {
    // 详情按 page_id 精确查询该页两个维度的完整评估记录
    const params = new URLSearchParams()
    params.set('days', '90')
    params.set('page_id', pageId)
    const data = await api.getJson('/api/v1/analytics/wiki-quality/?' + params.toString())
    // 旧响应后返回时丢弃
    if (mySeq !== wikiDetailSeq) return
    const row = (data.rows || [])[0]
    if (!row) {
      detailError.value = '未找到该页面的评估记录'
      return
    }
    detailScores.value = row.scores || {}
  } catch (e) {
    if (mySeq !== wikiDetailSeq) return
    detailError.value = '加载失败: ' + errMsg(e, String(e))
  } finally {
    if (mySeq === wikiDetailSeq) detailLoading.value = false
  }
}

/** 手动触发 Wiki 页面批量评估(异步,提交后提示等待,前端轮询列表) */
async function runWikiQualityEval() {
  ElMessage.info('评估已派发,正在后台批量评估页面质量,请稍后刷新...')
  running.value = true
  try {
    await api.postJson('/api/v1/analytics/wiki-quality/evaluate/', {})
    ElMessage.success('评估已派发')
    // LLM 评估耗时较长,间隔轮询刷新列表直到出现新数据
    setTimeout(() => loadWikiQuality(), 5000)
  } catch (e) {
    ElMessage.error('评估派发失败: ' + errMsg(e, '未知错误'))
  } finally {
    running.value = false
  }
}

/* ===== 维度得分单元格 ===== */
// 失败维度显示红色"失败"标签;完成维度显示分数;未评估显示 --
const ScoreCell = {
  props: { info: { type: Object, default: null } },
  setup(props) {
    return () => {
      if (!props.info) return h('span', { class: 'text-sub' }, '--')
      if (props.info.status === 'failed') return h('el-tag', { type: 'danger', size: 'small' }, '失败')
      return h('el-tag', { type: scoreTagType(props.info.score), size: 'small' }, fmtPct(props.info.score))
    }
  }
}

onMounted(loadWikiQuality)

defineExpose({ reload: loadWikiQuality })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,固定区(工具栏/KPI)在上,
   Wiki 页面质量明细占满剩余空间并在内部滚动 */
.wiki-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.wiki-panel-page .eval-toolbar,
.wiki-panel-page .kpi-grid,
.wiki-panel-page .eval-panel:not(.eval-panel-scroll) {
  flex-shrink: 0;
}

/* 显式标记滚动目标：el-dialog 会在面板内渲染 el-overlay 兄弟节点,
   不能依赖 :last-of-type 判定 */
.wiki-panel-page .eval-panel-scroll {
  flex: 1;
  min-height: 0;
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-3 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 滚动下移到面板 body：标题固定,明细表格占满剩余空间并在内部滚动 */
.wiki-panel-page .eval-panel-scroll .eval-panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.wiki-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.text-sub {
  color: var(--el-text-color-secondary, #6b7280);
}

.text-loading {
  padding: 20px;
  color: var(--app-text-sub);
  text-align: center;
}

.detail-error {
  padding: 20px;
  color: #f56c6c;
  text-align: center;
}

.wiki-error {
  font-size: 13px;
  color: #dc2626;
}
</style>
