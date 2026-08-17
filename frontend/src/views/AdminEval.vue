<template>
  <div class="page-container admin-eval-page">
    <!-- 权限守卫：质量评估仅管理角色（超管/文档管理员/用户管理员/部门经理/团队组长）可见 -->
    <PageGuard :allowed="allowed" message="无权限访问质量评估页面">
      <!-- 页头 -->
      <div class="page-header">
        <div class="eval-page-head">
          <div class="page-title">RAG 质量评估中心</div>
          <div class="page-desc">全链路质量监控 · 数据入库→检索→回答→反馈闭环</div>
        </div>
      </div>

      <!-- 内容区：Tab 卡片撑满剩余高度 -->
      <div class="page-body">
      <!-- Tab 栏：lazy 懒加载，首次切到对应 Tab 才挂载面板；切回时由 tab-change 驱动刷新 -->
      <div class="app-card tabs-card tabs-fill">
        <el-tabs v-model="activeTab" lazy @tab-change="onTabChange">
          <!-- 💬 回答质量：12 维画像 + 低分 Top N + 手动评估（生产监控最高频，放首位） -->
          <el-tab-pane label="💬 回答质量" name="answer">
            <AnswerPanel ref="answerRef" />
          </el-tab-pane>
          <!-- 🧪 低分归因：低分对话自动归因 + 优化建议 -->
          <el-tab-pane label="🧪 低分归因" name="attribution">
            <AttributionPanel ref="attributionRef" />
          </el-tab-pane>
          <!-- 🗺️ 路由分析：四层路由命中率与质量对比 -->
          <el-tab-pane label="🗺️ 路由分析" name="route">
            <RoutePanel ref="routeRef" />
          </el-tab-pane>
          <!-- 📄 文档质量：文档维度质量分布与常见问题 -->
          <el-tab-pane label="📄 文档质量" name="doc">
            <DocQualityPanel ref="docRef" />
          </el-tab-pane>
          <!-- 📖 Wiki 质量：忠实度/完整性评估 -->
          <el-tab-pane lazy label="📖 Wiki 质量" name="wiki">
            <WikiQualityPanel ref="wikiRef" />
          </el-tab-pane>
          <!-- 📊 覆盖率：部门/团队/知识空白覆盖 -->
          <el-tab-pane label="📊 覆盖率" name="coverage">
            <CoveragePanel ref="coverageRef" />
          </el-tab-pane>
          <!-- 🔄 反馈闭环：差评反馈分析与处理 -->
          <el-tab-pane lazy label="🔄 反馈闭环" name="feedback">
            <FeedbackLoopPanel ref="feedbackRef" />
          </el-tab-pane>
          <!-- 📋 测试集管理：回归测试集 + 部署前离线评估 -->
          <el-tab-pane lazy label="📋 测试集管理" name="golden">
            <GoldenPanel ref="goldenRef" />
          </el-tab-pane>
          <!-- 🔍 检索评估：离线检索命中/增益分析 -->
          <el-tab-pane label="🔍 检索评估" name="retrieval">
            <RetrievalPanel ref="retrievalRef" />
          </el-tab-pane>
        </el-tabs>
      </div>
      </div>
    </PageGuard>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useUserStore } from '../stores/user'
import PageGuard from '../components/base/PageGuard.vue'
import AnswerPanel from '../components/eval/AnswerPanel.vue'
import AttributionPanel from '../components/eval/AttributionPanel.vue'
import RoutePanel from '../components/eval/RoutePanel.vue'
import DocQualityPanel from '../components/eval/DocQualityPanel.vue'
import WikiQualityPanel from '../components/eval/WikiQualityPanel.vue'
import CoveragePanel from '../components/eval/CoveragePanel.vue'
import FeedbackLoopPanel from '../components/eval/FeedbackLoopPanel.vue'
import GoldenPanel from '../components/eval/GoldenPanel.vue'
import RetrievalPanel from '../components/eval/RetrievalPanel.vue'
// 公共样式：kpi 卡片/面板/趋势条等（各面板共用，全局引入一次）
import '../components/eval/eval-common.css'

/**
 * RAG 质量评估中心（原 admin-eval.html 迁移）
 * 9 个 Tab：回答质量/低分归因/路由分析/文档质量/Wiki 质量/覆盖率/反馈闭环/测试集管理/检索评估
 * - 顺序按「生产监控优先 → 治理巡检 → 运营闭环 → 离线评估殿后」排列,与原页面一致
 * - 面板 lazy 懒加载 + 每次切换刷新（原 switchTab → loadTabData 语义）
 * - 权限：仅管理角色可见（与旧 admin-eval 入口一致）
 */
const userStore = useUserStore()
const allowed = computed(() => userStore.isManagerRole)

// 默认落在「回答质量」(生产监控最高频),离线评估/治理类 Tab 按需切换
const activeTab = ref('answer')

// 各面板组件引用：lazy 模式下未挂载时 ref 为 undefined,调用前需判空
const answerRef = ref(null)
const attributionRef = ref(null)
const routeRef = ref(null)
const docRef = ref(null)
const wikiRef = ref(null)
const coverageRef = ref(null)
const feedbackRef = ref(null)
const goldenRef = ref(null)
const retrievalRef = ref(null)

/** Tab 切换：懒加载对应面板并刷新数据（每次切换均刷新,同原 loadTabData） */
function onTabChange(name) {
  const map = {
    answer: answerRef,
    attribution: attributionRef,
    route: routeRef,
    doc: docRef,
    wiki: wikiRef,
    coverage: coverageRef,
    feedback: feedbackRef,
    golden: goldenRef,
    retrieval: retrievalRef,
  }
  map[name]?.value?.reload()
}
</script>

<style scoped>
.admin-eval-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

/* 页头 */
.eval-page-head {
  flex-shrink: 0;
}

/* Tab 卡片：撑满剩余高度,面板内部各自滚动（el-tabs 三件套由全局 .tabs-fill 提供）；
   覆盖全局 .app-card 的底部 margin/padding,避免卡片下方出现多余留白 */
.tabs-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 4px 16px 0;
  margin-bottom: 0;
}
</style>
