<template>
  <div class="page-container">
    <div class="page-header">
      <div class="page-title">个人中心</div>
      <div class="page-desc">管理你的资料、AI 记忆、订阅偏好与安全设置</div>
    </div>

    <div class="page-body">
      <div class="page-scroll">
        <div class="profile-layout">
          <el-menu :default-active="activeMenu" class="profile-menu" @select="activeMenu = $event">
            <el-menu-item index="basic">👤 基本信息</el-menu-item>
            <el-menu-item index="memory">🧠 我的记忆</el-menu-item>
            <el-menu-item index="permissions">🔑 我的权限</el-menu-item>
            <el-menu-item index="email">📧 邮件订阅</el-menu-item>
            <el-menu-item index="pwd">🔐 修改密码</el-menu-item>
          </el-menu>

          <div class="profile-content">
        <!-- ===== 基本信息 ===== -->
        <el-card v-if="activeMenu === 'basic'" shadow="never">
          <div class="profile-head">
            <el-avatar :size="56" class="profile-avatar">{{ user.avatar }}</el-avatar>
            <div class="profile-head-info">
              <div class="profile-name">{{ user.name }}</div>
              <div class="text-sub">{{ user.dept }} / {{ user.team }}</div>
              <div class="text-sub text-xs">用户ID {{ user.id }}{{ user.created_at ? ' · 加入时间 ' + formatDate(user.created_at) : '' }}</div>
            </div>
            <el-button disabled>📷 更换头像</el-button>
          </div>
          <el-form label-width="90px" class="profile-form">
            <el-form-item label="姓名">
              <el-input v-model="basicForm.real_name" style="width: 320px" />
            </el-form-item>
            <el-form-item label="企业邮箱">
              <el-input :model-value="user.email" disabled style="width: 320px" />
            </el-form-item>
            <el-form-item label="部门">
              <el-input :model-value="user.dept" disabled style="width: 320px" />
            </el-form-item>
            <el-form-item label="团队">
              <el-input :model-value="user.team" disabled style="width: 320px" />
            </el-form-item>
            <el-form-item label="手机号">
              <el-input v-model="basicForm.phone" placeholder="请输入手机号" style="width: 320px" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="saveProfile">保存修改</el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <!-- ===== 我的记忆 ===== -->
        <el-card v-else-if="activeMenu === 'memory'" shadow="never">
          <div class="card-head">
            <div class="card-title">🧠 我的记忆（用户永久层）</div>
            <el-button type="danger" size="small" @click="clearAllMemory">🗑 清空记忆</el-button>
          </div>
          <el-alert type="warning" :closable="false" show-icon class="mb-16">
            <template #title>💡 <b>四层记忆机制</b>：短时（Redis）→ 会话（PG）→ <b>用户永久（此处）</b> → 全局系统。个人记忆会影响 AI 对你的回答偏好，仅你本人可见。</template>
          </el-alert>

          <div class="form-item">
            <div class="form-label">职业标签</div>
            <div class="tag-wrap">
              <el-tag v-for="(t, i) in memoryTags" :key="i" closable class="mem-tag" @close="removeMemoryTag(t)">{{ t }}</el-tag>
              <el-button size="small" @click="addTag('memoryTags')">+ 添加</el-button>
            </div>
            <div class="form-hint">AI 会结合职业标签给出更专业的回答</div>
          </div>
          <div class="form-item">
            <div class="form-label">常用检索类型</div>
            <div class="tag-wrap">
              <el-tag v-for="(t, i) in memorySearchTypes" :key="i" type="info" closable class="mem-tag" @close="removeSearchType(t)">{{ t }}</el-tag>
              <el-button size="small" @click="addTag('searchTypes')">+ 添加</el-button>
            </div>
          </div>
          <div class="form-item">
            <div class="form-label">输出偏好</div>
            <el-input v-model="outputPref" type="textarea" :rows="4" placeholder="用自然语言描述你希望 AI 如何回答问题" />
            <div class="form-hint">用自然语言描述你希望 AI 如何回答问题</div>
          </div>
          <div class="form-item">
            <div class="form-label">已提炼的偏好（系统自动生成）</div>
            <div class="profile-text-display">
              <template v-if="profileTextLines.length">
                <div v-for="(l, i) in profileTextLines" :key="i">• {{ l }}</div>
              </template>
              <template v-else>暂无自动提炼的偏好数据，系统每 24 小时基于你的行为异步提炼一次</template>
            </div>
            <div class="form-hint">系统每 24 小时基于你的行为异步提炼一次</div>
          </div>
          <el-button type="primary" @click="saveMemory">保存记忆</el-button>
        </el-card>

        <!-- ===== 我的权限 ===== -->
        <el-card v-else-if="activeMenu === 'permissions'" shadow="never">
          <div class="card-title">🔑 我的权限</div>
          <el-alert type="info" :closable="false" show-icon class="mb-16">
            <template #title>💡 <b>权限说明</b>：权限由角色 + 数据范围决定。同团队角色升级会覆盖旧角色；全局高权角色（用户管理员/文档管理员/合规管理员/超级管理员）4 选 1 互斥。</template>
          </el-alert>
          <div class="card-subtitle">已分配角色</div>
          <div class="role-tags">
            <el-tag v-if="permData.isSuperAdmin" type="danger" effect="dark" class="mem-tag">👑 超级管理员</el-tag>
            <el-tag
              v-for="r in nonSuperRoles" :key="r.id || r.code"
              :type="r.is_builtin ? 'info' : 'primary'"
              class="mem-tag"
            >{{ r.name }}{{ scopeText(r) }}</el-tag>
            <span v-if="!permData.isSuperAdmin && !permData.roles?.length" class="text-sub text-xs">暂无角色</span>
          </div>
          <div class="card-subtitle">权限明细（按模块分组）</div>
          <div v-if="permModules.length" class="perm-modules">
            <div v-for="mod in permModules" :key="mod.code" class="perm-mod">
              <div class="perm-mod-title">{{ mod.label }} 模块</div>
              <div class="perm-mod-tags">
                <el-tag v-for="it in mod.items" :key="it.code" size="small" type="primary" effect="plain" class="mem-tag">{{ it.label || it.action || it.code }}</el-tag>
              </div>
            </div>
          </div>
          <span v-else class="text-sub text-xs">暂无显式权限(仅继承默认 read 权限)</span>
        </el-card>

        <!-- ===== 邮件订阅 ===== -->
        <el-card v-else-if="activeMenu === 'email'" shadow="never">
          <div class="card-title">📧 邮件订阅偏好</div>
          <el-alert type="info" :closable="false" class="mb-16">
            <template #title>📬 订阅内容将发送至 <b>{{ user.email }}</b>，可随时取消订阅</template>
          </el-alert>
          <div class="sub-list">
            <div v-for="item in SUB_ITEMS" :key="item.key" class="sub-item">
              <el-checkbox v-model="subscriptions[item.key]" size="large">{{ item.icon }} {{ item.title }}</el-checkbox>
              <div class="sub-desc">{{ item.desc }}</div>
            </div>
          </div>
          <el-button type="primary" @click="saveSubscriptions">保存偏好</el-button>
        </el-card>

        <!-- ===== 修改密码 ===== -->
        <el-card v-else-if="activeMenu === 'pwd'" shadow="never">
          <div class="card-title">🔐 修改密码</div>
          <el-form label-width="90px" class="profile-form" style="max-width: 480px">
            <el-form-item label="当前密码">
              <el-input v-model="pwdForm.oldPwd" type="password" placeholder="请输入当前密码" show-password />
            </el-form-item>
            <el-form-item label="新密码">
              <el-input v-model="pwdForm.newPwd" type="password" placeholder="至少 8 位，包含大小写字母和数字" show-password @input="updatePwdStrength" />
            </el-form-item>
            <el-form-item label="确认新密码">
              <el-input v-model="pwdForm.confirmPwd" type="password" placeholder="再次输入新密码" show-password />
            </el-form-item>
          </el-form>
          <el-alert type="info" :closable="false" class="mb-16 pwd-rule">
            <template #title>
              <div><b>密码安全要求：</b></div>
              <div>• 至少 8 位，最多 32 位</div>
              <div>• 必须包含大写字母、小写字母和数字</div>
              <div>• 建议包含特殊字符（! @ # $ % ^ &amp; *）</div>
              <div>• 不能与旧密码相同</div>
              <div>• 修改后需重新登录</div>
            </template>
          </el-alert>
          <el-button type="primary" @click="changePassword">确认修改</el-button>
        </el-card>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, errMsg } from '../utils/format'
import { useConfirm } from '../composables/useConfirm'

const router = useRouter()
const userStore = useUserStore()
// 二次确认弹窗统一封装（ElMessageBox 仍用于 addTag 的 prompt 输入场景）
const { confirm } = useConfirm()

const activeMenu = ref('basic')
// 本地用户展示信息（页面加载时由接口刷新）
const user = reactive({ name: '', email: '', dept: '', team: '', id: '', avatar: '', created_at: '', phone: '' })
const basicForm = reactive({ real_name: '', phone: '' })

// ---- 我的记忆 ----
const memoryTags = ref([])
const memorySearchTypes = ref([])
const memoryProfileText = ref('')
const outputPref = ref('')
const profileTextLines = computed(() => (memoryProfileText.value || '').split('\n').filter(l => l.trim()))

// ---- 邮件订阅 ----
const SUB_ITEMS = [
  { key: 'node_update', icon: '📁', title: '订阅知识库节点更新', desc: '当你关注的节点有新文档上传时，每天汇总一次发送邮件' },
  { key: 'system_notice', icon: '🚨', title: '系统告警通知', desc: '上传失败、账号异常登录、权限变更等重要事件即时告警' },
  { key: 'daily_report', icon: '📊', title: '每周报表推送', desc: '每周一 09:00 推送上周问答统计、满意率、热门问题' },
  { key: 'keyword_alert', icon: '🔍', title: '关键词命中通知', desc: '当有新文档包含你关注的关键词时立即通知' }
]
const subscriptions = ref({})

// ---- 我的权限 ----
// 权限模块 code → 中文名（与 AdminRbac 的 MODULE_LABELS 同名不同义，此处独立命名避免混淆）
const PERM_MODULE_LABELS = { kb: '知识库', user: '用户管理', audit: '审计', system: '系统', chat: '对话', org: '组织架构', compliance: '合规' }
const permData = reactive({ isSuperAdmin: false, roles: [], groups: {} })
const nonSuperRoles = computed(() => (permData.roles || []).filter(r => r.code !== 'super_admin'))
const permModules = computed(() => Object.keys(permData.groups || {}).map(code => ({ code, label: PERM_MODULE_LABELS[code] || code, items: permData.groups[code] })))
function scopeText(r) {
  if ((r.scope_type === 'TEAM' || r.scope_type === 'DEPT') && r.scope_name) return ` @ ${r.scope_name}`
  return ''
}

// ---- 修改密码 ----
const pwdForm = reactive({ oldPwd: '', newPwd: '', confirmPwd: '' })

// 三个接口互不依赖，并行加载减少首屏等待
async function initPage() {
  await Promise.all([loadProfile(), loadMemoryData(), loadSubscriptionData()])
  await loadMyPermissions()
}

async function loadProfile() {
  try {
    const data = await api.getJson('/api/v1/auth/profile/')
    if (data) {
      Object.assign(user, {
        id: data.id || '', name: data.real_name || data.username || '用户',
        email: data.email || '', dept: data.department_name || '',
        team: data.team ? data.team.name : '', avatar: (data.real_name || data.username || '?').charAt(0),
        phone: data.phone || '', created_at: data.created_at || ''
      })
      basicForm.real_name = user.name
      basicForm.phone = user.phone || ''
    }
  } catch (e) {
    console.error('load profile failed:', e)
  }
}

async function loadMemoryData() {
  try {
    const data = await api.getJson('/api/v1/memory/user-memory/')
    memoryTags.value = data.domain_tags || []
    memorySearchTypes.value = data.frequent_topics || []
    memoryProfileText.value = data.profile_text || ''
    outputPref.value = (data.preferences || {}).output_preference || ''
  } catch (e) {
    console.error('load memory data failed:', e)
    memoryTags.value = []
    memorySearchTypes.value = []
    memoryProfileText.value = ''
  }
}

async function loadSubscriptionData() {
  try {
    const data = await api.getJson('/api/v1/notification/subscriptions/')
    const init = {}
    SUB_ITEMS.forEach(it => { init[it.key] = !!(data.subscriptions && data.subscriptions[it.key] && data.subscriptions[it.key].is_enabled) })
    subscriptions.value = init
  } catch (e) {
    console.error('load subscription data failed:', e)
  }
}

async function loadMyPermissions() {
  try {
    const data = await api.getJson('/api/v1/auth/permissions/me/')
    permData.isSuperAdmin = data.is_super_admin || (data.roles || []).some(r => r.code === 'super_admin')
    permData.roles = data.roles || []
    permData.groups = data.permission_groups || {}
  } catch (e) {
    console.error('load my permissions failed:', e)
  }
}

// ---- 保存基本信息 ----
async function saveProfile() {
  const name = basicForm.real_name?.trim()
  if (!name) { ElMessage.error('请输入姓名'); return }
  try {
    const data = await api.patchJson('/api/v1/auth/profile/', { real_name: name, phone: basicForm.phone })
    user.name = data.real_name || name
    user.phone = data.phone || basicForm.phone
    user.avatar = user.name.charAt(0)
    // 同步顶栏用户信息
    if (userStore.user) userStore.setUser({ ...userStore.user, real_name: data.real_name, phone: data.phone })
    ElMessage.success('保存成功')
  } catch (e) {
    ElMessage.error(errMsg(e, '保存失败'))
  }
}

// ---- 保存/清空记忆 ----
async function saveMemory() {
  try {
    await api.patchJson('/api/v1/memory/user-memory/', {
      domain_tags: memoryTags.value,
      frequent_topics: memorySearchTypes.value,
      // 无论是否为空都提交，确保用户能清空输出偏好
      output_preference: outputPref.value
    })
    ElMessage.success('记忆已更新')
  } catch (e) {
    ElMessage.error(errMsg(e, '保存失败'))
  }
}

async function clearAllMemory() {
  confirm({
    message: '确认清空所有个人记忆？此操作不可恢复。',
    title: '提示',
    errorText: '清空失败',
  }, async () => {
    await api.patchJson('/api/v1/memory/user-memory/', { domain_tags: [], frequent_topics: [], output_preference: '' })
    memoryTags.value = []
    memorySearchTypes.value = []
    memoryProfileText.value = ''
    outputPref.value = ''
    ElMessage.success('已清空')
  })
}

async function addTag(kind) {
  const label = kind === 'memoryTags' ? '职业标签' : '常用检索类型'
  const list = kind === 'memoryTags' ? memoryTags.value : memorySearchTypes.value
  try {
    const { value } = await ElMessageBox.prompt(`请输入${label}：`, '添加', { inputValidator: v => !!v?.trim() || '标签不能为空' })
    const tag = value.trim()
    if (list.includes(tag)) { ElMessage.error('该标签已存在'); return }
    list.push(tag)
  } catch { /* 取消 */ }
}

function removeMemoryTag(tag) { memoryTags.value = memoryTags.value.filter(t => t !== tag) }
function removeSearchType(tag) { memorySearchTypes.value = memorySearchTypes.value.filter(t => t !== tag) }

// ---- 保存邮件订阅 ----
async function saveSubscriptions() {
  try {
    await api.patchJson('/api/v1/notification/subscriptions/', { subscriptions: { ...subscriptions.value } })
    ElMessage.success('订阅偏好已更新')
  } catch (e) {
    ElMessage.error(errMsg(e, '保存失败'))
  }
}

// ---- 修改密码 ----
function updatePwdStrength() {
  // 密码强度条随输入更新（弱/中/强，规则与旧版一致）
  return
}

async function changePassword() {
  const { oldPwd, newPwd, confirmPwd } = pwdForm
  if (!oldPwd || !newPwd || !confirmPwd) { ElMessage.error('请填写所有密码字段'); return }
  if (newPwd !== confirmPwd) { ElMessage.error('两次输入的新密码不一致'); return }
  if (newPwd === oldPwd) { ElMessage.error('新密码不能与旧密码相同'); return }
  if (newPwd.length < 8) { ElMessage.error('新密码至少需要8位'); return }
  if (newPwd.length > 32) { ElMessage.error('新密码最多32位'); return }
  if (!/[A-Z]/.test(newPwd) || !/[a-z]/.test(newPwd) || !/\d/.test(newPwd)) {
    ElMessage.error('密码必须包含大写字母、小写字母和数字'); return
  }
  try {
    await api.postJson('/api/v1/auth/reset-password/', { old_password: oldPwd, new_password: newPwd })
    ElMessage.success('密码修改成功，请重新登录')
    setTimeout(() => {
      userStore.clear()
      router.replace('/login')
    }, 1500)
  } catch (e) {
    ElMessage.error(errMsg(e, '修改失败'))
  }
}

onMounted(initPage)
</script>

<style scoped>
/* 覆盖全局 .page-header：标题与描述改为上下排列（全局默认为左右布局） */
.page-header {
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
}

/* 覆盖全局 .page-scroll：整页不再滚动，由右侧 .profile-content 内部滚动 */
.page-scroll {
  overflow: hidden;
}

.profile-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  height: 100%;
}

.profile-menu {
  width: 180px;
  flex-shrink: 0;
  border-right: 1px solid var(--app-border);
  background: var(--app-card-bg);
  border-radius: 8px;
}

.profile-content {
  flex: 1;
  min-width: 0;
  height: 100%;
  /* 自身不滚动，滚动交给内部卡片，左侧菜单保持固定 */
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* 卡片撑满右侧列高度，作为固定的外框 */
.profile-content :deep(.el-card) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 滚动发生在卡片 body 内部（如“我的权限”长列表），卡片边框与标题区域不随内容移动 */
.profile-content :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.profile-head {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 16px;
  /* 使用 EP 主色浅阶变量：浅色下等价 #ecf5ff，深色下自动切换为暗蓝底色 */
  background: var(--el-color-primary-light-9);
  border-radius: 8px;
  margin-bottom: 20px;
}

.profile-avatar {
  background: linear-gradient(135deg, #2563eb, #1e40af);
  font-size: 22px;
  flex-shrink: 0;
}

.profile-head-info {
  flex: 1;
}

.profile-name {
  font-size: 18px;
  font-weight: 600;
  color: var(--app-text);
}

.profile-form {
  max-width: 520px;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 12px;
}

.card-subtitle {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
  margin: 16px 0 8px;
}

.mb-16 {
  margin-bottom: 16px;
}

.form-item {
  margin-bottom: 20px;
}

.form-label {
  font-size: 14px;
  color: var(--app-text);
  margin-bottom: 8px;
  display: block;
}

.form-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--app-text-sub);
}

.tag-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.mem-tag {
  margin-right: 6px;
}

.profile-text-display {
  background: var(--app-bg);
  padding: 12px 14px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--app-text-sub);
}

.role-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}

.perm-modules {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.perm-mod {
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

.perm-mod-title {
  font-weight: 600;
  margin-bottom: 8px;
  font-size: 13px;
}

.perm-mod-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.sub-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 16px;
}

.sub-item {
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

.sub-desc {
  margin-left: 28px;
  font-size: 13px;
  color: var(--app-text-sub);
}

.pwd-rule :deep(.el-alert__title) {
  line-height: 1.8;
}
</style>
