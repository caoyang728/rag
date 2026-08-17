<template>
  <div class="page-container admin-rbac-page">
    <!-- 无权限：RBAC 权限配置仅超级管理员可访问（持有 '*' 全权限） -->
    <PageGuard :allowed="userStore.isSuperAdmin" message="仅超级管理员可访问此页面">
      <!-- ===== 页头 ===== -->
      <div class="page-header">
        <div>
          <div class="page-title">RBAC 权限配置</div>
          <div class="page-desc">管理角色与权限分配</div>
        </div>
      </div>

      <!-- ===== 左角色列表 + 右权限分配（page-body 内撑满） ===== -->
      <div class="page-body">
      <div class="rbac-main">
        <!-- 角色列表 -->
        <div class="card role-panel">
          <PanelHeader>
            角色列表
            <template #actions>
              <el-button type="primary" size="small" @click="openRoleModal()">＋ 新增角色</el-button>
            </template>
          </PanelHeader>
          <div v-loading="loading" class="panel-body">
            <el-empty v-if="!loading && !allRoles.length" description="暂无角色" :image-size="60" />
            <div
              v-for="r in allRoles"
              :key="r.id"
              class="role-card"
              :class="{ 'role-card-active': selectedRoleId === r.id }"
              @click="selectRole(r.id)"
            >
              <div class="role-card-header">
                <div class="role-card-info">
                  <span class="role-card-name">{{ r.name }}</span>
                  <span class="role-card-code">{{ r.code }}</span>
                </div>
                <el-tag v-if="r.is_builtin" size="small" type="primary" effect="plain">内置</el-tag>
              </div>
              <div class="role-card-desc">{{ r.description || '—' }}</div>
              <!-- 编辑/删除按钮点击时不触发选中（stopPropagation） -->
              <div class="role-card-actions" @click.stop>
                <el-button size="small" @click="openRoleModal(r.id)">编辑</el-button>
                <el-button size="small" type="danger" @click="deleteRole(r)">删除</el-button>
              </div>
            </div>
          </div>
        </div>

        <!-- 权限分配 -->
        <div class="card perm-panel">
          <PanelHeader>
            权限分配
            <span v-if="selectedRole" class="text-sub text-sm perm-role-name">- {{ selectedRole.name }}</span>
            <template #actions>
              <el-button type="primary" size="small" :disabled="!selectedRoleId || savingPerms" :loading="savingPerms" @click="saveRolePermissions">保存权限</el-button>
            </template>
          </PanelHeader>
          <div v-loading="loading" class="panel-body">
            <el-empty v-if="!selectedRoleId" description="请从左侧选择一个角色" :image-size="60" />
            <template v-else>
              <!-- 权限按模块分组勾选 -->
              <div v-for="(perms, mod) in permGroups" :key="mod" class="perm-group">
                <div class="perm-group-title">{{ moduleLabel(mod) }}</div>
                <el-checkbox-group v-model="checkedPermIds" class="perm-group-items">
                  <el-checkbox v-for="p in perms" :key="p.id" :value="p.id" class="perm-item">
                    <div class="perm-item-info">
                      <div class="perm-item-name">{{ p.name }}</div>
                      <div class="perm-item-code">{{ p.code }}</div>
                    </div>
                  </el-checkbox>
                </el-checkbox-group>
              </div>
              <el-empty v-if="!allPermissions.length" description="暂无权限项" :image-size="60" />
            </template>
          </div>
        </div>
      </div>
      </div>

      <!-- ===== 新增/编辑角色弹窗（复用公共 BaseDialog：固定宽 460px，高度随内容自适应） ===== -->
      <BaseDialog
        v-model="roleModalVisible"
        :title="roleModalTitle"
        width="460px"
        min-width="460px"
        height="auto"
        min-height="0"
        :close-on-click-modal="false"
      >
        <div class="form-item">
          <label class="form-label">角色名称 <span class="required">*</span></label>
          <el-input v-model="roleForm.name" placeholder="如: 审计员" />
        </div>
        <div class="form-item">
          <label class="form-label">角色编码 <span class="required">*</span></label>
          <!-- 内置角色 code 只读，防止权限判定失效 -->
          <el-input v-model="roleForm.code" :disabled="roleCodeDisabled" placeholder="如: auditor（英文小写+下划线）" />
        </div>
        <div class="form-item">
          <label class="form-label">描述</label>
          <el-input v-model="roleForm.desc" placeholder="角色职责描述" />
        </div>
        <template #footer>
          <el-button @click="roleModalVisible = false">取消</el-button>
          <el-button type="primary" :loading="savingRole" @click="saveRole">保存</el-button>
        </template>
      </BaseDialog>
    </PageGuard>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { errMsg } from '../utils/format'
import { useConfirm } from '../composables/useConfirm'
import PanelHeader from '../components/base/PanelHeader.vue'
import BaseDialog from '../components/base/BaseDialog.vue'
import PageGuard from '../components/base/PageGuard.vue'

const userStore = useUserStore()
// 二次确认弹窗统一封装
const { confirm } = useConfirm()

const API_BASE = '/api/v1/auth'

// 权限模块中文名映射
const MODULE_LABELS = {
  knowledge: '知识库',
  user: '用户管理',
  system: '系统配置',
  audit: '审计',
}

/* ==========================================================
   状态（与原 admin-rbac.js 全局变量一一对应）
   ========================================================== */
const allRoles = ref([])
const allPermissions = ref([])
const selectedRoleId = ref(null)
const loading = ref(false)
const savingRole = ref(false)
const savingPerms = ref(false)
// 当前选中角色的权限勾选集合（permission_ids，数字数组）
const checkedPermIds = ref([])

const selectedRole = computed(() => allRoles.value.find(r => r.id === selectedRoleId.value))

// 权限按 module 分组（原 renderPermissionPanel 的 groups 逻辑）
const permGroups = computed(() => {
  const groups = {}
  allPermissions.value.forEach(p => {
    const mod = p.module || 'other'
    if (!groups[mod]) groups[mod] = []
    groups[mod].push(p)
  })
  return groups
})

function moduleLabel(mod) {
  return MODULE_LABELS[mod] || mod
}

// 角色变更提交成功后统一提示：走工单审批，审批通过后生效
function roleTicketToast(resp, actionLabel) {
  const risk = resp.risk_level === 'high' ? '（高风险，需双审）' : ''
  ElMessage.success(`${actionLabel}已提交工单 ${resp.ticket_no}${risk}，审批通过后生效`)
}

async function loadRoles() {
  const data = await api.getJson(`${API_BASE}/roles/`)
  allRoles.value = Array.isArray(data) ? data : (data.results || [])
}

/* ==========================================================
   选择角色 → 渲染权限
   ========================================================== */
function selectRole(roleId) {
  selectedRoleId.value = roleId
  const role = allRoles.value.find(r => r.id === roleId)
  checkedPermIds.value = role && Array.isArray(role.permission_ids) ? role.permission_ids.slice() : []
}

/* ==========================================================
   保存权限（走工单审批：提交后仅提示工单号，权限不立即生效，
   本地角色数据与勾选状态保持不变，审批通过后由审批人刷新页面查看）
   ========================================================== */
async function saveRolePermissions() {
  if (!selectedRoleId.value || savingPerms.value) return
  savingPerms.value = true
  try {
    const resp = await api.postJson(`${API_BASE}/roles/${selectedRoleId.value}/assign-permissions/`, { permission_ids: checkedPermIds.value })
    roleTicketToast(resp, '权限分配')
  } catch (e) { ElMessage.error('提交失败: ' + errMsg(e, '')) }
  finally { savingPerms.value = false }
}

/* ==========================================================
   角色增删改
   ========================================================== */
const roleModalVisible = ref(false)
const roleModalTitle = ref('新增角色')
const roleCodeDisabled = ref(false)
const roleForm = reactive({ id: null, name: '', code: '', desc: '' })

function openRoleModal(id) {
  roleForm.id = id || null
  roleForm.name = ''
  roleForm.code = ''
  roleForm.desc = ''
  roleCodeDisabled.value = false
  roleModalTitle.value = '新增角色'
  if (id) {
    const r = allRoles.value.find(x => x.id === id)
    if (r) {
      roleForm.id = r.id
      roleForm.name = r.name
      roleForm.code = r.code
      roleForm.desc = r.description || ''
      roleModalTitle.value = '编辑角色'
      // 内置角色 code 不可修改，防止权限判定失效
      if (r.is_builtin) roleCodeDisabled.value = true
    }
  }
  roleModalVisible.value = true
}

async function saveRole() {
  if (savingRole.value) return
  const id = roleForm.id
  const name = (roleForm.name || '').trim()
  const code = (roleForm.code || '').trim()
  const desc = (roleForm.desc || '').trim()
  if (!name) { ElMessage.warning('请输入角色名称'); return }
  if (!code) { ElMessage.warning('请输入角色编码'); return }
  if (!/^[a-z][a-z0-9_]*$/.test(code)) { ElMessage.warning('角色编码只能包含小写字母、数字和下划线，且以字母开头'); return }
  savingRole.value = true
  try {
    const body = { name, description: desc }
    let resp
    if (id) {
      // 编辑时：内置角色不提交 code（后端也会拦截）
      const r = allRoles.value.find(x => x.id === parseInt(id))
      if (!r || !r.is_builtin) {
        body.code = code
      }
      resp = await api.patchJson(`${API_BASE}/roles/${id}/`, body)
    } else {
      body.code = code
      resp = await api.postJson(`${API_BASE}/roles/`, body)
    }
    // 角色增改走工单审批：审批通过后生效，提交成功仅提示工单号
    roleTicketToast(resp, id ? '角色编辑' : '角色新增')
    roleModalVisible.value = false
    // 重新拉取角色列表（审批通过前列表不变，保持与服务端一致）
    await loadRoles()
  } catch (e) { ElMessage.error('提交失败: ' + errMsg(e, '')) }
  finally { savingRole.value = false }
}

// 删除角色走工单审批（高风险双审）：确认后提交删除工单，审批通过后软删
function deleteRole(r) {
  confirm({
    message: `删除角色"${r.name}"为高风险操作，需双审，审批通过后生效`,
    title: '删除角色', confirmText: '提交删除工单', errorText: '提交失败',
  }, async () => {
    const resp = await api.deleteJson(`${API_BASE}/roles/${r.id}/`)
    roleTicketToast(resp, '角色删除')
    // 删除的是当前选中角色时清空选择，避免权限面板残留
    if (selectedRoleId.value === r.id) {
      selectedRoleId.value = null
      checkedPermIds.value = []
    }
    await loadRoles()
  })
}

/* ==========================================================
   页面初始化
   ========================================================== */
onMounted(async () => {
  userStore.restore()
  if (!userStore.isSuperAdmin) return
  // 一次性全量加载角色（含 permission_ids）和权限列表
  loading.value = true
  try {
    const [rolesData, permsData] = await Promise.all([
      api.getJson(`${API_BASE}/roles/`),
      api.getJson(`${API_BASE}/permissions/`),
    ])
    allRoles.value = Array.isArray(rolesData) ? rolesData : (rolesData.results || [])
    allPermissions.value = Array.isArray(permsData) ? permsData : (permsData.results || [])
  } catch (e) {
    ElMessage.error('加载失败')
    console.error(e)
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
/* ===== 左右两栏（page-body 内撑满，两侧面板内部滚动） ===== */
.rbac-main {
  display: flex;
  gap: 16px;
  flex: 1;
  min-height: 0;
}

.role-panel {
  width: 360px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
}

.perm-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
  overflow: hidden;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
}

.perm-role-name {
  font-weight: 400;
  margin-left: 8px;
}

.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px;
}

/* ===== 角色卡片 ===== */
.role-card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.role-card:hover {
  border-color: var(--app-border);
  background: var(--app-bg);
}

.role-card-active,
.role-card-active:hover {
  /* 选中态颜色走 Element Plus 语义变量：浅色下浅蓝底，暗色下自动切换为暗蓝底 */
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.role-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}

.role-card-info {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.role-card-name {
  font-weight: 600;
  font-size: 14px;
  color: var(--app-text);
}

.role-card-code {
  font-size: 12px;
  color: var(--app-text-sub);
  font-family: monospace;
}

.role-card-desc {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 8px;
  line-height: 1.4;
}

.role-card-actions {
  display: flex;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.15s ease;
}

/* 仅"选中状态 + 鼠标悬停"时才展示编辑/删除按钮，避免未选中卡片上的按钮干扰视觉 */
.role-card-active:hover .role-card-actions {
  opacity: 1;
}

/* ===== 权限面板 ===== */
.perm-group {
  margin-bottom: 20px;
}

.perm-group-title {
  font-weight: 600;
  font-size: 13px;
  color: var(--app-text);
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--app-border);
}

.perm-group-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.perm-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: 6px;
  width: 100%;
  margin-right: 0;
  height: auto;
}

.perm-item:hover {
  background: var(--app-bg);
}

.perm-item-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.perm-item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--app-text);
}

.perm-item-code {
  font-size: 11px;
  color: var(--app-text-sub);
  font-family: monospace;
}
</style>
