import { defineStore } from 'pinia'
import { getUser, saveUser, clearLoginState } from '../utils/authStorage'

// 用户信息 store：从 authStorage 读取登录用户（JWT 登录态 + 用户信息分离存储，
// 存储位置由"记住我"决定：记住→localStorage，不记住→sessionStorage）
export const useUserStore = defineStore('user', {
  state: () => ({
    user: null,          // 后端返回的完整用户对象（含 roles 数组）
    sidebarCollapsed: localStorage.getItem('rag_sidebar_collapsed') === '1'
  }),
  getters: {
    roles() {
      return (this.user?.roles || []).map(r => r.code)
    },
    name() {
      return this.user?.real_name || this.user?.username || '用户'
    },
    avatar() {
      return (this.user?.real_name || this.user?.username || '?').charAt(0)
    },
    displayRole() {
      return this.user?.roles?.length ? this.user.roles[0].name : '用户'
    },
    departmentName() {
      return this.user?.department_name || ''
    },
    isSuperAdmin() {
      return this.roles.includes('super_admin')
    },
    // 可管理文档的角色：超级管理员 / 文档管理员
    isAdminOrOps() {
      return this.hasAnyRole('super_admin', 'kb_admin')
    },
    // 可查看/修改系统配置的角色：超级管理员 / 维护管理员
    isSystemMaintainer() {
      return this.hasAnyRole('super_admin', 'system_maintainer')
    },
    // 拥有管理权限的角色（可见全部管理后台项）：超管/文档/人员/部门经理/团队组长
    isManagerRole() {
      return this.hasAnyRole('super_admin', 'kb_admin', 'user_admin', 'dept_manager', 'team_leader')
    },
    // 只读角色（非 contributor 且无管理角色）：隐藏上传入口
    isReadonly() {
      return !this.hasAnyRole('contributor', 'super_admin', 'kb_admin', 'user_admin', 'dept_manager', 'team_leader')
    }
  },
  actions: {
    // 从存储恢复登录用户信息（页面刷新时调用）
    restore() {
      this.user = getUser()
      return this.user
    },
    // 保存登录用户信息
    setUser(u) {
      this.user = u
      saveUser(u)
    },
    // 判断是否拥有任一角色（与旧 layout.js hasAnyRole 行为一致）
    hasAnyRole(...codes) {
      return codes.some(c => this.roles.includes(c))
    },
    // 清空登录态（登出/过期）
    clear() {
      this.user = null
      clearLoginState()
    },
    toggleSidebarCollapse() {
      this.sidebarCollapsed = !this.sidebarCollapsed
      localStorage.setItem('rag_sidebar_collapsed', this.sidebarCollapsed ? '1' : '0')
    }
  }
})
