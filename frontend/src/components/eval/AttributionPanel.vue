<template>
  <div class="attr-panel-page">
    <!-- 工具栏：筛选器(左) + 手动触发(右) -->
    <div class="eval-toolbar mb-3">
      <div class="attr-toolbar">
        <div class="filters">
          <el-select v-model="days" style="width: 120px" @change="loadAttribution">
            <el-option v-for="opt in options" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <el-select v-model="category" placeholder="全部归因" clearable style="width: 140px" @change="loadAttribution">
            <el-option v-for="(label, key) in ATTR_CATEGORY_LABEL" :key="key" :label="label" :value="key" />
          </el-select>
          <el-select v-model="layer" placeholder="全部层级" clearable style="width: 120px" @change="loadAttribution">
            <el-option v-for="(label, key) in ATTR_LAYER_LABEL" :key="key" :label="label" :value="key" />
          </el-select>
          <el-select v-model="status" placeholder="全部状态" clearable style="width: 110px" @change="loadAttribution">
            <el-option label="已完成" value="completed" />
            <el-option label="待分析" value="pending" />
            <el-option label="失败" value="failed" />
          </el-select>
          <el-select v-model="org.deptId" placeholder="全部部门" clearable style="width: 160px" @change="onDeptChange">
            <el-option v-for="d in org.departments" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="org.teamId" placeholder="全部团队" clearable style="width: 160px" :disabled="!org.deptId" @change="loadAttribution">
            <el-option v-for="t in org.teamsOfDept" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
          <el-button @click="loadAttribution">🔄 刷新</el-button>
          <span class="text-sub text-sm" style="white-space: nowrap; margin-left: 8px">{{ summaryText }}</span>
        </div>
        <div class="manual-attr">
          <el-input v-model="manualQaId" placeholder="QA ID" type="number" style="width: 100px" />
          <el-button type="primary" :loading="manualRunning" @click="runManualAttribution">🧪 手动归因</el-button>
        </div>
      </div>
    </div>

    <!-- KPI 卡片:归因分类统计 -->
    <div class="kpi-grid mb-3">
      <div class="kpi-card"><div class="kpi-label">归因总数</div><div class="kpi-value">{{ stats.total }}</div></div>
      <div class="kpi-card kpi-red"><div class="kpi-label">检索层</div><div class="kpi-value">{{ stats.retrieval }}</div></div>
      <div class="kpi-card kpi-highlight"><div class="kpi-label">内容层</div><div class="kpi-value">{{ stats.content }}</div></div>
      <div class="kpi-card kpi-poor"><div class="kpi-label">生成层</div><div class="kpi-value">{{ stats.generation }}</div></div>
      <div class="kpi-card"><div class="kpi-label">规则归因</div><div class="kpi-value">{{ stats.rule }}</div></div>
      <div class="kpi-card kpi-good"><div class="kpi-label">LLM/混合</div><div class="kpi-value">{{ stats.llm }}</div></div>
    </div>

    <!-- 归因分类分布 -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">
        归因分类分布
        <template #actions>
          <div class="text-sub text-sm">按根因分类聚合,展示占比与平均分</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <div v-if="!categoryDist.length" class="text-sub">暂无归因数据</div>
        <div v-for="c in categoryDist" :key="c.category" class="attr-bar-row">
          <div class="attr-bar-label">{{ ATTR_CATEGORY_LABEL[c.category] || c.category }}</div>
          <div class="attr-bar-track">
            <!-- 条形宽度按占比相对最值缩放,渐变色突出 -->
            <div class="attr-bar-fill" :style="{ width: barWidth(c.count) + '%' }"></div>
          </div>
          <div class="attr-bar-count">{{ c.count }}</div>
          <div class="attr-bar-avg">均分 {{ (c.avg_score * 100).toFixed(1) }}%</div>
        </div>
      </div>
    </div>

    <!-- 低分归因列表 -->
    <div class="eval-panel eval-panel-scroll">
      <PanelHeader titleClass="eval-panel-title">
        低分归因列表
        <template #actions>
          <div class="text-sub text-sm">均分低于阈值的对话自动归因 + 优化建议(点击行查看详情)</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="rows" v-loading="loading" size="small">
          <el-table-column label="QA ID" width="80" prop="qa_record_id" />
          <el-table-column label="问题" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.question || '' }}</template>
          </el-table-column>
          <el-table-column label="回答摘要" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.answer || '' }}</template>
          </el-table-column>
          <el-table-column label="均分" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.avg_score)" size="small">{{ fmtPct(row.avg_score) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="归因分类" width="110">
            <template #default="{ row }"><el-tag size="small" effect="plain">{{ catLabel(row) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="影响层级" width="90">
            <template #default="{ row }"><span class="text-sub">{{ layerLabel(row) }}</span></template>
          </el-table-column>
          <el-table-column label="方法" width="90">
            <template #default="{ row }">
              <el-tag :type="row.analysis_method === 'rule' ? 'info' : 'primary'" size="small" effect="plain">{{ methodLabel(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="领域" width="100">
            <template #default="{ row }"><span class="text-sub">{{ row.root_type || '-' }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="statusTag(row.status).type" size="small" effect="plain">{{ statusTag(row.status).text }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="150">
            <template #default="{ row }"><span class="text-sub text-sm">{{ formatDate(row.created_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="120">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="showAttrDetail(row.qa_record_id)">详情</el-button>
              <el-button link type="success" size="small" @click="rerunAttr(row.qa_record_id)">重跑</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无归因数据(低分 QA 评估完成后会自动归因)" :image-size="70" /></template>
        </el-table>
      </div>
    </div>

    <!-- Dialog: 低分归因详情 -->
    <el-dialog :title="'低分归因详情 · QA #' + detailQaId" v-model="detailVisible" width="640px" top="6vh" :close-on-click-modal="false">
      <div v-if="detailLoading" class="text-loading">加载中...</div>
      <div v-else-if="detailError" class="detail-error">{{ detailError }}</div>
      <template v-else-if="detail">
        <!-- 对话内容 -->
        <div class="mb-3">
          <div class="detail-head mb-2">
            <strong>对话内容</strong>
            <span class="text-sm text-sub">
              均分 <el-tag :type="scoreTagType(detail.avg_score)" size="small">{{ fmtPct(detail.avg_score) }}</el-tag>
              · 阈值 {{ fmtPct(detail.threshold) }} · 领域 {{ detail.root_type || '-' }}
            </span>
          </div>
          <div class="mb-2"><strong class="text-sub">问题:</strong> {{ detail.full_question || detail.question || '' }}</div>
          <div><strong class="text-sub">回答:</strong> {{ detail.full_answer || detail.answer || '' }}</div>
        </div>

        <!-- 归因结论 -->
        <div class="attr-section mb-3">
          <div class="attr-section-title">归因结论</div>
          <div class="attr-conclusion">
            <div class="attr-meta-row">
              <span class="attr-meta-label">根因分类:</span>
              <el-tag type="danger" size="small">{{ catLabel(detail) }}</el-tag>
              <span class="attr-meta-label">影响层级:</span>
              <el-tag size="small" effect="plain">{{ layerLabel(detail) }}</el-tag>
              <span class="attr-meta-label">方法:</span>
              <el-tag size="small" effect="plain">{{ methodLabel(detail) }}</el-tag>
              <span class="attr-meta-label">状态:</span>
              <el-tag size="small" effect="plain">{{ statusTag(detail.status).text }}</el-tag>
            </div>
            <div class="attr-detail-text">{{ detail.root_cause_detail || '(无详细说明)' }}</div>
            <div v-if="detail.diagnosis" class="attr-diagnosis">💡 {{ detail.diagnosis }}</div>
            <div v-if="detail.error_message" class="attr-error">⚠️ {{ detail.error_message }}</div>
          </div>
        </div>

        <!-- 低分维度明细 -->
        <div v-if="detail.low_dimensions && detail.low_dimensions.length" class="attr-section mb-3">
          <div class="attr-section-title">低分维度明细(均分 &lt; 阈值)</div>
          <div v-for="(d, i) in detail.low_dimensions" :key="i" class="attr-dim-row">
            <div class="detail-head">
              <span>{{ DIM_LABEL[d.dimension] || d.dimension }}</span>
              <span><el-tag :type="scoreTagType(d.score)" size="small">{{ fmtPct(d.score) }}</el-tag></span>
            </div>
            <div v-if="d.reason" class="text-sm text-sub score-reason">{{ d.reason }}</div>
          </div>
        </div>

        <!-- 优化建议 -->
        <div v-if="detail.suggestions && detail.suggestions.length" class="attr-section">
          <div class="attr-section-title">优化建议</div>
          <div v-for="(sg, i) in detail.suggestions" :key="i" class="attr-suggestion-row">
            <el-tag :type="sg.type === 'short_term' ? 'warning' : 'primary'" size="small" effect="plain">{{ suggestionTypeLabel(sg.type) }}</el-tag>
            <span class="attr-suggestion-text">{{ sg.action || '' }}</span>
          </div>
        </div>

        <!-- LLM 调用元信息(仅 hybrid/llm 方法展示,体现成本可追溯) -->
        <div v-if="detail.analysis_method !== 'rule' && (detail.analysis_tokens_used || detail.analysis_latency_ms)" class="text-sm text-sub mt-3">
          LLM: {{ detail.analysis_model || '-' }} · Token {{ detail.analysis_tokens_used || 0 }} ·
          耗时 {{ detail.analysis_latency_ms || 0 }}ms · {{ formatDate(detail.created_at) }}
        </div>
      </template>
      <template #footer>
        <el-button type="primary" @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import PanelHeader from '../base/PanelHeader.vue'
import { useListLoader } from '../../composables/useListLoader'
import { useTimeRange } from '../../composables/useTimeRange'
import { useOrgFilter } from './useOrgFilter'
import { ATTR_CATEGORY_LABEL, ATTR_LAYER_LABEL, DIM_LABEL, fmtPct, scoreTagType } from './constants'

/**
 * 低分归因 Tab（原 attribution 面板）：低分对话自动归因 + 优化建议
 * - 列表与统计并行请求,筛选/刷新带请求序号守卫
 * - 手动/重跑归因统一走 dispatchAttribution(异步派发,3 秒后自动刷新列表)
 */
const org = useOrgFilter()
const { days, options } = useTimeRange()
const category = ref('')
const layer = ref('')
const status = ref('')

const rows = ref([])
const stats = reactive({ total: 0, retrieval: 0, content: 0, generation: 0, rule: 0, llm: 0 })
const categoryDist = ref([])
const listCount = ref(0)
const listDays = ref(7)

const summaryText = computed(() => `共 ${listCount.value} 条(最近 ${listDays.value} 天) · 范围 ${org.scopeText}`)

function onDeptChange() {
  org.onDeptChange()
  loadAttribution()
}

const { loading, load: loadAttribution } = useListLoader(async () => {
  // 列表查询参数
  const listParams = new URLSearchParams()
  listParams.set('days', days.value)
  listParams.set('limit', '100')
  if (category.value) listParams.set('category', category.value)
  if (layer.value) listParams.set('layer', layer.value)
  if (status.value) listParams.set('status', status.value)
  if (org.deptId.value) listParams.set('dept_id', org.deptId.value)
  if (org.teamId.value) listParams.set('team_id', org.teamId.value)

  // 统计查询参数(不需要 category/layer/status 过滤,stats 接口返回全分类聚合)
  const statsParams = new URLSearchParams()
  statsParams.set('days', days.value)
  if (org.deptId.value) statsParams.set('dept_id', org.deptId.value)
  if (org.teamId.value) statsParams.set('team_id', org.teamId.value)

  const [listData, statsData] = await Promise.all([
    api.getJson(`/api/v1/analytics/low-score-analysis/?${listParams.toString()}`),
    api.getJson(`/api/v1/analytics/low-score-analysis/stats/?${statsParams.toString()}`),
  ])
  rows.value = listData.rows || []
  listCount.value = (listData.rows || []).length
  listDays.value = listData.days || days.value
  renderAttrStats(statsData)
}, {
  // 失败时清空列表展示空状态并提示；onError 存在时不会走 useListLoader 的默认提示
  onError: (e, { silent }) => {
    if (silent) return
    rows.value = []
    ElMessage.error('加载归因数据失败: ' + errMsg(e, e))
  },
})

function renderAttrStats(data) {
  const byLayer = data.by_layer || []
  const byMethod = data.by_method || { rule: 0, llm: 0, hybrid: 0 }
  const layerCount = l => (byLayer.find(x => x.layer === l)?.count) || 0
  stats.total = data.total || 0
  stats.retrieval = layerCount('retrieval')
  stats.content = layerCount('content')
  stats.generation = layerCount('generation')
  stats.rule = byMethod.rule || 0
  stats.llm = (byMethod.llm || 0) + (byMethod.hybrid || 0)
  categoryDist.value = data.by_category || []
}

// 横向条形宽度:按 count 相对最大值的占比缩放
function barWidth(count) {
  const maxCount = Math.max(...categoryDist.value.map(c => c.count), 1)
  return (count / maxCount * 100).toFixed(1)
}

/* ===== 展示辅助 ===== */
// 分类/层级/方法标签优先取后端返回的 label,缺失时回退到本地映射
function catLabel(r) {
  return r.category_label || ATTR_CATEGORY_LABEL[r.root_cause_category] || r.root_cause_category
}
function layerLabel(r) {
  return r.layer_label || ATTR_LAYER_LABEL[r.affected_layer] || r.affected_layer
}
function methodLabel(r) {
  return r.method_label || r.analysis_method
}

// 状态 → el-tag type（completed=成功, failed=危险, 其余待分析）
function statusTag(st) {
  if (st === 'completed') return { type: 'success', text: '已完成' }
  if (st === 'failed') return { type: 'danger', text: '失败' }
  return { type: 'warning', text: '待分析' }
}

// 建议类型:短期/长期（未知类型原样返回）
function suggestionTypeLabel(t) {
  return t === 'short_term' ? '短期' : (t === 'long_term' ? '长期' : t)
}

/* ===== 归因派发 ===== */
const manualQaId = ref('')
const manualRunning = ref(false)

// 派发单条 QA 归因的公共逻辑(异步,提交后立即返回,前端不做轮询)
// 归因任务通常 2~10s 完成(规则归因秒级,LLM 归因取决于模型响应),
// 成功提示后 3 秒自动刷新列表
async function dispatchAttribution(qaId) {
  try {
    const resp = await api.postJson('/api/v1/analytics/low-score-analysis/run/', {
      qa_record_id: parseInt(qaId),
    })
    if (!resp || !resp.queued) {
      throw new Error(resp?.detail || '派发归因失败')
    }
    ElMessage.success('归因已派发,3 秒后自动刷新')
    // 3 秒后自动刷新(规则归因通常已完成,LLM 归因可能仍在跑)
    setTimeout(() => loadAttribution(), 3000)
  } catch (e) {
    ElMessage.error('归因失败: ' + errMsg(e, e))
  }
}

// 手动输入 QA ID 派发归因(带输入校验与按钮禁用提示)
async function runManualAttribution() {
  const qaId = (manualQaId.value || '').trim()
  if (!qaId) { ElMessage.error('请输入 QA 记录 ID'); return }
  ElMessage.info('归因已派发,规则归因秒级完成,LLM 归因约 10~30s,请稍后刷新查看')
  manualRunning.value = true
  await dispatchAttribution(qaId)
  manualRunning.value = false
}

// 列表"重跑"按钮:复用公共派发逻辑
async function rerunAttr(qaId) {
  ElMessage.info('正在重新归因...')
  await dispatchAttribution(qaId)
}

/* ===== 归因详情弹窗 ===== */
// 请求序号守卫:连续查看不同归因时,旧响应后返回不覆盖新弹窗内容
let attrDetailSeq = 0
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detailQaId = ref('')
const detail = ref(null)

async function showAttrDetail(qaId) {
  const mySeq = ++attrDetailSeq
  detailQaId.value = qaId
  detailVisible.value = true
  detailLoading.value = true
  detailError.value = ''
  detail.value = null
  try {
    const data = await api.getJson(`/api/v1/analytics/low-score-analysis/detail/?qa_record_id=${qaId}`)
    // 旧响应后返回时丢弃
    if (mySeq !== attrDetailSeq) return
    detail.value = data
  } catch (e) {
    if (mySeq !== attrDetailSeq) return
    detailError.value = '加载失败: ' + errMsg(e, String(e))
  } finally {
    if (mySeq === attrDetailSeq) detailLoading.value = false
  }
}

onMounted(loadAttribution)

defineExpose({ reload: loadAttribution })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,固定区(工具栏/KPI/分布)在上,
   低分归因列表占满剩余空间并在内部滚动 */
.attr-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.attr-panel-page .eval-toolbar,
.attr-panel-page .kpi-grid,
.attr-panel-page .eval-panel:not(.eval-panel-scroll) {
  flex-shrink: 0;
}

/* 显式标记滚动目标：el-dialog 会在面板内渲染 el-overlay 兄弟节点,
   不能依赖 :last-of-type 判定,否则 flex 撑满规则会被绕过 */
.attr-panel-page .eval-panel-scroll {
  flex: 1;
  min-height: 0;
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-3 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 滚动下移到表格内部：标题固定,表格占满剩余空间,行在表内滚动 */
.attr-panel-page .eval-panel-scroll .eval-panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.attr-panel-page .eval-panel-scroll .eval-panel-body .el-table {
  flex: 1;
  min-height: 0;
}

.attr-toolbar {
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

.manual-attr {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.mt-3 { margin-top: 12px; }

.text-sub { color: var(--el-text-color-secondary, #6b7280); }

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

.detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.score-reason {
  margin-top: 4px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
