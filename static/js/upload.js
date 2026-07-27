/* ============ 文档上传页 ============ */

/** 待上传的文件列表：{ id, file, name, size, type, icon } */
let pendingFiles = [];
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
let uploadHistoryIncludeDeleted = false;
let currentDocs = [];

async function initUploadPage() {
	await loadUploadHistory();
	initSearchFilter();
	await loadFilterOptions();
	const visRadio = document.querySelector('#visRow .upload-radio-inline.selected input');
	if (visRadio && visRadio.value === 'org') {
		await loadUploadDeptTeamOptions();
	}
}

function initSearchFilter() {
	const searchInput = $('#searchInput');

	if (searchInput) {
		searchInput.addEventListener('input', (e) => {
			uploadHistorySearch = e.target.value.trim();
			loadUploadHistory(1);
		});
	}

	startUploadPolling();
}

async function loadFilterOptions() {
	try {
		const deptSelect = $('#filterDept');
		
		if (deptSelect) {
			const depts = await api.getJson('/api/v1/knowledge/documents/available_depts/');
			depts.forEach(d => {
				const opt = document.createElement('option');
				opt.value = d.id;
				opt.textContent = d.name;
				deptSelect.appendChild(opt);
			});
		}
	} catch (e) {
		console.warn('加载筛选选项失败:', e);
	}
}

function toggleShowDeleted(checkbox) {
	uploadHistoryIncludeDeleted = checkbox.checked;
	loadUploadHistory(1);
}

async function loadUploadHistory(page = 1) {
	const tbody = $('#uploadHistoryBody');
	if (!tbody) return;

	try {
		let url = `/api/v1/knowledge/documents/?page=${page}&page_size=20`;
		if (uploadHistorySearch) {
			url += `&search=${encodeURIComponent(uploadHistorySearch)}`;
		}
		
		const filterFileType = $('#filterFileType')?.value || '';
		const filterDept = $('#filterDept')?.value || '';
		const filterOwner = $('#filterOwner')?.value || '';
		const filterVisible = $('#filterVisible')?.value || '';
		const filterStatus = $('#filterStatus')?.value || '';
		
		if (filterFileType) {
			url += `&file_type=${filterFileType}`;
		}
		if (filterDept) {
			url += `&dept_id=${filterDept}`;
		}
		if (filterOwner) {
			url += `&owner=${filterOwner}`;
		}
		if (filterVisible) {
			url += `&visible_scope=${filterVisible}`;
		}
		if (filterStatus) {
			url += `&status=${filterStatus}`;
		}
		if (uploadHistoryIncludeDeleted) {
			url += `&include_deleted=true`;
		}

		const data = await api.getJson(url);
		const docs = data.results || data;
		currentDocs = docs;
		uploadHistoryTotal = data.count || (docs.length || 0);
		uploadHistoryCurrentPage = page;

		if (!docs || docs.length === 0) {
			tbody.innerHTML = '<tr><td colspan="8" class="text-center text-sub">暂无上传记录</td></tr>';
			renderUploadPagination();
			return;
		}

		const rowTpl = document.getElementById('tmpl-upload-row').content;
		tbody.innerHTML = '';
		docs.forEach(function (h) {
			const row = document.importNode(rowTpl, true).querySelector('tr');
			row.querySelector('.up-row-icon').textContent = fileTypeIcon(h.file_type);
			row.querySelector('.up-row-name').textContent = h.file_name;
			row.querySelector('.up-row-type').textContent = fileTypeByExt(h.file_name);
			row.querySelector('.up-row-node').textContent = h.node_name || '-';
			row.querySelector('.up-row-owner').textContent = h.owner_name || '-';
			row.querySelector('.up-row-vis').innerHTML = visTag(h.visible_scope);
			
			if (h.is_deleted) {
				row.classList.add('deleted-row');
				row.querySelector('.up-row-status').innerHTML = '<span class="tag tag-danger">已删除</span>';
				row.querySelector('.up-row-time').textContent = '删除于 ' + formatDate(h.delete_time);
				row.querySelector('.up-row-view').onclick = null;
				row.querySelector('.up-row-view').style.cursor = 'default';
				row.querySelector('.up-row-reparse').onclick = null;
				row.querySelector('.up-row-reparse').style.cursor = 'default';
				row.querySelector('.up-row-delete').textContent = '🔄 恢复';
				row.querySelector('.up-row-delete').onclick = function () { restoreDocument(h.id); };
				
				const daysSinceDelete = h.delete_time ? Math.floor((Date.now() - new Date(h.delete_time)) / (1000 * 60 * 60 * 24)) : 0;
				const hasFilePath = h.file_path && h.file_path !== '';
				
				if (hasFilePath) {
					if (daysSinceDelete >= 30) {
						const hardDelBtn = document.createElement('button');
						hardDelBtn.className = 'btn-link btn-sm text-red';
						hardDelBtn.textContent = '🗑️ 物理删除';
						hardDelBtn.onclick = function () { hardDeleteDocument(h.id); };
						row.querySelector('.table-actions').appendChild(hardDelBtn);
					} else {
						const remaining = 30 - daysSinceDelete;
						const hintSpan = document.createElement('span');
						hintSpan.className = 'text-xs text-sub ml-4';
						hintSpan.textContent = `(${remaining}天后可物理删除)`;
						row.querySelector('.table-actions').appendChild(hintSpan);
					}
				}
			} else {
				row.querySelector('.up-row-status').innerHTML = statusTag(h.status);
				row.querySelector('.up-row-time').textContent = formatDate(h.created_at);
				row.querySelector('.up-row-view').onclick = function () { viewDocument(h.id); };
				row.querySelector('.up-row-reparse').onclick = function () { reparseDocument(h.id); };
				row.querySelector('.up-row-delete').onclick = function () { deleteDocument(h.id); };
			}
			tbody.appendChild(row);
		});

		renderUploadPagination();
	startUploadPolling();
	} catch (e) {
		console.error('load upload history failed:', e);
		tbody.innerHTML = '<tr><td colspan="8" class="text-center text-sub">加载失败，请刷新重试</td></tr>';
	}
}

function renderUploadPagination() {
	const container = $('#uploadPagination');
	if (!container) return;

	const total = uploadHistoryTotal;
	const page = uploadHistoryCurrentPage;
	const pageSize = 20;
	const totalPages = Math.max(1, Math.ceil(total / pageSize));

	if (total === 0) {
		container.innerHTML = '';
		return;
	}

	const tpl = document.getElementById('tmpl-upload-pagination').content;
	const frag = document.importNode(tpl, true);

	frag.querySelector('.up-pag-total-num').textContent = total;

	const prevBtn = frag.querySelector('.up-pag-prev');
	if (page <= 1) {
		prevBtn.disabled = true;
	} else {
		prevBtn.onclick = function () { loadUploadHistory(page - 1); };
	}

	const pagesDiv = frag.querySelector('.up-pag-pages');
	for (var i = 1; i <= totalPages; i++) {
		if (totalPages <= 7 || i === 1 || i === totalPages || (i >= page - 2 && i <= page + 2)) {
			var btn = document.createElement('button');
			btn.className = 'page-btn' + (i === page ? ' active' : '');
			btn.textContent = i;
			if (i !== page) {
				btn.onclick = (function (p) { return function () { loadUploadHistory(p); }; })(i);
			}
			pagesDiv.appendChild(btn);
		} else if (i === page - 3 || i === page + 3) {
			var span = document.createElement('span');
			span.textContent = '...';
			pagesDiv.appendChild(span);
		}
	}

	const nextBtn = frag.querySelector('.up-pag-next');
	if (page >= totalPages) {
		nextBtn.disabled = true;
	} else {
		nextBtn.onclick = function () { loadUploadHistory(page + 1); };
	}

	container.innerHTML = '';
	container.appendChild(frag);
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

async function restoreDocument(docId) {
	if (!confirm('确定恢复此文档？')) return;
	try {
		await api.postJson(`/api/v1/knowledge/documents/${docId}/restore/`, {});
		toast('文档已恢复', 'success');
		loadUploadHistory();
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

async function hardDeleteDocument(docId) {
	if (!confirm('⚠️ 警告：物理删除后无法恢复，确定继续？')) return;
	try {
		await api.postJson(`/api/v1/knowledge/documents/${docId}/hard_delete/`, {});
		toast('物理删除成功', 'success');
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

		const roles = getUserRoles();
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		const myDeptId = u.department_id;
		const myTeamIds = (u.teams || []).map(function (t) { return t.team__id; });

		const isAdmin = roles.includes('super_admin') || roles.includes('kb_admin');
		const isDeptManager = roles.includes('dept_manager');
		const isTeamLeader = roles.includes('team_leader');

		let filteredNodes = allNodes;
		let defaultNodeId = null;

		if (!isAdmin) {
			if (isDeptManager && myDeptId) {
				filteredNodes = allNodes.map(function (kbNode) {
					const deptNode = kbNode.children ? kbNode.children.find(d => d.ref_id === myDeptId) : null;
					if (deptNode) {
						defaultNodeId = deptNode.id;
						return { ...kbNode, children: [deptNode] };
					}
					return null;
				}).filter(n => n);
			} else if ((isTeamLeader || !isAdmin) && myTeamIds.length > 0) {
				filteredNodes = allNodes.map(function (kbNode) {
					if (!kbNode.children) return null;
					const deptNode = kbNode.children.find(d => d.ref_id === myDeptId);
					if (!deptNode || !deptNode.children) return null;
					const teamNodes = deptNode.children.filter(t => myTeamIds.includes(t.ref_id));
					if (teamNodes.length > 0) {
						defaultNodeId = teamNodes[0].id;
						return {
							...kbNode,
							children: [{ ...deptNode, children: teamNodes }]
						};
					}
					return null;
				}).filter(n => n);
			}
		}

		sel.innerHTML = '<option value="">-- 请选择归属节点 --</option>';
		function walk(nodes, prefix, depth) {
			nodes.forEach(n => {
				if (n.node_level === 1) {
					if (n.children && n.children.length) walk(n.children, '', depth + 1);
					return;
				}
				const indent = '&nbsp;'.repeat((depth - 1) * 4);
				const p = prefix ? prefix + ' / ' + n.name : n.name;
				const opt = document.createElement('option');
				opt.value = n.id;
				opt.innerHTML = indent + (n.name ? p : p);
				if (n.id === defaultNodeId) {
					opt.selected = true;
				}
				sel.appendChild(opt);
				if (n.children && n.children.length) walk(n.children, p, depth + 1);
			});
		}
		walk(filteredNodes, '', 1);
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
	'pdf', 'doc', 'docx', 'md', 'markdown', 'txt', 'rst',
	'py', 'js', 'ts', 'jsx', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h',
	'yml', 'yaml', 'json', 'xml', 'toml', 'ini', 'conf', 'cfg',
	'sh', 'bat', 'ps1', 'css'
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
	if (empty) empty.classList.add('hidden');

	const tpl = document.getElementById('tmpl-file-item').content;
	const div = tpl.cloneNode(true).querySelector('.file-item');
	div.id = info.id;
	div.querySelector('.up-fi-icon').textContent = info.icon;
	const nameEl = div.querySelector('.up-fi-name');
	nameEl.textContent = info.name;
	nameEl.title = info.name;
	div.querySelector('.up-fi-meta').textContent = info.type + ' · ' + formatSize(info.size) + ' · 待上传';
	div.querySelector('.up-fi-remove').onclick = function () { removeFile(info.id); };
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
	if (empty) empty.classList.toggle('hidden', n > 0);
	if (!n) hideUploadPanels();
}

/* ============ 面板显隐 ============ */
function showUploadPanels() {
	const panel = $('#uploadPanel');
	const opts = $('#uploadOptions');
	if (panel) panel.classList.remove('hidden');
	if (opts) opts.classList.remove('hidden');
}

function hideUploadPanels() {
	const panel = document.getElementById('uploadPanel');
	const opts = document.getElementById('uploadOptions');
	if (panel) panel.classList.add('hidden');
	if (opts) opts.classList.add('hidden');
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

/* ============ 单文件上传（自动作为新版本） ============ */
async function uploadSingleFile(info, nodeId, visibility, token, depts = [], teams = []) {
	const bar = document.querySelector('#' + info.id + ' .file-item-progress-bar');

	const formData = new FormData();
	formData.append('file', info.file, info.name);
	formData.append('node_id', nodeId);
	formData.append('visible_scope', visibility);
	if (depts.length > 0) {
		depts.forEach(function (id) { formData.append('visibility_depts', id); });
	}
	if (teams.length > 0) {
		teams.forEach(function (id) { formData.append('visibility_teams', id); });
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

	const visRadio = $('#visRow .upload-radio-inline.selected input');
	const visValue = visRadio ? visRadio.value : 'org';
	const visMap = { 'org': 'dept', 'public': 'public' };
	const visibility = visMap[visValue] || 'dept';

	const token = localStorage.getItem('rag_access');
	if (!token) { toast('请先登录', 'error'); return; }

	let depts = [];
	let teams = [];
	if (visValue === 'org') {
		if (!uploadMultiSelect) {
			await loadUploadDeptTeamOptions();
		}
		depts = [];
		document.querySelectorAll('#uploadDeptPanel input:checked').forEach(function (cb) {
			depts.push(parseInt(cb.value));
		});
		teams = [];
		document.querySelectorAll('#uploadTeamPanel input:checked').forEach(function (cb) {
			teams.push(parseInt(cb.value));
		});
	}

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
			const responseData = await uploadSingleFile(info, nodeId, visibility, token, depts, teams);

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
			// 获取详细错误信息
			let errorMsg = err.message || '上传失败';
			if (err.response) {
				if (err.response.detail) {
					errorMsg = err.response.detail;
				} else if (typeof err.response === 'string') {
					errorMsg = err.response;
				} else if (err.response.error) {
					errorMsg = err.response.error;
				}
			}
			if (metaEl) metaEl.innerHTML = info.type + ' · ' + formatSize(info.size) + ' · ' + errorMsg;
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
			startUploadPolling();
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
	// 重置文件列表中的进度条状态，保留文件供重新上传
	const fileItems = document.querySelectorAll('.file-item');
	fileItems.forEach(item => {
		const bar = item.querySelector('.file-item-progress-bar');
		if (bar) bar.style.width = '0%';
		const status = item.querySelector('.file-item-status');
		if (status) status.innerHTML = '<span class="tag tag-default">待上传</span>';
	});
}

function showGlobalProgress() {
	const panel = $('#uploadPanel');
	if (!panel) return;

	const existing = panel.querySelector('.global-progress');
	if (existing) existing.remove();

	const tpl = document.getElementById('tmpl-upload-progress').content;
	const frag = document.importNode(tpl, true);
	frag.querySelector('.up-prog-cancel').onclick = cancelUpload;
	frag.querySelector('.up-prog-text').textContent = '0/' + pendingFiles.length;
	panel.appendChild(frag);
}

function updateGlobalProgress(done, total) {
	const bar = document.querySelector('.global-progress .up-prog-bar-fill');
	const text = document.querySelector('.global-progress .up-prog-text');
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
		retryBtn.classList.toggle('hidden', ok);
	}
}

async function checkPendingDocs() {
	try {
		const data = await api.getJson('/api/v1/knowledge/documents/pending/');
		const retryBtn = $('#retryPendingBtn');
		if (retryBtn && data.total > 0) {
			retryBtn.classList.remove('hidden');
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

/* ============ 上传状态轮询（合并状态轮询和历史刷新）=========== */
const PROCESSING_STATUSES = new Set(['pending', 'parsing', 'desensitizing', 'chunking', 'embedding', 'embedding_failed']);
let uploadPollingInterval = null;

function hasProcessingDocuments(docs) {
	const targetDocs = docs || currentDocs;
	return targetDocs?.some(d => PROCESSING_STATUSES.has(d.status));
}

function startUploadPolling() {
	if (uploadPollingInterval) clearInterval(uploadPollingInterval);

	uploadPollingInterval = setInterval(() => {
		try {
			if (hasProcessingDocuments()) {
				loadUploadHistory(uploadHistoryCurrentPage);
			} else {
				stopUploadPolling();
			}
		} catch (e) {
			console.warn('上传状态轮询失败:', e);
		}
	}, 5000);
}

function stopUploadPolling() {
	if (uploadPollingInterval) {
		clearInterval(uploadPollingInterval);
		uploadPollingInterval = null;
	}
}

window.addEventListener('beforeunload', () => {
	stopUploadPolling();
});
document.addEventListener('visibilitychange', () => {
	if (document.hidden) {
		stopUploadPolling();
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
	$$('#visRow .upload-radio-inline').forEach(r => r.classList.remove('selected'));
	elm.classList.add('selected');
	elm.querySelector('input').checked = true;

	var visValue = elm.querySelector('input').value;
	var orgSelect = document.getElementById('uploadOrgSelect');
	if (visValue === 'org') {
		orgSelect.style.display = 'flex';
		loadUploadDeptTeamOptions();
	} else {
		orgSelect.style.display = 'none';
	}
}

var uploadDeptList = [];
var uploadTeamList = [];

function loadUploadDeptTeamOptions() {
	return new Promise((resolve) => {
		api.getJson('/api/v1/knowledge/documents/allowed_visibility/').then(function (res) {
			uploadDeptList = res.departments || [];
			uploadTeamList = res.teams || [];
			initDeptTeamSelect();
			resolve();
		}).catch(function (e) {
			console.error('Failed to load allowed visibility:', e);
			uploadDeptList = [];
			uploadTeamList = [];
			resolve();
		});
	});
}

function initDeptTeamSelect() {
	const roles = getUserRoles();
	const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
	const myDeptId = u.department_id;
	const myTeamIds = (u.teams || []).map(function (t) { return t.team__id; });

	const deptTrigger = document.querySelector('#uploadDeptSelect .multi-select-trigger');
	const teamTrigger = document.querySelector('#uploadTeamSelect .multi-select-trigger');

	if (!uploadMultiSelect) {
		uploadMultiSelect = createDeptTeamMultiSelect({
			prefix: 'upload',
			deptList: uploadDeptList,
			teamList: uploadTeamList
		});
	} else {
		uploadMultiSelect.setDeptList(uploadDeptList);
		uploadMultiSelect.setTeamList(uploadTeamList);
	}

	if (myDeptId && myTeamIds.length > 0) {
		uploadMultiSelect.renderDeptList([myDeptId]);
		uploadMultiSelect.renderTeamList(myTeamIds, [myDeptId]);
	} else if (myDeptId) {
		uploadMultiSelect.renderDeptList([myDeptId]);
		uploadMultiSelect.renderTeamList([], [myDeptId]);
	} else {
		uploadMultiSelect.renderDeptList([]);
		uploadMultiSelect.renderTeamList([], []);
	}

	deptTrigger.classList.remove('disabled');
	teamTrigger.classList.remove('disabled');
}

// multi-select组件实例
var uploadMultiSelect = null;

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
		sh: 'Shell', bat: 'Batch', ps1: 'PowerShell',
		css: 'CSS',
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
	const map = { 'team': '团队', 'dept': '部门', 'public': '公开' };
	const tagMap = { 'team': 'default', 'dept': 'info', 'public': 'success' };
	return `<span class="tag tag-${tagMap[v] || 'default'}">${map[v] || v}</span>`;
}

function statusTag(s) {
	const map = { 'done': 'success', 'parsing': 'warning', 'failed': 'danger', 'pending': 'default', 'desensitizing': 'warning', 'chunking': 'warning', 'embedding': 'warning' };
	const labelMap = { 'done': '已完成', 'parsing': '解析中', 'failed': '失败', 'pending': '等待', 'desensitizing': '脱敏中', 'chunking': '切片中', 'embedding': '向量化中' };
	return `<span class="tag tag-${map[s] || 'default'}">${labelMap[s] || s}</span>`;
}

/* ============ 已删除文件三选项对话框 ============ */
function showDeletedFileDialog(existing) {
	return new Promise((resolve) => {
		const tpl = document.getElementById('tmpl-conflict-dialog').content;
		const overlay = document.importNode(tpl, true).querySelector('.conflict-overlay');
		const dialog = overlay.querySelector('.conflict-dialog');

		dialog.querySelector('.con-filename').textContent = existing.file_name || '';
		dialog.querySelector('.con-owner').textContent = existing.owner_name || '未知';
		dialog.querySelector('.con-time').textContent = existing.created_at || '';

		const closeAndResolve = (value) => {
			document.body.removeChild(overlay);
			resolve(value);
		};

		dialog.querySelector('.con-btn-cancel').addEventListener('click', function () { closeAndResolve('cancel'); });
		dialog.querySelector('.con-btn-new').addEventListener('click', function () { closeAndResolve('create_new'); });
		dialog.querySelector('.con-btn-restore').addEventListener('click', function () { closeAndResolve('restore'); });

		const escHandler = (e) => {
			if (e.key === 'Escape') {
				closeAndResolve('cancel');
				document.removeEventListener('keydown', escHandler);
			}
		};
		document.addEventListener('keydown', escHandler);

		document.body.appendChild(overlay);
	});
}

