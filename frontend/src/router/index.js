import { createRouter, createWebHashHistory } from 'vue-router'
import { getToken } from '../utils/authStorage'

// 旧 MPA 页面名 → hash 路由的映射（/chat/ → #/chat），兼容历史书签与直接访问
const OLD_PATH_TO_ROUTE = {
  index: '/',
  '': '/',
  login: '/login',
  'reset-password': '/reset-password',
  chat: '/chat',
  upload: '/upload',
  profile: '/profile',
  'admin-users': '/admin-users',
  'admin-nodes': '/admin-nodes',
  ticket: '/ticket',
  'ticket-center': '/ticket-center',
  'admin-docs': '/admin-docs',
  'admin-analytics': '/admin-analytics',
  'admin-eval': '/admin-eval',
  'admin-audit': '/admin-audit',
  'admin-rbac': '/admin-rbac',
  'admin-org': '/admin-org',
  'admin-system-config': '/admin-system-config',
  'admin-scheduler': '/admin-scheduler',
  'admin-tasks': '/admin-tasks',
  wiki: '/wiki',
  graph: '/graph'
}

const history = createWebHashHistory()
const router = createRouter({
  history,
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/Login.vue'),
      meta: { public: true, title: '登录' }
    },
    {
      path: '/reset-password',
      name: 'reset-password',
      component: () => import('../views/ResetPassword.vue'),
      meta: { public: true, title: '重置密码' }
    },
    {
      path: '/',
      component: () => import('../layout/AppLayout.vue'),
      children: [
        { path: '', name: 'home', component: () => import('../views/Home.vue'), meta: { title: '工作台' } },
        { path: 'chat', name: 'chat', component: () => import('../views/Chat.vue'), meta: { title: '智能聊天' } },
        { path: 'upload', name: 'upload', component: () => import('../views/Upload.vue'), meta: { title: '文档上传' } },
        { path: 'profile', name: 'profile', component: () => import('../views/Profile.vue'), meta: { title: '个人资料' } },
        { path: 'wiki', name: 'wiki', component: () => import('../views/Wiki.vue'), meta: { title: 'Wiki 知识库' } },
        { path: 'graph', name: 'graph', component: () => import('../views/Graph.vue'), meta: { title: '知识图谱' } },
        { path: 'admin-nodes', name: 'admin-nodes', component: () => import('../views/AdminNodes.vue'), meta: { title: '知识库' } },
        { path: 'admin-docs', name: 'admin-docs', component: () => import('../views/AdminDocs.vue'), meta: { title: '文档审核' } },
        { path: 'admin-users', name: 'admin-users', component: () => import('../views/AdminUsers.vue'), meta: { title: '用户与角色' } },
        { path: 'admin-org', name: 'admin-org', component: () => import('../views/AdminOrg.vue'), meta: { title: '组织架构' } },
        { path: 'admin-rbac', name: 'admin-rbac', component: () => import('../views/AdminRbac.vue'), meta: { title: 'RBAC 权限配置' } },
        { path: 'admin-analytics', name: 'admin-analytics', component: () => import('../views/AdminAnalytics.vue'), meta: { title: '反馈与报表' } },
        { path: 'admin-eval', name: 'admin-eval', component: () => import('../views/AdminEval.vue'), meta: { title: '质量评估' } },
        { path: 'admin-audit', name: 'admin-audit', component: () => import('../views/AdminAudit.vue'), meta: { title: '审计与安全' } },
        { path: 'admin-system-config', name: 'admin-system-config', component: () => import('../views/AdminSystemConfig.vue'), meta: { title: '系统配置' } },
        { path: 'admin-scheduler', name: 'admin-scheduler', component: () => import('../views/AdminScheduler.vue'), meta: { title: '定时任务' } },
        { path: 'admin-tasks', name: 'admin-tasks', component: () => import('../views/AdminTasks.vue'), meta: { title: '任务看板' } },
        { path: 'ticket', name: 'ticket', component: () => import('../views/Ticket.vue'), meta: { title: '工单中心' } },
        { path: 'ticket-center', name: 'ticket-center', component: () => import('../views/TicketCenter.vue'), meta: { title: '工单中心' } }
      ]
    },
    { path: '/:pathMatch(.*)*', redirect: '/' }
  ]
})

// 兼容旧 MPA 直接访问：Django 对所有页面路径都返回 SPA 入口，
// 若 URL 带旧路径名且无 hash，则转换为对应 hash 路由（不刷新页面）
const pageName = window.location.pathname.replace(/\/$/, '').split('/').pop() || ''
if (pageName && !window.location.hash) {
  const target = OLD_PATH_TO_ROUTE[pageName]
  if (target && target !== '/') {
    history.replace(target)
  }
}

// 全局前置守卫：未登录跳登录页；已登录访问登录页跳工作台
router.beforeEach((to) => {
  const token = getToken()
  document.title = to.meta.title ? `${to.meta.title} · 知库 Agent` : '知库 Agent'
  if (!to.meta.public && !token) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta.public && token) {
    return { path: '/' }
  }
})

export default router
