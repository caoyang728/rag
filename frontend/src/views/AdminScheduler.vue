<template>
  <div class="page-container admin-scheduler-page">
    <!-- 权限检查：仅超级管理员 / 维护管理员可访问（与系统配置/任务看板页对齐） -->
    <PageGuard :allowed="userStore.isSystemMaintainer" message="仅超级管理员或维护管理员可访问此页面">
      <!-- ===== 页头 ===== -->
      <div class="page-header">
        <div>
          <div class="page-title">定时任务</div>
          <div class="page-desc">Celery Beat 调度配置（修改需提交工单，高风险项需复核，审批通过后热生效）</div>
        </div>
        <!-- 顶部操作入口：工单中心（默认待我处理视图，与系统配置页一致跳 #/ticket） -->
        <el-button size="small" @click="router.push('/ticket')">📋 工单列表</el-button>
      </div>

      <!-- ===== 内容区：tabs 撑满高度，面板内部滚动 ===== -->
      <div class="page-body tabs-fill">
      <!-- ===== 页签：任务调度 / 忙闲视图 ===== -->
      <el-tabs v-model="activeSheet" @tab-change="onSheetChange">
        <!-- ===== 任务调度：任务卡片列表 ===== -->
        <el-tab-pane label="任务调度" name="tasks">
          <div class="card task-sheet">
            <PanelHeader wrap>
              任务调度 <span class="text-sub task-count-sub">（{{ taskTotal }} 个任务）</span>
              <template #actions>
                <span class="text-sub text-sm">cron 格式：分 时 日 月 周（周 0-6，0=周日）</span>
              </template>
            </PanelHeader>
            <div v-loading="loading" class="task-list card-scroll">
              <div v-if="!loading && tasks.length === 0" class="task-empty">暂无定时任务</div>
              <div v-for="t in tasks" :key="t.key" class="task-item">
                <div class="task-item-head">
                  <div class="task-item-label">
                    <span>{{ t.label }}</span>
                    <!-- 所有调度配置均为高风险项，工单需复核 -->
                    <span class="config-badge config-badge-risk" title="高风险项，工单需复核">⚠️ 高风险</span>
                    <!-- 待审批工单 badge：有未完成工单时提示，点击打开工单中心（待我处理视图） -->
                    <span
                      v-if="t.pending_ticket_count > 0"
                      class="task-pending-badge"
                      title="该任务有待审批工单"
                      @click="router.push('/ticket')"
                    >⏳ 待审批 {{ t.pending_ticket_count }}</span>
                  </div>
                  <div class="task-item-actions">
                    <el-button size="small" @click="openEditModal(t)">✏️ 编辑</el-button>
                  </div>
                </div>
                <div class="task-item-desc">{{ t.description || '' }}</div>
                <div class="task-item-meta">
                  <div class="task-cron">
                    <!-- cron 分字段展示：每段一个灰底小框，未启用时整体置灰 -->
                    <span v-for="f in CRON_FIELDS" :key="f.key" class="cron-field">
                      <em>{{ f.label }}</em>{{ t.cron_fields[f.key] || '*' }}
                    </span>
                    <!-- humanize 中文解释：把 cron 翻译成人话，与任务描述解耦 -->
                    <span class="task-humanized">{{ t.humanized || humanizeCron(t.cron) }}</span>
                  </div>
                  <span class="task-status" :class="t.enabled ? 'task-status-on' : 'task-status-off'">
                    {{ t.enabled ? '运行中' : '已停用' }}
                  </span>
                </div>
                <div class="task-item-key">{{ t.key }}</div>
              </div>
            </div>
          </div>
        </el-tab-pane>

        <!-- ===== 忙闲视图：Outlook 风格日程（周视图 / 日视图） =====
             基于各任务 cron + estimated_minutes（含 20% 缓冲）在日历上按时间段摆放任务块，
             颜色按任务固定区分；重叠时段并排展示，便于错峰调整调度时间。
             全天运行类任务（步长/每小时）与每月/每年固定日期任务不纳入。 -->
        <el-tab-pane label="忙闲视图" name="busy">
          <div class="card busy-sheet">
            <div class="busy-head">
              <span class="card-title">忙闲视图 <span class="text-sub task-count-sub">预估工时已含 20% 缓冲，同色 = 同一任务</span></span>
              <el-radio-group v-model="busyView" size="small">
                <el-radio-button value="week">周视图</el-radio-button>
                <el-radio-button value="day">日视图</el-radio-button>
              </el-radio-group>
            </div>

            <!-- 周视图：7 天 × 24 小时时间轴，15 分钟一格。
                 表头（星期行）与格线区拆分为上下两块：表头固定不滚动，格线区内部纵向滚动 -->
            <div v-if="busyView === 'week'" class="cal-week">
              <!-- 固定表头：与格线区同列宽，滚动时始终停留顶部 -->
              <div class="cal-week-head">
                <div class="cal-corner">时/日</div>
                <div v-for="label in BUSY_DAY_LABELS" :key="label" class="cal-day-head">{{ label }}</div>
              </div>
              <!-- 时间轴格线区（内部纵向滚动）：24 小时背景格线（每小时 4 格 = 96 行），时间标签仅在整点显示。
                   每个时刻行 = 时间列 + 7 天列共 8 格，与 grid-template-columns: 56px repeat(7,1fr) 严格对齐，
                   多一格会换行导致整个周视图垂直错位（日视图为 2 列故不受影响） -->
              <div class="cal-week-body">
                <template v-for="h in 24" :key="'h' + h">
                  <template v-for="q in 4" :key="'q' + q">
                    <div class="cal-time" :class="q === 1 ? '' : 'cal-time-quarter'">
                      {{ q === 1 ? pad2(h - 1) + ':00' : '' }}
                    </div>
                    <div v-for="d in 7" :key="'d' + d" class="cal-hour-cell"></div>
                  </template>
                </template>
                <!-- 每个星期列叠加任务块（绝对定位，重叠并排） -->
                <div
                  v-for="(blocks, di) in weekDayBlocks"
                  :key="'col' + di"
                  class="cal-day-body"
                  :style="dayBodyStyle(di)"
                >
                  <div
                    v-for="(p, pi) in blocks"
                    :key="p.b.t.key + '-' + pi"
                    class="cal-block"
                    :style="blockStyle(p)"
                    :title="blockTitle(p.b)"
                  >
                    <span class="cal-block-name">{{ p.b.t.label }}</span>
                    <span v-if="blockShowDuration(p.b)" class="cal-block-duration">{{ p.b.endMin - p.b.startMin }} 分钟</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- 日视图：与周视图同构——表头（星期切换）固定，时间轴格线区内部纵向滚动。
                 星期切换与表头合并：选中项占满剩余宽度、其余固定宽度 -->
            <div v-else class="cal-day">
              <div class="cal-day-head-row">
                <div class="cal-corner">时/日</div>
                <button
                  v-for="(label, i) in BUSY_DAY_LABELS"
                  :key="label"
                  class="cal-day-tab"
                  :class="{ active: busyDayIndex === i }"
                  @click="busyDayIndex = i"
                >{{ label }}</button>
              </div>
              <!-- 时间轴格线区（内部纵向滚动）：单列 24 小时，表头已独立到上方 -->
              <div class="cal-day-scroll">
                <template v-for="h in 24" :key="'h' + h">
                  <template v-for="q in 4" :key="'q' + q">
                    <div class="cal-time" :class="q === 1 ? '' : 'cal-time-quarter'">
                      {{ q === 1 ? pad2(h - 1) + ':00' : '' }}
                    </div>
                    <div class="cal-hour-cell" :class="q === 1 ? 'cal-hour-mark' : 'cal-hour-quarter'"></div>
                  </template>
                </template>
                <div class="cal-day-body">
                  <div
                    v-for="(p, pi) in dayLayout"
                    :key="p.b.t.key + '-' + pi"
                    class="cal-block"
                    :style="blockStyle(p)"
                    :title="blockTitle(p.b)"
                  >
                    <span class="cal-block-name">{{ p.b.t.label }}</span>
                    <span v-if="blockShowDuration(p.b)" class="cal-block-duration">{{ p.b.endMin - p.b.startMin }} 分钟</span>
                  </div>
                </div>
              </div>
              <!-- 当日无任务提示 -->
              <div v-if="dayLayout.length === 0" class="busy-day-empty">{{ BUSY_DAY_LABELS[busyDayIndex] }}无定时任务</div>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
      </div>
    </PageGuard>

    <!-- ===== 编辑定时任务弹窗（复用公共 BaseDialog：高度随内容自适应） =====
         内容：cron 分字段输入 + 实时中文解释/表达式错误提示 + 启停开关 + 变更原因。
         表达式不合法或值未变化时提交按钮禁用，校验通过才允许提交工单。 -->
    <BaseDialog v-model="editVisible" :title="'编辑定时任务 · ' + (editingTask ? editingTask.label : '')" width="680px" min-width="680px" height="auto" min-height="0" :close-on-click-modal="false">
      <template v-if="editingTask">
        <el-alert type="info" :closable="false" show-icon>
          <template #title>调度键：{{ editingTask.key }}（高风险项，工单需复核）</template>
        </el-alert>

        <div class="form-item cron-form-block">
          <div class="form-label">cron 表达式（分 时 日 月 周，支持 * / 逗号 / 区间 / 步长）</div>
          <div class="cron-form-row">
            <div v-for="f in CRON_FIELDS" :key="f.key" class="cron-form-item">
              <label class="cron-form-label">{{ f.label }}<span class="cron-form-range">{{ f.range }}</span></label>
              <el-input v-model="cronForm[f.key]" placeholder="*" />
            </div>
          </div>
          <div class="cron-preview">当前表达式：<code>{{ cronPreview }}</code></div>
          <div class="cron-explain" :class="{ 'cron-explain-error': !cronState.valid }">{{ cronState.explain }}</div>
        </div>

        <div class="form-item cron-enabled-row">
          <div class="form-label">启用状态</div>
          <el-switch v-model="enabledFlag" />
          <span class="cron-enabled-hint">{{ enabledFlag ? '停用后任务将不再触发' : '启用后按新调度时间触发' }}</span>
        </div>

        <div class="form-item">
          <div class="form-label">变更原因 <span class="required">*</span></div>
          <el-input
            v-model="reasonText"
            type="textarea"
            :rows="3"
            placeholder="请说明本次调度变更的原因（如：评估任务成本控制、错峰避开高峰期），便于审批人判断"
          />
        </div>
      </template>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <!-- 表达式不合法或值未变化时禁用，避免无效工单 -->
        <el-button type="primary" :disabled="!canSubmit" @click="submitEditTicket">提交工单</el-button>
      </template>
    </BaseDialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { errMsg } from '../utils/format'
import PanelHeader from '../components/base/PanelHeader.vue'
import BaseDialog from '../components/base/BaseDialog.vue'
import PageGuard from '../components/base/PageGuard.vue'

const userStore = useUserStore()
const router = useRouter()

/* ==========================================================
   常量：cron 字段中文名/取值范围与校验区间
   （与后端 scheduler_registry 的语义保持一致，前端先行校验）
   ========================================================== */
const CRON_FIELDS = [
  { key: 'minute', label: '分', range: '0-59' },
  { key: 'hour', label: '时', range: '0-23' },
  { key: 'day_of_month', label: '日', range: '1-31' },
  { key: 'month', label: '月', range: '1-12' },
  { key: 'day_of_week', label: '周', range: '0-6 (0=周日)' },
]
// 各段取值范围，与后端 scheduler_registry._CRON_RANGES 一致
const CRON_RANGES = { minute: [0, 59], hour: [0, 23], day_of_month: [1, 31], month: [1, 12], day_of_week: [0, 6] }

/* ==========================================================
   状态
   ========================================================== */
const activeSheet = ref('tasks')     // 当前页签：tasks / busy
const loading = ref(false)
const tasks = ref([])                // 任务清单
const taskTotal = ref(0)
// 编辑弹窗状态
const editVisible = ref(false)
const editingTask = ref(null)        // 当前编辑中的任务（回显 + "值未变化"判断）
const cronForm = reactive({ minute: '', hour: '', day_of_month: '', month: '', day_of_week: '' })
const enabledFlag = ref(true)
const reasonText = ref('')

/* ============ 加载任务清单 ============ */
async function loadTasks() {
  loading.value = true
  try {
    const data = await api.getJson('/api/v1/system/scheduler/tasks/')
    tasks.value = data.tasks || []
    taskTotal.value = data.total || tasks.value.length
  } catch (e) {
    tasks.value = []
    taskTotal.value = 0
    ElMessage.error('加载失败：' + errMsg(e, '未知错误'))
  } finally {
    loading.value = false
  }
}

/* ==========================================================
   编辑弹窗：打开时回显当前值，由 cronState 实时校验并控制提交按钮
   ========================================================== */
function openEditModal(task) {
  editingTask.value = task
  // 回显 cron 分字段（预设当前值，便于微调）
  CRON_FIELDS.forEach(f => { cronForm[f.key] = task.cron_fields[f.key] || '*' })
  enabledFlag.value = task.enabled
  reasonText.value = ''
  editVisible.value = true
}

// 任一输入变化时触发（v-model 双向绑定后由 computed 自动重算，此函数仅占位保持语义）
function onCronInput() {}

/* ============ 从输入框拼装并校验 cron 表达式（非法返回 null） ============ */
function buildCronFromInputs() {
  const parts = []
  for (const f of CRON_FIELDS) {
    const val = (cronForm[f.key] || '').trim()
    const range = CRON_RANGES[f.key]
    if (!validateCronField(val, range[0], range[1])) return null
    parts.push(val || '*')
  }
  return parts.join(' ')
}

/* ============ 拼装 cron 表达式（不做校验，仅用于预览展示） ============ */
const cronPreview = computed(() =>
  CRON_FIELDS.map(f => (cronForm[f.key] || '').trim() || '*').join(' ')
)

/* ============ cron 实时校验结果：{ valid, cron, explain } ============
 * 表达式不合法 → explain 为错误提示（红色展示）；
 * 合法 → explain 为中文解释（humanizeCron）。
 */
const cronState = computed(() => {
  const cron = buildCronFromInputs()
  if (cron === null) return { valid: false, cron: '', explain: '表达式错误' }
  return { valid: true, cron, explain: humanizeCron(cron) }
})

/* ============ 提交按钮可用性：表达式合法且值有变化（cron 或启停任一变化） ============ */
const canSubmit = computed(() => {
  const task = editingTask.value
  if (!task || !cronState.value.valid) return false
  return !(cronState.value.cron === task.cron && enabledFlag.value === task.enabled)
})

/* ============ 提交调度变更工单（校验 + 值变化检查兜底） ============ */
async function submitEditTicket() {
  const task = editingTask.value
  if (!task) return
  if (!cronState.value.valid) {
    ElMessage.warning('cron 表达式不合法，请检查各字段取值范围')
    return
  }
  const reason = reasonText.value.trim()
  if (!reason) {
    ElMessage.warning('请填写变更原因')
    return
  }
  // 值未变化时不创建工单（按钮已禁用，此处兜底防止绕过 UI）
  if (!canSubmit.value) {
    ElMessage.warning('调度时间与启停状态均未变化，无需提交工单')
    return
  }
  try {
    await api.postJson('/api/v1/system/tickets/', {
      ticket_type: 'schedule',
      config_key: task.key,
      new_value: JSON.stringify({ cron: cronState.value.cron, enabled: enabledFlag.value }),
      reason,
    })
    editVisible.value = false
    ElMessage.success('工单已提交，等待审批（高风险需复核）')
    await loadTasks()
  } catch (e) {
    ElMessage.error('提交失败：' + errMsg(e, '未知错误'))
  }
}

/* ==========================================================
   cron 校验与中文解释（与后端 scheduler_registry 语义一致）
   ========================================================== */
const WEEKDAY_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

// 校验 cron 单段字段：支持 *、固定值、区间(a-b)、步长（斜杠前缀）及逗号组合
function validateCronField(value, lo, hi) {
  if (!value) return true // 空值按 * 处理
  for (const part of value.split(',')) {
    const p = part.trim()
    if (!p) return false
    if (/[^0-9*,\-/]/.test(p)) return false // 非法字符
    let base = p
    if (p.includes('/')) {
      const seg = p.split('/')
      if (seg.length !== 2 || !/^\d+$/.test(seg[1]) || parseInt(seg[1], 10) < 1) return false
      base = seg[0]
    }
    if (base === '*') continue
    if (base.includes('-')) {
      const seg = base.split('-')
      if (seg.length !== 2) return false
      const a = parseInt(seg[0], 10), b = parseInt(seg[1], 10)
      if (isNaN(a) || isNaN(b)) return false
      if (!(lo <= a && a <= b && b <= hi)) return false
    } else {
      if (!/^\d+$/.test(base)) return false
      const v = parseInt(base, 10)
      if (isNaN(v) || v < lo || v > hi) return false
    }
  }
  return true
}

// cron 中文解释：把 5 段 cron 翻译成人话；无法归类的复杂表达式原样返回
function isFixedField(v) { return /^\d+$/.test(v) }
function isStepField(v) { return v.includes('/') }
function stepValue(v) { return parseInt(v.split('/')[1], 10) }
function fmtHHMM(hour, minute) {
  return String(parseInt(hour, 10)).padStart(2, '0') + ':' + String(parseInt(minute, 10)).padStart(2, '0')
}
function fmtWeekdays(dow) {
  // 周字段 → 中文星期列表（支持固定值/区间/逗号列表）
  const names = []
  for (const item of dow.split(',')) {
    const p = item.trim()
    if (p.includes('-')) {
      const [a, b] = p.split('-')
      for (let i = parseInt(a, 10); i <= parseInt(b, 10); i++) names.push(WEEKDAY_NAMES[i % 7])
    } else if (p !== '*') {
      names.push(WEEKDAY_NAMES[parseInt(p, 10) % 7])
    }
  }
  return names.length ? names.join('、') : dow
}
function humanizeCron(cron) {
  const fields = String(cron || '').trim().split(/\s+/)
  if (fields.length !== 5) return String(cron || '')
  const [minute, hour, dom, month, dow] = fields
  // 每 N 分钟：*/N * * * *
  if (isStepField(minute) && hour === '*' && dom === '*' && month === '*' && dow === '*')
    return `每 ${stepValue(minute)} 分钟执行一次`
  // 每天 H 点内每 N 分钟：*/N H * * *
  if (isStepField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow === '*')
    return `每天 ${String(parseInt(hour, 10)).padStart(2, '0')} 点内每 ${stepValue(minute)} 分钟执行一次`
  // 每周 X 点内每 N 分钟：*/N H * * DOW
  if (isStepField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow !== '*')
    return `每周${fmtWeekdays(dow)} ${String(parseInt(hour, 10)).padStart(2, '0')} 点内每 ${stepValue(minute)} 分钟执行一次`
  // 每 N 小时（整点）：0 */N * * *
  if (minute === '0' && isStepField(hour) && dom === '*' && month === '*' && dow === '*')
    return `每 ${stepValue(hour)} 小时执行一次`
  // 每 N 小时的第 M 分钟：M */N * * *
  if (isFixedField(minute) && isStepField(hour) && dom === '*' && month === '*' && dow === '*')
    return `每 ${stepValue(hour)} 小时的第 ${parseInt(minute, 10)} 分钟执行一次`
  // 每小时的第 M 分钟：M * * * *
  if (isFixedField(minute) && hour === '*' && dom === '*' && month === '*' && dow === '*')
    return `每小时的第 ${parseInt(minute, 10)} 分钟执行一次`
  // 每天固定时间：M H * * *
  if (isFixedField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow === '*')
    return `每天 ${fmtHHMM(hour, minute)} 执行一次`
  // 每周：M H * * DOW
  if (isFixedField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow !== '*')
    return `每周${fmtWeekdays(dow)} ${fmtHHMM(hour, minute)} 执行一次`
  // 每月：M H D * *
  if (isFixedField(minute) && isFixedField(hour) && isFixedField(dom) && month === '*' && dow === '*')
    return `每月 ${parseInt(dom, 10)} 日 ${fmtHHMM(hour, minute)} 执行一次`
  // 每年：M H D MO *
  if (isFixedField(minute) && isFixedField(hour) && isFixedField(dom) && isFixedField(month) && dow === '*')
    return `每年 ${parseInt(month, 10)} 月 ${parseInt(dom, 10)} 日 ${fmtHHMM(hour, minute)} 执行一次`
  // 每年固定日期 + 星期限定：M H D MO DOW（如 "0 2 1 1 1" → 每年 1 月 1 日且为周一）
  if (isFixedField(minute) && isFixedField(hour) && isFixedField(dom) && isFixedField(month) && isFixedField(dow))
    return `每年 ${parseInt(month, 10)} 月 ${parseInt(dom, 10)} 日 且为${fmtWeekdays(dow)} ${fmtHHMM(hour, minute)} 执行一次`
  // 兜底：保留原始 cron，避免复杂表达式被错误简化
  return `cron 表达式：${cron}`
}

/* ==========================================================
   忙闲视图：Outlook 风格日程（周视图 / 日视图）
   展示顺序：周一~周日；cron 周字段 0=周日，映射为数组下标对应 BUSY_CRON_DAY_ORDER
   ========================================================== */
const BUSY_DAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const BUSY_CRON_DAY_ORDER = [1, 2, 3, 4, 5, 6, 0]
const BUSY_COLORS = ['#4f46e5', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#6366f1']

const busyView = ref('week') // 当前视图（week/day）
const busyDayIndex = ref(0)  // 日视图选中的星期（0=周一）

function pad2(n) { return String(n).padStart(2, '0') }
function fmtMin(m) {
  return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0')
}

// 把 cron 周字段解析为"周内星期值列表"（0=周日）
function parseCronDowList(value) {
  const days = []
  for (const item of String(value).split(',')) {
    const p = item.trim()
    if (p === '*') continue
    if (p.includes('-')) {
      const [a, b] = p.split('-')
      for (let i = parseInt(a, 10); i <= parseInt(b, 10); i++) days.push(i % 7)
    } else {
      days.push(parseInt(p, 10) % 7)
    }
  }
  return days
}

/* ============ 计算单个任务的忙碌信息 ============
 * TODO: 预估工时后续基于近一周/一个月实际执行耗时均值 + 10% 余量动态估算，
 *       替代当前静态的 estimated_minutes（当前仅作展示用估算）。
 * Returns: { days, startMin, endMin } 或 null（不纳入视图）
 * 不纳入视图的任务：
 *   - 步长/每小时类任务（如每 5 分钟、每小时、每 2 小时）近似全天运行，
 *     纳入会让所有时段都变忙、失去错峰参考意义，故直接排除
 *   - 每月/每年固定日期任务无法确定落在周内哪天
 */
function computeTaskBusy(task) {
  const durMin = Math.ceil((task.estimated_minutes || 0) * 1.2) // 预估工时 + 20% 缓冲
  if (!durMin) return null
  const f = task.cron_fields || {}
  // 步长（*/N）或每小时执行（15 * * * *）类任务全天运行，不纳入视图
  if (isStepField(f.minute) || isStepField(f.hour) || f.hour === '*') return null
  if (!isFixedField(f.minute) || !isFixedField(f.hour)) return null
  const startMin = parseInt(f.hour, 10) * 60 + parseInt(f.minute, 10)
  const endMin = Math.min(24 * 60, startMin + durMin)
  let days
  if (f.day_of_week !== '*') {
    days = parseCronDowList(f.day_of_week)
  } else if (f.day_of_month !== '*' || f.month !== '*') {
    return null // 每月/每年固定日期：无法确定落在周内哪天
  } else {
    days = [0, 1, 2, 3, 4, 5, 6] // 每天执行
  }
  return { days, startMin, endMin }
}

// 任务固定配色（按 name 哈希取色，同一任务始终同色）
function taskColor(name) {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0
  return BUSY_COLORS[h % BUSY_COLORS.length]
}

/* ============ 排布一天内的任务块（智能重叠处理，类似 Outlook 日程） ============
 * 输入需按 startMin 升序。
 * 使用区间图着色算法：将时间上有重叠的任务组成独立的"冲突组"，
 * 每个组内根据最大重叠数计算列数，同组任务按泳道错开排列，
 * 不同组的任务互不影响，可以各自占据整列宽度。
 */
function layoutDayBlocks(blocks) {
  if (blocks.length === 0) return []

  // 第 1 步：将任务划分为独立的冲突组
  // 定义：如果两个任务时间重叠，则它们属于同一组
  const groups = []
  const visited = new Set()

  for (let i = 0; i < blocks.length; i++) {
    if (visited.has(i)) continue
    // BFS 找到所有与当前任务（直接或间接）重叠的任务
    const group = [i]
    visited.add(i)
    let queue = [i]
    while (queue.length > 0) {
      const curr = queue.shift()
      for (let j = 0; j < blocks.length; j++) {
        if (visited.has(j)) continue
        // 检查是否与当前组任务重叠
        if (blocks[curr].startMin < blocks[j].endMin && blocks[j].startMin < blocks[curr].endMin) {
          visited.add(j)
          group.push(j)
          queue.push(j)
        }
      }
    }
    groups.push(group.sort((a, b) => blocks[a].startMin - blocks[b].startMin))
  }

  // 第 2 步：对每个组进行列分配
  const result = []
  for (const group of groups) {
    const groupBlocks = group.map(i => blocks[i])

    // 计算组内任意时刻的最大重叠数（即所需列数），使用 sweep-line 算法
    const events = []
    for (const b of groupBlocks) {
      events.push({ time: b.startMin, type: 'start', idx: groupBlocks.indexOf(b) })
      events.push({ time: b.endMin, type: 'end', idx: groupBlocks.indexOf(b) })
    }
    events.sort((a, b) => a.time - b.time || (a.type === 'end' ? -1 : 1))

    let maxOverlap = 0
    let currentOverlap = 0
    const activeSet = new Set()
    for (const e of events) {
      if (e.type === 'start') {
        activeSet.add(e.idx)
        currentOverlap = activeSet.size
        maxOverlap = Math.max(maxOverlap, currentOverlap)
      } else {
        activeSet.delete(e.idx)
      }
    }

    // 如果最大重叠为 1，所有任务各占整列
    if (maxOverlap <= 1) {
      for (const idx of group) {
        result.push({ b: blocks[idx], lane: 0, width: 100, left: 0 })
      }
      continue
    }

    // 最大重叠 > 1，需要分配泳道
    const laneCount = maxOverlap
    const width = 100 / laneCount

    // 贪心算法分配泳道：按开始时间排序，每个任务分配第一个可用的泳道
    const laneEndTimes = new Array(laneCount).fill(0)
    const sortedGroup = group.map(i => ({ idx: i, block: blocks[i] }))
      .sort((a, b) => a.block.startMin - b.block.startMin)

    for (const { idx, block } of sortedGroup) {
      // 找到第一个可用的泳道
      let lane = 0
      while (lane < laneCount && laneEndTimes[lane] > block.startMin) {
        lane++
      }
      if (lane >= laneCount) lane = 0 // 理论上不会发生

      laneEndTimes[lane] = block.endMin

      result.push({ b: blocks[idx], lane, width, left: lane * width })
    }
  }

  // 保持与输入 blocks 相同的顺序
  return result.sort((a, b) => {
    const ai = blocks.indexOf(a.b)
    const bi = blocks.indexOf(b.b)
    return ai - bi
  })
}

/* ============ 周视图/日视图渲染数据（响应式 computed） ============ */
const pct = m => (m / (24 * 60)) * 100

// 周视图：每个星期列一天内的任务块（已排布泳道）
const weekDayBlocks = computed(() => {
  const dayBlocks = [[], [], [], [], [], [], []]
  tasks.value.forEach(t => {
    const b = computeTaskBusy(t)
    if (!b) return
    for (const d of b.days) dayBlocks[d].push({ t, startMin: b.startMin, endMin: b.endMin })
  })
  return dayBlocks.map(list => layoutDayBlocks(list.sort((a, b) => a.startMin - b.startMin)))
})

// 日视图：选中星期的任务块（已排布泳道）
const dayLayout = computed(() => {
  const dow = BUSY_CRON_DAY_ORDER[busyDayIndex.value] // 当前选中星期的 cron 周值
  const blocks = []
  tasks.value.forEach(t => {
    const b = computeTaskBusy(t)
    if (!b) return
    if (b.days.includes(dow)) blocks.push({ t, startMin: b.startMin, endMin: b.endMin })
  })
  blocks.sort((a, b) => a.startMin - b.startMin)
  return layoutDayBlocks(blocks)
})

// 周视图星期列的绝对定位（避开表头行，与时间轴等宽等比切分）
function dayBodyStyle(di) {
  return {
    left: `calc(56px + (100% - 56px) * ${di} / 7)`,
    width: `calc((100% - 56px) / 7)`,
  }
}

// 任务块样式：按起止分钟百分比定位 + 任务固定配色
function blockStyle(p) {
  const durMin = p.b.endMin - p.b.startMin
  return {
    top: pct(p.b.startMin) + '%',
    height: Math.max(0.5, pct(durMin)) + '%',
    left: p.left + '%',
    width: p.width + '%',
    background: taskColor(p.b.t.name),
  }
}

// 任务块悬停提示：标签 + 时间段 + 时长
function blockTitle(b) {
  const durMin = b.endMin - b.startMin
  return `${b.t.label}（${fmtMin(b.startMin)} - ${fmtMin(b.endMin)}，${durMin} 分钟）`
}

// 高度阈值：≥ 3%（约 43 分钟）显示名称+时间两行，否则只显示名称
function blockShowDuration(b) {
  return pct(b.endMin - b.startMin) >= 3
}

/* ============ 页面初始化 ============ */
onMounted(async () => {
  userStore.restore()
  if (!userStore.isSystemMaintainer) return
  await loadTasks()
})
</script>

<style scoped>

/* el-tabs 三件套（撑满 + 面板内部滚动 + pane flex 列）由全局 .tabs-fill 提供 */

/* ===== 任务调度 sheet ===== */
.task-sheet {
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  height: 100%; /* 撑满 tab 面板剩余高度，任务列表内部滚动 */
  min-height: 0;
}

.task-count-sub {
  font-weight: 400;
  margin-left: 8px;
}

.task-list {
  flex: 1;
  min-height: 0; /* 由 flex 撑满卡片剩余高度，任务较多时面板内滚动 */
  overflow-y: auto;
}

.task-empty {
  padding: 60px 0;
  text-align: center;
  color: var(--app-text-sub);
}

/* ===== 任务卡片 ===== */
.task-item {
  padding: 14px 18px;
  border-bottom: 1px solid var(--app-border);
  transition: background 0.15s;
}

.task-item:hover {
  background: var(--app-menu-hover);
}

.task-item:last-child {
  border-bottom: none;
}

.task-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.task-item-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
  line-height: 1.4;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.task-item-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
  align-items: center;
}

.task-item-desc {
  font-size: 12px;
  color: var(--app-text-sub);
  line-height: 1.5;
  margin-top: 6px;
  max-width: 720px;
}

.task-item-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  flex-wrap: wrap;
}

/* cron 分字段展示：每段一个灰底小框 */
.task-cron {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}

.cron-field {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-size: 12px;
  font-family: 'SF Mono', 'Consolas', monospace;
  background: var(--app-menu-hover);
  padding: 2px 8px;
  border-radius: 4px;
  color: var(--app-text);
}

.cron-field em {
  font-style: normal;
  font-size: 10px;
  color: var(--app-text-sub);
  font-family: inherit;
}

/* cron 中文解释（人性化）：紧随分字段框后展示，与描述区解耦 */
.task-humanized {
  font-size: 12px;
  color: #4f46e5;
  background: #eef2ff;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 500;
}

/* 启用状态徽标 */
.task-status {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 500;
  white-space: nowrap;
}

.task-status-on {
  background: #dcfce7;
  color: #166534;
}

.task-status-off {
  background: var(--app-menu-hover);
  color: var(--app-text-sub);
}

/* 待审批工单 badge */
.task-pending-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: #dbeafe;
  color: #1e40af;
  cursor: pointer;
  font-weight: 500;
  white-space: nowrap;
  transition: background 0.15s;
}

.task-pending-badge:hover {
  background: #bfdbfe;
}

/* 调度键（灰色小字，mono 字体） */
.task-item-key {
  font-size: 10px;
  color: var(--app-text-sub);
  font-family: 'SF Mono', 'Consolas', monospace;
  margin-top: 6px;
  opacity: 0.6;
}

/* ===== 高风险 badge ===== */
.config-badge {
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 4px;
  font-weight: 400;
  line-height: 1.5;
  white-space: nowrap;
}

.config-badge-risk {
  background: #fff3e0;
  color: #e65100;
  border: 1px solid #ffcc80;
}

/* ===== 忙闲视图 ===== */
.busy-sheet {
  display: flex;
  flex-direction: column;
  padding: 14px 16px;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  height: 100%; /* 撑满 tab 面板剩余高度，日历超出部分内部滚动 */
  overflow: auto;
}

.busy-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  flex-shrink: 0;
}

/* 日历骨架：左侧时间轴 + 7 天列（日视图为单列）。
   15 分钟一个格线（每小时 4 行共 96 行），行高 32px；
   position:relative 作为 .cal-day-body 绝对定位的包含块；
   .cal-day-body 不参与 grid 自动放置，避免与时间标签/格线争抢单元格。 */

/* 周视图：表头与格线区拆分为上下两块（表头固定，格线区内部纵向滚动） */
.cal-week {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--app-card-bg);
}

/* 固定表头：与格线区相同的列宽定义，滚动时始终停留顶部 */
.cal-week-head {
  display: grid;
  grid-template-columns: 56px repeat(7, 1fr);
  flex-shrink: 0;
  border-bottom: 1px solid var(--app-border);
}

/* 格线区：内部纵向滚动（overflow-y），任务块绝对定位的包含块 */
.cal-week-body {
  display: grid;
  grid-template-columns: 56px repeat(7, 1fr);
  grid-auto-rows: 32px;
  position: relative;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--app-card-bg);
}

/* 日视图：与周视图同构——表头（星期切换）固定 + 时间轴格线区内部纵向滚动 */
.cal-day {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--app-card-bg);
}

/* 表头行（含星期切换）：时/日角格固定 56px，选中星期占满剩余宽度、其余固定宽度 */
.cal-day-head-row {
  display: flex;
  flex-shrink: 0;
  border-bottom: 1px solid var(--app-border);
}

.cal-day-tab {
  flex: 0 0 64px; /* 未选中星期固定宽度 */
  padding: 5px 0;
  border: none;
  border-right: 1px solid var(--app-border);
  background: var(--app-menu-hover);
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.cal-day-tab:hover {
  background: var(--app-bg);
}

.cal-day-tab.active {
  flex: 1 1 0; /* 选中星期占满剩余空间 */
  color: #fff;
  background: #4f46e5; /* 浅色主题：实心靛蓝，选中状态清晰醒目（纯浅色底对比太弱） */
}

/* 暗色主题下选中星期：实心靛蓝在暗底上过亮发闷，改用半透明靛蓝 + 亮色文字 + 底部高亮条适配暗底 */
html.dark .cal-day-tab.active {
  color: #a5b4fc;
  background: rgba(99, 102, 241, 0.3);
  box-shadow: inset 0 -2px 0 #6366f1;
}

/* 时间轴格线区：内部纵向滚动（overflow-y），任务块绝对定位的包含块 */
.cal-day-scroll {
  display: grid;
  grid-template-columns: 56px 1fr;
  grid-auto-rows: 32px;
  position: relative;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  background: var(--app-card-bg);
}

.cal-corner {
  flex: 0 0 56px; /* 日视图表头行（flex）固定宽；周视图表头为 grid 场景，此值无效由列宽决定 */
  border-right: 1px solid var(--app-border);
  border-bottom: 1px solid var(--app-border);
  background: var(--app-menu-hover);
  font-size: 11px;
  color: var(--app-text-sub);
  display: flex;
  align-items: center;
  justify-content: center;
}

.cal-day-head {
  text-align: center;
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
  padding: 5px 0;
  background: var(--app-menu-hover);
  border-right: 1px solid var(--app-border);
  border-bottom: 1px solid var(--app-border);
}

.cal-time {
  font-size: 10px;
  color: var(--app-text-sub);
  padding: 1px 6px 0 4px;
  text-align: right;
  border-right: 1px solid var(--app-border);
  border-top: 1px solid var(--app-border);
}

.cal-hour-cell {
  border-right: 1px solid var(--app-border);
  border-top: 1px solid var(--app-border);
  background: var(--app-card-bg);
}

/* 15 分钟格线：非整点行用虚线弱化，整点行保持实线 */
.cal-hour-quarter,
.cal-time-quarter {
  border-top-style: dashed;
  border-top-color: var(--app-border);
}

/* 任务块叠加层：绝对定位覆盖在整块时间轴格线上（表头之下），不占 grid 单元格。
   height 显式 = 96 行(24h × 4 格) × 32px：子块 top/height 百分比必须解析到确定高度，
   避免容器高度不确定时退化为 top:auto 全部堆在顶部。
   日视图表头在 grid 首行，故 top:32px 跳过表头；周视图表头已拆到 .cal-week-head，
   格线区从首行起算，top 归零（见下方 .cal-week-body .cal-day-body）。 */
.cal-day-body {
  position: absolute;
  top: 32px; /* 日视图表头行高 */
  height: calc(32px * 96); /* 时间轴格线总高 */
  z-index: 1;
}

/* 周视图：表头独立于格线区，任务块从格线区首行起算 */
.cal-week-body .cal-day-body {
  top: 0;
}

/* 日视图：表头已拆到 .cal-day-head-row，任务块从格线区首行起算 */
.cal-day-scroll .cal-day-body {
  top: 0;
  left: 56px;
  width: calc(100% - 56px);
}

.cal-block {
  position: absolute;
  border-radius: 4px;
  padding: 3px 4px;
  font-size: 11px;
  color: #fff;
  line-height: 1.3;
  overflow: hidden;
  box-sizing: border-box;
  border-left: 3px solid rgba(0, 0, 0, 0.15);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  cursor: default;
}

/* 任务名：加粗显示，居中 */
.cal-block .cal-block-name {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

/* 预计时间：在任务名下方显示 */
.cal-block .cal-block-duration {
  font-size: 10px;
  opacity: 0.85;
  margin-top: 2px;
}

/* 当日无任务提示 */
.busy-day-empty {
  padding: 12px 16px;
  color: var(--app-text-sub);
  font-size: 13px;
}

/* ===== 编辑弹窗：cron 字段表单 ===== */
.form-label {
  font-size: 13px;
  color: var(--app-text);
  margin-bottom: 6px;
}

.form-label .required {
  color: #e5484d;
}

.cron-form-block {
  margin-top: 14px;
}

.cron-form-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.cron-form-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-width: 76px;
}

.cron-form-label {
  font-size: 12px;
  color: var(--app-text);
  display: flex;
  align-items: center;
  gap: 4px;
}

.cron-form-range {
  font-size: 10px;
  color: var(--app-text-sub);
  font-weight: 400;
}

.cron-preview {
  margin-top: 10px;
  font-size: 12px;
  color: var(--app-text-sub);
}

.cron-preview code {
  background: var(--app-menu-hover);
  padding: 2px 8px;
  border-radius: 4px;
  font-family: 'SF Mono', 'Consolas', monospace;
  font-size: 12px;
  color: var(--app-text);
}

/* 编辑弹窗 cron 表达式解释行 */
.cron-explain {
  margin-top: 6px;
  font-size: 13px;
  color: #4f46e5;
  font-weight: 500;
}

.cron-explain-error {
  color: #dc2626;
}

.cron-enabled-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.cron-enabled-row .form-label {
  margin-bottom: 0;
}

.cron-enabled-hint {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-left: 4px;
}

/* 滚动条美化 */
.card-scroll::-webkit-scrollbar {
  width: 6px;
}

.card-scroll::-webkit-scrollbar-track {
  background: transparent;
}

.card-scroll::-webkit-scrollbar-thumb {
  background: var(--app-border);
  border-radius: 3px;
}

.card-scroll::-webkit-scrollbar-thumb:hover {
  background: var(--app-text-sub);
}
</style>
