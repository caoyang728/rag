<template>
  <div class="page-container">
    <div class="page-header">
      <div class="page-title">工作台</div>
      <div class="page-desc">知库 Agent 企业私有化多场景智能 RAG 知识库平台</div>
    </div>

    <div class="page-body">
      <div class="page-scroll">
        <div v-for="group in menuGroups" :key="group.label" class="home-section">
          <div class="home-section-title">
            <el-icon :size="16" color="#409eff"><component :is="group.groupIcon" /></el-icon>
            {{ group.label }}
          </div>
          <div class="home-grid">
            <div
              v-for="item in group.items"
              :key="item.path"
              class="home-card"
              @click="router.push(item.path)"
            >
              <div class="home-card-icon">
                <el-icon :size="24" color="#409eff"><component :is="item.icon" /></el-icon>
              </div>
              <div class="home-card-info">
                <div class="home-card-title">{{ item.name }}</div>
                <div class="home-card-desc">{{ item.desc }}</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  Aim, ChatDotRound, Collection, DataAnalysis, DocumentChecked, FolderOpened,
  Key, Lock, Monitor, OfficeBuilding, Share, Tickets, Timer, Tools,
  UploadFilled, User, UserFilled, Setting
} from '@element-plus/icons-vue'
import { useUserStore } from '../stores/user'

const router = useRouter()
const userStore = useUserStore()

// 功能入口卡片按角色分组动态渲染，与侧边栏菜单保持一致的可见性
const menuGroups = computed(() => {
  const kbItems = []
  if (!userStore.isReadonly) {
    kbItems.push({ icon: UploadFilled, name: '文档上传', path: '/upload', desc: '上传 PDF / Word / MD，自动解析与向量化' })
  }
  kbItems.push({ icon: FolderOpened, name: '知识库', path: '/admin-nodes', desc: '知识库树形结构与文档维护' })
  kbItems.push({ icon: Collection, name: 'Wiki 知识库', path: '/wiki', desc: '浏览 LLM 自动生成的 Wiki 页面' })
  kbItems.push({ icon: Share, name: '知识图谱', path: '/graph', desc: '图谱可视化、实体检索与社区浏览' })

  const groups = [
    { group: '会话', groupIcon: ChatDotRound, items: [{ icon: ChatDotRound, name: '智能聊天', path: '/chat', desc: '基于 RAG 的多轮问答，支持多知识库检索' }] },
    { group: '知识库', groupIcon: FolderOpened, items: kbItems },
    { group: '工单', groupIcon: Tickets, items: [{ icon: Tickets, name: '工单中心', path: '/ticket', desc: '权限/配置/定时/模型工单统一审批' }] },
    { group: '账户', groupIcon: User, items: [{ icon: User, name: '个人资料', path: '/profile', desc: '查看与维护个人账号信息' }] }
  ]

  const adminItems = []
  if (userStore.isManagerRole) {
    adminItems.push({ icon: DocumentChecked, name: '文档审核', path: '/admin-docs', desc: '文档发布双审与合规复核' })
    adminItems.push({ icon: UserFilled, name: '用户与角色', path: '/admin-users', desc: '管理用户、角色与 RBAC 权限' })
  }
  if (userStore.isManagerRole) {
    adminItems.push({ icon: DataAnalysis, name: '反馈与报表', path: '/admin-analytics', desc: '用户反馈收集与准确率分析' })
    adminItems.push({ icon: Aim, name: '质量评估', path: '/admin-eval', desc: 'RAG 质量评估与回归分析' })
    adminItems.push({ icon: Lock, name: '审计与安全', path: '/admin-audit', desc: '操作审计日志与安全策略' })
  }
  if (userStore.isAdminOrOps || userStore.hasAnyRole('team_leader', 'dept_manager')) {
    adminItems.push({ icon: OfficeBuilding, name: '组织架构', path: '/admin-org', desc: '部门与团队结构管理' })
  }
  if (userStore.isAdminOrOps) {
    adminItems.push({ icon: Key, name: 'RBAC 权限配置', path: '/admin-rbac', desc: '角色权限矩阵配置' })
  }
  if (userStore.isSystemMaintainer) {
    adminItems.push({ icon: Tools, name: '系统配置', path: '/admin-system-config', desc: '系统运行参数与模型管理' })
    adminItems.push({ icon: Timer, name: '定时任务', path: '/admin-scheduler', desc: 'Beat 调度时间与启停配置（需审批）' })
    adminItems.push({ icon: Monitor, name: '任务看板', path: '/admin-tasks', desc: 'Celery 任务执行状态与失败重试' })
  }
  if (adminItems.length) groups.push({ group: '管理', groupIcon: Setting, items: adminItems })
  return groups
})
</script>

<style scoped>
.home-section {
  margin-bottom: 24px;
}

.home-section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 12px;
}

.home-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.home-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 16px;
  cursor: pointer;
  transition: all 0.2s;
}

.home-card:hover {
  border-color: #409eff;
  box-shadow: 0 4px 12px rgba(64, 158, 255, 0.15);
  transform: translateY(-2px);
}

.home-card-icon {
  width: 48px;
  height: 48px;
  border-radius: 8px;
  background: #ecf5ff;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.home-card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
}

.home-card-desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--app-text-sub);
}
</style>
