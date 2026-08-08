/* ==========================================================
   文档预览弹窗（公共模块，upload / admin-nodes 共用）
   - 首次调用时动态创建弹窗 DOM，HTML 中无需预留
   - 依赖 common.js（toast/escapeHtml/showModal/closeModal）与 api.js
   - 页面需实现 getDocForPreview(id) -> Promise<doc>：
     返回预览元信息对象（file_name/file_type/file_size/version_tag/
     owner_name/created_at/visible_scope/visibility_level/can_download），
     找不到时 resolve(null) 或 reject
   ========================================================== */
var currentPreviewDocId = null;
var currentPreviewPage = 1;
var currentPreviewTotalPages = 1;
var currentPreviewTotalChars = 0;
var previewDocMeta = null;  // 当前预览文档元信息（原文不可用时用于展示文件名/下载入口）
var previewTargetId = null; // 当前预览请求对应的文档 ID（用于丢弃异步过期响应，防快速切换错位）

/* 懒创建弹窗 DOM（首次调用时初始化，页面无需预留 HTML） */
function _ensurePreviewModal() {
	var overlay = document.getElementById('docPreviewModal');
	if (overlay) return overlay;
	overlay = document.createElement('div');
	overlay.id = 'docPreviewModal';
	overlay.className = 'modal';
	overlay.innerHTML =
		'<div class="modal-content" style="width:640px;max-width:95vw;height:72vh">' +
		'  <div class="modal-header">' +
		'    <div class="modal-title" id="docPreviewTitle">文档预览</div>' +
		'    <button class="modal-close" onclick="closeModal(\'docPreviewModal\')">&times;</button>' +
		'  </div>' +
		'  <div class="modal-body overflow-y-auto">' +
		'    <div id="docPreviewMeta" class="doc-preview-meta hidden"></div>' +
		'    <div id="docPreviewContent" class="doc-preview-content select-none"></div>' +
		'  </div>' +
		'  <div class="modal-footer hidden doc-preview-footer" id="docPreviewFooter">' +
		'    <span class="text-sm text-sub" id="docPreviewInfo"></span>' +
		'    <div class="doc-preview-pager">' +
		'      <button class="btn btn-sm" id="docPreviewPrev" onclick="previewDocPage(currentPreviewDocId, currentPreviewPage - 1)">‹ 上一页</button>' +
		'      <span class="text-sm" id="docPreviewPage"></span>' +
		'      <button class="btn btn-sm" id="docPreviewNext" onclick="previewDocPage(currentPreviewDocId, currentPreviewPage + 1)">下一页 ›</button>' +
		'    </div>' +
		'    <button class="btn btn-sm" onclick="closeModal(\'docPreviewModal\')">关闭</button>' +
		'  </div>' +
		'</div>';
	document.body.appendChild(overlay);
	return overlay;
}

/* 打开文档预览（重置分页后加载第 1 页） */
function previewDoc(id) {
	_ensurePreviewModal();
	previewTargetId = id;
	previewDocPage(id, 1);
}

/* 文档预览（原文优先，不可复制，支持分页）
   首次打开：渲染标题/元信息条/footer 初始状态并显示弹窗；
   切换分页：仅更新正文内容与 footer 页码，其他区域保持不动 */
function previewDocPage(id, page) {
	var modalEl = _ensurePreviewModal();
	var titleEl = document.getElementById('docPreviewTitle');
	var contentEl = document.getElementById('docPreviewContent');
	var footerEl = document.getElementById('docPreviewFooter');
	var infoEl = document.getElementById('docPreviewInfo');
	var pageEl = document.getElementById('docPreviewPage');
	var prevBtn = document.getElementById('docPreviewPrev');
	var nextBtn = document.getElementById('docPreviewNext');
	var metaEl = document.getElementById('docPreviewMeta');

	var isFirstOpen = !modalEl.classList.contains('show');

	if (isFirstOpen) {
		titleEl.textContent = '文档预览';
		footerEl.classList.add('hidden');

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

	// 仅重新渲染正文内容（加载中占位 → 请求结果）
	contentEl.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-sub)">加载中...</div>';

	// 原文内容优先（支持分页）；原文不可用时展示"原文暂不可用" + 下载入口，
	// 不再降级为检索切片列表（切片是 RAG 内部细节，非面向用户的查看形态）
	api.getJson('/api/v1/knowledge/documents/' + id + '/raw_content/?page=' + page).then(function (data) {
		// 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
		if (previewTargetId !== id) return;
		if (!data.content || !data.content.trim()) {
			contentEl.innerHTML = '<div class="doc-preview-disabled"><div class="doc-preview-disabled-icon">📭</div>文档无内容</div>';
			return;
		}

		currentPreviewDocId = id;
		currentPreviewPage = data.current_page || 1;
		currentPreviewTotalPages = data.total_pages || 1;
		currentPreviewTotalChars = data.total_chars || 0;

		if (isFirstOpen) {
			// textContent 赋值自动转义，无需 escapeHtml
			titleEl.textContent = '文档预览：' + (data.file_name || '') + '（不可复制）';
		}
		contentEl.innerHTML = '<pre class="doc-preview-content" style="white-space:pre-wrap;word-break:break-word;font-family:inherit">' + escapeHtml(data.content) + '</pre>';

		// 分页栏仅在多页时显示（翻页时 footer 已显示，仅更新页码与按钮状态）
		if (currentPreviewTotalPages > 1) {
			footerEl.classList.remove('hidden');
			infoEl.textContent = '共 ' + currentPreviewTotalChars.toLocaleString() + ' 字符';
			pageEl.textContent = '第 ' + currentPreviewPage + ' / ' + currentPreviewTotalPages + ' 页';
			prevBtn.disabled = currentPreviewPage <= 1;
			nextBtn.disabled = currentPreviewPage >= currentPreviewTotalPages;
		} else {
			footerEl.classList.add('hidden');
		}
	}).catch(function (e) {
		// 异步返回期间弹窗可能已切换到其他文档，丢弃过期响应
		if (previewTargetId !== id) return;
		console.warn('raw_content failed:', e);
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
