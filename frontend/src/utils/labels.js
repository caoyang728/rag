// 状态/枚举 → 中文文案 + el-tag type 的公共映射工具
// 业务背景：多个页面需要"枚举值 → 展示文案/标签色"的映射，早期各页面各自维护
// 一份同构的 MAP + label()/tagType() 函数对，口径容易漂移，统一收敛到这里。

/**
 * 由 labelMap/tagMap 生成取数函数对，替代各页面重复的 `MAP[v] || fallback` 写法
 * @param {Record<string,string>} labelMap 枚举 → 中文文案
 * @param {Record<string,string>} tagMap 枚举 → el-tag type
 * @param {{ labelFallback?: string, tagFallback?: string }} [opts] 取不到映射时的兜底值
 * @returns {{ label: (v:any)=>string, tagType: (v:any)=>string }}
 */
export function makeStatusMeta(labelMap, tagMap, opts = {}) {
  const { labelFallback = '—', tagFallback = 'info' } = opts
  return {
    label: v => labelMap[v] || v || labelFallback,
    tagType: v => tagMap[v] || tagFallback,
  }
}

// 工单主表状态（大写枚举）→ 文案 / el-tag type
// Ticket 审批视角与 TicketCenter 共用同一份口径，避免两处漂移
export const TICKET_STATUS_LABEL_MAP = {
  PENDING: '待审批', APPROVED: '已通过', EXECUTED: '已执行', REJECTED: '已驳回', CANCELLED: '已撤回',
}
export const TICKET_STATUS_TAG_MAP = {
  PENDING: 'warning', APPROVED: 'primary', EXECUTED: 'success', REJECTED: 'danger', CANCELLED: 'info',
}

// 可见范围 → el-tag type / 中文文案（团队/部门/公开），Upload 与 AdminNodes 共用同一口径
export function visTagType(v) {
  const tagMap = { team: 'info', dept: 'primary', public: 'success' }
  return tagMap[v] || 'info'
}

export function visTagText(v) {
  const map = { team: '团队', dept: '部门', public: '公开' }
  return map[v] || v
}

// 文档类型枚举（后端 file_type 字段）→ emoji 图标，Upload 与 AdminNodes 共用
export function fileTypeIcon(t) {
  const map = { pdf: '📕', docx: '📄', markdown: '📝', txt: '📃', code: '💻', config: '⚙️', other: '📄' }
  return map[t] || '📄'
}
