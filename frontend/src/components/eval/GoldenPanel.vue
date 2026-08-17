<template>
  <div class="golden-panel-page">
    <!-- 工具栏：操作按钮 + 摘要 -->
    <div class="eval-toolbar mb-3">
      <div class="golden-toolbar">
        <div class="flex gap-2 items-center">
          <el-button type="primary" @click="openCreateDialog">➕ 创建测试集</el-button>
          <el-button @click="openImportDialog">📥 批量导入</el-button>
          <el-button @click="confirmSiphonRegression">🔬 沉淀低分</el-button>
          <el-button @click="confirmRunRegressionEval">🧪 评估回归</el-button>
        </div>
        <span class="text-sub text-sm">{{ summaryText }}</span>
      </div>
    </div>

    <!-- 低分回归说明条(仅提示,不影响操作) -->
    <div class="regression-tip mb-3">
      <span class="regression-tip-icon">💡</span>
      <span class="regression-tip-text">
        <strong>低分回归测试集</strong>:从生产低分对话自动沉淀,防止已知 bad case 退化。
        连续通过 <b>{{ suggestRemovePasses }}</b> 次后建议人工 review 移除;
        评估失败则通过次数重置为 0。
      </span>
    </div>

    <!-- 测试集列表 -->
    <div class="eval-panel eval-panel-scroll">
      <PanelHeader titleClass="eval-panel-title">测试集列表</PanelHeader>
      <div class="eval-panel-body">
        <el-table :data="datasets" v-loading="loading" size="small">
          <el-table-column label="ID" width="60" prop="id" />
          <el-table-column label="名称" min-width="160" prop="name" show-overflow-tooltip />
          <el-table-column label="分类" width="130">
            <template #default="{ row }">
              <el-tag v-if="row.dataset_type === 'regression_low_score'" size="small" class="tag-regression">
                {{ row.dataset_type_label || row.dataset_type || '自定义' }}
              </el-tag>
              <el-tag v-else size="small" effect="plain">{{ row.dataset_type_label || row.dataset_type || '自定义' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="领域" width="110">
            <template #default="{ row }"><el-tag size="small" effect="plain">{{ rootTypeLabel(row.root_type) }}</el-tag></template>
          </el-table-column>
          <el-table-column label="问题数" width="80" prop="question_count" align="right" />
          <el-table-column label="版本" width="90" prop="version" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)" size="small" effect="plain">{{ statusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="150">
            <template #default="{ row }"><span class="text-sub">{{ formatDate(row.updated_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="120">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="viewDataset(row.id)">查看</el-button>
              <el-button link type="danger" size="small" @click="confirmDeleteDataset(row.id)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无测试集，点击上方「创建测试集」或「沉淀低分」开始" :image-size="70" />
          </template>
        </el-table>
      </div>
    </div>

    <!-- Dialog: 创建测试集 -->
    <el-dialog v-model="createVisible" title="创建测试集" width="480px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="测试集名称" required>
          <el-input v-model="createForm.name" placeholder="如: HR领域2026Q3" />
        </el-form-item>
        <el-form-item label="领域">
          <el-select v-model="createForm.rootType" style="width: 100%">
            <el-option label="全部领域" value="all" />
            <el-option v-if="!rootTypes.length" label="加载中..." value="all" disabled />
            <el-option v-for="t in rootTypes" :key="t.code" :label="t.name" :value="t.code" />
          </el-select>
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="createForm.version" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="3" placeholder="测试集用途说明" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="createDataset">创建</el-button>
      </template>
    </el-dialog>

    <!-- Dialog: 批量导入问题 -->
    <el-dialog v-model="importVisible" title="批量导入测试问题" width="640px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="选择测试集" required>
          <el-select v-model="importDsId" style="width: 100%">
            <el-option v-for="d in datasets" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="JSON 数据" required>
          <el-input v-model="importJsonText" type="textarea" :rows="10"
            placeholder='[{"question":"问题","relevant_doc_ids":[1,2],"reference_answer":"答案"}]' />
        </el-form-item>
        <div class="text-sub text-sm">格式说明：question(问题), relevant_doc_ids(相关文档ID数组), reference_answer(参考答案), key_points(关键点数组)</div>
      </el-form>
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="importQuestions">导入</el-button>
      </template>
    </el-dialog>

    <!-- Dialog: 测试集问题列表 -->
    <el-dialog v-model="detailVisible" :title="'测试集问题 · ' + detailName" width="640px" :close-on-click-modal="false">
      <div class="text-sub text-sm mb-2">{{ detailMeta }}</div>
      <div class="ds-list">
        <div v-if="!detailRows.length" class="eval-empty">
          <div class="eval-empty-icon">📋</div>
          <div>此测试集暂无问题</div>
        </div>
        <div v-for="(q, i) in detailRows" :key="i" class="ds-item">
          <span class="ds-idx">{{ i + 1 }}.</span>
          <span class="ds-question">{{ questionSummary(q.question) }}</span>
          <span class="text-sub text-sm">{{ questionExtra(q) }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../../api/http'
import { formatDate, errMsg } from '../../utils/format'
import PanelHeader from '../base/PanelHeader.vue'
import { useConfirm } from '../../composables/useConfirm'
import { rootTypeLabel, statusLabel, statusTagType } from './constants'

/**
 * 测试集管理 Tab（原 golden 面板）：创建/导入/删除测试集 + 低分回归沉淀与回归评估
 * - 低分回归测试集从生产低分对话自动沉淀,防止已知 bad case 退化
 * - suggest_remove_passes（建议移除阈值）从后端获取,避免前端硬编码不一致
 */
const { confirm } = useConfirm()
const datasets = ref([])
const loading = ref(false)
const suggestRemovePasses = ref(3)
// 领域列表（从后端动态获取,避免硬编码与实际节点树脱节）
const rootTypes = ref([])

const summaryText = computed(() => {
  const total = datasets.value.length
  const totalQuestions = datasets.value.reduce((s, d) => s + (d.question_count || 0), 0)
  return `共 ${total} 个测试集，${totalQuestions} 个问题`
})

async function loadDatasets() {
  loading.value = true
  try {
    const data = await api.getJson('/api/v1/analytics/golden-datasets/')
    datasets.value = data.rows || []
    // 同步低分回归说明条中的建议移除阈值
    if (data.suggest_remove_passes) suggestRemovePasses.value = data.suggest_remove_passes
  } catch (e) {
    ElMessage.error('加载失败: ' + errMsg(e, '未知错误'))
  } finally {
    loading.value = false
  }
}

/* ===== 创建测试集 ===== */
const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ name: '', rootType: 'all', version: 'v1', description: '' })

/** 加载领域列表:接口不可用时返回兜底值但不缓存,下次打开弹窗会重试 */
async function loadRootTypes() {
  try {
    const data = await api.getJson('/api/v1/knowledge/nodes/root_types/')
    rootTypes.value = data.root_types || []
  } catch (e) {
    rootTypes.value = [{ code: 'company_doc', name: 'company_doc' }]
  }
}

async function openCreateDialog() {
  createForm.value = { name: '', rootType: 'all', version: 'v1', description: '' }
  createVisible.value = true
  await loadRootTypes()
}

async function createDataset() {
  const name = (createForm.value.name || '').trim()
  if (!name) { ElMessage.error('请输入测试集名称'); return }
  creating.value = true
  try {
    await api.post('/api/v1/analytics/golden-datasets/', {
      name,
      root_type: createForm.value.rootType,
      version: createForm.value.version,
      description: createForm.value.description,
    })
    ElMessage.success('创建成功')
    createVisible.value = false
    loadDatasets()
  } catch (e) {
    ElMessage.error('创建失败: ' + errMsg(e, '未知错误'))
  } finally {
    creating.value = false
  }
}

/* ===== 删除测试集 ===== */
async function confirmDeleteDataset(id) {
  await confirm(
    { message: '关联的问题和标注也会被删除,此操作不可恢复', title: '删除测试集', confirmText: '确认删除', errorText: '删除失败' },
    async () => {
      await api.delete(`/api/v1/analytics/golden-datasets/${id}/`)
      ElMessage.success('删除成功')
      loadDatasets()
    },
  )
}

/* ===== 查看测试集问题 ===== */
const detailVisible = ref(false)
const detailName = ref('')
const detailMeta = ref('')
const detailRows = ref([])
const detailIsRegression = ref(false)
const detailSuggestPasses = ref(3)

async function viewDataset(id) {
  try {
    const data = await api.getJson(`/api/v1/analytics/golden-datasets/${id}/`)
    const rows = data.questions || []
    // 低分回归测试集展示 pass_count/last_eval_at,自定义测试集展示难度
    detailIsRegression.value = data.dataset_type === 'regression_low_score'
    detailSuggestPasses.value = data.suggest_remove_passes || 3
    if (!rows.length) {
      ElMessage.info('此测试集暂无问题，可点击"批量导入"或"沉淀低分"添加')
      return
    }
    detailName.value = data.name
    detailMeta.value = `${data.dataset_type_label || '自定义'} · 共 ${rows.length} 个问题`
    detailRows.value = rows
    detailVisible.value = true
  } catch (e) {
    ElMessage.error('加载失败: ' + errMsg(e, e))
  }
}

function questionSummary(question) {
  return (question || '').substring(0, 60)
}

// 问题行右侧附加信息:回归集展示通过次数+评估时间+建议移除标记,自定义集展示难度
function questionExtra(q) {
  if (detailIsRegression.value) {
    const passInfo = `通过 ${q.pass_count || 0} 次`
    const evalTime = q.last_eval_at ? formatDate(q.last_eval_at) : '未评估'
    const suggest = (q.pass_count || 0) >= detailSuggestPasses.value ? ' ⭐建议移除' : ''
    return `${passInfo} | ${evalTime}${suggest}`
  }
  return `难度:${q.difficulty}`
}

/* ===== 批量导入 ===== */
const importVisible = ref(false)
const importing = ref(false)
const importDsId = ref('')
const importJsonText = ref('')

function openImportDialog() {
  if (!datasets.value.length) {
    ElMessage.error('请先创建测试集')
    return
  }
  importDsId.value = ''
  importJsonText.value = ''
  importVisible.value = true
}

async function importQuestions() {
  const dsId = importDsId.value
  const jsonText = (importJsonText.value || '').trim()
  if (!dsId) { ElMessage.error('请选择测试集'); return }
  if (!jsonText) { ElMessage.error('请输入 JSON 数据'); return }
  let questions
  try {
    questions = JSON.parse(jsonText)
  } catch (e) {
    ElMessage.error('JSON 解析失败: ' + e.message)
    return
  }
  // 校验为数组,避免用户输入对象或字符串通过解析但在后端报错
  if (!Array.isArray(questions)) { ElMessage.error('JSON 数据必须是数组格式'); return }
  if (!questions.length) { ElMessage.error('JSON 数据不能为空'); return }
  importing.value = true
  try {
    const result = await api.postJson(`/api/v1/analytics/golden-datasets/${dsId}/import/`, { questions })
    ElMessage.success(`导入成功: 创建 ${result.created}, 更新 ${result.updated}`)
    importVisible.value = false
    loadDatasets()
  } catch (e) {
    ElMessage.error('导入失败: ' + errMsg(e, '未知错误'))
  } finally {
    importing.value = false
  }
}

/* ===== 低分回归 ===== */
/** 从生产低分对话沉淀到回归测试集(同步,后端直接返回结果) */
async function confirmSiphonRegression() {
  await confirm(
    { message: '从生产低分对话中取 top 50 沉淀到回归测试集。按 12 维均分升序取最低分,按领域分流', title: '沉淀低分对话', confirmText: '开始沉淀', type: 'info', errorText: '沉淀失败' },
    async () => {
      ElMessage.info('正在沉淀低分对话...')
      const result = await api.postJson('/api/v1/analytics/regression/siphon/', {})
      const n = result.siphoned || 0
      if (n === 0) {
        ElMessage.info(result.reason === 'no_candidates' ? '暂无新的低分对话可沉淀(可能已全部沉淀过)' : '沉淀完成,无新增')
      } else {
        const byRoot = result.by_root || {}
        const detail = Object.entries(byRoot).map(([k, v]) => `${k}:${v}`).join(' ')
        ElMessage.success(`沉淀完成: 新增 ${n} 条(${detail})`)
      }
      loadDatasets()
    },
  )
}

/** 对低分回归测试集执行全链路评估(异步派发,前端提示后刷新查看 pass_count) */
async function confirmRunRegressionEval() {
  await confirm(
    { message: '检索→生成→12 维评估,每问题约 90~180s,耗时较长', title: '评估回归', confirmText: '开始评估', errorText: '评估失败' },
    async () => {
      ElMessage.info('正在派发回归评估任务...')
      const result = await api.postJson('/api/v1/analytics/regression/eval/', {})
      if (result.queued) {
        ElMessage.info(result.message || '评估已派发,请稍后刷新查看 pass_count 变化')
      } else {
        ElMessage.success(`评估完成: 通过 ${result.passed || 0} / 失败 ${result.failed || 0}`)
        loadDatasets()
      }
    },
  )
}

onMounted(loadDatasets)

defineExpose({ reload: loadDatasets })
</script>

<style scoped>
/* 面板容器：撑满 Tab 剩余高度,固定区(工具栏/说明条)在上,
   测试集列表占满剩余空间并在内部滚动 */
.golden-panel-page {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.golden-panel-page .eval-toolbar,
.golden-panel-page .regression-tip,
.golden-panel-page .eval-panel:not(.eval-panel-scroll) {
  flex-shrink: 0;
}

/* 显式标记滚动目标：el-dialog 会在面板内渲染 el-overlay 兄弟节点,
   不能依赖 :last-of-type 判定 */
.golden-panel-page .eval-panel-scroll {
  flex: 1;
  min-height: 0;
  margin-bottom: 16px; /* 与同级 eval-panel 的 mb-3 间距保持一致 */
  display: flex;
  flex-direction: column;
}

/* 滚动下移到面板 body：标题固定,测试集列表占满剩余空间并在内部滚动 */
.golden-panel-page .eval-panel-scroll .eval-panel-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.golden-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}

.text-sub {
  color: var(--el-text-color-secondary, #6b7280);
}
</style>
