<template>
  <div class="page-container ticket-page">
    <!-- ===== 页头 ===== -->
    <div class="page-header">
      <div>
        <div class="page-title">工单中心</div>
        <div class="page-desc">工单统一审批处理（权限/配置/定时/模型/组织/安全）</div>
      </div>
    </div>

    <!-- ===== 列表卡片：toolbar 固定 + 表格滚动 ===== -->
    <div class="page-body">
    <div class="app-card ticket-card">
      <!-- 工具栏：视角切换 + 类型/状态筛选 + 搜索 + 操作按钮 -->
      <div class="ticket-toolbar">
        <el-radio-group v-model="currentView" size="small" @change="switchView">
          <el-radio-button value="pending">待我审批<span v-if="pendingCount > 0" class="view-badge">{{ pendingCount }}</span></el-radio-button>
          <el-radio-button value="processed">我已审批</el-radio-button>
          <el-radio-button value="mine">我的工单</el-radio-button>
          <el-radio-button value="all">全部工单</el-radio-button>
        </el-radio-group>

        <div class="toolbar-right">
          <el-select v-model="typeFilter" placeholder="全部类型" clearable style="width: 130px" @change="refreshCurrent">
            <el-option label="权限审批" value="permission" />
            <el-option label="配置变更" value="config" />
            <el-option label="定时任务" value="schedule" />
            <el-option label="模型变更" value="model" />
            <el-option label="组织变更" value="org" />
            <el-option label="安全设置" value="security" />
          </el-select>
          <!-- 状态筛选：仅"全部工单"视角显示 -->
          <el-select v-if="currentView === 'all'" v-model="statusFilter" placeholder="全部状态" clearable style="width: 120px" @change="refreshCurrent">
            <el-option label="待审批" value="PENDING" />
            <el-option label="已通过" value="EXECUTED" />
            <el-option label="已驳回" value="REJECTED" />
            <el-option label="已撤回" value="CANCELLED" />
          </el-select>
          <el-input v-model="searchInput" placeholder="工单号 / ID / 创建人 / 任务名" clearable style="width: 200px" @keyup.enter="refreshCurrent" @clear="refreshCurrent" />
          <el-button size="small" @click="refreshCurrent">搜索</el-button>
          <el-button size="small" @click="refreshCurrent">刷新</el-button>
        </div>
      </div>

      <!-- 表格滚动区：固定表头 + 表体内部滚动 -->
      <div class="page-scroll pad-x-12">
      <!-- 工单列表（四视角共用一张表，列内容按视角微调） -->
      <el-table :data="rows" v-loading="loading" class="ticket-table" height="100%" @row-click="onRowClick">
        <el-table-column label="工单号" width="120">
          <template #default="{ row }"><span class="mono">{{ row.ticket_no }}</span></template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag :type="bizTagType(row.biz_type)" size="small" effect="plain">{{ bizTypeLabel(row.biz_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="任务名" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title || '—' }}</template>
        </el-table-column>
        <el-table-column label="申请人" width="120">
          <template #default="{ row }">
            <template v-if="currentView === 'mine'">
              <span class="text-sub">我</span>
            </template>
            <template v-else>
              <div class="fw-500">{{ row.applicant_name || '—' }}</div>
              <div class="text-sub text-xs">{{ row.applicant_username || '' }}</div>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="目标" min-width="160">
          <template #default="{ row }">
            <div>{{ targetCell(row).main }}</div>
            <div v-if="targetCell(row).sub" class="text-sub text-xs">{{ targetCell(row).sub }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <!-- 待我审批视角固定显示"待审批"，其他视角显示实际状态 -->
            <el-tag v-if="currentView === 'pending'" type="warning" size="small" effect="plain">待审批</el-tag>
            <el-tag v-else :type="statusTagType(row.status)" size="small" effect="plain">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="currentView === 'processed' ? '处理时间' : '申请时间'" width="135">
          <template #default="{ row }">
            <span class="text-sub">{{ formatDate(timeField(row)) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="60">
          <template #default="{ row }">
            <!-- 待审批视角固定蓝色提示；其他视角已完成步数 >= 总步数时显示绿色 -->
            <el-tag v-if="currentView === 'pending'" type="primary" size="small" effect="plain">第 {{ (row.current_step || 0) + 1 }}/{{ row.total_steps || 1 }} 步</el-tag>
            <el-tag v-else :type="row.total_steps > 0 && row.current_step >= row.total_steps ? 'success' : 'primary'" size="small" effect="plain">{{ (row.current_step || 0) + 1 }}/{{ row.total_steps || 1 }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button v-if="currentView === 'pending'" link type="primary" size="small" @click.stop="onRowClick(row)">处理</el-button>
            <el-button v-else-if="currentView === 'mine' && row.status === 'PENDING'" link type="warning" size="small" @click.stop="onRowClick(row)">撤回</el-button>
            <el-button v-else link type="primary" size="small" @click.stop="onRowClick(row)">查看</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty :description="emptyText" :image-size="60" /></template>
      </el-table>
      </div>

      <!-- 分页：后端控制，切换视角/筛选/搜索后回到第 1 页 -->
      <AppPagination
        class="pagination-bar"
        :total="total"
        :page-size="PAGE_SIZE"
        :page="page"
        @page-change="onPageChange"
      />
    </div>
    </div>

    <!-- ===== 审批详情弹窗（复用公共 BaseDialog：宽度 50%，小屏兜底 720px） ===== -->
    <BaseDialog
      v-model="approvalVisible"
      :title="`工单详情 · ${currentTicket ? currentTicket.ticket_no : ''}`"
      width="50%"
      min-width="720px"
      :close-on-click-modal="false"
    >
      <div v-if="currentTicket" class="approval-body">
        <!-- 申请人卡片 -->
        <div class="applicant-card">
          <div class="applicant-avatar">{{ avatarChar }}</div>
          <div class="applicant-info">
            <div class="applicant-name">{{ currentTicket.applicant_name || '—' }}</div>
            <div class="applicant-meta">{{ currentTicket.applicant_username || '' }} · {{ currentTicket.applicant_email || '' }}</div>
          </div>
          <div class="applicant-time">
            <div class="applicant-time-label">申请时间</div>
            {{ formatDate(currentTicket.created_at) }}
          </div>
        </div>

        <!-- 工单信息 -->
        <div class="detail-section-title">工单信息</div>
        <div class="detail-grid">
          <div class="detail-cell">
            <div class="detail-cell-label">工单类型</div>
            <div class="detail-cell-value">
              <el-tag :type="bizTagType(currentTicket.biz_type)" size="small" effect="plain">{{ bizTypeLabel(currentTicket.biz_type) }}</el-tag>
            </div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">任务名</div>
            <div class="detail-cell-value">{{ currentTicket.title || '—' }}</div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">风险等级</div>
            <div class="detail-cell-value">{{ riskLabel(currentTicket.risk_level) }}</div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">当前状态</div>
            <div class="detail-cell-value">
              <el-tag :type="statusTagType(currentTicket.status)" size="small" effect="plain">{{ statusText(currentTicket.status) }}</el-tag>
            </div>
          </div>
        </div>

        <!-- ===== 业务详情（按类型渲染） ===== -->
        <!-- permission：变更类型/目标用户/角色/权限范围/生效时间/截至日期 -->
        <template v-if="currentTicket.biz_type === 'permission'">
          <div class="detail-section-title">变更内容</div>
          <div class="detail-grid">
            <div class="detail-cell">
              <div class="detail-cell-label">变更类型</div>
              <div class="detail-cell-value">
                <el-tag :type="changeTypeTagType(currentTicket.change_type)" size="small" effect="plain">{{ changeTypeLabel(currentTicket.change_type) }}</el-tag>
              </div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">目标用户</div>
              <div v-if="isSelfApply" class="detail-cell-value">本人申请</div>
              <div v-else>
                <div class="detail-cell-value">{{ currentTicket.target_user_name || '—' }}</div>
                <div class="detail-cell-sub">{{ currentTicket.target_user_email || '' }}</div>
              </div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">{{ currentTicket.change_type === 'ROLE_CHANGE' ? '角色变更' : '目标角色' }}</div>
              <div class="detail-cell-value">
                <!-- 角色变更展示 旧角色 → 新角色 -->
                <template v-if="currentTicket.change_type === 'ROLE_CHANGE' && currentTicket.previous_role_name">
                  <el-tag type="warning" size="small" effect="plain">{{ currentTicket.previous_role_name }}</el-tag>
                  <span class="text-sub role-arrow">→</span>
                  <el-tag type="primary" size="small" effect="plain">{{ currentTicket.role_name || '—' }}</el-tag>
                </template>
                <el-tag v-else-if="currentTicket.role_name" type="primary" size="small" effect="plain">{{ currentTicket.role_name }}</el-tag>
                <span v-else>—</span>
              </div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">权限范围</div>
              <div class="detail-cell-value">{{ scopeText }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">生效时间</div>
              <div class="detail-cell-value">{{ currentTicket.effective_from ? formatDate(currentTicket.effective_from) : '立即生效' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">截至日期</div>
              <div class="detail-cell-value">{{ currentTicket.expires_at ? formatDate(currentTicket.expires_at) : '长期有效' }}</div>
            </div>
          </div>
          <div class="detail-section-title">申请理由</div>
          <div class="reason-box">{{ currentTicket.reason || '—' }}</div>
        </template>

        <!-- config / schedule：配置项/操作/原值/新值/变更摘要/掩码提示 -->
        <template v-else-if="currentTicket.biz_type === 'config' || currentTicket.biz_type === 'schedule'">
          <div class="detail-section-title">变更内容</div>
          <div class="detail-grid">
            <div class="detail-cell">
              <div class="detail-cell-label">配置项</div>
              <div class="detail-cell-value">{{ currentTicket.config_label || currentTicket.config_key || '—' }}</div>
              <div v-if="currentTicket.config_key" class="detail-cell-sub mono">{{ currentTicket.config_key }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">{{ currentTicket.biz_type === 'schedule' ? '操作' : '变更类型' }}</div>
              <div class="detail-cell-value">{{ currentTicket.operation_display || '—' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">原值</div>
              <div class="detail-cell-value word-break">{{ displayValue(currentTicket.old_value) }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">新值</div>
              <div class="detail-cell-value word-break">{{ displayValue(currentTicket.new_value) }}</div>
            </div>
            <!-- 多值类配置变更摘要：added 绿 / removed 红 -->
            <div v-if="!isSecret && configSummaryParts.length" class="detail-cell full-cell">
              <div class="detail-cell-label">变更摘要</div>
              <div class="detail-cell-value">
                <div v-if="configSummaryParts.added.length" class="summary-line summary-added">新增：{{ configSummaryParts.added.join('、') }}</div>
                <div v-if="configSummaryParts.removed.length" class="summary-line summary-removed">移除：{{ configSummaryParts.removed.join('、') }}</div>
              </div>
            </div>
          </div>
          <div v-if="isSecret" class="text-sub secret-hint">⚠ 敏感配置项，旧值/新值已掩码</div>
          <div class="detail-section-title">变更原因</div>
          <div class="reason-box">{{ currentTicket.reason || '—' }}</div>
        </template>

        <!-- model：目标模型/操作类型/变更字段 -->
        <template v-else-if="currentTicket.biz_type === 'model'">
          <div class="detail-section-title">变更内容</div>
          <div class="detail-grid">
            <div class="detail-cell">
              <div class="detail-cell-label">目标模型</div>
              <div class="detail-cell-value">{{ currentTicket.model_name || '—' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">操作类型</div>
              <div class="detail-cell-value">{{ currentTicket.operation_display || '—' }}</div>
            </div>
            <div class="detail-cell full-cell">
              <div class="detail-cell-label">变更字段</div>
              <div class="detail-cell-value">
                <el-tag v-for="f in currentTicket.changed_fields || []" :key="f" type="info" size="small" effect="plain" class="field-tag">{{ modelFieldLabel(f) }}</el-tag>
                <span v-if="!(currentTicket.changed_fields || []).length">—</span>
              </div>
            </div>
          </div>
          <div class="detail-section-title">变更原因</div>
          <div class="reason-box">{{ currentTicket.reason || '—' }}</div>
        </template>

        <!-- org：组织类型/操作/目标 + 变更前后 diff -->
        <template v-else-if="currentTicket.biz_type === 'org'">
          <div class="detail-section-title">变更内容</div>
          <div class="detail-grid">
            <div class="detail-cell">
              <div class="detail-cell-label">组织类型</div>
              <div class="detail-cell-value">{{ currentTicket.org_type_display || '—' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">操作</div>
              <div class="detail-cell-value">{{ currentTicket.operation_display || '—' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">目标</div>
              <div class="detail-cell-value">{{ currentTicket.org_name || '—' }}</div>
            </div>
          </div>
          <template v-if="orgDiffRows.length">
            <div class="detail-section-title">变更前后</div>
            <div class="detail-grid">
              <div v-for="r in orgDiffRows" :key="r.label" class="detail-cell">
                <div class="detail-cell-label">{{ r.label }}</div>
                <div class="detail-cell-value">{{ r.newValue }}</div>
                <div v-if="r.oldValue" class="detail-cell-sub">原值: {{ r.oldValue }}</div>
              </div>
            </div>
          </template>
          <div class="detail-section-title">申请理由</div>
          <div class="reason-box">{{ currentTicket.reason || '—' }}</div>
        </template>

        <!-- security：安全配置类型/操作/目标 -->
        <template v-else-if="currentTicket.biz_type === 'security'">
          <div class="detail-section-title">变更内容</div>
          <div class="detail-grid">
            <div class="detail-cell">
              <div class="detail-cell-label">安全配置类型</div>
              <div class="detail-cell-value">{{ currentTicket.security_type_display || '—' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">操作</div>
              <div class="detail-cell-value">{{ currentTicket.operation_display || '—' }}</div>
            </div>
            <div class="detail-cell">
              <div class="detail-cell-label">目标</div>
              <div class="detail-cell-value mono">{{ currentTicket.security_target || '—' }}</div>
            </div>
          </div>
          <div class="detail-section-title">申请理由</div>
          <div class="reason-box">{{ currentTicket.reason || '—' }}</div>
        </template>

        <!-- 审批链进度：多节点时间线 -->
        <div class="detail-section-title">审批链进度</div>
        <ol class="chain-timeline">
          <li v-for="(cn, i) in chainNodes" :key="i" :class="cn.isRejected ? 'step-rejected' : cn.isApproved ? 'step-done' : cn.isCurr ? 'step-curr' : 'step-pending'">
            <span class="chain-node-dot" :class="cn.isRejected ? 'dot-rejected' : cn.isApproved ? 'dot-done' : cn.isCurr ? 'dot-curr' : 'dot-pending'"></span>
            <div class="chain-node-role">
              {{ approverRoleLabel(cn.node.approver_role) }}
              <span class="chain-node-status" :class="cn.isRejected ? 'rejected' : cn.isApproved ? 'done' : cn.isCurr ? 'curr' : 'pending'">{{ cn.statusLabel }}</span>
            </div>
            <div v-if="cn.isApproved || cn.isRejected" class="chain-node-approver">审批人：{{ cn.node.approver_name }}</div>
            <div v-if="cn.node.comment" class="chain-node-comment">{{ cn.node.comment }}</div>
            <div v-if="cn.node.approved_at" class="chain-node-time">{{ formatDate(cn.node.approved_at) }}</div>
          </li>
        </ol>
        <!-- 当前待审批人提示条 -->
        <div v-if="currentTicket.status === 'PENDING'" class="current-approver-bar">
          <span>📋</span>
          <span>当前待审批：{{ currentApproverLabel }}</span>
        </div>

        <!-- 我已审批视角：追加展示当前用户在各节点的审批记录 -->
        <template v-if="currentView === 'processed' && myApprovals.length">
          <div class="detail-section-title">我的审批记录</div>
          <div v-for="(n, i) in myApprovals" :key="i" class="my-approval-box">
            <div class="detail-grid">
              <div class="detail-cell">
                <div class="detail-cell-label">审批角色</div>
                <div class="detail-cell-value">{{ approverRoleLabel(n.approver_role) }}</div>
              </div>
              <div class="detail-cell">
                <div class="detail-cell-label">处理时间</div>
                <div class="detail-cell-value">{{ n.approved_at ? formatDate(n.approved_at) : '—' }}</div>
              </div>
              <div class="detail-cell full-cell">
                <div class="detail-cell-label">审批意见</div>
                <div class="detail-cell-value">{{ n.comment || '—' }}</div>
              </div>
            </div>
          </div>
        </template>
      </div>
      <template #footer>
        <div class="approval-footer">
          <el-button @click="approvalVisible = false">关闭</el-button>
          <!-- 我的工单视角 + PENDING：显示撤回 -->
          <el-button v-if="showWithdraw" type="warning" :loading="submitting" @click="onWithdrawClick">↩ 撤回</el-button>
          <!-- 待我审批视角 + PENDING：显示驳回/通过 -->
          <template v-if="showActions">
            <el-button type="danger" :loading="submitting" @click="onRejectClick">驳回</el-button>
            <el-button type="primary" :loading="submitting" @click="onApproveClick">通过</el-button>
          </template>
        </div>
      </template>
    </BaseDialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, errMsg, displayValue } from '../utils/format'
import { makeStatusMeta, TICKET_STATUS_LABEL_MAP, TICKET_STATUS_TAG_MAP } from '../utils/labels'
import { usePagination } from '../composables/usePagination'
import { useListLoader } from '../composables/useListLoader'
import BaseDialog from '../components/base/BaseDialog.vue'
import AppPagination from '../components/base/AppPagination.vue'

/* ============ 常量与映射 ============ */
const PAGE_SIZE = 20 // 每页条数（后端控制分页）

// 类型显示映射：badge 与提示消息共用同一份映射，避免两处口径漂移
const BIZ_TYPE_LABEL_MAP = {
  permission: '权限审批', config: '配置变更', schedule: '定时任务',
  model: '模型变更', org: '组织变更', security: '安全设置',
}
const BIZ_TYPE_TAG_MAP = { permission: 'primary', config: 'warning', schedule: 'info', model: 'danger', org: 'primary', security: 'warning' }

// 工单状态文案/标签色：走 utils/labels 的共享映射（与后端统一主表大写枚举一致）

// 审批链审批人角色中文名
const APPROVER_ROLE_MAP = {
  TEAM_LEADER: '团队组长', DEPT_LEADER: '部门经理', DEPT_MANAGER: '部门经理',
  USER_ADMIN: '用户管理员', KB_ADMIN: '知识管理员', SUPER_ADMIN: '超级管理员', SYSTEM_AUDITOR: '系统审核员',
}

// 权限范围类型中文名
const SCOPE_TYPE_MAP = { GLOBAL: '全局', DEPT: '部门', TEAM: '团队', NONE: '—' }

// 变更类型中文名 + tag type
const CHANGE_TYPE_MAP = {
  GRANT: { label: '授予权限', type: 'success' },
  REVOKE: { label: '撤销权限', type: 'warning' },
  ROLE_CHANGE: { label: '角色变更', type: 'primary' },
  SCOPE_CHANGE: { label: '范围变更', type: 'primary' },
  EXPIRE_EXTEND: { label: '延期', type: 'primary' },
}

// 模型字段中文名（模型工单变更字段标签）
const MODEL_FIELD_LABELS = {
  name: '显示名', provider: 'Provider', model_type: '类型',
  base_url: '接口地址', model_name: '模型名', timeout: '超时(秒)', is_active: '启用状态',
}

// org 变更字段中文名（old_data/new_data 快照字段）
const ORG_FIELD_LABELS = {
  name: '名称', code: '编码', description: '描述', department_id: '所属部门', department_name: '所属部门',
}

// 统一轮询间隔：10 分钟（需求要求固定间隔，不做角色区分）
const POLL_INTERVAL = 10 * 60 * 1000

/* ============ 页面状态 ============ */
const userStore = useUserStore()

const currentView = ref('pending') // 当前视角：pending / processed / mine / all
const typeFilter = ref('')         // 类型筛选（空=全部）
const statusFilter = ref('')       // 状态筛选（仅 all 视角）
const searchInput = ref('')        // 搜索关键词
const rows = ref([])               // 当前页工单
const total = ref(0)
// 列表加载：由 useListLoader 统一管理 loading/请求序号守卫/错误提示；
// 轮询静默刷新走 { silent: true }，失败保留旧数据不打扰用户
const { loading, load } = useListLoader(fetchList, {
  onError: (e, { silent }) => {
    // 静默刷新失败：保留现有列表与红点，仅告警日志，不打扰用户
    if (silent) { console.warn('工单轮询刷新失败:', e); return }
    pendingCount.value = 0
    rows.value = []
    total.value = 0
    ElMessage.error('加载工单失败: ' + errMsg(e, '未知错误'))
  },
})
// 分页状态：由 usePagination 统一管理翻页后的重新加载
const { page, onPageChange, reset, guardOverflow } = usePagination(() => load())
const pendingCount = ref(0)        // 待我审批红点
const submitting = ref(false)      // 提交防重锁：防止审批通过/驳回/撤回重复提交
let pollTimer = null

const emptyText = computed(() => ({
  pending: '暂无待审批工单',
  processed: '暂无已审批记录',
  mine: '暂无工单记录',
  all: '暂无工单',
}[currentView.value] || '暂无数据'))

/* ============ 视角切换与统一加载 ============ */
function switchView(view) {
  currentView.value = view
  reset() // 切换视角回到第一页
}

// 筛选/搜索条件变化后回到第一页
function refreshCurrent() {
  reset()
}

/* ============ 统一工单列表加载（全类型） ============ */
async function fetchList() {
  // 组装查询参数：视角 + 类型 + 状态 + 搜索 + 分页
  const params = new URLSearchParams({
    view: currentView.value,
    page: page.value,
    page_size: PAGE_SIZE,
  })
  if (typeFilter.value) params.set('type', typeFilter.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  if (searchInput.value.trim()) params.set('search', searchInput.value.trim())

  const res = await api.getJson('/api/v1/auth/tickets/?' + params.toString())
  const count = res?.count || 0
  total.value = count
  // 数据量减少（如工单被处理/撤回）导致当前页越界时，回退到最后一页重新加载
  if (guardOverflow(count)) return
  rows.value = res?.rows || []
  if (currentView.value === 'pending') pendingCount.value = count || rows.value.length
}

/* ============ 行渲染辅助 ============ */
function bizTypeLabel(t) {
  return BIZ_TYPE_LABEL_MAP[t] || t || '—'
}
function bizTagType(t) {
  return BIZ_TYPE_TAG_MAP[t] || 'info'
}
// 工单状态取数：由共享映射生成 label/tagType，避免各页面重复 MAP + fallback
const { label: statusText, tagType: statusTagType } = makeStatusMeta(TICKET_STATUS_LABEL_MAP, TICKET_STATUS_TAG_MAP)

// 目标列内容：permission=目标用户+角色；config/schedule=配置项；model=模型名；
// org=目标部门/团队+操作；security=目标 IP/敏感词+类型·操作
function targetCell(t) {
  if (t.biz_type === 'permission') {
    const scopeTxt = t.scope_name || ''
    const permParts = [
      t.role_name ? t.role_name : '',
      scopeTxt ? `(${scopeTxt})` : '',
    ].filter(Boolean).join(' ')
    return {
      main: t.target_user_name || '—',
      sub: permParts || t.target_user_email || '',
    }
  }
  if (t.biz_type === 'config' || t.biz_type === 'schedule') {
    return { main: t.config_label || t.config_key || '—', sub: t.config_key || '' }
  }
  if (t.biz_type === 'model') {
    return { main: t.model_name || '—', sub: '' }
  }
  if (t.biz_type === 'org') {
    return { main: t.org_name || '—', sub: t.operation_display || '' }
  }
  if (t.biz_type === 'security') {
    const parts = [t.security_type_display || '', t.operation_display || ''].filter(Boolean).join(' · ')
    return { main: t.security_target || '—', sub: parts }
  }
  return { main: '—', sub: '' }
}

// 时间列：我已审批=处理时间（approved_at），其他=申请时间
function timeField(t) {
  if (currentView.value === 'processed' && t.approved_at) return t.approved_at
  return t.created_at
}

/* ============ 详情弹窗 ============ */
const approvalVisible = ref(false)
const currentTicket = ref(null)

const avatarChar = computed(() => (currentTicket.value ? (currentTicket.value.applicant_name || '?').charAt(0).toUpperCase() : '?'))
const isSelfApply = computed(() => {
  const t = currentTicket.value
  return t && t.applicant_id && t.target_user_id && t.applicant_id === t.target_user_id
})
// 权限范围文本：优先后端 scope_name，缺省时用 scope_type + id 拼接
const scopeText = computed(() => {
  const t = currentTicket.value
  if (!t) return '—'
  return t.scope_name || (scopeTypeLabel(t.scope_type) + (t.scope_id ? ` #${t.scope_id}` : '')) || '—'
})
// 敏感配置项：old/new 值均为 *** 时掩码展示
const isSecret = computed(() => {
  const t = currentTicket.value
  return t && t.old_value === '***' && t.new_value === '***'
})
// config/schedule 变更摘要（多值类配置 added/removed）
const configSummaryParts = computed(() => {
  const s = currentTicket.value ? currentTicket.value.change_summary : null
  if (!s) return { added: [], removed: [] }
  return { added: s.added || [], removed: s.removed || [] }
})
// org 变更前后 diff 行：changed_fields 并集驱动
const orgDiffRows = computed(() => {
  const t = currentTicket.value
  if (!t) return []
  const oldData = t.old_data || {}
  const newData = t.new_data || {}
  // 变更字段并集：新增场景只有 new，删除场景只有 old，编辑场景两者都有
  const keys = t.changed_fields && t.changed_fields.length
    ? t.changed_fields
    : (Object.keys(newData).length ? Object.keys(newData) : Object.keys(oldData))
  return keys.map(k => {
    const oldV = oldData[k] !== undefined ? String(oldData[k]) : ''
    const newV = newData[k] !== undefined ? String(newData[k]) : ''
    return {
      label: ORG_FIELD_LABELS[k] || k,
      // 新值列：删除场景显示"（删除）"
      newValue: newV === '' ? '（删除）' : newV,
      // 原值列：字段有变化才显示旧值
      oldValue: (oldV !== '' && oldV !== newV) ? oldV : '',
    }
  })
})

// 我的审批记录（"我已审批"视角）：展示当前用户在各审批节点的记录
const myApprovals = computed(() => {
  const t = currentTicket.value
  if (!t) return []
  return (t.approval_chain || []).filter(n => n.approver_id)
})

// 审批链节点状态计算：已驳回 / 已通过（含此前所有节点）/ 当前节点 / 待处理
const chainNodes = computed(() => {
  const t = currentTicket.value
  if (!t) return []
  return (t.approval_chain || []).map((n, i) => {
    const ns = n.status || ''
    const isRejected = ns === 'REJECTED'
    const isApproved = ns === 'APPROVED' || i < t.current_step
    const isCurr = !isRejected && !isApproved && i === t.current_step
    return {
      node: n,
      isRejected,
      isApproved,
      isCurr,
      statusLabel: isRejected ? '已驳回'
        : isApproved ? '已通过'
        : isCurr ? '待审批' : '待处理',
    }
  })
})

// 当前待审批人：优先审批链当前节点角色，旧结构工单按类型给提示
const currentApproverLabel = computed(() => {
  const t = currentTicket.value
  if (!t) return '待审批'
  const chain = t.approval_chain || []
  const node = chain[t.current_step] || {}
  if (node.approver_role) return approverRoleLabel(node.approver_role)
  return bizTypeLabel(t.biz_type)
})

function openTicketModal(t) {
  currentTicket.value = t
  approvalVisible.value = true
}

function onRowClick(row) {
  openTicketModal(row)
}

// 操作按钮显隐：
// - 待我审批视角 + PENDING：显示通过/驳回
// - 我的工单视角 + PENDING：显示撤回
const showActions = computed(() => {
  const t = currentTicket.value
  return currentView.value === 'pending' && t && t.status === 'PENDING'
})
const showWithdraw = computed(() => {
  const t = currentTicket.value
  return currentView.value === 'mine' && t && t.status === 'PENDING'
})

/* ============ 展示辅助 ============ */
function riskLabel(r) {
  return { normal: '普通', high: '高风险' }[r] || r || '—'
}
function approverRoleLabel(r) {
  return APPROVER_ROLE_MAP[r] || r || '—'
}
function scopeTypeLabel(st) {
  return SCOPE_TYPE_MAP[st] || st || '—'
}
function changeTypeLabel(ct) {
  return (CHANGE_TYPE_MAP[ct] || {}).label || ct || '—'
}
function changeTypeTagType(ct) {
  return (CHANGE_TYPE_MAP[ct] || {}).type || 'info'
}
function modelFieldLabel(f) {
  return MODEL_FIELD_LABELS[f] || f
}

/* ============ 审批动作（通过/驳回/撤回） ============ */
async function onApproveClick() {
  if (!currentTicket.value || showActions.value === false) return
  const t = currentTicket.value
  try {
    const { value: comment } = await ElMessageBox.prompt(
      `确认通过工单 ${t.ticket_no}？通过后将按审批链流转。`,
      '确认通过审批',
      {
        confirmButtonText: '确认通过',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '可填写备注说明，记录审批意见...',
        type: 'success',
      }
    )
    await submitTicketAction('approve', t.id, (comment || '').trim())
  } catch (e) {
    // 用户取消输入框时不提示错误
    if (e === 'cancel' || e === 'close') return
    ElMessage.error('操作失败: ' + errMsg(e, '未知错误'))
  }
}

async function onRejectClick() {
  if (!currentTicket.value || showActions.value === false) return
  const t = currentTicket.value
  try {
    // 驳回理由必填：便于申请人了解问题
    const { value: comment } = await ElMessageBox.prompt(
      `确认驳回工单 ${t.ticket_no}？驳回后工单将终止流转。`,
      '驳回理由',
      {
        confirmButtonText: '确认驳回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '必填，请说明驳回原因，便于申请人了解问题...',
        inputValidator: v => (v && v.trim() ? true : '驳回理由不能为空'),
        inputErrorMessage: '驳回理由不能为空',
        type: 'warning',
      }
    )
    await submitTicketAction('reject', t.id, comment.trim())
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error('操作失败: ' + errMsg(e, '未知错误'))
  }
}

async function onWithdrawClick() {
  if (!currentTicket.value || showWithdraw.value === false) return
  const t = currentTicket.value
  try {
    const { value: comment } = await ElMessageBox.prompt(
      `确认撤回工单 ${t.ticket_no}？撤回后工单将终止流转。`,
      '确认撤回工单',
      {
        confirmButtonText: '确认撤回',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '可填写撤回原因...',
        type: 'info',
      }
    )
    await submitTicketAction('withdraw', t.id, (comment || '').trim())
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error('操作失败: ' + errMsg(e, '未知错误'))
  }
}

// 统一提交审批动作：通过/驳回/撤回共用（api.postJson 非 2xx 已抛错，此处 then 必然成功）
async function submitTicketAction(action, id, comment) {
  if (submitting.value) return
  submitting.value = true
  try {
    const res = await api.postJson(`/api/v1/auth/tickets/${id}/${action}/`, { comment })
    const msg = {
      // 多节点审批链首次通过时 status 仍为 PENDING，此时提示等待后续节点而非"待审批"
      approve: (res?.status && res.status !== 'PENDING')
        ? `工单已通过，状态：${statusText(res.status)}`
        : '工单已通过，等待后续节点处理',
      reject: '工单已驳回',
      withdraw: '工单已撤回',
    }[action]
    ElMessage.success(msg)
    approvalVisible.value = false
    currentTicket.value = null
    await load()
  } catch (err) {
    ElMessage.error('操作失败: ' + errMsg(err, '未知错误'))
  } finally {
    submitting.value = false
  }
}

/* ============ 自动轮询刷新 ============
 * 10 分钟固定间隔；页面切到后台暂停，回到前台立即刷新并恢复。
 */
function startPolling() {
  stopPolling()
  pollTimer = setInterval(pollRefresh, POLL_INTERVAL)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// 轮询刷新：静默刷新当前列表；非待办视角下额外轻量刷新待办红点，
// 保证切到"我已审批/我的工单/全部"视角时红点仍能随轮询自动更新
function pollRefresh() {
  load({ silent: true })
  if (currentView.value !== 'pending') refreshPendingBadge()
}

// 轻量获取待办数：view=pending + page_size=1 仅取 count（1 条返回体），不扰动当前列表
async function refreshPendingBadge() {
  try {
    const params = new URLSearchParams({ view: 'pending', page: '1', page_size: '1' })
    const res = await api.getJson('/api/v1/auth/tickets/?' + params.toString())
    pendingCount.value = res?.count || 0
  } catch (err) {
    console.warn('待办红点刷新失败:', err)
  }
}

function onVisibilityChange() {
  if (document.hidden) {
    stopPolling()
  } else {
    pollRefresh()
    startPolling()
  }
}

/* ============ 初始化 ============ */
onMounted(() => {
  // 合规管理员默认看"全部工单"（审计视角，不参与审批）
  const isPureCompliance = userStore.hasAnyRole('compliance_admin')
    && !userStore.hasAnyRole('super_admin', 'user_admin', 'dept_manager', 'team_leader', 'kb_admin')
  if (isPureCompliance) currentView.value = 'all'
  load()
  startPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style scoped>
/* ===== 页头与卡片 ===== */
.ticket-card {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1;
  /* 剔除全局 .app-card 的默认 padding/margin（对齐旧版 ticket.html 的 padding:0;margin:0），
     卡片内部间距由 toolbar / 表格滚动区 / 分页各自控制 */
  padding: 0;
  margin: 0;
}

/* ===== 工具栏 =====
   对齐旧版 ticket.html 的工具栏：通栏 + 仅底部边框，卡片内不再有内边距 */
.ticket-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  padding: 12px;
  border-bottom: 1px solid var(--app-border);
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-left: auto;
}

/* 待办红点：嵌在"待我审批"tab 内 */
.view-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 8px;
  background: #f56c6c;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
}

/* ===== 表格与分页 ===== */
.ticket-table {
  width: 100%;
}

.ticket-table :deep(.el-table__row) {
  cursor: pointer;
}

/* 表格内等宽文本（工单号等）：全局 .mono 基础上补 12px 字号 */
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

/* ===== 详情弹窗 ===== */
/* 位于 BaseDialog 的 body 内：height:100% 撑满弹窗可用高度并内部滚动（同 DocPreviewDialog 用法） */
.approval-body {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}

/* 申请人卡片 */
.applicant-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  background: var(--app-menu-hover);
  border-radius: 8px;
  border: 1px solid var(--app-border);
  margin-bottom: 14px;
}

.applicant-avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: #409eff;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  font-weight: 600;
  flex-shrink: 0;
}

.applicant-info {
  flex: 1;
  min-width: 0;
}

.applicant-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
}

.applicant-meta {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.applicant-time {
  text-align: right;
  font-size: 13px;
  color: var(--app-text);
  flex-shrink: 0;
}

.applicant-time-label {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 2px;
}

/* 详情区块标题与网格 */
.detail-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
  margin: 16px 0 10px;
  padding-left: 8px;
  border-left: 3px solid #409eff;
  line-height: 1.2;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px 20px;
}

.detail-cell-label {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 4px;
}

.detail-cell-value {
  font-size: 13px;
  color: var(--app-text);
  word-break: break-all;
}

.detail-cell-sub {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-top: 2px;
}

.full-cell {
  grid-column: 1 / -1;
}

.word-break {
  word-break: break-all;
}

/* 申请理由引用块 */
.reason-box {
  background: #fffbeb;
  border-left: 3px solid #e6a23c;
  border-radius: 0 6px 6px 0;
  padding: 12px 16px;
  font-size: 13px;
  color: var(--app-text);
  line-height: 1.7;
  word-break: break-all;
  white-space: pre-wrap;
}

/* 深色模式：浅色底引用块改为暗色半透明底，避免刺眼 */
html.dark .reason-box {
  background: rgba(230, 162, 60, 0.12);
  border-left-color: rgba(230, 162, 60, 0.55);
}

/* 角色变更箭头 */
.role-arrow {
  margin: 0 6px;
}

/* 模型变更字段标签 */
.field-tag {
  margin: 2px 6px 2px 0;
}

/* 变更摘要（多值类配置） */
.summary-line {
  font-size: 13px;
  line-height: 1.6;
}

.summary-added {
  color: #16a34a;
}

.summary-removed {
  color: #dc2626;
}

/* 敏感配置项掩码提示 */
.secret-hint {
  margin-top: 8px;
  font-size: 12px;
}

/* 审批链时间线 */
.chain-timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  position: relative;
}

.chain-timeline::before {
  content: '';
  position: absolute;
  left: 8px;
  top: 12px;
  bottom: 12px;
  width: 2px;
  background: var(--app-border);
  border-radius: 1px;
}

.chain-timeline > li {
  position: relative;
  padding: 0 0 18px 30px;
}

.chain-timeline > li:last-child {
  padding-bottom: 0;
}

/* 节点圆点：4 种状态（已通过绿 / 已驳回红 / 当前蓝 / 待处理灰） */
.chain-node-dot {
  position: absolute;
  left: 2px;
  top: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  z-index: 3;
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px var(--app-border);
}

.dot-done {
  background: #22c55e;
}

.dot-rejected {
  background: #ef4444;
}

.dot-curr {
  background: #2563eb;
  box-shadow: 0 0 0 3px #dbeafe;
}

.dot-pending {
  background: var(--app-card-bg);
  border: 2px solid var(--app-border);
}

.chain-node-role {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
}

.chain-node-status {
  display: inline-block;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  margin-left: 6px;
  font-weight: 500;
}

.chain-node-status.done {
  background: #ecfdf5;
  color: #16a34a;
}

.chain-node-status.curr {
  background: #dbeafe;
  color: #2563eb;
}

.chain-node-status.pending {
  background: var(--app-bg);
  color: var(--app-text-sub);
}

.chain-node-status.rejected {
  background: #fef2f2;
  color: #f56c6c;
}

.chain-node-approver {
  font-size: 12px;
  color: #409eff;
  margin-top: 2px;
  font-weight: 500;
}

.chain-node-comment {
  font-size: 13px;
  color: var(--app-text);
  background: var(--app-bg);
  padding: 6px 10px;
  border-radius: 6px;
  line-height: 1.5;
  margin-top: 6px;
}

.chain-node-time {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  margin-top: 4px;
}

/* 当前审批人提示条 */
.current-approver-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: #ecf5ff;
  border-radius: 6px;
  font-size: 13px;
  color: #409eff;
  margin-top: 16px;
  font-weight: 500;
}

/* 深色模式：提示条改为暗色半透明底 + 浅蓝文字，避免亮蓝底刺眼 */
html.dark .current-approver-bar {
  background: rgba(64, 158, 255, 0.12);
  color: #79bbff;
}

/* 我的审批记录卡片 */
.my-approval-box {
  background: #ecf5ff;
  border-radius: 6px;
  padding: 12px 14px;
  border-left: 3px solid #409eff;
  margin-bottom: 8px;
}

/* 详情弹窗底部按钮 */
.approval-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
