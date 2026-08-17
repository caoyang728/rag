<template>
  <div class="page-container ticket-center-page">
    <!-- ===== 页头 ===== -->
    <div class="page-header">
      <div class="page-title">工单中心</div>
      <div class="page-desc">发起配置/调度/模型/权限变更工单，跟踪审批流转进度</div>
    </div>

    <!-- ===== 列表卡片：toolbar 固定 + 列表滚动 ===== -->
    <div class="page-body">
    <div class="app-card ticket-center-card">
      <!-- 筛选工具栏：类型下拉 + 状态 tab + 搜索 + 操作按钮（单行紧凑） -->
      <div class="tc-toolbar">
        <el-select v-model="typeFilter" class="tc-type-select" @change="switchType">
          <el-option label="全部类型" value="all" />
          <el-option label="配置工单" value="config" />
          <el-option label="定时任务" value="schedule" />
          <el-option label="模型工单" value="model" />
        </el-select>

        <div class="tc-filter-tabs">
          <el-radio-group v-model="filterTab" size="small" @change="switchFilterTab">
            <el-radio-button value="todo">待我处理<span v-if="todoBadge > 0" class="tc-todo-count">{{ todoBadge }}</span></el-radio-button>
            <el-radio-button value="approved">已通过</el-radio-button>
            <el-radio-button value="rejected">已驳回</el-radio-button>
            <el-radio-button value="withdrawn">已撤回</el-radio-button>
            <el-radio-button value="all">全部工单</el-radio-button>
            <el-radio-button value="mine">我的工单</el-radio-button>
          </el-radio-group>
        </div>

        <el-input v-model="searchQuery" class="tc-search" placeholder="搜索 ID / 创建人 / 名称" clearable @input="onSearch" @clear="onSearch('')" />
        <el-button type="primary" size="small" @click="openCreateDialog">＋ 发起工单</el-button>
        <el-button size="small" @click="router.push('/ticket')">📋 审批列表</el-button>
      </div>

      <!-- 工单列表滚动区：固定工具栏 + 列表内部滚动 -->
      <div class="page-scroll">
      <!-- 工单列表：浅色卡片 + 固定两行网格布局（标题行 + meta 行） -->
      <div v-loading="loading" class="tc-list">
        <div v-for="t in tickets" :key="t.id" class="tc-item" @click="openDetail(t)">
          <!-- 标题行：类型徽标 + 操作标签 + 名称 + key + 风险 + 状态 -->
          <div class="tc-item-title">
            <el-tag :type="TYPE_TAG_MAP[t.ticket_type] || 'info'" size="small" effect="plain">{{ TYPE_LABEL_MAP[t.ticket_type] || t.ticket_type }}</el-tag>
            <el-tag v-if="t.ticket_type === 'model'" :type="opTagType(t.action)" size="small" effect="plain">{{ opLabel(t) }}</el-tag>
            <el-tag v-else type="primary" size="small" effect="plain">修改</el-tag>
            <span class="tc-item-name">{{ displayName(t) }}</span>
            <span class="tc-item-key">{{ t.config_key }}</span>
            <span v-if="t.risk_level === 'high'" class="tc-item-risk">⚠️ 高风险</span>
            <el-tag :type="statusTagType(t.status)" size="small" class="tc-item-status">{{ statusLabel(t.status) }}</el-tag>
          </div>
          <!-- meta 行：原因（2行截断）+ 操作摘要 + 创建人/时间 -->
          <div class="tc-item-meta">
            <div class="tc-meta-left">
              <div v-if="t.reason" class="tc-meta-reason" :title="t.reason">{{ t.reason }}</div>
              <span class="tc-action-label">{{ actionSummary(t) }}</span>
            </div>
            <div class="tc-meta-info">
              <span>创建人：{{ t.creator || '-' }}</span>
              <span>{{ formatDate(t.created_at) }}</span>
            </div>
          </div>
        </div>
        <el-empty v-if="!loading && tickets.length === 0" :description="emptyText" :image-size="80" />
      </div>
      </div>

      <!-- 分页：后端控制，切换 tab/类型/搜索后回到第 1 页 -->
      <AppPagination
        class="tc-pagination"
        :total="total"
        :page-size="PAGE_SIZE"
        :page="page"
        @page-change="onPageChange"
      />
    </div>
    </div>

    <!-- ===== 工单详情弹窗（统一 840px 二级弹窗，按类型渲染） ===== -->
    <el-dialog v-model="detailVisible" :title="detailTitle" width="840px" top="4vh" :close-on-click-modal="false">
      <div v-if="detailTicket" class="tc-detail-body">
        <!-- 头卡：类型徽标 + 名称 + key + 状态 + 提交信息 -->
        <div class="tc-detail-card">
          <div class="tc-detail-header">
            <el-tag :type="TYPE_TAG_MAP[detailTicket.ticket_type] || 'info'" size="small" effect="plain">{{ TYPE_LABEL_MAP[detailTicket.ticket_type] || detailTicket.ticket_type }}</el-tag>
            <span v-if="detailTicket.ticket_type === 'model'" class="tc-item-key">{{ MODEL_ACTION_LABELS[detailTicket.action] || detailTicket.action }}</span>
            <span class="tc-detail-title">{{ detailName }}</span>
            <span v-if="detailTicket.config_key" class="tc-item-key">{{ detailTicket.config_key }}</span>
            <span v-if="detailTicket.risk_level === 'high'" class="tc-item-risk">⚠️ 高风险</span>
            <el-tag :type="statusTagType(detailTicket.status)" size="small" class="tc-item-status">{{ statusLabel(detailTicket.status) }}</el-tag>
          </div>
          <div class="tc-detail-meta">
            <span v-if="detailTicket.ticket_type === 'model'">模型 ID：{{ detailTicket.model_id ?? '-' }}</span>
            <span>提交人：{{ detailTicket.creator || '-' }}</span>
            <span>提交时间：{{ formatDate(detailTicket.created_at) }}</span>
          </div>
        </div>

        <!-- config / schedule：变更对比（含 cron 中文解释） + 变更摘要 -->
        <div v-if="detailTicket.ticket_type === 'config' || detailTicket.ticket_type === 'schedule'" class="tc-detail-card">
          <div class="tc-diff-label">{{ detailTicket.ticket_type === 'schedule' ? '调度变更' : '变更对比' }}</div>
          <template v-if="detailTicket.ticket_type === 'schedule'">
            <div v-for="(row, i) in scheduleDiffRows" :key="i" class="tc-diff-row">
              <div class="tc-diff-side tc-diff-side-old">
                <div class="tc-diff-side-label">{{ row.oldLabel }}</div>
                <div class="tc-diff-side-value">{{ row.oldValue }}</div>
                <div v-if="row.oldHint" class="tc-diff-side-hint">{{ row.oldHint }}</div>
              </div>
              <div class="tc-diff-arrow">→</div>
              <div class="tc-diff-side tc-diff-side-new">
                <div class="tc-diff-side-label">{{ row.newLabel }}</div>
                <div class="tc-diff-side-value">{{ row.newValue }}</div>
                <div v-if="row.newHint" class="tc-diff-side-hint">{{ row.newHint }}</div>
              </div>
            </div>
          </template>
          <template v-else>
            <div class="tc-diff-row">
              <div class="tc-diff-side tc-diff-side-old">
                <div class="tc-diff-side-label">原值</div>
                <div class="tc-diff-side-value">{{ displayValue(detailTicket.old_value) }}</div>
              </div>
              <div class="tc-diff-arrow">→</div>
              <div class="tc-diff-side tc-diff-side-new">
                <div class="tc-diff-side-label">新值</div>
                <div class="tc-diff-side-value">{{ displayValue(detailTicket.new_value) }}</div>
              </div>
            </div>
          </template>
          <!-- 多值类配置变更摘要：added 绿 / removed 红 -->
          <div v-if="changeSummaryParts.length" class="tc-change-summary">
            <div v-if="changeSummaryParts.added.length" class="tc-change-added">+ 新增：<code v-for="(v, i) in changeSummaryParts.added" :key="i">{{ v }}</code></div>
            <div v-if="changeSummaryParts.removed.length" class="tc-change-removed">- 移除：<code v-for="(v, i) in changeSummaryParts.removed" :key="i">{{ v }}</code></div>
          </div>
        </div>

        <!-- model：删除警示+信息 / 停用警示+状态对照 / 修改字段级 diff -->
        <div v-if="detailTicket.ticket_type === 'model'" class="tc-detail-card">
          <div class="tc-diff-label">{{ modelBodyTitle }}</div>
          <div v-if="detailTicket.action === 'delete'" class="tc-warning tc-warning-danger">
            <div class="tc-warning-icon">🗑️</div>
            <div class="tc-warning-text"><strong>确认删除此模型？</strong>删除后该模型将不可恢复，且引用该模型的配置项将失效。</div>
          </div>
          <div v-else-if="detailTicket.action === 'deactivate'" class="tc-warning tc-warning-warn">
            <div class="tc-warning-icon">⏸️</div>
            <div class="tc-warning-text"><strong>停用后该模型将不可用</strong>，引用该模型的配置项将受影响。</div>
          </div>
          <template v-if="detailTicket.action === 'delete'">
            <!-- 删除：模型当前信息列表（不做 diff） -->
            <div class="tc-info-card">
              <div class="tc-info-title">模型当前信息</div>
              <div v-for="f in modelInfoRows" :key="f.label" class="tc-info-row">
                <span class="tc-info-label">{{ f.label }}</span>
                <span class="tc-info-value">{{ f.value }}</span>
              </div>
            </div>
          </template>
          <template v-else-if="detailTicket.action === 'deactivate'">
            <!-- 停用：当前状态 → 变更后状态对照 -->
            <div class="tc-state-grid">
              <div class="tc-state-item tc-state-old">
                <div class="tc-state-label">当前状态</div>
                <div class="tc-state-value">● 启用中</div>
              </div>
              <div class="tc-state-item tc-state-new">
                <div class="tc-state-label">变更为</div>
                <div class="tc-state-value">● 已停用</div>
              </div>
            </div>
          </template>
          <template v-else>
            <!-- 修改：字段级 diff 对比 -->
            <div v-for="f in detailTicket.changed_fields" :key="f" class="tc-diff-row model-diff-row">
              <div class="tc-diff-side tc-diff-side-old">
                <div class="tc-diff-side-label">{{ modelFieldLabel(f) }} 原值</div>
                <div class="tc-diff-side-value">{{ modelFieldOld(f) }}</div>
              </div>
              <div class="tc-diff-arrow">→</div>
              <div class="tc-diff-side tc-diff-side-new">
                <div class="tc-diff-side-label">{{ modelFieldLabel(f) }} 新值</div>
                <div class="tc-diff-side-value">{{ modelFieldNew(f) }}</div>
              </div>
            </div>
          </template>
          <!-- 依赖引用警示：删除/停用受影响项 -->
          <div v-if="detailTicket.dependency_refs && detailTicket.dependency_refs.length" class="tc-warning tc-warning-danger dep-warning">
            <div class="tc-warning-icon">⚠️</div>
            <div class="tc-warning-text"><strong>依赖引用</strong>{{ detailTicket.dependency_refs.join(', ') }}</div>
          </div>
        </div>

        <!-- 变更原因 -->
        <div v-if="detailTicket.reason" class="tc-detail-card">
          <div class="tc-reason">
            <div class="tc-reason-label">变更原因</div>
            <div class="tc-reason-value">{{ detailTicket.reason }}</div>
          </div>
        </div>

        <!-- 流转进度时间线：提交 → 审核 → 复核 → 生效 / 驳回 / 撤回 -->
        <div class="tc-detail-card">
          <div class="tc-diff-label">流转进度</div>
          <div class="tc-timeline">
            <div v-for="(n, i) in timelineNodes" :key="i" class="tc-tl-item">
              <div class="tc-tl-dot" :class="'tc-tl-dot-' + n.dot"></div>
              <div class="tc-tl-body">
                <div class="tc-tl-head">
                  <span class="tc-tl-title">{{ n.title }}</span>
                  <span v-if="n.actor" class="tc-tl-actor">{{ n.actor }}</span>
                  <span v-if="n.time" class="tc-tl-time">{{ formatDate(n.time) }}</span>
                </div>
                <div class="tc-tl-comment">{{ n.comment || '无备注' }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <div class="tc-detail-footer">
          <template v-if="detailTicket">
            <!-- 创建人可撤回未完成工单 -->
            <el-button v-if="canWithdraw" size="small" @click="onWithdrawClick">↩ 撤回</el-button>
            <div v-if="canApprove || canReview" class="footer-actions">
              <el-button size="small" @click="onRejectClick">✗ 驳回</el-button>
              <el-button type="primary" size="small" @click="onApproveClick">✓ {{ detailTicket.status === 'PENDING' && detailTicket.audited_at ? '复核通过' : '通过' }}</el-button>
            </div>
          </template>
          <el-button size="small" @click="detailVisible = false">关闭</el-button>
        </div>
      </template>
    </el-dialog>

    <!-- ===== 发起工单弹窗：选择类型 → 按类型填写变更内容 → 提交 ===== -->
    <el-dialog v-model="createVisible" title="发起工单" width="640px" :close-on-click-modal="false">
      <el-form ref="createFormRef" :model="createForm" label-position="top">
        <el-form-item label="工单类型">
          <el-radio-group v-model="createForm.ticketType">
            <el-radio-button value="permission">权限变更</el-radio-button>
            <el-radio-button value="config">系统配置</el-radio-button>
            <el-radio-button value="schedule">定时任务</el-radio-button>
            <el-radio-button value="model">模型管理</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <!-- 权限变更：目标用户 + 目标角色 + 授权范围 + 范围ID -->
        <template v-if="createForm.ticketType === 'permission'">
          <el-form-item label="目标用户" prop="targetUserId" :rules="[{ required: true, message: '请选择目标用户', trigger: 'change' }]">
            <el-select v-model="createForm.targetUserId" filterable remote :remote-method="searchUsers" :loading="userSearching" placeholder="输入用户名/姓名搜索" style="width: 100%">
              <el-option v-for="u in userOptions" :key="u.id" :label="(u.real_name || u.username) + (u.email ? `（${u.email}）` : '')" :value="u.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="目标角色" prop="roleKey" :rules="[{ required: true, message: '请选择目标角色', trigger: 'change' }]">
            <el-select v-model="createForm.roleKey" placeholder="选择要授予的角色" style="width: 100%">
              <el-option v-for="r in assignableRoles" :key="r.role_key" :label="r.name" :value="r.role_key" />
            </el-select>
          </el-form-item>
          <div class="form-row">
            <el-form-item label="授权范围" style="flex: 1">
              <el-select v-model="createForm.scopeType" style="width: 100%">
                <el-option label="全局（无范围）" value="NONE" />
                <el-option label="部门" value="DEPT" />
                <el-option label="团队" value="TEAM" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="createForm.scopeType !== 'NONE'" label="范围 ID" style="flex: 1">
              <el-input v-model="createForm.scopeId" placeholder="部门/团队 ID" />
            </el-form-item>
          </div>
        </template>

        <!-- 系统配置：选择配置项 + 新值 -->
        <template v-else-if="createForm.ticketType === 'config'">
          <el-form-item label="配置项" prop="configKey" :rules="[{ required: true, message: '请选择配置项', trigger: 'change' }]">
            <el-select v-model="createForm.configKey" filterable placeholder="选择要变更的配置项" style="width: 100%">
              <el-option v-for="c in allConfigs" :key="c.key" :label="`${c.label}（${c.key}）`" :value="c.key" />
            </el-select>
          </el-form-item>
          <el-form-item label="新值" prop="newValue" :rules="[{ required: true, message: '请输入新值', trigger: 'blur' }]">
            <el-input v-model="createForm.newValue" placeholder="填写变更后的新值" />
          </el-form-item>
        </template>

        <!-- 定时任务：调度任务 + cron + 启停 -->
        <template v-else-if="createForm.ticketType === 'schedule'">
          <el-form-item label="调度任务" prop="configKey" :rules="[{ required: true, message: '请选择调度任务', trigger: 'change' }]">
            <el-select v-model="createForm.configKey" filterable placeholder="选择要变更的调度任务" style="width: 100%">
              <el-option v-for="c in scheduleConfigs" :key="c.key" :label="`${c.label}（${c.key}）`" :value="c.key" />
            </el-select>
          </el-form-item>
          <el-form-item label="Cron 表达式" prop="cron" :rules="[{ required: true, message: '请输入 Cron 表达式', trigger: 'blur' }]">
            <el-input v-model="createForm.cron" placeholder="分 时 日 月 周，如 0 2 * * *" />
          </el-form-item>
          <el-form-item label="启停状态">
            <el-switch v-model="createForm.enabled" active-text="启用" inactive-text="停用" />
          </el-form-item>
        </template>

        <!-- 模型管理：目标模型 + 操作类型 -->
        <template v-else>
          <el-form-item label="目标模型" prop="targetModelId" :rules="[{ required: true, message: '请选择目标模型', trigger: 'change' }]">
            <el-select v-model="createForm.targetModelId" filterable placeholder="选择要变更的模型" style="width: 100%">
              <el-option v-for="m in allModels" :key="m.id" :label="`${m.name || m.model_name}（${m.provider || ''}）`" :value="m.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="操作类型" prop="operation" :rules="[{ required: true, message: '请选择操作类型', trigger: 'change' }]">
            <el-select v-model="createForm.operation" style="width: 100%">
              <el-option label="修改模型" value="update_normal" />
              <el-option label="停用模型" value="deactivate" />
              <el-option label="删除模型" value="delete" />
            </el-select>
          </el-form-item>
        </template>

        <el-form-item label="变更原因" prop="reason" :rules="[{ required: true, message: '请填写变更原因', trigger: 'blur' }]">
          <el-input v-model="createForm.reason" type="textarea" :rows="3" placeholder="请说明本次变更的原因，便于审批人判断" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSaving" @click="submitCreate">提交工单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, errMsg, displayValue } from '../utils/format'
import { debounce } from '../utils/debounce'
import { makeStatusMeta, TICKET_STATUS_LABEL_MAP, TICKET_STATUS_TAG_MAP } from '../utils/labels'
import { usePagination } from '../composables/usePagination'
import { useListLoader } from '../composables/useListLoader'
import AppPagination from '../components/base/AppPagination.vue'

/* ============ 常量与映射 ============ */
const PAGE_SIZE = 10 // 每页条数（后端控制分页）

// 调度类配置 key 前缀（与后端 scheduler_registry.SCHEDULE_KEY_PREFIX 一致）
const SCHEDULE_PREFIX = 'SCHEDULE_'

// 工单类型 → el-tag type / 中文名（config 蓝 / schedule 紫 / model 青，与旧 tc-type-badge 视觉一致）
const TYPE_TAG_MAP = { config: 'primary', schedule: 'warning', model: 'success' }
const TYPE_LABEL_MAP = { config: '配置工单', schedule: '定时任务', model: '模型工单' }

// 工单状态 → el-tag type / 中文名：走 utils/labels 的共享映射（与后端统一主表大写枚举一致）
const { label: statusLabel, tagType: statusTagType } = makeStatusMeta(TICKET_STATUS_LABEL_MAP, TICKET_STATUS_TAG_MAP)

// 模型操作 → el-tag type / 中文名（删除红 / 停用橙 / 修改蓝）
const MODEL_ACTION_LABELS = { update_normal: '修改', update: '修改', deactivate: '停用', delete: '删除' }
const MODEL_ACTION_TAG_MAP = { delete: 'danger', deactivate: 'warning', update_normal: 'primary', update: 'primary' }
// 模型字段中文名（修改 diff 与删除信息列表共用）
const MODEL_FIELD_LABELS = {
  base_url: 'Base URL', timeout: '超时时间', model_name: '模型名称', display_name: '显示名',
  api_key: 'API Key', name: '名称', model_type: '模型类型', is_active: '状态', provider: '服务商',
}

/* ============ 页面状态 ============ */
const router = useRouter()
const userStore = useUserStore()

const filterTab = ref('todo')     // 当前筛选 tab：todo/approved/rejected/withdrawn/all/mine
const typeFilter = ref('all')     // 类型筛选：all/config/schedule/model
const searchQuery = ref('')       // 搜索关键词（匹配 id/创建人/名称/key）
const tickets = ref([])           // 当前页工单
const total = ref(0)
// 列表加载：由 useListLoader 统一管理 loading/请求序号守卫/错误提示
const { loading, load } = useListLoader(fetchTickets, { errorPrefix: '加载工单失败' })
// 分页状态：由 usePagination 统一管理翻页后的重新加载（每页条数固定 10，与 PAGE_SIZE 一致）
const { page, onPageChange, reset, guardOverflow } = usePagination(() => load(), { initialSize: 10 })
const todoBadge = ref(0)          // "待我处理"tab 红色待办计数

// 各 tab 下后端筛选参数：待我处理=待审批（后端已排除本人创建/已审）；
// approved/rejected/withdrawn 按状态；mine=我的工单；all=全量浏览
function tabStatusParam(tab) {
  if (tab === 'todo') return 'PENDING'
  if (tab === 'approved') return 'APPROVED'
  if (tab === 'rejected') return 'REJECTED'
  if (tab === 'withdrawn') return 'CANCELLED'
  return ''
}

/* ============ 加载工单（统一工单 API，后端分页 + 过滤） ============ */
async function fetchTickets() {
  const params = new URLSearchParams({ page: page.value, page_size: PAGE_SIZE })
  const statusParam = tabStatusParam(filterTab.value)
  if (filterTab.value === 'mine') params.set('creator', 'me')
  else if (statusParam) params.set('status', statusParam)
  if (typeFilter.value !== 'all') params.set('ticket_type', typeFilter.value)
  if (searchQuery.value.trim()) params.set('search', searchQuery.value.trim())
  const data = await api.getJson('/api/v1/system/tickets/?' + params.toString())
  tickets.value = data.tickets || []
  total.value = data.total || 0
  // 待办计数：todo tab 时后端 total 即待办总数（已排除本人创建/已审）
  if (filterTab.value === 'todo') todoBadge.value = total.value
  // 数据量减少导致当前页越界时，回退到最后一页重新加载
  if (guardOverflow(total.value)) return
}

// 切换筛选 tab：重置状态筛选与页码后重新加载
function switchFilterTab() {
  reset()
}

// 切换类型筛选下拉
function switchType() {
  reset()
}

// 搜索输入：防抖 500ms 触发后端搜索（避免每次击键都请求，定时器由 utils/debounce 统一管理）
const onSearch = debounce((val) => {
  searchQuery.value = (val || '').trim()
  reset()
}, 500)

const emptyText = computed(() => (searchQuery.value ? '未搜索到相关的工单' : '暂无工单'))

/* ============ 卡片渲染辅助 ============ */
// 卡片名称：config/schedule 用配置名，model 优先 snapshot 名称
function displayName(t) {
  if (t.ticket_type === 'model') {
    const snap = t.snapshot_data || {}
    return snap.name || t.model_name || snap.model_name || '-'
  }
  return t.config_label || t.config_key || '-'
}

// 操作摘要：配置恒为"修改了 xxx 参数"；定时任务按 cron/启停差异；模型按操作类型
function actionSummary(t) {
  if (t.ticket_type === 'model') return modelActionSummary(t)
  if (t.ticket_type === 'schedule') return scheduleActionSummary(t)
  return '修改了 ' + (t.config_label || t.config_key) + ' 参数'
}

// 定时任务操作摘要：cron 变更 → "修改了定时任务执行时间"；启停变更 → "修改了启停状态"；两者皆改则逗号拼接
function scheduleActionSummary(t) {
  const oldP = parseScheduleValue(t.old_value)
  const newP = parseScheduleValue(t.new_value)
  if (!oldP || !newP) return ''
  const parts = []
  if (oldP.cron !== newP.cron) parts.push('定时任务执行时间')
  if (oldP.enabled !== newP.enabled) parts.push('启停状态')
  if (!parts.length) return ''
  return parts.length === 1 ? '修改了' + parts[0] : '修改了' + parts[0] + '、' + parts[1]
}

// 模型工单操作摘要：停用/删除按操作类型；修改按字段列表（多字段收起为"N个参数"）
function modelActionSummary(t) {
  const action = t.action || ''
  const snap = t.snapshot_data || {}
  const name = snap.name || t.model_name || snap.model_name || '-'
  if (action === 'deactivate') return '停用了 ' + name
  if (action === 'delete') return '删除了 ' + name
  if (t.changed_fields && t.changed_fields.length) {
    const fields = t.changed_fields.map(f => MODEL_CHANGE_FIELD_LABELS[f] || f)
    if (fields.length === 1) return '修改了' + fields[0]
    return '修改了' + fields[0] + '等' + fields.length + '个参数'
  }
  return '修改了模型配置'
}

// schedule 的 old/new_value 存的是 JSON 字符串 {cron, enabled}，解析失败返回 null
function parseScheduleValue(value) {
  try {
    const data = typeof value === 'string' ? JSON.parse(value) : value
    return { cron: data.cron, enabled: !!data.enabled }
  } catch (e) {
    return null
  }
}

function opLabel(t) {
  return MODEL_ACTION_LABELS[t.action] || '修改'
}
function opTagType(action) {
  return MODEL_ACTION_TAG_MAP[action] || 'primary'
}

/* ============ 详情弹窗 ============ */
const detailVisible = ref(false)
const detailTicket = ref(null)

const detailTitle = computed(() => {
  const t = detailTicket.value
  if (!t) return '工单详情'
  return `${TYPE_LABEL_MAP[t.ticket_type] || '工单'}详情 #${t.id}`
})

// 详情头卡名称：model 优先 snapshot 名称，config/schedule 用配置名
const detailName = computed(() => {
  const t = detailTicket.value
  if (!t) return ''
  if (t.ticket_type === 'model') {
    const snap = t.snapshot_data || {}
    return snap.name || t.model_name || '-'
  }
  return t.config_label || t.config_key || '-'
})

function openDetail(t) {
  detailTicket.value = t
  detailVisible.value = true
}

// 模型值统一：布尔转中文，空值兜底
function normalizeModelValue(v) {
  if (v === true) return '启用'
  if (v === false) return '停用'
  if (v === null || v === undefined) return '-'
  return v
}

function modelFieldLabel(f) {
  return MODEL_CHANGE_FIELD_LABELS[f] || f
}
function modelFieldOld(f) {
  const d = (detailTicket.value && detailTicket.value.change_data) || {}
  return normalizeModelValue(d[f] ? d[f].old : '-')
}
function modelFieldNew(f) {
  const d = (detailTicket.value && detailTicket.value.change_data) || {}
  return normalizeModelValue(d[f] ? d[f].new : '-')
}

// 删除模型的信息列表：过滤掉空值字段
const modelInfoRows = computed(() => {
  const snap = detailTicket.value ? detailTicket.value.snapshot_data : {}
  const fields = [
    { key: 'name', label: '名称' },
    { key: 'model_name', label: '模型名称' },
    { key: 'model_type', label: '模型类型' },
    { key: 'provider', label: '服务商' },
    { key: 'base_url', label: 'Base URL' },
    { key: 'timeout', label: '超时时间' },
  ]
  return fields
    .filter(f => snap[f.key] !== undefined && snap[f.key] !== null && snap[f.key] !== '')
    .map(f => ({ label: f.label, value: f.key === 'timeout' ? snap[f.key] + 's' : String(snap[f.key]) }))
})

// 详情变更区标题：delete=模型信息 / deactivate=停用信息 / 修改=变更详情
const modelBodyTitle = computed(() => {
  const t = detailTicket.value
  if (!t) return ''
  if (t.action === 'delete') return '模型信息'
  if (t.action === 'deactivate') return '停用信息'
  return '变更详情'
})

// schedule 详情 diff 行：cron 变化 + 启停变化，附带 cron 中文解释（humanize）
const scheduleDiffRows = computed(() => {
  const t = detailTicket.value
  if (!t) return []
  const oldP = parseScheduleValue(t.old_value)
  const newP = parseScheduleValue(t.new_value)
  if (!oldP || !newP) return []
  const summary = t.change_summary || {}
  const cronSummary = summary.schedule && summary.schedule.cron
  const rows = []
  if (oldP.cron !== newP.cron) {
    rows.push({
      oldLabel: '原 Cron', oldValue: oldP.cron, oldHint: (cronSummary && cronSummary.old_desc) || '',
      newLabel: '新 Cron', newValue: newP.cron, newHint: (cronSummary && cronSummary.new_desc) || '',
    })
  }
  if (oldP.enabled !== newP.enabled) {
    rows.push({
      oldLabel: '原状态', oldValue: oldP.enabled ? '启用' : '停用', oldHint: '',
      newLabel: '新状态', newValue: newP.enabled ? '启用' : '停用', newHint: '',
    })
  }
  return rows
})

// 多值类配置变更摘要（added/removed）
const changeSummaryParts = computed(() => {
  const s = detailTicket.value ? detailTicket.value.change_summary : null
  if (!s) return { added: [], removed: [] }
  return { added: s.added || [], removed: s.removed || [] }
})

/* ============ 流转时间线 ============
 * 依据平铺字段（auditor/reviewer/audited_at/reviewed_at/applied_at）拼出
 * 提交 → 审核 → 复核 → 生效 的流转时间线；驳回/撤回作为终止节点展示。
 * 统一主表无 pending_review 状态，用 audited_at 是否存在判断"审核完成未复核"。
 */
const timelineNodes = computed(() => {
  const t = detailTicket.value
  if (!t) return []
  const nodes = []
  // 提交节点：始终存在；待审核（无审核时间）时标记为当前节点
  const isCurrentPending = t.status === 'PENDING' && !t.audited_at
  nodes.push({ dot: isCurrentPending ? 'current' : 'done', title: '提交工单', actor: t.creator, time: t.created_at })
  if (t.status === 'CANCELLED') {
    // 撤回终止：无审批节点
    nodes.push({ dot: 'withdrawn', title: '已撤回', actor: t.creator, time: t.created_at })
  } else if (t.status === 'REJECTED') {
    // 驳回终止：展示驳回人（审核或复核阶段均可驳回）
    nodes.push({
      dot: 'rejected', title: '已驳回',
      actor: t.auditor || t.reviewer,
      time: t.audited_at || t.reviewed_at,
      comment: t.audit_comment || t.review_comment,
    })
  } else {
    // 审批流转中：按已完成的阶段推进展示
    if (t.audited_at) {
      nodes.push({ dot: 'done', title: '审核通过', actor: t.auditor, time: t.audited_at, comment: t.audit_comment })
    }
    if (t.status === 'PENDING' && t.audited_at && !t.reviewed_at) {
      // 待复核：当前节点，无具体审批人
      nodes.push({ dot: 'current', title: '待复核', actor: '', time: '' })
    }
    if (t.status === 'APPROVED' || t.status === 'EXECUTED') {
      if (t.reviewed_at) {
        nodes.push({ dot: 'done', title: '复核通过', actor: t.reviewer, time: t.reviewed_at, comment: t.review_comment })
      }
      if (t.applied_at) {
        nodes.push({ dot: 'done', title: '已生效', actor: '', time: t.applied_at })
      }
    }
  }
  return nodes
})

/* ============ 审批动作（通过/驳回/撤回） ============ */
// 创建人/审核人判断：creator/auditor 存的是 username，需与用户 store 的 username 比较（不能用显示名）
const me = computed(() => (userStore.user && userStore.user.username) || '')
const isCreator = computed(() => detailTicket.value && detailTicket.value.creator === me.value)
const isAuditor = computed(() => detailTicket.value && detailTicket.value.auditor === me.value)
// 待审批状态（含审核阶段与复核阶段）
const isPending = computed(() => {
  const t = detailTicket.value
  return t && (t.status === 'PENDING')
})
// 审核阶段（status=PENDING 且审核未完成）：非创建人可 通过/驳回
const canApprove = computed(() => {
  const t = detailTicket.value
  return isPending.value && !isCreator.value && !t.audited_at
})
// 复核阶段（审核已完成未复核）：仅超管且非创建人/非审核人可 复核通过/驳回
const canReview = computed(() => {
  const t = detailTicket.value
  return t && t.status === 'PENDING' && !!t.audited_at
    && userStore.isSuperAdmin && !isCreator.value && !isAuditor.value
})
// 创建人可撤回未完成工单
const canWithdraw = computed(() => isCreator.value && isPending.value)

async function onApproveClick() {
  if (!detailTicket.value) return
  const t = detailTicket.value
  if (canReview.value && !userStore.isSuperAdmin) {
    ElMessage.error('复核仅超级管理员可操作')
    return
  }
  const title = t.config_label || t.config_key || detailName.value || '-'
  const isReviewing = t.status === 'PENDING' && !!t.audited_at
  try {
    const { value: comment } = await ElMessageBox.prompt(
      isReviewing
        ? `确认通过工单 #${t.id}（${title}）？复核通过后将立即生效。`
        : `确认通过工单 #${t.id}（${title}）？通过后${t.ticket_type === 'model' ? '操作将立即生效' : '配置将立即生效'}。`,
      isReviewing ? '复核通过' : '审批通过',
      {
        confirmButtonText: '确认通过',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '填写审批意见（可选）',
        type: 'success',
      }
    )
    await api.postJson(`/api/v1/system/tickets/${t.id}/approve/`, { comment: (comment || '').trim() })
    detailVisible.value = false
    ElMessage.success(isReviewing ? '复核通过' : '审批通过')
    await load()
  } catch (e) {
    // 用户取消输入框时不提示错误
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(`审批失败：${e.message}`)
  }
}

async function onRejectClick() {
  if (!detailTicket.value) return
  const t = detailTicket.value
  if (canReview.value && !userStore.isSuperAdmin) {
    ElMessage.error('复核仅超级管理员可操作')
    return
  }
  const title = t.config_label || t.config_key || detailName.value || '-'
  try {
    // 驳回理由必填：便于申请人了解问题
    const { value: comment } = await ElMessageBox.prompt(
      `确认驳回工单 #${t.id}（${title}）？`,
      '驳回工单',
      {
        confirmButtonText: '确认驳回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '请填写驳回原因',
        inputValidator: v => (v && v.trim() ? true : '请填写驳回原因'),
        inputErrorMessage: '请填写驳回原因',
        type: 'warning',
      }
    )
    await api.postJson(`/api/v1/system/tickets/${t.id}/reject/`, { comment: comment.trim() })
    detailVisible.value = false
    ElMessage.success('已驳回')
    await load()
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(`驳回失败：${e.message}`)
  }
}

async function onWithdrawClick() {
  if (!detailTicket.value) return
  const t = detailTicket.value
  const title = t.config_label || t.config_key || detailName.value || '-'
  try {
    const { value: comment } = await ElMessageBox.prompt(
      `确认撤回工单 #${t.id}（${title}）？撤回后该工单将作废。`,
      '撤回工单',
      {
        confirmButtonText: '确认撤回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '填写撤回原因（可选）',
        type: 'info',
      }
    )
    await api.postJson(`/api/v1/system/tickets/${t.id}/withdraw/`, { comment: (comment || '').trim() })
    detailVisible.value = false
    ElMessage.success('已撤回')
    await load()
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(`撤回失败：${e.message}`)
  }
}

/* ============ 发起工单 ============
 * 支持四类：permission（权限变更，走共享审批池）/ config / schedule / model（走系统工单）。
 * 选择类型后按类型展示变更内容字段，reason 必填。
 */
const createVisible = ref(false)
const createSaving = ref(false)
const createFormRef = ref()
const createForm = reactive({
  ticketType: 'config',
  targetUserId: null,
  roleKey: '',
  scopeType: 'NONE',
  scopeId: '',
  configKey: '',
  newValue: '',
  cron: '',
  enabled: true,
  targetModelId: null,
  operation: 'update_normal',
  reason: '',
})
const allConfigs = ref([])      // 全部配置项（展平）
const scheduleConfigs = ref([]) // 调度类配置项（key 前缀 SCHEDULE_）
const allModels = ref([])       // 全部模型（展平）
const assignableRoles = ref([]) // 管理岗可任命角色
const userOptions = ref([])     // 用户搜索候选
const userSearching = ref(false)
let userSearchSeq = 0

async function openCreateDialog() {
  // 打开时并行预载发起所需的静态下拉数据（配置/模型/角色），避免提交时才发现缺失
  createVisible.value = true
  if (!allConfigs.value.length) await loadConfigs()
  if (!allModels.value.length) await loadModels()
  if (!assignableRoles.value.length) await loadAssignableRoles()
}

// 加载配置列表（按分类分组的 groups 展平为单层，供配置项下拉使用）
async function loadConfigs() {
  try {
    const data = await api.getJson('/api/v1/system/configs/')
    const groups = data.groups || {}
    const list = []
    for (const cat of Object.keys(groups)) {
      for (const c of groups[cat] || []) list.push(c)
    }
    allConfigs.value = list
    // 调度类配置：仅 key 以 SCHEDULE_ 开头的可发起定时任务工单
    scheduleConfigs.value = list.filter(c => (c.key || '').startsWith(SCHEDULE_PREFIX))
  } catch (e) {
    ElMessage.error('加载配置列表失败: ' + errMsg(e, '未知错误'))
  }
}

// 加载模型列表（按 model_type 分组的 groups 展平为单层）
async function loadModels() {
  try {
    const data = await api.getJson('/api/v1/system/llm-models/')
    const groups = data.groups || {}
    const list = []
    for (const type of Object.keys(groups)) {
      for (const m of groups[type] || []) list.push(m)
    }
    allModels.value = list
  } catch (e) {
    ElMessage.error('加载模型列表失败: ' + errMsg(e, '未知错误'))
  }
}

// 加载管理岗任命角色清单（super_admin 不在此列，超管任命走用户编辑接口）
async function loadAssignableRoles() {
  try {
    const data = await api.getJson('/api/v1/auth/permissions/assignable-roles/?purpose=management')
    assignableRoles.value = data.rows || []
  } catch (e) {
    ElMessage.error('加载角色清单失败: ' + errMsg(e, '未知错误'))
  }
}

// 用户远程搜索（权限变更工单选择目标用户）
async function searchUsers(q) {
  const query = (q || '').trim()
  if (!query) {
    userOptions.value = []
    return
  }
  const seq = ++userSearchSeq
  userSearching.value = true
  try {
    const data = await api.getJson(`/api/v1/auth/users/search/?q=${encodeURIComponent(query)}`)
    // 竞态检查：丢弃过期请求结果
    if (seq !== userSearchSeq) return
    userOptions.value = data.users || []
  } catch (e) {
    userOptions.value = []
  } finally {
    if (seq === userSearchSeq) userSearching.value = false
  }
}

async function submitCreate() {
  if (createSaving.value) return
  try {
    await createFormRef.value.validate()
  } catch {
    return
  }
  // 权限变更：scope 范围非 NONE 时必须填写 scope_id（部门/团队 ID）
  if (createForm.ticketType === 'permission' && createForm.scopeType !== 'NONE' && !createForm.scopeId.trim()) {
    ElMessage.warning('请填写范围 ID（部门/团队 ID）')
    return
  }
  createSaving.value = true
  try {
    if (createForm.ticketType === 'permission') {
      // 权限变更工单走共享审批池（与旧 admin-org 任命逻辑一致，change_type 固定 GRANT）
      const payload = {
        role_key: createForm.roleKey,
        scope_type: createForm.scopeType,
        change_type: 'GRANT',
        target_user_id: createForm.targetUserId,
        reason: createForm.reason.trim(),
      }
      // scope 范围非 NONE 时携带 scope_id（部门/团队 ID）
      if (createForm.scopeType !== 'NONE') payload.scope_id = parseInt(createForm.scopeId, 10)
      const resp = await api.postJson('/api/v1/auth/permissions/applications/', payload)
      ElMessage.success((resp.detail || '申请已提交') + (resp.ticket_no ? `（${resp.ticket_no}）` : ''))
    } else {
      // 配置/定时/模型工单走统一系统工单 API
      const body = { ticket_type: createForm.ticketType, reason: createForm.reason.trim() }
      if (createForm.ticketType === 'schedule') {
        // 调度类配置的 new_value 为 JSON 字符串 {cron, enabled}
        body.config_key = createForm.configKey
        body.new_value = JSON.stringify({ cron: createForm.cron.trim(), enabled: createForm.enabled })
      } else if (createForm.ticketType === 'config') {
        body.config_key = createForm.configKey
        body.new_value = createForm.newValue
      } else {
        body.target_model_id = createForm.targetModelId
        body.operation = createForm.operation
      }
      const ticket = await api.postJson('/api/v1/system/tickets/', body)
      ElMessage.success(`工单已提交（#${ticket.id}），等待审批`)
    }
    createVisible.value = false
    // 切到"我的工单"tab 立即看到新提交的工单
    filterTab.value = 'mine'
    page.value = 1
    await load()
  } catch (e) {
    ElMessage.error(`提交失败：${e.message}`)
  } finally {
    createSaving.value = false
  }
}

/* ============ 初始化 ============ */
onMounted(async () => {
  await load()
})
</script>

<style scoped>
/* ===== 页头与卡片 ===== */
.ticket-center-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
}

/* ===== 筛选工具栏：类型下拉 + 状态 tab + 搜索 + 按钮同行排列（单行紧凑） ===== */
.tc-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  padding: 10px 12px;
  background: var(--app-menu-hover);
  border-radius: 6px;
  border: 1px solid var(--app-border);
}

.tc-type-select {
  flex-shrink: 0;
  width: 130px;
}

.tc-filter-tabs {
  display: flex;
  align-items: center;
  flex: 1;
  min-width: 0;
}

.tc-search {
  width: 200px;
  flex-shrink: 0;
}

/* 待办计数：红色气泡，嵌在"待我处理"tab 内 */
.tc-todo-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 8px;
  background: #ef4444;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
}

/* ===== 工单列表容器 ===== */
.tc-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 200px;
  flex: 1;
}

/* 工单卡片：统一两行网格布局，浅色背景 + hover 高亮 */
.tc-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 14px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: background 0.12s, border-color 0.12s;
  border-radius: 8px;
  background: var(--app-card-bg);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}

.tc-item:hover {
  background: #eff6ff;
  border-left-color: #2563eb;
}

/* 标题行：徽标 + 名称 + key + 风险 + 状态 */
.tc-item-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* 状态徽标推到标题行最右侧 */
.tc-item-status {
  margin-left: auto;
  flex-shrink: 0;
}

/* 名称占剩余空间，过长省略 */
.tc-item-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 240px;
}

.tc-item-key {
  font-size: 11px;
  color: var(--app-text-sub);
  font-family: 'SF Mono', 'Consolas', monospace;
  background: var(--app-menu-hover);
  padding: 1px 6px;
  border-radius: 4px;
  white-space: nowrap;
}

/* 高风险标记（红色文字） */
.tc-item-risk {
  font-size: 11px;
  color: #dc2626;
  font-weight: 500;
  white-space: nowrap;
}

/* Meta 行：左侧（原因 + 操作摘要）+ 右侧（创建人/时间底部对齐） */
.tc-item-meta {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  font-size: 12px;
  color: var(--app-text-sub);
}

.tc-meta-left {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

/* 申请原因：默认 2 行截断，hover 展开更多 */
.tc-meta-reason {
  line-height: 17px;
  max-height: 34px;
  overflow: hidden;
  color: var(--app-text-sub);
}

.tc-item:hover .tc-meta-reason {
  max-height: 85px;
}

.tc-meta-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  flex-shrink: 0;
  white-space: nowrap;
  text-align: right;
}

.tc-action-label {
  font-size: 12px;
  color: var(--app-text-sub);
  padding-top: 2px;
}

/* ===== 分页 ===== */
.tc-pagination {
  margin-top: 14px;
  justify-content: flex-end;
}

/* ===== 详情弹窗 ===== */
.tc-detail-body {
  background: var(--app-menu-hover);
  padding: 4px;
}

.tc-detail-card {
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 10px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
}

.tc-detail-card:last-child {
  margin-bottom: 0;
}

.tc-detail-header {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.tc-detail-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
}

.tc-detail-meta {
  display: flex;
  gap: 20px;
  font-size: 12px;
  color: var(--app-text-sub);
  flex-wrap: wrap;
}

.tc-diff-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 12px;
  letter-spacing: 0.02em;
}

.tc-diff-row {
  display: flex;
  align-items: stretch;
  gap: 14px;
}

.tc-diff-row + .tc-diff-row {
  margin-top: 10px;
}

.model-diff-row {
  margin-top: 10px;
}

.tc-diff-side {
  flex: 1;
  border-radius: 10px;
  padding: 12px 14px;
  border: 1px solid;
  min-width: 0;
}

.tc-diff-side-old {
  background: linear-gradient(135deg, #fef2f2 0%, #fff5f5 100%);
  border-color: #fecaca;
}

.tc-diff-side-new {
  background: linear-gradient(135deg, #f0f9ff 0%, #f5f8ff 100%);
  border-color: #bfdbfe;
}

.tc-diff-side-label {
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 6px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tc-diff-side-old .tc-diff-side-label { color: #dc2626; }
.tc-diff-side-new .tc-diff-side-label { color: #2563eb; }

.tc-diff-side-value {
  font-size: 13px;
  color: var(--app-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
  line-height: 1.6;
  max-height: 96px;
  overflow-y: auto;
}

.tc-diff-side-hint {
  font-size: 12px;
  margin-top: 4px;
  line-height: 1.4;
}

.tc-diff-side-old .tc-diff-side-hint { color: #b91c1c; }
.tc-diff-side-new .tc-diff-side-hint { color: #1d4ed8; }

.tc-diff-arrow {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  color: var(--app-text-sub);
  font-weight: 300;
  flex-shrink: 0;
  width: 32px;
}

/* 变更原因 */
.tc-reason {
  background: var(--app-menu-hover);
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 12px 14px;
}

.tc-reason-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text-sub);
  margin-bottom: 6px;
}

.tc-reason-value {
  font-size: 13px;
  color: var(--app-text);
  line-height: 1.5;
  word-break: break-all;
  white-space: pre-wrap;
}

/* 多值类配置变更摘要（added 绿 / removed 红） */
.tc-change-summary {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--app-border);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tc-change-added { color: #16a34a; }
.tc-change-removed { color: #dc2626; }

.tc-change-added code,
.tc-change-removed code {
  display: inline-block;
  padding: 1px 6px;
  margin: 2px 4px 2px 0;
  border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.tc-change-added code {
  background: #f0fdf4;
  color: #166534;
  border: 1px solid #bbf7d0;
}

.tc-change-removed code {
  background: #fef2f2;
  color: #991b1b;
  border: 1px solid #fecaca;
}

/* 模型工单删除/停用警示条 */
.tc-warning {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  border-radius: 8px;
  margin-bottom: 14px;
}

.tc-warning-danger {
  background: linear-gradient(135deg, #fef2f2 0%, #fff1f2 100%);
  border: 1px solid #fecaca;
}

.tc-warning-warn {
  background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
  border: 1px solid #fde68a;
}

.tc-warning-icon {
  font-size: 20px;
  line-height: 1;
  flex-shrink: 0;
}

.tc-warning-text {
  font-size: 12px;
  line-height: 1.6;
}

.tc-warning-danger .tc-warning-text { color: #991b1b; }
.tc-warning-warn .tc-warning-text { color: #92400e; }

.tc-warning-text strong {
  display: block;
  font-size: 13px;
  margin-bottom: 2px;
}

.tc-warning-danger .tc-warning-text strong { color: #dc2626; }
.tc-warning-warn .tc-warning-text strong { color: #b45309; }

.dep-warning {
  margin-top: 12px;
  margin-bottom: 0;
}

/* 模型删除：信息列表卡 */
.tc-info-card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-menu-hover);
  overflow: hidden;
}

.tc-info-title {
  padding: 10px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
  background: var(--app-menu-hover);
  border-bottom: 1px solid var(--app-border);
}

.tc-info-row {
  display: flex;
  align-items: center;
  padding: 10px 14px;
  border-bottom: 1px solid var(--app-border);
}

.tc-info-row:last-child {
  border-bottom: none;
}

.tc-info-label {
  flex: 0 0 110px;
  font-size: 12px;
  color: var(--app-text-sub);
  font-weight: 500;
}

.tc-info-value {
  flex: 1;
  font-size: 13px;
  color: var(--app-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  word-break: break-all;
}

/* 模型停用：当前→变更 状态对照 */
.tc-state-grid {
  display: flex;
  gap: 12px;
}

.tc-state-item {
  flex: 1;
  border-radius: 8px;
  padding: 12px 14px;
  border: 1px solid;
}

.tc-state-old {
  border-color: #fecaca;
  background: #fef2f2;
}

.tc-state-new {
  border-color: #bfdbfe;
  background: #f0f9ff;
}

.tc-state-label {
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 4px;
}

.tc-state-old .tc-state-label { color: #dc2626; }
.tc-state-new .tc-state-label { color: #2563eb; }

.tc-state-value {
  font-size: 13px;
  font-weight: 500;
}

/* 审批时间线：提交→审核→复核→生效/驳回/撤回 */
.tc-timeline {
  padding: 4px 0;
}

.tc-tl-item {
  display: flex;
  gap: 12px;
  position: relative;
  padding-bottom: 18px;
}

.tc-tl-item:last-child {
  padding-bottom: 0;
}

.tc-tl-item:not(:last-child)::before {
  content: '';
  position: absolute;
  left: 7px;
  top: 18px;
  bottom: 0;
  width: 2px;
  background: var(--app-border);
}

.tc-tl-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  flex-shrink: 0;
  position: relative;
  z-index: 1;
  margin-top: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.tc-tl-dot-current {
  background: #2563eb;
  box-shadow: 0 0 0 4px #eff6ff;
}

.tc-tl-dot-current::after {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--app-card-bg);
}

.tc-tl-dot-done {
  background: #10b981;
}

.tc-tl-dot-done::after {
  content: '✓';
  color: #fff;
  font-size: 10px;
  font-weight: 700;
}

.tc-tl-dot-rejected {
  background: #ef4444;
}

.tc-tl-dot-rejected::after {
  content: '✕';
  color: #fff;
  font-size: 10px;
  font-weight: 700;
}

.tc-tl-dot-withdrawn {
  background: var(--app-text-sub);
}

.tc-tl-dot-withdrawn::after {
  content: '↩';
  color: #fff;
  font-size: 10px;
  font-weight: 700;
}

.tc-tl-body {
  flex: 1;
  min-width: 0;
}

.tc-tl-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.tc-tl-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
}

.tc-tl-actor {
  font-size: 12px;
  color: var(--app-text-sub);
}

.tc-tl-time {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-left: auto;
}

.tc-tl-comment {
  margin-top: 6px;
  font-size: 13px;
  color: var(--app-text);
  background: var(--app-bg);
  border-radius: 6px;
  padding: 8px 10px;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.5;
}

/* 详情弹窗底部按钮：驳回+通过右对齐，撤回靠左 */
.tc-detail-footer {
  display: flex;
  align-items: center;
  gap: 10px;
}

.tc-detail-footer .footer-actions {
  margin-left: auto;
  display: flex;
  gap: 10px;
}
</style>
