<template>
  <div class="page-container admin-org-page">
    <!-- 无权限：仅超管/文档管理员可管理，组长/部门经理可做成员授权（协作角色入口） -->
    <PageGuard :allowed="canAccessOrgPage()" message="仅超级管理员和用户管理员可访问此页面">
      <!-- ===== 页头 ===== -->
      <div class="page-header">
        <div>
          <div class="page-title">组织架构管理</div>
          <div class="page-desc">管理组织架构（部门/团队）</div>
        </div>
      </div>

      <!-- ===== 内容区：卡片撑满页面高度，部门列表在卡片体内滚动 ===== -->
      <div class="page-body">
      <!-- ===== 部门管理 ===== -->
      <div class="card org-card">
        <PanelHeader>
          部门管理
          <!-- 新增部门仅管理端可见（组长/部门经理只做成员授权） -->
          <template #actions>
            <el-button v-if="isOrgManager()" type="primary" size="small" @click="openDeptModal()">＋ 新增部门</el-button>
          </template>
        </PanelHeader>
        <div v-loading="deptLoading" class="org-card-body">
          <el-empty v-if="!deptLoading && !visibleDepts.length" description="暂无部门" :image-size="60" />
          <div v-for="d in visibleDepts" :key="d.id" class="dept-card">
            <div class="dept-card-head">
              <div class="dept-card-meta">
                <span class="dept-card-name">{{ d.name }}</span>
                <span class="text-sub text-sm">{{ d.code || '—' }}</span>
                <el-tag size="small" type="info" effect="plain">{{ d.user_count || 0 }} 人</el-tag>
                <span v-if="d.leader_name" class="text-sub text-sm">· 经理: {{ d.leader_name }}</span>
              </div>
              <div class="dept-card-actions">
                <el-button v-if="isOrgManager()" size="small" @click="openDeptModal(d.id)">编辑</el-button>
                <el-button v-if="canManageTeamOfDept(d)" size="small" @click="openTeamManageModal(d.id)">管理团队</el-button>
                <!-- 授权成员按"能否提单"控制：超管全量兜底 + 部门经理管辖部门（kb_admin 仅参与审核不直接提单） -->
                <el-button v-if="canGrantDept(d)" size="small" @click="openGrantModal('dept', d.id, d.name)">授权成员</el-button>
                <el-button v-if="isOrgManager()" size="small" type="primary" @click="openNominateModal('dept', d.id, 'dept_manager', d.name)">任命经理</el-button>
                <el-button v-if="isOrgManager()" size="small" type="danger" @click="deleteDept(d)">删除</el-button>
              </div>
            </div>
            <!-- 部门下团队标签（带组长名） -->
            <div v-if="(d.teams || []).length" class="dept-card-teams">
              <el-tag v-for="t in d.teams" :key="t.id" size="small" type="primary" effect="plain" class="dept-team-tag">
                {{ t.name }}<span v-if="t.leader_name"> ({{ t.leader_name }})</span>
              </el-tag>
            </div>
          </div>
        </div>
      </div>
      </div>

      <!-- ===== 部门新增/编辑弹窗（复用公共 BaseDialog，高度随内容自适应） ===== -->
      <BaseDialog
        v-model="deptModalVisible"
        :title="deptModalTitle"
        width="460px"
        min-width="460px"
        height="auto"
        min-height="0"
        :close-on-click-modal="false"
      >
        <div class="form-item">
          <label class="form-label">部门名称 <span class="required">*</span></label>
          <el-input v-model="deptForm.name" placeholder="如: 研发部" />
        </div>
        <div class="form-item">
          <label class="form-label">编码 <span class="form-hint">（留空自动生成拼音首字母）</span></label>
          <el-input v-model="deptForm.code" placeholder="如: yfb" />
        </div>
        <div class="form-item">
          <label class="form-label">部门经理 <span class="form-hint">（通过"任命经理"发起审批工单）</span></label>
          <div class="text-sub text-sm">{{ deptLeaderDisplay }}</div>
        </div>
        <template #footer>
          <el-button @click="deptModalVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="saveDept">保存</el-button>
        </template>
      </BaseDialog>

      <!-- ===== 团队管理弹窗（按部门展示团队列表，复用公共 BaseDialog） ===== -->
      <BaseDialog
        v-model="teamManageVisible"
        :title="teamManageTitle"
        width="560px"
        min-width="560px"
        height="auto"
        min-height="0"
        :close-on-click-modal="false"
      >
        <div v-loading="deptLoading" class="team-manage-list">
          <el-empty v-if="currentManageDept && !(currentManageDept.teams || []).length" description="暂无团队" :image-size="60" />
          <div v-for="t in currentManageDept ? (currentManageDept.teams || []) : []" :key="t.id" class="team-card">
            <div class="team-card-info">
              <span class="team-card-name">{{ t.name }}</span>
              <span class="text-sub text-sm">{{ t.user_count || 0 }} 人</span>
              <span v-if="t.leader_name" class="team-card-leader">TL: {{ t.leader_name }}</span>
            </div>
            <div class="team-card-actions">
              <el-button v-if="isOrgManager()" size="small" @click="openTeamForm(t.id)">编辑</el-button>
              <!-- 授权成员按能否提单控制：超管全量兜底 + 管辖团队（get_user_managed_teams 已含组长本团队/团队属地授权/部门经理属地授权部门下的全部团队） -->
              <el-button v-if="canGrantTeam(t)" size="small" @click="openGrantModal('team', t.id, t.name)">授权成员</el-button>
              <el-button v-if="isOrgManager()" size="small" type="primary" @click="openNominateModal('team', t.id, 'team_leader', t.name)">任命组长</el-button>
              <el-button v-if="isOrgManager()" size="small" type="danger" @click="deleteTeam(t)">删除</el-button>
            </div>
          </div>
        </div>
        <template #footer>
          <div class="team-manage-footer">
            <!-- 新增团队仅管理端可见 -->
            <el-button v-if="isOrgManager()" type="primary" size="small" @click="openTeamForm()">＋ 新增团队</el-button>
            <el-button @click="teamManageVisible = false">关闭</el-button>
          </div>
        </template>
      </BaseDialog>

      <!-- ===== 团队新增/编辑弹窗（表单内联，提交即创建组织变更工单，复用公共 BaseDialog） ===== -->
      <BaseDialog
        v-model="teamFormVisible"
        :title="teamFormTitle"
        width="480px"
        min-width="480px"
        height="auto"
        min-height="0"
        :close-on-click-modal="false"
      >
        <div class="form-item">
          <label class="form-label">团队名称 <span class="required">*</span></label>
          <el-input v-model="teamForm.name" placeholder="如: AI 平台组" @keyup.enter="submitTeamForm" />
        </div>
        <div class="form-item">
          <label class="form-label">编码 <span class="form-hint">（留空自动生成，含部门前缀）</span></label>
          <el-input v-model="teamForm.code" placeholder="如: yfzx_aiptz" @keyup.enter="submitTeamForm" />
        </div>
        <div class="form-item">
          <label class="form-label">描述</label>
          <el-input v-model="teamForm.desc" placeholder="团队描述" @keyup.enter="submitTeamForm" />
        </div>
        <template #footer>
          <el-button @click="teamFormVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitTeamForm">{{ teamForm.id ? '提交修改' : '提交新增' }}</el-button>
        </template>
      </BaseDialog>

      <!-- ===== 任命管理岗 / 协作角色授权弹窗（上级发起授予工单，复用公共 BaseDialog；nominate-dialog 类用于放开用户搜索结果下拉的溢出裁剪） ===== -->
      <BaseDialog
        v-model="nominateVisible"
        :title="nominateTitle"
        width="460px"
        min-width="460px"
        height="auto"
        min-height="0"
        :close-on-click-modal="false"
        class="nominate-dialog"
      >
        <div class="form-item">
          <label class="form-label">任命岗位</label>
          <div class="text-sub text-sm">{{ nominateRoleDisplay }}</div>
        </div>
        <!-- 协作角色授权模式（查看者/贡献者）才显示：角色与授权范围选择 -->
        <template v-if="nominateMode === 'grant'">
          <div class="form-item">
            <label class="form-label">授权角色</label>
            <el-select v-model="grantRole" style="width: 100%" @change="loadGrantChainHint">
              <el-option label="查看者" value="viewer" />
              <el-option label="贡献者" value="contributor" />
            </el-select>
          </div>
          <div class="form-item">
            <label class="form-label">授权范围</label>
            <el-select v-model="grantScope" style="width: 100%">
              <el-option v-for="opt in grantScopeOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
          </div>
        </template>
        <div ref="nominateSearchWrap" class="form-item nominate-search-wrap">
          <label class="form-label">被任命人 <span class="required">*</span></label>
          <el-input v-model="nominateUserSearch" placeholder="搜索用户姓名..." autocomplete="off" @input="searchNominateUser" @focus="searchNominateUser" />
          <!-- 用户搜索结果下拉 -->
          <div v-if="nominateResultsVisible" class="nominate-results">
            <div v-if="!nominateUserResults.length" class="nominate-empty">无匹配用户</div>
            <div v-for="u in nominateUserResults" :key="u.id" class="nominate-item" @mousedown.prevent="selectNominateUser(u)">
              <span class="nominate-item-name">{{ u.real_name || u.username }}</span>
              <span class="text-sub text-sm">{{ u.email || '' }}</span>
            </div>
          </div>
        </div>
        <div class="form-item">
          <label class="form-label">任命理由 <span class="required">*</span></label>
          <el-input v-model="nominateReason" type="textarea" :rows="2" placeholder="如: 组织任命/团队管理需要" />
        </div>
        <div class="text-sub text-sm">{{ nominateChainHint }}</div>
        <template #footer>
          <el-button @click="nominateVisible = false">取消</el-button>
          <el-button type="primary" :loading="saving" @click="submitNominate">{{ nominateMode === 'grant' ? '提交授权' : '提交任命' }}</el-button>
        </template>
      </BaseDialog>
    </PageGuard>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { errMsg } from '../utils/format'
import { debounce } from '../utils/debounce'
import { useConfirm } from '../composables/useConfirm'
import PanelHeader from '../components/base/PanelHeader.vue'
import BaseDialog from '../components/base/BaseDialog.vue'
import PageGuard from '../components/base/PageGuard.vue'

const userStore = useUserStore()
// 二次确认弹窗统一封装
const { confirm } = useConfirm()

const API_BASE = '/api/v1/auth'

/* ==========================================================
   状态（与原 admin-org.js 全局变量一一对应）
   ========================================================== */
const allDepts = ref([])
const deptLoading = ref(false)
const saving = ref(false)
// 当前用户管辖范围（从 profile 实时拉取，避免 localStorage.rag_user 过期）：
// _managedTeamIds: 可授权成员的团队（组长/部门经理属地授权/本团队）
// _managedDeptIds: 可授权成员的部门（部门经理属地授权）
const managedTeamIds = ref(new Set())
const managedDeptIds = ref(new Set())
const currentManageDeptId = ref(null)
// 用户搜索竞态序号（防抖定时器由 utils/debounce 统一管理）
let nominateSearchSeq = 0

/* ==========================================================
   权限辅助（与旧 layout.js / admin-org.js 行为一致）
   ========================================================== */
// 组织架构管理功能（部门/团队 CRUD + 任命管理岗）：仅超级管理员 / 文档管理员
function isOrgManager() {
  return userStore.isAdminOrOps
}

// 页面可访问：管理端 + 团队组长 + 部门经理（协作角色授权入口）
function canAccessOrgPage() {
  return userStore.isAdminOrOps || userStore.hasAnyRole('team_leader', 'dept_manager')
}

// 当前用户可见的部门列表：管理端全量；组长/部门经理仅管辖部门
// （部门经理属地授权部门 + 包含管辖团队的部门，保证组长能找到本团队所在部门）
const visibleDepts = computed(() => {
  if (isOrgManager()) return allDepts.value
  return allDepts.value.filter(d => {
    if (managedDeptIds.value.has(d.id)) return true
    return (d.teams || []).some(t => managedTeamIds.value.has(t.id))
  })
})

// 部门卡片按钮：管理团队是否可见（管理端 / 管辖部门 / 含管辖团队的部门）
function canManageTeamOfDept(d) {
  if (isOrgManager()) return true
  if (managedDeptIds.value.has(d.id)) return true
  return (d.teams || []).some(t => managedTeamIds.value.has(t.id))
}

// 授权成员按钮按"能否提单"控制：超管全量兜底 + 部门经理管辖部门
// （kb_admin 仅参与审核不直接提单，除非其兼任组长/部门经理）
function canGrantDept(d) {
  return userStore.isSuperAdmin || managedDeptIds.value.has(d.id)
}

// 团队授权成员：超管全量兜底 + 管辖团队（get_user_managed_teams 已含
// 组长本团队/团队属地授权/部门经理属地授权部门下的全部团队）
function canGrantTeam(t) {
  return userStore.isSuperAdmin || managedTeamIds.value.has(t.id)
}

/* ==========================================================
   数据加载
   ========================================================== */
// 管辖范围实时刷新（本地登录态可能过期），失败时按无管辖范围降级
async function loadManagedScopes() {
  try {
    const p = await api.getJson('/api/v1/auth/profile/')
    managedTeamIds.value = new Set(p.managed_team_ids || [])
    managedDeptIds.value = new Set(p.managed_dept_ids || [])
  } catch (e) {
    console.error('加载管辖范围失败:', e)
  }
}

async function loadDepts() {
  deptLoading.value = true
  try {
    const data = await api.getJson(`${API_BASE}/departments/`)
    allDepts.value = Array.isArray(data) ? data : (data.results || [])
  } catch (e) {
    ElMessage.error('加载失败: ' + errMsg(e, '未知错误'))
    console.error(e)
  } finally {
    deptLoading.value = false
  }
}

/* ==========================================================
   任命管理岗 / 协作角色授权
   ========================================================== */
const nominateVisible = ref(false)
const nominateTitle = ref('任命组长')
const nominateMode = ref('nominate')      // 'nominate' 管理岗任命 / 'grant' 协作角色授权
const nominateTargetType = ref('')        // 'dept' | 'team'
const nominateTargetId = ref(null)
const nominateRoleKey = ref('')           // 管理岗模式隐藏角色：team_leader / dept_manager
const nominateUserId = ref(null)
const nominateUserSearch = ref('')
const nominateReason = ref('')
const nominateUserResults = ref([])
const nominateResultsVisible = ref(false)
const nominateRoleDisplay = ref('')
const nominateChainHint = ref('')
const grantRole = ref('viewer')
const grantScope = ref('')
const grantScopeOptions = ref([])
const nominateSearchWrap = ref(null)

// 管理岗名额唯一预检：任命前检查目标团队/部门是否已有现任组长/经理
// （后端 create_ticket 校验 5 为权威兜底，此处提前提示避免无效提单；
//  换人需先撤销现任，现任本人续期不拦截 —— 与后端一致）
function findExistingLeader(targetType, targetId) {
  const id = String(targetId)
  if (targetType === 'dept') {
    const d = allDepts.value.find(x => String(x.id) === id)
    if (d && d.leader_id) return d.leader_name || '现任经理'
    return ''
  }
  for (const d of allDepts.value) {
    const t = (d.teams || []).find(x => String(x.id) === id)
    if (t && t.leader_id) return t.leader_name || '现任组长'
  }
  return ''
}

// 任命管理岗：applicant=当前操作者，target_user=被任命者，审批通过后由工单执行同步 leader_id + 角色授权
function openNominateModal(targetType, targetId, roleKey, targetName) {
  // 名额唯一预检：已有现任组长/经理时提示先撤销（与后端校验 5 对齐）
  if (roleKey === 'team_leader' || roleKey === 'dept_manager') {
    const existing = findExistingLeader(targetType, targetId)
    if (existing) {
      ElMessage.warning(`该${targetType === 'team' ? '团队' : '部门'}已有${existing},如需更换请先撤销现任后再任命`)
      return
    }
  }
  nominateMode.value = 'nominate'
  nominateTargetType.value = targetType
  nominateTargetId.value = targetId
  nominateRoleKey.value = roleKey
  nominateUserId.value = null
  nominateUserSearch.value = ''
  nominateReason.value = ''
  nominateUserResults.value = []
  nominateResultsVisible.value = false
  nominateRoleDisplay.value = (roleKey === 'team_leader' ? '团队组长' : '部门经理') + ' · ' + targetName
  nominateTitle.value = roleKey === 'team_leader' ? '任命团队组长' : '任命部门经理'
  nominateChainHint.value = ''
  // 加载审批链概要（提示本任命走哪些审批环节）
  loadNominateChainHint(roleKey)
  nominateVisible.value = true
}

// 协作角色授权：团队卡片入口范围固定为当前团队；部门卡片入口可选部门级或该部门下任一团队
function openGrantModal(targetType, targetId, targetName) {
  nominateMode.value = 'grant'
  nominateTargetType.value = targetType
  nominateTargetId.value = targetId
  nominateRoleKey.value = ''
  nominateUserId.value = null
  nominateUserSearch.value = ''
  nominateReason.value = ''
  nominateUserResults.value = []
  nominateResultsVisible.value = false
  grantRole.value = 'viewer'
  nominateRoleDisplay.value = '查看者 / 贡献者 · ' + targetName
  nominateTitle.value = '授权成员'
  // 填充授权范围：团队入口仅当前团队；部门入口为部门级 + (仅超管兜底)部门下各团队
  // 部门经理在部门卡片只走部门级授权（覆盖部门下全部团队），团队级由组长在团队卡片提单
  const scopeOptions = []
  if (targetType === 'team') {
    scopeOptions.push({ value: `TEAM:${targetId}`, label: `团队 · ${targetName}` })
  } else {
    scopeOptions.push({ value: `DEPT:${targetId}`, label: `部门级 · ${targetName}` })
    if (userStore.isSuperAdmin) {
      const dept = allDepts.value.find(d => String(d.id) === String(targetId))
      ;((dept && dept.teams) || []).forEach(t => {
        scopeOptions.push({ value: `TEAM:${t.id}`, label: `团队 · ${t.name}` })
      })
    }
  }
  grantScopeOptions.value = scopeOptions
  grantScope.value = scopeOptions.length ? scopeOptions[0].value : ''
  nominateChainHint.value = ''
  loadGrantChainHint()
  nominateVisible.value = true
}

// 协作角色审批链提示：viewer/contributor 的审批流在 assignable-roles?purpose=self 下返回
async function loadGrantChainHint() {
  try {
    const data = await api.getJson('/api/v1/auth/permissions/assignable-roles/?purpose=self')
    const role = (data.rows || []).find(r => r.role_key === grantRole.value)
    if (role && role.approval_desc) nominateChainHint.value = '审批流: ' + role.approval_desc
  } catch (e) { /* 审批流提示加载失败不阻断授权 */ }
}

// 管理岗任命审批链提示：在 assignable-roles?purpose=management 下返回
async function loadNominateChainHint(roleKey) {
  try {
    const data = await api.getJson('/api/v1/auth/permissions/assignable-roles/?purpose=management')
    const role = (data.rows || []).find(r => r.role_key === roleKey)
    if (role && role.approval_desc) nominateChainHint.value = '审批流: ' + role.approval_desc
  } catch (e) { /* 审批流提示加载失败不阻断任命 */ }
}

// 用户搜索：300ms 防抖，避免每次按键都发请求（定时器由 utils/debounce 统一管理）
const searchNominateUser = debounce(doSearchNominateUser, 300)

async function doSearchNominateUser() {
  const seq = ++nominateSearchSeq
  const q = (nominateUserSearch.value || '').trim()
  if (!q) {
    nominateResultsVisible.value = false
    return
  }
  try {
    const data = await api.getJson(`${API_BASE}/users/search/?q=${encodeURIComponent(q)}`)
    // 竞态检查：若有更新的请求已发出，丢弃本次结果
    if (seq !== nominateSearchSeq) return
    nominateUserResults.value = data.users || []
    nominateResultsVisible.value = true
  } catch (e) {
    console.error('搜索用户失败:', e)
  }
}

function selectNominateUser(u) {
  nominateUserId.value = u.id
  nominateUserSearch.value = u.real_name || u.username
  nominateResultsVisible.value = false
}

// 提交任命/授权工单：协作模式角色与范围由下拉决定；管理岗任命模式沿用隐藏字段
async function submitNominate() {
  if (saving.value) return
  const userId = nominateUserId.value
  const reason = (nominateReason.value || '').trim()
  if (!userId) { ElMessage.warning('请选择被授权人'); return }
  if (!reason) { ElMessage.warning('请填写授权理由'); return }
  let roleKey, scopeType, scopeId
  if (nominateMode.value === 'grant') {
    roleKey = grantRole.value
    const scopeVal = grantScope.value || ''
    const idx = scopeVal.indexOf(':')
    if (idx === -1) { ElMessage.warning('请选择授权范围'); return }
    scopeType = scopeVal.slice(0, idx)
    scopeId = parseInt(scopeVal.slice(idx + 1))
  } else {
    roleKey = nominateRoleKey.value
    scopeType = nominateTargetType.value === 'dept' ? 'DEPT' : 'TEAM'
    scopeId = parseInt(nominateTargetId.value)
  }
  saving.value = true
  try {
    const body = {
      role_key: roleKey,
      scope_type: scopeType,
      scope_id: scopeId,
      change_type: 'GRANT',
      target_user_id: parseInt(userId),
      reason: reason,
    }
    const resp = await api.postJson('/api/v1/auth/permissions/applications/', body)
    ElMessage.success((resp.detail || '申请已提交') + (resp.ticket_no ? `（${resp.ticket_no}）` : ''))
    nominateVisible.value = false
    await loadDepts()
  } catch (e) {
    ElMessage.error('提交失败: ' + errMsg(e, ''))
  } finally {
    saving.value = false
  }
}

/* ==========================================================
   部门管理（部门增删改统一走组织变更工单：提交后由后端 create_org_ticket
   创建审批工单，审批通过后工单执行层落库（删除为高风险双审，增改为单审），
   页面仅提示工单号；第 1~3 层知识节点由部门/团队生命周期自动同步）
   ========================================================== */
const deptModalVisible = ref(false)
const deptModalTitle = ref('新增部门')
const deptForm = reactive({ id: null, name: '', code: '' })
const deptLeaderDisplay = ref('')

function openDeptModal(id) {
  deptForm.id = id || null
  deptForm.name = ''
  deptForm.code = ''
  deptLeaderDisplay.value = ''
  deptModalTitle.value = '新增部门'
  if (id) {
    const d = allDepts.value.find(x => x.id === id)
    if (d) {
      deptForm.id = d.id
      deptForm.name = d.name
      deptForm.code = d.code || ''
      // 部门经理由任命工单设置，编辑弹窗仅展示当前经理
      deptLeaderDisplay.value = d.leader_name ? `当前经理: ${d.leader_name}` : '当前无经理,可通过"任命经理"发起工单'
      deptModalTitle.value = '编辑部门'
    }
  }
  deptModalVisible.value = true
}

async function saveDept() {
  if (saving.value) return
  const id = deptForm.id
  const name = (deptForm.name || '').trim()
  const code = (deptForm.code || '').trim()
  if (!name) { ElMessage.warning('请输入部门名称'); return }
  const body = { name, code: code || undefined }
  saving.value = true
  try {
    let resp
    if (id) {
      resp = await api.patchJson(`${API_BASE}/departments/${id}/`, body)
    } else {
      resp = await api.postJson(`${API_BASE}/departments/`, body)
    }
    ElMessage.info(`已创建审批工单 ${resp.ticket_no}，审批通过后生效`)
    deptModalVisible.value = false
    await loadDepts()
  } catch (e) { ElMessage.error('提交失败: ' + errMsg(e, '')) }
  finally { saving.value = false }
}

// 部门删除：确认后提交删除工单（高风险，需双审后生效）
function deleteDept(d) {
  confirm({
    message: `删除部门"${d.name}"需双审，审批通过后生效`,
    title: '删除部门', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    const res = await api.deleteJson(`${API_BASE}/departments/${d.id}/`)
    ElMessage.info(`已创建审批工单 ${res.ticket_no}，需双审后生效`)
    await loadDepts()
  })
}

/* ==========================================================
   团队管理
   ========================================================== */
const teamManageVisible = ref(false)
const teamManageTitle = ref('管理团队')

// 团队管理弹窗展示的部门（按当前管辖部门 ID 查找，数据随 loadDepts 刷新）
const currentManageDept = computed(() => allDepts.value.find(d => d.id === currentManageDeptId.value))

function openTeamManageModal(deptId) {
  currentManageDeptId.value = deptId
  const dept = allDepts.value.find(d => d.id === deptId)
  teamManageTitle.value = `管理团队 - ${dept ? dept.name : ''}`
  teamManageVisible.value = true
}

// 团队新增/编辑弹窗（提交即创建组织变更工单，审批通过后生效）
const teamFormVisible = ref(false)
const teamFormTitle = ref('新增团队')
const teamForm = reactive({ id: null, name: '', code: '', desc: '' })

function openTeamForm(teamId) {
  const dept = currentManageDept.value
  if (!dept) return
  const isEdit = !!teamId
  const team = isEdit ? (dept.teams || []).find(t => t.id === teamId) : null
  teamForm.id = isEdit ? teamId : null
  teamForm.name = team ? team.name : ''
  teamForm.code = team ? (team.code || '') : ''
  teamForm.desc = team ? (team.description || '') : ''
  teamFormTitle.value = isEdit ? '编辑团队' : '新增团队'
  teamFormVisible.value = true
}

async function submitTeamForm() {
  if (saving.value) return
  const name = (teamForm.name || '').trim()
  const code = (teamForm.code || '').trim()
  const desc = (teamForm.desc || '').trim()
  if (!name) { ElMessage.warning('请输入团队名称'); return }
  // 团队组长通过"任命组长"发起工单设置，不随组织基本信息一起提交
  const body = { name, code: code || undefined, description: desc || undefined, department_id: currentManageDeptId.value }
  saving.value = true
  try {
    let resp
    if (teamForm.id) {
      resp = await api.patchJson(`${API_BASE}/teams/${teamForm.id}/`, body)
    } else {
      resp = await api.postJson(`${API_BASE}/teams/`, body)
    }
    teamFormVisible.value = false
    ElMessage.info(`已创建审批工单 ${resp.ticket_no}，审批通过后生效`)
    await loadDepts()
  } catch (e) { ElMessage.error(errMsg(e, '提交失败')) }
  finally { saving.value = false }
}

// 团队删除：确认后提交删除工单（高风险，需双审后生效）
function deleteTeam(t) {
  confirm({
    message: `删除团队"${t.name}"需双审，审批通过后生效`,
    title: '删除团队', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    const res = await api.deleteJson(`${API_BASE}/teams/${t.id}/`)
    ElMessage.info(`已创建审批工单 ${res.ticket_no}，需双审后生效`)
    await loadDepts()
  })
}

/* ==========================================================
   页面初始化 / 清理
   ========================================================== */
// 点击任命弹窗外部时收起用户搜索结果下拉
function onDocClick(e) {
  const wrap = nominateSearchWrap.value
  if (wrap && !wrap.contains(e.target)) {
    nominateResultsVisible.value = false
  }
}

onMounted(() => {
  userStore.restore()
  if (!canAccessOrgPage()) return
  // 组长/部门经理的管辖范围实时刷新（本地登录态可能过期），失败时按无管辖范围降级
  loadManagedScopes()
  loadDepts()
  document.addEventListener('click', onDocClick)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  searchNominateUser.cancel()
})
</script>

<style scoped>
/* ===== 部门管理卡片 ===== */
/* 卡片撑满 page-body 剩余高度：页头固定，部门列表在 .org-card-body 内部滚动 */
.org-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
}

.org-card-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px;
}

/* ===== 部门卡片 ===== */
.dept-card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 8px;
  transition: border-color 0.15s ease;
}

.dept-card:hover {
  border-color: var(--app-border);
}

.dept-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.dept-card-name {
  font-weight: 600;
  font-size: 15px;
  color: var(--app-text);
}

.dept-card-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.dept-card-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.dept-card-teams {
  margin-top: 8px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.dept-team-tag {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ===== 团队管理弹窗 ===== */
.team-manage-list {
  max-height: 50vh;
  overflow-y: auto;
}

.team-manage-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

.team-card {
  background: var(--app-bg);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  transition: background 0.15s ease;
}

.team-card:hover {
  background: #ecf5ff;
}

.team-card-info {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}

.team-card-name {
  font-weight: 500;
  font-size: 14px;
  color: var(--app-text);
}

.team-card-leader {
  font-size: 13px;
  color: var(--app-text-sub);
}

.team-card-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

/* ===== 用户搜索结果下拉 ===== */
/* 任命/授权弹窗：用户搜索结果下拉为绝对定位浮层，会溢出弹窗 body/面板。
   BaseDialog 默认 overflow:hidden 会裁剪浮层，此处仅对本弹窗（nominate-dialog 类）放开裁剪，
   恢复与旧 el-dialog 一致的浮层效果；el-dialog teleport 到 body，需用 :global() 穿透 */
:global(.nominate-dialog.nominate-dialog),
:global(.nominate-dialog.nominate-dialog .el-dialog__body) {
  overflow: visible;
}

/* 相对定位父容器：让绝对定位的搜索结果下拉以输入框为准对齐 */
.nominate-search-wrap {
  position: relative;
}

.nominate-results {
  position: absolute;
  z-index: 3000;
  width: 100%;
  max-height: 220px;
  overflow-y: auto;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
  margin-top: 4px;
}

.nominate-empty {
  padding: 10px 14px;
  font-size: 13px;
  color: var(--app-text-sub);
}

.nominate-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 14px;
  cursor: pointer;
  transition: background 0.15s ease;
}

.nominate-item:hover {
  background: var(--app-bg);
}

.nominate-item-name {
  font-weight: 500;
  color: var(--app-text);
}
</style>
