<template>
  <div class="page-container upload-page">
    <!-- ===== 页头：标题 + Celery 解析服务状态 + 队列积压 ===== -->
    <div class="page-header">
      <div>
        <div class="page-title">文档上传</div>
        <div class="page-desc">支持 PDF / Word / MD / TXT / 源代码 / YML / JSON 等文档，自动脱敏与向量化入库</div>
      </div>
      <div class="header-status">
        <el-tag :type="celeryOk ? 'success' : 'danger'" effect="light" class="header-tag">
          {{ celeryOk ? '✅ ' : '❌ ' }}{{ celeryText }}
        </el-tag>
        <el-tag v-if="queueDepth.visible" :type="queueDepth.total > 0 ? 'warning' : 'success'" effect="light" class="header-tag">
          {{ queueDepthText }}
        </el-tag>
      </div>
    </div>

    <!-- ===== 内容区：可整体上下滚动（拖拽区 + 上传面板/历史互斥） ===== -->
    <div class="page-body">
      <div class="page-scroll">
    <!-- ===== 拖拽上传区（el-upload 自定义文件列表，仅负责选文件与拖拽） ===== -->
    <el-upload
      ref="uploadRef"
      class="upload-drop"
      drag
      multiple
      :auto-upload="false"
      :show-file-list="false"
      :accept="UPLOAD_ACCEPT"
      :on-change="onFileSelect"
    >
      <div class="upload-drop-inner">
        <div class="upload-drop-icon">📤</div>
        <div class="upload-drop-title">拖拽文件到此处，或<span class="up-primary-text">点击选择上传</span></div>
        <div class="upload-drop-desc">支持批量上传，单个文件最大 100 MB，单批次最多 100 个文件</div>
        <div class="upload-types">
          <el-tag v-for="t in TYPE_TAGS" :key="t" type="info" effect="light" size="small" class="up-type-tag">{{ t }}</el-tag>
        </div>
      </div>
    </el-upload>

    <!-- ===== 中部区域：本次上传面板与上传历史互斥共用 ===== -->
    <div class="upload-body">
      <!-- 本次上传：归属节点 + 可见范围 + 文件列表 -->
      <template v-if="pendingFiles.length || isUploading">
        <div class="upload-options">
          <div class="upload-options-row">
            <!-- 归属节点 -->
            <div class="upload-opt-cell upload-opt-node">
              <div class="form-label">归属节点 <span class="up-required">*</span></div>
              <div class="node-row">
                <el-select
                  v-model="nodeId"
                  placeholder="-- 请选择归属文件夹 --"
                  filterable
                  style="flex: 1; min-width: 0"
                  @change="onNodeChange"
                >
                  <el-option
                    v-for="opt in nodeOptions"
                    :key="opt.value + opt.label"
                    :label="opt.label"
                    :value="opt.value"
                    :disabled="opt.disabled"
                  />
                </el-select>
                <el-checkbox v-model="autoDesensitize">自动脱敏</el-checkbox>
              </div>
            </div>
            <!-- 可见范围 -->
            <div class="upload-opt-cell">
              <div class="form-label">可见范围 <span class="up-required">*</span></div>
              <div class="vis-row">
                <el-radio-group v-model="visValue" @change="onVisChange">
                  <el-radio-button value="public">🌐 全公司可见</el-radio-button>
                  <el-radio-button value="org">🏢 部门/团队</el-radio-button>
                </el-radio-group>
                <div v-if="visValue === 'org'" class="org-selects">
                  <el-select v-model="selectedDepts" multiple collapse-tags filterable placeholder="请选择部门" style="width: 170px">
                    <el-option v-for="d in deptOptions" :key="d.id" :label="d.name" :value="d.id" />
                  </el-select>
                  <el-select v-model="selectedTeams" multiple collapse-tags filterable placeholder="请选择团队" style="width: 170px">
                    <el-option v-for="t in visibleTeamOptions" :key="t.id" :label="t.name" :value="t.id" />
                  </el-select>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 本次上传文件面板 -->
        <div class="app-card upload-panel">
          <PanelHeader plain>
            本次上传文件（<span>{{ pendingFiles.length }}</span>）
            <template #actions>
              <el-button size="small" @click="clearFileList">清空列表</el-button>
              <el-button size="small" type="primary" :loading="isUploading" @click="startUpload">🚀 开始上传</el-button>
            </template>
          </PanelHeader>
          <div class="file-list">
            <div v-for="f in pendingFiles" :key="f.id" class="file-item">
              <div class="file-item-icon">{{ f.icon }}</div>
              <div class="file-item-info">
                <div class="file-item-name" :title="f.name">{{ f.name }}</div>
                <div class="file-item-meta">{{ f.type }} · {{ formatFileSize(f.size) }} · {{ fileMetaText(f) }}</div>
              </div>
              <div class="file-item-progress">
                <el-progress :percentage="f.progress" :stroke-width="6" :show-text="false" />
              </div>
              <div class="file-item-status">
                <el-tag :type="fileStatusTag(f).type" size="small" effect="plain">{{ fileStatusTag(f).text }}</el-tag>
              </div>
              <el-button link type="danger" size="small" @click="removeFile(f.id)">✕</el-button>
            </div>
            <el-empty v-if="!pendingFiles.length" description="还没有添加任何文件，请点击上方拖拽区添加" :image-size="60" />
          </div>

          <!-- 整体上传进度（含取消） -->
          <div v-if="globalProgress.visible" class="global-progress">
            <div class="global-progress-head">
              <span>整体上传进度</span>
              <el-button link type="danger" size="small" @click="cancelUpload">取消上传</el-button>
            </div>
            <el-progress :percentage="globalProgressPercent" :stroke-width="6" />
            <div class="global-progress-text">{{ globalProgress.done }}/{{ globalProgress.total }}</div>
          </div>
        </div>
      </template>

      <!-- 上传历史 -->
      <template v-else>
        <div class="history-section">
          <div class="filter-bar">
            <el-input
              v-model="historySearch"
              placeholder="🔍 搜索文件名/上传人"
              clearable
              style="width: 200px"
              @input="loadUploadHistory(1)"
              @clear="loadUploadHistory(1)"
            />
            <el-select v-model="filterFileType" placeholder="全部类型" style="width: 110px" @change="loadUploadHistory(1)">
              <el-option label="全部类型" value="" />
              <el-option v-for="t in FILE_TYPE_FILTERS" :key="t.value" :label="t.text" :value="t.value" />
            </el-select>
            <el-select v-model="filterDept" placeholder="全部部门" style="width: 120px" @change="loadUploadHistory(1)">
              <el-option label="全部部门" value="" />
              <el-option v-for="d in deptFilterOptions" :key="d.id" :label="d.name" :value="d.id" />
            </el-select>
            <el-select v-model="filterVisible" placeholder="全部可见范围" style="width: 130px" @change="loadUploadHistory(1)">
              <el-option label="全部可见范围" value="" />
              <el-option label="公开" value="public" />
              <el-option label="部门" value="dept" />
              <el-option label="团队" value="team" />
            </el-select>
            <el-select
              v-model="filterStatus"
              placeholder="全部状态"
              style="width: 170px"
              @change="loadUploadHistory(1)"
              @focus="fetchStatusCounts"
            >
              <el-option label="全部状态" value="" />
              <el-option-group v-for="g in STATUS_GROUPS" :key="g.label" :label="g.label">
                <el-option v-for="opt in g.options" :key="opt.value" :value="opt.value" :label="statusOptionLabel(opt)" />
              </el-option-group>
            </el-select>
            <el-checkbox v-model="includeDeleted" @change="loadUploadHistory(1)">显示已删除</el-checkbox>
          </div>

          <div class="app-card history-card">
            <el-table :data="currentDocs" v-loading="historyLoading" :row-class-name="historyRowClass" size="default">
              <el-table-column label="文件名" min-width="176" show-overflow-tooltip>
                <template #default="{ row }">
                  <span class="flex items-center gap-6">
                    <span>{{ fileTypeIcon(row.file_type) }}</span>
                    <span class="up-row-name" :class="{ 'up-name-deleted': row.is_deleted }">{{ row.file_name }}</span>
                    <template v-if="row.version_count > 1">
                      <el-tag v-if="row.is_active" type="success" size="small" effect="plain">活跃</el-tag>
                      <el-tag v-else type="info" size="small" effect="plain">旧版本</el-tag>
                    </template>
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="类型" width="100">
                <template #default="{ row }">
                  <el-tag type="info" size="small" effect="plain">{{ fileTypeByExt(row.file_name) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="归属节点" min-width="98" show-overflow-tooltip>
                <template #default="{ row }">
                  <span class="text-sub up-node-path" :title="nodePathText(row)">{{ nodePathText(row) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="上传人" width="110">
                <template #default="{ row }">{{ row.owner_name || '-' }}</template>
              </el-table-column>
              <el-table-column label="可见范围" width="80">
                <template #default="{ row }">
                  <el-tag :type="visTagType(row.visible_scope)" size="small" effect="plain">{{ visTagText(row.visible_scope) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <el-tag v-if="row.is_deleted" type="danger" size="small" effect="plain">已删除</el-tag>
                  <el-tag v-else :type="uploadStatusTag(row).type" size="small" effect="plain">{{ uploadStatusTag(row).text }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="上传时间" width="135">
                <template #default="{ row }">
                  <span class="text-sub">{{ row.is_deleted ? '删除于 ' + formatDate(row.delete_time) : formatDate(row.created_at) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="230">
                <template #default="{ row }">
                  <template v-if="!row.is_deleted">
                    <el-button link type="primary" size="small" @click="viewDocument(row)">预览</el-button>
                    <el-button link type="primary" size="small" @click="showDocProgress(row)">进度</el-button>
                    <el-button link type="primary" size="small" @click="reparseDocument(row.id)">重新解析</el-button>
                    <el-button v-if="row.version_count > 1" link type="primary" size="small" @click="showVersionModal(row.id)">版本</el-button>
                    <el-button link type="danger" size="small" @click="deleteDocument(row.id)">删除</el-button>
                  </template>
                  <template v-else>
                    <el-button link type="primary" size="small" disabled>预览</el-button>
                    <el-button link type="primary" size="small" disabled>进度</el-button>
                    <el-button link type="primary" size="small" disabled>重新解析</el-button>
                    <el-button link type="success" size="small" @click="restoreDocument(row.id)">🔄 恢复</el-button>
                    <template v-if="row.file_path">
                      <el-button v-if="daysSinceDelete(row) >= 30" link type="danger" size="small" @click="hardDeleteDocument(row.id)">🗑️ 物理删除</el-button>
                      <span v-else class="text-sub text-xs">({{ 30 - daysSinceDelete(row) }}天后可物理删除)</span>
                    </template>
                  </template>
                </template>
              </el-table-column>
              <template #empty>
                <el-empty description="暂无上传记录" :image-size="60" />
              </template>
            </el-table>
            <AppPagination
              class="up-pagination"
              :total="historyTotal"
              :page-size="historyPageSize"
              :page="historyPage"
              @page-change="loadUploadHistory"
            />
          </div>
        </div>
      </template>
    </div>
      </div>
    </div>

    <!-- ===== 文档处理进度弹窗（8 步步骤条） ===== -->
    <el-dialog v-model="progressVisible" :title="progressTitle" width="560px">
      <div class="progress-steps">
        <div
          v-for="(step, i) in progressSteps"
          :key="i"
          class="progress-step"
          :class="{ 'is-active': step.state === 'active' }"
        >
          <span class="progress-step-icon" :class="step.state">{{ PROGRESS_ICONS[step.state] || '○' }}</span>
          <span class="progress-step-name">{{ step.name }}</span>
          <el-tag :type="PROGRESS_TAG[step.state] || 'info'" size="small" effect="plain">{{ step.text }}</el-tag>
        </div>
      </div>
    </el-dialog>

    <!-- 文档预览弹窗（公共组件） -->
    <DocPreviewDialog v-model="previewVisible" :doc-id="previewDocId" :initial-page="previewInitialPage" />

    <!-- ===== 版本历史弹窗 ===== -->
    <el-dialog v-model="versionVisible" :title="versionTitle" width="760px">
      <el-table :data="versionList" v-loading="versionLoading" size="small">
        <el-table-column label="文件名" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title || '-' }}</template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }">{{ row.version_tag || ('v' + row.version) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small" effect="plain">活跃</el-tag>
            <el-tag v-else type="info" size="small" effect="plain">旧版本</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="处理状态" width="120">
          <template #default="{ row }">
            <el-tag :type="versionStatusTag(row).type" size="small" effect="plain">{{ versionStatusTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="90">
          <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="上传时间" width="120">
          <template #default="{ row }">{{ formatDateShort(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <template v-if="row.can_read !== false && isPreviewableFileType(row.file_type)">
              <el-button link type="primary" size="small" @click="openPreview(row.id)">预览</el-button>
              <el-button v-if="!row.is_active && row.is_owner" link type="primary" size="small" @click="setVersionActive(row.id, versionDocId)">设为活跃</el-button>
            </template>
            <template v-else-if="row.is_active">
              <span class="text-sub text-xs">当前</span>
            </template>
            <template v-else-if="row.is_owner">
              <el-button link type="primary" size="small" @click="setVersionActive(row.id, versionDocId)">设为活跃</el-button>
            </template>
            <template v-else>
              <span class="text-sub text-xs">仅上传者可切换</span>
            </template>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无版本记录" :image-size="60" />
        </template>
      </el-table>
    </el-dialog>

    <!-- ===== 同内容重复上传对话框 ===== -->
    <el-dialog v-model="duplicateVisible" title="相同内容的文件已存在" width="440px" @closed="onDuplicateClosed">
      <div class="duplicate-dialog">
        <div class="dup-desc">检测到同节点已有内容完全一致的文件</div>
        <div class="dup-info">
          <div><span>文件名</span><b>{{ duplicateExisting.file_name || '-' }}</b></div>
          <div><span>原上传者</span><b>{{ duplicateExisting.owner_name || '未知' }}</b></div>
          <div><span>原上传时间</span><b>{{ formatDate(duplicateExisting.created_at) }}</b></div>
        </div>
      </div>
      <template #footer>
        <el-button @click="onDuplicateAction('cancel')">取消</el-button>
        <el-button @click="viewExistingDuplicate">查看现有文件</el-button>
        <el-button type="primary" @click="onDuplicateAction('force')">强制新建版本</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import DocPreviewDialog from '../components/doc-preview/DocPreviewDialog.vue'
import { formatDate, formatDateShort, formatFileSize, isPreviewableFileType, pipelineStatus, errMsg } from '../utils/format'
import { visTagType, visTagText, fileTypeIcon } from '../utils/labels'
import { getToken } from '../utils/authStorage'
import { useListLoader } from '../composables/useListLoader'
import { useConfirm } from '../composables/useConfirm'
import PanelHeader from '../components/base/PanelHeader.vue'
import AppPagination from '../components/base/AppPagination.vue'

const userStore = useUserStore()
// 预览水印：文案与字体配置从公共 composable 获取（暗色/浅色自适应）

/* ==========================================================
   常量（与旧 upload.js 保持一致，不可变更）
   ========================================================== */
// 文件选择器仅展示主要类型；其余扩展名（rst/wps/et/dps/ini/conf/cfg/bat/ps1 等）
// 仍支持拖拽上传，由 ALLOWED_EXTS 统一校验
const UPLOAD_ACCEPT = '.pdf,.doc,.docx,.md,.markdown,.txt,.csv,.xlsx,.xls,.ppt,.pptx,.py,.js,.ts,.jsx,.tsx,.java,.go,.rs,.c,.cpp,.h,.yml,.yaml,.json,.xml,.toml,.sh,.css'
const ALLOWED_EXTS = new Set([
  'pdf', 'doc', 'docx', 'md', 'markdown', 'txt', 'rst',
  'csv', 'xlsx', 'xls',
  'ppt', 'pptx',
  'wps', 'et', 'dps',
  'py', 'js', 'ts', 'jsx', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h',
  'yml', 'yaml', 'json', 'xml', 'toml', 'ini', 'conf', 'cfg',
  'sh', 'bat', 'ps1', 'css',
  'jpg', 'jpeg', 'png', 'bmp', 'webp'
])
const MAX_FILE_SIZE_MB = 100
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
const MAX_FILES_PER_BATCH = 100       // 单批次最多 100 个文件
const MAX_CONCURRENT = 3              // 同时并发上传数
const POLL_INTERVAL_MS = 10000        // 上传状态轮询间隔
// 触发自动刷新的进行中状态：仅解析/切片/向量构建（等待解析、构建失败等不触发，减少刷新频率）
const PROCESSING_STATUSES = new Set(['parsing', 'desensitizing', 'chunking', 'embedding'])

const TYPE_TAGS = ['📕 PDF', '📄 Word', '📝 Markdown', '📃 TXT', '📊 Excel/CSV', '📽️ PPT', '💻 源代码', '⚙️ YML', '📊 JSON']
const FILE_TYPE_FILTERS = [
  { value: 'pdf', text: 'PDF' }, { value: 'docx', text: 'Word' },
  { value: 'markdown', text: 'Markdown' }, { value: 'txt', text: 'TXT' },
  { value: 'code', text: '代码' }, { value: 'config', text: '配置' },
  { value: 'other', text: '其他' }
]
// 状态筛选：只保留"需要关注"的状态，按类别分组；
// 终态（已通过/未启用等）不单独筛选：全部结束即"已完成"，未结束则显示下一步待办状态
const STATUS_GROUPS = [
  { label: '处理中', options: [
    { value: 'pending', text: '等待解析' }, { value: 'parsing', text: '解析中' },
    { value: 'chunking', text: '切片中' }, { value: 'embedding', text: '向量构建中' },
  ] },
  { label: '审核中', options: [
    { value: 'pending_team', text: '待审核' }, { value: 'pending_compliance', text: '待合规复核' },
  ] },
  { label: '构建中', options: [
    { value: 'graph_pending', text: '等待图谱构建' }, { value: 'graph_extracting', text: '图谱构建中' },
    { value: 'wiki_pending', text: '等待Wiki生成' }, { value: 'wiki_extracting', text: 'Wiki生成中' },
  ] },
  { label: '异常', options: [
    { value: 'failed', text: '解析失败' }, { value: 'embedding_failed', text: '向量构建失败' },
    { value: 'rejected', text: '审核驳回' }, { value: 'graph_failed', text: '图谱构建失败' },
    { value: 'wiki_failed', text: 'Wiki生成失败' },
  ] },
  { label: '汇总', options: [{ value: 'done', text: '已完成' }] },
]
// 步骤状态映射：图标 + 标签样式
const PROGRESS_ICONS = { done: '✓', active: '◐', todo: '○', failed: '✗', skipped: '—' }
const PROGRESS_TAG = { done: 'success', active: 'primary', todo: 'info', failed: 'danger', skipped: 'info' }

/* ==========================================================
   状态定义
   ========================================================== */
// 页头：Celery 解析服务状态 + 队列积压
const celeryOk = ref(true)
const celeryText = ref('检查文档解析服务状态...')
const queueDepth = reactive({ visible: false, total: 0, parseSize: 0 })
const queueDepthText = computed(() => {
  if (queueDepth.total > 0) {
    return '📥 队列积压 ' + queueDepth.total + (queueDepth.parseSize ? '（解析 ' + queueDepth.parseSize + '）' : '')
  }
  return '📥 队列空闲'
})

// 待上传文件列表：{ id, file, name, size, type, icon, status, progress, error }
const pendingFiles = ref([])
const uploadRef = ref(null)
const isUploading = ref(false)
let uploadingXhrs = []
const globalProgress = reactive({ visible: false, done: 0, total: 0 })
const globalProgressPercent = computed(() => globalProgress.total > 0 ? Math.round((globalProgress.done / globalProgress.total) * 100) : 0)

// 上传选项
const nodeId = ref('')
const nodeOptions = ref([])
const folderNodeIds = ref(new Set())
const autoDesensitize = ref(true)     // 与原页面一致：默认勾选（脱敏在解析流水线统一执行）
const visValue = ref('org')           // public=全公司可见 / org=部门/团队
const deptOptions = ref([])
const teamOptions = ref([])
const selectedDepts = ref([])
const selectedTeams = ref([])
const visibleTeamOptions = computed(() =>
  teamOptions.value.filter(t => selectedDepts.value.includes(t.department_id))
)

// 上传历史
const currentDocs = ref([])
const historyPage = ref(1)
const historyTotal = ref(0)
const historyPageSize = 20
// 上传历史加载：由 useListLoader 统一管理 loading/请求序号守卫/错误提示（文案固定，用 onError 覆盖默认前缀）
const { loading: historyLoading, load: loadUploadHistory } = useListLoader(fetchUploadHistory, {
  onError: (e) => {
    console.error('load upload history failed:', e)
    ElMessage.error('加载失败，请刷新重试')
  },
})
const historySearch = ref('')
const filterFileType = ref('')
const filterDept = ref('')
const filterVisible = ref('')
const filterStatus = ref('')
const includeDeleted = ref(false)
const deptFilterOptions = ref([])
// 状态统计（点击状态下拉时触发，5 秒节流）
const statusCounts = ref({})
let statusCountsLastFetch = 0

// 处理进度弹窗
const progressVisible = ref(false)
const progressTitle = ref('处理进度')
const progressSteps = ref([])

// 版本历史弹窗
const versionVisible = ref(false)
const versionLoading = ref(false)
const versionTitle = ref('版本历史')
const versionList = ref([])
const versionDocId = ref(null)

// 重复文件对话框（Promise 模式：cancel / force）
const duplicateVisible = ref(false)
const duplicateExisting = ref({})
let duplicateResolver = null

/* ==========================================================
   页面初始化
   ========================================================== */
onMounted(() => {
  userStore.restore()
  loadUploadHistory()
  loadFilterOptions()
  initNodeSelect()
  checkAndShowCeleryStatus()
  // 队列深度展示（异步刷新，不阻塞页面主流程）
  refreshQueueDepth()
  // 默认可见范围为部门/团队时，懒加载部门/团队选项
  if (visValue.value === 'org') loadUploadDeptTeamOptions()
  startUploadPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  stopUploadPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

// 页面隐藏时停止轮询，避免后台空转
function onVisibilityChange() {
  if (document.hidden) stopUploadPolling()
}

/* ==========================================================
   Celery 状态与队列深度
   ========================================================== */
async function checkAndShowCeleryStatus() {
  try {
    const data = await api.getJson('/api/v1/knowledge/celery/status/')
    celeryOk.value = !!data.celery_ok
    celeryText.value = data.detail || (data.celery_ok ? 'Celery 运行正常' : 'Celery 未启动')
  } catch (e) {
    celeryOk.value = false
    celeryText.value = '状态检查失败'
  }
}

// 队列深度：低成本 Redis 读取，失败时隐藏且不打扰用户
async function refreshQueueDepth() {
  try {
    const data = await api.getJson('/api/v1/knowledge/queues/depth/')
    const queues = data.queues || {}
    const names = Object.keys(queues)
    queueDepth.visible = names.length > 0
    queueDepth.parseSize = Number((queues.parse || {}).size) || 0
    queueDepth.total = names.reduce((acc, n) => acc + (Number(queues[n].size) || 0), 0)
  } catch (e) {
    console.warn('queue depth refresh failed:', e)
    queueDepth.visible = false
  }
}

// 上传前检查 Celery：未就绪时二次确认，用户可取消或继续（上传后手动重传解析）
async function checkCeleryStatusBeforeUpload() {
  try {
    const data = await api.getJson('/api/v1/knowledge/celery/status/')
    if (!data.celery_ok) {
      // 仅确认场景：用 confirm 的返回值（省略 action）判断用户是否继续
      const confirmed = await confirm({
        message: '是否继续上传？上传后可在历史列表中点击"重新解析"按钮手动触发解析。',
        title: '解析服务未就绪',
        confirmText: '继续上传',
        cancelText: '取消上传',
      })
      if (!confirmed) throw new Error('用户取消上传')
    }
  } catch (e) {
    if (e.message !== '用户取消上传') console.warn('Celery 状态检查失败:', e)
    else throw e
  }
}

/* ==========================================================
   上传历史
   ========================================================== */
async function fetchUploadHistory(page = 1, opts = {}) {
  let url = `/api/v1/knowledge/documents/?page=${page}&page_size=${historyPageSize}`
  if (historySearch.value) url += '&search=' + encodeURIComponent(historySearch.value)
  if (filterFileType.value) url += '&file_type=' + filterFileType.value
  if (filterDept.value) url += '&dept_id=' + filterDept.value
  if (filterVisible.value) url += '&visible_scope=' + filterVisible.value
  if (filterStatus.value) url += '&status=' + filterStatus.value
  if (includeDeleted.value) url += '&include_deleted=true'

  const data = await api.getJson(url)
  const docs = data.results || data
  currentDocs.value = docs
  const count = data.count || (docs.length || 0)
  // 数据量减少（文档被删除/恢复）导致当前页越界时，回退到最后一页重新加载
  if (page > Math.max(1, Math.ceil(count / historyPageSize))) {
    const lastPage = Math.max(1, Math.ceil(count / historyPageSize))
    historyPage.value = lastPage
    await loadUploadHistory(lastPage, opts)
    return
  }
  historyTotal.value = count
  historyPage.value = page
  startUploadPolling()
  // 切换页码/筛选后回到表格顶部，避免停留在上一页的滚动位置
  nextTick(resetHistoryScroll)
}

// 重置历史表格滚动到顶部（切换页码/刷新后调用）
function resetHistoryScroll() {
  const wrap = document.querySelector('.history-card .el-scrollbar__wrap')
  if (wrap) wrap.scrollTop = 0
}

// 筛选选项：可用部门（按权限过滤后的全量）
async function loadFilterOptions() {
  try {
    const depts = await api.getJson('/api/v1/knowledge/documents/available_depts/')
    deptFilterOptions.value = depts || []
  } catch (e) {
    console.warn('加载筛选选项失败:', e)
  }
}

// 状态下拉聚焦时拉取状态统计（5 秒节流），选项文案追加数量
async function fetchStatusCounts() {
  const now = Date.now()
  if (now - statusCountsLastFetch < 5000) return
  statusCountsLastFetch = now
  try {
    const data = await api.getJson('/api/v1/knowledge/documents/status_counts/')
    statusCounts.value = data || {}
  } catch (e) {
    console.warn('获取状态统计失败:', e)
  }
}

function statusOptionLabel(opt) {
  const c = statusCounts.value[opt.value] || 0
  return c > 0 ? `${opt.text} (${c})` : opt.text
}

// 已删除行样式：半透明 + 浅红底，文件名删除线
function historyRowClass({ row }) {
  return row.is_deleted ? 'up-row-deleted' : ''
}

function daysSinceDelete(doc) {
  return doc.delete_time ? Math.floor((Date.now() - new Date(doc.delete_time)) / (1000 * 60 * 60 * 24)) : 0
}

/* ==========================================================
   归属节点下拉（树形结构，仅文件夹可选）
   ========================================================== */
async function initNodeSelect() {
  try {
    const data = await api.getJson('/api/v1/knowledge/nodes/tree/')
    const allNodes = data.tree || []

    const roles = userStore.roles
    const u = userStore.user || {}
    const myDeptId = u.department_id
    const myTeamIds = u.team ? [u.team.id] : []

    // 可管理文档的角色：超级管理员 / 文档管理员
    const isAdmin = roles.includes('super_admin') || roles.includes('kb_admin')
    const isDeptManager = roles.includes('dept_manager')
    const isTeamLeader = roles.includes('team_leader')

    let filteredNodes = allNodes
    let defaultNodeId = null

    // 非管理员按身份收窄可选范围：部门经理只看本部门，团队组长/普通员工只看本团队
    if (!isAdmin) {
      if (isDeptManager && myDeptId) {
        filteredNodes = allNodes.map(kbNode => {
          const deptNode = kbNode.children ? kbNode.children.find(d => d.ref_id === myDeptId) : null
          if (deptNode) {
            defaultNodeId = deptNode.id
            return { ...kbNode, children: [deptNode] }
          }
          return null
        }).filter(n => n)
      } else if ((isTeamLeader || !isAdmin) && myTeamIds.length > 0) {
        filteredNodes = allNodes.map(kbNode => {
          if (!kbNode.children) return null
          const deptNode = kbNode.children.find(d => d.ref_id === myDeptId)
          if (!deptNode || !deptNode.children) return null
          const teamNodes = deptNode.children.filter(t => myTeamIds.includes(t.ref_id))
          if (teamNodes.length > 0) {
            defaultNodeId = teamNodes[0].id
            return { ...kbNode, children: [{ ...deptNode, children: teamNodes }] }
          }
          return null
        }).filter(n => n)
      }
    }

    const opts = []
    const folderIds = new Set()

    // 判断节点子树中是否存在文件夹（FOLDER），用于决定组织节点是否展示为灰色分支标题
    function hasFolder(n) {
      if (n.node_kind === 'FOLDER') return true
      return (n.children || []).some(hasFolder)
    }

    // 树形层级缩进（每层 3 个全角空格，语义同旧版 &nbsp; 缩进）
    function indent(level) { return '\u3000'.repeat((level - 1) * 3) }

    // 归属节点以树形结构展示：
    // - 知识库根 / 部门 / 团队：仅当子树含文件夹时展示为灰色层级标题（根）或可选组织节点（部门/团队）
    // - 文件夹（FOLDER）：可选，文档只能上传到文件夹
    function walk(nodes, level) {
      nodes.forEach(n => {
        if (n.node_level === 1) {
          // 根节点：仅当子树中存在文件夹时展示为分支标题
          if (hasFolder(n)) {
            opts.push({ value: 'root:' + n.id, label: indent(level) + '📚 ' + (n.name || '知识库'), disabled: true })
          }
          if (n.children && n.children.length) walk(n.children, level + 1)
          return
        }
        if (n.node_kind === 'FOLDER') {
          const id = String(n.id)
          folderIds.add(id)
          opts.push({ value: id, label: indent(level) + '📁 ' + n.name, disabled: false })
          if (String(n.id) === String(defaultNodeId)) {
            nodeId.value = id
          }
        } else if (hasFolder(n)) {
          // 组织节点（部门/团队）有文件夹后代时保持可选，选中时提示不可作为归属并重置
          opts.push({ value: 'org:' + n.id, label: indent(level) + (n.node_level === 2 ? '🏢 ' : '👥 ') + n.name, disabled: false })
        }
        if (n.children && n.children.length) walk(n.children, level + 1)
      })
    }
    walk(filteredNodes, 1)

    nodeOptions.value = opts
    folderNodeIds.value = folderIds
  } catch (e) {
    console.error('load nodes failed:', e)
    nodeOptions.value = [{ value: '', label: '加载节点失败', disabled: true }]
  }
}

// 组织节点（value 前缀 org:）虽可选但不可作为归属，选中即提示并重置回占位
function onNodeChange(val) {
  if (String(val || '').startsWith('org:')) {
    ElMessage.warning('不可选择节点，请先在节点上创建文件夹')
    nodeId.value = ''
  }
}

/* ==========================================================
   可见范围：部门/团队多选
   ========================================================== */
function onVisChange() {
  if (visValue.value === 'org') loadUploadDeptTeamOptions()
}

async function loadUploadDeptTeamOptions() {
  try {
    const res = await api.getJson('/api/v1/knowledge/documents/allowed_visibility/')
    deptOptions.value = res.departments || []
    teamOptions.value = res.teams || []
  } catch (e) {
    console.error('Failed to load allowed visibility:', e)
    deptOptions.value = []
    teamOptions.value = []
  }
  // 默认预选本部门/本团队（与旧版 multi-select renderDeptList/renderTeamList 行为一致）
  const u = userStore.user || {}
  if (u.department_id) selectedDepts.value = [u.department_id]
  else selectedDepts.value = []
  selectedTeams.value = u.team ? [u.team.id] : []
}

/* ==========================================================
   文件选择与过滤
   ========================================================== */
function isAllowedFile(name) {
  const ext = name.split('.').pop().toLowerCase()
  return ALLOWED_EXTS.has(ext)
}

// el-upload 选择/拖拽回调：把原始文件加入自定义列表（auto-upload=false，不渲染组件内部列表）
function onFileSelect(uploadFile) {
  if (!uploadFile.raw) return
  addFiles([uploadFile.raw])
}

function addFiles(fileList) {
  let added = 0, skipped = 0
  for (const f of fileList) {
    if (pendingFiles.value.length >= MAX_FILES_PER_BATCH) {
      ElMessage.error('最多同时上传 ' + MAX_FILES_PER_BATCH + ' 个文件')
      break
    }
    if (f.size > MAX_FILE_SIZE_BYTES) {
      ElMessage.error(`文件 ${f.name} 超过大小限制（最大 ${MAX_FILE_SIZE_MB} MB）`)
      skipped++
      continue
    }
    if (!isAllowedFile(f.name)) {
      skipped++
      continue
    }
    // 同名同大小视为重复，跳过
    if (pendingFiles.value.some(p => p.name === f.name && p.size === f.size)) {
      skipped++
      continue
    }
    pendingFiles.value.push({
      id: 'f' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
      file: f,
      name: f.name,
      size: f.size,
      type: fileTypeByExt(f.name),
      icon: fileIconByExt(f.name),
      status: 'pending',
      progress: 0,
      error: ''
    })
    added++
  }
  if (skipped > 0) {
    ElMessage({ message: '已跳过 ' + skipped + ' 个不支持或重复的文件', type: 'info', duration: 3000 })
  }
}

function removeFile(id) {
  pendingFiles.value = pendingFiles.value.filter(f => f.id !== id)
}

function clearFileList() {
  pendingFiles.value = []
  globalProgress.visible = false
  ElMessage({ message: '已清空', type: 'info', duration: 1500 })
}

// 文件项副标题与状态标签（按上传流程状态动态展示）
function fileMetaText(f) {
  if (f.status === 'skipped') return '已跳过（内容重复）'
  if (f.status === 'warning') return 'Celery未启动，等待手动触发'
  if (f.status === 'success') return '解析中...'
  if (f.status === 'failed') return f.error || '上传失败'
  if (f.status === 'uploading') return '上传中 ' + f.progress + '%'
  return '待上传'
}

function fileStatusTag(f) {
  return {
    pending: { type: 'info', text: '等待中' },
    uploading: { type: 'warning', text: '上传中' },
    success: { type: 'success', text: '已上传' },
    warning: { type: 'warning', text: '等待解析' },
    failed: { type: 'danger', text: '失败' },
    skipped: { type: 'info', text: '已跳过' }
  }[f.status] || { type: 'info', text: '等待中' }
}

/* ==========================================================
   上传（单文件 XHR 携带 JWT + 进度；批次并发 3）
   ========================================================== */
function uploadSingleFile(info, nodeIdVal, visibility, token, depts, teams, forceNewVersion = false) {
  const formData = new FormData()
  formData.append('file', info.file, info.name)
  formData.append('node_id', nodeIdVal)
  formData.append('visible_scope', visibility)
  // 相同内容文件默认被后端拦截（409 duplicate_file），用户选择"强制新建版本"时携带该标记跳过拦截
  formData.append('force_new_version', forceNewVersion ? 'true' : 'false')
  depts.forEach(id => formData.append('visibility_depts', id))
  teams.forEach(id => formData.append('visibility_teams', id))

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    uploadingXhrs.push(xhr)
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) {
        info.progress = Math.round((e.loaded / e.total) * 100)
      }
    })
    xhr.addEventListener('load', () => {
      let data = {}
      try { data = JSON.parse(xhr.responseText || '{}') } catch (e) { /* 解析失败按空对象处理 */ }
      // 同内容重复上传：返回标记由调用方弹窗选择（取消/查看现有/强制新建版本）
      if (xhr.status === 409 && data.code === 'duplicate_file') {
        resolve({ duplicate: true, existing: data.existing || {} })
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (data.status === 'failed') reject(new Error(data.detail || '上传失败'))
        else resolve(data)
      } else {
        reject(new Error(xhr.status + ' ' + (data.detail || xhr.statusText)))
      }
    })
    xhr.addEventListener('error', () => reject(new Error('网络错误')))
    xhr.open('POST', '/api/v1/knowledge/documents/upload/')
    xhr.setRequestHeader('Authorization', 'Bearer ' + token)
    xhr.send(formData)
  }).finally(() => {
    uploadingXhrs = uploadingXhrs.filter(x => x !== xhr)
  })
}

async function startUpload() {
  if (isUploading.value) {
    ElMessage({ message: '上传进行中，请稍候', type: 'info', duration: 2000 })
    return
  }
  if (!pendingFiles.value.length) {
    ElMessage.error('请先添加文件')
    return
  }
  const nodeIdVal = nodeId.value
  if (!nodeIdVal) {
    ElMessage.error('请选择归属节点')
    return
  }
  // 前端二次拦截：仅文件夹（FOLDER）可直接上传文档，组织节点（部门/团队）需先选其下文件夹
  if (!folderNodeIds.value.has(String(nodeIdVal))) {
    ElMessage.warning('文档只能上传到文件夹中，请选择文件夹节点')
    return
  }

  const visMap = { org: 'dept', public: 'public' }
  const visibility = visMap[visValue.value] || 'dept'

  const token = getToken()
  if (!token) {
    ElMessage.error('请先登录')
    return
  }

  let depts = []
  let teams = []
  if (visValue.value === 'org') {
    depts = selectedDepts.value.slice()
    teams = selectedTeams.value.slice()
  }

  const total = pendingFiles.value.length
  // 瞬时反馈用 info（蓝色 3 秒自动关闭），上传进度由全局进度条持续反馈
  ElMessage({ message: '正在上传 ' + total + ' 个文件', type: 'info', duration: 3000 })

  try {
    await checkCeleryStatusBeforeUpload()
  } catch (e) {
    if (e.message === '用户取消上传') return
    console.warn('Celery 状态检查失败:', e)
  }

  isUploading.value = true
  uploadingXhrs = []

  let completedCount = 0
  let successCount = 0
  let failCount = 0
  const failReasons = []          // 失败原因列表，最终 toast 中展示第一条
  const uploadedDocIds = []

  globalProgress.visible = true
  globalProgress.done = 0
  globalProgress.total = total

  const uploadTasks = pendingFiles.value.map(info => async () => {
    info.status = 'uploading'
    info.progress = 0
    info.error = ''
    try {
      let responseData = await uploadSingleFile(info, nodeIdVal, visibility, token, depts, teams)

      // 同内容重复上传：弹窗让用户选择（取消 / 查看现有文件 / 强制新建版本）
      if (responseData && responseData.duplicate) {
        const choice = await showDuplicateFileDialog(responseData.existing || {})
        if (choice === 'cancel') {
          info.status = 'skipped'
          info.progress = 0
          return 'skipped'
        }
        // force：携带 force_new_version=true 重新提交，绕过同内容拦截
        responseData = await uploadSingleFile(info, nodeIdVal, visibility, token, depts, teams, true)
      }

      info.progress = 100

      if (responseData.celery_ok === false) {
        // Celery 未启动：文档已入库但未触发解析，提示等待手动触发
        info.status = 'warning'
      } else {
        info.status = 'success'
      }

      if (responseData.document_id) {
        uploadedDocIds.push({ id: responseData.document_id, infoId: info.id })
      }
      return 'success'
    } catch (err) {
      info.status = 'failed'
      let errorMsg = errMsg(err, '上传失败')
      if (err.response) {
        if (err.response.detail) errorMsg = err.response.detail
        else if (typeof err.response === 'string') errorMsg = err.response
        else if (err.response.error) errorMsg = err.response.error
      }
      info.error = errorMsg
      // 记录失败原因（含文件名），最终汇总提示，避免用户只看到"x 失败"不明原因
      if (failReasons.length < 3) {
        failReasons.push((info.name || '文件') + '：' + errorMsg)
      }
      return 'failed'
    } finally {
      completedCount++
      globalProgress.done = completedCount
    }
  })

  for (let i = 0; i < uploadTasks.length; i += MAX_CONCURRENT) {
    const batch = uploadTasks.slice(i, i + MAX_CONCURRENT)
    const results = await Promise.all(batch.map(fn => fn()))
    results.forEach(r => {
      if (r === 'success') successCount++
      else if (r === 'skipped') { /* 跳过，不计入成功/失败 */ }
      else failCount++
    })

    if (!isUploading.value) {
      // 用户中途取消：保留文件供重新上传
      globalProgress.visible = false
      ElMessage({ message: '上传已取消', type: 'info', duration: 3000 })
      return
    }
  }

  try {
    globalProgress.visible = false
    isUploading.value = false

    if (failCount === 0) {
      ElMessage.success('全部 ' + successCount + ' 个文件上传成功')
    } else {
      // 告警提示（黄色 5s 自动关闭），附第一条失败原因
      const reasonText = failReasons.length ? '：' + failReasons[0] : ''
      ElMessage.warning('上传完成：' + successCount + ' 成功，' + failCount + ' 失败' + reasonText)
    }

    if (uploadedDocIds.length > 0) startUploadPolling()
    finishUpload()
  } catch (e) {
    ElMessage.error('上传收尾异常: ' + e.message)
  }
}

// 取消上传：中止全部进行中的 XHR，文件列表重置为待上传状态保留
function cancelUpload() {
  isUploading.value = false
  uploadingXhrs.forEach(xhr => {
    try { xhr.abort() } catch (e) { /* 忽略 */ }
  })
  uploadingXhrs = []
  globalProgress.visible = false
  pendingFiles.value.forEach(f => {
    f.progress = 0
    f.status = 'pending'
    f.error = ''
  })
}

// 上传完成收尾：清空列表、刷新历史与队列积压
function finishUpload() {
  pendingFiles.value = []
  globalProgress.visible = false
  isUploading.value = false
  loadUploadHistory()
  // 上传完成后立即刷新队列积压，让用户直观看到有多少任务在排队
  refreshQueueDepth()
}

/* ==========================================================
   上传状态轮询（合并状态轮询和历史刷新）
   ========================================================== */
let pollingTimer = null

function hasProcessingDocuments() {
  return currentDocs.value.some(d => PROCESSING_STATUSES.has(d.status))
}

function startUploadPolling() {
  stopUploadPolling()
  pollingTimer = setInterval(() => {
    try {
      if (hasProcessingDocuments()) {
        // 静默刷新：不显示 loading 遮罩，避免轮询时表格闪烁
        loadUploadHistory(historyPage.value, { silent: true })
        // 处理期间同步刷新队列积压（低成本 Redis 读取）
        refreshQueueDepth()
      } else {
        stopUploadPolling()
      }
    } catch (e) {
      console.warn('上传状态轮询失败:', e)
    }
  }, POLL_INTERVAL_MS)
}

function stopUploadPolling() {
  if (pollingTimer) {
    clearInterval(pollingTimer)
    pollingTimer = null
  }
}

/* ==========================================================
   历史行操作
   ========================================================== */
// 文档预览弹窗（DocPreviewDialog 组件）：显隐与打开参数
const previewVisible = ref(false)
const previewDocId = ref(null)        // 当前预览文档 ID
const previewInitialPage = ref(1)     // 打开预览定位页

// 打开文档预览弹窗（默认第 1 页）
function openPreview(id) {
  if (!id) return
  previewDocId.value = id
  previewInitialPage.value = 1
  previewVisible.value = true
}

function viewDocument(doc) {
  if (!doc) {
    ElMessage.error('文档不存在')
    return
  }
  if (doc.status === 'failed') {
    ElMessage.error('失败原因：' + (doc.error_message || '未知错误'))
    return
  }
  openPreview(doc.id)
}

// 乐观更新：立即把本地状态置为 pending（重新触发轮询），失败时提示
async function reparseDocument(docId) {
  const docIdx = currentDocs.value.findIndex(d => d.id === docId)
  if (docIdx !== -1) {
    currentDocs.value[docIdx].status = 'pending'
    currentDocs.value[docIdx].error_message = ''
  }
  // 立即重启轮询（确保不被 hasProcessingDocuments 判断停止）
  startUploadPolling()
  try {
    await api.postJson(`/api/v1/knowledge/documents/${docId}/reparse/`, {})
    ElMessage.success('已触发重新解析')
    // 最终用服务端数据覆盖一次，保证一致性
    loadUploadHistory(historyPage.value)
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

function deleteDocument(docId) {
  confirm({
    message: '确定删除此文档？删除后不可恢复。',
    title: '删除文档', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    await api.deleteJson(`/api/v1/knowledge/documents/${docId}/`)
    ElMessage.success('文档已删除')
    loadUploadHistory(historyPage.value)
  })
}

function restoreDocument(docId) {
  confirm({
    message: '确定恢复此文档？',
    title: '恢复文档', confirmText: '确认恢复',
  }, async () => {
    await api.postJson(`/api/v1/knowledge/documents/${docId}/restore/`, {})
    ElMessage.success('文档已恢复')
    loadUploadHistory(historyPage.value)
  })
}

function hardDeleteDocument(docId) {
  confirm({
    message: '⚠️ 警告：物理删除后无法恢复，确定继续？',
    title: '物理删除文档', confirmText: '确认删除', type: 'error',
  }, async () => {
    await api.postJson(`/api/v1/knowledge/documents/${docId}/hard_delete/`, {})
    ElMessage.success('物理删除成功')
    loadUploadHistory(historyPage.value)
  })
}

/* ==========================================================
   文档状态标签（处理维度 + 审核维度聚合，每篇文档唯一归属一个状态）
   归属顺序：处理异常 > 审核驳回（终态）> 处理全部完成后按审核维度归类 > 处理未完成按流水线展示
   ========================================================== */
function uploadStatusTag(h) {
  const s = h.status || 'pending'
  // 处理异常最优先（可重试）
  if (s === 'failed') return { type: 'danger', text: '解析失败' }
  if (s === 'embedding_failed') return { type: 'danger', text: '向量构建失败' }
  // 驳回为终态，无论处理进行到哪一步都优先展示；按驳回阶段区分文案
  if ((h.audit_status || '') === 'rejected') {
    return h.reject_stage === 'compliance'
      ? { type: 'danger', text: '复核驳回' }
      : { type: 'danger', text: '审核驳回' }
  }
  // 处理全部完成（解析 + 图谱/wiki 均 done/skipped）时按审核维度归属
  const g = h.graph_status || 'pending'
  const w = h.wiki_status || 'pending'
  const processingDone = s === 'done' &&
    (g === 'done' || g === 'skipped') &&
    (w === 'done' || w === 'skipped')
  if (processingDone) {
    const audit = h.audit_status || 'pending_team'
    if (audit === 'pending_team') return { type: 'info', text: '待审核' }
    if (audit === 'pending_compliance') return { type: 'warning', text: '待合规复核' }
    return { type: 'success', text: '已完成' }
  }
  // 处理未完成：按处理流水线展示（解析中/切片中/图谱等待构建等）
  const [ptype, ptext] = pipelineStatus(h)
  return { type: ptype === 'default' ? 'info' : ptype, text: ptext }
}

// 版本列表处理状态：复用共享流水线（含图谱/wiki 阶段），与历史列表保持一致
function versionStatusTag(doc) {
  const [ptype, ptext] = pipelineStatus(doc || {})
  return { type: ptype === 'default' ? 'info' : ptype, text: ptext }
}

/* ==========================================================
   文档处理进度弹窗（8 步步骤条：处理线/审核线/构建线）
   ========================================================== */
// 当前查看进度的文档（弹窗标题与步骤条数据源）
const progressDoc = ref(null)

function showDocProgress(doc) {
  if (!doc) {
    ElMessage.error('文档不存在')
    return
  }
  progressDoc.value = doc
  progressSteps.value = buildProgressSteps(doc)
  progressTitle.value = '处理进度 · ' + (doc.title || doc.file_name || '')
  progressVisible.value = true
}

// 步骤状态：done=完成 / active=进行中 / todo=待办 / failed=失败 / skipped=跳过
// 三线并行：处理线(status) → 审核线(audit_status) → 构建线(graph_status + wiki_status)
function buildProgressSteps(doc) {
  const s = doc.status || 'pending'
  const audit = doc.audit_status || 'pending_team'
  const g = doc.graph_status || 'pending'
  const w = doc.wiki_status || 'pending'

  // 处理线：解析 / 切片 / 向量化，按主流水线 status 判定（脱敏并入解析）
  let parseState, parseText
  if (s === 'failed') { parseState = 'failed'; parseText = '解析失败' }
  else if (s === 'parsing' || s === 'desensitizing') { parseState = 'active'; parseText = '解析中' }
  else if (s === 'pending') { parseState = 'todo'; parseText = '等待解析' }
  else { parseState = 'done'; parseText = '解析完成' }

  let chunkState, chunkText
  if (s === 'failed') { chunkState = 'failed'; chunkText = '解析失败' }
  else if (s === 'chunking') { chunkState = 'active'; chunkText = '切片中' }
  else if (s === 'pending' || s === 'parsing' || s === 'desensitizing') { chunkState = 'todo'; chunkText = '等待切片' }
  else { chunkState = 'done'; chunkText = '切片完成' }

  let embedState, embedText
  if (s === 'embedding_failed') { embedState = 'failed'; embedText = '向量构建失败' }
  else if (s === 'embedding') { embedState = 'active'; embedText = '向量构建中' }
  else if (s === 'done') { embedState = 'done'; embedText = '向量构建完成' }
  else { embedState = 'todo'; embedText = '等待向量化' }

  // 审核线：双审（团队审核 → 合规复核），按 audit_status 判定（驳回分支见下方统一处理）
  let audit1State, audit1Text
  if (audit === 'pending_team') { audit1State = 'active'; audit1Text = '待审核' }
  else if (audit === 'pending_compliance' || audit === 'passed') { audit1State = 'done'; audit1Text = '审核通过' }
  else { audit1State = 'todo'; audit1Text = '待审核' }

  let audit2State, audit2Text
  if (audit === 'pending_compliance') { audit2State = 'active'; audit2Text = '待合规复核' }
  else if (audit === 'passed') { audit2State = 'done'; audit2Text = '复核通过' }
  else { audit2State = 'todo'; audit2Text = '等待复核' }

  // 构建线：图谱 / Wiki 仅在解析完成(status=done)后由节点级防抖任务驱动
  const buildStep = (st, name) => {
    if (s !== 'done') return { state: 'todo', text: '等待解析完成' }
    if (st === 'extracting') return { state: 'active', text: name + '中' }
    if (st === 'done') return { state: 'done', text: '构建完成' }
    if (st === 'failed') return { state: 'failed', text: '构建失败' }
    if (st === 'skipped') return { state: 'skipped', text: '未启用' }
    return { state: 'todo', text: '等待构建' }
  }
  const graphStep = buildStep(g, '图谱构建')
  const wikiStep = buildStep(w, 'Wiki生成')

  const steps = [
    { name: '上传', state: 'done', text: '已上传' },
    { name: '解析/脱敏', state: parseState, text: parseText },
    { name: '切片', state: chunkState, text: chunkText },
    { name: '向量化', state: embedState, text: embedText },
    { name: '团队审核', state: audit1State, text: audit1Text },
    { name: '合规复核', state: audit2State, text: audit2Text },
    { name: '图谱构建', state: graphStep.state, text: graphStep.text },
    { name: 'Wiki生成', state: wikiStep.state, text: wikiStep.text },
  ]

  // 驳回为终态：仅驳回阶段标注失败文案，其余步骤统一显示横杠
  if (audit === 'rejected') {
    const rejectStage = doc.reject_stage || 'team'
    const failIdx = rejectStage === 'compliance' ? 5 : 4  // 步骤下标：4=团队审核, 5=合规复核
    const failText = rejectStage === 'compliance' ? '复核驳回' : '审核驳回'
    steps.forEach((step, i) => {
      if (i === failIdx) { step.state = 'failed'; step.text = failText }
      else { step.state = 'skipped'; step.text = '—' }
    })
  }
  return steps
}

/* ==========================================================
   版本历史弹窗（同组版本列表 + 设为活跃）
   ========================================================== */
async function showVersionModal(docId) {
  versionDocId.value = docId
  versionVisible.value = true
  versionLoading.value = true
  versionList.value = []
  versionTitle.value = '版本历史'
  try {
    const data = await api.getJson('/api/v1/knowledge/documents/' + docId + '/versions/')
    const docs = data.documents || []
    versionList.value = docs
    // 弹窗标题取活跃版本标题（无活跃时取最新一条），避免在模板中直接嵌入用户输入
    const titleDoc = docs.find(v => v.is_active) || docs[0]
    if (titleDoc && titleDoc.title) versionTitle.value = '版本历史 · ' + titleDoc.title
  } catch (e) {
    ElMessage.error('加载失败：' + errMsg(e, ''))
  } finally {
    versionLoading.value = false
  }
}

// 设为活跃版本：切换成功后刷新弹窗内版本列表 + 上传历史列表（与旧版 setDocVersionActive 一致）
async function setVersionActive(versionId, docId) {
  try {
    await api.postJson('/api/v1/knowledge/documents/' + versionId + '/set_active/')
    ElMessage.success('已切换为活跃版本')
    showVersionModal(docId)
    loadUploadHistory(historyPage.value)
  } catch (e) {
    ElMessage.error(errMsg(e, '切换失败'))
  }
}

/* ==========================================================
   同内容重复上传对话框（Promise 模式：cancel / force）
   ========================================================== */
// 返回 Promise，选择结果由调用方决定（cancel=跳过 / force=强制新建版本）
function showDuplicateFileDialog(existing) {
  duplicateExisting.value = existing || {}
  duplicateVisible.value = true
  return new Promise((resolve) => {
    duplicateResolver = resolve
  })
}

function onDuplicateAction(action) {
  duplicateVisible.value = false
  if (duplicateResolver) {
    duplicateResolver(action)
    duplicateResolver = null
  }
}

// 弹窗被 ESC/遮罩/关闭按钮关闭时按"取消"处理：
// 避免 Promise 永远悬挂导致批次上传卡死（对应旧版 ESC 监听）
function onDuplicateClosed() {
  if (duplicateResolver) {
    duplicateResolver('cancel')
    duplicateResolver = null
  }
}

// 点击"查看现有文件"只打开预览弹窗，不关闭本对话框：
// 用户查看完内容后可继续决定"取消"或"强制新建版本"
function viewExistingDuplicate() {
  if (duplicateExisting.value.id) openPreview(duplicateExisting.value.id)
}


/* ==========================================================
   工具函数
   ========================================================== */
function fileTypeByExt(name) {
  const ext = name.split('.').pop().toLowerCase()
  const map = {
    pdf: 'PDF', doc: 'Word', docx: 'Word', wps: 'WPS文字',
    md: 'Markdown', txt: 'TXT', rst: 'TXT',
    csv: 'CSV', xlsx: 'Excel', xls: 'Excel', et: 'WPS表格',
    ppt: 'PPT', pptx: 'PPT', dps: 'WPS演示',
    py: 'Python', js: 'JavaScript', ts: 'TypeScript',
    jsx: 'React JSX', tsx: 'React TSX', java: 'Java',
    go: 'Go', rs: 'Rust', c: 'C', cpp: 'C++', h: 'C/C++ Header',
    yml: 'YAML', yaml: 'YAML', json: 'JSON', xml: 'XML',
    toml: 'TOML', ini: 'INI', conf: '配置', cfg: '配置',
    sh: 'Shell', bat: 'Batch', ps1: 'PowerShell', css: 'CSS'
  }
  return map[ext] || ext.toUpperCase()
}

function fileIconByExt(name) {
  const ext = name.split('.').pop().toLowerCase()
  if (ext === 'pdf') return '📕'
  if (['doc', 'docx', 'wps'].includes(ext)) return '📄'
  if (ext === 'md') return '📝'
  if (ext === 'txt' || ext === 'rst') return '📃'
  if (['csv', 'xlsx', 'xls', 'et'].includes(ext)) return '📊'
  if (['ppt', 'pptx', 'dps'].includes(ext)) return '📽️'
  if (['yml', 'yaml'].includes(ext)) return '⚙️'
  if (ext === 'json') return '📊'
  if (['py', 'js', 'ts', 'jsx', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h', 'sh', 'bat', 'ps1'].includes(ext)) return '💻'
  return '📄'
}

// 归属节点展示文案：优先"部门 - 团队"（后端新字段），无部门/团队时回退文件夹名
function nodePathText(row) {
  const parts = []
  if (row.dept_name) parts.push(row.dept_name)
  if (row.team_name) parts.push(row.team_name)
  if (!parts.length) return row.node_name || '-'
  return parts.join(' - ')
}
</script>

<style scoped>
/* ===== 上传页布局：拖拽区 + 面板/历史 ===== */
.upload-page {
  min-height: 100%;
}

/* 覆盖全局 .page-scroll：改为不整体滚动的 flex 列容器（upload-drop 固定顶部，
   历史卡片撑满剩余空间且不超出屏幕，表格主体内部滚动） */
.upload-page .page-scroll {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.header-status {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.header-tag {
  border-radius: 6px;
}

/* 拖拽上传区：固定在页头下方，不随内容滚动 */
.upload-drop {
  flex-shrink: 0;
  margin-bottom: 16px;
}

.upload-drop-inner {
  padding: 10px 0;
  text-align: center;
}

.upload-drop-icon {
  font-size: 40px;
  line-height: 1;
  margin-bottom: 8px;
}

.upload-drop-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--app-text);
  margin-bottom: 6px;
}

.up-primary-text {
  color: #409eff;
}

.upload-drop-desc {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 10px;
}

.upload-types {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
}

.up-type-tag {
  border-radius: 4px;
}

/* 中部区域（上传面板/历史互斥）：撑满剩余空间，内部各自滚动 */
.upload-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 上传选项 */
.upload-options {
  flex-shrink: 0;
  background: var(--app-card-bg);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 16px;
}

.upload-options-row {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.upload-opt-cell {
  flex: 1;
  min-width: 280px;
}

.upload-opt-node {
  flex: 0.58;
  min-width: 144px;
}

.form-label {
  font-size: 13px;
  color: var(--app-text);
  margin-bottom: 8px;
}

.up-required {
  color: #f56c6c;
}

.node-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.vis-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.org-selects {
  display: flex;
  gap: 8px;
}

/* 本次上传文件面板：撑满剩余空间，文件列表内部滚动 */
.upload-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin-bottom: 0;
}

.file-list {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.file-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--app-border);
}

.file-item:hover {
  background: var(--app-bg);
}

.file-item-icon {
  font-size: 22px;
  flex-shrink: 0;
}

.file-item-info {
  flex: 1;
  min-width: 0;
}

.file-item-name {
  font-size: 13px;
  color: var(--app-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-item-meta {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-top: 2px;
}

.file-item-progress {
  width: 140px;
  flex-shrink: 0;
}

.file-item-status {
  width: 80px;
  flex-shrink: 0;
}

/* 整体上传进度 */
.global-progress {
  flex-shrink: 0;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--app-border);
}

.global-progress-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--app-text);
}

.global-progress-text {
  margin-top: 4px;
  font-size: 12px;
  color: var(--app-text-sub);
}

/* 上传历史 */
.history-section {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 筛选条固定在卡片上方，不随表格滚动 */
.filter-bar {
  flex-shrink: 0;
}

.history-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  margin-bottom: 0;
}

/* 表格主体：表头固定、正文撑满剩余空间并内部滚动 */
.history-card :deep(.el-table) {
  flex: 1;
  min-height: 0;
}

.up-pagination {
  flex-shrink: 0;
  margin-top: 16px;
  justify-content: flex-end;
}

/* 归属节点：部门-团队超出列宽时省略，悬停显示完整 */
.up-node-path {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}

/* 表格内文件名 */
.up-row-name {
  font-size: 13px;
  color: var(--app-text);
}

.up-name-deleted {
  text-decoration: line-through;
  color: var(--app-text-sub);
}

/* 已删除行样式：半透明 + 浅红底 */
:deep(.up-row-deleted) {
  opacity: 0.75;
  background: #fef0f0;
}

:deep(.up-row-deleted:hover > td) {
  background: #fde2e2 !important;
}

/* 处理进度弹窗：步骤条 */
.progress-steps {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 60vh;
  overflow: auto;
}

.progress-step {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--app-border);
}

.progress-step.is-active {
  border-color: #409eff;
  background: #ecf5ff;
}

.progress-step-icon {
  width: 20px;
  text-align: center;
  font-size: 14px;
  color: var(--app-text-sub);
  flex-shrink: 0;
}

.progress-step-icon.done { color: #67c23a; }
.progress-step-icon.active { color: #409eff; }
.progress-step-icon.failed { color: #f56c6c; }
.progress-step-icon.skipped { color: var(--el-text-color-placeholder); }

/* 暗色模式：未进行步骤更贴近暗色背景，当前步骤用半透明蓝替代浅蓝底；
   用 :not() 排除已完成/进行中/失败，避免覆盖状态色 */
html.dark .progress-step.is-active {
  border-color: var(--el-color-primary-dark-2);
  background: rgba(64, 158, 255, 0.15);
}

html.dark .progress-step-icon:not(.done):not(.active):not(.failed) {
  color: var(--el-text-color-disabled);
}

.progress-step-name {
  flex: 1;
  font-size: 13px;
  color: var(--app-text);
}



/* 重复文件对话框 */
.dup-desc {
  font-size: 13px;
  color: var(--app-text);
  margin-bottom: 12px;
}

.dup-info {
  background: var(--app-bg);
  border-radius: 6px;
  padding: 10px 14px;
  font-size: 13px;
}

.dup-info div {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  color: var(--app-text-sub);
}

.dup-info b {
  color: var(--app-text);
  font-weight: 500;
  max-width: 70%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>

