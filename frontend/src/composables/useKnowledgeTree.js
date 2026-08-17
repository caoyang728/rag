import { nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { errMsg } from '../utils/format'

// 节点树 API 前缀（本 composable 内部专用）
const NODE_API = '/api/v1/knowledge/nodes'

// 默认图标列表（按顺序分配给新根类型）
const DEFAULT_ROOT_ICONS = ['📁', '💻', '🧠', '🛠️', '📚', '🔧', '📋', '📊', '🔍', '⭐']

// 节点可见级别文案（与后端 visibility_level 枚举对应）
const NODE_VIS_LABELS = { TEAM_ONLY: '仅团队', DEPT_ONLY: '仅部门', PUBLIC: '全局公开' }

/**
 * 知识节点树共享逻辑：节点树加载/选中/图标/路径、根类型映射，
 * 以及"节点树 + 团队组长权限"相关判断辅助。
 * 页面上的节点弹窗、文档列表、可见范围设置等多个业务模块都依赖这些状态与函数，
 * 抽取后统一维护，避免各模块重复写树的遍历与权限判断。
 * @param {{ onSelect?: (id: number) => void }} [opts]
 *        onSelect：节点选中后的回调，用于联动同步文档列表的节点筛选（父页面按需传入）
 */
export function useKnowledgeTree(opts = {}) {
  const { onSelect } = opts
  const userStore = useUserStore()

  /* ==========================================================
     权限辅助（与旧版 isAdminOrOps/isTeamLeader 行为一致）
     ========================================================== */
  function isTeamLeader() {
    return userStore.hasAnyRole('team_leader')
  }

  function canManageNodes() {
    return userStore.isAdminOrOps || isTeamLeader()
  }

  /** 获取团队组长可管理的团队节点 ID 列表（从 nodeTree 中查找 node_level=3 且 ref_id 匹配的节点） */
  function getTeamLeaderTeamNodeIds() {
    if (!isTeamLeader()) return []
    const u = userStore.user || {}
    const teamIds = u.team ? [u.team.id] : []
    if (!teamIds.length) return []
    const ids = []
    ;(function walk(nodes) {
      nodes.forEach(n => {
        if (n.node_level === 3 && teamIds.includes(n.ref_id)) ids.push(n.id)
        if (n.children) walk(n.children)
      })
    })(nodeTree.value)
    return ids
  }

  /** 判断节点是否在团队组长的团队子树内 */
  function isNodeInTeam(n, teamNodeIds) {
    if (!teamNodeIds.length) return false
    for (let i = 0; i < teamNodeIds.length; i++) {
      if (n.path) {
        const found = findNodeById(teamNodeIds[i])
        if (found && found.path && (n.path === found.path || n.path.indexOf(found.path) === 0)) {
          return true
        }
      }
    }
    return false
  }

  /** 按 id 在节点树中查找节点（深度优先遍历） */
  function findNodeById(id) {
    let result = null
    ;(function walk(nodes) {
      for (let i = 0; !result && i < nodes.length; i++) {
        if (nodes[i].id === id) { result = nodes[i]; return }
        if (nodes[i].children) walk(nodes[i].children)
      }
    })(nodeTree.value)
    return result
  }

  /* ==========================================================
     节点树
     ========================================================== */
  const nodeTree = ref([])
  const selectedNodeId = ref(null)
  const selectedNode = ref(null)
  const treeRef = ref(null)
  const treeLoading = ref(false)
  // 动态加载的根类型映射（API 不可用时降级为默认值，防止页面完全崩溃）
  const rootTypeMap = ref({})
  const rootIconMap = ref({})

  async function loadRootTypes() {
    try {
      const res = await api.getJson(NODE_API + '/root_types/')
      const types = res.root_types || []
      const map = {}
      const iconMap = {}
      types.forEach((t, index) => {
        map[t.code] = t.name
        iconMap[t.code] = DEFAULT_ROOT_ICONS[index % DEFAULT_ROOT_ICONS.length]
      })
      rootTypeMap.value = map
      rootIconMap.value = iconMap
    } catch (e) {
      rootTypeMap.value = { company_doc: '企业文档' }
      rootIconMap.value = { company_doc: '📁' }
    }
  }

  /** 加载节点树（后端已返回嵌套树结构，直接使用） */
  async function loadTree() {
    treeLoading.value = true
    try {
      const res = await api.getJson(NODE_API + '/tree/')
      nodeTree.value = res.tree || []
      // 旧版树默认全展开，加载完成后展开全部节点
      nextTick(() => expandAllTree())
    } catch (e) {
      ElMessage.error('加载失败: ' + errMsg(e, '未知错误'))
    } finally {
      treeLoading.value = false
    }
  }

  /** 节点图标：根节点按领域图标；组织节点（部门/团队）🏢；叶子 📄；文件夹 📂 */
  function nodeIcon(n) {
    if (n.node_kind === 'ROOT' || n.node_type === 'root') return rootIconMap.value[n.root_type] || '📁'
    if (n.node_kind === 'ORG') return '🏢'
    if (n.node_type === 'leaf') return '📄'
    return '📂'
  }

  function expandAllTree() {
    const tree = treeRef.value
    if (!tree) return
    const nodesMap = tree.store.nodesMap || {}
    Object.values(nodesMap).forEach(n => {
      if (n.childNodes && n.childNodes.length) n.expanded = true
    })
  }

  /** 点击节点：记录选中 + 同步文档列表节点筛选 + 加载节点详情 */
  function onTreeNodeClick(data) {
    selectNode(data.id)
  }

  async function selectNode(id) {
    selectedNodeId.value = id
    // 同步文档列表的节点筛选（不自动展开）
    if (onSelect) onSelect(id)
    // 高亮当前选中节点
    nextTick(() => {
      try { treeRef.value && treeRef.value.setCurrentKey(id) } catch (e) { /* 树未就绪时忽略 */ }
    })
    try {
      const node = await api.getJson(NODE_API + '/' + id + '/')
      if (selectedNodeId.value === id) selectedNode.value = node
    } catch (e) {
      ElMessage.error('加载节点详情失败')
    }
  }

  function nodePathText(n) {
    return n.path ? n.path.replace(/\/$/, '').replace(/\//g, ' / ').trim() : '/'
  }

  function rootTypeName(n) {
    return rootTypeMap.value[n.root_type] || n.root_type
  }

  function nodeVisLabel(n) {
    return n.visibility_level ? (NODE_VIS_LABELS[n.visibility_level] || n.visibility_level) : '继承父级'
  }

  /** 编辑权限：admin/ops 全可见，团队组长仅本团队范围内可见 */
  function canEditNode(n) {
    return userStore.isAdminOrOps || (isTeamLeader() && isNodeInTeam(n, getTeamLeaderTeamNodeIds()))
  }

  return {
    nodeTree, selectedNodeId, selectedNode, treeRef, treeLoading,
    loadRootTypes, loadTree, expandAllTree, onTreeNodeClick, selectNode,
    findNodeById, isTeamLeader, canManageNodes, getTeamLeaderTeamNodeIds, canEditNode,
    nodeIcon, nodePathText, rootTypeName, nodeVisLabel,
  }
}
