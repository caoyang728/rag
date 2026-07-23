/* ============ 文档上传页 ============ */

/** 待上传的文件列表：{ id, file, name, size, type, icon } */
let pendingFiles = [];
let allNodes = [];
let uploadHistoryCurrentPage = 1;
let uploadHistoryTotal = 0;

document.addEventListener('DOMContentLoaded', () => {
	initUploadPage();
	initDropZone();
	initNodeSelect();
	checkAndShowCeleryStatus();
});

/* ============ 上传历史表格 ============ */
let uploadHistorySearch = '';
let uploadHistoryStatus = '';
let currentDocs = [];

async function initUploadPage() {
	await loadUploadHistory();
	initSearchFilter();
}

function initSearchFilter() {
	const searchInput = document.querySelector('.page-actions .input[placeholder*="搜索"]');
	const statusSelect = document.querySelector('.page-actions .select');

	if (searchInput) {
		searchInput.addEventListener('input', (e) => {
			uploadHistorySearch = e.target.value.trim();
			loadUploadHistory(1);
		});
	}

	if (statusSelect) {
		statusSelect.addEventListener('change', (e) => {
			uploadHistoryStatus = e.target.value === '全部状态' ? '' : e.target.value;
			loadUploadHistory(1);
		});
	}

	// 启动上传历史持续刷新
	startUploadHistoryPolling();
}

async function loadUploadHistory(page = 1) {
	const tbody = $('#uploadHistoryBody');
	if (!tbody) return;

	try {
		let url = `/api/v1/knowledge/documents/?page=${page}&page_size=10`;
		if (uploadHistorySearch) {
			url += `&search=${encodeURIComponent(uploadHistorySearch)}`;
		}
		if (uploadHistoryStatus) {
			url += `&status=${uploadHistoryStatus}`;
		}

		const data = await api.getJson(url);
		const docs = data.results || data;
		currentDocs = docs;
		uploadHistoryTotal = data.count || (docs.length || 0);
		uploadHistoryCurrentPage = page;

		if (!docs || docs.length === 0) {
			tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-sub)">暂无上传记录</td></tr>';
			renderUploadPagination();
			return;
		}

		tbody.innerHTML = docs.map(h => `
      <tr>
        <td><span style="display:inline-flex;align-items:center;gap:6px">${fileTypeIcon(h.file_type)} ${escapeHtml(h.file_name)}</span></td>
        <td><span class="tag">${fileTypeByExt(h.file_name)}</span></td>
        <td class="text-sub">${escapeHtml(h.node_name || '-')}</td>
        <td>${escapeHtml(h.owner_name || '-')}</td>
        <td>${visTag(h.visibility)}</td>
        <td>${statusTag(h.status)}</td>
        <td class="text-sub">${formatDate(h.created_at)}</td>
        <td>
          <div class="table-actions">
            <button class="btn-link btn-sm" onclick="viewDocument(${h.id})">查看</button>
            <button class="btn-link btn-sm" onclick="reparseDocument(${h.id})">重传</button>
            <button class="btn-link btn-sm" style="color:var(--danger)" onclick="deleteDocument(${h.id})">删除</button>
          </div>
        </td>
      </tr>
    `).join('');

		renderUploadPagination();
		scheduleUploadHistoryRefresh(docs);
	} catch (e) {
		console.error('load upload history failed:', e);
		tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-sub)">加载失败，请刷新重试</td></tr>';
	}
}

function renderUploadPagination() {
	const container = $('#uploadPagination');
	if (!container) return;

	const total = uploadHistoryTotal;
	const page = uploadHistoryCurrentPage;
	const pageSize = 10;
	const totalPages = Math.max(1, Math.ceil(total / pageSize));

	if (total === 0) {
		container.innerHTML = '';
		return;
	}

	let html = `<span>共 ${total} 条</span>`;

	if (page > 1) {
		html += `<button class="page-btn" onclick="loadUploadHistory(${page - 1})">‹</button>`;
	} else {
		html += `<button class="page-btn" disabled>‹</button>`;
	}

	for (let i = 1; i <= totalPages; i++) {
		if (totalPages <= 7 || i === 1 || i === totalPages || (i >= page - 2 && i <= page + 2)) {
			if (i === page) {
				html += `<button class="page-btn active">${i}</button>`;
			} else {
				html += `<button class="page-btn" onclick="loadUploadHistory(${i})">${i}</button>`;
			}
		} else if (i === page - 3 || i === page + 3) {
			html += `<span>...</span>`;
		}
	}

	if (page < totalPages) {
		html += `<button class="page-btn" onclick="loadUploadHistory(${page + 1})">›</button>`;
	} else {
		html += `<button class="page-btn" disabled>›</button>`;
	}

	container.innerHTML = html;
}

async function viewDocument(docId) {
	const doc = currentDocs.find(d => d.id === docId);
	if (!doc) {
		toast('文档不存在', 'error');
		return;
	}

	if (doc.status === 'failed') {
		toast('失败原因：' + (doc.error_message || '未知错误'), 'error');
		return;
	}

	try {
		const data = await api.getJson(`/api/v1/knowledge/documents/${docId}/chunks/`);
		toast(`文档 ${docId} 共 ${data.total || 0} 个切片`, '');
	} catch (e) {
		toast('查看失败', 'error');
	}
}

async function reparseDocument(docId) {
	try {
		await api.postJson(`/api/v1/knowledge/documents/${docId}/reparse/`, {});
		toast('已触发重新解析', 'success');
		loadUploadHistory();
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

async function deleteDocument(docId) {
	if (!confirm('确定删除此文档？删除后不可恢复。')) return;
	try {
		await api.deleteJson(`/api/v1/knowledge/documents/${docId}/`);
		toast('文档已删除', 'success');
		loadUploadHistory();
	} catch (e) {
		toast(e.message || '删除失败', 'error');
	}
}

/* ============ 归属节点下拉填充 ============ */
async function initNodeSelect() {
	const sel = $('#nodeSelect');
	if (!sel) return;

	try {
		const data = await api.getJson('/api/v1/knowledge/nodes/tree/');
		allNodes = data.tree || [];

		sel.innerHTML = '<option value="">-- 请选择归属节点 --</option>';
		function walk(nodes, prefix) {
			nodes.forEach(n => {
				const p = prefix ? prefix + ' / ' + n.name : n.name;
				const opt = document.createElement('option');
				opt.value = n.id;
				opt.textContent = n.name ? p : p;
				sel.appendChild(opt);
				if (n.children && n.children.length) walk(n.children, p);
			});
		}
		walk(allNodes, '');
	} catch (e) {
		console.error('load nodes failed:', e);
		sel.innerHTML = '<option value="">加载节点失败</option>';
	}
}

/* ============ 拖拽区域 ============ */
function initDropZone() {
	const zone = $('#dropZone');
	const input = $('#fileInput');
	if (!zone || !input) return;

	let dragCounter = 0;

	zone.addEventListener('dragenter', (e) => {
		e.preventDefault();
		dragCounter++;
		zone.classList.add('dragover');
	});

	zone.addEventListener('dragleave', (e) => {
		e.preventDefault();
		dragCounter--;
		if (dragCounter <= 0) {
			dragCounter = 0;
			zone.classList.remove('dragover');
		}
	});

	zone.addEventListener('dragover', (e) => {
		e.preventDefault();
		zone.classList.add('dragover');
	});

	zone.addEventListener('drop', (e) => {
		e.preventDefault();
		dragCounter = 0;
		zone.classList.remove('dragover');
		const dt = e.dataTransfer;
		if (dt && dt.files && dt.files.length) {
			addFiles(dt.files);
		}
	});

	input.addEventListener('change', () => {
		if (input.files && input.files.length) {
			addFiles(input.files);
			input.value = '';
		}
	});
}

/* ============ 文件过滤 ============ */
const ALLOWED_EXTS = new Set([
	'pdf', 'doc', 'docx', 'md', 'txt', 'rst',
	'py', 'js', 'ts', 'jsx', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h',
	'yml', 'yaml', 'json', 'xml', 'toml', 'ini', 'conf', 'cfg',
	'sh', 'bat', 'ps1'
]);
const MAX_FILE_SIZE_MB = 100;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024;

function isAllowedFile(name) {
	const ext = name.split('.').pop().toLowerCase();
	return ALLOWED_EXTS.has(ext);
}

/* ============ 添加文件到列表 ============ */
function addFiles(fileList) {
	const maxFiles = 20;
	let added = 0, skipped = 0;

	for (const f of fileList) {
		if (pendingFiles.length >= maxFiles) {
			toast('最多同时上传 ' + maxFiles + ' 个文件', 'error');
			break;
		}
		if (f.size > MAX_FILE_SIZE_BYTES) {
			toast(`文件 ${f.name} 超过大小限制（最大 ${MAX_FILE_SIZE_MB} MB）`, 'error');
			skipped++;
			continue;
		}
		if (!isAllowedFile(f.name)) {
			skipped++;
			continue;
		}
		if (pendingFiles.some(p => p.name === f.name && p.size === f.size)) {
			skipped++;
			continue;
		}

		const info = {
			id: 'f' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
			file: f,
			name: f.name,
			size: f.size,
			type: fileTypeByExt(f.name),
			icon: fileIconByExt(f.name),
			status: 'pending'
		};
		pendingFiles.push(info);
		renderFileItem(info);
		added++;
	}

	if (added > 0) {
		updateFileCount();
		showUploadPanels();
	}
	if (skipped > 0) {
		toast('已跳过 ' + skipped + ' 个不支持或重复的文件', '');
	}
}

/* ============ 渲染单个文件项 ============ */
function renderFileItem(info) {
	const list = $('#fileList');
	const empty = $('#fileEmpty');
	if (empty) empty.style.display = 'none';

	const div = el('div', { class: 'file-item', id: info.id });
	div.innerHTML = `
    <div class="file-item-icon">${info.icon}</div>
    <div class="file-item-info">
      <div class="file-item-name" title="${escapeHtml(info.name)}">${escapeHtml(info.name)}</div>
      <div class="file-item-meta">${info.type} · ${formatSize(info.size)} · 待上传</div>
    </div>
    <div class="file-item-progress"><div class="file-item-progress-bar"></div></div>
    <div class="file-item-status"><span class="tag">等待中</span></div>
    <button class="btn-link btn-sm" style="color:var(--danger)" onclick="removeFile('${info.id}')">✕</button>`;
	list.appendChild(div);
}

/* ============ 移除文件 ============ */
function removeFile(id) {
	pendingFiles = pendingFiles.filter(f => f.id !== id);
	const el = document.getElementById(id);
	if (el) el.remove();
	updateFileCount();
}
window.removeFile = removeFile;

/* ============ 更新文件计数 ============ */
function updateFileCount() {
	const n = pendingFiles.length;
	const c = $('#fileCount');
	if (c) c.textContent = n;
	const empty = $('#fileEmpty');
	if (empty) empty.style.display = n ? 'none' : 'block';
	if (!n) hideUploadPanels();
}

/* ============ 面板显隐 ============ */
function showUploadPanels() {
	const panel = $('#uploadPanel');
	const opts = $('#uploadOptions');
	if (panel) panel.style.display = '';
	if (opts) opts.style.display = '';
}

function hideUploadPanels() {
	const panel = document.getElementById('uploadPanel');
	const opts = document.getElementById('uploadOptions');
	if (panel) panel.style.display = 'none';
	if (opts) opts.style.display = 'none';
}

function hideUploadOptionsOnly() {
	const opts = $('#uploadOptions');
	if (opts) opts.style.display = 'none';
}

/* ============ 上传完成收尾 ============ */
function finishUpload() {
	hideUploadPanels();
	pendingFiles = [];
	$('#fileList').innerHTML = '';
	updateFileCount();
	loadUploadHistory();
}

/* ============ 清空列表 ============ */
function clearFileList() {
	pendingFiles = [];
	$('#fileList').innerHTML = '';
	updateFileCount();
	toast('已清空', '');
}

/* ============ 单文件上传（支持冲突重试） ============ */
async function uploadSingleFile(info, nodeId, visibility, token, forceUpload = false) {
	const bar = document.querySelector('#' + info.id + ' .file-item-progress-bar');

	const formData = new FormData();
	formData.append('file', info.file, info.name);
	formData.append('node_id', nodeId);
	formData.append('visibility', visibility);
	if (forceUpload) {
		formData.append('force_upload', 'true');
	}

	const xhr = new XMLHttpRequest();
	uploadingXhrs.push(xhr);

	try {
		const responseData = await new Promise((resolve, reject) => {
			xhr.upload.addEventListener('progress', (e) => {
				if (e.lengthComputable && bar) {
					const pct = Math.round((e.loaded / e.total) * 100);
					bar.style.width = pct + '%';
				}
			});
			xhr.addEventListener('load', () => {
				if (xhr.status >= 200 && xhr.status < 300) {
					const data = JSON.parse(xhr.responseText || '{}');
					if (data.status === 'failed') {
						reject(new Error(data.detail || '上传失败'));
					} else {
						resolve(data);
					}
				} else {
					reject(new Error(xhr.status + ' ' + (JSON.parse(xhr.responseText || '{}').detail || xhr.statusText)));
				}
			});
			xhr.addEventListener('error', () => reject(new Error('网络错误')));
			xhr.open('POST', '/api/v1/knowledge/documents/upload/');
			xhr.setRequestHeader('Authorization', 'Bearer ' + token);
			xhr.send(formData);
		});

		return responseData;
	} finally {
		uploadingXhrs = uploadingXhrs.filter(x => x !== xhr);
	}
}

/* ============ 开始上传 ============ */
let uploadingXhrs = [];
let isUploading = false;

function getUploadBtn() {
	return document.querySelector('#uploadPanel .btn-primary');
}

function setUploadBtnDisabled(disabled) {
	const btn = getUploadBtn();
	if (btn) {
		btn.disabled = disabled;
		btn.style.opacity = disabled ? '0.6' : '';
		btn.style.cursor = disabled ? 'not-allowed' : '';
	}
}

async function startUpload() {
	if (isUploading) { toast('上传进行中，请稍候', ''); return; }
	if (!pendingFiles.length) { toast('请先添加文件', 'error'); return; }

	const nodeId = $('#nodeSelect')?.value;
	if (!nodeId) { toast('请选择归属节点', 'error'); return; }

	const visRadio = $('#visRadios .upload-radio.selected input');
	const visibility = visRadio ? ['self', 'team', 'dept', 'all'].indexOf(visRadio.value) + 1 : 1;

	const token = localStorage.getItem('rag_access');
	if (!token) { toast('请先登录', 'error'); return; }

	toast('正在准备上传...', '');

	try {
		await checkCeleryStatusBeforeUpload();
	} catch (e) {
		if (e.message === '用户取消上传') {
			return;
		}
		console.warn('Celery 状态检查失败:', e);
	}

	isUploading = true;
	setUploadBtnDisabled(true);
	uploadingXhrs = [];

	// 隐藏上传选项面板
	hideUploadOptionsOnly();

	const total = pendingFiles.length;
	let completedCount = 0;
	let successCount = 0;
	let failCount = 0;
	const uploadedDocIds = [];
	const maxConcurrent = 3;

	showGlobalProgress();

	const uploadPromises = pendingFiles.map(info => async () => {
		const bar = document.querySelector('#' + info.id + ' .file-item-progress-bar');
		const statusEl = document.querySelector('#' + info.id + ' .file-item-status');
		const metaEl = document.querySelector('#' + info.id + ' .file-item-meta');
		if (statusEl) statusEl.innerHTML = '<span class="tag tag-info">上传中</span>';

		try {
			let responseData = await uploadSingleFile(info, nodeId, visibility, token);

			// -- 处理冲突响应：弹出确认对话框 --
			while (responseData && responseData.conflict) {
				const existing = responseData.existing;
				let msg;
				if (responseData.conflict === 'duplicate') {
					msg = [
						'已存在相同内容的文件：' + (existing.file_name || ''),
						'上传者：' + (existing.owner_name || '未知'),
						'上传时间：' + (existing.created_at || ''),
						'',
						'是否继续上传（将创建新记录）？'
					].join('\n');
				} else {
					msg = [
						'此文件「' + (existing.file_name || '') + '」之前已被删除',
						'原上传者：' + (existing.owner_name || '未知'),
						'原上传时间：' + (existing.created_at || ''),
						'',
						'是否恢复此文件并重新解析？'
					].join('\n');
				}

				if (!confirm(msg)) {
					if (statusEl) statusEl.innerHTML = '<span class="tag tag-default">已跳过</span>';
					if (metaEl) metaEl.innerHTML = info.type + ' · ' + formatSize(info.size) + ' · 已取消（文件已存在）';
					return 'skipped';
				}

				// 用户确认，使用 force_upload 重新上传
				if (statusEl) statusEl.innerHTML = '<span class="tag tag-info">确认上传</span>';
				responseData = await uploadSingleFile(info, nodeId, visibility, token, true);
			}

			if (bar) bar.style.width = '100%';

			if (responseData.celery_ok === false) {
				if (statusEl) statusEl.innerHTML = '<span class="tag tag-warning">等待解析</span>';
				if (metaEl) metaEl.innerHTML = info.type + ' · ' + formatSize(info.size) + ' · Celery未启动，等待手动触发';
			} else {
				if (statusEl) statusEl.innerHTML = '<span class="tag tag-success">已上传</span>';
				if (metaEl) metaEl.innerHTML = info.type + ' · ' + formatSize(info.size) + ' · 解析中...';
			}

			if (responseData.document_id) {
				uploadedDocIds.push({ id: responseData.document_id, infoId: info.id });
			}
			return 'success';
		} catch (err) {
			if (statusEl) statusEl.innerHTML = '<span class="tag tag-danger">失败</span>';
			if (metaEl) metaEl.innerHTML = info.type + ' · ' + formatSize(info.size) + ' · ' + err.message;
			return 'failed';
		} finally {
			completedCount++;
			updateGlobalProgress(completedCount, total);
		}
	});

	for (let i = 0; i < uploadPromises.length; i += maxConcurrent) {
		const batch = uploadPromises.slice(i, i + maxConcurrent);
		const results = await Promise.all(batch.map(fn => fn()));
		results.forEach(r => {
			if (r === 'success') successCount++;
			else if (r === 'skipped') { /* 跳过，不计入成功/失败 */ }
			else failCount++;
		});

		if (!isUploading) {
			hideGlobalProgress();
			setUploadBtnDisabled(false);
			toast('上传已取消', '');
			finishUpload();
			return;
		}
	}

	try {
		hideGlobalProgress();
		isUploading = false;
		setUploadBtnDisabled(false);

		if (failCount === 0) {
			toast('全部 ' + successCount + ' 个文件上传成功', 'success');
		} else {
			toast('上传完成：' + successCount + ' 成功，' + failCount + ' 失败', failCount === total ? 'error' : '');
		}

		if (uploadedDocIds.length > 0) {
			startStatusPolling(uploadedDocIds);
			// 确保上传历史持续刷新，直到所有文档完成
			startUploadHistoryPolling();
		}
		finishUpload();
	} catch (e) {
		toast('上传收尾异常: ' + e.message, 'error');
	}
}

function cancelUpload() {
	isUploading = false;
	setUploadBtnDisabled(false);
	uploadingXhrs.forEach(xhr => {
		try { xhr.abort(); } catch (e) { }
	});
	uploadingXhrs = [];
	hideGlobalProgress();
	finishUpload();
}

function showGlobalProgress() {
	const panel = $('#uploadPanel');
	if (!panel) return;

	const existing = panel.querySelector('.global-progress');
	if (existing) existing.remove();

	const html = `
    <div class="global-progress" style="margin-top:12px;padding:12px;background:var(--primary-light);border-radius:var(--radius)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:13px;color:var(--text-primary)">整体上传进度</span>
        <button class="btn-link btn-sm" onclick="cancelUpload()" style="color:var(--danger)">取消上传</button>
      </div>
      <div style="width:100%;height:6px;background:var(--border);border-radius:3px;overflow:hidden">
        <div id="globalProgressBar" style="height:100%;background:var(--primary);transition:width 0.3s ease;width:0%"></div>
      </div>
      <div id="globalProgressText" style="text-align:right;font-size:11.5px;color:var(--text-sub);margin-top:4px">0/${pendingFiles.length}</div>
    </div>
  `;
	panel.insertAdjacentHTML('beforeend', html);
}

function updateGlobalProgress(done, total) {
	const bar = $('#globalProgressBar');
	const text = $('#globalProgressText');
	if (bar) bar.style.width = Math.round((done / total) * 100) + '%';
	if (text) text.textContent = done + '/' + total;
}

function hideGlobalProgress() {
	const el = document.querySelector('.global-progress');
	if (el) el.remove();
}

/* ============ 页面加载时检查 Celery 状态 ============ */
async function checkAndShowCeleryStatus() {
	try {
		const data = await api.getJson('/api/v1/knowledge/celery/status/');
		updateCeleryStatusUI(data.celery_ok, data.detail);
		checkPendingDocs();
	} catch (e) {
		updateCeleryStatusUI(false, '状态检查失败');
	}
}

function updateCeleryStatusUI(ok, detail) {
	const iconEl = $('#celeryIcon');
	const textEl = $('#celeryText');
	const tagEl = iconEl?.parentElement;
	const retryBtn = $('#retryPendingBtn');

	if (!iconEl || !textEl || !tagEl) return;

	if (ok) {
		iconEl.textContent = '✅';
		textEl.textContent = detail || 'Celery 运行正常';
		tagEl.className = 'tag tag-success';
	} else {
		iconEl.textContent = '❌';
		textEl.textContent = detail || 'Celery 未启动';
		tagEl.className = 'tag tag-danger';
	}

	if (retryBtn) {
		retryBtn.style.display = ok ? 'none' : 'inline-flex';
	}
}

async function checkPendingDocs() {
	try {
		const data = await api.getJson('/api/v1/knowledge/documents/pending/');
		const retryBtn = $('#retryPendingBtn');
		if (retryBtn && data.total > 0) {
			retryBtn.style.display = 'inline-flex';
			retryBtn.textContent = `🔄 重试 ${data.total} 个待处理文档`;
		}
	} catch (e) {
		console.warn('检查待处理文档失败:', e);
	}
}

/* ============ Celery 状态检查 ============ */
async function checkCeleryStatusBeforeUpload() {
	try {
		const data = await api.getJson('/api/v1/knowledge/celery/status/');
		if (!data.celery_ok) {
			if (!confirm('检测到文档解析服务未启动或连接失败，文档上传后将无法自动解析。\n\n是否继续上传？\n（上传后可在历史列表中点击"重传"按钮手动触发解析）')) {
				throw new Error('用户取消上传');
			}
		}
	} catch (e) {
		if (e.message !== '用户取消上传') {
			console.warn('Celery 状态检查失败:', e);
		} else {
			throw e;
		}
	}
}

/* ============ 状态轮询 ============ */
let pollingInterval = null;

function startStatusPolling(docIds) {
	if (pollingInterval) clearInterval(pollingInterval);

	pollingInterval = setInterval(async () => {
		try {
			const data = await api.getJson('/api/v1/knowledge/documents/pending/');
			const pendingIds = new Set(data.documents?.map(d => d.id) || []);

			const allDone = docIds.every(item => !pendingIds.has(item.id));
			if (allDone) {
				clearInterval(pollingInterval);
				pollingInterval = null;
				loadUploadHistory();
				return;
			}
		} catch (e) {
			console.warn('状态轮询失败:', e);
		}
	}, 5000);
}

function stopStatusPolling() {
	if (pollingInterval) {
		clearInterval(pollingInterval);
		pollingInterval = null;
	}
}

/* ============ 上传历史自动刷新 ============ */
const PROCESSING_STATUSES = new Set(['pending', 'parsing', 'desensitizing', 'chunking', 'embedding', 'embedding_failed']);
let historyRefreshInterval = null;

function scheduleUploadHistoryRefresh(docs) {
	clearHistoryRefresh();
	const hasProcessing = docs.some(d => PROCESSING_STATUSES.has(d.status));
	if (hasProcessing) {
		historyRefreshInterval = setInterval(() => {
			loadUploadHistory(uploadHistoryCurrentPage);
		}, 10000);
	}
}

function clearHistoryRefresh() {
	if (historyRefreshInterval) {
		clearInterval(historyRefreshInterval);
		historyRefreshInterval = null;
	}
}

/**
 * 启动上传历史持续刷新（页面加载时调用）
 * 只要存在进行中的文档，就每10秒刷新一次
 */
function startUploadHistoryPolling() {
	clearHistoryRefresh();
	historyRefreshInterval = setInterval(() => {
		try {
			// 检查是否有进行中的文档
			const hasProcessing = currentDocs?.some(d => PROCESSING_STATUSES.has(d.status));
			if (hasProcessing) {
				loadUploadHistory(uploadHistoryCurrentPage);
			} else {
				// 所有文档都已完成，停止刷新
				clearHistoryRefresh();
			}
		} catch (e) {
			console.warn('上传历史刷新失败:', e);
		}
	}, 10000);
}

document.addEventListener('beforeunload', () => {
	stopStatusPolling();
	clearHistoryRefresh();
});
document.addEventListener('visibilitychange', () => {
	if (document.hidden) {
		stopStatusPolling();
		clearHistoryRefresh();
	}
});

/* ============ 重试待处理文档 ============ */
async function retryPendingDocs() {
	try {
		const data = await api.postJson('/api/v1/knowledge/documents/pending/', {});
		if (data.ok) {
			toast(`已重新触发 ${data.retriggered} 个待处理文档的解析`, 'success');
			if (data.failed && data.failed.length > 0) {
				toast(`有 ${data.failed.length} 个文档触发失败`, 'error');
			}
			loadUploadHistory();
		} else {
			toast(data.detail || '操作失败', 'error');
		}
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ============ 可见范围选择 ============ */
function pickVis(elm) {
	$$('#visRadios .upload-radio').forEach(r => r.classList.remove('selected'));
	elm.classList.add('selected');
	elm.querySelector('input').checked = true;
}

/* ============ 工具函数 ============ */
function fileTypeByExt(name) {
	const ext = name.split('.').pop().toLowerCase();
	const map = {
		pdf: 'PDF', doc: 'Word', docx: 'Word',
		md: 'Markdown', txt: 'TXT', rst: 'TXT',
		py: 'Python', js: 'JavaScript', ts: 'TypeScript',
		jsx: 'React JSX', tsx: 'React TSX', java: 'Java',
		go: 'Go', rs: 'Rust', c: 'C', cpp: 'C++', h: 'C/C++ Header',
		yml: 'YAML', yaml: 'YAML', json: 'JSON', xml: 'XML',
		toml: 'TOML', ini: 'INI', conf: '配置', cfg: '配置',
		sh: 'Shell', bat: 'Batch', ps1: 'PowerShell'
	};
	return map[ext] || ext.toUpperCase();
}

function fileIconByExt(name) {
	const ext = name.split('.').pop().toLowerCase();
	if (ext === 'pdf') return '📕';
	if (['doc', 'docx'].includes(ext)) return '📄';
	if (ext === 'md') return '📝';
	if (ext === 'txt' || ext === 'rst') return '📃';
	if (['yml', 'yaml'].includes(ext)) return '⚙️';
	if (ext === 'json') return '📊';
	if (['py', 'js', 'ts', 'jsx', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h', 'sh', 'bat', 'ps1'].includes(ext)) return '💻';
	return '📄';
}

function formatSize(bytes) {
	if (bytes < 1024) return bytes + ' B';
	if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
	return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function fileTypeIcon(t) {
	const map = { 'pdf': '📕', 'docx': '📄', 'markdown': '📝', 'txt': '📃', 'code': '💻', 'config': '⚙️', 'other': '📄' };
	return map[t] || '📄';
}

function visTag(v) {
	const map = { 1: '仅本人', 2: '团队可见', 3: '部门可见', 4: '全平台' };
	const tagMap = { 1: 'default', 2: 'primary', 3: 'info', 4: 'success' };
	return `<span class="tag tag-${tagMap[v] || 'default'}">${map[v] || v}</span>`;
}

function statusTag(s) {
	const map = { 'done': 'success', 'parsing': 'warning', 'failed': 'danger', 'pending': 'default', 'desensitizing': 'warning', 'chunking': 'warning', 'embedding': 'warning' };
	const labelMap = { 'done': '已完成', 'parsing': '解析中', 'failed': '失败', 'pending': '等待', 'desensitizing': '脱敏中', 'chunking': '切片中', 'embedding': '向量化中' };
	return `<span class="tag tag-${map[s] || 'default'}">${labelMap[s] || s}</span>`;
}

