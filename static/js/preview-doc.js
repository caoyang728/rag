/* ==========================================================
   文档预览弹窗（公共模块，upload / admin-nodes / chat 共用）
   - 首次调用时动态创建弹窗 DOM，HTML 中无需预留
   - 依赖 common.js（toast/escapeHtml/showModal/closeModal）与 api.js
   - 页面需实现 getDocForPreview(id) -> Promise<doc>：
     返回预览元信息对象（file_name/file_type/file_size/version_tag/
     owner_name/created_at/visible_scope/visibility_level/can_download），
     找不到时 resolve(null) 或 reject
   - 预览形态按后端 preview 接口返回的 mode 区分：
     image：PDF/Office 页图（天然按页，上一页/下一页分页，不可复制）
     code / text：行模式——小文件（≤1000 行且 ≤512KB）后端 whole=true 整文件直出；
       大文件 whole=false，前端滚动触底按 500 行/块追加（连续滚动拼接，行号续接）
   ========================================================== */
var currentPreviewDocId = null;
var currentPreviewPage = 1;       // image 模式当前 PDF 页
var previewDocMeta = null;        // 当前预览文档元信息（原文不可用时用于展示文件名/下载入口）
var previewTargetId = null;       // 当前预览请求对应的文档 ID（用于丢弃异步过期响应，防快速切换错位）
var _previewState = null;         // 当前预览形态状态（由 preview 接口返回后写入）
var _previewCache = new Map();    // 会话级预览缓存（TTL 10 分钟，仅缓存 whole/image 内容）
var _PREVIEW_CACHE_TTL = 10 * 60 * 1000;
var _PREVIEW_JUMP_PAGE_LINES = 500;   // 聊天跳页换算粒度（与后端 _PREVIEW_CHUNK_LINES 一致）
var _previewWatermarkText = '';       // 预览水印文案（用户id + 打开时间），每次打开弹窗时初始化
var _previewPageImgCache = new Map(); // 页图 Blob URL 缓存（key: '{docId}:{page}'），会话级，翻页时避免重复请求
var _previewPageInflight = {};        // 页图加载中标记（key: '{docId}:{page}' → true），防同一页重复请求
var _previewPageWaiters = {};         // 页图加载中的等待 img 列表（key: '{docId}:{page}' → [img]），完成后统一赋值

/* ---- 初始化水印文案：当前用户账号 + 打开时间 ---- */
function _initWatermark() {
	var u = {};
	try { u = JSON.parse(localStorage.getItem('rag_user') || '{}'); } catch (e) { /* 忽略解析失败 */ }
	// 水印展示账号（username，可读可追溯），无账号时兜底用户 id
	var uid = u.username || u.id || '?';
	_previewWatermarkText = uid + ' · ' + formatDate(new Date());
}

/* 更新预览水印内容（防截图泄密；pointer-events:none 不阻挡滚动与点击）
   水印节点固定在弹窗滚动容器之外（header 与 footer 之间），
   无论代码/文本/页图内容多长，水印始终覆盖在 body 可视区域上，不随内容滚动 */
function _applyWatermark() {
	var modalEl = document.getElementById('docPreviewModal');
	if (!modalEl || !_previewWatermarkText) return;
	var wm = modalEl.querySelector('.doc-preview-watermark');
	if (!wm) return;
	var html = '';
	// 固定 12 个水印单元（3 列 × 4 行）覆盖整个可视区域
	for (var i = 0; i < 12; i++) {
		html += '<span>' + escapeHtml(_previewWatermarkText) + '</span>';
	}
	wm.innerHTML = html;
}

/* ---- 会话级缓存读写（惰性清理过期项） ---- */
function _previewCacheGet(id) {
	var item = _previewCache.get(id);
	if (!item) return null;
	if (Date.now() - item.ts > _PREVIEW_CACHE_TTL) {
		_previewCache.delete(id);
		return null;
	}
	return item;
}

function _previewCacheSet(id, state) {
	// 只缓存可完整复用的内容（whole 全文 / image 页图信息），分块模式重开时重新拉首块
	_previewCache.set(id, {
		id: id,
		mode: state.mode,
		whole: state.whole,
		language: state.language,
		pageUrl: state.pageUrl,
		totalPages: state.totalPages,
		formatLabel: state.formatLabel,
		fallbackNotice: state.fallbackNotice,
		fileName: state.fileName,
		totalLines: state.totalLines,
		pageSizeLines: state.pageSizeLines,
		startLine: state.startLine,
		currentPage: state.currentPage,
		loadedLines: state.loadedLines,
		allLoaded: state.allLoaded,
		ts: Date.now(),
	});
	// 防内存膨胀：超过 30 个文档时清掉最旧的一半
	if (_previewCache.size > 30) {
		var keys = Array.from(_previewCache.keys()).slice(0, 15);
		keys.forEach(function (k) { _previewCache.delete(k); });
	}
}

/* 懒创建弹窗 DOM（首次调用时初始化，页面无需预留 HTML） */
function _ensurePreviewModal() {
	var overlay = document.getElementById('docPreviewModal');
	if (overlay) return overlay;
	overlay = document.createElement('div');
	overlay.id = 'docPreviewModal';
	overlay.className = 'modal';
	overlay.innerHTML =
		'<div class="modal-content" style="width:760px;max-width:95vw;height:80vh">' +
		'  <div class="modal-header">' +
		'    <div class="modal-title" id="docPreviewTitle">文档预览</div>' +
		'    <button class="modal-close" onclick="closeDocPreviewModal()">&times;</button>' +
		'  </div>' +
		'  <div class="modal-body overflow-y-auto">' +
		'    <div id="docPreviewMeta" class="doc-preview-meta hidden"></div>' +
		'    <div id="docPreviewContent" class="doc-preview-content select-none"></div>' +
		'  </div>' +
		// 水印层固定在滚动容器外（header/footer 之间），覆盖 body 可视区域且不随内容滚动
		'  <div class="doc-preview-watermark"></div>' +
		'  <div class="modal-footer hidden doc-preview-footer" id="docPreviewFooter">' +
		'    <span class="text-sm text-sub" id="docPreviewInfo"></span>' +
		'    <div class="doc-preview-pager">' +
		'      <button class="btn btn-sm" id="docPreviewPrev" onclick="previewDocPage(currentPreviewDocId, currentPreviewPage - 1)">‹ 上一页</button>' +
		'      <span class="text-sm" id="docPreviewPage"></span>' +
		'      <button class="btn btn-sm" id="docPreviewNext" onclick="previewDocPage(currentPreviewDocId, currentPreviewPage + 1)">下一页 ›</button>' +
		'    </div>' +
		'    <button class="btn btn-sm" onclick="closeDocPreviewModal()">关闭</button>' +
		'  </div>' +
		'</div>';
	document.body.appendChild(overlay);
	return overlay;
}

/* 关闭预览弹窗：销毁正文/元信息/footer 并重置状态
   避免再次打开时残留上一次内容（如 docx 关闭后再打开代码，残留代码内容） */
function closeDocPreviewModal() {
	var contentEl = document.getElementById('docPreviewContent');
	if (contentEl) contentEl.innerHTML = '';
	var metaEl = document.getElementById('docPreviewMeta');
	if (metaEl) metaEl.classList.add('hidden');
	var titleEl = document.getElementById('docPreviewTitle');
	if (titleEl) titleEl.textContent = '文档预览';
	var footerEl = document.getElementById('docPreviewFooter');
	if (footerEl) footerEl.classList.add('hidden');
	_previewState = null;
	currentPreviewDocId = null;
	currentPreviewPage = 1;
	closeModal('docPreviewModal');
}

/* 打开文档预览（从第 1 页/第 1 行开始） */
function previewDoc(id) {
	_ensurePreviewModal();
	previewTargetId = id;
	previewDocPage(id, 1);
}

/* 文档预览入口（原文优先，不可复制）
   - image 模式：page 即 PDF 页号，直接切换页图
   - code/text 行模式：page 换算为目标行号 (page-1)*500+1，跳转并滚动定位
   首次打开渲染标题/元信息条/footer 初始状态并显示弹窗；后续仅更新正文与 footer。 */
function previewDocPage(id, page) {
	page = Math.max(1, parseInt(page, 10) || 1);
	// 已缓存且为 image 模式：直接切换页图（不重新请求元信息）
	var cached = _previewCacheGet(id);
	if (cached && cached.mode === 'image') {
		currentPreviewDocId = id;
		_previewState = cached;
		// 弹窗可能处于关闭状态（docx 打开过一次缓存后，关闭再打开会走到这里）：
		// 必须重新显示弹窗并刷新水印（按本次打开时间），否则看起来"打不开"
		_initWatermark();
		showModal('docPreviewModal');
		_applyWatermark();
		_switchImagePage(page);
		return;
	}
	var targetLine = page > 1 ? (page - 1) * _PREVIEW_JUMP_PAGE_LINES + 1 : 1;
	loadPreview(id, { targetLine: targetLine, imagePage: page });
}

/* 加载预览：缓存命中（whole/image）直接渲染；否则请求后端并初始化形态 */
function loadPreview(id, opts) {
	opts = opts || {};
	var modalEl = _ensurePreviewModal();
	var contentEl = document.getElementById('docPreviewContent');
	var footerEl = document.getElementById('docPreviewFooter');
	var titleEl = document.getElementById('docPreviewTitle');
	var metaEl = document.getElementById('docPreviewMeta');
	var isFirstOpen = !modalEl.classList.contains('show');

	if (isFirstOpen) {
		titleEl.textContent = '文档预览';
		footerEl.classList.add('hidden');
		// 水印文案按本次打开时间初始化（用户id + 打开时间）
		_initWatermark();

		// 元信息条仅首次打开时渲染（页面注入 getDocForPreview 返回 Promise）
		previewDocMeta = null;
		var openedId = previewTargetId;
		var p = (typeof getDocForPreview === 'function') ? getDocForPreview(id) : Promise.resolve(null);
		Promise.resolve(p).then(function (doc) {
			// 异步返回期间弹窗可能已切换到其他文档，丢弃过期元信息
			if (previewTargetId !== openedId) return;
			previewDocMeta = doc;
			if (doc) {
				renderDocPreviewMeta(metaEl, doc);
			} else {
				metaEl.classList.add('hidden');
			}
		}).catch(function () {
			metaEl.classList.add('hidden');
		});

		showModal('docPreviewModal');
	}

	// 缓存命中：whole 整文件直出，直接渲染全文并定位
	var cached = _previewCacheGet(id);
	if (cached && cached.whole) {
		currentPreviewDocId = id;
		_previewState = cached;
		_renderLineView(cached, opts.targetLine || 1);
		return;
	}

	// 仅重新渲染正文内容（加载中占位 → 请求结果）；首次加载渲染耗时，给出转圈动画提示
	contentEl.innerHTML =
		'<div class="doc-preview-loading">' +
		'  <div class="doc-preview-loading-spinner"></div>' +
		'  <div class="doc-preview-loading-text">加载中...</div>' +
		'</div>';

	var q = '';
	if (opts.targetLine && opts.targetLine > 1) {
		// 跳页：从目标行往前取一屏上下文（约 0.8 屏），让跳转点前后都有内容
		var jumpOffset = Math.max(1, opts.targetLine - Math.floor(_PREVIEW_JUMP_PAGE_LINES * 0.8));
		q = '?offset=' + jumpOffset;
	}

	api.getJson('/api/v1/knowledge/documents/' + id + '/preview/' + q).then(function (data) {
		// 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
		if (previewTargetId !== id) return;
		currentPreviewDocId = id;
		_initPreviewState(data, id, opts);
	}).catch(function (e) {
		// 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
		if (previewTargetId !== id) return;
		console.warn('doc preview failed:', e);
		// 无访问权限时明确提示并关闭弹窗，避免误导为"原文暂不可用"
		if (e && e.status === 403) {
			toast('无该文档访问权限', 'error');
			closeModal('docPreviewModal');
			return;
		}
		if (isFirstOpen) {
			// textContent 赋值自动转义，无需 escapeHtml
			titleEl.textContent = '文档预览：' + (previewDocMeta && previewDocMeta.file_name ? previewDocMeta.file_name : '');
		}
		var downloadBtn = previewDocMeta && previewDocMeta.can_download
			? '<button class="btn btn-sm" style="margin-top:14px" onclick="downloadDoc(' + id + ')">⬇ 下载原文</button>'
			: '';
		contentEl.innerHTML =
			'<div class="doc-preview-disabled">' +
			'<div class="doc-preview-disabled-icon">📭</div>' +
			'<div>原文暂不可用（解析未完成或文件缺失）</div>' +
			downloadBtn +
			'</div>';
	});
}

/* 由后端 preview 响应初始化形态状态并渲染 */
function _initPreviewState(data, id, opts) {
	var mode = data.mode || 'text';
	var state = {
		id: id,
		mode: mode,
		whole: !!data.whole,
		language: data.language || 'plaintext',
		pageUrl: data.page_url || '',
		totalPages: data.total_pages || 1,
		formatLabel: data.format_label || '',
		fallbackNotice: data.fallback_notice || '',
		fileName: data.file_name || '',
		totalLines: data.total_lines || 0,
		pageSizeLines: data.page_size_lines || _PREVIEW_JUMP_PAGE_LINES,
		currentPage: 1,
	};
	if (mode === 'image') {
		// PDF/Office 页图：按 PDF 页分页，跳页时定位到目标页
		currentPreviewPage = Math.min(Math.max(1, parseInt(opts.imagePage, 10) || 1), state.totalPages);
		state.currentPage = currentPreviewPage;
		state.loadedLines = null;
		state.allLoaded = true;
		_previewState = state;
		// 写入会话缓存：后续翻页直接走 _switchImagePage 切页图，
		// 不再重新请求 preview 元信息（否则每次翻页都带 offset 冗余请求）
		_previewCacheSet(id, state);
		// 先清空加载占位，再渲染页图
		var contentEl = document.getElementById('docPreviewContent');
		contentEl.innerHTML = '';
		_renderImageView(state, currentPreviewPage);
		_applyWatermark();
		renderPreviewFooter();
		return;
	}
	// 行模式：整文件直出或分块
	state.startLine = data.start_line || 1;
	state.loadedLines = String(data.content || '').split('\n');
	if (state.loadedLines.length === 1 && state.loadedLines[0] === '') {
		state.loadedLines = [];
	}
	state.allLoaded = data.whole ? true : !data.has_more;
	state.nextOffset = state.startLine + state.loadedLines.length;
	_previewState = state;
	// 仅整文件直出内容写入会话缓存（分块模式重开时重新拉首块，保证从文件开头展示）
	if (state.whole) {
		_previewCacheSet(id, state);
	}
	_renderLineView(state, (opts.targetLine || 1));
}

/* ---- 行模式渲染（code 高亮 / text 纯文本，均带行号列，追加时行号续接） ---- */
function _renderLineView(state, targetLine) {
	var contentEl = document.getElementById('docPreviewContent');
	var titleEl = document.getElementById('docPreviewTitle');
	contentEl.innerHTML = '';

	// 降级提示（如 Office 未安装格式转换组件，以文本模式预览）
	if (state.fallbackNotice) {
		var notice = document.createElement('div');
		notice.className = 'doc-preview-notice';
		notice.textContent = state.fallbackNotice;
		contentEl.appendChild(notice);
	}
	// textContent 赋值自动转义，无需 escapeHtml
	titleEl.textContent = '文档预览：' + (state.fileName || '') + '（不可复制）';

	if (!state.loadedLines || state.loadedLines.length === 0) {
		contentEl.innerHTML += '<div class="doc-preview-disabled"><div class="doc-preview-disabled-icon">📭</div>文档无内容</div>';
		_applyWatermark();
		renderPreviewFooter();
		return;
	}

	var box = document.createElement('div');
	// 文本模式加 text-flow 修饰：长段落允许换行（代码保持单行横向滚动）
	box.className = 'doc-preview-code' + (state.mode === 'code' ? '' : ' text-flow');
	contentEl.appendChild(box);
	state.view = { box: box };
	state.renderedCount = 0;
	_appendLineRows(state, 0);
	state.renderedCount = state.loadedLines.length;

	// 未加载完：挂哨兵 + 滚动触底追加
	if (!state.allLoaded) {
		_attachInfiniteScroll(state);
	}
	_applyWatermark();
	renderPreviewFooter();
	if (targetLine && targetLine > 1) {
		_scrollToLine(state, targetLine);
	}
}

/* 渲染/追加行（fromIdx 起）到行容器，行号按 startLine+i 连续 */
function _appendLineRows(state, fromIdx) {
	var box = state.view.box;
	var frag = document.createDocumentFragment();
	for (var i = fromIdx; i < state.loadedLines.length; i++) {
		frag.appendChild(_buildLineRow(state.startLine + i, state.loadedLines[i], state.mode, state.language));
	}
	box.appendChild(frag);
}

function _buildLineRow(lineNo, text, mode, language) {
	var row = document.createElement('div');
	row.className = 'doc-preview-code-row';
	row.setAttribute('data-line', String(lineNo));
	var ln = document.createElement('span');
	ln.className = 'doc-preview-code-ln';
	ln.textContent = String(lineNo);
	var txt = document.createElement('code');
	txt.className = 'doc-preview-code-txt';
	// 代码走轻量高亮，文本直接 textContent（自动转义防 XSS）
	if (mode === 'code') {
		txt.innerHTML = highlightCode(text, language);
	} else {
		txt.textContent = text;
	}
	row.appendChild(ln);
	row.appendChild(txt);
	return row;
}

/* 挂载滚动触底哨兵：进入视口即追加下一块（rootMargin 提前 300px 预取） */
function _attachInfiniteScroll(state) {
	var contentEl = document.getElementById('docPreviewContent');
	var sentinel = document.createElement('div');
	sentinel.className = 'doc-preview-sentinel';
	sentinel.textContent = '已加载 ' + state.loadedLines.length.toLocaleString() + ' / ' + state.totalLines.toLocaleString() + ' 行，继续滚动加载...';
	contentEl.appendChild(sentinel);
	state.sentinel = sentinel;
	if (!('IntersectionObserver' in window)) return;
	state.observer = new IntersectionObserver(function (entries) {
		entries.forEach(function (entry) {
			if (entry.isIntersecting && !state.loading && !state.allLoaded) {
				_loadMoreChunk(state);
			}
		});
	}, { rootMargin: '300px 0px' });
	state.observer.observe(sentinel);
}

/* 追加加载下一块并拼接（行号续接，无缝） */
function _loadMoreChunk(state) {
	state.loading = true;
	state.sentinel.textContent = '加载中...';
	api.getJson('/api/v1/knowledge/documents/' + state.id + '/preview/?offset=' + state.nextOffset +
		'&limit=' + state.pageSizeLines).then(function (data) {
		// 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
		if (previewTargetId !== state.id) return;
		state.loading = false;
		state.loadedLines = state.loadedLines.concat(String(data.content || '').split('\n'));
		state.allLoaded = !data.has_more;
		state.nextOffset = state.startLine + state.loadedLines.length;
		_appendLineRows(state, state.renderedCount);
		state.renderedCount = state.loadedLines.length;
		if (state.allLoaded) {
			state.sentinel.textContent = '';
			if (state.observer) { state.observer.disconnect(); }
		} else {
			state.sentinel.textContent = '已加载 ' + state.loadedLines.length.toLocaleString() + ' / ' + state.totalLines.toLocaleString() + ' 行，继续滚动加载...';
		}
		renderPreviewFooter();
	}).catch(function () {
		// 加载失败不阻塞阅读：保留哨兵，向下滚动可重试
		state.loading = false;
		if (state.sentinel) {
			state.sentinel.textContent = '加载失败，继续滚动重试';
		}
	});
}

/* 跳页定位：滚动到目标行（行容器内 scrollIntoView，滚动发生在 modal-body） */
function _scrollToLine(state, targetLine) {
	var box = state.view && state.view.box;
	if (!box) return;
	var row = box.querySelector('[data-line="' + targetLine + '"]');
	if (row) {
		row.scrollIntoView({ block: 'start', behavior: 'auto' });
	}
}

/* image 模式：切换页图（不重新请求元信息），同时更新 footer */
function _switchImagePage(page) {
	var state = _previewState;
	if (!state || state.mode !== 'image') return;
	page = Math.max(1, Math.min(page, state.totalPages || 1));
	currentPreviewPage = page;
	state.currentPage = page;
	_previewCacheSet(state.id, state);
	var contentEl = document.getElementById('docPreviewContent');
	// 整体清空而非只移除页图 wrap：避免残留上一次模式（如代码）的内容
	contentEl.innerHTML = '';
	_renderImageView(state, page);
	renderPreviewFooter();
}

/* ---- image 模式渲染：页图走 fetch + Blob URL（img 无法携带 JWT，直接 src 会 401） ---- */
function _renderImageView(state, page) {
	var contentEl = document.getElementById('docPreviewContent');
	// ≤2 页：一次性渲染全部页图（连续堆叠展示），无需翻页
	if (state.totalPages <= 2) {
		for (var p = 1; p <= state.totalPages; p++) {
			contentEl.appendChild(_buildPageImage(state, p));
		}
		return;
	}
	contentEl.appendChild(_buildPageImage(state, page));
	// 仅预取当前页的下一页：翻页后由 _switchImagePage 再触发预取新的下一页，
	// 避免打开第 1 页就把全部页预取完；已是最后一页则不再发起请求
	if (page < state.totalPages) {
		_loadPageImage(state, page + 1, null);
	}
}

function _buildPageImage(state, page) {
	var wrap = document.createElement('div');
	wrap.className = 'doc-preview-image-wrap';
	var img = document.createElement('img');
	img.className = 'doc-preview-image';
	img.alt = '第 ' + page + ' 页';
	wrap.appendChild(img);
	_loadPageImage(state, page, img);
	return wrap;
}

/* 加载页图：缓存命中直接显示；否则 fetch（携带 token）→ Blob URL 并预取下一页
   同一页并发请求（渲染预取 + 翻页）通过 inflight/waiters 去重合并 */
function _loadPageImage(state, page, img) {
	var key = state.id + ':' + page;
	var cached = _previewPageCacheGet(state, page);
	if (cached) {
		if (img) img.src = cached;
		return;
	}
	if (_previewPageInflight[key]) {
		// 该页已在加载中（多为预取触发）：把当前 img 挂为等待者，完成后统一赋值
		if (img) {
			(_previewPageWaiters[key] = _previewPageWaiters[key] || []).push(img);
		}
		return;
	}
	_previewPageInflight[key] = true;
	var token = localStorage.getItem('rag_access') || '';
	fetch(state.pageUrl + page, {
		headers: { 'Authorization': 'Bearer ' + token }
	}).then(function (res) {
		if (!res.ok) throw new Error('page ' + page + ' http ' + res.status);
		return res.blob();
	}).then(function (blob) {
		var url = URL.createObjectURL(blob);
		_previewPageCacheSet(state, page, url);
		if (img) img.src = url;
		// 预取期间翻页挂起的 img 统一赋值
		var waiters = _previewPageWaiters[key] || [];
		for (var i = 0; i < waiters.length; i++) { waiters[i].src = url; }
		delete _previewPageWaiters[key];
		delete _previewPageInflight[key];
	}).catch(function () {
		delete _previewPageWaiters[key];
		delete _previewPageInflight[key];
		// 预加载失败不影响当前页；仅当前页（img 存在）失败才降级
		if (img) renderPreviewImageError(state);
	});
}

/* 页图 Blob URL 缓存读写：key 为 '{docId}:{page}'，防翻页重复请求 */
function _previewPageCacheGet(state, page) {
	return _previewPageImgCache.get(state.id + ':' + page) || null;
}

function _previewPageCacheSet(state, page, url) {
	_previewPageImgCache.set(state.id + ':' + page, url);
	// 防内存/Blob 泄漏：超过 60 张页图时清掉最旧一半并 revoke 释放
	if (_previewPageImgCache.size > 60) {
		var keys = Array.from(_previewPageImgCache.keys()).slice(0, 30);
		keys.forEach(function (k) {
			var old = _previewPageImgCache.get(k);
			if (old) URL.revokeObjectURL(old);
			_previewPageImgCache.delete(k);
		});
	}
}

/* 页图加载失败降级：提示 + 下载入口（可下载时） */
function renderPreviewImageError(state) {
	var contentEl = document.getElementById('docPreviewContent');
	var downloadBtn = previewDocMeta && previewDocMeta.can_download
		? '<button class="btn btn-sm" style="margin-top:14px" onclick="downloadDoc(' + currentPreviewDocId + ')">⬇ 下载原文</button>'
		: '';
	contentEl.innerHTML =
		'<div class="doc-preview-disabled">' +
		'<div class="doc-preview-disabled-icon">🖼</div>' +
		'<div>页图加载失败（文件可能已变更或暂不支持此格式）</div>' +
		downloadBtn +
		'</div>';
}

/* ---- footer 渲染：image 显示分页按钮；行模式显示行信息、隐藏分页按钮 ---- */
function renderPreviewFooter() {
	var footerEl = document.getElementById('docPreviewFooter');
	var infoEl = document.getElementById('docPreviewInfo');
	var pageEl = document.getElementById('docPreviewPage');
	var prevBtn = document.getElementById('docPreviewPrev');
	var nextBtn = document.getElementById('docPreviewNext');
	var state = _previewState;
	if (!state) {
		footerEl.classList.add('hidden');
		return;
	}

	// image 模式：≤2 页已全部加载，隐藏分页按钮；多页显示上一页/下一页（单页时按钮置灰）
	if (state.mode === 'image') {
		if (state.totalPages <= 2) {
			infoEl.textContent = (state.formatLabel ? state.formatLabel + '，' : '') + '共 ' + state.totalPages + ' 页（已全部加载）';
			pageEl.textContent = '';
			footerEl.classList.add('no-pager');
		} else {
			infoEl.textContent = (state.formatLabel ? state.formatLabel + '，' : '') + '共 ' + state.totalPages + ' 页';
			pageEl.textContent = '第 ' + currentPreviewPage + ' / ' + state.totalPages + ' 页';
			prevBtn.disabled = currentPreviewPage <= 1;
			nextBtn.disabled = currentPreviewPage >= state.totalPages;
			footerEl.classList.remove('no-pager');
		}
		footerEl.classList.remove('hidden');
		return;
	}

	// 行模式：无翻页按钮，仅展示行信息（直出显示总数，分块显示已加载进度）
	infoEl.textContent = state.allLoaded
		? '共 ' + (state.totalLines || state.loadedLines.length).toLocaleString() + ' 行'
		: '已加载 ' + state.loadedLines.length.toLocaleString() + ' / ' + state.totalLines.toLocaleString() + ' 行';
	pageEl.textContent = '';
	footerEl.classList.add('no-pager');
	footerEl.classList.remove('hidden');
}

/* ==========================================================
   轻量语法高亮（无第三方依赖）
   - 按 字符串/注释/数字/关键字 顺序做单趟分词，每段单独转义，
     避免对已转义 HTML 再做正则匹配导致错乱
   ========================================================== */
var _CODE_KEYWORDS = {
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
};

function highlightCode(code, lang) {
	var keywords = (_CODE_KEYWORDS[lang] || '').split(/\s+/).filter(Boolean);
	var groups = [
		'("(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\]|\\\\.)*\'|`(?:[^`\\\\]|\\\\.)*`)', // 1 字符串
		'(\\/\\/.*$|#[^\\n]*$|--.*$|\\/\\*[\\s\\S]*?\\*\\/)',                   // 2 注释
		'\\b(\\d+(?:\\.\\d+)?)\\b'                                              // 3 数字
	];
	if (keywords.length) {
		groups.push('\\b(' + keywords.join('|') + ')\\b');                       // 4 关键字
	}
	var re = new RegExp(groups.join('|'), 'gm');
	var out = '';
	var last = 0;
	var m;
	while ((m = re.exec(code)) !== null) {
		out += escapeHtml(code.slice(last, m.index));
		var cls = m[1] != null ? 'tok-str'
			: m[2] != null ? 'tok-com'
			: m[3] != null ? 'tok-num'
			: 'tok-kw';
		out += '<span class="' + cls + '">' + escapeHtml(m[0]) + '</span>';
		last = m.index + m[0].length;
	}
	out += escapeHtml(code.slice(last));
	return out;
}

/* ---- 预览弹窗元信息条渲染（文件名/类型/大小/版本/上传人/时间/可见范围） ---- */
function renderDocPreviewMeta(metaEl, doc) {
	if (!metaEl || !doc) return;
	function fmtSize(bytes) {
		if (!bytes && bytes !== 0) return '-';
		if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
		if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
		return bytes + ' B';
	}
	var visLabel = { team: '仅团队', dept: '仅部门', public: '全局公开' }[doc.visible_scope]
		|| doc.visible_scope || (doc.visibility_level || '-');
	metaEl.innerHTML =
		'<span class="doc-preview-meta-item">📄 ' + escapeHtml(doc.file_name || '') + '</span>' +
		'<span class="doc-preview-meta-item">类型：' + escapeHtml(doc.file_type || '-') + '</span>' +
		'<span class="doc-preview-meta-item">大小：' + fmtSize(doc.file_size) + '</span>' +
		'<span class="doc-preview-meta-item">版本：' + escapeHtml(doc.version_tag || '-') + '</span>' +
		'<span class="doc-preview-meta-item">上传人：' + escapeHtml(doc.owner_name || '-') + '</span>' +
		'<span class="doc-preview-meta-item">时间：' + formatDate(doc.created_at) + '</span>' +
		'<span class="doc-preview-meta-item">可见：' + visLabel + '</span>';
	metaEl.classList.remove('hidden');
}

/* ---- 下载文档原文（fetch 携带 token，失败时提示） ---- */
function downloadDoc(docId) {
	var token = localStorage.getItem('rag_access');
	if (!token) { toast('请先登录', 'error'); return; }
	fetch('/api/v1/knowledge/documents/' + docId + '/download/', {
		headers: { 'Authorization': 'Bearer ' + token }
	}).then(function (res) {
		if (!res.ok) return res.json().then(function (d) { throw new Error(d.detail || '下载失败'); });
		// OSS 跳转（302）或文件流
		if (res.headers.get('content-type') && res.headers.get('content-type').indexOf('json') >= 0) {
			return res.json();
		}
		return res.blob();
	}).then(function (data) {
		if (data instanceof Blob) {
			var a = document.createElement('a');
			a.href = URL.createObjectURL(data);
			a.download = '';
			document.body.appendChild(a);
			a.click();
			a.remove();
			// 延迟撤销对象 URL：立即 revoke 偶发导致大文件下载中断
			setTimeout(function () { URL.revokeObjectURL(a.href); }, 10000);
		} else if (data && data.url) {
			// OSS 签名 URL 跳转
			window.open(data.url, '_blank');
		} else if (data && data.detail) {
			toast(data.detail, 'error');
		}
	}).catch(function (err) {
		toast(err.message || '下载失败', 'error');
	});
}