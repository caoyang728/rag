// 通用工具函数（由原 common.js 纯函数迁移，供各页面复用）

export function escapeHtml(s) {
  if (s == null) return ''
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}

export function formatDate(dt) {
  if (!dt) return '-'
  const d = new Date(dt)
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') +
    ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0')
}

// 短日期格式（YYYY-MM-DD）：版本历史等紧凑表格场景使用
export function formatDateShort(dt) {
  if (!dt) return '-'
  const d = new Date(dt)
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0')
}

export function formatFileSize(bytes) {
  if (bytes == null || isNaN(bytes)) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}

export function errMsg(err, fallback) {
  try {
    if (typeof err === 'string') return err
    if (err?.detail) return err.detail
    if (err?.message) return err.message
    if (err?.error) return err.error
  } catch { /* 忽略 */ }
  return fallback
}

// 可预览文件类型：文本/代码走行模式，PDF/Office 走页图模式（未知二进制类型除外）
export function isPreviewableFileType(fileType) {
  return ['markdown', 'txt', 'code', 'config', 'pdf', 'docx', 'spreadsheet', 'presentation'].includes(fileType)
}

/**
 * 文档上传流水线合并状态：主解析状态 + 图谱 + Wiki
 * 优先级：解析失败 > 向量失败 > 主状态 > 图谱/wiki 失败 > 构建中 > 等待构建 > 未启用 > 已完成
 * @returns {[type: 'danger'|'warning'|'success'|'default', text: string]}
 */
export function pipelineStatus(doc) {
  const s = doc?.status
  if (s === 'failed') return ['danger', '解析失败']
  if (s === 'embedding_failed') return ['danger', '向量构建失败']
  const main = {
    pending: ['default', '等待解析'],
    parsing: ['warning', '解析中'],
    desensitizing: ['warning', '解析中'],   // 脱敏并入解析阶段展示
    chunking: ['warning', '切片中'],
    embedding: ['warning', '向量构建中']
  }[s]
  if (main) return main
  if (s !== 'done') return ['default', s || '未知']

  // 解析完成：合并图谱/wiki 阶段状态
  const g = doc.graph_status || 'pending'
  const w = doc.wiki_status || 'pending'
  if (g === 'failed') return ['danger', '图谱构建失败']
  if (w === 'failed') return ['danger', 'wiki 构建失败']
  if (g === 'extracting') return ['warning', '图谱构建中']
  if (w === 'extracting') return ['warning', 'wiki 构建中']
  if (g === 'pending') return ['default', '图谱等待构建']
  if (w === 'pending') return ['default', '等待构建 wiki']
  if (g === 'skipped' && w === 'done') return ['default', '图谱未启用']
  if (g === 'done' && w === 'skipped') return ['default', 'wiki 未启用']
  return ['success', '已完成']
}

/**
 * 校验 IP 模式是否合法（单 IP / CIDR / 通配符 / 范围）
 * @returns {{ valid: boolean, error?: string }}
 */
export function validateIpPattern(pattern) {
  if (!pattern || !pattern.trim()) return { valid: false, error: 'IP 不能为空' }
  pattern = pattern.trim()

  if (pattern.includes('/')) {
    const parts = pattern.split('/')
    if (parts.length !== 2) return { valid: false, error: 'CIDR 格式错误，示例：10.0.0.0/24' }
    if (!isValidIpv4(parts[0])) return { valid: false, error: 'CIDR 中的 IP 地址不合法' }
    const prefix = parseInt(parts[1], 10)
    if (isNaN(prefix) || prefix < 0 || prefix > 32) return { valid: false, error: 'CIDR 前缀长度需为 0~32' }
    return { valid: true }
  }

  if (pattern.includes('*')) {
    const parts = pattern.split('.')
    if (parts.length !== 4) return { valid: false, error: '通配符格式需为四段，示例：10.0.*.*' }
    for (const part of parts) {
      if (part === '*') continue
      const num = parseInt(part, 10)
      if (isNaN(num) || num < 0 || num > 255) return { valid: false, error: `通配符中的 "${part}" 不是合法的 0~255 数字` }
    }
    return { valid: true }
  }

  if (pattern.includes('-')) {
    const parts = pattern.split('-')
    if (parts.length !== 2) return { valid: false, error: '范围格式错误，示例：10.0.0.1-10.0.0.100' }
    const start = parts[0].trim()
    const end = parts[1].trim()
    if (!isValidIpv4(start)) return { valid: false, error: '范围起始 IP 不合法' }
    if (!isValidIpv4(end)) return { valid: false, error: '范围结束 IP 不合法' }
    if (ipv4ToNumber(start) > ipv4ToNumber(end)) return { valid: false, error: '范围起始 IP 不能大于结束 IP' }
    return { valid: true }
  }

  if (!isValidIpv4(pattern)) return { valid: false, error: 'IP 地址不合法，示例：10.0.0.1' }
  return { valid: true }
}

function isValidIpv4(ip) {
  const parts = ip.split('.')
  if (parts.length !== 4) return false
  return parts.every(p => {
    const n = parseInt(p, 10)
    return !isNaN(n) && n >= 0 && n <= 255 && String(n) === p
  })
}

function ipv4ToNumber(ip) {
  return ip.split('.').reduce((acc, octet) => (acc << 8) + parseInt(octet, 10), 0) >>> 0
}

// 耗时格式化：不足 1s 显示毫秒，否则显示秒（保留 2 位小数），任务耗时等场景复用
export function formatDuration(ms) {
  const v = Number(ms) || 0
  if (v < 1000) return v + ' ms'
  return (v / 1000).toFixed(2) + ' s'
}

// 会话时间展示：刚刚 / N 分钟前 / N 小时前 / M/D（会话侧边栏与历史消息时间复用）
export function formatSessionTime(dt) {
  if (!dt) return '-'
  const d = new Date(dt)
  const now = new Date()
  const diff = now.getTime() - d.getTime()
  if (diff < 60 * 1000) return '刚刚'
  if (diff < 60 * 60 * 1000) return Math.floor(diff / (60 * 1000)) + ' 分钟前'
  if (diff < 24 * 60 * 60 * 1000) return Math.floor(diff / (60 * 60 * 1000)) + ' 小时前'
  return (d.getMonth() + 1) + '/' + d.getDate()
}

// JSON 安全展示：对象/数组序列化美化展示，解析失败或非对象时原样返回，避免页面各自 try/catch
export function safeJson(value) {
  if (value == null) return '-'
  if (typeof value === 'string') return value
  try { return JSON.stringify(value, null, 2) } catch (e) { return String(value) }
}

// 字段值展示兜底：undefined 显示"—"、空字符串显示"（空）"，避免表格/详情中出现裸 undefined
export function displayValue(v) {
  if (v === undefined) return '—'
  if (v === '') return '（空）'
  return v
}

// 0~1 比例/分值 → 百分比文本；decimals 控制小数位，缺失值显示 missing（默认 '--'）。
// 收敛 eval 面板（1 位小数）与 analytics 面板（2 位小数/'-' 兜底）各自的百分比格式化实现
export function fmtPct(v, decimals = 1, missing = '--') {
  if (v === null || v === undefined || isNaN(v)) return missing
  return (Number(v) * 100).toFixed(decimals) + '%'
}
