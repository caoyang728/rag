<template>
  <div class="page-container admin-tasks-page">
    <!-- 无权限：仅超级管理员 / 维护管理员可访问（与系统配置/定时任务页对齐） -->
    <PageGuard :allowed="userStore.isSystemMaintainer" message="仅超级管理员或维护管理员可访问此页面">
      <!-- ===== 页头 ===== -->
      <div class="page-header">
        <div>
          <div class="page-title">任务看板</div>
          <div class="page-desc">Celery 任务执行状态与失败重试（每 30 秒自动刷新）</div>
        </div>
        <el-button size="small" @click="refreshAll">🔄 刷新</el-button>
      </div>

      <!-- ===== 内容区：统计/队列固定在上部，任务列表卡片占满剩余空间、表格内部滚动 ===== -->
      <div class="page-body">
        <!-- ===== 状态统计卡片 ===== -->
        <div class="stats-grid">
          <div class="stat-card">
            <div class="stat-num stat-success">{{ stats.success }}</div>
            <div class="stat-label">成功</div>
          </div>
          <div class="stat-card">
            <div class="stat-num stat-failure">{{ stats.failure }}</div>
            <div class="stat-label">失败</div>
          </div>
          <div class="stat-card">
            <div class="stat-num stat-running">{{ stats.running }}</div>
            <div class="stat-label">运行中</div>
          </div>
          <div class="stat-card">
            <div class="stat-num">{{ stats.total }}</div>
            <div class="stat-label">任务总数</div>
          </div>
          <div class="stat-card">
            <div class="stat-num">{{ formatDuration(stats.avgDurationMs) }}</div>
            <div class="stat-label">平均耗时 (ms)</div>
          </div>
          <div class="stat-card">
            <div class="stat-num">{{ formatDuration(stats.maxDurationMs) }}</div>
            <div class="stat-label">最慢耗时 (ms)</div>
          </div>
        </div>

        <!-- ===== 队列深度 ===== -->
        <div class="card queue-card">
          <div class="queue-card-title">队列深度 <span class="text-sub text-sm">（Redis 实时等待任务数，每分钟由队列监控任务更新）</span></div>
          <el-empty v-if="!queueList.length" description="队列监控暂不可用（Redis 或监控任务未就绪）" :image-size="50" />
          <div v-else class="queue-depth">
            <div v-for="q in queueList" :key="q.name" class="queue-item">
              <div class="queue-head">
                <span class="queue-name" :title="q.name">{{ q.name }}</span>
                <span class="queue-size">{{ q.size }}</span>
              </div>
              <!-- 水位条按 50 为满刻度归一化，超出则 100%（便于一眼看出堆积） -->
              <el-progress :percentage="q.pct" :color="q.color" :show-text="false" :stroke-width="8" />
            </div>
          </div>
        </div>

        <!-- ===== 任务日志列表 ===== -->
        <div class="card task-list-card">
          <PanelHeader wrap>
            任务执行日志 <span class="text-sub text-sm task-count-sub">（共 {{ total }} 条）</span>
            <template #actions>
              <el-select v-model="filterStatus" placeholder="全部状态" clearable style="width: 160px" @change="resetPageLoad">
                <el-option v-for="opt in STATUS_FILTERS" :key="opt.value" :label="opt.text" :value="opt.value" />
              </el-select>
              <el-input v-model="filterTaskName" placeholder="任务名模糊搜索" clearable style="width: 200px" @keyup.enter="resetPageLoad" @clear="resetPageLoad" />
              <el-button size="small" @click="resetPageLoad">筛选</el-button>
            </template>
          </PanelHeader>
          <el-table :data="tasks" v-loading="listLoading" class="task-table">
            <!-- 任务名通常为模块路径较长，行内截断、悬停显示全名 -->
            <el-table-column label="任务" min-width="220" show-overflow-tooltip>
              <template #default="{ row }"><span class="task-name-cell" :title="row.task_name">{{ row.task_name }}</span></template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.status)" size="small" effect="plain">{{ statusText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="队列" width="90">
              <template #default="{ row }"><span class="text-sub">{{ row.queue || '—' }}</span></template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
            </el-table-column>
            <el-table-column label="开始时间" width="150">
              <template #default="{ row }">{{ formatDate(row.started_at || row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="重试" width="60" prop="retry_count" />
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openDetail(row.task_id)">详情</el-button>
                <!-- 失败任务可一键重试：重新派发不覆盖原记录 -->
                <el-button v-if="row.status === 'failure'" link type="danger" size="small" @click="retryTask(row.task_id)">重试</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无任务日志" :image-size="60" />
            </template>
          </el-table>
          <!-- 分页：后端按 page_size 切片；切换每页条数时重置回第 1 页 -->
          <AppPagination
            class="task-pagination"
            layout="total, sizes, prev, pager, next"
            :total="total"
            :page-size="pageSize"
            :page="page"
            :page-sizes="[20, 50, 100]"
            @page-change="onPageChange"
            @size-change="onPageSizeChange"
          />
        </div>
      </div>
    </PageGuard>

    <!-- ===== 任务详情弹窗 ===== -->
    <el-dialog v-model="detailVisible" title="任务详情" width="640px" top="6vh" :close-on-click-modal="false">
      <div v-if="detailRow" class="task-detail">
        <div class="detail-grid">
          <div class="detail-item">
            <div class="detail-label">任务名</div>
            <div class="detail-value">{{ detailRow.task_name }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">task_id</div>
            <div class="detail-value mono">{{ detailRow.task_id }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">状态</div>
            <div class="detail-value">
              <el-tag :type="statusTagType(detailRow.status)" size="small" effect="plain">{{ statusText(detailRow.status) }}</el-tag>
            </div>
          </div>
          <div class="detail-item">
            <div class="detail-label">队列</div>
            <div class="detail-value">{{ detailRow.queue }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">耗时</div>
            <div class="detail-value">{{ formatDuration(detailRow.duration_ms) }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">重试次数</div>
            <div class="detail-value">{{ detailRow.retry_count || 0 }}</div>
          </div>
        </div>
        <div class="detail-block">
          <div class="detail-label">开始时间</div>
          <div class="detail-value">{{ formatDate(detailRow.started_at) }}</div>
        </div>
        <div class="detail-block">
          <div class="detail-label">结束时间</div>
          <div class="detail-value">{{ formatDate(detailRow.finished_at) }}</div>
        </div>
        <div class="detail-block">
          <div class="detail-label">参数 args</div>
          <pre class="detail-pre">{{ safeJson(detailRow.args) }}</pre>
        </div>
        <div class="detail-block">
          <div class="detail-label">参数 kwargs</div>
          <pre class="detail-pre">{{ safeJson(detailRow.kwargs) }}</pre>
        </div>
        <div class="detail-block">
          <div class="detail-label">执行结果 result</div>
          <pre class="detail-pre">{{ detailRow.result || '-' }}</pre>
        </div>
        <div v-if="detailRow.status === 'failure'" class="detail-block">
          <div class="detail-label detail-error-label">错误信息</div>
          <pre class="detail-pre detail-error">{{ detailRow.error_message || '-' }}</pre>
        </div>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <!-- 详情弹窗的"重试"按钮仅失败任务可用 -->
        <el-button v-if="detailRow && detailRow.status === 'failure'" type="danger" @click="retryFromDetail">重试此任务</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, formatDuration, errMsg, safeJson } from '../utils/format'
import { makeStatusMeta } from '../utils/labels'
import { usePagination } from '../composables/usePagination'
import { useConfirm } from '../composables/useConfirm'
import PanelHeader from '../components/base/PanelHeader.vue'
import PageGuard from '../components/base/PageGuard.vue'
import AppPagination from '../components/base/AppPagination.vue'

const userStore = useUserStore()
// 二次确认弹窗统一封装
const { confirm } = useConfirm()

// 自动刷新间隔（毫秒）
const REFRESH_INTERVAL = 30000

// 状态展示配置：文案 + 标签类型（拆成扁平 MAP 后由共享 makeStatusMeta 生成函数对，与 STATUS_FILTERS 下拉口径一致）
const STATUS_LABEL_MAP = {
  success: '成功', failure: '失败', started: '运行中',
  pending: '待执行', retry: '重试中', revoked: '已撤销',
}
const STATUS_TAG_MAP = {
  success: 'success', failure: 'danger', started: 'primary',
  pending: 'info', retry: 'warning', revoked: 'info',
}

// 状态筛选选项（与旧 HTML 下拉一致）
const STATUS_FILTERS = [
  { value: 'pending', text: 'pending 待执行' },
  { value: 'started', text: 'started 运行中' },
  { value: 'success', text: 'success 成功' },
  { value: 'failure', text: 'failure 失败' },
  { value: 'retry', text: 'retry 重试中' },
  { value: 'revoked', text: 'revoked 已撤销' },
]

/* ==========================================================
   状态
   ========================================================== */
// 分页状态：由 usePagination 统一管理翻页/改每页条数后的重新加载
const { page, pageSize, onPageChange, onPageSizeChange, reset, guardOverflow } = usePagination(loadTasks, { initialSize: 50 })
const total = ref(0)
// 筛选
const filterStatus = ref('')
const filterTaskName = ref('')
// 列表与加载
const tasks = ref([])
const listLoading = ref(false)
// 详情弹窗当前任务（供"重试此任务"按钮使用）
const detailVisible = ref(false)
const detailRow = ref(null)
// 统计卡片（- 表示尚未加载）
const stats = reactive({ success: '-', failure: '-', running: '-', total: '-', avgDurationMs: 0, maxDurationMs: 0 })
// 队列深度 [{name, size, pct, color}]
const queueList = ref([])
// 请求序号：防止快速筛选/翻页时旧响应后返回覆盖新状态
let loadSeq = 0
let refreshTimer = null

/* ============ 加载统计 + 队列深度 ============ */
async function loadStats() {
  try {
    const data = await api.getJson('/api/v1/system/tasks/stats/')
    const counts = data.counts || {}
    stats.success = counts.success ?? 0
    stats.failure = counts.failure ?? 0
    // 运行中 = started + pending（已派发但尚未结束的任务）
    stats.running = (counts.started ?? 0) + (counts.pending ?? 0)
    stats.total = data.counts_total || Object.values(counts).reduce((a, b) => a + (b || 0), 0)
    stats.avgDurationMs = data.avg_duration_ms
    stats.maxDurationMs = data.max_duration_ms
    renderQueueDepth(data.queues || {})
  } catch (e) {
    // 统计失败不阻塞列表；静默降级，避免频繁弹错
    console.warn('任务统计加载失败:', e.message)
  }
}

// 渲染队列深度：深度分档着色 <10 正常绿 / <50 偏高黄 / >=50 堆积红
function renderQueueDepth(queues) {
  const names = Object.keys(queues)
  if (!names.length) {
    queueList.value = []
    return
  }
  queueList.value = names.map(name => {
    const q = queues[name] || {}
    const size = Number(q.size) || 0
    // 水位条按 50 为满刻度归一化，超出则 100%（便于一眼看出堆积）
    const pct = Math.min(100, Math.round(size / 50 * 100))
    const color = size >= 50 ? '#e5484d' : size >= 10 ? '#f59e0b' : '#16a34a'
    return { name, size, pct, color }
  })
}

/* ============ 加载任务日志列表 ============ */
async function loadTasks() {
  const seq = ++loadSeq
  listLoading.value = true
  try {
    const params = new URLSearchParams({ page: page.value, page_size: pageSize.value })
    if (filterStatus.value) params.set('status', filterStatus.value)
    const taskName = (filterTaskName.value || '').trim()
    if (taskName) params.set('task_name', taskName)

    const data = await api.getJson('/api/v1/system/tasks/?' + params.toString())
    // 竞态检查：若有更新的请求已发出，丢弃本次结果
    if (seq !== loadSeq) return
    total.value = data.total || 0
    tasks.value = data.items || []

    // 数据量减少（任务被清理）导致当前页越界时，回退到最后一页重新加载
    if (guardOverflow(total.value)) return
  } catch (e) {
    if (seq !== loadSeq) return
    tasks.value = []
    ElMessage.error('加载失败: ' + errMsg(e, '未知错误'))
  } finally {
    if (seq === loadSeq) listLoading.value = false
  }
}

// 任务状态文案/标签色：由共享 makeStatusMeta 生成（未命中状态回退原值 / info）
const { label: statusText, tagType: statusTagType } = makeStatusMeta(STATUS_LABEL_MAP, STATUS_TAG_MAP)

/* ============ 详情弹窗 ============ */
async function openDetail(taskId) {
  try {
    // 详情接口与列表共用（带 task_id 精确过滤，取第一条即为目标记录）
    const data = await api.getJson(`/api/v1/system/tasks/?task_id=${encodeURIComponent(taskId)}&page_size=1`)
    const row = (data.items || [])[0]
    if (!row) {
      ElMessage.error('任务记录不存在')
      return
    }
    detailRow.value = row
    detailVisible.value = true
  } catch (e) {
    ElMessage.error('加载任务详情失败：' + errMsg(e, ''))
  }
}

// 详情弹窗内重试：关闭详情后走统一的 retryTask 流程
function retryFromDetail() {
  if (!detailRow.value) return
  detailVisible.value = false
  retryTask(detailRow.value.task_id)
}

/* ============ 失败任务重试 ============ */
function retryTask(taskId) {
  confirm({
    message: `将以相同参数重新派发该任务（生成新的 task_id，不影响原记录）\n${taskId}`,
    title: '重试任务', confirmText: '确认重试', errorText: '重试失败',
  }, async () => {
    await api.postJson(`/api/v1/system/tasks/${encodeURIComponent(taskId)}/retry/`, {})
    ElMessage.success('已重新派发，新任务执行后自动入库')
    refreshAll()
  })
}

/* ============ 过滤条件变化时重置到第一页再加载 ============ */
function resetPageLoad() {
  reset()
}

/* ============ 全量刷新（统计 + 列表） ============ */
function refreshAll() {
  loadStats()
  loadTasks()
}

/* ============ 页面初始化 / 清理 ============ */
onMounted(() => {
  userStore.restore()
  if (!userStore.isSystemMaintainer) return
  // 首屏并行加载统计与列表，避免先后串行等待
  refreshAll()
  // 自动轮询：页面不可见时跳过，减少后台无谓请求
  refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') refreshAll()
  }, REFRESH_INTERVAL)
})

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
/* ===== 状态统计卡片（page-body 为纵向 flex，固定不压缩） ===== */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
  margin-bottom: 16px;
  flex-shrink: 0;
}

@media (max-width: 1100px) {
  .stats-grid { grid-template-columns: repeat(3, 1fr); }
}

.stat-card {
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 14px 16px;
}

.stat-num {
  font-size: 26px;
  font-weight: 700;
  color: var(--app-text);
  line-height: 1.2;
}

.stat-success { color: #16a34a; }
.stat-failure { color: #e5484d; }
.stat-running { color: #2563eb; }

.stat-label {
  margin-top: 4px;
  font-size: 12px;
  color: var(--app-text-sub);
}

/* ===== 队列深度（固定不压缩，位于统计卡片下方） ===== */
.queue-card {
  margin-bottom: 16px;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 14px 16px;
  flex-shrink: 0;
}

.queue-card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 12px;
}

.queue-depth {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
}

@media (max-width: 900px) {
  .queue-depth { grid-template-columns: repeat(2, 1fr); }
}

.queue-item {
  min-width: 0;
}

.queue-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.queue-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.queue-size {
  font-size: 14px;
  font-weight: 700;
  color: var(--app-text);
  flex-shrink: 0;
  margin-left: 8px;
}

/* ===== 任务日志列表：占满 page-body 剩余空间，内部（表格）纵向滚动 ===== */
.task-list-card {
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  flex: 1;
  min-height: 0;
}

.task-count-sub {
  font-weight: 400;
  margin-left: 8px;
}

/* 表格占满卡片剩余空间；高度被约束后，EP 表格内部 el-scrollbar 自动接管纵向滚动（表头固定、表体滚动） */
.task-table {
  flex: 1;
  min-height: 0;
}

/* 任务名较长：单行截断 + 悬停 title 全名 */
.task-name-cell {
  max-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.task-pagination {
  margin-top: 16px;
  justify-content: flex-end;
  padding: 0 16px 16px;
  flex-shrink: 0;
}

/* ===== 详情弹窗 ===== */
/* .detail-grid/.detail-block/.detail-label/.detail-value 为全局公共类（assets/style.css），此处仅保留页面特有样式 */

.detail-error-label {
  color: #e5484d;
}

.detail-value.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.detail-pre {
  background: var(--app-menu-hover);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow: auto;
  margin: 0;
  color: var(--app-text);
}

.detail-error {
  background: #fdecec;
  border-color: #f5c6c8;
  color: #c0392b;
}
</style>
