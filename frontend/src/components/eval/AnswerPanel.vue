<template>
  <div class="answer-panel-page">
    <!-- 工具栏：筛选器(左) + 手动评估(右) -->
    <div class="eval-toolbar mb-3">
      <div class="answer-toolbar">
        <div class="filters">
          <el-select v-model="days" style="width: 120px" @change="loadDashboard">
            <el-option v-for="opt in options" :key="opt.value" :label="opt.label" :value="opt.value" />
          </el-select>
          <el-select v-model="org.deptId" placeholder="全部部门" clearable style="width: 160px" @change="onDeptChange">
            <el-option v-for="d in org.departments" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="org.teamId" placeholder="全部团队" clearable style="width: 160px" :disabled="!org.deptId" @change="loadDashboard">
            <el-option v-for="t in org.teamsOfDept" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
          <el-button @click="loadDashboard">🔄 刷新</el-button>
          <span class="text-sub text-sm" style="white-space: nowrap; margin-left: 8px">{{ summaryText }}</span>
        </div>
        <div class="manual-eval">
          <el-input v-model="manualQaId" placeholder="QA ID" type="number" style="width: 100px" @input="manualEvalLoading = false" />
          <el-button type="primary" :loading="manualEvalLoading" @click="runManualEval">🔍 手动评估</el-button>
        </div>
      </div>
    </div>

    <!-- KPI 卡片 -->
    <div class="kpi-grid mb-3">
      <div class="kpi-card">
        <div class="kpi-label">评估对话数</div>
        <div class="kpi-value">{{ overview.totalEvaluated || 0 }}</div>
        <div class="text-sub text-sm">{{ `覆盖率 ${fmtPct(overview.coverageRate)} · 总对话 ${overview.totalQa || 0}` }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">低分对话</div>
        <div class="kpi-value kpi-red">{{ overview.lowScoreCount || 0 }}</div>
        <div class="text-sub text-sm">{{ `占比 ${fmtPct(overview.lowScoreRate)}` }}</div>
      </div>
      <div class="kpi-card kpi-highlight">
        <div class="kpi-label">安全告警</div>
        <div class="kpi-value">{{ overview.safetyAlertCount || 0 }}</div>
        <div class="text-sub text-sm">toxicity/bias &lt; 0.5</div>
      </div>
      <div class="kpi-card kpi-good">
        <div class="kpi-label">整体均分</div>
        <div class="kpi-value">{{ hasDimData ? fmtPct(overallAvg) : '--' }}</div>
        <div class="text-sub text-sm">12 维平均</div>
      </div>
    </div>

    <!-- 12 维质量画像(左:雷达图 右:每行维度+sparkline+环比) -->
    <div class="eval-panel mb-3">
      <PanelHeader titleClass="eval-panel-title">
        12 维质量画像
        <template #actions>
          <div class="text-sm text-sub">环比 = 本周均值 vs 上周均值 <span class="ml-2">↑ 上升 / ↓ 下降</span></div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body eval-portrait-body">
        <div class="eval-radar-wrap">
          <div v-if="dimsCleared" class="eval-empty">
            <div class="eval-empty-icon">🚫</div>
            <div>未选择任何展示维度，请在「系统配置 → 评估 → 评估维度」中勾选</div>
          </div>
          <div v-else-if="!hasDimData" class="eval-empty">
            <div class="eval-empty-icon">📊</div>
            <div>暂无评估数据</div>
          </div>
          <!-- 展示维度不足 3 个时雷达图无法成型（至少需要三角形），给出空态提示 -->
          <div v-else-if="visibleDimsOrdered.length < 3" class="eval-empty">
            <div class="eval-empty-icon">🚫</div>
            <div>展示维度不足 3 个，无法绘制雷达图</div>
          </div>
          <div v-else class="radar-chart-box">
            <VChart :option="radarOption" />
          </div>
        </div>
        <div ref="dimWrapRef" class="eval-dim-wrap">
          <div v-if="dimsCleared" class="eval-empty">
            <div class="eval-empty-icon">🚫</div>
            <div>未选择任何展示维度，请在「系统配置 → 评估 → 评估维度」中勾选</div>
          </div>
          <div v-else-if="!hasDimData" class="eval-empty">
            <div class="eval-empty-icon">📊</div>
            <div>暂无评估数据</div>
          </div>
          <template v-else>
            <!-- 按白名单过滤后的分组展开维度,未在白名单中的维度不再渲染 -->
            <div v-for="g in dimGroupsOrder" :key="g.key" class="dim-group mb-3">
              <div class="dim-group-title">{{ g.label }}</div>
              <div class="dim-table">
                <div class="dim-row dim-row-head">
                  <span class="dim-col-name">维度</span>
                  <span class="dim-col-avg">均分</span>
                  <span class="dim-col-mom">环比</span>
                  <span class="dim-col-spark">7日趋势</span>
                  <span class="dim-col-cnt">样本</span>
                </div>
                <div v-for="r in g.rows" :key="r.dimName" class="dim-row">
                  <span class="dim-col-name">{{ r.label }}</span>
                  <span class="dim-col-avg"><el-tag :type="scoreTagType(r.avg)" size="small">{{ fmtPct(r.avg) }}</el-tag></span>
                  <span class="dim-col-mom"><span :class="r.momInfo.cls">{{ r.momInfo.text }}</span></span>
                  <span class="dim-col-spark"><Sparkline :values="r.trend" :width="sparkWidth" /></span>
                  <span class="dim-col-cnt text-sub text-sm">{{ r.count }}</span>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 低分对话列表 -->
    <div class="eval-panel answer-low-panel">
      <PanelHeader titleClass="eval-panel-title">
        低分对话 Top N
        <template #actions>
          <div class="text-sub text-sm">均分 &lt; 0.5 的对话（点击查看 12 维明细）</div>
        </template>
      </PanelHeader>
      <div class="eval-panel-body eval-low-score-body">
        <el-table :data="lowScoreRows" v-loading="loading" size="small">
          <el-table-column label="QA ID" width="80" prop="qa_record_id" />
          <el-table-column label="问题" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.question }}</template>
          </el-table-column>
          <el-table-column label="回答摘要" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.answer }}</template>
          </el-table-column>
          <el-table-column label="均分" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.avg_score)" size="small">{{ fmtPct(row.avg_score) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="最低维度" width="100">
            <template #default="{ row }"><el-tag size="small" effect="plain">{{ DIM_LABEL[row.min_dimension] || row.min_dimension }}</el-tag></template>
          </el-table-column>
          <el-table-column label="最低分" width="90" align="right">
            <template #default="{ row }"><el-tag :type="scoreTagType(row.min_score)" size="small">{{ fmtPct(row.min_score) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="知识库" width="110" prop="root_type" />
          <el-table-column label="时间" width="150">
            <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="showQaDetail(row.qa_record_id)">查看明细</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <!-- 区分两种空状态: 无评估数据 vs 有评估但无低分对话 -->
            <el-empty v-if="!totalEvaluated" description="暂无评估数据" :image-size="70" />
            <el-empty v-else description="无低分对话,质量良好" :image-size="70" />
          </template>
        </el-table>
      </div>
    </div>

    <!-- Dialog: QA 评估明细(12 维) -->
    <el-dialog :title="'QA 评估明细 · QA #' + qaDetailId" v-model="qaDetailVisible" width="640px" top="6vh" :close-on-click-modal="false">
      <div v-if="qaDetailLoading" class="text-loading">加载中...</div>
      <div v-else-if="qaDetailError" class="detail-error">{{ qaDetailError }}</div>
      <template v-else-if="qaDetail">
        <!-- 对话区 -->
        <div class="mb-3">
          <div class="detail-head mb-2">
            <strong>对话内容</strong>
            <span class="text-sm text-sub">
              均分 <el-tag :type="scoreTagType(qaDetail.avgScore)" size="small">{{ fmtPct(qaDetail.avgScore) }}</el-tag>
              · 用户 {{ qaDetail.user }} · 领域 {{ qaDetail.rootType }}
            </span>
          </div>
          <div class="mb-2"><strong class="text-sub">问题:</strong> {{ qaDetail.question }}</div>
          <div><strong class="text-sub">回答:</strong> {{ qaDetail.answer }}</div>
        </div>
        <div class="mb-3 text-sm text-sub">
          耗时 {{ qaDetail.latencyMs }}ms · Token {{ qaDetail.tokensTotal }} · 命中切片 {{ qaDetail.hitCount }} 个 · {{ qaDetail.createdAt }}
        </div>
        <!-- 12 维明细(按 4 大类分组,受展示维度白名单过滤) -->
        <div v-if="qaDetailGroups.length">
          <div><strong>评估明细</strong></div>
          <div v-for="g in qaDetailGroups" :key="g.label" class="mb-3">
            <div class="detail-head mb-2"><strong>{{ g.label }}</strong></div>
            <div v-for="s in g.scores" :key="s.dimension" class="score-row">
              <div class="detail-head">
                <span>{{ DIM_LABEL[s.dimension] || s.dimension }}</span>
                <span>
                  <el-tag :type="scoreTagType(s.score)" size="small">{{ fmtPct(s.score) }}</el-tag>
                  <span class="text-sub text-sm">{{ s.eval_latency_ms }}ms</span>
                </span>
              </div>
              <div class="text-sm text-sub score-reason">{{ s.reason || '(无理由)' }}</div>
            </div>
          </div>
        </div>
        <div v-else class="text-sub">未选择任何展示维度</div>
      </template>
      <template #footer>
        <el-button type="primary" @click="qaDetailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import Sparkline from './Sparkline.vue'
import PanelHeader from '../base/PanelHeader.vue'
import { useTheme } from '../../composables/useTheme'
import { buildRadarOption, chartThemeColors } from '../../utils/chart'
import VChart from '../base/VChart.vue'
import { useListLoader } from '../../composables/useListLoader'
import { useTimeRange } from '../../composables/useTimeRange'
import { useOrgFilter } from './useOrgFilter'
import { ALL_DIMS_ORDERED, DIM_GROUPS, DIM_LABEL, fmtPct, scoreTagType } from './constants'

/**
 * 回答质量 Tab（原 answer 面板）——生产对话采样评估看板（DeepEval 12 维）
 * - 12 维质量画像（雷达图 + 每维度 sparkline/环比）,受展示维度白名单过滤
 * - 低分对话 Top N 列表 + 单条 12 维明细
 * - 手动评估:派发后轮询 qa-detail,带 token 取消旧轮询与 localStorage 5 分钟缓存
 */
const org = useOrgFilter()
const { days, options } = useTimeRange()

const overview = reactive({ totalEvaluated: 0, coverageRate: null, totalQa: 0, lowScoreCount: 0, lowScoreRate: null, safetyAlertCount: 0, threshold: null, days: 7 })
const groups = ref({})
// 展示维度白名单:由 SystemConfig.EVAL_DISPLAY_DIMENSIONS 控制
// null = 未加载（默认全部可见,保持向后兼容）;空数组 = 用户主动清空（不展示任何维度）
const displayDimensions = ref(null)
const lowScoreRows = ref([])
const totalEvaluated = ref(0)

const summaryText = computed(() => {
  if (overview.days === null) return ''
  return `窗口 ${overview.days} 天 · 范围 ${org.scopeText} · 阈值 ${overview.threshold}`
})

function onDeptChange() {
  org.onDeptChange()
  loadDashboard()
}

/* ===== 展示维度白名单过滤 ===== */
function isDimVisible(dim) {
  // null/undefined = 配置未加载,默认全部可见（首次渲染或老部署未初始化配置时兜底）
  if (displayDimensions.value === null) return true
  return displayDimensions.value.includes(dim)
}

// 用户主动清空展示维度时（空数组）,提示并清空维度画像区域（区别于"无评估数据"）
const dimsCleared = computed(() => Array.isArray(displayDimensions.value) && displayDimensions.value.length === 0)

// 按白名单过滤后的分组（仅保留仍有可见维度的分组）
const visibleGroups = computed(() => {
  const result = {}
  for (const [groupKey, g] of Object.entries(DIM_GROUPS)) {
    const visibleDims = g.dims.filter(d => isDimVisible(d))
    if (visibleDims.length > 0) result[groupKey] = { label: g.label, dims: visibleDims }
  }
  return result
})

// 按白名单过滤后的维度顺序（雷达图用）
const visibleDimsOrdered = computed(() => ALL_DIMS_ORDERED.filter(d => isDimVisible(d)))

// dimension_groups 为空对象时(后端无评估数据),整体均分显示 -- ,不渲染雷达图/sparkline
const hasDimData = computed(() => Object.values(groups.value).some(g => g.dimensions && g.dimensions.length > 0))

const { isDark } = useTheme()
// 图表主题色：依赖 isDark，主题切换时重建（canvas 图表需取计算后的 CSS 变量色值）
const chartColors = computed(() => chartThemeColors(isDark.value))
// 雷达图 option：维度白名单/分组数据变化时自动重渲染
const radarOption = computed(() => buildRadarOption({
  groups: groups.value,
  dims: visibleDimsOrdered.value,
  labels: DIM_LABEL,
  colors: chartColors.value,
}))

// 整体均分 = 4 大类均分的平均
const overallAvg = computed(() => {
  const groupAvgs = Object.values(groups.value).map(g => g.avg_score).filter(v => v > 0)
  return groupAvgs.length ? groupAvgs.reduce((s, v) => s + v, 0) / groupAvgs.length : 0
})

// 环比展示:上升/下降/持平/无数据
function momInfo(mom) {
  if (mom === null || mom === undefined) return { text: '环比 —', cls: 'text-sub' }
  const pct = fmtPct(mom)
  if (mom > 0) return { text: '↑ ' + pct, cls: 'mom-up' }
  if (mom < 0) return { text: '↓ ' + pct, cls: 'mom-down' }
  return { text: '— 0.0%', cls: 'mom-flat' }
}

// 按白名单过滤后的分组展开成一行一维度的表
const dimRows = computed(() => {
  const rows = []
  for (const [groupKey, g] of Object.entries(visibleGroups.value)) {
    const gd = groups.value[groupKey] || { dimensions: [] }
    for (const dimName of g.dims) {
      const info = gd.dimensions.find(x => x.name === dimName)
      if (!info) continue
      rows.push({
        groupKey,
        label: DIM_LABEL[dimName] || dimName,
        dimName,
        avg: info.avg || 0,
        count: info.count || 0,
        momInfo: momInfo(info.mom_change),
        trend: info.trend_7d || [],
      })
    }
  }
  return rows
})

// 按分组渲染:保持 visibleGroups 顺序（已按 DIM_GROUPS 原始顺序过滤）
const dimGroupsOrder = computed(() => {
  const grouped = {}
  for (const r of dimRows.value) {
    if (!grouped[r.groupKey]) grouped[r.groupKey] = { label: visibleGroups.value[r.groupKey].label, rows: [] }
    grouped[r.groupKey].rows.push(r)
  }
  return Object.entries(grouped).map(([key, g]) => ({ key, label: g.label, rows: g.rows }))
})

/* ===== sparkline 容器宽度测量（viewBox 等比缩放,与原 requestAnimationFrame 测量等价） ===== */
const dimWrapRef = ref(null)
const sparkWidth = ref(140)
let resizeObserver = null

const { loading, load: loadDashboard } = useListLoader(async () => {
  const params = new URLSearchParams()
  params.set('days', days.value)
  if (org.deptId.value) params.set('dept_id', org.deptId.value)
  if (org.teamId.value) params.set('team_id', org.teamId.value)
  const qs = '?' + params.toString()

  // 并行加载 overview + low-score 两个接口(trend 接口当前 UI 未使用,避免无效请求)
  const [data, lowScore] = await Promise.all([
    api.getJson('/api/v1/analytics/eval-dashboard/overview/' + qs),
    api.getJson('/api/v1/analytics/eval-dashboard/low-score-qa/' + qs + '&limit=20'),
  ])
  // 缓存展示维度白名单:后端返回 null/undefined 时保持 null（按全部展示兜底）
  displayDimensions.value = data.display_dimensions ?? null
  groups.value = data.dimension_groups || {}
  overview.totalEvaluated = data.total_evaluated || 0
  overview.coverageRate = data.coverage_rate
  overview.totalQa = data.total_qa || 0
  overview.lowScoreCount = data.low_score_count || 0
  overview.lowScoreRate = data.low_score_rate
  overview.safetyAlertCount = data.safety_alert_count || 0
  overview.threshold = data.threshold
  overview.days = data.days
  // 传入 total_evaluated 以区分"无评估数据"和"有评估但无低分"两种空状态
  totalEvaluated.value = data.total_evaluated
  lowScoreRows.value = lowScore.rows || []
}, { errorPrefix: '看板加载失败' })

/* ===== QA 评估明细 ===== */
// 请求序号守卫:连续查看不同 QA 时,旧响应后返回不覆盖新弹窗内容
let qaDetailSeq = 0
const qaDetailVisible = ref(false)
const qaDetailLoading = ref(false)
const qaDetailError = ref('')
const qaDetailId = ref('')
const qaDetail = ref(null)

async function showQaDetail(qaId) {
  const mySeq = ++qaDetailSeq
  qaDetailId.value = qaId
  qaDetailVisible.value = true
  qaDetailLoading.value = true
  qaDetailError.value = ''
  qaDetail.value = null
  try {
    const data = await api.getJson('/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=' + qaId)
    // 旧响应后返回时丢弃
    if (mySeq !== qaDetailSeq) return
    const qa = data.qa
    qaDetail.value = {
      avgScore: data.avg_score,
      user: qa.user,
      rootType: qa.root_type,
      question: qa.question,
      answer: qa.answer,
      latencyMs: qa.latency_total_ms,
      tokensTotal: qa.tokens_total,
      hitCount: (qa.retrieval_hits || []).length,
      createdAt: formatDate(qa.created_at),
    }
    qaDetailScores.value = data.scores || []
  } catch (e) {
    if (mySeq !== qaDetailSeq) return
    qaDetailError.value = '加载失败: ' + errMsg(e, String(e))
  } finally {
    if (mySeq === qaDetailSeq) qaDetailLoading.value = false
  }
}

const qaDetailScores = ref([])
// 12 维明细按 4 大类分组（仅保留白名单内维度且有得分的组）
const qaDetailGroups = computed(() => {
  const groupsOut = []
  for (const [, g] of Object.entries(visibleGroups.value)) {
    const scores = qaDetailScores.value.filter(s => g.dims.includes(s.dimension))
    if (!scores.length) continue
    groupsOut.push({ label: g.label, scores })
  }
  return groupsOut
})

/* ===== 手动评估（派发 + 轮询,带 token 取消与 localStorage 缓存） ===== */
// 手动评估相关的组件级状态
// evalToken: 每次派发评估时递增,用于取消旧轮询(用户切换 QA ID 时)
const evalToken = ref(0)
// localStorage key:缓存最近评估过的 QA ID,避免重复消耗 LLM 配额
const EVAL_CACHE_KEY = 'rag_manual_eval_ids'
// 缓存有效期(ms):5 分钟内同一 QA ID 重复评估会 toast 提醒
const EVAL_CACHE_TTL = 5 * 60 * 1000

const manualQaId = ref('')
const manualEvalLoading = ref(false)

/** 读取本地缓存的已评估 QA ID 列表(自动清理过期) */
function loadEvalCache() {
  try {
    const raw = localStorage.getItem(EVAL_CACHE_KEY)
    if (!raw) return {}
    const map = JSON.parse(raw)
    const now = Date.now()
    // 清理过期条目并写回
    const cleaned = {}
    for (const [id, ts] of Object.entries(map)) {
      if (now - ts < EVAL_CACHE_TTL) cleaned[id] = ts
    }
    if (Object.keys(cleaned).length !== Object.keys(map).length) {
      localStorage.setItem(EVAL_CACHE_KEY, JSON.stringify(cleaned))
    }
    return cleaned
  } catch {
    return {}
  }
}

/** 将 QA ID 写入本地缓存(评估成功后调用) */
function saveEvalCache(qaId) {
  try {
    const map = loadEvalCache()
    map[String(qaId)] = Date.now()
    localStorage.setItem(EVAL_CACHE_KEY, JSON.stringify(map))
  } catch { /* 忽略 localStorage 写入失败(如隐私模式) */ }
}

/** 检查 QA ID 是否在缓存中,返回 true 表示近期已评估过 */
function checkEvalCache(qaId) {
  const map = loadEvalCache()
  return String(qaId) in map
}

async function runManualEval() {
  const qaId = (manualQaId.value || '').trim()
  if (!qaId) { ElMessage.error('请输入 QA 记录 ID'); return }

  // localStorage 缓存检查:5 分钟内同一 QA ID 已评估过则 toast 提醒
  // 不阻止用户,因为可能需要重新评估(如模型升级/内容变更)
  if (checkEvalCache(qaId)) {
    ElMessage.info(`QA ID ${qaId} 近期已评估过,将覆盖之前的结果`)
  }

  // 递增 token,使旧轮询(如存在)在下一次迭代时自动取消
  const myToken = ++evalToken.value
  manualEvalLoading.value = true
  ElMessage.info('评估已派发,正在后台执行(约 2~3 分钟),请勿重复点击...')

  try {
    // POST 立即返回 eval_batch_id,实际评估在 Celery 异步执行
    const resp = await api.postJson('/api/v1/analytics/multi-dim-eval/', { qa_record_id: parseInt(qaId) })
    if (!resp || !resp.queued || !resp.eval_batch_id) {
      throw new Error(resp?.detail || '派发评估失败')
    }
    const evalBatchId = resp.eval_batch_id
    // 轮询 qa-detail 接口,检查本次 batch_id 的评估结果是否落库
    // 12 维评估串行耗时 90~180s+,超时设 5 分钟兜底;传入 myToken,若用户中途切换 QA ID,旧轮询自动取消
    await pollManualEvalResult(parseInt(qaId), evalBatchId, myToken)
    // 评估成功后写入 localStorage 缓存,避免短期重复评估
    saveEvalCache(qaId)
    ElMessage.success('评估完成,已弹出明细')
    showQaDetail(parseInt(qaId))
    loadDashboard()
  } catch (e) {
    // 若 token 已被更新(用户切换了 QA ID),说明是被主动取消,不算错误
    if (myToken !== evalToken.value) return
    ElMessage.error('评估失败: ' + errMsg(e, e))
  } finally {
    // 只有当前 token 仍有效时才恢复按钮(否则是新评估已接管)
    if (myToken === evalToken.value) manualEvalLoading.value = false
  }
}

// 轮询单条 QA 评估结果,直到本次 batch_id 的维度数达标或超时
// 评估维度数由后端 EVAL_DISPLAY_DIMENSIONS 控制(默认 12),用 8 作为最低门槛
// 避免配置变更后前端硬编码 12 导致永远等不到
// token:用于取消旧轮询——当用户切换 QA ID 时,evalToken 递增,旧 token 失效
async function pollManualEvalResult(qaId, evalBatchId, token) {
  const POLL_INTERVAL_MS = 3000   // 每 3 秒轮询一次
  const MAX_WAIT_MS = 5 * 60 * 1000 // 最长等待 5 分钟
  const MIN_DIMS_THRESHOLD = 8     // 至少 8 维落库才算完成(兼容分组配置)
  const startedAt = Date.now()

  while (Date.now() - startedAt < MAX_WAIT_MS) {
    // 若 token 已被更新(用户切换了 QA ID),取消本次轮询
    if (token !== evalToken.value) throw new Error('cancelled')
    await new Promise(r => setTimeout(r, POLL_INTERVAL_MS))
    // 睡眠后再次检查 token,避免在取消后仍发请求
    if (token !== evalToken.value) throw new Error('cancelled')
    try {
      const data = await api.getJson(`/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=${qaId}`)
      // 只统计本次 batch_id 的维度,避免被旧评估结果误判为完成
      const currentBatchScores = (data.scores || []).filter(s => s.eval_batch_id === evalBatchId)
      if (currentBatchScores.length >= MIN_DIMS_THRESHOLD) return // 评估完成
    } catch (e) {
      // 轮询单次失败不中断,继续重试(可能是网络抖动)
      console.warn('[manualEval] 轮询失败,将继续重试:', e)
    }
  }
  throw new Error('评估超时(5 分钟内未完成),请稍后刷新查看结果')
}

onMounted(() => {
  loadDashboard()
  // 容器宽度变化时重测 sparkline 宽度（含 Tab 首次挂载时）
  resizeObserver = new ResizeObserver(() => {
    if (dimWrapRef.value) {
      const w = Math.round(dimWrapRef.value.clientWidth)
      if (w > 0) sparkWidth.value = w
    }
  })
  if (dimWrapRef.value) resizeObserver.observe(dimWrapRef.value)
})

onBeforeUnmount(() => {
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
})

defineExpose({ reload: loadDashboard })
</script>

<style scoped>
/* 内容超高时在面板容器内部滑动,避免低分列表面板超出父级 */
.answer-panel-page {
  height: 100%;
  overflow-y: auto;
}

.answer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: nowrap;
}

/* 低分对话列表是最后一个面板,补底部间距与同级 mb-3 对齐 */
.answer-low-panel {
  margin-bottom: 16px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
  flex-wrap: wrap;
}

.manual-eval {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.ml-2 { margin-left: 8px; }

.text-sub { color: var(--el-text-color-secondary, #6b7280); }

/* 雷达图容器：固定高度（ECharts 需显式容器尺寸） */
.radar-chart-box {
  height: 400px;
  width: 100%;
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

.detail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.score-row {
  padding: 8px 0;
  border-bottom: 1px solid var(--app-border);
}

.score-reason {
  margin-top: 4px;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
