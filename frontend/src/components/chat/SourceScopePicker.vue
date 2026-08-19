<template>
  <!-- 知识来源选择器（el-popover 下拉面板）：来源开关 + 知识范围节点树 -->
  <el-popover
    v-model:visible="scopeOpen"
    trigger="click"
    placement="bottom-end"
    :width="380"
    popper-class="chat-scope-popover"
  >
    <div class="scope-dropdown-inner">
      <div class="scope-hint">💡 选择数据来源与知识范围，可更精准地查询</div>
      <!-- 来源开关：按 /api/v1/chat/config/ 返回的 sources_enabled 动态渲染
           外层用 div 而非 label：el-checkbox 内部已是 label，label 嵌套 label 会让浏览器的
           点击转发行为失效/混乱，导致点文字无法勾选。改为 div 后点文字走 onSourceRowToggle，
           点 checkbox 走 @click.stop + @change，互不干扰 -->
      <!-- 快速问答模式提示 -->
      <div v-if="disabled" class="scope-hint" style="color: var(--el-color-warning); margin-bottom: 8px;">
        ⚡ 快速问答模式仅支持内部文档，数据来源已锁定
      </div>
      <div class="source-switches">
        <div
          v-for="k in enabled"
          :key="k"
          class="scope-item source-switch"
          :class="{ 'is-disabled': disabled }"
          @click="disabled ? null : onSourceRowToggle(k)"
        >
          <el-checkbox
            @click.stop
            :model-value="!!sources[k]"
            :disabled="disabled"
            @change="val => onSourceChange(k, val)"
          />
          <span class="scope-label">
            <span class="scope-label-name">{{ SOURCE_META[k].icon }} {{ SOURCE_META[k].label }}</span>
            <span class="scope-label-desc">{{ SOURCE_META[k].desc }}</span>
          </span>
        </div>
      </div>
      <!-- 内部文档节点树（勾选「内部文档」时展示） -->
      <div v-if="sources.doc" class="doc-scope-wrap">
        <div class="scope-hint doc-scope-title">📚 知识范围（内部文档）</div>
        <div class="scope-quick-actions">
          <el-button size="small" @click="selectAllScopes">全选</el-button>
          <el-button size="small" @click="clearAllScopes">清空</el-button>
        </div>
        <el-scrollbar class="scope-list">
          <div
            v-for="n in scopeFlatList"
            :key="n.id"
            class="scope-item"
            :style="{ paddingLeft: 20 + n.depth * 20 + 'px' }"
            @click="onScopeRowToggle(n.id)"
          >
            <el-checkbox
              @click.stop
              :model-value="scopes.has(n.id)"
              @change="val => onScopeChange(n.id, val)"
            />
            <span class="scope-label">{{ n.name }}</span>
          </div>
          <div v-if="!scopeFlatList.length" class="scope-empty">暂无可用知识节点</div>
        </el-scrollbar>
      </div>
    </div>
    <template #reference>
      <el-button size="small" class="scope-switch-btn">
        <span>📚 {{ scopeBadge }}</span>
        <el-icon class="el-icon--right"><ArrowDown /></el-icon>
      </el-button>
    </template>
  </el-popover>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import api from '../../api/http'

// 来源/知识范围选择器（自 Chat.vue 抽出）：
// 勾选状态通过 v-model 与父组件共享（sources=enabled=scopes），
// 面板显隐、节点树加载、localStorage 持久化均在本组件内部完成

const SOURCE_META = {
  doc: { icon: '📄', label: '内部文档', desc: '已审核通过的企业知识，权威可信' },
  db: { icon: '🗄️', label: '数据库', desc: '实时查询业务数据库，数据真实' },
  web: { icon: '🌐', label: '联网', desc: '实时搜索全网公开信息，范围更大，仅供参考' },
  llm: { icon: '🤖', label: 'LLM', desc: '基于大模型自身知识作答，仅供参考，可能有误' },
}
const SCOPE_STORAGE_KEY = 'rag_chat_scope'       // 知识范围（节点）持久化
const SOURCE_STORAGE_KEY = 'rag_chat_sources'    // 数据来源开关持久化
const NODES_CACHE_KEY = 'rag_nodes_tree_cache_v2' // 节点树缓存（v2：仅权限节点，与旧全量树不兼容）
const NODES_CACHE_TTL = 2 * 60 * 60 * 1000       // 节点树缓存 TTL 2 小时

// 与父组件共享的勾选状态（父组件发送消息时读取）
const enabled = defineModel('enabled', { default: () => ['doc', 'db', 'web', 'llm'] })
const sources = defineModel('sources', { default: () => ({ doc: true, db: true, web: true, llm: true }) })
const scopes = defineModel('scopes', { default: () => new Set() })

// 快速问答模式下禁用来源切换
const props = defineProps({
  disabled: { type: Boolean, default: false },
})

// 节点树内部状态
const scopeTreeData = ref([])
const scopeFlatList = ref([])                     // 扁平化节点 [{id,name,depth,parent_id}]
const allScopeIds = ref([])
const scopeOpen = ref(false)

/* ==========================================================
   数据来源开关
   ========================================================== */
// 1) 请求 /api/v1/chat/config/ 获取系统开启的来源（失败静默回退全开）；
// 2) 从 localStorage 恢复上次勾选；
// 3) 保证至少保留一种来源，避免整组关闭后界面行为与预期不一致
async function initSourceSwitches() {
  try {
    const data = await api.getJson('/api/v1/chat/config/')
    if (data && Array.isArray(data.sources_enabled) && data.sources_enabled.length) {
      const filtered = data.sources_enabled.filter(k => SOURCE_META[k])
      if (filtered.length) enabled.value = filtered
      // 配置未开启的来源从当前勾选状态中剔除，避免残留被禁来源的本地状态
      Object.keys(sources.value).forEach(k => {
        if (!enabled.value.includes(k)) delete sources.value[k]
      })
    }
  } catch (e) { /* 配置接口失败时保持全开，聊天不受影响 */ }

  try {
    const saved = JSON.parse(localStorage.getItem(SOURCE_STORAGE_KEY) || 'null')
    if (saved && typeof saved === 'object') {
      enabled.value.forEach(k => { sources.value[k] = saved[k] !== false })
    }
  } catch (e) { /* 解析失败回退默认全开 */ }
  if (!enabled.value.some(k => sources.value[k])) sources.value[enabled.value[0]] = true

  // 勾选「内部文档」时懒加载节点树
  if (sources.value.doc && !scopeTreeData.value.length) loadScopeTree()
}

// 勾选/取消勾选数据来源：拦截"最后一个来源被取消"；切换后持久化
function onSourceChange(key, checked) {
  if (!key || !(key in sources.value)) return
  if (!checked && enabled.value.filter(k => sources.value[k]).length <= 1) {
    ElMessage.warning('至少保留一种数据来源')
    return
  }
  sources.value[key] = checked
  localStorage.setItem(SOURCE_STORAGE_KEY, JSON.stringify(sources.value))
  // 未勾选「内部文档」时不展示节点树；首次开启时懒加载
  if (sources.value.doc && !scopeTreeData.value.length) loadScopeTree()
}

// 整行点击切换数据来源（点 checkbox 本身走 @change，点文字/空白区域走这里）
function onSourceRowToggle(key) {
  if (!key || !(key in sources.value)) return
  if (sources.value[key] && enabled.value.filter(k => sources.value[k]).length <= 1) {
    ElMessage.warning('至少保留一种数据来源')
    return
  }
  onSourceChange(key, !sources.value[key])
}

// 当前开启的数据来源列表（按系统配置开启的顺序，随请求体发送）
function currentSourcesList() {
  return enabled.value.filter(k => sources.value[k])
}

// 顶部按钮徽标：快速问答模式下固定显示"内部文档"；全开→"全开"；部分开启→逗号分隔来源名
const scopeBadge = computed(() => {
  if (props.disabled) return '内部文档'
  const on = currentSourcesList()
  if (on.length === enabled.value.length) return '全开'
  return on.map(k => SOURCE_META[k].label).join(' / ')
})

/* ==========================================================
   知识库节点树（仅返回当前用户有权限检索的节点）
   ========================================================== */
// 节点树 localStorage 缓存：TTL 2 小时，节点树变更频率低
function getNodesCache() {
  try {
    const raw = localStorage.getItem(NODES_CACHE_KEY)
    if (!raw) return null
    const data = JSON.parse(raw)
    if (Date.now() - data.timestamp > NODES_CACHE_TTL) return null
    return data.tree || []
  } catch (e) { return null }
}

function setNodesCache(tree) {
  try {
    localStorage.setItem(NODES_CACHE_KEY, JSON.stringify({ timestamp: Date.now(), tree }))
  } catch (e) { /* localStorage 满或不可用，静默降级 */ }
}

// 应用节点树数据：扁平化 + 初始化选中状态（无本地记录时默认全选）
function applyScopeData(tree) {
  scopeTreeData.value = tree
  scopeFlatList.value = flattenScopeNodes(tree, 0)
  allScopeIds.value = scopeFlatList.value.map(n => String(n.id))
  const saved = localStorage.getItem(SCOPE_STORAGE_KEY)
  if (!saved || scopes.value.size === 0) {
    scopes.value = new Set(allScopeIds.value)
    saveScopeState()
  }
}

// 加载节点树：优先用缓存立即渲染，再后台静默刷新
async function loadScopeTree() {
  const saved = localStorage.getItem(SCOPE_STORAGE_KEY)
  if (saved) {
    try { scopes.value = new Set(JSON.parse(saved).map(String)) } catch (e) { scopes.value = new Set() }
  }
  const cachedTree = getNodesCache()
  if (cachedTree && cachedTree.length > 0) {
    applyScopeData(cachedTree)
    // 后台静默刷新：不阻塞页面渲染
    api.getJson('/api/v1/knowledge/nodes/tree/?permission_only=1').then(data => {
      const freshTree = data.tree || []
      if (freshTree.length > 0) {
        setNodesCache(freshTree)
        applyScopeData(freshTree)
      }
    }).catch(() => { /* 静默失败，保留缓存数据 */ })
    return
  }
  try {
    const data = await api.getJson('/api/v1/knowledge/nodes/tree/?permission_only=1')
    const tree = data.tree || []
    applyScopeData(tree)
    if (tree.length > 0) setNodesCache(tree)
  } catch (e) {
    console.error('load nodes failed:', e)
  }
}

function flattenScopeNodes(nodes, depth, parentId) {
  const result = []
  for (const n of nodes) {
    const id = String(n.id)
    result.push({ id, name: n.name, depth, parent_id: parentId ? String(parentId) : null })
    if (n.children && n.children.length) {
      result.push(...flattenScopeNodes(n.children, depth + 1, n.id))
    }
  }
  return result
}

/* 勾选/取消勾选节点时的级联逻辑：
 *  - 勾选父节点：所有子孙节点都勾选；勾选子节点：所有祖先节点都勾选
 *  - 取消勾选父节点：所有子孙节点都取消；取消勾选子节点：若同级全部未勾选则祖先也取消 */
function onScopeChange(id, checked) {
  id = String(id)
  if (checked) {
    scopes.value.add(id)
    selectChildNodes(id)
    selectAncestorNodes(id)
  } else {
    scopes.value.delete(id)
    unselectChildNodes(id)
    unselectAncestorIfNoChildSelected(id)
  }
  saveScopeState()
}

function getChildIds(parentId) {
  parentId = String(parentId)
  return scopeFlatList.value.filter(n => n.parent_id === parentId).map(n => n.id)
}

function selectChildNodes(parentId) {
  const childIds = getChildIds(parentId)
  for (const cid of childIds) {
    scopes.value.add(cid)
    selectChildNodes(cid)  // 递归
  }
}

function unselectChildNodes(parentId) {
  const childIds = getChildIds(parentId)
  for (const cid of childIds) {
    scopes.value.delete(cid)
    unselectChildNodes(cid)  // 递归
  }
}

function selectAncestorNodes(nodeId) {
  const node = scopeFlatList.value.find(n => n.id === nodeId)
  if (!node || !node.parent_id) return
  scopes.value.add(node.parent_id)
  selectAncestorNodes(node.parent_id)  // 递归向上
}

function unselectAncestorIfNoChildSelected(nodeId) {
  const node = scopeFlatList.value.find(n => n.id === nodeId)
  if (!node || !node.parent_id) return
  const siblings = getChildIds(node.parent_id)
  const anyChecked = siblings.some(sid => scopes.value.has(sid))
  if (!anyChecked) {
    scopes.value.delete(node.parent_id)
    unselectAncestorIfNoChildSelected(node.parent_id)  // 递归向上
  }
}

function selectAllScopes() {
  scopes.value = new Set(allScopeIds.value)
  saveScopeState()
}

function clearAllScopes() {
  scopes.value = new Set()
  saveScopeState()
}

// 整行点击切换节点勾选（点 checkbox 本身走 @change，点文字/空白区域走这里）
function onScopeRowToggle(id) {
  id = String(id)
  if (scopes.value.has(id)) {
    // 取消勾选：直接复用级联取消逻辑（若仅剩该节点，允许清空后由用户重新全选）
    onScopeChange(id, false)
  } else {
    onScopeChange(id, true)
  }
}

function saveScopeState() {
  localStorage.setItem(SCOPE_STORAGE_KEY, JSON.stringify([...scopes.value]))
}

onMounted(initSourceSwitches)
</script>

<style>
/* ============ 知识来源选择器（el-popover 下拉面板） ============ */
.chat-header .scope-switch-btn.el-button--small {
  padding: 6px 12px;
  font-size: 12px;
  border-radius: 6px;
}

.chat-scope-popover {
  padding: 0 !important;
}

.scope-dropdown-inner {
  display: flex;
  flex-direction: column;
  max-height: min(560px, calc(100vh - 120px));
  overflow: hidden;
}

.scope-hint {
  padding: 10px 14px;
  font-size: 12px;
  color: var(--text-sub);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 4px;
}

.scope-quick-actions {
  display: flex;
  gap: 6px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border);
}

.scope-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 0;
  min-height: 0;
}

.scope-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.1s;
}

.scope-item:hover {
  background: var(--hover);
}

.scope-item .scope-label {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scope-empty {
  padding: 20px 14px;
  text-align: center;
  font-size: 12px;
  color: var(--text-sub);
}

/* 数据来源开关 */
.source-switches {
  padding: 6px 8px 8px;
  border-bottom: 1px solid var(--border);
}

.source-switch {
  border-radius: 8px;
  margin: 1px 4px;
  align-items: flex-start;
}

.source-switch.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
  pointer-events: none;
}

.source-switch .scope-label {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  white-space: normal;
  overflow: visible;
}

.scope-label-name {
  font-weight: 500;
  font-size: 13px;
  color: var(--text);
}

.scope-label-desc {
  font-size: 11px;
  color: var(--text-sub);
  line-height: 1.4;
}

/* 内部文档节点树区域 */
.doc-scope-wrap {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-height: 340px;
}

.doc-scope-wrap .scope-hint {
  border-bottom: none;
  padding: 8px 14px 4px;
  font-weight: 600;
  color: var(--text-main);
}

.doc-scope-wrap .scope-quick-actions {
  border-bottom: none;
  padding: 2px 14px 6px;
}
</style>
