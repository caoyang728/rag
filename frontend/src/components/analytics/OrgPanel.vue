<template>
  <div class="org-panel">
    <!-- 面板头部：标题 + 日期/层级选择 -->
    <div class="panel-header">
      <div class="panel-title">🏢 部门/团队使用统计（每日预计算）</div>
      <div class="panel-filters">
        <span class="text-sub">日期：</span>
        <el-date-picker v-model="reportDate" type="date" value-format="YYYY-MM-DD" :clearable="false"
          :disabled-date="d => d > maxDate" style="width: 160px" @change="loadOrgUsage" />
        <span class="text-sub">层级：</span>
        <el-select v-model="orgLevel" style="width: 140px" @change="loadOrgUsage">
          <el-option label="按团队" value="team" />
          <el-option label="按部门汇总" value="dept" />
        </el-select>
      </div>
    </div>

    <!-- 空态：该日期该层级无报表 -->
    <div v-if="emptyMessage" class="app-card card-empty">
      <div class="empty-emoji">🧾</div>
      <div class="empty-title">{{ emptyMessage }}</div>
      <div class="text-sub">报表日期：{{ reportDate || '-' }}（请等待凌晨聚合任务完成或切换到其他日期）</div>
    </div>

    <!-- 使用统计表 -->
    <div v-else class="app-card">
      <div class="table-title">🏢 {{ orgLevel === 'dept' ? '部门' : '团队' }}级使用统计 · {{ reportDate || '-' }}</div>
      <el-table :data="rows" v-loading="loading" size="small" class="org-table">
        <el-table-column label="部门" min-width="120" prop="department_name">
          <template #default="{ row }">{{ row.department_name || '-' }}</template>
        </el-table-column>
        <el-table-column v-if="orgLevel === 'team'" label="团队" min-width="120">
          <template #default="{ row }">{{ row.team_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="QA 次数" min-width="90" align="right">
          <template #default="{ row }">{{ (row.qa_count || 0).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="活跃用户" min-width="90" align="right">
          <template #default="{ row }">{{ (row.user_count || 0).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="总 Token" min-width="100" align="right">
          <template #default="{ row }">{{ (row.total_tokens || 0).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="预估费用（¥）" min-width="110" align="right">
          <template #default="{ row }">¥ {{ (row.total_cost || 0).toFixed(4) }}</template>
        </el-table-column>
        <el-table-column label="平均延迟 (ms)" min-width="110" align="right">
          <template #default="{ row }">{{ (row.avg_latency_ms || 0).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="P95 延迟 (ms)" min-width="110" align="right">
          <template #default="{ row }">{{ (row.p95_latency_ms || 0).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="好评率 (%)" min-width="100" align="right">
          <template #default="{ row }">{{ fmtPctLocal(row.good_feedback_rate) }}</template>
        </el-table-column>
        <el-table-column label="缓存命中数" min-width="100" align="right">
          <template #default="{ row }">{{ (row.cache_hit_count || 0).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="缓存命中率 (%)" min-width="110" align="right">
          <template #default="{ row }">{{ fmtPctLocal(row.cache_hit_rate) }}</template>
        </el-table-column>
        <template #empty><el-empty description="暂无该日期的组织使用报表" :image-size="50" /></template>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { errMsg, fmtPct } from '../../utils/format'
import { useListLoader } from '../../composables/useListLoader'

/**
 * 部门/团队 Tab：按团队或按部门汇总的使用统计（QA 次数/活跃用户/Token/费用/延迟/好评率/缓存）
 * 部门汇总通过 team_id=-1 哨兵值请求；数据每日凌晨预计算
 */
const reportDate = ref('')
const maxDate = new Date(Date.now() - 86400000)
const orgLevel = ref('team')
const rows = ref([])
const emptyMessage = ref('')

const { loading, load: loadOrgUsage } = useListLoader(async () => {
  let url = '/api/v1/analytics/org-usage/'
  const params = []
  if (reportDate.value) params.push('date=' + encodeURIComponent(reportDate.value))
  // 部门汇总哨兵值：后端按 team_id=-1 返回部门级聚合
  if (orgLevel.value === 'dept') params.push('team_id=-1')
  const finalUrl = params.length ? url + '?' + params.join('&') : url
  const data = await api.getJson(finalUrl)
  rows.value = data.rows || []
  emptyMessage.value = rows.value.length === 0 ? '暂无该日期的组织使用报表' : ''
}, {
  // 失败时清空列表并提示；onError 存在时不会走 useListLoader 的默认提示
  onError: (e, { silent }) => {
    if (silent) return
    rows.value = []
    emptyMessage.value = '加载组织统计失败'
    ElMessage.error('加载组织统计失败: ' + errMsg(e, '未知错误'))
  },
})

// 组织统计字段为 0~1 比例：复用 utils/format 的 fmtPct（2 位小数，缺失显示 '-'）
function fmtPctLocal(v) {
  return fmtPct(v, 2, '-')
}

onMounted(() => {
  // 默认日期预填昨日（系统报表通常已就绪）
  reportDate.value = new Date(Date.now() - 86400000).toISOString().slice(0, 10)
  loadOrgUsage()
})

defineExpose({ reload: loadOrgUsage })
</script>

<style scoped>
.org-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* 面板头部：固定 + 内部留白 */
.panel-header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 14px;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  margin-bottom: 12px;
}

.panel-title {
  font-size: 16px;
  font-weight: 600;
}

.panel-filters {
  display: flex;
  align-items: center;
  gap: 8px;
}

.table-title {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
}

.org-table {
  width: 100%;
}

/* 空态卡片：撑满剩余高度，app-card 全局 margin-bottom 在此多余，置 0 避免底部空隙 */
.card-empty {
  flex: 1;
  min-height: 0;
  padding: 40px;
  text-align: center;
  margin-bottom: 0;
}

.empty-emoji {
  font-size: 48px;
  margin-bottom: 16px;
}

.empty-title {
  font-size: 16px;
  font-weight: 500;
  margin-bottom: 8px;
}
</style>
