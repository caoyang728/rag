<template>
  <el-container class="app-layout">
    <el-aside :width="collapsed ? '64px' : '220px'" class="app-aside">
      <!-- 顶部固定：logo + 折叠按钮（折叠按钮置于 logo 右侧） -->
      <div class="app-logo" :class="{ 'app-logo-collapsed': collapsed }" @click="router.push('/')">
        <!-- 折叠态隐藏左侧 logo 图标（点击跳转主页），避免与展开侧栏的折叠按钮混淆 -->
        <el-icon v-if="!collapsed" :size="22"><MagicStick /></el-icon>
        <span v-if="!collapsed" class="app-logo-text">知库 Agent</span>
        <el-icon
          class="collapse-btn"
          @click.stop="userStore.toggleSidebarCollapse()"
        >
          <Expand v-if="collapsed" />
          <Fold v-else />
        </el-icon>
      </div>

      <!-- 中间区域：菜单可滚动（超出侧栏高度时滑动） -->
      <el-scrollbar class="app-menu-scroll">
        <el-menu
          :default-active="activePath"
          :collapse="collapsed"
          :collapse-transition="false"
          router
          class="app-menu"
        >
          <template v-for="group in menuGroups" :key="group.label">
            <div v-if="!collapsed" class="menu-group-label">{{ group.label }}</div>
            <el-menu-item
              v-for="item in group.items"
              :key="item.path"
              :index="item.path"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <template #title>
                {{ item.name }}
                <!-- 待审批角标：紧跟"工单中心"label 右侧（原绝对定位于菜单项右侧，现贴住 label） -->
                <span v-if="item.path === '/ticket' && ticketCount > 0" class="ticket-nav-badge">{{ ticketCount > 99 ? '99+' : ticketCount }}</span>
              </template>
              <!-- 折叠态下 title 插槽不渲染，角标单独绝对定位在图标右侧，保持折叠态仍可见 -->
              <span v-if="collapsed && item.path === '/ticket' && ticketCount > 0" class="ticket-nav-badge ticket-nav-badge--collapse">{{ ticketCount > 99 ? '99+' : ticketCount }}</span>
            </el-menu-item>
          </template>
        </el-menu>
      </el-scrollbar>

      <!-- 底部固定：用户信息（含主题切换）+ 退出登录 -->
      <div class="app-user">
        <div class="user-info" :class="{ 'user-info-collapsed': collapsed }" title="进入个人资料" @click="router.push('/profile')">
          <el-avatar :size="32">{{ userStore.avatar }}</el-avatar>
          <div v-if="!collapsed" class="user-meta">
            <div class="user-name">{{ userStore.name }}</div>
            <div class="user-role">{{ userStore.displayRole }}</div>
          </div>
          <!-- 主题切换：并入用户信息行右侧，点击不触发进入个人资料 -->
          <el-tooltip :content="isDark ? '切换到浅色模式' : '切换到暗色模式'" placement="top">
            <el-button text circle class="theme-btn" @click.stop="toggleTheme">
              <el-icon :size="17"><Moon v-if="isDark" /><Sunny v-else /></el-icon>
            </el-button>
          </el-tooltip>
        </div>
        <el-button v-if="!collapsed" class="logout-btn" plain @click="doLogout">
          <el-icon><SwitchButton /></el-icon>&nbsp;退出登录
        </el-button>
        <el-tooltip v-else content="退出登录" placement="right">
          <el-button text circle class="logout-btn" @click="doLogout">
            <el-icon :size="17"><SwitchButton /></el-icon>
          </el-button>
        </el-tooltip>
      </div>
    </el-aside>

    <el-container class="app-main-wrap">
      <!-- 已移除顶部 el-header：内容区直接渲染，最大化可视高度 -->
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ChatDotRound, Collection, DataAnalysis, DocumentChecked,
  Expand, Fold, FolderOpened, Key, Lock, MagicStick, Monitor, Moon,
  OfficeBuilding, Share, Sunny, SwitchButton, Tickets, Timer, Tools,
  UploadFilled, UserFilled, Aim
} from '@element-plus/icons-vue'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { useTheme } from '../composables/useTheme'
import { getRefreshToken, getToken } from '../utils/authStorage'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const { isDark, toggleTheme } = useTheme()

const collapsed = computed(() => userStore.sidebarCollapsed)
const activePath = computed(() => route.path)
const ticketCount = ref(0)

// 有审批身份的角色才轮询待办（普通员工后端短路待办恒为空，无需请求）
const TICKET_APPROVE_ROLES = ['super_admin', 'user_admin', 'kb_admin', 'compliance_admin', 'system_maintainer', 'dept_manager', 'team_leader']
// 轮询间隔：团队组长 2 小时，部门经理与其他管理员 1 小时
const TICKET_INTERVAL_LEADER = 2 * 60 * 60 * 1000
const TICKET_INTERVAL_ADMIN = 1 * 60 * 60 * 1000
let ticketTimer = null

// 侧边栏菜单（与首页功能入口共用同一份数据，保证两处可见性一致）
const menuGroups = computed(() => {
  const kbItems = []
  // 文档上传：只读角色隐藏；知识库：所有登录用户可见
  if (!userStore.isReadonly) {
    kbItems.push({ icon: UploadFilled, name: '文档上传', path: '/upload' })
  }
  kbItems.push({ icon: FolderOpened, name: '知识库', path: '/admin-nodes' })
  kbItems.push({ icon: Collection, name: 'Wiki 知识库', path: '/wiki' })
  kbItems.push({ icon: Share, name: '知识图谱', path: '/graph' })

  const groups = [
    { label: '会话', items: [{ icon: ChatDotRound, name: '智能聊天', path: '/chat' }] },
    { label: '知识库', items: kbItems },
    // 工单中心：所有登录用户开放（后端按角色过滤可见范围）
    { label: '工单', items: [{ icon: Tickets, name: '工单中心', path: '/ticket' }] }
  ]

  const adminItems = []
  // 用户与角色、反馈与报表、审计与安全：仅管理角色可见
  if (userStore.isManagerRole) {
    adminItems.push({ icon: DocumentChecked, name: '文档审核', path: '/admin-docs' })
    adminItems.push({ icon: UserFilled, name: '用户与角色', path: '/admin-users' })
  }
  if (userStore.isManagerRole) {
    adminItems.push({ icon: DataAnalysis, name: '反馈与报表', path: '/admin-analytics' })
    adminItems.push({ icon: Aim, name: '质量评估', path: '/admin-eval' })
    adminItems.push({ icon: Lock, name: '审计与安全', path: '/admin-audit' })
  }
  // 组织架构：管理端 + 组长/部门经理可见
  if (userStore.isAdminOrOps || userStore.hasAnyRole('team_leader', 'dept_manager')) {
    adminItems.push({ icon: OfficeBuilding, name: '组织架构', path: '/admin-org' })
  }
  // RBAC 权限配置：仅超级管理员和文档管理员可见
  if (userStore.isAdminOrOps) {
    adminItems.push({ icon: Key, name: 'RBAC 权限配置', path: '/admin-rbac' })
  }
  // 系统配置：超级管理员 / 维护管理员可见
  if (userStore.isSystemMaintainer) {
    adminItems.push({ icon: Tools, name: '系统配置', path: '/admin-system-config' })
    adminItems.push({ icon: Timer, name: '定时任务', path: '/admin-scheduler' })
    adminItems.push({ icon: Monitor, name: '任务看板', path: '/admin-tasks' })
  }
  if (adminItems.length) groups.push({ label: '管理', items: adminItems })
  return groups
})

// 退出登录：通知后端撤销 refresh token 后清空本地登录态
function doLogout() {
  const refresh = getRefreshToken()
  const access = getToken()
  if (refresh) {
    const headers = { 'Content-Type': 'application/json' }
    if (access) headers['Authorization'] = 'Bearer ' + access
    api.post('/api/v1/auth/logout/', { refresh }, { headers })
      .catch(() => { /* 退出失败不阻断本地登出 */ })
  }
  userStore.clear()
  ElMessage.success('已退出登录')
  router.replace('/login')
}

function setTicketBadge(count) {
  ticketCount.value = count
}

// 轻量查询待审批数量刷新"工单中心"导航角标；轮询失败静默，不打扰用户
function refreshTicketReminder() {
  api.getJson('/api/v1/auth/tickets/?view=pending&page=1&page_size=1')
    .then(res => setTicketBadge(res?.count || 0))
    .catch(() => { /* 静默 */ })
}

function startTicketReminderPolling() {
  stopTicketReminderPolling()
  if (!TICKET_APPROVE_ROLES.some(r => userStore.hasAnyRole(r))) return
  refreshTicketReminder()
  const interval = userStore.hasAnyRole('team_leader') ? TICKET_INTERVAL_LEADER : TICKET_INTERVAL_ADMIN
  ticketTimer = setInterval(refreshTicketReminder, interval)
}

function stopTicketReminderPolling() {
  if (ticketTimer) { clearInterval(ticketTimer); ticketTimer = null }
}

function onVisibilityChange() {
  if (document.hidden) {
    stopTicketReminderPolling()
  } else {
    refreshTicketReminder()
    startTicketReminderPolling()
  }
}

onMounted(() => {
  userStore.restore()
  startTicketReminderPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  stopTicketReminderPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<style scoped>
.app-layout {
  height: 100%;
}

/* 侧栏：纵向三段式布局（logo 固定顶部 / 菜单滚动中部 / 用户区固定底部） */
.app-aside {
  display: flex;
  flex-direction: column;
  background: var(--app-card-bg);
  border-right: 1px solid var(--app-border);
  transition: width 0.2s;
  overflow: hidden;
}

.app-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  height: var(--app-header-height);
  padding: 0 12px;
  cursor: pointer;
  color: var(--el-color-primary);
  font-weight: 600;
  white-space: nowrap;
  border-bottom: 1px solid var(--app-border);
}

.app-logo-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}

.collapse-btn {
  cursor: pointer;
  color: var(--app-text-sub);
  /* 图标与导航菜单图标同为 18px（element-plus 菜单图标固定 18px）；
     固定宽高 32px 作为点击热区，padding 置 0 避免全局 box-sizing:border-box 挤压图标 */
  width: 32px;
  height: 32px;
  padding: 0;
  font-size: 18px;
  border-radius: 8px;
  flex-shrink: 0;
}

/* 折叠态侧栏仅 64px 宽：隐藏 logo 图标与文字后，折叠按钮铺满整行作为展开点击区（18px 图标居中） */
.app-logo-collapsed {
  padding: 0 4px;
  gap: 4px;
}

.app-logo-collapsed .collapse-btn {
  flex: 1;
  height: 100%;
  padding: 0;
}

.collapse-btn:hover {
  color: var(--el-color-primary);
  background: var(--app-menu-hover);
}

.app-menu-scroll {
  flex: 1;
  min-height: 0;
}

/* 折叠态 el-menu 固定 64px 宽，而侧栏 border-right 占 1px、内容区仅 63px，
   禁止横向滚动避免导航栏出现左右滑动 */
.app-menu-scroll :deep(.el-scrollbar__wrap) {
  overflow-x: hidden;
}

/* 折叠态下将菜单宽度收敛到容器内，消除与侧栏边框宽度差产生的 1px 溢出 */
.app-menu-scroll :deep(.el-menu) {
  max-width: 100%;
}

.app-menu {
  border-right: none;
  --el-menu-bg-color: transparent;
  --el-menu-item-height: 44px;
}

/* 角标绝对定位需要，确保菜单项作为定位参照 */
.app-menu :deep(.el-menu-item) {
  position: relative;
}

/* 待审批角标：红色圆角数字，紧跟"工单中心"label 右侧 */
.ticket-nav-badge {
  display: inline-block;
  margin-left: 6px;
  vertical-align: middle;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: #f56c6c;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  text-align: center;
}

/* 折叠态角标：title 插槽被隐藏，改为绝对定位覆盖在菜单项（图标）右侧 */
.ticket-nav-badge--collapse {
  position: absolute;
  top: 50%;
  right: 6px;
  transform: translateY(-50%);
  margin-left: 0;
}

.menu-group-label {
  padding: 12px 16px 4px;
  font-size: 12px;
  color: var(--app-text-sub);
  white-space: nowrap;
}

/* 底部用户区：固定于侧栏最下方 */
.app-user {
  flex-shrink: 0;
  padding: 10px;
  border-top: 1px solid var(--app-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.theme-btn {
  color: var(--app-text-sub);
  flex-shrink: 0;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px;
  border-radius: 8px;
  cursor: pointer;
  min-height: 36px;
}

/* 折叠态：用户信息区纵向排列（头像 + 主题按钮居中堆叠），适配 64px 侧栏宽度 */
.user-info-collapsed {
  flex-direction: column;
  gap: 6px;
  padding: 4px 0;
}

.user-info:hover {
  background: var(--app-menu-hover);
}

.user-meta {
  flex: 1;
  min-width: 0;
}

.user-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-role {
  font-size: 12px;
  color: var(--app-text-sub);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.logout-btn {
  width: 100%;
  color: var(--app-text-sub);
}

.app-main-wrap {
  height: 100%;
}

.app-main {
  padding: 0;
  overflow: hidden;
  background: var(--app-bg);
}
</style>
