// Chat 页面专用渲染与来源卡片构建纯函数（自 Chat.vue 抽出）
// 说明：均为纯函数，不依赖组件响应式状态；引用方需显式传入所需上下文
// （如 llmEnabled），便于复用与单测

import { escapeHtml } from './format'
import hljs from 'highlight.js'

/* ==========================================================
   溯源来源构建（由 buildSourceHtml 迁移为响应式数据结构）
   ========================================================== */
// 文档引用卡片：徽标 + 可点击标题（有 document_id 时跳转预览并定位页码）
export function buildDocSourceCardData(c, routeSource) {
  let badge = '文档'
  if (routeSource === 'wiki') badge = '文档 · Wiki'
  else if (routeSource && String(routeSource).startsWith('graphrag')) badge = '文档 · 图谱'
  const docId = c.document_id || c.doc_id
  const meta = []
  if (c.section) meta.push({ text: '章节: ' + c.section })
  if (c.page && Array.isArray(c.page) && c.page.length) {
    meta.push({ text: '页码: ' + c.page.map(p => 'P' + p).join(', ') })
  }
  if (c.chunk_ids && c.chunk_ids.length > 0) {
    meta.push({ text: '引用 ' + c.chunk_ids.length + ' 处' })
  }
  return {
    type: 'doc', badge, title: c.doc_title || '未知文档',
    docId, page: (Array.isArray(c.page) && c.page[0]) || 1,
    chunkIds: Array.isArray(c.chunk_ids) ? c.chunk_ids : [],
    meta,
    clickable: !!docId,   // 历史数据缺失 document_id → 降级为不可点击纯文本
    sql: null, sqlOpen: false,
  }
}

// 数据库来源卡片：表名 + 行数 + 可展开的 SQL（便于复核查询语句）
export function buildDbSourceCardData(t) {
  const sql = extractToolSql(t)
  const meta = []
  const rows = extractRowsFromResult(t)
  if (rows != null) meta.push({ text: '查询到 ' + rows + ' 行' })
  return {
    type: 'db', badge: '数据库', title: getDbTableName(t) + ' 表',
    docId: null, page: 1, chunkIds: [], meta,
    clickable: false, sql, sqlOpen: false,
  }
}

// 网络来源卡片：徽标 + 搜索关键词
export function buildWebSourceCardData(t) {
  const args = t.tool_args || {}
  return {
    type: 'web', badge: '网络', title: '联网搜索',
    docId: null, page: 1, chunkIds: [], meta: [{ text: '关键词: ' + (args.query || '-') }],
    clickable: false, sql: null, sqlOpen: false,
  }
}

// 构建来源标签行（明确标识回答数据来源，置于溯源卡片之前）
// llmEnabled：当前是否开启了 LLM 来源，用于"大模型知识"标签的兜底判定
export function buildSourceTags(docCount, dbTraces, webTraces, answerType, llmEnabled) {
  const tags = []
  if (docCount > 0) tags.push({ text: '📄 内部文档 · ' + docCount + ' 篇', cls: 'tag-doc' })
  dbTraces.forEach(t => { tags.push({ text: '🗄️ 数据库 · ' + getDbTableName(t), cls: 'tag-db' }) })
  if (webTraces.length > 0) tags.push({ text: '🌐 联网搜索', cls: 'tag-web' })
  // 无任何检索/工具来源：仅当后端明确标记为 LLM 直接作答（general）且前端开启了
  // LLM 来源时才视为大模型知识；拒答（agent 工具检索无果）不标识
  const llmLike = answerType === 'general' && llmEnabled
  if (tags.length === 0 && llmLike) tags.push({ text: '🤖 大模型知识 · 仅供参考', cls: 'tag-llm' })
  return tags
}

/* 构建溯源来源数据（由 buildSourceHtml 迁移）
 * 返回 { cards, tags, ready }：ready 表示来源区是否可渲染
 * （start 阶段 answer_type 未知时先不渲染，等 done 事件再补充，避免工具执行中闪烁） */
export function buildSourceData(citations, routeSource, toolTraces, isPending, answerType, llmEnabled) {
  const cards = []
  const traces = Array.isArray(toolTraces) ? toolTraces : []
  const docList = Array.isArray(citations) ? citations : []
  // 数据库/网络来源：仅统计执行成功的工具调用，失败的不作为回答来源
  const dbTraces = traces.filter(t => t.tool_name === 'text2sql' && t.ok !== false && t.result_ok !== false)
  const webTraces = traces.filter(t => t.tool_name === 'web_search' && t.ok !== false && t.result_ok !== false)

  docList.forEach(c => cards.push(buildDocSourceCardData(c, routeSource)))
  dbTraces.forEach(t => { const card = buildDbSourceCardData(t); if (card) cards.push(card) })
  webTraces.forEach(t => cards.push(buildWebSourceCardData(t)))

  // 拒答：答案文本已说明原因（未找到资料/来源已关闭），不渲染来源标识
  if (answerType === 'refused' || answerType === 'blocked') return { cards: [], tags: [], ready: true }

  // 来源标签行：start 阶段（answerType=null）不渲染，等 done 事件携带 answer_type 再统一补充
  const tagsReady = answerType !== null
  const tags = tagsReady ? buildSourceTags(docList.length, dbTraces, webTraces, answerType, llmEnabled) : []

  // 无任何引用/工具调用：Agent 工具未执行完（isPending）先留空，避免闪烁
  if (cards.length === 0) {
    if (isPending) return { cards: [], tags: [], ready: false }
    return { cards: [], tags, ready: true }
  }
  return { cards, tags, ready: true }
}

// 提取 text2sql 执行的 SQL：流式 tool_traces 带 meta.sql，历史记录从结果文本 "SQL: ..." 解析
export function extractToolSql(t) {
  const meta = t.meta || {}
  if (meta.sql) return String(meta.sql).trim()
  const text = t.result || t.tool_result || ''
  const m = String(text).match(/^SQL:\s*([\s\S]*?)(?:\n\s*\n|$)/)
  return m ? m[1].trim() : ''
}

// 从 SQL 的 FROM/JOIN 子句提取表名（去重；带 schema 前缀时只取表名）
export function extractTablesFromSql(sql) {
  const tables = []
  const re = /\b(?:FROM|JOIN)\s+([A-Za-z0-9_."]+)/gi
  let m
  while ((m = re.exec(sql)) !== null) {
    const name = m[1].replace(/["']/g, '').split('.').pop()
    if (name && !tables.includes(name)) tables.push(name)
  }
  return tables
}

// 提取数据库表名：优先工具参数 tables，缺失时从 SQL 的 FROM/JOIN 提取
export function getDbTableName(t) {
  const args = t.tool_args || {}
  const sql = extractToolSql(t)
  let tables = Array.isArray(args.tables) ? args.tables.slice() : []
  if (tables.length === 0 && sql) tables = extractTablesFromSql(sql)
  return tables.length > 0 ? tables.join(' / ') : '业务数据库'
}

// 提取查询返回行数：优先 meta.rows，历史记录从结果文本 "共 N 行" 解析
export function extractRowsFromResult(t) {
  const meta = t.meta || {}
  if (meta.rows != null) return meta.rows
  const text = t.result || t.tool_result || ''
  const m = String(text).match(/共\s*(\d+)\s*行/)
  return m ? parseInt(m[1], 10) : null
}

/* ==========================================================
   行内 Markdown 渲染（输入须为已转义文本，防止 XSS）
   ========================================================== */
// 把 [n] 引用标记渲染为来源上标：仅当 n 对应实际引用序号时才转换
export function renderCiteSup(text, citeIdx) {
  if (!citeIdx || citeIdx.size === 0) return text
  return text.replace(/\[(\d+)\]/g, (m, num) => {
    return citeIdx.has(parseInt(num, 10)) ? '<sup class="cite-ref">[' + num + ']</sup>' : m
  })
}

// 行内 Markdown：加粗/斜体/行内代码/链接（链接仅放行安全协议，拦截 javascript: 等）
export function renderInline(text) {
  let s = text
  // 加粗占位（占位符为控制字符 + 序号，不会被后续正则匹配）
  const strongs = []
  s = s.replace(/\*\*([^*\n]+)\*\*/g, (m, inner) => {
    strongs.push(inner)
    return '\u0000S' + (strongs.length - 1) + '\u0000'
  })
  // 斜体：* 包裹，前后为行首/空白/中英文括号/标点（不误伤加粗与乘法）
  s = s.replace(/(^|[\s(（【《『])[*]([^*\n][^*\n]*?)[*](?=$|[\s,.;:!?！？。，、)）】》』])/g, '$1<em>$2</em>')
  // 行内代码
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>')
  // 链接（仅安全协议）
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s()<>]+|mailto:[^\s()<>]+|#[^\s()<>]*)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
  // 还原加粗
  strongs.forEach((inner, i) => {
    s = s.replace('\u0000S' + i + '\u0000', '<strong>' + inner + '</strong>')
  })
  return s
}

// 工具结果文本按 Markdown 渲染（表格/加粗/代码等美化），无引用来源
export function formatToolResult(text) {
  if (!text) return ''
  return formatAnswer(text, [])
}

// 回答全文 Markdown 渲染（表格聚合 / 代码块 / 列表 / 引用等），输入原始文本，内部转义防 XSS
export function formatAnswer(text, citations) {
  if (!text) return '<p>暂无回答</p>'
  // 引用序号集合：正文 [n] 上标只对实际存在的引用生效
  const citeIdx = new Set()
  ;(Array.isArray(citations) ? citations : []).forEach(c => {
    if (c && c.index) citeIdx.add(Number(c.index))
  })

  const lines = text.split('\n')
  const result = []
  let inCodeBlock = false
  let inList = false
  let inTable = false
  let tableRows = []
  let codeLang = ''
  let codeBuf = []       // 代码块内容缓冲，闭合时一次性高亮

  // 表格行判定：以 | 开头且至少含 2 个 |（Markdown 表格，LLM/数据库工具常用）
  const isTableRow = (line) => /^\s*\|/.test(line) && line.indexOf('|') !== line.lastIndexOf('|')
  // 表头分隔行判定：整行由 | 与 - / : 组成
  const isSepRow = (row) => /^[\s|:-]+$/.test(row) && row.includes('-')
  const splitCells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|')
  // 行内输出管线：转义 → 行内 Markdown → 引用上标（防 XSS）
  const inline = (raw) => renderCiteSup(renderInline(escapeHtml(raw.trim())), citeIdx)

  // 聚合的表格行渲染为 <table>：首行为表头（分隔行跳过），其余为数据行
  const flushTable = () => {
    if (!tableRows.length) return
    const sep = tableRows.length > 1 && isSepRow(tableRows[1])
    const headerCells = splitCells(tableRows[0])
    const dataRows = sep ? tableRows.slice(2) : tableRows.slice(1)
    let html = '<div class="md-table-wrap"><table><thead><tr>'
    html += headerCells.map(c => '<th>' + inline(c) + '</th>').join('')
    html += '</tr></thead>'
    if (dataRows.length) {
      html += '<tbody>'
      dataRows.forEach(r => {
        html += '<tr>' + splitCells(r).map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>'
      })
      html += '</tbody>'
    }
    html += '</table></div>'
    result.push(html)
    tableRows = []
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith('```')) {
      if (inTable) { flushTable(); inTable = false }
      if (inList) { result.push('</ul>'); inList = false }
      if (!inCodeBlock) {
        codeLang = line.slice(3).trim().replace(/[^a-zA-Z0-9_-]/g, '')
        codeBuf = []
        inCodeBlock = true
      } else {
        // 代码块闭合：将收集的行拼接后用 highlight.js 高亮，保留语言 class
        const code = codeBuf.join('\n')
        const language = codeLang && hljs.getLanguage(codeLang) ? codeLang : null
        const highlighted = language
          ? hljs.highlight(code, { language }).value
          : hljs.highlightAuto(code).value
        const langClass = language ? ' class="language-' + language + '"' : ''
        result.push('<pre class="md-code"><code' + langClass + '>' + highlighted + '</code></pre>')
        inCodeBlock = false
        codeLang = ''
        codeBuf = []
      }
      continue
    }
    if (inCodeBlock) {
      codeBuf.push(line)
      continue
    }
    if (isTableRow(line)) {
      if (!inTable) {
        if (inList) { result.push('</ul>'); inList = false }
        inTable = true
        tableRows = []
      }
      tableRows.push(line.trim())
      continue
    }
    if (inTable) { flushTable(); inTable = false }

    if (line.startsWith('### ')) {
      if (inList) { result.push('</ul>'); inList = false }
      result.push('<h5>' + inline(line.slice(4)) + '</h5>')
      continue
    }
    if (line.startsWith('## ')) {
      if (inList) { result.push('</ul>'); inList = false }
      result.push('<h4>' + inline(line.slice(3)) + '</h4>')
      continue
    }
    if (line.startsWith('# ')) {
      if (inList) { result.push('</ul>'); inList = false }
      result.push('<h3>' + inline(line.slice(2)) + '</h3>')
      continue
    }
    if (line.startsWith('> ')) {
      if (inList) { result.push('</ul>'); inList = false }
      result.push('<blockquote>' + inline(line.slice(2)) + '</blockquote>')
      continue
    }
    if (/^---+$/.test(line.trim())) {
      if (inList) { result.push('</ul>'); inList = false }
      result.push('<hr>')
      continue
    }
    if (line.startsWith('- ') || line.startsWith('* ') || line.match(/^\d+\./)) {
      if (!inList) { result.push('<ul>'); inList = true }
      const content = line.replace(/^(- |\* |\d+\.\s*)/, '')
      result.push('<li>' + inline(content) + '</li>')
      continue
    }
    if (inList) { result.push('</ul>'); inList = false }
    if (line.trim()) {
      result.push('<p>' + inline(line) + '</p>')
    }
  }

  if (inTable) flushTable()
  if (inList) result.push('</ul>')
  // 未闭合的代码块：同样用 hljs 高亮后输出
  if (inCodeBlock) {
    const code = codeBuf.join('\n')
    const language = codeLang && hljs.getLanguage(codeLang) ? codeLang : null
    const highlighted = language
      ? hljs.highlight(code, { language }).value
      : hljs.highlightAuto(code).value
    const langClass = language ? ' class="language-' + language + '"' : ''
    result.push('<pre class="md-code"><code' + langClass + '>' + highlighted + '</code></pre>')
  }
  return result.join('\n')
}

// 格式化工具参数为可读字符串（对象 JSON 序列化并限制长度）
export function formatToolArgs(args) {
  if (args == null) return ''
  if (typeof args === 'string') return args.length > 200 ? args.slice(0, 200) + '...' : args
  try {
    const json = JSON.stringify(args, null, 2)
    return json.length > 500 ? json.slice(0, 500) + '\n...' : json
  } catch (e) {
    return String(args)
  }
}

/* ==========================================================
   多 Agent 工作流辅助
   ========================================================== */
// 节点类型图标：research=子Agent / tool=工具 / approval=人工确认 / finalize=汇总
export function wfStepIcon(stepType) {
  return { research: '🔬', tool: '🔧', approval: '👤', finalize: '📝' }[stepType] || '•'
}

// 工作流整体状态中文文案
export function workflowStatusText(status) {
  return {
    running: '执行中', succeeded: '✅ 已完成', degraded: '⚠️ 降级完成',
    failed: '❌ 执行失败', waiting_approval: '⏸ 等待人工确认'
  }[status] || status
}

// 节点卡片状态文案
export function wfNodeStatusText(status) {
  return {
    running: '执行中', succeeded: '成功', failed: '失败',
    blocked: '等待确认', pending: '待执行',
    approved: '已批准', rejected: '已拒绝', skipped: '已跳过'
  }[status] || status
}
