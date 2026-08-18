<template>
  <div class="page-container wiki-page">
    <!-- ============ 统一页头（列表/详情动态切换） ============ -->
    <div class="page-header">
      <!-- 列表态 -->
      <template v-if="detailId === null">
        <div>
          <div class="page-title">Wiki 知识库</div>
          <div class="page-desc">浏览 LLM 基于知识节点自动生成的 Wiki 页面，过期页面可一键刷新</div>
        </div>
        <!-- 生成按钮：无管理角色（纯查看者）隐藏，避免无权限操作 -->
        <el-button v-if="canGenerate" type="primary" size="small" @click="openGenerateModal">＋ 生成 Wiki</el-button>
      </template>
      <!-- 详情态：返回列表按钮 + 当前标题 + 操作按钮 -->
      <template v-else>
        <div class="detail-header-left">
          <el-button size="small" @click="showWikiList">← 返回列表</el-button>
          <div class="detail-header-title" :title="detail ? detail.title : ''">{{ detail ? detail.title : 'Wiki 详情' }}</div>
        </div>
        <!-- 操作按钮：仅当前用户对该节点有管理权限时展示（can_manage 由后端判定） -->
        <div class="page-header-actions">
          <el-button v-if="detail && detail.can_manage" size="small" @click="expireWiki(detail.id)">标记过期</el-button>
          <el-button v-if="detail && detail.can_manage" type="primary" size="small" @click="refreshWiki(detail.id)">🔄 刷新</el-button>
        </div>
      </template>
    </div>

    <!-- ============ 内容区：header 固定，body 内滚动 ============ -->
    <div class="page-body">
      <!-- 列表视图 -->
      <template v-if="detailId === null">
        <div class="wiki-filter-bar">
        <el-input
          v-model="filterQ"
          placeholder="🔍 搜索标题 / 摘要 / 标签"
          clearable
          style="width: 260px"
          @keyup.enter="resetAndLoad()"
          @clear="resetAndLoad()"
        />
        <el-select v-model="filterStatus" placeholder="全部状态" clearable style="width: 140px" @change="resetAndLoad()">
          <el-option label="已发布" value="published" />
          <el-option label="已过期" value="expired" />
          <el-option label="草稿" value="draft" />
        </el-select>
        <el-select v-model="filterRootType" placeholder="全部领域" clearable style="width: 150px" @change="resetAndLoad()">
          <el-option v-for="t in rootTypes" :key="t.code" :label="t.name" :value="t.code" />
        </el-select>
        <el-button type="primary" size="small" @click="resetAndLoad()">查询</el-button>
      </div>

        <div class="wiki-panel">
          <div class="page-scroll">
            <div v-loading="listLoading" class="wiki-list">
            <div v-if="!listLoading && wikiRows.length === 0" class="wiki-empty">
              <div class="wiki-empty-icon">📄</div>
              <div>暂无 Wiki 页面，点击右上角"生成 Wiki"创建</div>
            </div>
            <div v-for="r in wikiRows" :key="r.id" class="wiki-card" @click="openWikiDetail(r.id)">
              <div class="wiki-card-head">
                <span class="wiki-card-title" :title="r.title">{{ r.title }}</span>
                <span class="wiki-status" :class="statusClass(r.status)">{{ statusLabel(r.status) }}</span>
              </div>
              <div class="wiki-card-summary">{{ r.summary || '暂无摘要' }}</div>
              <div class="wiki-card-meta">
                <span v-if="r.node_path" class="wiki-node-path" :title="r.node_path">📁 {{ r.node_name || r.node_path }}</span>
                <span v-if="r.root_type">🏷️ {{ r.root_type }}</span>
                <span>👁️ {{ r.view_count || 0 }}</span>
                <span>🕐 {{ formatDate(r.updated_at) }}</span>
              </div>
            </div>
            </div>
          </div>
        </div>
        <!-- 分页：后端按 page_size 切片；切换每页条数时重置回第 1 页 -->
        <AppPagination
          class="wiki-pagination"
          :total="wikiTotal"
          :page-size="PAGE_SIZE"
          :page="page"
          @page-change="onPageChange"
        />
      </template>

      <!-- 详情视图 -->
      <div v-else class="wiki-panel">
        <div class="page-scroll">
          <div v-loading="detailLoading" class="wiki-detail-body">
          <template v-if="detail">
            <div class="wiki-detail">
              <!-- 已过期提示条：过期页面他人仍可浏览，但显著提示内容可能已过时（含审计信息） -->
              <div v-if="detail.status === 'expired'" class="wiki-expired-banner">
                <div class="wiki-expired-banner-title">⚠️ 该页面已标记为过期，内容可能已过时</div>
                <div class="wiki-expired-banner-meta">
                  <span v-if="detail.expired_at">标记时间：{{ formatDate(detail.expired_at) }}</span>
                  <span v-if="detail.expired_by_name">操作人：{{ detail.expired_by_name }}</span>
                  <span v-if="detail.expire_reason">原因：{{ detail.expire_reason }}</span>
                  <span v-else-if="!detail.expired_by_name">由系统在源文档更新时自动标记</span>
                  <span v-if="detail.can_manage">可点击右上角"刷新"重新生成</span>
                </div>
              </div>
              <div class="wiki-detail-head">
                <span class="wiki-detail-title">{{ detail.title }}</span>
                <span class="wiki-status" :class="statusClass(detail.status)">{{ statusLabel(detail.status) }}</span>
              </div>
              <div class="wiki-detail-meta">
                <span v-if="detail.node_path" :title="detail.node_path">📁 {{ detail.node_name || detail.node_path }}</span>
                <span>👁️ 浏览 {{ detail.view_count || 0 }}</span>
                <span>🕐 更新于 {{ formatDate(detail.updated_at) }}</span>
                <span v-if="detail.root_type">🏷️ {{ detail.root_type }}</span>
              </div>
              <div v-if="detail.tags && detail.tags.length" class="wiki-detail-tags">
                <span v-for="t in detail.tags" :key="t" class="tag">{{ t }}</span>
              </div>
              <div v-if="detail.summary" class="wiki-detail-summary">{{ detail.summary }}</div>
              <hr class="wiki-detail-divider">
              <!-- 正文走 Markdown 渲染（v-html 内容已经 escapeHtml + 白名单链接处理，防 XSS）；
                   正文内参考资料链接（[文件名](#)）点击委托 onMdLinkClick 打开文档预览 -->
              <div class="wiki-md" v-html="renderMarkdown(detail.content)" @click="onMdLinkClick"></div>
              <template v-if="detail.sections && detail.sections.length">
                <hr class="wiki-detail-divider">
                <!-- 章节内容同样走 Markdown 渲染，复用 wiki-md 样式 -->
                <div class="wiki-sections">
                  <div class="wiki-sections-title">📑 结构化章节</div>
                  <div v-for="s in detail.sections" :key="s.title" class="wiki-section">
                    <div class="wiki-section-title">{{ s.title }}</div>
                    <div class="wiki-md wiki-section-content" v-html="renderMarkdown(s.content)" @click="onMdLinkClick"></div>
                  </div>
                </div>
              </template>
              <hr class="wiki-detail-divider">
              <div class="wiki-links">
                <div class="wiki-links-block">
                  <div class="wiki-links-block-title">关联页面（本页指向）</div>
                  <template v-if="detail.outgoing_links && detail.outgoing_links.length">
                    <span
                      v-for="l in detail.outgoing_links"
                      :key="l.target_page_id"
                      class="wiki-link-item"
                      @click="openWikiDetail(l.target_page_id)"
                    >→ {{ l.link_text || l.target_title }}</span>
                  </template>
                  <div v-else class="text-sub">暂无</div>
                </div>
                <div class="wiki-links-block">
                  <div class="wiki-links-block-title">被引用（其他页指向本页）</div>
                  <template v-if="detail.incoming_links && detail.incoming_links.length">
                    <span
                      v-for="l in detail.incoming_links"
                      :key="l.target_page_id"
                      class="wiki-link-item"
                      @click="openWikiDetail(l.target_page_id)"
                    >← {{ l.link_text || l.target_title }}</span>
                  </template>
                  <div v-else class="text-sub">暂无</div>
                </div>
              </div>
            </div>
          </template>
            <div v-else-if="!detailLoading" class="wiki-empty">页面不存在或已删除</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 生成 Wiki 弹窗 ============ -->
    <el-dialog v-model="generateVisible" title="生成 Wiki" width="560px" top="10vh">
      <p class="text-sub generate-tip">选择一个知识节点，系统将基于该节点下已发布的文档异步生成 Wiki 页面。</p>
      <el-input v-model="generateNodeSearch" placeholder="🔍 搜索节点名称" clearable style="margin-bottom: 10px" @input="filterGenerateNodes" />
      <div v-loading="nodeLoading" class="generate-node-list">
        <div v-if="!nodeLoading && filteredGenerateNodes.length === 0" class="generate-node-empty">无可选节点</div>
        <div
          v-for="n in filteredGenerateNodes"
          :key="n.id"
          class="generate-node-item"
          :class="{ selected: selectedGenerateNodeId === n.id }"
          @click="selectGenerateNode(n.id)"
        >
          <span class="gni-name" :style="{ paddingLeft: n.depth * 18 + 'px' }">{{ n.depth > 0 ? '▸ ' : '' }}{{ n.name }}</span>
          <span class="gni-docs">📄 {{ n.doc_count }} 篇文档</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="generateVisible = false">取消</el-button>
        <!-- 未选中节点时不可提交 -->
        <el-button type="primary" :disabled="selectedGenerateNodeId === null" :loading="generating" @click="doGenerate">开始生成</el-button>
      </template>
    </el-dialog>

    <!-- ============ 文档预览弹窗（参考资料：有权限直接预览原文） ============ -->
    <DocPreviewDialog v-model="previewVisible" :doc-id="previewDocId" />

    <!-- ============ 申请文档访问权限弹窗（参考资料无权限时触发） ============ -->
    <el-dialog v-model="reqVisible" title="申请访问权限" width="480px" top="10vh">
      <p class="text-sub" style="margin-bottom: 10px">申请访问文档《{{ reqDocTitle }}》。提交后将生成审批工单，由资源所有者或管理员审批。</p>
      <el-input
        v-model="reqReason"
        type="textarea"
        :rows="3"
        placeholder="请填写申请原因（选填）"
        maxlength="500"
        show-word-limit
      />
      <template #footer>
        <el-button @click="reqVisible = false">取消</el-button>
        <el-button type="primary" :loading="reqSubmitting" @click="submitRefRequest">提交申请</el-button>
      </template>
    </el-dialog>

    <!-- ============ 标记过期弹窗（需填写原因，供审计与其他读者理解） ============ -->
    <el-dialog v-model="expireVisible" title="标记过期" width="480px" top="10vh">
      <p class="text-sub" style="margin-bottom: 10px">确认将该页面标记为过期？过期后他人仍可浏览，但页首会显著提示内容可能已过时；建议随后点击"刷新"重新生成。</p>
      <el-input
        v-model="expireReason"
        type="textarea"
        :rows="3"
        placeholder="请填写过期原因（选填，便于审计与其他读者理解）"
        maxlength="500"
        show-word-limit
      />
      <template #footer>
        <el-button @click="expireVisible = false">取消</el-button>
        <el-button type="primary" :loading="expiring" @click="doExpireWiki">确认标记过期</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, errMsg } from '../utils/format'
import { renderMarkdown } from '../utils/markdown'
import DocPreviewDialog from '../components/doc-preview/DocPreviewDialog.vue'
import AppPagination from '../components/base/AppPagination.vue'
import { usePagination } from '../composables/usePagination'
import { useListLoader } from '../composables/useListLoader'
import { useConfirm } from '../composables/useConfirm'

const WIKI_API = '/api/v1/wiki'
const NODE_API = '/api/v1/knowledge/nodes'
const DOC_API = '/api/v1/knowledge/documents'
const PAGE_SIZE = 20 // 后端分页默认每页 20 条

const userStore = useUserStore()

/* ==========================================================
   状态
   ========================================================== */
const wikiRows = ref([])
const wikiTotal = ref(0)
// 列表加载：由 useListLoader 统一管理 loading/请求序号守卫/错误提示（403 等业务错误直接展示后端消息）
const { loading: listLoading, load } = useListLoader(fetchWikiList, {
  directError: (e) => e.status === 403,
})
// 二次确认弹窗统一封装
const { confirm } = useConfirm()
// 分页状态：由 usePagination 统一管理翻页后的重新加载
const { page, onPageChange } = usePagination(() => load())
const detailId = ref(null)       // 当前详情页 ID（null = 列表视图）
const detail = ref(null)         // 当前详情数据
const detailLoading = ref(false)
const filterQ = ref('')
const filterStatus = ref('')
const filterRootType = ref('')
const rootTypes = ref([])        // 领域（root_type）筛选项
// 生成弹窗
const generateVisible = ref(false)
const nodeLoading = ref(false)
const generating = ref(false)
const generateNodeSearch = ref('')
const generateNodes = ref([])    // 生成弹窗的扁平化节点列表
const selectedGenerateNodeId = ref(null)
// 参考资料：文档预览弹窗（有权限时）
const previewVisible = ref(false)
const previewDocId = ref(null)
// 参考资料：申请文档访问权限弹窗（无权限时）
const reqVisible = ref(false)
const reqDocId = ref(null)
const reqDocTitle = ref('')
const reqReason = ref('')
const reqSubmitting = ref(false)
// 标记过期弹窗（需填写原因，供审计）
const expireVisible = ref(false)
const expireReason = ref('')
const expiring = ref(false)

// 可生成 Wiki 的角色（纯查看者隐藏生成入口）
const canGenerate = computed(() =>
  userStore.hasAnyRole('contributor', 'super_admin', 'kb_admin', 'dept_manager', 'team_leader')
)

// 筛选/搜索/生成等场景：回到第 1 页重新加载
function resetAndLoad() {
  page.value = 1
  load()
}

/* ============ 列表 ============ */
async function fetchWikiList() {
  const params = new URLSearchParams({ page: String(page.value), page_size: String(PAGE_SIZE) })
  const q = filterQ.value.trim()
  if (q) params.set('q', q)
  if (filterStatus.value) params.set('status', filterStatus.value)
  if (filterRootType.value) params.set('root_type', filterRootType.value)

  const data = await api.getJson(`${WIKI_API}/pages/?${params.toString()}`)
  wikiRows.value = data.results || []
  wikiTotal.value = data.count || 0
}
/* ============ 状态标签 ============ */
function statusLabel(status) {
  return { published: '已发布', expired: '已过期', draft: '草稿' }[status] || '草稿'
}

function statusClass(status) {
  return { published: 'wiki-status-published', expired: 'wiki-status-expired', draft: 'wiki-status-draft' }[status] || 'wiki-status-draft'
}

/* ============ 详情 ============ */
async function openWikiDetail(id) {
  detailId.value = id
  detail.value = null
  detailLoading.value = true
  try {
    const d = await api.getJson(`${WIKI_API}/pages/${id}/`)
    detail.value = d
  } catch (e) {
    detail.value = null
    // 403 等业务错误直接展示后端消息
    const msg = e.status === 403 ? errMsg(e, '未知错误') : `加载失败：${errMsg(e, '未知错误')}`
    ElMessage.error(msg)
  } finally {
    detailLoading.value = false
  }
}

function showWikiList() {
  detailId.value = null
  detail.value = null
  load()
}

/* ============ 操作：刷新 / 标记过期 ============ */
async function refreshWiki(id) {
  try {
    const res = await api.postJson(`${WIKI_API}/pages/${id}/refresh/`, {})
    ElMessage.success(res.detail || '刷新任务已提交')
    // 异步任务生成期间展示加载态，稍后自动刷新详情（用户已离开详情则不打扰）
    setTimeout(() => {
      if (detailId.value === id) openWikiDetail(id)
    }, 3000)
  } catch (e) {
    ElMessage.error('刷新失败：' + errMsg(e, '未知错误'))
  }
}

// 标记过期入口：先弹窗确认并填写原因，再提交后端（后端记录操作人 / 时间 / 原因）
function expireWiki(id) {
  expireReason.value = ''
  expireVisible.value = true
}

async function doExpireWiki() {
  if (!detail.value) return
  expiring.value = true
  try {
    const res = await api.postJson(`${WIKI_API}/pages/${detail.value.id}/expire/`, {
      reason: (expireReason.value || '').trim(),
    })
    expireVisible.value = false
    ElMessage.success(res.detail || '已标记过期')
    openWikiDetail(detail.value.id)
  } catch (e) {
    ElMessage.error('操作失败：' + errMsg(e, '未知错误'))
  } finally {
    expiring.value = false
  }
}

/* ============ 参考资料：预览 / 申请权限 ============ */

// 点击参考资料：有权限直接打开文档预览；无权限先弹窗询问是否申请权限
async function onRefDocClick(d) {
  if (d.can_access) {
    previewDocId.value = d.id
    previewVisible.value = true
    return
  }
  const ok = await confirm({
    message: `当前账号没有权限访问文档《${d.title}》，是否申请权限？`,
    title: '无法访问',
    confirmText: '申请权限',
  })
  if (!ok) return
  reqDocId.value = d.id
  reqDocTitle.value = d.title
  reqReason.value = ''
  reqVisible.value = true
}

// 正文内参考资料链接（.wiki-md-ref）点击委托：按链接文字匹配来源文档，
// 有权限直接预览，无权限提示申请权限；外部链接保持新开页面行为
async function onMdLinkClick(e) {
  const el = e.target.closest ? e.target.closest('a.wiki-md-ref') : null
  if (!el) return
  e.preventDefault()
  const name = (el.textContent || '').trim()
  const srcDocs = detail.value?.source_docs || []
  // 先匹配前 20 条本地参考资料（优先文件名，再按标题兜底）
  const doc = srcDocs.find(d => d.file_name === name || d.title === name)
  if (doc) {
    onRefDocClick(doc)
    return
  }
  // 无参考资料（如社区页）则静默，不做服务端解析
  if (!srcDocs.length) return
  // 本地未匹配：服务端按名兜底解析（不设条数上限，覆盖超 20 条/状态变化场景）
  try {
    const res = await api.getJson(
      `${WIKI_API}/pages/${detail.value.id}/resolve_doc/?name=${encodeURIComponent(name)}`)
    if (res.found) {
      onRefDocClick({
        id: res.id, title: res.title, file_name: res.file_name,
        file_type: res.file_type, can_access: res.can_access,
      })
      return
    }
  } catch (err) {
    // 解析接口异常不阻断，落到下方提示
  }
  ElMessage.info(`未找到对应文档：${name}`)
}

// 提交文档访问申请（走统一审批工单）
async function submitRefRequest() {
  reqSubmitting.value = true
  try {
    await api.postJson(`${DOC_API}/${reqDocId.value}/request_access/`, {
      action: 'read',
      reason: (reqReason.value || '').trim(),
    })
    reqVisible.value = false
    ElMessage.success('申请已提交，等待审批')
  } catch (e) {
    ElMessage.error(errMsg(e, '申请失败'))
  } finally {
    reqSubmitting.value = false
  }
}

/* ============ 生成弹窗 ============ */
async function openGenerateModal() {
  generateVisible.value = true
  selectedGenerateNodeId.value = null
  generateNodeSearch.value = ''
  generateNodes.value = []
  nodeLoading.value = true
  try {
    const res = await api.getJson(`${NODE_API}/tree/`)
    generateNodes.value = flattenNodes(res.tree || [])
  } catch (e) {
    generateNodes.value = []
    ElMessage.error('节点加载失败：' + errMsg(e, '未知错误'))
  } finally {
    nodeLoading.value = false
  }
}

// 将树形节点扁平化，保留层级缩进信息用于展示；
// doc_count 自底向上汇总为"该节点及全部子节点的文档总数"（子树文档量，一次遍历 O(n)）
function flattenNodes(nodes, depth = 0) {
  const acc = []
  const byId = new Map()
  function walk(ns, d) {
    for (const n of ns) {
      const item = {
        id: n.id,
        parent_id: n.parent_id,
        name: n.name,
        path: n.path,
        root_type: n.root_type,
        node_level: n.node_level,
        depth: d,
        doc_count: n.document_count || 0,
      }
      byId.set(n.id, item)
      acc.push(item)
      if (n.children && n.children.length) walk(n.children, d + 1)
    }
  }
  walk(nodes, depth)
  // 深度优先先父后子，逆序即自底向上：子节点计数累加到父节点
  for (let i = acc.length - 1; i >= 0; i--) {
    const item = acc[i]
    if (item.parent_id && byId.has(item.parent_id)) {
      byId.get(item.parent_id).doc_count += item.doc_count
    }
  }
  return acc
}

// 过滤出名称匹配的节点及其全部祖先（保证层级上下文可见）
const filteredGenerateNodes = computed(() => {
  const q = generateNodeSearch.value.trim().toLowerCase()
  if (!q) return generateNodes.value
  const all = generateNodes.value
  const matched = new Set(all.filter(n => n.name.toLowerCase().includes(q)).map(n => n.id))
  const ids = new Set()
  all.forEach(n => {
    if (matched.has(n.id)) {
      ids.add(n.id)
      // 依据 path 前缀找出祖先
      all.forEach(p => {
        if (p.path && n.path && n.path !== p.path && n.path.startsWith(p.path)) ids.add(p.id)
      })
    }
  })
  return all.filter(n => ids.has(n.id))
})

function selectGenerateNode(id) {
  selectedGenerateNodeId.value = id
}

async function doGenerate() {
  if (selectedGenerateNodeId.value === null) {
    ElMessage.warning('请先选择一个知识节点')
    return
  }
  generating.value = true
  try {
    const res = await api.postJson(`${WIKI_API}/pages/generate/`, { node_id: selectedGenerateNodeId.value })
    ElMessage.success(res.detail || '生成任务已提交')
    generateVisible.value = false
    resetAndLoad()
  } catch (e) {
    ElMessage.error('生成失败：' + errMsg(e, '未知错误'))
  } finally {
    generating.value = false
  }
}

/* ============ 初始化 ============ */
async function loadRootTypeFilter() {
  try {
    const res = await api.getJson(`${NODE_API}/root_types/`)
    rootTypes.value = res.root_types || []
  } catch (e) {
    // 根类型加载失败不阻塞页面，仅无法按领域过滤
    console.error('load root types failed:', e)
  }
}

onMounted(() => {
  userStore.restore()
  loadRootTypeFilter().then(resetAndLoad)
})
</script>

<style scoped>
/* ===== 详情态页头左侧（返回按钮 + 标题） ===== */
.detail-header-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.detail-header-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 480px;
}

/* ===== 过滤栏 ===== */
.wiki-filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

/* ===== 列表卡片 ===== */
.wiki-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  /* 面板内四边留白，避免卡片贴边 */
  padding: 16px 20px;
}

.wiki-card {
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 16px 20px;
  background: var(--app-card-bg);
  cursor: pointer;
  transition: box-shadow 0.15s, border-color 0.15s;
}

.wiki-card:hover {
  border-color: #2563eb;
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.08);
}

.wiki-card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.wiki-card-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.wiki-card-summary {
  font-size: 13px;
  color: var(--app-text-sub);
  margin-bottom: 10px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.wiki-card-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  color: var(--app-text-sub);
  flex-wrap: wrap;
}

.wiki-card-meta .wiki-node-path {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ===== 状态标签 ===== */
.wiki-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  line-height: 1;
  padding: 4px 10px;
  border-radius: 999px;
  flex-shrink: 0;
}

.wiki-status::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.wiki-status-published {
  background: #ecfdf5;
  color: #16a34a;
}

.wiki-status-expired {
  background: #fffbeb;
  color: #f59e0b;
}

.wiki-status-draft {
  background: var(--app-menu-hover);
  color: var(--app-text-sub);
}

/* ===== 空态 ===== */
.wiki-empty {
  padding: 60px 0;
  text-align: center;
  color: var(--app-text-sub);
}

.wiki-empty-icon {
  font-size: 48px;
  margin-bottom: 8px;
}

.wiki-pagination {
  margin-top: 16px;
  justify-content: center;
  flex-shrink: 0;
}

/* ===== 详情页 ===== */
/* 内容面板：page-body 与 page-scroll 之间的美化层，负责背景 / 边框 / 圆角；
   page-body 只负责框架撑满，page-scroll 在面板内部负责滚动展示内容 */
.wiki-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  overflow: hidden;
}

/* 详情正文：卡片外观已由 .wiki-panel 承担，这里只保留阅读栏宽度与内边距 */
.wiki-detail {
  padding: 28px 32px;
  max-width: 960px;
  margin: 0 auto;
}

/* 已过期提示条：过期页面仍可浏览，但显著提示内容可能已过时（含审计信息） */
.wiki-expired-banner {
  background: #fff8e1;
  border: 1px solid #f0d98c;
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 20px;
}

.wiki-expired-banner-title {
  font-size: 14px;
  font-weight: 600;
  color: #9a6700;
}

.wiki-expired-banner-meta {
  margin-top: 6px;
  font-size: 12px;
  color: #9a6700;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
}

html.dark .wiki-expired-banner {
  background: rgba(245, 158, 11, 0.12);
  border-color: rgba(245, 158, 11, 0.35);
}

html.dark .wiki-expired-banner-title,
html.dark .wiki-expired-banner-meta {
  color: #f0c674;
}

.wiki-detail-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 10px;
}

.wiki-detail-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--app-text);
  line-height: 1.4;
  word-break: break-all;
}

.wiki-detail-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.wiki-detail-tags {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 18px;
}

.tag {
  display: inline-block;
  background: var(--app-menu-hover);
  color: var(--app-text);
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 4px;
}

.wiki-detail-summary {
  background: #eff6ff;
  border-left: 3px solid #2563eb;
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 14px;
  color: var(--app-text);
  margin-bottom: 20px;
}

/* 暗色系：浅蓝底配 var(--app-text)（暗色下为浅色文字）会白底浅字不可读，
   改为深色半透明蓝底 + 正常文字色 */
html.dark .wiki-detail-summary {
  background: rgba(37, 99, 235, 0.15);
  border-left-color: #3b82f6;
}

.wiki-detail-divider {
  border: none;
  border-top: 1px solid var(--app-border);
  margin: 20px 0;
}

/* ===== 章节目录 ===== */
.wiki-sections {
  margin-bottom: 20px;
}

.wiki-sections-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 10px;
}

.wiki-section {
  margin-bottom: 18px;
}

.wiki-section-title {
  font-size: 16px;
  font-weight: 600;
  color: #2563eb;
  margin-bottom: 8px;
  border-left: 3px solid #2563eb;
  padding-left: 10px;
}

.wiki-section-content {
  font-size: 14px;
  color: var(--app-text);
  line-height: 1.8;
  word-break: break-word;
}

/* ===== 相关链接 ===== */
.wiki-links {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

@media (max-width: 720px) {
  .wiki-links { grid-template-columns: 1fr; }
}

.wiki-links-block {
  background: var(--app-menu-hover);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 12px 16px;
}

.wiki-links-block-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 8px;
}

.wiki-link-item {
  display: block;
  padding: 6px 0;
  font-size: 13px;
  color: #2563eb;
  cursor: pointer;
  border-bottom: 1px dashed var(--app-border);
}

.wiki-link-item:last-child { border-bottom: none; }
.wiki-link-item:hover { text-decoration: underline; }

.text-sub {
  color: var(--app-text-sub);
  font-size: 13px;
}

/* ===== 生成弹窗：节点选择 ===== */
.generate-tip {
  margin-bottom: 12px;
}

.generate-node-list {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  max-height: 320px;
  overflow-y: auto;
  min-height: 60px;
}

.generate-node-empty {
  text-align: center;
  color: var(--app-text-sub);
  padding: 16px;
  font-size: 13px;
}

.generate-node-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 14px;
  cursor: pointer;
  font-size: 13px;
  transition: background 0.1s;
}

.generate-node-item:hover { background: #eff6ff; }

.generate-node-item.selected {
  background: #eff6ff;
  color: #2563eb;
  font-weight: 500;
}

.generate-node-item .gni-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.generate-node-item .gni-docs {
  font-size: 12px;
  color: var(--app-text-sub);
  white-space: nowrap;
  flex-shrink: 0;
}

.generate-node-item.selected .gni-docs { color: #2563eb; }

/* ==========================================================
   Markdown 渲染（v-html 内容没有 scoped 属性，需用 :deep 命中）
   ========================================================== */
.wiki-md {
  font-size: 14px;
  line-height: 1.8;
  color: var(--app-text);
  word-break: break-word;
}

/* 正文内参考资料链接：蓝字可点、悬停下划线（与外部链接视觉一致，点击为预览弹窗）；
   链接元素在 v-html 内容中，需用 :deep 命中 */
.wiki-md :deep(a.wiki-md-ref) {
  color: #2563eb;
  text-decoration: none;
  cursor: pointer;
}

.wiki-md :deep(a.wiki-md-ref:hover) {
  text-decoration: underline;
}

.wiki-md :deep(h1) { font-size: 22px; margin: 20px 0 10px; }
.wiki-md :deep(h2) { font-size: 19px; margin: 18px 0 8px; }
.wiki-md :deep(h3) { font-size: 16px; margin: 16px 0 6px; }
.wiki-md :deep(h4) { font-size: 14px; margin: 12px 0 4px; }
.wiki-md :deep(p) { margin: 8px 0; }
.wiki-md :deep(ul), .wiki-md :deep(ol) { padding-left: 22px; margin: 8px 0; }
.wiki-md :deep(li) { margin: 4px 0; }
.wiki-md :deep(a) { color: #2563eb; text-decoration: underline; }
.wiki-md :deep(blockquote) {
  border-left: 3px solid var(--app-border);
  background: var(--app-menu-hover);
  color: var(--app-text-sub);
  padding: 8px 14px;
  margin: 10px 0;
  border-radius: 0 6px 6px 0;
}
.wiki-md :deep(code.md-inline-code) {
  background: var(--app-menu-hover);
  color: #d6336c;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
}
.wiki-md :deep(pre.md-code) {
  background: #1f2937;
  color: #e5e7eb;
  padding: 14px 16px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 10px 0;
}
.wiki-md :deep(pre.md-code code) {
  background: transparent;
  color: inherit;
  padding: 0;
  font-size: 13px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  line-height: 1.6;
}
.wiki-md :deep(hr) {
  border: none;
  border-top: 1px solid var(--app-border);
  margin: 16px 0;
}
.wiki-md :deep(img) { max-width: 100%; border-radius: 6px; }
.wiki-md :deep(strong) { color: var(--app-text); }
.wiki-md :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
  width: 100%;
  font-size: 13px;
}
.wiki-md :deep(th), .wiki-md :deep(td) {
  border: 1px solid var(--app-border);
  padding: 6px 12px;
  text-align: left;
}
.wiki-md :deep(th) { background: var(--app-menu-hover); font-weight: 600; }
</style>
