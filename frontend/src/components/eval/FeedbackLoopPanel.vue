<template>
  <div class="fb-panel-page">
    <!-- 工具栏：执行分析 -->
    <div class="eval-toolbar mb-3">
      <div class="flex gap-2 items-center">
        <el-button type="primary" :loading="running" @click="runFeedbackLoop">🔍 执行反馈闭环分析</el-button>
        <span class="text-sm text-sub">将差评自动关联到对应文档 chunk，给出处理建议</span>
      </div>
    </div>

    <!-- KPI 卡片 -->
    <div class="kpi-grid mb-3">
      <div class="kpi-card"><div class="kpi-label">差评总数</div><div class="kpi-value">{{ fbTotal }}</div></div>
      <div class="kpi-card"><div class="kpi-label">已关联闭环</div><div class="kpi-value">{{ fbLinked }}</div></div>
      <div class="kpi-card"><div class="kpi-label">关联率</div><div class="kpi-value" :class="kpiClass(fbLinkRate)">{{ fmtPct(fbLinkRate) }}</div></div>
    </div>

    <!-- 待处理的反馈闭环 -->
    <div class="eval-panel eval-panel-scroll">
      <PanelHeader titleClass="eval-panel-title">待处理的反馈闭环</PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="issues" v-loading="running" size="small">
          <el-table-column label="反馈ID" width="80" prop="feedback_id" />
          <el-table-column label="问题" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.question || '' }}</template>
          </el-table-column>
          <el-table-column label="评分" width="80">
            <template #default="{ row }"><el-tag type="danger" size="small">{{ String(row.rating) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="标签" min-width="110">
            <template #default="{ row }">
              <template v-if="(row.tags || []).length">
                <el-tag v-for="(t, i) in row.tags" :key="i" size="small" effect="plain" style="margin-right: 4px">{{ t }}</el-tag>
              </template>
              <span v-else class="text-sub">-</span>
            </template>
          </el-table-column>
          <el-table-column label="关联Chunk" min-width="100">
            <template #default="{ row }">
              <template v-if="(row.chunk_ids || []).length">
                <el-tag v-for="cid in row.chunk_ids" :key="cid" size="small" style="margin-right: 4px">#{{ cid }}</el-tag>
              </template>
              <span v-else class="text-sub">-</span>
            </template>
          </el-table-column>
          <el-table-column label="建议处理" min-width="200">
            <template #default="{ row }">
              <div class="fb-suggestion"><strong>💡 建议:</strong> {{ row.suggestion || '' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100">
            <template #default="{ row }">
              <el-button link type="danger" size="small" @click="markFeedbackResolved(row.feedback_id)">标记处理</el-button>
            </template>
          </el-table-column>
          <template #empty><el-empty description="点击上方按钮执行分析" :image-size="70" /></template>
        </el-table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { errMsg } from '../../utils/format'
import PanelHeader from '../base/PanelHeader.vue'
import { fmtPct, kpiClass } from './constants'

/**
 * 反馈闭环 Tab（原 feedback 面板）：执行反馈闭环分析,
 * 将差评自动关联到对应文档 chunk,给出处理建议;人工标记处理后闭环关闭
 */
const running = ref(false)
const fbTotal = ref(0)
const fbLinked = ref(0)
const fbLinkRate = ref(null)
const issues = ref([])

async function runFeedbackLoop() {
  ElMessage.info('正在分析反馈闭环...')
  running.value = true
  try {
    const data = await api.postJson('/api/v1/analytics/feedback-loop/', { days: 7 })
    const total = data.total_bad_feedbacks || 0
    const linked = data.linked_count || 0
    const rate = total > 0 ? (linked / total * 100).toFixed(1) : '0.0'
    fbTotal.value = total
    fbLinked.value = linked
    fbLinkRate.value = parseFloat(rate) / 100
    issues.value = data.issue_chunks || []
  } catch (e) {
    ElMessage.error('分析失败: ' + errMsg(e, '未知错误'))
  } finally {
    running.value = false
  }
}

async function markFeedbackResolved(id) {
  try {
    await api.put(`/api/v1/analytics/bad-feedbacks/${id}/`, { status: 'resolved' })
    ElMessage.success('已标记为已处理')
    runFeedbackLoop()
  } catch (e) {
    ElMessage.error('操作失败: ' + errMsg(e, '未知错误'))
  }
}

onMounted(runFeedbackLoop)

defineExpose({ reload: runFeedbackLoop })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,固定区(工具栏/KPI)在上,
   待处理的反馈闭环列表占满剩余空间并在内部滚动 */
.fb-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.fb-panel-page .eval-toolbar,
.fb-panel-page .kpi-grid,
.fb-panel-page .eval-panel:not(.eval-panel-scroll) {
  flex-shrink: 0;
}

/* 显式标记滚动目标：el-dialog 会在面板内渲染 el-overlay 兄弟节点,
   不能依赖 :last-of-type 判定 */
.fb-panel-page .eval-panel-scroll {
  flex: 1;
  min-height: 0;
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-3 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 滚动下移到表格内部：标题固定,表格占满剩余空间,行在表内滚动 */
.fb-panel-page .eval-panel-scroll .eval-panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.fb-panel-page .eval-panel-scroll .eval-panel-body .el-table {
  flex: 1;
  min-height: 0;
}

.text-sub {
  color: var(--el-text-color-secondary, #6b7280);
}
</style>
