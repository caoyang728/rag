<template>
  <div class="page-container admin-docs-page">
    <!-- ===== 页头 ===== -->
    <div class="page-header">
      <div>
        <div class="page-title">文档审核</div>
        <div class="page-desc">文档双审流程处理（团队组长审核 / 合规复核）</div>
      </div>
    </div>

    <!-- ===== 列表卡片：toolbar 固定 + 表格滚动 ===== -->
    <div class="page-body">
    <div class="app-card doc-list-card">
      <!-- 工具条：tab 切换 + 阶段筛选 + 搜索 + 刷新 -->
      <div class="toolbar">
        <el-radio-group v-model="auditTab" @change="onTabChange">
          <el-radio-button value="pending">待审核</el-radio-button>
          <el-radio-button value="rejected">已驳回</el-radio-button>
          <el-radio-button value="records">审核记录</el-radio-button>
        </el-radio-group>
        <div class="toolbar-right">
          <!-- 阶段筛选：仅待审核 tab 生效（待审核 / 待复核） -->
          <el-select
            v-if="auditTab === 'pending'"
            v-model="auditStage"
            placeholder="全部阶段"
            style="width: 110px"
            @change="onAuditFilterChange"
          >
            <el-option label="全部阶段" value="" />
            <el-option label="待审核" value="pending_team" />
            <el-option label="待复核" value="pending_compliance" />
          </el-select>
          <el-input
            v-model="searchKeyword"
            placeholder="搜索标题 / 文件名 / 上传人"
            clearable
            style="width: 220px"
            @input="onAuditSearchInput"
            @clear="onAuditSearchCommit"
            @keyup.enter="onAuditSearchCommit"
          />
          <el-button @click="load()">刷新</el-button>
        </div>
      </div>

      <!-- 表格滚动区：固定表头 + 表体内部滚动 -->
      <div class="page-scroll pad-x-12">
      <!-- 审核记录列与其他 tab 列不同 -->
      <el-table
        v-if="auditTab === 'records'"
        :data="docs"
        v-loading="listLoading"
        class="doc-table"
        size="default"
        height="100%"
      >
        <el-table-column label="文档标题" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="text-strong">{{ row.document_title || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-tag :type="recordActionTag(row.action_label)" size="small" effect="plain">{{ row.action_label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作人" width="120">
          <template #default="{ row }"><span class="text-sub">{{ row.operator_name || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="审批意见" min-width="180" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sub">{{ row.comment || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
        </el-table-column>
        <template #empty>
          <el-empty :description="emptyTip" :image-size="60" />
        </template>
      </el-table>

      <!-- 待审核 / 已驳回列表（行点击打开审核详情弹窗） -->
      <el-table
        v-else
        :data="docs"
        v-loading="listLoading"
        class="doc-table"
        size="default"
        height="100%"
        row-class-name="doc-row-hover"
        @row-click="onRowClick"
      >
        <el-table-column label="文档标题" min-width="200">
          <template #default="{ row }">
            <div class="flex items-center gap-8">
              <span class="doc-file-icon">{{ iconForFileType(row.file_type) }}</span>
              <div class="doc-title-cell">
                <div class="text-strong">{{ row.title }}</div>
                <!-- 文件名与标题相同时不重复展示（避免标题下方文件名重复出现） -->
                <div v-if="row.file_name && row.file_name !== row.title" class="text-sub text-xs">{{ row.file_name }}</div>
                <!-- 已驳回文档在标题下方展示驳回理由，便于一眼定位问题 -->
                <div v-if="row.reject_comment" class="text-xs reject-comment">驳回：{{ row.reject_comment }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="140">
          <template #default="{ row }"><span class="text-sm">{{ row.file_type || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="密级" width="90">
          <template #default="{ row }">
            <!-- 密级 1（普通）原版无徽章，纯文本展示；2~4 用不同颜色徽章区分 -->
            <el-tag v-if="row.secret_level > 1" :type="secLvTagType(row.secret_level)" size="small" effect="plain">{{ secLvMap[row.secret_level] }}</el-tag>
            <span v-else-if="secLvMap[row.secret_level]">{{ secLvMap[row.secret_level] }}</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="上传人" width="110" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sm">{{ row.owner_username || row.owner_name || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="归属" width="180" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sm belong-text">{{ belongText(row) || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="阶段" width="100">
          <template #default="{ row }">
            <el-tag :type="stageTag(row).type" size="small" effect="plain">{{ stageTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="140">
          <template #default="{ row }"><span class="text-sm text-sub">{{ formatDate(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <!-- 已驳回文档仅可查看详情，不提供"处理"入口 -->
            <el-button v-if="row.audit_status === 'rejected'" link type="info" size="small" @click.stop="onRowClick(row)">查看</el-button>
            <el-button v-else link type="primary" size="small" @click.stop="onRowClick(row)">处理</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="emptyTip" :image-size="60" />
        </template>
      </el-table>
      </div>

      <!-- 分页：后端按 page_size 切片，固定每页条数（不提供每页条数切换） -->
      <AppPagination
        class="doc-pagination"
        :total="auditTotal"
        :page-size="auditPageSize"
        :page="auditPage"
        @page-change="onPageChange"
      />
    </div>
    </div>

    <!-- ===== 文档审核详情弹窗（复用公共 BaseDialog） ===== -->
    <BaseDialog
      v-model="detailVisible"
      :title="detailTitle"
      width="680px"
      min-width="680px"
      :close-on-click-modal="false"
    >
      <div v-if="currentDoc" class="doc-detail-body">
        <!-- 申请人卡片：头像 + 姓名/账号 + 当前阶段标签 + 上传时间 -->
        <div class="applicant-card">
          <div class="applicant-avatar">{{ avatarChar(currentDoc) }}</div>
          <div class="applicant-info">
            <div class="applicant-name-line">
              <span class="applicant-name">{{ currentDoc.owner_name }}</span>
              <el-tag :type="detailStageTag.type" size="small" effect="plain">{{ detailStageTag.text }}</el-tag>
            </div>
            <div class="applicant-meta">账号：{{ currentDoc.owner_username || '—' }}</div>
          </div>
          <div class="applicant-time">
            <div class="applicant-time-label">上传时间</div>
            <div class="applicant-time-value">{{ formatDate(currentDoc.created_at) }}</div>
          </div>
        </div>

        <div class="detail-section-title">文档信息</div>
        <div class="detail-grid">
          <div class="detail-cell detail-cell-full">
            <div class="detail-cell-label">文档标题</div>
            <div class="doc-title-row">
              <div class="doc-title-main">
                <div class="doc-title-line">
                  <span class="doc-file-icon">{{ iconForFileType(currentDoc.file_type) }}</span>
                  <div class="doc-title-text">
                    <div class="detail-cell-value">{{ currentDoc.title }}</div>
                    <div v-if="currentDoc.file_name && currentDoc.file_name !== currentDoc.title" class="detail-cell-sub">{{ currentDoc.file_name }}</div>
                  </div>
                </div>
              </div>
              <!-- 预览按钮：唤起公共预览弹窗 -->
              <el-button size="small" class="doc-preview-btn" @click="openPreview(currentDoc.id)">👁 预览</el-button>
            </div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">当前阶段</div>
            <div class="detail-cell-value">
              <el-tag :type="detailStageTag.type" size="small" effect="plain">{{ detailStageTag.text }}</el-tag>
            </div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">版本</div>
            <div class="detail-cell-value">{{ versionText(currentDoc) }}</div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">文件类型</div>
            <div class="detail-cell-value">{{ currentDoc.file_type || '—' }} · {{ fileSizeText(currentDoc) }}</div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">可见性</div>
            <div class="detail-cell-value">{{ visMap[currentDoc.visibility_level] || '—' }}</div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">密级</div>
            <div class="detail-cell-value">{{ secLvMap[currentDoc.secret_level] || '—' }}</div>
          </div>
          <div class="detail-cell">
            <div class="detail-cell-label">归属路径</div>
            <div class="detail-cell-value">
              {{ belongText(currentDoc) || '—' }}
              <span v-if="currentDoc.node_name" class="detail-cell-sub">（节点：{{ currentDoc.node_name }}）</span>
            </div>
          </div>
          <!-- 已驳回：展示驳回理由与时间 -->
          <div v-if="currentDoc.audit_status === 'rejected'" class="detail-cell detail-cell-full">
            <div class="detail-cell-label">驳回理由</div>
            <div class="detail-cell-value reject-reason">{{ currentDoc.reject_comment || '—' }}</div>
            <div class="detail-cell-sub">驳回时间：{{ currentDoc.rejected_at ? formatDate(currentDoc.rejected_at) : '—' }}</div>
          </div>
        </div>

        <div class="detail-section-title">敏感内容检测</div>
        <!-- 检测中 -->
        <div v-if="scanState === 'loading'" class="doc-scan-loading">
          <el-icon class="is-loading" :size="16"><Loading /></el-icon>
          <span>检测中...</span>
        </div>
        <!-- 有命中：统计 + 详细片段 -->
        <div v-else-if="scanState === 'result'" class="doc-scan-wrap">
          <div class="doc-scan-summary">
            <div class="doc-scan-summary-title">⚠ 共检测到 {{ scanTotal }} 处敏感内容</div>
            <ul class="doc-scan-stats-list">
              <li v-for="c in scanCategories" :key="c.label" class="doc-scan-stats-item">
                <span>{{ c.label }}</span>
                <span class="doc-scan-stats-count">{{ c.count }}</span>
              </li>
            </ul>
          </div>
          <div class="doc-scan-detail">
            <div class="doc-scan-detail-title">详细片段</div>
            <div class="doc-scan-frags">
              <div v-for="(f, i) in scanFragments" :key="i" class="doc-scan-frag">
                <div class="doc-scan-frag-head">
                  <span class="doc-scan-cat">{{ f.label }}</span>
                  <span v-if="f.count > 1" class="doc-scan-frag-count">共 {{ f.count }} 处</span>
                </div>
                <!-- ctx 保留原文换行/空格，命中词高亮标记 -->
                <div class="doc-scan-frag-ctx">{{ f.context_before }}<mark class="doc-scan-mark">{{ f.matched }}</mark>{{ f.context_after }}</div>
              </div>
            </div>
            <div v-if="scanTruncated" class="text-sub text-xs doc-scan-truncated">片段较多，仅展示前 30 条</div>
          </div>
        </div>
        <!-- 无命中 -->
        <div v-else-if="scanState === 'clean'" class="doc-scan-clean">
          <span class="doc-scan-clean-icon">✓</span><span>未检测到敏感内容</span>
        </div>
        <!-- 检测失败：非阻断提示（不影响审核操作） -->
        <div v-else-if="scanState === 'error'" class="doc-scan-todo">
          <span class="doc-scan-todo-icon">⚠</span><span>敏感内容检测失败：{{ scanError }}</span>
        </div>
      </div>

      <!-- 已驳回文档不可再审核：隐藏通过/拒绝按钮 -->
      <template #footer>
        <div class="doc-dialog-footer">
          <template v-if="currentDoc && currentDoc.audit_status !== 'rejected'">
            <el-button type="success" :loading="submitting" @click="onDocApproveClick">通过</el-button>
            <el-button type="danger" :loading="submitting" @click="onDocRejectClick">拒绝</el-button>
            <el-button @click="detailVisible = false">取消</el-button>
          </template>
          <el-button v-else @click="detailVisible = false">关闭</el-button>
        </div>
      </template>
    </BaseDialog>

    <!-- ===== 文档预览弹窗（公共组件 DocPreviewDialog，与 Chat/Upload/AdminNodes 共用） ===== -->
    <DocPreviewDialog v-model="previewVisible" :doc-id="previewDocId" :initial-page="previewInitialPage" />
  </div>
</template>

<script setup>
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElInput, ElMessage, ElMessageBox } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, formatFileSize, errMsg } from '../utils/format'
import { debounce } from '../utils/debounce'
import { usePagination } from '../composables/usePagination'
import { useListLoader } from '../composables/useListLoader'
import BaseDialog from '../components/base/BaseDialog.vue'
import DocPreviewDialog from '../components/doc-preview/DocPreviewDialog.vue'
import AppPagination from '../components/base/AppPagination.vue'

const userStore = useUserStore()
const router = useRouter()

const DOC_API = '/api/v1/knowledge/documents'

/* ==========================================================
   常量映射（与旧 admin-docs.js 保持一致）
   ========================================================== */
// 密级映射：1=普通, 2=内部, 3=机密, 4=绝密
const secLvMap = { 1: '普通', 2: '内部', 3: '机密', 4: '绝密' }
// 可见性枚举文案（TEAM_ONLY/DEPT_ONLY/PUBLIC）
const visMap = { PUBLIC: '全局公开', DEPT_ONLY: '部门内可见', TEAM_ONLY: '团队内可见' }

/* ==========================================================
   页面级权限：仅 super_admin / kb_admin / dept_manager / team_leader 可见
   普通用户直接跳回首页并提示无权限（与旧版前后端双重校验一致）
   ========================================================== */
function canAccessPage() {
  return userStore.hasAnyRole('super_admin', 'kb_admin', 'dept_manager', 'team_leader')
}

/* ==========================================================
   Tab 列表状态
   ========================================================== */
// 当前 tab：pending=待审核 / rejected=已驳回 / records=审核记录
const auditTab = ref('pending')
// 列表加载：由 useListLoader 统一管理 loading/请求序号守卫/错误提示（错误文案固定，用 onError 覆盖默认前缀）
const { loading: listLoading, load } = useListLoader(fetchAuditList, {
  onError: (e) => { console.error(e); ElMessage.error('加载文档列表失败') },
})
// 分页状态：由 usePagination 统一管理翻页后的重新加载
const { page: auditPage, pageSize: auditPageSize, onPageChange, reset, guardOverflow } = usePagination(() => load(), { initialSize: 20 })
const auditTotal = ref(0)
const docs = ref([])
// 搜索关键字（所有 tab 生效）
const searchKeyword = ref('')
// 阶段筛选：pending_team=待审核 / pending_compliance=待复核（仅待审核 tab 生效）
const auditStage = ref('')
// 当前正在审核的文档对象
const currentDoc = ref(null)
const detailVisible = ref(false)
// 提交防重锁（防止审核通过/驳回重复提交）
const submitting = ref(false)

/** 当前 tab 的接口地址（pending/rejected/records 三个列表接口） */
function auditApiUrl() {
  return {
    pending: DOC_API + '/pending-audits/',
    rejected: DOC_API + '/audit-rejected/',
    records: DOC_API + '/audit-records/'
  }[auditTab.value]
}

/** 空列表提示文案 */
const emptyTip = computed(() => ({
  pending: '暂无待审核文档',
  rejected: '暂无已驳回文档',
  records: '暂无审核记录'
}[auditTab.value]))

/** 列表加载：由 useListLoader 包装，本函数只负责组装参数与写入状态（页码由 usePagination 维护） */
async function fetchAuditList() {
  // 组装查询参数：分页 + 搜索关键字 + 阶段筛选（阶段筛选仅待审核 tab 生效）
  const params = new URLSearchParams({ page: auditPage.value, page_size: auditPageSize.value })
  if (searchKeyword.value) params.set('keyword', searchKeyword.value)
  if (auditTab.value === 'pending' && auditStage.value) params.set('status', auditStage.value)

  const res = await api.getJson(auditApiUrl() + '?' + params.toString())
  docs.value = res?.rows || []
  auditTotal.value = res?.count || 0
  // 数据量减少导致当前页越界时，回退到最后一页重新加载
  if (guardOverflow(auditTotal.value)) return
}

/** 切换 tab：重置页码并加载；同时清空搜索与阶段筛选，避免旧条件串到其他列表 */
function onTabChange(tab) {
  auditTab.value = tab
  searchKeyword.value = ''
  auditStage.value = ''
  reset()
}

/** 搜索输入（300ms 防抖，避免每次按键都发请求；由 utils/debounce 统一管理定时器） */
const onAuditSearchInput = debounce(onAuditSearchCommit, 300)

/** 提交搜索条件并重新加载（回车/清空也走这里） */
function onAuditSearchCommit() {
  // 去除首尾空格，避免无效条件触发请求
  searchKeyword.value = (searchKeyword.value || '').trim()
  reset()
}

/** 阶段筛选变化（待审核 / 待复核，仅待审核 tab 展示） */
function onAuditFilterChange() {
  reset()
}

/** 行点击：打开审核详情弹窗（审核记录行不弹窗） */
function onRowClick(row) {
  if (auditTab.value === 'records') return
  openDocModal(row)
}

/** 审核记录操作徽章颜色 */
function recordActionTag(label) {
  return {
    '审核通过': 'info',
    '复核通过': 'success',
    '驳回': 'danger'
  }[label] || 'info'
}

/** 归属文案：部门 / 团队 */
function belongText(d) {
  return [d.dept_name, d.team_name].filter(Boolean).join(' / ')
}

/** 密级徽章颜色（1 普通无高亮，2 内部蓝，3 机密橙，4 绝密红） */
function secLvTagType(level) {
  return { 1: 'info', 2: 'info', 3: 'warning', 4: 'danger' }[level] || 'info'
}

/** 阶段：待审核(橙)/待复核(蓝)/已驳回(红)，用不同颜色徽章区分流程阶段 */
function stageTag(d) {
  if (d.audit_status === 'rejected') return { type: 'danger', text: '已驳回' }
  if (d.audit_status === 'pending_compliance') return { type: 'info', text: '待复核' }
  return { type: 'warning', text: '待审核' }
}

/** 文件类型 → emoji 图标 */
function iconForFileType(ft) {
  const f = (ft || '').toLowerCase()
  if (f === 'pdf') return '📄'
  if (['doc', 'docx'].includes(f)) return '📝'
  if (['xls', 'xlsx'].includes(f)) return '📊'
  if (['ppt', 'pptx'].includes(f)) return '📽️'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(f)) return '🖼️'
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(f)) return '🗜️'
  if (['txt', 'md'].includes(f)) return '📃'
  return '📁'
}

/* ==========================================================
   审核详情弹窗
   ========================================================== */
/** 弹窗标题：按审核状态生成（已驳回 → 文档详情，其余 → 文档审核） */
const detailTitle = computed(() => {
  const d = currentDoc.value
  if (!d) return '文档审核'
  if (d.audit_status === 'rejected') return '文档详情 · ' + d.title
  return '文档审核 · ' + d.title
})

/** 上传人姓名首字作为头像占位 */
function avatarChar(d) {
  return (d.owner_name || '?').charAt(0).toUpperCase()
}

/** 版本：version_tag 已含 v 前缀（如 v1），无标签时兜底 v{version} */
function versionText(d) {
  return d.version_tag || ('v' + (d.version || 1))
}

/** 文件大小展示（无大小时占位） */
function fileSizeText(d) {
  return d.file_size ? formatFileSize(d.file_size) : '—'
}

/** 弹窗内当前阶段标签 */
const detailStageTag = computed(() => stageTag(currentDoc.value || {}))

function openDocModal(d) {
  currentDoc.value = d
  detailVisible.value = true
  // 弹窗打开后异步加载敏感内容检测结果（不阻塞审核操作）
  loadSensitiveScan(d.id)
}

/* ==========================================================
   敏感内容检测 —— 弹窗内自动扫描（敏感词/手机号/邮箱/IP/身份证/银行卡）
   打开弹窗后异步加载检测结果：不阻塞审核操作，失败仅展示占位提示
   ========================================================== */
const scanState = ref('loading')   // loading | result | clean | error
const scanError = ref('')
const scanTotal = ref(0)
const scanCategories = ref([])
const scanFragments = ref([])
const scanTruncated = ref(false)

async function loadSensitiveScan(docId) {
  scanState.value = 'loading'
  scanError.value = ''
  try {
    const res = await api.getJson(DOC_API + '/' + docId + '/sensitive-scan/')
    // 弹窗已切换到其他文档时丢弃过期结果
    if (currentDoc.value && currentDoc.value.id !== docId) return
    if (res?.ok !== true) {
      scanState.value = 'error'
      scanError.value = res?.detail || '检测失败'
      return
    }
    scanTotal.value = res.total || 0
    scanCategories.value = res.categories || []
    scanFragments.value = res.fragments || []
    scanTruncated.value = !!res.truncated
    scanState.value = scanTotal.value > 0 ? 'result' : 'clean'
  } catch (err) {
    scanState.value = 'error'
    scanError.value = errMsg(err, '检测服务异常')
  }
}

/* ==========================================================
   审核动作 —— 通过 / 驳回
   ========================================================== */
/** 审核通过（二次确认，备注选填） */
function onDocApproveClick() {
  const d = currentDoc.value
  if (!d || submitting.value) return
  const comment = ref('')
  ElMessageBox({
    title: '确认通过审核',
    type: 'success',
    message: () => h('div', [
      h('div', { class: 'adm-msgbox-banner' }, '确认通过文档《' + d.title + '》？'),
      h('div', { class: 'adm-msgbox-form' }, [
        h('label', { class: 'adm-msgbox-label' }, '审批意见（选填）'),
        h(ElInput, {
          type: 'textarea',
          rows: 3,
          placeholder: '可填写备注说明，记录审批意见...',
          modelValue: comment.value,
          'onUpdate:modelValue': (v) => { comment.value = v }
        })
      ])
    ]),
    confirmButtonText: '确认通过',
    cancelButtonText: '取消',
    beforeClose: (action, instance, done) => {
      if (action === 'confirm') {
        submitDocApprove(d.id, comment.value.trim())
      }
      done()
    }
  }).catch(() => {})
}

/** 审核驳回（二次确认，理由必填，支持 Ctrl+Enter 提交） */
function onDocRejectClick() {
  const d = currentDoc.value
  if (!d || submitting.value) return
  const comment = ref('')
  ElMessageBox({
    title: '驳回理由',
    type: 'error',
    message: () => h('div', [
      h('div', { class: 'adm-msgbox-banner' }, '确认驳回文档《' + d.title + '》？驳回后需上传人重新提交。'),
      h('div', { class: 'adm-msgbox-form' }, [
        h('label', { class: 'adm-msgbox-label' }, '驳回理由 '),
        h('span', { class: 'adm-msgbox-required' }, '*'),
        h(ElInput, {
          type: 'textarea',
          rows: 4,
          placeholder: '必填，请说明驳回原因，便于申请人了解问题...',
          modelValue: comment.value,
          'onUpdate:modelValue': (v) => { comment.value = v },
          // Ctrl/Cmd + Enter 快捷提交驳回（与旧版一致）
          onKeydown: (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
              const btn = document.querySelector('.el-message-box__btns .el-button--primary')
              if (btn) btn.click()
            }
          }
        })
      ])
    ]),
    confirmButtonText: '确认驳回',
    cancelButtonText: '取消',
    beforeClose: (action, instance, done) => {
      if (action === 'confirm') {
        // 驳回理由必填，空则拦截并提示（不关闭弹窗）
        if (!comment.value.trim()) {
          ElMessage.warning('驳回理由不能为空')
          return
        }
        submitDocReject(d.id, comment.value.trim())
      }
      done()
    }
  }).catch(() => {})
}

/** 提交：文档通过 */
function submitDocApprove(id, comment) {
  if (submitting.value) return
  submitting.value = true
  api.postJson(DOC_API + '/' + id + '/audit-approve/', { comment })
    .then(res => {
      if (res?.ok) {
        // 复核通过 → 已发布；审核通过 → 流转复核
        const nextLabel = res.audit_status === 'passed'
          ? '审核通过（已发布）'
          : '审核通过，流转至：' + auditStatusLabel(res.audit_status)
        ElMessage.success(nextLabel)
        detailVisible.value = false
        currentDoc.value = null
        load()
      } else {
        ElMessage.error(res?.detail || '审核失败')
      }
    })
    .catch(err => {
      ElMessage.error(errMsg(err, '审核失败'))
      console.error(err)
    })
    .finally(() => { submitting.value = false })
}

/** 提交：文档驳回 */
function submitDocReject(id, comment) {
  if (submitting.value) return
  submitting.value = true
  api.postJson(DOC_API + '/' + id + '/audit-reject/', { comment })
    .then(res => {
      if (res?.ok) {
        ElMessage.success('文档已驳回')
        detailVisible.value = false
        currentDoc.value = null
        load()
      } else {
        ElMessage.error(res?.detail || '驳回失败')
      }
    })
    .catch(err => {
      ElMessage.error(errMsg(err, '驳回失败'))
      console.error(err)
    })
    .finally(() => { submitting.value = false })
}

/** 审核状态文案映射 */
function auditStatusLabel(s) {
  return {
    'pending_team': '待审核',
    'pending_compliance': '待复核',
    'passed': '已通过',
    'rejected': '已驳回',
    'archived': '已归档',
    'deleted': '已删除'
  }[s] || s
}

/* ==========================================================
   文档预览（公共组件 DocPreviewDialog：显隐与打开参数）
   ========================================================== */
const previewVisible = ref(false)
const previewDocId = ref(null)        // 当前预览文档 ID
const previewInitialPage = ref(1)     // 打开预览定位页（image 为页号）

// 打开文档预览弹窗（默认第 1 页）
function openPreview(id) {
  if (!id) return
  previewDocId.value = id
  previewInitialPage.value = 1
  previewVisible.value = true
}

/* ==========================================================
   页面初始化
   ========================================================== */
onMounted(() => {
  userStore.restore()
  // 页面级权限校验：仅管理角色可进入
  if (!canAccessPage()) {
    ElMessage.error('您没有权限访问文档审核')
    setTimeout(() => { router.replace('/') }, 800)
    return
  }
  // 加载待审核文档列表
  load()
})

onBeforeUnmount(() => {
  onAuditSearchInput.cancel()
})
</script>

<style scoped>
/* ===== 列表卡片：表格与工具条左右留白与卡片边缘对齐（复用 .app-card 底，对齐 ticket 页面 body 布局） ===== */
.doc-list-card {
  display: flex;
  flex-direction: column;
  padding: 0;
  margin: 0;
  overflow: hidden;
  flex: 1;
  min-height: 0;
}

.doc-table {
  flex: 1;
  min-height: 0;
}

/* 行点击打开详情：悬停提示可点 */
.doc-row-hover {
  cursor: pointer;
}

.doc-file-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.doc-title-cell {
  min-width: 0;
}

.reject-comment {
  color: #f56c6c;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.belong-text {
  white-space: nowrap;
}

.doc-pagination {
  margin-top: 14px;
  justify-content: flex-end;
  padding: 0 12px 12px;
  flex-shrink: 0;
}

/* ===== 审核详情弹窗 ===== */
/* 位于 BaseDialog 的 body 内：height:100% 撑满弹窗可用高度并内部滚动 */
.doc-detail-body {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
  /* 细滚动条：弹窗内容较长，尽量少遮挡正文（非常细） */
  scrollbar-width: thin;
  scrollbar-color: rgba(144, 147, 153, 0.4) transparent;
}

.doc-detail-body::-webkit-scrollbar {
  width: 4px;
  height: 4px;
}

.doc-detail-body::-webkit-scrollbar-thumb {
  background: rgba(144, 147, 153, 0.35);
  border-radius: 2px;
}

.doc-detail-body::-webkit-scrollbar-thumb:hover {
  background: rgba(144, 147, 153, 0.6);
}

.doc-detail-body::-webkit-scrollbar-track {
  background: transparent;
}

.detail-section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 16px 0 12px;
  color: var(--app-text);
  /* 与工单详情弹窗一致：左侧蓝色强调条 */
  padding-left: 8px;
  border-left: 3px solid #409eff;
  line-height: 1.2;
}

/* 申请人卡片：浅蓝渐变底 + 蓝色渐变头像 */
.applicant-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: linear-gradient(135deg, rgba(64, 158, 255, 0.08), rgba(64, 158, 255, 0.02));
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 14px 16px;
}

.applicant-avatar {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: linear-gradient(135deg, #409eff, #79bbff);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 600;
  flex-shrink: 0;
}

.applicant-info {
  flex: 1;
  min-width: 0;
}

.applicant-name-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.applicant-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
}

.applicant-meta {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-top: 2px;
}

.applicant-time {
  text-align: right;
  flex-shrink: 0;
}

.applicant-time-label {
  font-size: 12px;
  color: var(--app-text-sub);
}

.applicant-time-value {
  font-size: 13px;
  color: var(--app-text);
  margin-top: 2px;
}

/* 文档信息网格 */
.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 20px;
}

/* 信息单元卡片化：浅底 + 细边框，视觉更聚焦 */
.detail-cell {
  min-width: 0;
  background: var(--app-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 10px 12px;
}

.detail-cell-full {
  grid-column: 1 / -1;
}

.detail-cell-label {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 4px;
}

.detail-cell-value {
  font-size: 13px;
  color: var(--app-text);
  word-break: break-word;
}

.detail-cell-sub {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-top: 2px;
}

/* 弹窗：文档标题行（左侧图标+标题 + 右侧悬浮预览按钮，按钮唤起公共预览弹窗） */
.doc-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.doc-title-main {
  min-width: 0;
  flex: 1;
}

.doc-title-line {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  min-width: 0;
}

.doc-title-text {
  min-width: 0;
}

.doc-preview-btn {
  flex-shrink: 0;
}

.reject-reason {
  color: #f56c6c;
}

/* 详情弹窗底部按钮：右对齐 */
.doc-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* ===== 敏感内容检测结果区 ===== */
.doc-scan-loading {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--app-text-sub);
  padding: 2px 0;
}

.doc-scan-wrap {
  width: 100%;
}

/* 检测失败占位（接口异常/无权限时展示，不影响审核操作） */
.doc-scan-todo {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--app-bg);
  border: 1px dashed var(--app-border);
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
  color: var(--app-text-sub);
}

.doc-scan-todo-icon {
  font-size: 14px;
}

/* 有命中：统计（上方）与详细片段（下方）上下排列 */
.doc-scan-summary {
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: 8px;
  padding: 12px 14px;
}

.doc-scan-summary-title {
  font-size: 13px;
  font-weight: 600;
  color: #dc2626;
  margin-bottom: 8px;
}

.doc-scan-stats-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.doc-scan-stats-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--app-text);
}

.doc-scan-stats-count {
  color: #dc2626;
  font-weight: 600;
}

.doc-scan-detail {
  margin-top: 10px;
}

.doc-scan-detail-title {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 8px;
}

.doc-scan-truncated {
  margin-top: 8px;
}

/* 无命中：绿色提示 */
.doc-scan-clean {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(16, 185, 129, 0.08);
  border: 1px solid rgba(16, 185, 129, 0.25);
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
  color: #67c23a;
}

.doc-scan-clean-icon {
  font-size: 14px;
  font-weight: 700;
}

/* 命中片段列表 */
.doc-scan-frags {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.doc-scan-frag {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--app-bg);
}

.doc-scan-frag-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.doc-scan-cat {
  flex-shrink: 0;
  font-size: 12px;
  color: #fff;
  background: #f56c6c;
  border-radius: 4px;
  padding: 1px 6px;
}

.doc-scan-frag-count {
  font-size: 12px;
  color: var(--app-text-sub);
}

.doc-scan-frag-ctx {
  font-size: 12px;
  line-height: 1.7;
  color: var(--app-text);
  word-break: break-word;
  /* 保留原文换行与空格，便于对照原文定位命中位置 */
  white-space: pre-wrap;
}

.doc-scan-mark {
  background: #ffe08a;
  color: #8a5a00;
  border-radius: 2px;
  padding: 0 1px;
}

.text-strong {
  font-weight: 600;
  color: var(--app-text);
}
</style>

<style>
/* ===== 审核确认弹窗（ElMessageBox message VNode）=====
 * ElMessageBox 挂载在 body 下，scoped 样式不生效，故独立为非 scoped 样式块 */
.adm-msgbox-banner {
  font-size: 14px;
  color: var(--app-text);
  line-height: 1.6;
}

.adm-msgbox-form {
  margin-top: 12px;
}

.adm-msgbox-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
  margin-bottom: 8px;
  display: block;
}

.adm-msgbox-required {
  color: #f56c6c;
}
</style>
