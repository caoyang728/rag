<template>
  <div class="panel-body">
    <!-- 关键词权重调优 + 差评反馈明细：左右并排（5:4） -->
    <div class="tools-grid">
      <div class="app-card tools-card">
        <div class="card-head">
          <span class="card-head-title">🔧 关键词权重调优</span>
          <el-button type="primary" size="small" @click="openAddKeyword">＋ 新增关键词</el-button>
        </div>
        <el-table :data="keywords" v-loading="kwLoading" size="small" class="tools-table">
          <el-table-column label="关键词" min-width="140">
            <template #default="{ row }"><span class="fw-500">{{ row.keyword }}</span></template>
          </el-table-column>
          <el-table-column label="当前权重" width="100">
            <template #default="{ row }">
              <el-tag :type="kwWeightType(row.weight_score)" size="small" effect="plain">×{{ (row.weight_score || 1).toFixed(1) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="调整历史" min-width="160">
            <template #default="{ row }">
              <span class="text-sub">{{ (row.hit_count || 0) }} 次命中 · {{ (row.good_feedback || 0) }} 好评 · {{ (row.bad_feedback || 0) }} 差评</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button link type="success" size="small" @click="adjustKeywordWeight(row.id, 0.1)">+0.1</el-button>
              <el-button link type="danger" size="small" @click="adjustKeywordWeight(row.id, -0.1)">-0.1</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无关键词数据" :image-size="50" /></template>
        </el-table>
      </div>

      <!-- 差评反馈明细：卡片列表，浅红底 + 左侧红色边条 -->
      <div class="app-card tools-card">
        <div class="card-head">
          <span class="card-head-title">👎 差评反馈明细</span>
        </div>
        <div class="feedback-list">
          <div v-if="badFeedbacks.length === 0 && !fbLoading" class="empty">暂无差评反馈</div>
          <div v-for="f in badFeedbacks" :key="f.id" class="feedback-item">
            <div class="fb-question">Q: {{ f.question || '' }}</div>
            <!-- 答案摘要：超过 120 字截断 -->
            <div class="fb-answer">{{ answerSummary(f.answer) }}</div>
            <div class="fb-comment"><b>反馈：</b>{{ f.comment || '无详细反馈' }}</div>
            <div class="fb-meta-row">
              <span class="text-sub">{{ f.user || '-' }} · {{ formatDate(f.created_at) }}<el-tag v-if="f.status === 'resolved'" type="success" size="small" effect="plain" style="margin-left:6px">已处理</el-tag></span>
              <div class="fb-actions">
                <el-button link type="primary" size="small" @click="adjustKeywordWeightByFeedback()">调整权重</el-button>
                <el-button v-if="f.status !== 'resolved'" link type="success" size="small" @click="markFeedbackProcessed(f.id)">已处理</el-button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 反馈闭环自动调整记录 -->
    <div class="app-card tools-card agg-card">
      <div class="card-head">
        <span class="card-head-title">🔁 反馈闭环自动调整记录</span>
        <div class="card-head-actions">
          <el-button size="small" @click="runFeedbackLoop">▶ 立即聚合</el-button>
          <el-button size="small" @click="loadFeedbackLoopAggs">刷新</el-button>
        </div>
      </div>
      <div class="agg-desc text-sub">每日聚合点击/反馈自动调整关键词权重（幅度受控 + 人工复核开关）；「待复核」记录需人工应用后生效，手动 +/- 覆盖优先</div>
      <div class="agg-table-wrap">
        <el-table :data="fbAggs" v-loading="fbAggLoading" size="small" class="tools-table">
        <el-table-column label="日期" width="100" prop="report_date" />
        <el-table-column label="关键词" min-width="120">
          <template #default="{ row }"><span class="fw-500">{{ row.keyword }}</span></template>
        </el-table-column>
        <el-table-column label="展示/点击/采纳/差评" width="150">
          <template #default="{ row }">{{ (row.shown_count || 0) }} / {{ (row.click_count || 0) }} / {{ (row.adopt_count || 0) }} / {{ (row.bad_count || 0) }}</template>
        </el-table-column>
        <el-table-column label="采纳率" width="80">
          <template #default="{ row }">{{ Math.round((row.adopt_rate || 0) * 100) }}%</template>
        </el-table-column>
        <el-table-column label="权重变化" width="120">
          <template #default="{ row }">{{ (row.old_score || 1).toFixed(2) }} → {{ (row.new_score || 1).toFixed(2) }}</template>
        </el-table-column>
        <el-table-column label="原因" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.reason || '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="fbAggStatus(row.status).type" size="small" effect="plain">{{ fbAggStatus(row.status).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <!-- 待复核记录需人工应用/忽略；已处理的显示调整来源 -->
            <template v-if="row.status === 'pending'">
              <el-button link type="success" size="small" @click="applyFeedbackAgg(row.id, 'apply')">应用</el-button>
              <el-button link type="info" size="small" @click="applyFeedbackAgg(row.id, 'ignore')">忽略</el-button>
            </template>
            <span v-else class="text-sub">{{ row.adjust_type === 'manual' ? '手动' : '自动' }}</span>
          </template>
        </el-table-column>
        <template #empty><el-empty description="暂无自动调整记录（点击/反馈数据不足或尚未聚合）" :image-size="50" /></template>
        </el-table>
      </div>
    </div>

    <!-- 新增关键词弹窗 -->
    <el-dialog v-model="addKeywordVisible" title="新增关键词" width="480px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="关键词" required>
          <el-input v-model="newKeywordText" placeholder="请输入关键词" maxlength="50" />
        </el-form-item>
        <el-form-item label="权重">
          <el-input-number v-model="newKeywordWeight" :min="0.1" :max="5.0" :step="0.1" step-strictly controls-position="right" style="width: 160px" />
          <div class="form-hint">权重范围 0.1 ~ 5.0，默认 1.0</div>
        </el-form-item>
        <el-form-item label="所属根节点">
          <el-select v-model="newKeywordRootId" style="width: 100%">
            <el-option label="全部" value="all" />
            <el-option v-if="rootTypes.length === 0" label="暂无节点数据" value="all" disabled />
            <el-option v-for="n in rootTypes" :key="n.id" :label="n.name" :value="n.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addKeywordVisible = false">取消</el-button>
        <el-button type="primary" @click="submitNewKeyword">确定新增</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import { useListLoader } from '../../composables/useListLoader'

/**
 * 运营工具 Tab：关键词权重调优 + 差评反馈明细 + 反馈闭环自动调整记录
 * 关键词 +/- 手动调整优先于自动聚合；「待复核」记录需人工应用后生效
 */
const props = defineProps({
  rootType: { type: String, default: '' },   // 当前根节点筛选（root_type）
  rootTypes: { type: Array, default: () => [] }, // 根节点列表 [{ id, root_type, name }]，用于新增关键词归属
})

/* ===== 关键词权重调优 ===== */
const keywords = ref([])

// 关键词加载：根节点快速切换时 useListLoader 内部请求序号保证只采用最新响应；
// 加载失败时清空列表，避免展示过期数据
const { loading: kwLoading, load: loadKeywords } = useListLoader(async () => {
  let url = '/api/v1/analytics/keywords/'
  if (props.rootType) url += '?root_type=' + encodeURIComponent(props.rootType)
  const data = await api.getJson(url)
  keywords.value = data.rows || []
}, {
  onError: (e) => {
    keywords.value = []
    ElMessage.error('加载关键词数据失败: ' + errMsg(e, '未知错误'))
  },
})

// 权重偏离 1.0 时着色：调高=绿，调低=黄，保持默认
function kwWeightType(w) {
  if (w > 1) return 'success'
  if (w < 1) return 'warning'
  return 'info'
}

async function adjustKeywordWeight(id, delta) {
  try {
    await api.put(`/api/v1/analytics/keywords/${id}/`, { delta })
    ElMessage.success(delta > 0 ? '已加权 +0.1' : '已降权 -0.1')
    // 刷新关键词表 + 自动调整记录（手动调整也写入审计）
    loadKeywords()
    loadFeedbackLoopAggs()
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

/* ===== 新增关键词 ===== */
const addKeywordVisible = ref(false)
const newKeywordText = ref('')
const newKeywordWeight = ref(1.0)
const newKeywordRootId = ref('all') // 选中根节点的节点 id（'all' 表示全局）

function openAddKeyword() {
  newKeywordText.value = ''
  newKeywordWeight.value = 1.0
  newKeywordRootId.value = 'all'
  addKeywordVisible.value = true
}

async function submitNewKeyword() {
  const keyword = newKeywordText.value.trim()
  const weight = newKeywordWeight.value
  if (!keyword) { ElMessage.error('请输入关键词'); return }
  if (isNaN(weight) || weight < 0.1 || weight > 5.0) { ElMessage.error('权重范围 0.1 ~ 5.0'); return }
  // 所属根节点：下拉 value 是节点 id，实际 root_type 在 rootTypes 中查，
  // 与筛选下拉同口径，避免把节点 id 误当 root_type 写入
  const node = props.rootTypes.find(n => n.id === newKeywordRootId.value)
  const rootType = node ? node.root_type : 'all'
  try {
    await api.postJson('/api/v1/analytics/keywords/', { keyword, weight_score: weight, root_type: rootType })
    ElMessage.success('已新增关键词')
    addKeywordVisible.value = false
    loadKeywords()
  } catch (e) {
    ElMessage.error(errMsg(e, '添加失败'))
  }
}

/* ===== 差评反馈列表 ===== */
const badFeedbacks = ref([])

const { loading: fbLoading, load: loadBadFeedbacks } = useListLoader(async () => {
  let url = '/api/v1/analytics/bad-feedbacks/'
  if (props.rootType) url += '?root_type=' + encodeURIComponent(props.rootType)
  const data = await api.getJson(url)
  badFeedbacks.value = data.rows || []
}, {
  onError: (e) => {
    badFeedbacks.value = []
    ElMessage.error('加载反馈数据失败: ' + errMsg(e, '未知错误'))
  },
})

// 答案摘要：超过 120 字截断加省略号（纯文本展示，模板插值自动转义）
function answerSummary(answer) {
  const s = answer || ''
  return 'A（摘要）: ' + (s.slice(0, 120) + (s.length > 120 ? '…' : ''))
}

/* 差评卡「调整权重」：反馈与关键词无直接关联，暂不自动定位，引导运营到关键词列表手动调整 */
function adjustKeywordWeightByFeedback() {
  ElMessage.info('请在关键词列表中手动调整相关关键词权重')
}

async function markFeedbackProcessed(fbId) {
  try {
    await api.put(`/api/v1/analytics/bad-feedbacks/${fbId}/`, { status: 'resolved' })
    ElMessage.success('已标记为已处理')
    loadBadFeedbacks()
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

/* ===== 反馈闭环自动调整记录 ===== */
const fbAggs = ref([])

const { loading: fbAggLoading, load: loadFeedbackLoopAggs } = useListLoader(async () => {
  const data = await api.getJson('/api/v1/analytics/feedback-loop/aggregations/?limit=100')
  fbAggs.value = data.rows || []
}, {
  onError: (e) => {
    fbAggs.value = []
    ElMessage.error('加载自动调整记录失败: ' + errMsg(e, '未知错误'))
  },
})

const FB_AGG_STATUS = {
  pending: { type: 'warning', text: '待复核' },
  applied: { type: 'success', text: '已应用' },
  ignored: { type: 'info', text: '已忽略' },
}
function fbAggStatus(status) {
  return FB_AGG_STATUS[status] || { type: '', text: status || '' }
}

/* 手动触发一次反馈闭环聚合（默认聚合昨天，支持运营即时回补） */
async function runFeedbackLoop() {
  try {
    await api.postJson('/api/v1/analytics/feedback-loop/run/', {})
    ElMessage.success('聚合完成，已刷新记录')
    loadFeedbackLoopAggs()
    loadKeywords()
  } catch (e) {
    ElMessage.error(errMsg(e, '聚合失败'))
  }
}

/* 人工复核：应用/忽略一条待复核的自动调整 */
async function applyFeedbackAgg(id, action) {
  try {
    await api.postJson('/api/v1/analytics/feedback-loop/apply/', { id, action })
    ElMessage.success(action === 'apply' ? '已应用调整' : '已忽略')
    loadFeedbackLoopAggs()
    loadKeywords()
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

// 根节点筛选变化：重载关键词 + 差评 + 反馈闭环（reloadCurrentTab → loadTabData('tools')）
watch(() => props.rootType, () => {
  loadKeywords()
  loadBadFeedbacks()
  loadFeedbackLoopAggs()
})

onMounted(() => {
  loadKeywords()
  loadBadFeedbacks()
  loadFeedbackLoopAggs()
})

defineExpose({ reload: () => { loadKeywords(); loadBadFeedbacks(); loadFeedbackLoopAggs() } })
</script>

<style scoped>
.panel-body {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 关键词 + 差评反馈：左右并排（5:4），保持自然高度 */
.tools-grid {
  flex-shrink: 0;
  display: grid;
  grid-template-columns: 5fr 4fr;
  gap: 16px;
  margin-bottom: 16px;
}

.tools-card {
  display: flex;
  flex-direction: column;
  min-width: 0;
  margin-bottom: 0;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.card-head-title {
  font-size: 16px;
  font-weight: 600;
}

.card-head-actions {
  display: flex;
  gap: 8px;
}

.tools-table {
  width: 100%;
}

.fw-500 {
  font-weight: 500;
}

/* 差评反馈列表：限定最大高度并内部滚动，避免页面过长 */
.feedback-list {
  max-height: 520px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.feedback-item {
  padding: 12px;
  background: var(--el-color-error-light-9, #fef2f2);
  border-left: 3px solid #f56c6c;
  border-radius: 6px;
  font-size: 13px;
}

.fb-question {
  font-weight: 500;
  margin-bottom: 6px;
  word-break: break-word;
}

.fb-answer {
  color: var(--app-text-sub);
  font-size: 12px;
  margin-bottom: 6px;
  word-break: break-word;
}

.fb-comment {
  padding: 6px 8px;
  background: var(--app-card-bg);
  border-radius: 4px;
  color: var(--app-text);
  word-break: break-word;
}

.fb-meta-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
}

.fb-actions {
  display: flex;
  gap: 4px;
}

/* 反馈闭环表：卡片撑满剩余高度，表格区域内部滚动，避免页面级滚动条 */
.agg-card {
  flex: 1;
  min-height: 0;
  margin-top: 0;
  margin-bottom: 0;
}

.agg-table-wrap {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.agg-desc {
  font-size: 13px;
  margin-bottom: 10px;
}

.empty {
  padding: 30px 0;
  text-align: center;
  color: var(--app-text-sub);
  font-size: 13px;
}

.form-hint {
  font-size: 12px;
  color: var(--app-text-sub);
  line-height: 1.5;
  margin-top: 4px;
}

@media (max-width: 900px) {
  .tools-grid {
    grid-template-columns: 1fr;
  }
}
</style>
