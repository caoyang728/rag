/**
 * 质量评估公共常量与工具（原 admin-eval.js 中跨 Tab 共享的部分）
 * 各面板组件从这里引用,避免重复定义导致口径不一致
 */

// 0-1 分值 → 百分比展示（-- 表示缺失）；统一复用 utils/format 的 fmtPct（默认 1 位小数）
export { fmtPct } from '../../utils/format'

// 0-1 分值 → el-tag type（对应旧 score-pill 的高/中/低三档着色）
export function scoreTagType(score) {
  if (score >= 0.8) return 'success'
  if (score >= 0.6) return 'warning'
  return 'danger'
}

// 0-1 分值 → KPI 数值的语义色类（null/undefined 不渲染颜色）
export function kpiClass(score) {
  if (score === null || score === undefined) return ''
  if (score >= 0.8) return 'val-good'
  if (score >= 0.6) return 'val-mid'
  return 'val-poor'
}

// 0-100 分制（文档质量分）→ el-tag type
export function qualityTagType(score) {
  if (score >= 85) return 'success'
  if (score >= 70) return 'warning'
  return 'danger'
}

// 维度中文名 + 4 大类分组（与后端 _DIMENSION_GROUPS 保持一致）
export const DIM_LABEL = {
  faithfulness: '忠实度', hallucination: '幻觉', answer_relevancy: '回答相关性',
  context_relevancy: '检索相关性', toxicity: '毒性', bias: '偏见',
  completeness: '完整性', conciseness: '简洁性', clarity: '清晰度',
  professionalism: '专业性', helpfulness: '有用性', actionability: '可操作性',
}

export const DIM_GROUPS = {
  retrieval: { label: '检索质量', dims: ['context_relevancy'] },
  quality: { label: '答案质量', dims: ['faithfulness', 'hallucination', 'answer_relevancy', 'completeness', 'conciseness', 'clarity'] },
  safety: { label: '安全性', dims: ['toxicity', 'bias'] },
  business: { label: '业务体验', dims: ['professionalism', 'helpfulness', 'actionability'] },
}

// 所有 12 维按分组顺序展开（雷达图用）
export const ALL_DIMS_ORDERED = Object.values(DIM_GROUPS).flatMap(g => g.dims)

// 归因分类 → 中文标签（与后端 LowScoreAnalysis.CATEGORY_CHOICES 对齐）
export const ATTR_CATEGORY_LABEL = {
  retrieval_recall: '检索召回不足',
  retrieval_rank: '检索排序失效',
  content_gap: '知识盲区',
  content_quality: '内容质量差',
  generation_hallucination: '生成幻觉',
  generation_offtopic: '生成跑题',
  generation_incomplete: '生成不完整',
  generation_format: '生成表达差',
  safety: '安全问题',
  question_side: '问题侧',
  unknown: '无法归因',
}

// 影响层级 → 中文标签（与后端 LowScoreAnalysis.LAYER_CHOICES 对齐）
export const ATTR_LAYER_LABEL = {
  retrieval: '检索层',
  content: '内容层',
  generation: '生成层',
  safety: '安全层',
  system: '系统层',
  question: '问题侧',
  unknown: '未知',
}

// 路由层级固定顺序与后端 ROUTE_ORDER 对齐,前端据此渲染,缺失的层补 0
export const ROUTE_ORDER = ['wiki', 'graphrag_local', 'graphrag_global', 'rag']
export const ROUTE_LABEL = {
  wiki: 'Wiki 直答',
  graphrag_local: 'GraphRAG 局部',
  graphrag_global: 'GraphRAG 全局',
  rag: 'RAG 兜底',
}
// 每层固定配色（堆叠条/命中分布用）,顺序与 ROUTE_ORDER 一致
export const ROUTE_COLOR = ['#10b981', '#3b82f6', '#8b5cf6', '#f59e0b']

// Wiki 评估维度中文名
export const WIKI_DIM_LABEL = { faithfulness: '忠实度', completeness: '完整性' }

// 测试集状态 → 中文显示（白名单映射,防止 status 字段注入）
export function statusLabel(s) {
  return { draft: '草稿', active: '已启用', archived: '已归档' }[s] || s
}

// 测试集状态 → el-tag type
export function statusTagType(s) {
  return { draft: 'info', active: 'success', archived: 'warning' }[s] || 'info'
}

// 领域显示名映射:'all' 显示为"全部领域",其余原样返回
export function rootTypeLabel(rt) {
  return rt === 'all' ? '全部领域' : (rt || '-')
}
