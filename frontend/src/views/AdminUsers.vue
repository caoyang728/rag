<template>
  <div class="page-container admin-users-page">
    <!-- ===== 页头 ===== -->
    <div class="page-header">
      <div>
        <div class="page-title">用户与角色</div>
        <div class="page-desc">检索、新增、编辑、禁用及导出用户</div>
      </div>
    </div>

    <!-- ===== 列表卡片：toolbar 固定 + 表格滚动（复用 .app-card 底，对齐 admin-docs 页面 body 布局） ===== -->
    <div class="page-body">
    <div class="app-card user-list-card">
      <!-- 工具栏 -->
      <div class="toolbar">
        <div class="toolbar-left">
          <!-- 隐藏表单防浏览器自动填充用户名/密码 -->
          <form class="hidden">
            <input type="text" autocomplete="username" readonly />
            <input type="password" autocomplete="current-password" readonly />
          </form>
          <el-input
            v-model="searchKeyword"
            placeholder="搜索用户"
            clearable
            style="width: 180px"
            @input="onSearchInput"
            @clear="searchUsers"
            @keyup.enter="searchUsers"
          />
          <el-select v-model="filterDeptId" placeholder="全部部门" style="width: 130px" @change="onFilterDeptChange">
            <el-option label="全部部门" value="" />
            <el-option v-for="d in filterOptions.departments" :key="d.id" :label="d.name" :value="String(d.id)" />
          </el-select>
          <el-select v-model="filterTeamId" placeholder="全部团队" style="width: 130px" :disabled="!filterDeptId" @change="searchUsers">
            <el-option label="全部团队" value="" />
            <el-option v-for="t in filterTeamsOfDept" :key="t.id" :label="t.name" :value="String(t.id)" />
          </el-select>
          <el-select v-model="filterStatus" placeholder="全部状态" style="width: 110px" @change="searchUsers">
            <el-option label="全部状态" value="" />
            <el-option label="启用" value="active" />
            <el-option label="禁用" value="disabled" />
          </el-select>
          <el-button type="primary" @click="searchUsers">搜索</el-button>
        </div>
        <div class="toolbar-right">
          <el-button @click="downloadImportTemplate">下载模板</el-button>
          <el-button @click="batchImport">批量导入</el-button>
          <el-button @click="batchExport">批量导出</el-button>
          <el-button @click="exportAll">导出全部</el-button>
          <el-button type="primary" @click="openUserModal()">＋ 新建用户</el-button>
          <!-- 隐藏的 CSV 文件选择框：由"批量导入"按钮触发 -->
          <input ref="importFileRef" type="file" accept=".csv" style="display: none" @change="handleImportFile" />
        </div>
      </div>

      <!-- 表格滚动区：固定表头 + 表体内部滚动 -->
      <div class="page-scroll pad-x-12">
      <!-- 用户表格 -->
      <el-table
        :data="users"
        v-loading="listLoading"
        class="user-table"
        row-key="id"
        height="100%"
        @sort-change="onSortChange"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="40" />
        <el-table-column label="用户名" prop="username" sortable="custom" min-width="120" show-overflow-tooltip>
          <template #default="{ row }"><strong>{{ row.username }}</strong></template>
        </el-table-column>
        <el-table-column label="姓名" prop="real_name" sortable="custom" min-width="90" show-overflow-tooltip>
          <template #default="{ row }">{{ row.real_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="邮箱" prop="email" sortable="custom" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.email || '—' }}</template>
        </el-table-column>
        <el-table-column label="部门" min-width="100" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sub">{{ row.department_name || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="团队" min-width="100" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sub">{{ row.team ? row.team.name : '—' }}</span></template>
        </el-table-column>
        <el-table-column label="最后登录" width="140">
          <template #default="{ row }"><span class="text-sub text-sm">{{ row.last_login_at ? formatDate(row.last_login_at) : '—' }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'danger'" size="small" effect="plain">
              {{ row.status === 'active' ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openPermModal(row.id)">权限</el-button>
            <el-button link type="primary" size="small" @click="openUserModal(row.id)">编辑</el-button>
            <el-button link :type="row.status === 'active' ? 'danger' : 'success'" size="small" @click="toggleUserStatus(row)">
              {{ row.status === 'active' ? '禁用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="listEmptyTip" :image-size="60" />
        </template>
      </el-table>
      </div>

      <!-- 分页：固定每页 20 条，不提供每页数量切换 -->
      <AppPagination
        class="pagination-bar"
        :total="userTotal"
        :page-size="pageSize"
        :page="currentPage"
        @page-change="onPageChange"
      />
    </div>
    </div>

    <!-- ===== 新建/编辑用户弹窗（复用公共 BaseDialog：固定宽 600px，高度随内容自适应） ===== -->
    <BaseDialog
      v-model="userModalVisible"
      :title="userModalTitle"
      width="600px"
      min-width="600px"
      height="auto"
      min-height="0"
      :close-on-click-modal="false"
    >
      <div class="user-form-body">
        <input type="hidden" :value="userForm.id" />

        <!-- ===== 基本信息 ===== -->
        <div class="form-section-title">基本信息</div>
        <div class="form-row">
          <div class="form-item flex-1">
            <label class="form-label">用户名 <span class="required">*</span></label>
            <!-- 用户名：新建时可编辑，编辑时禁用（账号不可改） -->
            <el-input v-model="userForm.username" :disabled="isEditMode" placeholder="登录账号" />
          </div>
          <div class="form-item flex-1">
            <label class="form-label">姓名 <span class="required">*</span></label>
            <el-input v-model="userForm.real_name" placeholder="真实姓名" />
          </div>
        </div>
        <div class="form-row">
          <div class="form-item flex-1">
            <label class="form-label">邮箱</label>
            <el-input v-model="userForm.email" type="email" placeholder="用于登录与找回密码" />
          </div>
          <div class="form-item flex-1">
            <label class="form-label">状态</label>
            <el-select v-model="userForm.status" style="width: 100%">
              <el-option label="启用" value="active" />
              <el-option label="禁用" value="disabled" />
            </el-select>
          </div>
        </div>
        <!-- 新建时可选填初始密码：留空由后端生成随机密码（避免可预测性攻击） -->
        <div v-if="!isEditMode" class="form-item">
          <label class="form-label">初始密码</label>
          <el-input v-model="userForm.password" type="password" show-password placeholder="留空则由系统生成随机密码" />
        </div>

        <!-- ===== 组织架构 ===== -->
        <div class="form-section-title">组织架构</div>
        <div class="form-row">
          <div class="form-item flex-1">
            <label class="form-label">部门</label>
            <!-- 越权锁定：组长锁部门+团队，部门经理锁部门；超管/用户管理员可自由修改 -->
            <el-select v-model="userForm.department_id" style="width: 100%" :disabled="deptSelectDisabled" @change="onUserDeptChange">
              <el-option label="— 无 —" value="" />
              <el-option v-for="d in filterOptions.departments" :key="d.id" :label="d.name" :value="String(d.id)" />
            </el-select>
          </div>
          <div class="form-item flex-1">
            <label class="form-label">团队</label>
            <el-select v-model="userForm.team_id" style="width: 100%" :disabled="teamSelectDisabled">
              <el-option :label="teamSelectPlaceholder" value="" />
              <el-option v-for="t in userTeamOptions" :key="t.id" :label="t.name" :value="String(t.id)" />
            </el-select>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="user-modal-footer">
          <!-- 编辑模式才展示删除按钮（软删除，靠左） -->
          <el-button v-if="isEditMode" type="danger" plain class="mr-auto" @click="deleteUserFromModal">删除用户</el-button>
          <el-button @click="userModalVisible = false">取消</el-button>
          <el-button type="primary" :loading="savingUser" @click="saveUser">保存</el-button>
        </div>
      </template>
    </BaseDialog>

    <!-- ===== 用户权限详情弹窗（复用公共 BaseDialog：高度随内容自适应） ===== -->
    <BaseDialog v-model="permModalVisible" :title="permModalTitle" width="720px" min-width="720px" height="auto" min-height="0">
      <div v-loading="permLoading" class="perm-detail-body">
        <el-table v-if="permRows.length" :data="permRows" size="small">
          <el-table-column label="部门" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.dept_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="团队" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.team_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="权限" width="120">
            <template #default="{ row }">
              <el-tag :type="permRoleTagType(row.role_code)" size="small" effect="plain">{{ row.role_name }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="生效时间" width="110">
            <template #default="{ row }"><span class="text-sub">{{ row.effective_from || '—' }}</span></template>
          </el-table-column>
          <el-table-column label="截至日期" width="110">
            <template #default="{ row }"><span class="text-sub">{{ row.expires_at || '永久' }}</span></template>
          </el-table-column>
          <template #empty>
            <el-empty description="该用户暂无任何权限授权" :image-size="60" />
          </template>
        </el-table>
      </div>
      <template #footer>
        <el-button @click="permModalVisible = false">关闭</el-button>
      </template>
    </BaseDialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { escapeHtml, formatDate, errMsg } from '../utils/format'
import { downloadBlob } from '../utils/download'
import { debounce } from '../utils/debounce'
import { usePagination } from '../composables/usePagination'
import { useConfirm } from '../composables/useConfirm'
import { getToken } from '../utils/authStorage'
import BaseDialog from '../components/base/BaseDialog.vue'
import AppPagination from '../components/base/AppPagination.vue'

const userStore = useUserStore()

const USERS_API = '/api/v1/auth/users'

/* ==========================================================
   列表状态
   ========================================================== */
const users = ref([])
// 分页状态：翻页回调统一由 usePagination 管理（loadUsers 内部读取 currentPage/pageSize）
const { page: currentPage, pageSize, onPageChange } = usePagination(loadUsers)
// 二次确认弹窗统一封装
const { confirm } = useConfirm()
const totalCount = ref(0)
const listLoading = ref(false)
const listEmptyTip = ref('加载中...')
// loadUsers 请求序列号，防止竞态（快速筛选/翻页时丢弃过期响应）
let loadSeq = 0
// loadFilterOptions 请求序列号，防止竞态
let filterLoadSeq = 0
// 当前排序：空表示默认 -created_at；order 为 'asc' | 'desc' | ''（取消）
const sortField = ref('')
const sortOrder = ref('')
// 勾选用户 id（批量导出）
const selectedIds = ref([])

// 筛选项（部门/团队/角色），由 /form_options/ 返回
const filterOptions = reactive({ departments: [], teams: [], roles: [], assignable_roles: [] })
const filterDeptId = ref('')
const filterTeamId = ref('')
const filterStatus = ref('')
const searchKeyword = ref('')

/** 团队筛选下拉：仅展示当前所选部门下的团队（未选部门时禁用下拉） */
const filterTeamsOfDept = computed(() => {
  if (!filterDeptId.value) return []
  const deptId = Number(filterDeptId.value)
  return (filterOptions.teams || []).filter(t => t.department_id === deptId)
})

/** 加载筛选项（部门/团队/角色） */
async function loadFilterOptions(force = false) {
  // 已加载且非强制刷新时跳过，避免重复请求
  if (filterOptions.roles && filterOptions.roles.length && !force) return
  const seq = ++filterLoadSeq
  try {
    const data = await api.getJson(USERS_API + '/form_options/')
    if (seq !== filterLoadSeq) return // 过时请求，忽略
    const myRoles = userStore.roles
    const isDeptManager = myRoles.includes('dept_manager')
    const isTeamLeader = myRoles.includes('team_leader')
    // 部门/团队筛选范围：组长和部门经理只看自己的范围（越权防护）
    if (isDeptManager || isTeamLeader) {
      const u = userStore.user || {}
      const myDeptId = u.department_id
      if (myDeptId) {
        // 仅保留自己的部门
        data.departments = (data.departments || []).filter(d => d.id === myDeptId)
        if (isTeamLeader) {
          // 组长仅保留自己所在的团队
          const myTeamIds = u.team ? [u.team.id] : []
          data.teams = (data.teams || []).filter(t => myTeamIds.includes(t.id))
        }
      }
    }
    filterOptions.departments = data.departments || []
    filterOptions.teams = data.teams || []
    filterOptions.roles = data.roles || []
    filterOptions.assignable_roles = data.assignable_roles || []
  } catch (e) {
    console.error('加载筛选项失败:', e)
  }
}

/** 搜索回车/清空：重置到第 1 页并重新加载 */
function searchUsers() {
  currentPage.value = 1
  loadUsers()
}

/** 搜索输入（300ms 防抖，避免每次按键都发请求；由 utils/debounce 统一管理定时器） */
const onSearchInput = debounce(searchUsers, 300)

/** 部门筛选变化：重置团队筛选并联动团队下拉（未选部门时禁用） */
function onFilterDeptChange() {
  filterTeamId.value = ''
  searchUsers()
}

/** 点击表头切换排序：同字段 asc → desc → 取消；不同字段默认升序 */
function onSortChange({ prop, order }) {
  if (!order) {
    // 取消排序：回到默认 -created_at
    sortField.value = ''
    sortOrder.value = ''
  } else {
    sortField.value = prop
    sortOrder.value = order === 'ascending' ? 'asc' : 'desc'
  }
  currentPage.value = 1
  loadUsers()
}

/** 加载用户列表（分页/搜索/筛选/排序，携带请求序号守卫） */
async function loadUsers() {
  const seq = ++loadSeq
  listLoading.value = true
  const params = new URLSearchParams({ page: currentPage.value, page_size: pageSize.value })
  const q = (searchKeyword.value || '').trim()
  if (q) params.set('search', q)
  if (filterDeptId.value) params.set('department_id', filterDeptId.value)
  if (filterTeamId.value) params.set('team_id', filterTeamId.value)
  if (filterStatus.value) params.set('status', filterStatus.value)
  // 排序参数：DRF OrderingFilter 接受 ordering=field（升序）或 ordering=-field（降序）
  if (sortField.value) {
    params.set('ordering', (sortOrder.value === 'desc' ? '-' : '') + sortField.value)
  }
  try {
    const data = await api.getJson(USERS_API + '/?' + params.toString())
    if (seq !== loadSeq) return // 过时请求，忽略
    totalCount.value = data.count || 0
    users.value = data.results || []
    listEmptyTip.value = users.value.length ? '' : '暂无用户'
  } catch (e) {
    if (seq !== loadSeq) return
    users.value = []
    listEmptyTip.value = '加载失败'
    console.error(e)
  } finally {
    if (seq === loadSeq) listLoading.value = false
  }
}

/** 表格勾选变化：记录选中 id（批量导出用） */
function onSelectionChange(rows) {
  selectedIds.value = rows.map(r => r.id)
}

/* ==========================================================
   导出 / 导入 / 模板
   ========================================================== */
/** 批量导出：导出勾选的用户（未勾选时提示） */
async function batchExport() {
  const ids = selectedIds.value
  if (!ids.length) { ElMessage.warning('请先勾选用户'); return }
  try {
    const blob = await api.post(USERS_API + '/batch_export/', JSON.stringify({ ids })).then(r => r.blob())
    downloadBlob(blob, 'users_export.csv')
    ElMessage.success('导出成功')
  } catch (e) {
    ElMessage.error('导出失败: ' + escapeHtml(e.message))
  }
}

/** 导出全部用户 */
async function exportAll() {
  try {
    const blob = await api.post(USERS_API + '/batch_export/', JSON.stringify({ ids: [] })).then(r => r.blob())
    downloadBlob(blob, 'users_export.csv')
    ElMessage.success('导出成功')
  } catch (e) {
    ElMessage.error('导出失败: ' + escapeHtml(e.message))
  }
}

/** 触发隐藏的 file input，选择 CSV 文件 */
function batchImport() {
  importFileRef.value && importFileRef.value.click()
}

const importFileRef = ref(null)

/** 处理 CSV 文件上传：发送到后端批量导入接口，下载带结果列的 CSV */
async function handleImportFile(event) {
  const file = event.target.files[0]
  if (!file) return
  // 重置 input value，允许再次选择同一文件
  event.target.value = ''
  if (!file.name.toLowerCase().endsWith('.csv')) {
    ElMessage.warning('请选择 .csv 文件')
    return
  }
  const formData = new FormData()
  formData.append('file', file)
  try {
    // FormData 上传不能通过 api 对象（会强制设 Content-Type: application/json）
    // 直接用 fetch + 手动携带 token，让浏览器自动设置 multipart/form-data; boundary=...
    const token = getToken() || ''
    const resp = await fetch(USERS_API + '/batch_import/', {
      method: 'POST',
      headers: token ? { 'Authorization': 'Bearer ' + token } : {},
      body: formData
    })
    if (resp.status === 401) {
      ElMessage.error('登录已过期，请重新登录')
      return
    }
    if (!resp.ok) {
      let detail = '导入失败'
      try { const data = await resp.json(); detail = data.detail || detail } catch (e) { /* 忽略解析失败 */ }
      ElMessage.error('导入失败: ' + escapeHtml(detail))
      return
    }
    const blob = await resp.blob()
    // 后端通过自定义 header 返回成功/失败计数
    const successCount = resp.headers.get('X-Import-Success') || '0'
    const failCount = resp.headers.get('X-Import-Fail') || '0'
    downloadBlob(blob, 'users_import_result.csv')
    ElMessage({ message: '导入完成：成功 ' + successCount + ' 条，失败 ' + failCount + ' 条', type: failCount > 0 ? 'warning' : 'success' })
    loadUsers()
  } catch (e) {
    ElMessage.error('导入失败: ' + escapeHtml(e.message))
  }
}

/** 下载 CSV 导入模板（含表头和示例行） */
async function downloadImportTemplate() {
  try {
    const blob = await api.get(USERS_API + '/import_template/').then(r => r.blob())
    downloadBlob(blob, 'users_import_template.csv')
    ElMessage.success('模板已下载')
  } catch (e) {
    ElMessage.error('下载模板失败: ' + escapeHtml(e.message))
  }
}

/* ==========================================================
   新建/编辑用户弹窗
   ========================================================== */
const userModalVisible = ref(false)
const userModalTitle = ref('新建用户')
const savingUser = ref(false)
const isEditMode = ref(false)
// 越权锁定：编辑模式下部门/团队下拉是否禁用
const deptSelectDisabled = ref(false)
const teamSelectDisabled = ref(false)

const userForm = reactive({
  id: null,
  username: '',
  real_name: '',
  email: '',
  status: 'active',
  password: '',
  department_id: '',  // 字符串（'' = 无部门）
  team_id: '',        // 字符串（'' = 无团队）
})

/** 弹窗内团队下拉：按所选部门过滤 */
const userTeamOptions = computed(() => {
  if (!userForm.department_id) return []
  const deptId = Number(userForm.department_id)
  return (filterOptions.teams || []).filter(t => t.department_id === deptId)
})

/** 团队下拉占位文案（未选部门时提示先选部门） */
const teamSelectPlaceholder = computed(() => {
  if (!userForm.department_id) return '请先选择部门'
  if (!userTeamOptions.value.length) return '该部门暂无团队'
  return '— 无 —'
})

/** 当前用户角色与越权信息（与旧版 _getMyRoleInfo 行为一致） */
function myRoleInfo() {
  const u = userStore.user || {}
  const codes = userStore.roles
  const isSuper = codes.includes('super_admin')
  const isUserAdmin = codes.includes('user_admin')
  return {
    isSuper,
    isUserAdmin,
    // 超管/用户管理员可管理任意部门/团队
    canManageAll: isSuper || isUserAdmin,
    isDept: codes.includes('dept_manager'),
    isTeam: codes.includes('team_leader'),
    deptId: u.department_id || 0,
    teamIds: u.team ? [u.team.id] : []
  }
}

/** 部门变更：清空团队选择并联动团队下拉 */
function onUserDeptChange() {
  userForm.team_id = ''
}

/** 打开新建/编辑用户弹窗 */
function openUserModal(id) {
  isEditMode.value = !!id
  userModalTitle.value = id ? '编辑用户' : '新建用户'
  userForm.id = id || null
  userForm.username = ''
  userForm.real_name = ''
  userForm.email = ''
  userForm.status = 'active'
  userForm.password = ''
  userForm.department_id = ''
  userForm.team_id = ''
  deptSelectDisabled.value = false
  teamSelectDisabled.value = false

  const me = myRoleInfo()
  const applyLock = () => {
    // 越权锁定：组长锁部门+团队，部门经理锁部门；超管/用户管理员可自由修改
    if (!me.canManageAll) {
      if (me.isTeam) { deptSelectDisabled.value = true; teamSelectDisabled.value = true }
      else if (me.isDept) deptSelectDisabled.value = true
    }
  }

  const doOpen = () => {
    if (id) {
      // 编辑模式：加载用户详情回填（不含角色：编辑弹窗不提供角色分配，与旧版 admin-users.html 一致）
      loadUserDetail(id).then(u => {
        userForm.username = u.username || ''
        userForm.real_name = u.real_name || ''
        userForm.email = u.email || ''
        userForm.status = u.status || 'active'
        userForm.department_id = u.department_id ? String(u.department_id) : ''
        userForm.team_id = u.team ? String(u.team.id) : ''
        applyLock()
      })
    } else {
      applyLock()
      if (me.canManageAll) {
        // 超管/用户管理员：部门默认"— 无 —"，可自由选择
        userForm.department_id = ''
        userForm.team_id = ''
      } else if (me.isTeam) {
        // 组长：锁定本部门/本团队（单团队时团队也锁定）
        userForm.department_id = String(me.deptId) || ''
        if (me.teamIds.length === 1) {
          userForm.team_id = String(me.teamIds[0])
        }
      } else if (me.isDept) {
        // 部门经理：锁定本部门，团队默认"— 无 —"
        userForm.department_id = String(me.deptId) || ''
      }
    }
    userModalVisible.value = true
  }

  // 筛选项（含可分配角色）未加载时先加载再打开，保证角色下拉可用
  if (!filterOptions.roles.length) {
    loadFilterOptions().then(doOpen)
  } else {
    doOpen()
  }
}

/** 加载用户详情（编辑回填用） */
async function loadUserDetail(id) {
  try {
    return await api.getJson(USERS_API + '/' + id + '/')
  } catch (e) {
    ElMessage.error('获取用户详情失败: ' + errMsg(e, ''))
    throw e
  }
}

/** 删除用户（软删除，二次确认） */
function deleteUserFromModal() {
  const id = userForm.id
  const username = userForm.username
  if (!id) return
  confirm({
    message: '确认删除用户 "' + username + '"？此操作为软删除。',
    title: '删除用户', confirmText: '删除', errorText: '删除失败',
  }, async () => {
    await api.deleteJson(USERS_API + '/' + id + '/')
    userModalVisible.value = false
    loadUsers()
    ElMessage.success('已删除')
  })
}

/** 启用/禁用（二次确认后调用 toggle_status） */
function toggleUserStatus(row) {
  const target = row.status === 'active' ? '禁用' : '启用'
  confirm({
    message: '确认' + target + '用户 "' + row.username + '"？',
    title: target + '用户', confirmText: '确认' + target, errorText: '操作失败',
  }, async () => {
    const data = await api.postJson(USERS_API + '/' + row.id + '/toggle_status/', {})
    ElMessage.success(data.status === 'disabled' ? '已禁用' : '已启用')
    loadUsers()
  })
}

/** 保存用户（新建/编辑） */
async function saveUser() {
  const id = userForm.id
  const teamId = Number(userForm.team_id) || 0
  const deptId = userForm.department_id ? Number(userForm.department_id) : null

  const base = {
    real_name: (userForm.real_name || '').trim(),
    email: (userForm.email || '').trim(),
    department_id: deptId,
    status: userForm.status,
    team_ids: teamId ? [teamId] : []
  }
  // 新建时需验证用户名；角色默认 viewer（人事归属兜底只读，写权限需后续申请 contributor）。
  // 编辑时一律不传 role_ids（后端保留原角色）：编辑弹窗不提供角色分配，与旧版 admin-users.html 一致
  if (!id) {
    const username = (userForm.username || '').trim()
    if (!username) { ElMessage.warning('用户名不能为空'); return }
    base.username = username
    // 初始密码：留空由后端生成随机密码
    if (userForm.password) base.password = userForm.password
    const viewerRole = (filterOptions.roles || []).find(r => r.code === 'viewer')
    if (viewerRole) base.role_ids = [viewerRole.id]
  }
  if (!base.real_name) { ElMessage.warning('姓名为必填'); return }
  savingUser.value = true
  try {
    if (id) {
      await api.patchJson(USERS_API + '/' + id + '/', base)
      ElMessage.success('用户已更新')
      userModalVisible.value = false
      loadUsers()
    } else {
      await api.postJson(USERS_API + '/', base)
      ElMessage.success('用户已创建')
      userModalVisible.value = false
      loadUsers()
    }
  } catch (e) {
    // 409 + USER_REVIVABLE：邮箱命中已删除用户，弹窗询问是否恢复
    // 恢复 → 调用 revive 接口传当前表单数据（覆盖姓名/部门/团队）
    if (e.status === 409 && e.data && e.data.code === 'USER_REVIVABLE' && e.data.revivable_user) {
      const rv = e.data.revivable_user
      const deletedAt = rv.deleted_at ? new Date(rv.deleted_at).toLocaleString('zh-CN') : ''
      await confirm({
        message: '该邮箱曾属于已删除用户 <b>' + escapeHtml(rv.real_name || rv.username) + '</b>（删除于 ' + deletedAt + '）。是否恢复原账号？<br><br>恢复后原账号的姓名/部门/团队将被当前表单内容覆盖，权限重置为查看者（需重新申请）。',
        title: '检测到已删除用户',
        confirmText: '恢复原账号',
        dangerouslyUseHTMLString: true,
        errorText: '恢复失败',
      }, async () => {
        await api.postJson(USERS_API + '/' + rv.id + '/revive/', {
          real_name: base.real_name,
          department_id: base.department_id,
          team_ids: base.team_ids,
          status: base.status
        })
        ElMessage.success('用户已恢复')
        userModalVisible.value = false
        loadUsers()
      })
      return
    }
    ElMessage.error('保存失败: ' + errMsg(e, ''))
  } finally {
    savingUser.value = false
  }
}

/* ==========================================================
   权限详情弹窗
   ========================================================== */
const permModalVisible = ref(false)
const permModalTitle = ref('用户权限详情')
const permLoading = ref(false)
const permRows = ref([])

/** 权限角色徽章颜色（viewer 蓝 / contributor 绿 / 其余橙） */
function permRoleTagType(code) {
  return { viewer: 'info', contributor: 'success' }[code] || 'warning'
}

/** 打开权限详情弹窗：展示用户扁平的授权行（部门-团队-权限-截至日期） */
async function openPermModal(userId) {
  permModalVisible.value = true
  permLoading.value = true
  permRows.value = []
  permModalTitle.value = '用户权限详情'
  try {
    const data = await api.getJson(USERS_API + '/' + userId + '/permission-detail/')
    const u = data.user || {}
    permRows.value = data.rows || []
    // 弹窗标题取用户姓名，避免在模板中直接嵌入用户输入
    permModalTitle.value = '权限详情 · ' + escapeHtml(u.real_name || u.username || '')
  } catch (e) {
    ElMessage.error('加载失败: ' + errMsg(e, String(e)))
  } finally {
    permLoading.value = false
  }
}

/* ==========================================================
   页面初始化 / 清理
   ========================================================== */
onMounted(() => {
  userStore.restore()
  loadFilterOptions()
  loadUsers()
})

onBeforeUnmount(() => {
  onSearchInput.cancel()
})
</script>

<style scoped>
/* ===== 列表卡片：表格与工具条左右留白与卡片边缘对齐（对齐 admin-docs 页面 body 布局） ===== */
.user-list-card {
  display: flex;
  flex-direction: column;
  padding: 0;
  margin: 0;
  overflow: hidden;
  flex: 1;
  min-height: 0;
}

.user-table {
  flex: 1;
  min-height: 0;
}

/* ===== 用户弹窗 ===== */
/* 位于 BaseDialog 的 body 内：height:100% 撑满弹窗可用高度并内部滚动 */
.user-form-body {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}

/* 权限详情弹窗 body：同 BaseDialog 内部滚动模式 */
.perm-detail-body {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
}

.form-section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 8px 0 12px;
  color: var(--app-text);
}

.form-row {
  display: flex;
  gap: 16px;
}

.flex-1 {
  flex: 1;
}

.user-modal-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.mr-auto {
  margin-right: auto;
}

/* 隐藏表单（防浏览器自动填充） */
.hidden {
  display: none;
}
</style>
