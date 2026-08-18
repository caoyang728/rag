/**
 * Markdown 渲染与代码高亮工具（基于 marked + highlight.js）
 * 替换各组件中的自实现解析器，统一 Markdown 渲染与语法高亮逻辑。
 */
import { marked } from 'marked'
import hljs from 'highlight.js'

// 白名单链接协议：仅允许 http/https/mailto 与站内路径，拦截 javascript: 等危险协议
function safeLink(url) {
  if (!url) return ''
  if (/^(https?:\/\/|mailto:|\/|\.\/|\.\.\/|#)/i.test(url)) return url
  return ''
}

// 自定义渲染器：链接协议白名单 + 代码块/行内代码高亮
const renderer = {
  // 链接：仅放行安全协议，保留原有防 XSS 语义
  // 内部链接（非 http/mailto）添加 wiki-md-ref class，供 Wiki.vue onMdLinkClick 委托拦截
  // 打开文档预览弹窗，而非浏览器默认跳转
  link({ href, text, title }) {
    const safe = safeLink(href)
    const titleAttr = title ? ` title="${title}"` : ''
    if (!safe) return text
    if (/^https?:\/\//.test(safe)) {
      return `<a href="${safe}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`
    }
    // 内部/锚点/相对路径：标记为 wiki-md-ref，由父级 @click 委托处理
    return `<a class="wiki-md-ref" href="#" role="button"${titleAttr}>${text}</a>`
  },

  // 图片：同样走白名单
  image({ href, title, text }) {
    const safe = safeLink(href)
    if (!safe) return text || ''
    return `<img src="${safe}" alt="${text || ''}"${title ? ` title="${title}"` : ''} loading="lazy">`
  },

  // 围栏代码块：使用 highlight.js 高亮，保留语言 class
  code({ text, lang }) {
    const language = lang && hljs.getLanguage(lang) ? lang : null
    const highlighted = language
      ? hljs.highlight(text, { language }).value
      : hljs.highlightAuto(text).value
    const langClass = language ? ` class="language-${language}"` : ''
    return `<pre class="md-code"><code${langClass}>${highlighted}</code></pre>`
  },

  // 行内代码：保持原有 class 名，与现有 CSS 兼容
  codespan({ text }) {
    return `<code class="md-inline-code">${text}</code>`
  },
}

// 配置 marked：启用 GFM + 自定义渲染器 + 代码高亮
marked.setOptions({
  gfm: true,
  breaks: false,
})

marked.use({ renderer })

/**
 * Markdown → HTML（用于 Wiki 详情页等场景）
 * @param {string} src - 原始 Markdown 文本
 * @returns {string} 渲染后的 HTML（已做 XSS 防护）
 */
export function renderMarkdown(src = '') {
  if (!src) return ''
  return marked.parse(src)
}

/**
 * 单行代码语法高亮（用于文档预览行模式）
 * @param {string} code - 单行代码文本
 * @param {string} lang - 编程语言标识（如 python、javascript）
 * @returns {string} 高亮后的 HTML 片段
 */
export function highlightCode(code, lang) {
  if (!code) return ''
  if (lang && hljs.getLanguage(lang)) {
    return hljs.highlight(code, { language: lang }).value
  }
  return hljs.highlightAuto(code).value
}
