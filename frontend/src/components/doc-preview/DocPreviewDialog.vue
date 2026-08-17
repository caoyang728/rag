<template>
  <!-- 文档预览弹窗（Chat / Upload 等页面共用，preview-doc.js 迁移）
       复用 BaseDialog 骨架：60% 视口宽 + 屏幕居中 + 轻微模糊遮罩；
       高度按 PPT 页（16:9）比例自适应：宽度 60vw × 9/16 = 33.75vw 为页图高，
       再加头部/元信息条/底部约 190px 骨架高，保证弹窗刚好容纳一页（不再固定 80vh）；
       :z-index 显式抬高层级，保证盖在文档列表等其他弹窗之上 -->
  <BaseDialog
    v-model="dialogVisible"
    class="doc-preview-dialog"
    :title="previewTitle"
    width="60%"
    height="calc(33.75vw + 190px)"
    :z-index="3000"
    min-width="640px"
    @closed="onPreviewClosed"
  >
    <div class="doc-preview-body">
      <!-- 水印层（防截图泄密，不阻挡点击/滚动）：
           absolute 铺满 doc-preview-body（即弹窗 header 与 footer 之间的可视区域），
           放在 body 内部避免被 BaseDialog 的 body 溢出裁剪影响 -->
      <div
        v-if="previewWatermark"
        class="doc-preview-watermark"
        :style="{ backgroundImage: watermarkBg }"
        aria-hidden="true"
      ></div>

      <!-- 元信息条 -->
      <div v-if="previewMeta" class="doc-preview-meta">
        <span class="doc-preview-meta-item">📄 {{ previewMeta.file_name || '' }}</span>
        <span class="doc-preview-meta-item">类型：{{ previewMeta.file_type || '-' }}</span>
        <span class="doc-preview-meta-item">大小：{{ formatFileSize(previewMeta.file_size) }}</span>
        <span class="doc-preview-meta-item">版本：{{ previewMeta.version_tag || '-' }}</span>
        <span class="doc-preview-meta-item">上传人：{{ previewMeta.owner_name || '-' }}</span>
        <span class="doc-preview-meta-item">时间：{{ formatDate(previewMeta.created_at) }}</span>
        <span class="doc-preview-meta-item">可见：{{ previewVisibleLabel }}</span>
      </div>

      <!-- 加载中 -->
      <div v-if="previewLoading" class="doc-preview-loading">
        <el-icon class="is-loading" :size="22"><Loading /></el-icon>
        <div class="doc-preview-loading-text">加载中...</div>
      </div>

      <!-- 降级提示 + 原文不可用 -->
      <template v-else-if="previewError">
        <div class="doc-preview-disabled">
          <div class="doc-preview-disabled-icon">📭</div>
          <div>{{ previewError }}</div>
          <el-button v-if="previewMeta && previewMeta.can_download" size="small" style="margin-top:14px" @click="downloadDoc(previewDocId)">
            ⬇ 下载原文
          </el-button>
        </div>
      </template>

      <!-- 行模式（code 高亮 / text 纯文本，带行号） -->
      <template v-else-if="previewState && previewState.mode !== 'image'">
        <div v-if="previewState.fallbackNotice" class="doc-preview-notice">{{ previewState.fallbackNotice }}</div>
        <el-scrollbar
          ref="previewScrollRef"
          class="doc-preview-scroll"
          @scroll="onPreviewScroll"
        >
          <div class="doc-preview-code" :class="{ 'text-flow': previewState.mode !== 'code' }">
            <div
              v-for="line in previewLines"
              :key="line.no"
              class="doc-preview-code-row"
              :data-line="line.no"
            >
              <span class="doc-preview-code-ln">{{ line.no }}</span>
              <code v-if="line.html !== null" class="doc-preview-code-txt" v-html="line.html"></code>
              <code v-else class="doc-preview-code-txt">{{ line.text }}</code>
            </div>
            <div v-if="previewChunkError" class="doc-preview-sentinel" @click="loadMoreLines">加载失败，点击重试</div>
            <div v-else-if="previewState && !previewState.allLoaded" class="doc-preview-sentinel">
              {{ previewState.loading ? '加载中...' : '已加载 ' + previewState.loadedLines.length.toLocaleString() + ' / ' + previewState.totalLines.toLocaleString() + ' 行，继续滚动加载...' }}
            </div>
          </div>
        </el-scrollbar>
      </template>

      <!-- 页图模式（PDF/Office，按页切换）：边框挂在滚动容器上，图片内不留白 -->
      <template v-else-if="previewState && previewState.mode === 'image'">
        <el-scrollbar ref="previewImgScrollRef" class="doc-preview-scroll preview-img-scroll">
          <template v-if="previewState.totalPages <= 2">
            <div v-for="p in previewAllPages" :key="p.page" class="doc-preview-image-wrap">
              <img v-if="p.src" :src="p.src" class="doc-preview-image" :alt="'第 ' + p.page + ' 页'" />
              <div v-else class="doc-preview-loading">
                <el-icon class="is-loading"><Loading /></el-icon> 加载中...
              </div>
            </div>
          </template>
          <template v-else>
            <div class="doc-preview-image-wrap">
              <img v-if="previewPageSrc" :src="previewPageSrc" class="doc-preview-image" :alt="'第 ' + previewPage + ' 页'" />
              <div v-else class="doc-preview-loading">
                <el-icon class="is-loading"><Loading /></el-icon> 加载中...
              </div>
            </div>
          </template>
        </el-scrollbar>
      </template>
    </div>

    <template #footer>
      <div class="doc-preview-footer">
        <span class="doc-preview-info">{{ previewInfoText }}</span>
        <div v-if="previewState && previewState.mode === 'image' && previewState.totalPages > 2" class="doc-preview-pager">
          <el-button size="small" :disabled="previewPage <= 1" @click="switchImagePage(previewPage - 1)">‹ 上一页</el-button>
          <span class="doc-preview-page">第 {{ previewPage }} / {{ previewState.totalPages }} 页</span>
          <el-button size="small" :disabled="previewPage >= previewState.totalPages" @click="switchImagePage(previewPage + 1)">下一页 ›</el-button>
        </div>
        <el-button size="small" @click="dialogVisible = false">关闭</el-button>
      </div>
    </template>
  </BaseDialog>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import api from '../../api/http'
import { escapeHtml, formatDate, formatFileSize } from '../../utils/format'
import { downloadBlob } from '../../utils/download'
import { useDocWatermark } from '../../composables/useDocWatermark'
import { getToken } from '../../utils/authStorage'
import BaseDialog from '../base/BaseDialog.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false }, // v-model 控制弹窗显隐
  docId: { type: [Number, String], default: null }, // 要预览的文档 ID
  initialPage: { type: Number, default: 1 },       // 打开时定位页（image 为页号，行模式换算为行号）
})
const emit = defineEmits(['update:modelValue'])

const { previewWatermark, watermarkFont, refreshWatermark } = useDocWatermark()

// 弹窗显隐：读写 props.modelValue（父组件 v-model 控制）
const dialogVisible = computed({
  get: () => props.modelValue,
  set: v => emit('update:modelValue', v),
})
// 本次预览会话是否已打开（打开时执行一次状态重置/水印刷新/元信息补取）
const previewSessionActive = ref(false)

/* ==========================================================
   文档预览逻辑（preview-doc.js 迁移）
   - 行模式（code 高亮 / text 纯文本，带行号，滚动触底按 500 行/块追加）
   - 页图模式（PDF/Office，按 PDF 页切换，fetch + Blob URL 携带 JWT）
   ========================================================== */
const PREVIEW_JUMP_PAGE_LINES = 500   // 跳页换算粒度（与后端 _PREVIEW_CHUNK_LINES 一致）
const PREVIEW_CACHE_TTL = 10 * 60 * 1000  // 会话级预览缓存 TTL 10 分钟

const previewDocId = ref(null)        // 当前预览文档 ID（用于下载）
const previewTargetId = ref(null)     // 当前预览请求对应的文档 ID（丢弃异步过期响应，防快速切换错位）
const previewPage = ref(1)            // image 模式当前 PDF 页
const previewMeta = ref(null)         // 当前预览文档元信息
const previewState = ref(null)        // 预览形态状态（由 preview 接口返回后写入）
const previewLines = ref([])          // 行模式行数据 [{no, text, html}]
const previewLoading = ref(false)
const previewError = ref(null)        // 原文不可用提示（含下载入口）
const previewChunkError = ref(false)  // 分块加载失败标记（可点击重试）
const previewScrollRef = ref(null)    // 行模式滚动容器
const previewImgScrollRef = ref(null) // 页图模式滚动容器（翻页后需回到页顶）

// 会话级预览缓存（仅缓存 whole 全文 / image 页图信息，分块模式重开时重新拉首块）
const previewCache = new Map()
// 页图 Blob URL 缓存（key: '{docId}:{page}'），翻页时避免重复请求
const previewPageImgCache = reactive({})
// 页图加载中标记（key: '{docId}:{page}' → true），防同一页重复请求
const previewPageInflight = {}

function previewCacheGet(id) {
  const item = previewCache.get(id)
  if (!item) return null
  if (Date.now() - item.ts > PREVIEW_CACHE_TTL) {
    previewCache.delete(id)
    return null
  }
  return item
}

function previewCacheSet(id, state) {
  previewCache.set(id, {
    id, mode: state.mode, whole: state.whole, language: state.language,
    pageUrl: state.pageUrl, totalPages: state.totalPages, formatLabel: state.formatLabel,
    fallbackNotice: state.fallbackNotice, fileName: state.fileName,
    totalLines: state.totalLines, pageSizeLines: state.pageSizeLines,
    startLine: state.startLine, currentPage: state.currentPage,
    loadedLines: state.loadedLines, allLoaded: state.allLoaded,
    ts: Date.now(),
  })
  // 防内存膨胀：超过 30 个文档时清掉最旧的一半
  if (previewCache.size > 30) {
    Array.from(previewCache.keys()).slice(0, 15).forEach(k => previewCache.delete(k))
  }
}

// 预览水印文案初始化（逻辑已抽离到 useDocWatermark）：当前用户账号 + 打开时间（防截图泄密）
function initWatermark() {
  refreshWatermark()
}

// 自定义水印背景：SVG data URL，文字斜向平铺。
// 水印颜色统一使用 useDocWatermark 的字体颜色（暗色 rgba(255,255,255,0.06) /
// 浅色 rgba(0,0,0,0.05) 自适应）：word 页图与代码模式保持一致，不再单独加深
const watermarkBg = computed(() => {
  const text = previewWatermark.value
  if (!text) return 'none'
  const f = watermarkFont.value || {}
  const color = f.color || '#999'
  const size = 200
  const cx = size / 2
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">` +
    `<text x="${cx}" y="${cx}" fill="${color}" font-size="${f.fontSize || 14}" ` +
    `text-anchor="middle" dominant-baseline="middle" ` +
    `transform="rotate(${f.rotate || -20} ${cx} ${cx})" font-family="sans-serif">` +
    `${escapeHtml(text)}</text></svg>`
  const encoded = btoa(unescape(encodeURIComponent(svg)))
  return `url("data:image/svg+xml;base64,${encoded}")`
})

// 预览弹窗标题：行模式与页图（image）模式统一显示"文档预览：文件名"，
// 与代码预览弹窗展示一致（不展示"不可复制"字样）
const previewTitle = computed(() => {
  const st = previewState.value
  if (!st) return '文档预览'
  return '文档预览：' + (st.fileName || '')
})

// 可见范围展示文案
const previewVisibleLabel = computed(() => {
  const doc = previewMeta.value
  if (!doc) return '-'
  return { team: '仅团队', dept: '仅部门', public: '全局公开' }[doc.visible_scope]
    || doc.visible_scope || (doc.visibility_level || '-')
})

// 预览元信息数据源：按文档 ID 拉取；失败返回 null（预览主流程不受影响）
async function getDocForPreview(id) {
  try {
    return await api.getJson('/api/v1/knowledge/documents/' + id + '/')
  } catch (e) {
    return null
  }
}

// 文档预览入口（原文优先，不可复制）：
// image 模式 page 即 PDF 页号；code/text 行模式 page 换算为目标行号 (page-1)*500+1
function previewDocPage(id, page) {
  page = Math.max(1, parseInt(page, 10) || 1)
  // 已缓存且为 image 模式：直接切换页图（不重新请求元信息）
  const cached = previewCacheGet(id)
  if (cached && cached.mode === 'image') {
    previewDocId.value = id
    previewState.value = cached
    // 弹窗可能处于关闭状态：必须重新显示弹窗并刷新水印（按本次打开时间）
    if (!previewSessionActive.value) {
      previewSessionActive.value = true
      initWatermark()
    }
    // 缓存不保存元信息：异步补取一次，供标题/元信息条展示（丢弃过期响应）
    getDocForPreview(id).then(doc => {
      if (previewTargetId.value === id) previewMeta.value = doc
    })
    switchImagePage(page)
    return
  }
  const targetLine = page > 1 ? (page - 1) * PREVIEW_JUMP_PAGE_LINES + 1 : 1
  loadPreview(id, { targetLine, imagePage: page })
}

// 加载预览：缓存命中（whole/image）直接渲染；否则请求后端并初始化形态
async function loadPreview(id, opts) {
  opts = opts || {}
  const isFirstOpen = !previewSessionActive.value
  if (isFirstOpen) {
    previewDocId.value = null
    previewError.value = null
    previewMeta.value = null
    initWatermark()
    previewSessionActive.value = true
    const openedId = id
    getDocForPreview(id).then(doc => {
      // 异步返回期间弹窗可能已切换到其他文档，丢弃过期元信息
      if (previewTargetId.value !== openedId) return
      previewMeta.value = doc
    })
  }

  // 缓存命中：whole 整文件直出，直接渲染全文并定位
  // （目标行超出文件总行数时由 renderLineView 内部 clamp 到文件末尾）
  const cached = previewCacheGet(id)
  if (cached && cached.whole) {
    previewDocId.value = id
    previewState.value = cached
    renderLineView(cached, opts.targetLine || 1)
    return
  }

  previewLoading.value = true
  previewChunkError.value = false
  previewLines.value = []
  previewState.value = null
  try {
    // 跳页：从目标行往前取一屏上下文（约 0.8 屏），让跳转点前后都有内容
    let q = ''
    if (opts.targetLine && opts.targetLine > 1) {
      const jumpOffset = Math.max(1, opts.targetLine - Math.floor(PREVIEW_JUMP_PAGE_LINES * 0.8))
      q = '?offset=' + jumpOffset
    }
    const data = await api.getJson('/api/v1/knowledge/documents/' + id + '/preview/' + q)
    // 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
    if (previewTargetId.value !== id) return
    previewDocId.value = id
    initPreviewState(data, id, opts)
  } catch (e) {
    if (previewTargetId.value !== id) return
    console.warn('doc preview failed:', e)
    // 无访问权限时明确提示并关闭弹窗，避免误导为"原文暂不可用"
    if (e && e.status === 403) {
      ElMessage.error('无该文档访问权限')
      dialogVisible.value = false
      return
    }
    previewError.value = '原文暂不可用（解析未完成或文件缺失）'
  } finally {
    previewLoading.value = false
  }
}

// 由后端 preview 响应初始化形态状态并渲染
function initPreviewState(data, id, opts) {
  const mode = data.mode || 'text'
  const state = {
    id, mode,
    whole: !!data.whole,
    language: data.language || 'plaintext',
    pageUrl: data.page_url || '',
    totalPages: data.total_pages || 1,
    formatLabel: data.format_label || '',
    fallbackNotice: data.fallback_notice || '',
    fileName: data.file_name || '',
    totalLines: data.total_lines || 0,
    pageSizeLines: data.page_size_lines || PREVIEW_JUMP_PAGE_LINES,
    currentPage: 1,
    startLine: 1, nextOffset: 1,
    loadedLines: [], allLoaded: true, loading: false,
  }
  if (mode === 'image') {
    // PDF/Office 页图：按 PDF 页分页，跳页时定位到目标页
    previewPage.value = Math.min(Math.max(1, parseInt(opts.imagePage, 10) || 1), state.totalPages)
    state.currentPage = previewPage.value
    // 写入会话缓存：后续翻页直接走 switchImagePage 切页图，不再重新请求 preview 元信息
    previewCacheSet(id, state)
    previewState.value = state
    return
  }
  // 行模式：整文件直出或分块
  state.startLine = data.start_line || 1
  state.loadedLines = String(data.content || '').split('\n')
  if (state.loadedLines.length === 1 && state.loadedLines[0] === '') {
    state.loadedLines = []
  }
  state.allLoaded = data.whole ? true : !data.has_more
  state.nextOffset = state.startLine + state.loadedLines.length
  // 仅整文件直出内容写入会话缓存（分块模式重开时重新拉首块，保证从文件开头展示）
  if (state.whole) previewCacheSet(id, state)
  renderLineView(state, opts.targetLine || 1)
}

/* ---- 行模式渲染 ---- */
function renderLineView(state, targetLine) {
  previewState.value = state
  previewLines.value = state.loadedLines.map((text, i) => ({
    no: state.startLine + i,
    text,
    // 代码走轻量高亮，文本直接 textContent（自动转义防 XSS）
    html: state.mode === 'code' ? highlightCode(text, state.language) : null,
  }))
  // 仅跳页定位（page > 1）时滚动到目标行；普通打开默认停在文件开头，
  // 不调用 scrollPreviewBottom，避免进入预览时被滚到底部显示文档末尾。
  // 目标行超出已加载范围/文件总行数时定位到最后一行（小文件被引用不存在的页时，
  // 仍能让用户看到文件结尾而非停在开头，避免"点了第 2 页却显示第 1 页"的假象）
  if (targetLine && targetLine > 1) {
    const last = state.startLine + state.loadedLines.length - 1
    scrollPreviewToLine(Math.min(targetLine, last))
  }
}

// 行模式滚动触底：追加下一块并拼接（行号续接，无缝）
async function loadMoreLines() {
  const state = previewState.value
  if (!state || state.allLoaded || state.loading) return
  state.loading = true
  previewChunkError.value = false
  try {
    const data = await api.getJson('/api/v1/knowledge/documents/' + state.id + '/preview/?offset=' + state.nextOffset +
      '&limit=' + state.pageSizeLines)
    // 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
    if (previewTargetId.value !== state.id) return
    state.loading = false
    const newLines = String(data.content || '').split('\n')
    if (newLines.length === 1 && newLines[0] === '') newLines.length = 0
    state.loadedLines = state.loadedLines.concat(newLines)
    state.allLoaded = !data.has_more
    state.nextOffset = state.startLine + state.loadedLines.length
    previewLines.value = previewLines.value.concat(
      newLines.map((text, i) => ({
        no: state.startLine + state.loadedLines.length - newLines.length + i,
        text,
        html: state.mode === 'code' ? highlightCode(text, state.language) : null,
      }))
    )
  } catch (e) {
    // 加载失败不阻塞阅读：保留哨兵，可点击重试
    state.loading = false
    previewChunkError.value = true
  }
}

function onPreviewScroll() {
  // el-scrollbar 的 scroll 事件只携带 scrollTop/scrollLeft，不含 clientHeight/scrollHeight
  // （见 element-plus scrollbar.mjs: scroll: ({scrollTop, scrollLeft})），
  // 触底判断所需的高度信息从 wrap 实例读取，否则判断恒为 NaN<300=false、永不加载下一块
  const wrap = previewScrollRef.value && previewScrollRef.value.wrapRef
  if (!wrap) return
  // 触底前 300px 预取下一块（与旧版 IntersectionObserver rootMargin 行为一致）
  if (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 300) {
    loadMoreLines()
  }
}

// 跳页定位：滚动到目标行
function scrollPreviewToLine(targetLine) {
  nextTick(() => {
    const wrap = previewScrollRef.value && previewScrollRef.value.wrapRef
    if (!wrap) return
    const row = wrap.querySelector('[data-line="' + targetLine + '"]')
    if (row) row.scrollIntoView({ block: 'start', behavior: 'auto' })
  })
}

/* ---- image 模式：切换页图（不重新请求元信息） ---- */
function switchImagePage(page) {
  const state = previewState.value
  if (!state || state.mode !== 'image') return
  page = Math.max(1, Math.min(page, state.totalPages || 1))
  previewPage.value = page
  state.currentPage = page
  previewCacheSet(state.id, state)
  // 仅预取当前页的下一页：翻页后再次触发预取，避免打开第 1 页就把全部页预取完
  if (page < state.totalPages) {
    loadPageImage(state, page + 1)
  }
  // 翻页后回到该页顶部：上一页若滚动到页面下部，切页后不能停留在相同滚动位置
  nextTick(() => {
    const wrap = previewImgScrollRef.value && previewImgScrollRef.value.wrapRef
    if (wrap) wrap.scrollTop = 0
  })
}

// 当前页图 src（image 模式多页时使用）；渲染时若未缓存则触发加载
const previewPageSrc = computed(() => {
  const st = previewState.value
  if (!st || st.mode !== 'image') return ''
  return pageImgSrc(st, previewPage.value)
})

// 全部页图 src（totalPages <= 2 时一次性渲染全部，无需翻页）
const previewAllPages = computed(() => {
  const st = previewState.value
  if (!st || st.mode !== 'image' || st.totalPages > 2) return []
  return Array.from({ length: st.totalPages }, (_, i) => ({ page: i + 1, src: pageImgSrc(st, i + 1) }))
})

// 读取页图缓存，未命中则触发加载（同一页并发请求通过 inflight 标记去重）
function pageImgSrc(state, page) {
  const key = state.id + ':' + page
  const url = previewPageImgCache[key]
  if (!url) loadPageImage(state, page)
  return url || ''
}

// 加载页图：fetch（携带 token）→ Blob URL（img 无法携带 JWT，直接 src 会 401）
function loadPageImage(state, page) {
  const key = state.id + ':' + page
  if (previewPageInflight[key]) return
  previewPageInflight[key] = true
  const token = getToken() || ''
  fetch(state.pageUrl + page, {
    headers: { 'Authorization': 'Bearer ' + token }
  }).then(res => {
    if (!res.ok) throw new Error('page ' + page + ' http ' + res.status)
    return res.blob()
  }).then(blob => {
    const url = URL.createObjectURL(blob)
    previewPageImgCache[key] = url
    delete previewPageInflight[key]
    trimPageImgCache()
  }).catch(() => {
    delete previewPageInflight[key]
    // 预加载失败不影响当前页；仅当前页失败才降级
    if (page === previewPage.value) {
      previewError.value = '页图加载失败（文件可能已变更或暂不支持此格式）'
    }
  })
}

// 防内存/Blob 泄漏：超过 60 张页图时清掉最旧一半并 revoke 释放
function trimPageImgCache() {
  const keys = Object.keys(previewPageImgCache)
  if (keys.length > 60) {
    keys.slice(0, 30).forEach(k => {
      const old = previewPageImgCache[k]
      if (old) URL.revokeObjectURL(old)
      delete previewPageImgCache[k]
    })
  }
}

// footer 信息文案：image 显示格式 + 总页数；行模式显示行信息
const previewInfoText = computed(() => {
  const st = previewState.value
  if (!st) return ''
  if (st.mode === 'image') {
    if (st.totalPages <= 2) {
      return (st.formatLabel ? st.formatLabel + '，' : '') + '共 ' + st.totalPages + ' 页（已全部加载）'
    }
    return (st.formatLabel ? st.formatLabel + '，' : '') + '共 ' + st.totalPages + ' 页'
  }
  return st.allLoaded
    ? '共 ' + (st.totalLines || st.loadedLines.length).toLocaleString() + ' 行'
    : '已加载 ' + st.loadedLines.length.toLocaleString() + ' / ' + st.totalLines.toLocaleString() + ' 行'
})

// 关闭弹窗后清理：销毁正文/元信息并重置状态，避免再次打开时残留上一次内容
function onPreviewClosed() {
  previewState.value = null
  previewDocId.value = null
  previewPage.value = 1
  previewMeta.value = null
  previewError.value = null
  previewChunkError.value = false
  previewLines.value = []
  previewSessionActive.value = false
}

/* ---- 下载文档原文（fetch 携带 token，失败时提示） ---- */
async function downloadDoc(docId) {
  const token = getToken()
  if (!token) { ElMessage.error('请先登录'); return }
  try {
    const res = await fetch('/api/v1/knowledge/documents/' + docId + '/download/', {
      headers: { 'Authorization': 'Bearer ' + token }
    })
    if (!res.ok) {
      const d = await res.json().catch(() => ({}))
      throw new Error(d.detail || '下载失败')
    }
    // OSS 跳转（302）或文件流
    const ct = res.headers.get('content-type') || ''
    if (ct.indexOf('json') >= 0) {
      const data = await res.json()
      if (data.url) { window.open(data.url, '_blank'); return }
      if (data.detail) { ElMessage.error(data.detail); return }
      return
    }
    const blob = await res.blob()
    // 延迟撤销对象 URL：立即 revoke 偶发导致大文件下载中断
    downloadBlob(blob, '', { revokeDelay: 10000 })
  } catch (err) {
    ElMessage.error(errMsg(err, '下载失败'))
  }
}

/* ---- 轻量语法高亮（无第三方依赖） ----
 * 按 字符串/注释/数字/关键字 顺序做单趟分词，每段单独转义，
 * 避免对已转义 HTML 再做正则匹配导致错乱 */
const CODE_KEYWORDS = {
  python: 'def class return import from if elif else for while try except finally with as lambda pass break continue None True False and or not in is global nonlocal yield raise assert del async await',
  javascript: 'function return const let var if else for while do switch case break continue new class extends super this typeof instanceof try catch finally throw async await import export default null undefined true false',
  typescript: 'function return const let var if else for while do switch case break continue new class extends implements interface super this typeof instanceof try catch finally throw async await import export default null undefined true false readonly enum',
  java: 'public private protected class interface extends implements return void static final new if else for while do switch case break continue try catch finally throw throws import package null true false this super int long double float boolean char byte short String Object instanceof synchronized',
  go: 'package import func return var const if else for range switch case break continue defer go chan map struct interface type nil true false len cap make append delete select',
  c: 'int char float double void struct union enum typedef static extern const return if else for while do switch case break continue goto sizeof include define null true false unsigned signed long short volatile',
  cpp: 'int char float double void struct union enum class namespace template typename const return if else for while do switch case break continue try catch throw new delete this public private protected virtual override nullptr true false using',
  rust: 'fn let mut const if else for while loop match return pub use mod impl trait struct enum self Self Some None true false move ref dyn as where async await unsafe static',
  csharp: 'public private protected class interface struct enum namespace using return void static const readonly new if else for while do switch case break continue try catch finally throw null true false this base int long double float bool string object var async await is as',
  shell: 'if then else elif fi for while do done case esac function return export local echo cd set unset exit readonly',
  sql: 'SELECT INSERT UPDATE DELETE FROM WHERE GROUP BY ORDER HAVING JOIN LEFT RIGHT INNER OUTER ON AND OR NOT IN IS NULL LIKE CREATE TABLE ALTER DROP INDEX VIEW UNION DISTINCT LIMIT OFFSET AS',
  php: 'function class public private protected return if else foreach for while switch case break continue try catch finally throw new null true false echo isset empty array this namespace use static final interface',
  ruby: 'def class module return if elsif else unless for while do end yield nil true false and or not begin rescue ensure attr_reader attr_accessor new puts require',
  json: 'true false null',
  yaml: 'true false null yes no on off',
  toml: 'true false',
  ini: 'true false'
}

function highlightCode(code, lang) {
  const keywords = (CODE_KEYWORDS[lang] || '').split(/\s+/).filter(Boolean)
  const groups = [
    '("(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\]|\\\\.)*\'|`(?:[^`\\\\]|\\\\.)*`)', // 1 字符串
    '(\\/\\/.*$|#[^\\n]*$|--.*$|\\/\\*[\\s\\S]*?\\*\\/)',                   // 2 注释
    '\\b(\\d+(?:\\.\\d+)?)\\b'                                              // 3 数字
  ]
  if (keywords.length) {
    groups.push('\\b(' + keywords.join('|') + ')\\b')                       // 4 关键字
  }
  const re = new RegExp(groups.join('|'), 'gm')
  let out = ''
  let last = 0
  let m
  while ((m = re.exec(code)) !== null) {
    out += escapeHtml(code.slice(last, m.index))
    const cls = m[1] != null ? 'tok-str'
      : m[2] != null ? 'tok-com'
      : m[3] != null ? 'tok-num'
      : 'tok-kw'
    out += '<span class="' + cls + '">' + escapeHtml(m[0]) + '</span>'
    last = m.index + m[0].length
  }
  out += escapeHtml(code.slice(last))
  return out
}

// 弹窗显隐/文档切换：父组件打开或切换预览目标时触发加载
watch(
  () => [props.modelValue, props.docId, props.initialPage],
  () => {
    if (props.modelValue && props.docId != null) {
      previewTargetId.value = props.docId
      previewDocPage(props.docId, props.initialPage || 1)
    }
  },
  { immediate: true }
)

onBeforeUnmount(() => {
  // 释放页图 Blob URL，防止内存泄漏
  Object.keys(previewPageImgCache).forEach(k => {
    const url = previewPageImgCache[k]
    if (url) URL.revokeObjectURL(url)
    delete previewPageImgCache[k]
  })
})
</script>

<style>
/* ============ 文档预览弹窗（preview-doc.css 迁移） ============ */
/* 弹窗本体布局（固定 80% 视口尺寸 + 屏幕居中 + 头部/正文/底部结构）
   由 BaseDialog 骨架负责（.base-dialog 类生效），此处只补充预览特有样式 */

/* 预览水印：absolute 铺满 .doc-preview-body（弹窗 header 与 footer 之间的可视区域），
   防截图泄密（不阻挡交互）。background 由自定义 SVG data URL 生成（见 watermarkBg），
   平铺单元 200x200；不依赖弹窗外壳定位，避免被 BaseDialog 的 body 溢出裁剪 */
.doc-preview-body .doc-preview-watermark {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  pointer-events: none;
  overflow: hidden;
  z-index: 20;
  background-repeat: repeat;
  background-size: 200px 200px;
}

/* 弹窗 body 剔除默认内边距（BaseDialog 为 20px）：页图按 PPT 比例铺满整个宽度，
   四周间距改由内部元素自控（元信息条、代码块各自带外边距） */
.doc-preview-dialog .el-dialog__body {
  margin: 0;
  padding: 0;
}

/* 预览正文容器（禁止复制）：
   relative 作为水印定位基准；
   height:100% 撑满 BaseDialog 的 body 占位容器，弹窗高度固定后内部滚动容器才有确定高度 */
.doc-preview-body {
  position: relative;
  user-select: none;
  -webkit-user-select: none;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* 滚动容器：撑满剩余空间（含加载/降级提示等场景内容不高时按内容展示） */
.doc-preview-dialog .doc-preview-scroll {
  flex: 1;
  min-height: 0;
}

/* 首次加载占位 */
.doc-preview-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 64px 20px;
  color: var(--text-sub);
  font-size: 13px;
}

/* 页图模式：画布与滚动容器同色（--app-menu-hover，即代码模式背景），
   保证 word/PDF 预览的背景颜色与代码预览一致，滚动时露出的始终是同色背景。
   白色边框统一挂在滚动容器（.el-scrollbar__wrap）上，图片本身不带边框 */
.doc-preview-image-wrap {
  text-align: center;
  background: var(--app-menu-hover);
}

.doc-preview-image {
  /* 统一按 PPT 页（16:9）比例展示：宽铺满容器，高按比例固定；
     object-fit: contain 保留原图比例，PDF/Word 竖版页图在画布中居中留白，
     留白背景与画布同色，滚动翻页时观感连续（暗色下由 html.dark 规则处理） */
  width: 100%;
  aspect-ratio: 16 / 9;
  object-fit: contain;
  /* 与代码模式背景保持一致：img 元素自身的底色不再是白色，
     避免页图边缘/透明区域露出白底造成与代码预览颜色不一致 */
  background: var(--app-menu-hover);
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.08);
  user-select: none;
  -webkit-user-select: none;
}

/* 页图滚动容器外框（图片不带边框，滚动翻页时观感连续），背景同代码模式 */
.doc-preview-dialog .preview-img-scroll .el-scrollbar__wrap {
  border: 1px solid var(--border);
  border-radius: 0;
  background: var(--app-menu-hover);
}

/* 暗色主题下页图（PDF/Office 白纸渲染图）反色为深色底。
   invert(0.87)：白纸→RGB(33,33,33)，黑字→浅灰可读。
   不用 hue-rotate(180deg)：对灰度无影响，反而会让彩色页图（印章/插图）
   产生"棕色"等奇怪偏色。
   background:#fff：16:9 留白区（letterbox）与页图白纸同色，
   经 invert 后变为 RGB(33,33,33)，与下方画布颜色融为一体 */
html.dark .doc-preview-dialog .doc-preview-image {
  filter: invert(0.87);
  background: #fff;
}

/* 暗色下画布与滚动容器背景改为与页图反色结果一致（invert(0.87)→rgb(33,33,33)），
   页图、画布、滚动容器三者融为一体，不再有面板色差 */
html.dark .doc-preview-dialog .doc-preview-image-wrap,
html.dark .doc-preview-dialog .preview-img-scroll .el-scrollbar__wrap {
  background: rgb(33, 33, 33);
}

/* 降级提示条（body 无内边距，横向留 20px 与代码块对齐） */
.doc-preview-notice {
  margin: 0 20px 12px;
  padding: 8px 12px;
  font-size: 13px;
  color: #9a6700;
  background: #fff8e1;
  border: 1px solid #f0d98c;
  border-radius: var(--radius-lg);
}

/* 代码模式：行号 + 高亮代码（不可复制，无圆角贴合滚动容器；
   body 无内边距，代码块贴边对齐） */
.doc-preview-code {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Consolas, "Courier New", monospace;
  font-size: 13px;
  line-height: 1.7;
  background: var(--app-menu-hover);
  border: 1px solid var(--border);
  overflow: auto;
  user-select: none;
  -webkit-user-select: none;
}

.doc-preview-code-row {
  display: flex;
  /* 长文本/代码滚动优化：跳过视口外行的渲染与布局，
     contain-intrinsic-size 提供行高占位避免滚动条跳动 */
  content-visibility: auto;
  contain-intrinsic-size: auto 22px;
}

.doc-preview-code-ln {
  flex: 0 0 48px;
  padding: 0 10px;
  text-align: right;
  color: var(--text-sub);
  background: var(--app-menu-hover);
  border-right: 1px solid var(--border);
  user-select: none;
  -webkit-user-select: none;
}

.doc-preview-code-txt {
  flex: 1;
  padding: 0 12px;
  white-space: pre;
  word-break: normal;
}

.doc-preview-code.text-flow .doc-preview-code-txt {
  white-space: pre-wrap;
  word-break: break-word;
}

/* 连续滚动触底哨兵（追加加载提示） */
.doc-preview-sentinel {
  padding: 10px 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-sub);
  cursor: pointer;
}

/* 轻量高亮配色 */
.doc-preview-code-txt .tok-kw { color: #0550ae; font-weight: 600; }
.doc-preview-code-txt .tok-str { color: #0a7a33; }
.doc-preview-code-txt .tok-com { color: #6e7781; font-style: italic; }
.doc-preview-code-txt .tok-num { color: #953800; }

/* 原文不可用提示 */
.doc-preview-disabled {
  text-align: center;
  padding: 60px 20px;
  color: var(--text-sub);
}

.doc-preview-disabled-icon {
  font-size: 48px;
  margin-bottom: 12px;
}

/* 元信息条（body 无内边距，横向铺满弹窗边缘，仅上下留间距） */
.doc-preview-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  padding: 10px 0;
  margin: 12px 0;
  background: var(--app-menu-hover);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  font-size: 12px;
  color: var(--text-sub);
}

.doc-preview-meta-item {
  white-space: nowrap;
}

.doc-preview-meta-item:first-child {
  font-weight: 600;
  color: var(--text);
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* footer：字符数(左下) / 分页(居中) / 关闭(右下) 三栏布局 */
.doc-preview-footer {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 8px;
}

.doc-preview-footer .doc-preview-pager {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-self: center;
  white-space: nowrap;
}

.doc-preview-footer > .el-button {
  justify-self: end;
  white-space: nowrap;
  grid-column: 3;
}

.doc-preview-info {
  justify-self: start;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
  color: var(--text-sub);
}

/* 极窄视口：隐藏左下角字符数 */
@media (max-width: 480px) {
  .doc-preview-footer .doc-preview-info {
    display: none;
  }
  .doc-preview-footer {
    grid-template-columns: minmax(0, 1fr) auto;
  }
}

@media (max-width: 340px) {
  .doc-preview-footer > .el-button {
    display: none;
  }
  .doc-preview-footer {
    grid-template-columns: 1fr;
  }
}
</style>
