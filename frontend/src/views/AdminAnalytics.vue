<template>
  <div class="page-container admin-analytics-page">
    <!-- 权限守卫：反馈与报表仅管理角色（超管/文档/用户/部门经理/团队组长）可见 -->
    <PageGuard :allowed="allowed" message="无权限访问反馈与报表页面">
      <!-- 页头：标题 + 根节点筛选（影响所有受 root_type 约束的 Tab） -->
      <div class="page-header">
        <div>
          <div class="page-title">反馈与准确率报表</div>
          <div class="page-desc">监控召回准确率、响应耗时与用户反馈趋势，驱动持续优化</div>
        </div>
        <div class="root-filter">
          <span class="text-sub">根节点：</span>
          <el-select v-model="rootType" placeholder="全部节点" clearable style="width: 200px" @change="onRootTypeChange">
            <el-option v-if="rootTypes.length === 0" label="暂无节点数据" value="" disabled />
            <el-option v-for="n in rootTypes" :key="n.id" :label="n.name" :value="n.root_type" />
          </el-select>
        </div>
      </div>

      <!-- 内容区：Tab 卡片撑满剩余高度 -->
      <div class="page-body">
      <!-- Tab 栏：lazy 懒加载，首次切到对应 Tab 才挂载面板；切回时由 tab-change 驱动刷新 -->
      <div class="app-card tabs-card tabs-fill">
        <el-tabs v-model="activeTab" lazy @tab-change="onTabChange">
          <!-- 📊 概览：今日实时（Redis 快照轮询）+ 指标趋势 + 导出报表 -->
          <el-tab-pane label="📊 概览" name="overview">
            <OverviewPanel ref="overviewRef" :root-type="rootType" />
          </el-tab-pane>
          <!-- 📅 历史指标：P50/P95/P99 / 缓存命中率 / Token / 延迟与错误分布 -->
          <el-tab-pane label="📅 历史指标" name="system">
            <SystemMetricsPanel ref="systemRef" />
          </el-tab-pane>
          <!-- 📦 队列深度：实时快照 + 历史趋势 -->
          <el-tab-pane label="📦 队列深度" name="queue">
            <QueuePanel ref="queueRef" />
          </el-tab-pane>
          <!-- 🏢 部门/团队：每日预计算使用统计 -->
          <el-tab-pane label="🏢 部门/团队" name="org">
            <OrgPanel ref="orgRef" />
          </el-tab-pane>
          <!-- 📝 QA 记录：筛选 + 分页 + 详情 -->
          <el-tab-pane label="📝 QA 记录" name="qa">
            <QaPanel ref="qaRef" />
          </el-tab-pane>
          <!-- 📅 日报详情：多日趋势 + 今日 vs 昨日对比 -->
          <el-tab-pane label="📅 日报详情" name="daily">
            <DailyPanel ref="dailyRef" :root-type="rootType" />
          </el-tab-pane>
          <!-- 🔧 运营工具：关键词权重 / 差评反馈 / 反馈闭环 -->
          <el-tab-pane label="🔧 运营工具" name="tools">
            <ToolsPanel ref="toolsRef" :root-type="rootType" :root-types="rootTypes" />
          </el-tab-pane>
        </el-tabs>
      </div>
      </div>
    </PageGuard>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api/http'
import { useUserStore } from '../stores/user'
import PageGuard from '../components/base/PageGuard.vue'
import { errMsg } from '../utils/format'
import OverviewPanel from '../components/analytics/OverviewPanel.vue'
import SystemMetricsPanel from '../components/analytics/SystemMetricsPanel.vue'
import QueuePanel from '../components/analytics/QueuePanel.vue'
import OrgPanel from '../components/analytics/OrgPanel.vue'
import QaPanel from '../components/analytics/QaPanel.vue'
import DailyPanel from '../components/analytics/DailyPanel.vue'
import ToolsPanel from '../components/analytics/ToolsPanel.vue'

/**
 * 反馈与准确率报表页（原 admin-analytics.html 迁移）
 * 7 个 Tab：概览/历史指标/队列深度/部门团队/QA 记录/日报详情/运营工具
 * - 概览 Tab 常驻实时轮询（5 分钟），切走暂停、切回恢复
 * - 其余面板 lazy 懒加载 + 每次切换刷新（原 switchTab → loadTabData 语义）
 * - 根节点筛选变化时按当前 Tab 懒加载对应数据（reloadCurrentTab 语义）
 */
const userStore = useUserStore()
const allowed = computed(() => userStore.isManagerRole)

const activeTab = ref('overview')
const rootType = ref('') // 当前根节点筛选值（root_type 字符串，与原 data-root-type 口径一致）
const rootTypes = ref([]) // 根节点列表 [{ id, root_type, name }]

// 各面板组件引用：lazy 模式下未挂载时 ref 为 undefined，调用前需判空
const overviewRef = ref(null)
const systemRef = ref(null)
const queueRef = ref(null)
const orgRef = ref(null)
const qaRef = ref(null)
const dailyRef = ref(null)
const toolsRef = ref(null)

/** 动态加载根节点树：只取根节点作为领域筛选项（子节点随选中根节点联动，无需前端展开树） */
async function loadRootTypes() {
  try {
    const data = await api.getJson('/api/v1/knowledge/nodes/tree/')
    const tree = data.tree || []
    rootTypes.value = tree
      .filter(n => n.node_type === 'root')
      .map(n => ({ id: n.id, root_type: n.root_type, name: n.name }))
  } catch (e) {
    ElMessage.error('加载节点树失败: ' + errMsg(e, '未知错误'))
    rootTypes.value = []
  }
}

/** Tab 切换：概览恢复实时轮询并立即刷新；其他 Tab 停止轮询 + 懒加载对应面板 */
function onTabChange(name) {
  if (name === 'overview') {
    overviewRef.value?.activate()
  } else {
    overviewRef.value?.deactivate()
    reloadPanel(name)
  }
}

/** 面板懒加载分发：切到对应 Tab 时刷新该面板数据（每次切换均刷新，同原 loadTabData） */
function reloadPanel(name) {
  const map = { system: systemRef, queue: queueRef, org: orgRef, qa: qaRef, daily: dailyRef, tools: toolsRef }
  map[name]?.value?.reload()
}

/** 根节点切换：按当前 Tab 懒加载对应数据（qa 分页重置回第 1 页由面板自身处理） */
function onRootTypeChange() {
  reloadCurrentTab()
}

function reloadCurrentTab() {
  // 仅受 root_type 约束的面板需要重载（趋势/日报/运营工具），其余面板不受根节点影响
  if (activeTab.value === 'overview') overviewRef.value?.reload()
  else if (activeTab.value === 'daily') dailyRef.value?.reload()
  else if (activeTab.value === 'tools') toolsRef.value?.reload()
}

onMounted(() => {
  loadRootTypes()
})

// 页面卸载时停止概览实时轮询，避免定时器泄漏
onBeforeUnmount(() => {
  overviewRef.value?.deactivate()
})
</script>

<style scoped>
.admin-analytics-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.root-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 2px;
}

/* Tab 卡片：撑满剩余高度，面板内部各自滚动（el-tabs 三件套由全局 .tabs-fill 提供）
   app-card 全局默认 margin-bottom: 16px，此处页面 body 已带底部内边距，再叠加会造成底部多余空隙 */
.tabs-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 4px 16px 16px;
  margin-bottom: 0;
}
</style>
