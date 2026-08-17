<template>
  <div class="app-card qa-card">
    <!-- 工具栏：标题 + 筛选条件同一行（下拉框在常规宽度上再缩小约 10%，并去掉冗余的日期标签，
         保证常见笔记本宽度下一行放得下；极端窄屏仍由 flex-wrap 兜底换行） -->
    <div class="qa-toolbar">
      <div class="qa-title">📝 QA 记录明细</div>
      <el-select v-model="filters.answerType" placeholder="全部类型" clearable size="small" style="width: 95px" @change="onFilterChange">
        <el-option label="RAG" value="rag" />
        <el-option label="推理" value="reasoning" />
        <el-option label="混合" value="mixed" />
        <el-option label="Agent" value="agent" />
        <el-option label="拒答" value="refused" />
        <el-option label="通用" value="general" />
      </el-select>
      <el-select v-model="filters.cache" placeholder="全部缓存" clearable size="small" style="width: 95px" @change="onFilterChange">
        <el-option label="命中缓存" value="1" />
        <el-option label="未命中" value="0" />
      </el-select>
      <el-select v-model="filters.rating" placeholder="全部评分" clearable size="small" style="width: 110px" @change="onFilterChange">
        <el-option label="好评" value="1" />
        <el-option label="中性/未评" value="0" />
        <el-option label="差评" value="-1" />
      </el-select>
      <el-select v-model="filters.latency" placeholder="全部延迟" clearable size="small" style="width: 95px" @change="onFilterChange">
        <el-option label="&lt; 1s" value="0-1000" />
        <el-option label="1~3s" value="1000-3000" />
        <el-option label="3~5s" value="3000-5000" />
        <el-option label="&gt; 5s" value="5000-" />
      </el-select>
      <!-- 日期选择：占位符已含"开始/结束"语义，不再重复加标签，节省宽度保证整行放得下 -->
      <el-date-picker v-model="filters.startDate" type="date" placeholder="开始日期" value-format="YYYY-MM-DD"
        size="small" style="width: 105px" :clearable="true" @change="onFilterChange" />
      <el-date-picker v-model="filters.endDate" type="date" placeholder="结束日期" value-format="YYYY-MM-DD"
        size="small" style="width: 105px" :clearable="true" @change="onFilterChange" />
      <el-input v-model="filters.q" placeholder="搜索问题" clearable size="small" style="width: 115px"
        @input="onSearchInput" @keyup.enter="onFilterChange" />
      <el-button size="small" @click="resetFilters">重置</el-button>
    </div>

    <!-- QA 记录表：外层 div 占满工具栏与分页器之间的剩余高度；
         el-table 通过 height 属性固定容器高度，表头不滚动，仅表格内部纵向滚动 -->
    <div class="qa-table-wrap" ref="tableWrapEl">
      <el-table :data="rows" v-loading="loading" size="small" class="qa-table" :height="tableHeight" @row-click="showQaDetail">
        <el-table-column label="ID" width="70" prop="id" />
        <el-table-column label="问题" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.question }}</template>
        </el-table-column>
        <el-table-column label="回答类型" width="90">
          <template #default="{ row }">
            <el-tag :type="typeBadge(row.answer_type).type" size="small" effect="plain">{{ typeBadge(row.answer_type).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="缓存" width="70">
          <template #default="{ row }">
            <el-tag v-if="row.is_hit_cache" type="warning" size="small" effect="plain">是</el-tag>
            <span v-else class="text-sub">-</span>
          </template>
        </el-table-column>
        <el-table-column label="评分" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.rating === 1" type="success" size="small" effect="plain">👍 好评</el-tag>
            <el-tag v-else-if="row.rating === -1" type="danger" size="small" effect="plain">👎 差评</el-tag>
            <el-tag v-else size="small" effect="plain">-</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="总延迟" width="90" align="right">
          <template #default="{ row }">{{ (row.latency_total_ms || 0).toLocaleString() }} ms</template>
        </el-table-column>
        <el-table-column label="Token 总数" width="100" align="right">
          <template #default="{ row }">{{ (row.tokens_prompt || 0) + (row.tokens_completion || 0) }}</template>
        </el-table-column>
        <el-table-column label="预估费用" width="100" align="right">
          <template #default="{ row }">¥ {{ (row.cost_estimate || 0).toFixed(4) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="150">
          <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
        </el-table-column>
        <template #empty><el-empty description="暂无 QA 记录" :image-size="60" /></template>
      </el-table>
    </div>

    <!-- 分页：服务端分页，page/page_size 由后端控制（固定每页条数，不提供切换） -->
    <AppPagination
      class="qa-pagination"
      :total="total"
      :page-size="pageSize"
      :page="page"
      @page-change="onPageChange"
    />

    <!-- QA 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="QA 详情" width="720px" top="6vh" :close-on-click-modal="false">
      <div v-if="detailLoading" class="text-loading">加载中...</div>
      <div v-else-if="detailError" class="detail-error">{{ detailError }}</div>
      <template v-else-if="detail">
        <div class="qa-block">
          <div class="block-label">问题</div>
          <div class="qa-detail-question">{{ detail.question }}</div>
        </div>
        <div class="qa-block">
          <div class="block-label">回答</div>
          <div class="qa-detail-answer">{{ detail.answer }}</div>
        </div>
        <div class="detail-grid">
          <div>
            <div class="block-label">回答类型</div>
            <div>{{ detail.answer_type || '-' }}</div>
          </div>
          <div>
            <div class="block-label">领域</div>
            <div>{{ detail.root_type || '-' }}</div>
          </div>
          <div>
            <div class="block-label">总延迟</div>
            <div>{{ (detail.latency_total_ms || 0).toLocaleString() }} ms</div>
          </div>
          <div>
            <div class="block-label">缓存命中</div>
            <div>{{ detail.is_hit_cache ? '是' : '否' }}</div>
          </div>
          <div>
            <div class="block-label">Prompt Token</div>
            <div>{{ (detail.tokens_prompt || 0).toLocaleString() }}</div>
          </div>
          <div>
            <div class="block-label">Completion Token</div>
            <div>{{ (detail.tokens_completion || 0).toLocaleString() }}</div>
          </div>
          <div>
            <div class="block-label">预估费用</div>
            <div>¥ {{ (detail.cost_estimate || 0).toFixed(4) }}</div>
          </div>
          <div>
            <div class="block-label">时间</div>
            <div>{{ formatDate(detail.created_at) }}</div>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button type="primary" @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import { debounce } from '../../utils/debounce'
import { useListLoader } from '../../composables/useListLoader'
import { usePagination } from '../../composables/usePagination'
import AppPagination from '../../components/base/AppPagination.vue'

/**
 * QA 记录 Tab：多条件筛选（类型/缓存/评分/延迟区间/日期/搜索）+ 服务端分页 + 详情弹窗
 * 搜索输入 300ms 防抖；筛选/翻页均带请求序号守卫，防止快速操作时旧响应覆盖新状态
 * 布局：工具栏固定 → 表格区占满剩余高度（表头固定、内部纵向滚动，高度由 ResizeObserver 实测）→ 分页器固定
 */
const filters = reactive({ answerType: '', cache: '', rating: '', latency: '', startDate: '', endDate: '', q: '' })
const rows = ref([])
const total = ref(0)

/* 表格区高度：外层包裹 div 撑满剩余空间，实测其高度传给 el-table 的 height 属性，
   使表头固定、表格内部纵向滚动；分页器显隐/窗口变化时由 ResizeObserver 自动重算 */
const tableWrapEl = ref(null)
const tableHeight = ref(300)
let wrapObserver = null

const { loading, load: loadQaRecords } = useListLoader(async () => {
  const params = []
  const q = (filters.q || '').trim()
  if (q) params.push('q=' + encodeURIComponent(q))
  if (filters.startDate) params.push('start_date=' + encodeURIComponent(filters.startDate))
  if (filters.endDate) params.push('end_date=' + encodeURIComponent(filters.endDate))
  if (filters.answerType) params.push('answer_type=' + encodeURIComponent(filters.answerType))
  if (filters.cache) params.push('cache=' + encodeURIComponent(filters.cache))
  if (filters.rating) params.push('rating=' + encodeURIComponent(filters.rating))
  if (filters.latency) {
    // 延迟区间格式为 min-max，max 为空表示无上限
    const [latMin, latMax] = filters.latency.split('-')
    if (latMin !== '') params.push('latency_min=' + encodeURIComponent(latMin))
    if (latMax !== '') params.push('latency_max=' + encodeURIComponent(latMax))
  }
  params.push('page=' + page.value)
  params.push('page_size=' + pageSize.value)
  const data = await api.getJson('/api/v1/analytics/qa-records/?' + params.join('&'))
  total.value = data.total || 0
  rows.value = data.rows || []
  // 后端响应只有 total/page/page_size，未返回 total_pages，需前端按总数与每页条数换算；
  // 否则 total_pages 兜底为 1，翻到第 2 页会被误判为"越界"而回退第 1 页
  if (guardOverflow(total.value)) return
}, { errorPrefix: '加载 QA 记录失败' })

// 分页状态：由 usePagination 统一管理翻页/越界回退/筛选重置（loadQaRecords 内部读取 page/pageSize）
const { page, pageSize, onPageChange, reset, guardOverflow } = usePagination(loadQaRecords)

/** 筛选条件变化：重置回第 1 页并重新加载 */
function onFilterChange() {
  reset()
}

/** 问题搜索输入：300ms 防抖后触发筛选（定时器由 utils/debounce 统一管理） */
const onSearchInput = debounce(onFilterChange, 300)

function resetFilters() {
  filters.answerType = ''
  filters.cache = ''
  filters.rating = ''
  filters.latency = ''
  filters.startDate = ''
  filters.endDate = ''
  filters.q = ''
  onFilterChange()
}

/* ===== 展示辅助 ===== */
// 与 QaRecord 实际写入值对齐（rag/reasoning/mixed/refused/agent/general），未知类型回退为默认样式
const TYPE_MAP = {
  rag: { type: 'info', text: 'RAG' },
  reasoning: { type: 'primary', text: '推理' },
  mixed: { type: 'warning', text: '混合' },
  refused: { type: 'danger', text: '拒答' },
  agent: { type: 'success', text: 'Agent' },
  general: { type: 'info', text: '通用' },
}
function typeBadge(t) {
  return TYPE_MAP[t] || { type: '', text: t || '-' }
}

/* ===== QA 详情弹窗 ===== */
const detailVisible = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detail = ref(null)

async function showQaDetail(row) {
  if (!row || !row.id) return
  detailVisible.value = true
  detailLoading.value = true
  detailError.value = ''
  detail.value = null
  try {
    // 调用后端新增的 qa_id 参数接口，直接查询单条（避免 page_size=100 的前 100 条限制）
    const d = await api.getJson(`/api/v1/analytics/qa-records/?qa_id=${encodeURIComponent(row.id)}`)
    const r = d.row
    if (!r) {
      detailError.value = '未找到该 QA 记录'
      return
    }
    detail.value = r
  } catch (e) {
    detailError.value = '加载失败：' + errMsg(e, '未知错误')
  } finally {
    detailLoading.value = false
  }
}

onMounted(() => {
  wrapObserver = new ResizeObserver(() => {
    if (tableWrapEl.value) tableHeight.value = Math.max(0, tableWrapEl.value.clientHeight)
  })
  wrapObserver.observe(tableWrapEl.value)
  loadQaRecords()
})

onBeforeUnmount(() => {
  if (wrapObserver) { wrapObserver.disconnect(); wrapObserver = null }
  onSearchInput.cancel()
})

defineExpose({ reload: loadQaRecords })
</script>

<style scoped>
/* 卡片：flex 列布局撑满 Tab 面板高度（app-card 全局 margin-bottom 在此多余，置 0 避免底部空隙） */
.qa-card {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-bottom: 0;
}

/* 工具栏：标题 + 筛选条件同一行，固定不随表格滚动（间距收紧保证一行放得下） */
.qa-toolbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 14px;
}

.qa-title {
  font-size: 15px;
  font-weight: 600;
  margin-right: 4px;
}

/* 表格区：占满工具栏与分页器之间的剩余高度，表头固定、内部纵向滚动 */
.qa-table-wrap {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.qa-table {
  width: 100%;
  cursor: pointer;
}

/* 分页器：固定在底部，不随表格内容滚动 */
.qa-pagination {
  flex-shrink: 0;
  margin-top: 14px;
  justify-content: flex-end;
}

/* 详情弹窗 */
.text-loading {
  padding: 20px;
  color: var(--app-text-sub);
  text-align: center;
}

.detail-error {
  padding: 20px;
  color: #f56c6c;
  text-align: center;
}

.qa-block {
  margin-bottom: 14px;
}

.block-label {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 4px;
}

.qa-detail-question,
.qa-detail-answer {
  border-radius: 6px;
  padding: 12px 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.qa-detail-question {
  background: var(--app-menu-hover);
}

.qa-detail-answer {
  background: var(--el-color-primary-light-9, #eff6ff);
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px 20px;
  margin-top: 8px;
}
</style>
