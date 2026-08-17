import { computed, reactive, ref } from 'vue'
import api from '../../api/http'

/**
 * 部门/团队级联筛选（原 common.js OrgFilter 的 Vue 版）
 * - 组织架构数据全局缓存,所有面板共享一份,只拉取一次
 * - 部门 API: /api/v1/auth/departments/?page_size=100
 * - 团队 API: /api/v1/auth/teams/?page_size=100
 * - 失败时降级为"全部",下拉保持可用
 */

// 全局缓存必须用 reactive 包裹：computed(() => cache.depts) 首次求值发生在异步加载完成之前,
// 若 cache 是普通对象,之后 load() 给 cache.depts 赋值时 computed 无法感知变化,下拉将永远为空
const cache = reactive({ loaded: false, depts: [], teams: [], deptMap: {}, teamMap: {}, teamsByDept: {} })
// Promise 缓存:多个面板并发初始化时复用同一个加载 Promise,避免重复发请求
let loadPromise = null

async function load() {
  if (cache.loaded) return
  if (loadPromise) return loadPromise
  loadPromise = (async () => {
    try {
      const [deptResp, teamResp] = await Promise.all([
        api.getJson('/api/v1/auth/departments/?page_size=100'),
        api.getJson('/api/v1/auth/teams/?page_size=100'),
      ])
      // 兼容 DRF 分页 {results:[...]} 和裸数组两种返回格式
      const rawDepts = Array.isArray(deptResp) ? deptResp : (deptResp.results || [])
      const rawTeams = Array.isArray(teamResp) ? teamResp : (teamResp.results || [])
      // 过滤无 id 的脏数据：后端极端情况下可能返回空元素,若不去掉,
      // 渲染期 v-for 迭代到 undefined 项时会抛 "Cannot read properties of undefined (reading 'id')"
      const depts = rawDepts.filter(d => d && d.id != null)
      const teams = rawTeams.filter(t => t && t.id != null)
      cache.depts = depts.sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0) || String(a.name).localeCompare(String(b.name)))
      cache.teams = teams.sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0) || String(a.name).localeCompare(String(b.name)))
      cache.deptMap = Object.fromEntries(cache.depts.map(d => [d.id, d]))
      cache.teamMap = Object.fromEntries(cache.teams.map(t => [t.id, t]))
      // 按部门分组团队,department_id 为 null 的归入 "__orphan__"（不影响下拉）
      cache.teamsByDept = {}
      for (const t of cache.teams) {
        const key = t.department_id ?? '__orphan__'
        ;(cache.teamsByDept[key] ||= []).push(t)
      }
    } catch (e) {
      console.warn('[OrgFilter] 加载组织架构失败,将降级为"全部":', e)
    } finally {
      cache.loaded = true
      loadPromise = null
    }
  })()
  return loadPromise
}

/** 将 dept_id / team_id 转为人类可读的范围描述（teamId 优先,都为空时返回"全部"） */
function describeScope(deptId, teamId) {
  const tId = Number(teamId)
  const dId = Number(deptId)
  if (teamId && cache.teamMap[tId]) {
    const t = cache.teamMap[tId]
    const d = cache.deptMap[t.department_id]
    return `${d ? d.name + ' / ' : ''}${t.name}`
  }
  if (deptId && cache.deptMap[dId]) return cache.deptMap[dId].name
  return '全部'
}

/**
 * 初始化一对部门/团队级联下拉状态（每对独立管理）
 * - departments: 部门选项; teamsOfDept: 当前部门下的团队
 * - 部门变更时需调用 onDeptChange 清空团队选中值（与原 _fillTeamSelect 回退逻辑一致）
 * - scopeText: 组织范围描述,用于筛选 summary 文案
 */
export function useOrgFilter() {
  const deptId = ref('')
  const teamId = ref('')

  const departments = computed(() => cache.depts)
  const teamsOfDept = computed(() => {
    if (!deptId.value) return []
    // 再次过滤脏数据：防止极端情况下数组混入 undefined 项导致渲染期 v-for 崩溃
    return (cache.teamsByDept[Number(deptId.value)] || []).filter(t => t && t.id != null)
  })

  function onDeptChange() {
    teamId.value = ''
  }

  const scopeText = computed(() => describeScope(deptId.value, teamId.value))

  // 首次使用即触发加载（并发场景复用同一个 Promise,不重复请求）
  load()

  // 用 reactive 包裹返回：模板中 org.departments / org.teamsOfDept 等属性
  // 访问时会被自动解包为真实数组,否则 v-for 会迭代到 ComputedRefImpl 内部字段
  // （_value 未求值时为 undefined,读取其 .id 直接抛 TypeError）
  return reactive({ deptId, teamId, departments, teamsOfDept, onDeptChange, scopeText })
}
