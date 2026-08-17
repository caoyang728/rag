/* ============ 文档上传页 ============ */

/** 待上传的文件列表：{ id, file, name, size, type, icon } */
let pendingFiles = [];
let uploadHistoryCurrentPage = 1;
let uploadHistoryTotal = 0;
/** 上传历史每页条数（可经公共分页组件切换，后端按 page_size 切片，上限 100） */
let uploadHistoryPageSize = 20;
/** 分页组件是否已初始化（首次 render 绑定回调，后续仅 update 状态） */
let uploadPaginationInited = false;
/** 请求序号：防止快速连续操作（翻页/切条数/改筛选）时旧请求后返回覆盖新状态 */
let uploadRequestSeq = 0;
/** 归属节点下拉中可选的文件夹节点 ID 集合（仅 FOLDER 可选，用于 startUpload 二次拦截组织节点） */
let folderNodeIds = new Set();
/** 状态统计缓存：避免重复请求，key 为状态值，value 为数量 */
let statusCountsCache = null;
/** 状态统计上次请求时间（5秒节流） */
let statusCountsLastFetch = 0;

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
	// 状态统计：点击下拉框时触发（5秒节流）
	initStatusCountsOnFocus();
	// 队列深度展示（异步刷新，不阻塞页面主流程）
	refreshQueueDepth();
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

/* ============ 状态统计（点击下拉框时触发，5秒节流） ============ */
function initStatusCountsOnFocus() {
	const filterStatus = $('#filterStatus');
	if (!filterStatus) return;

	filterStatus.addEventListener('focus', async () => {
		const now = Date.now();
		// 5秒节流：距上次请求不足5秒则跳过
		if (now - statusCountsLastFetch < 5000) return;
		statusCountsLastFetch = now;

		await fetchStatusCounts();
	});
}

async function fetchStatusCounts() {
	try {
		const data = await api.getJson('/api/v1/knowledge/documents/status_counts/');
		statusCountsCache = data;
		updateStatusCountsUI(data);
	} catch (e) {
		console.warn('获取状态统计失败:', e);
	}
}

function updateStatusCountsUI(counts) {
	const filterStatus = $('#filterStatus');
	if (!filterStatus || !counts) return;

	// 状态值与统计 key 的映射
	const statusMap = {
		'pending': 'pending',
		'parsing': 'parsing',
		'chunking': 'chunking',
		'embedding': 'embedding',
		'embedding_failed': 'embedding_failed',
		'failed': 'failed',
		'pending_team': 'pending_team',
		'pending_compliance': 'pending_compliance',
		'rejected': 'rejected',
		'graph_pending': 'graph_pending',
		'graph_extracting': 'graph_extracting',
		'graph_failed': 'graph_failed',
		'wiki_pending': 'wiki_pending',
		'wiki_extracting': 'wiki_extracting',
		'wiki_failed': 'wiki_failed',
		'done': 'done',
	};

	// 更新每个选项的文本
	Array.from(filterStatus.options).forEach(opt => {
		const countKey = statusMap[opt.value];
		if (!countKey) return; // "全部状态" 选项无 value，跳过

		// 获取原始文本（不含数量部分）
		const baseText = opt.dataset.baseText || opt.textContent.replace(/\s*\(\d+\)$/, '');
		// 首次访问时保存原始文本
		if (!opt.dataset.baseText) {
			opt.dataset.baseText = baseText;
		}

		const count = counts[countKey] || 0;
		opt.textContent = count > 0 ? `${baseText} (${count})` : baseText;
	});
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
	const seq = ++uploadRequestSeq;
	const tbody = $('#uploadHistoryBody');
	if (!tbody) return;

	try {
		let url = `/api/v1/knowledge/documents/?page=${page}&page_size=${uploadHistoryPageSize}`;
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
		if (seq !== uploadRequestSeq) return; // 已有更新的请求发出，丢弃本次旧响应
		const docs = data.results || data;
		currentDocs = docs;
		const count = data.count || (docs.length || 0);
		// 数据量减少（文档被删除/恢复）导致当前页越界时，回退到最后一页重新加载
		if (page > Math.max(1, Math.ceil(count / uploadHistoryPageSize))) {
			uploadHistoryCurrentPage = Math.max(1, Math.ceil(count / uploadHistoryPageSize));
			loadUploadHistory(uploadHistoryCurrentPage);
			return;
		}
		uploadHistoryTotal = count;
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
			row.setAttribute('data-doc-id', h.id);
			row.querySelector('.up-row-icon').textContent = fileTypeIcon(h.file_type);
			// 活跃标记：同组存在多版本时标注当前生效版本；非活跃旧版本标注灰色「旧版本」
			let versionMarker = '';
			if (h.version_count > 1) {
				versionMarker = h.is_active
					? ' <span class="tag tag-success" style="margin-left:4px">活跃</span>'
					: ' <span class="tag" style="margin-left:4px;background:#eee;color:#888">旧版本</span>';
			}
			row.querySelector('.up-row-name').innerHTML = escapeHtml(h.file_name) + versionMarker;
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
				row.querySelector('.up-row-progress').onclick = null;
				row.querySelector('.up-row-progress').style.cursor = 'default';
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
				row.querySelector('.up-row-status').innerHTML = uploadStatusTag(h);
				row.querySelector('.up-row-time').textContent = formatDate(h.created_at);
				row.querySelector('.up-row-view').onclick = function () { viewDocument(h.id); };
				row.querySelector('.up-row-progress').onclick = function () { showDocProgress(h.id); };
				row.querySelector('.up-row-reparse').onclick = function () { reparseDocument(h.id); };
				row.querySelector('.up-row-delete').onclick = function () { deleteDocument(h.id); };
				// 版本切换入口：同组存在多版本时展示（版本历史弹窗由 common.js 提供）
				if (h.version_count > 1) {
					const verBtn = document.createElement('button');
					verBtn.className = 'btn-link btn-sm';
					verBtn.textContent = '版本';
					verBtn.onclick = function () { showVersionModal(h.id); };
					row.querySelector('.table-actions').appendChild(verBtn);
				}
			}
			tbody.appendChild(row);
		});

		renderUploadPagination();
		// 切换页码后将表格滚动层滚回顶部，避免用户在旧位置看到新数据
		const tableScroll = document.querySelector('#uploadHistorySection .table-scroll');
		if (tableScroll) tableScroll.scrollTop = 0;
	startUploadPolling();
	} catch (e) {
		if (seq !== uploadRequestSeq) return;
		console.error('load upload history failed:', e);
		tbody.innerHTML = '<tr><td colspan="8" class="text-center text-sub">加载失败，请刷新重试</td></tr>';
	}
}

// 上传历史分页：复用公共 Pagination 组件（common.js）。
// 首次 render 绑定回调，后续 update 仅刷新页码状态；每页条数切换由后端按 page_size 重新切片
function renderUploadPagination() {
	const totalPages = Math.max(1, Math.ceil(uploadHistoryTotal / uploadHistoryPageSize));
	if (!uploadPaginationInited) {
		Pagination.render({
			container: '#uploadPagination',
			page: uploadHistoryCurrentPage,
			totalPages: totalPages,
			total: uploadHistoryTotal,
			pageSize: uploadHistoryPageSize,
			align: 'center',
			// pageSizeOptions: [10, 20, 50],
			onPageChange(p) { loadUploadHistory(p); },
			onPageSizeChange(size) { uploadHistoryPageSize = size; loadUploadHistory(1); },
		});
		uploadPaginationInited = true;
	} else {
		Pagination.update({
			page: uploadHistoryCurrentPage,
			totalPages: totalPages,
			total: uploadHistoryTotal,
			pageSize: uploadHistoryPageSize,
		});
	}
}

function viewDocument(docId) {
	const doc = currentDocs.find(d => d.id === docId);
	if (!doc) {
		toast('文档不存在', 'error');
		return;
	}

	if (doc.status === 'failed') {
		toast('失败原因：' + (doc.error_message || '未知错误'), 'error');
		return;
	}

	// 打开文档预览弹窗（元信息 + 原文内容，公共模块 preview-doc.js 实现）
	previewDoc(docId);
}

/* ============ 文档处理进度弹窗（8 步步骤条） ============ */

// 上传历史行内状态：处理维度 + 审核维度聚合，每篇文档唯一归属一个状态
// 归属顺序：处理异常 > 审核驳回（终态）> 处理全部完成后按审核维度归类 > 处理未完成按流水线展示
// 与下拉筛选/后端统计口径一致：待审核/待复核仅计入处理全部完成的文档
function uploadStatusTag(h) {
	const s = h.status || 'pending';
	// 处理异常最优先（可重试）
	if (s === 'failed') return '<span class="tag tag-danger">解析失败</span>';
	if (s === 'embedding_failed') return '<span class="tag tag-danger">向量构建失败</span>';
	// 驳回为终态，无论处理进行到哪一步都优先展示；按驳回阶段区分文案
	if ((h.audit_status || '') === 'rejected') {
		return h.reject_stage === 'compliance'
			? '<span class="tag tag-danger">复核驳回</span>'
			: '<span class="tag tag-danger">审核驳回</span>';
	}

	// 处理全部完成（解析 + 图谱/wiki 均 done/skipped）时按审核维度归属
	const g = h.graph_status || 'pending';
	const w = h.wiki_status || 'pending';
	const processingDone = s === 'done' &&
		(g === 'done' || g === 'skipped') &&
		(w === 'done' || w === 'skipped');
	if (processingDone) {
		const audit = h.audit_status || 'pending_team';
		if (audit === 'pending_team') return '<span class="tag tag-info">待审核</span>';
		if (audit === 'pending_compliance') return '<span class="tag tag-warning">待合规复核</span>';
		return '<span class="tag tag-success">已完成</span>';
	}
	// 处理未完成：按处理流水线展示（解析中/切片中/图谱等待构建等）
	return pipelineStatusTag(h);
}

// 步骤状态：done=完成 / active=进行中 / todo=待办 / failed=失败 / skipped=跳过
// 三线并行：处理线(status) → 审核线(audit_status) → 构建线(graph_status + wiki_status)
function showDocProgress(docId) {
	const doc = currentDocs.find(function (d) { return d.id === docId; });
	if (!doc) {
		toast('文档不存在', 'error');
		return;
	}

	const s = doc.status || 'pending';
	const audit = doc.audit_status || 'pending_team';
	const g = doc.graph_status || 'pending';
	const w = doc.wiki_status || 'pending';

	// 处理线：解析 / 切片 / 向量化，按主流水线 status 判定（脱敏并入解析）
	let parseState, parseText;
	if (s === 'failed') { parseState = 'failed'; parseText = '解析失败'; }
	else if (s === 'parsing' || s === 'desensitizing') { parseState = 'active'; parseText = '解析中'; }
	else if (s === 'pending') { parseState = 'todo'; parseText = '等待解析'; }
	else { parseState = 'done'; parseText = '解析完成'; }

	let chunkState, chunkText;
	if (s === 'failed') { chunkState = 'failed'; chunkText = '解析失败'; }
	else if (s === 'chunking') { chunkState = 'active'; chunkText = '切片中'; }
	else if (s === 'pending' || s === 'parsing' || s === 'desensitizing') { chunkState = 'todo'; chunkText = '等待切片'; }
	else { chunkState = 'done'; chunkText = '切片完成'; }

	let embedState, embedText;
	if (s === 'embedding_failed') { embedState = 'failed'; embedText = '向量构建失败'; }
	else if (s === 'embedding') { embedState = 'active'; embedText = '向量构建中'; }
	else if (s === 'done') { embedState = 'done'; embedText = '向量构建完成'; }
	else { embedState = 'todo'; embedText = '等待向量化'; }

	// 审核线：双审（团队审核 → 合规复核），按 audit_status 判定（驳回分支见下方统一处理）
	let audit1State, audit1Text;
	if (audit === 'pending_team') { audit1State = 'active'; audit1Text = '待审核'; }
	else if (audit === 'pending_compliance' || audit === 'passed') { audit1State = 'done'; audit1Text = '审核通过'; }
	else { audit1State = 'todo'; audit1Text = '待审核'; }

	let audit2State, audit2Text;
	if (audit === 'pending_compliance') { audit2State = 'active'; audit2Text = '待合规复核'; }
	else if (audit === 'passed') { audit2State = 'done'; audit2Text = '复核通过'; }
	else { audit2State = 'todo'; audit2Text = '等待复核'; }

	// 构建线：图谱 / Wiki 仅在解析完成(status=done)后由节点级防抖任务驱动
	const buildStep = function (st, name) {
		if (s !== 'done') return { state: 'todo', text: '等待解析完成' };
		if (st === 'extracting') return { state: 'active', text: name + '中' };
		if (st === 'done') return { state: 'done', text: '构建完成' };
		if (st === 'failed') return { state: 'failed', text: '构建失败' };
		if (st === 'skipped') return { state: 'skipped', text: '未启用' };
		return { state: 'todo', text: '等待构建' };
	};
	const graphStep = buildStep(g, '图谱构建');
	const wikiStep = buildStep(w, 'Wiki生成');

	const steps = [
		{ name: '上传', state: 'done', text: '已上传' },
		{ name: '解析/脱敏', state: parseState, text: parseText },
		{ name: '切片', state: chunkState, text: chunkText },
		{ name: '向量化', state: embedState, text: embedText },
		{ name: '团队审核', state: audit1State, text: audit1Text },
		{ name: '合规复核', state: audit2State, text: audit2Text },
		{ name: '图谱构建', state: graphStep.state, text: graphStep.text },
		{ name: 'Wiki生成', state: wikiStep.state, text: wikiStep.text },
	];

	// 驳回为终态：仅驳回阶段标注失败文案（团队审核驳回/合规复核驳回），其余步骤统一显示横杠
	if (audit === 'rejected') {
		const rejectStage = doc.reject_stage || 'team';
		const failIdx = rejectStage === 'compliance' ? 5 : 4;  // 步骤下标：4=团队审核, 5=合规复核
		const failText = rejectStage === 'compliance' ? '复核驳回' : '审核驳回';
		steps.forEach(function (step, i) {
			if (i === failIdx) { step.state = 'failed'; step.text = failText; }
			else { step.state = 'skipped'; step.text = '—'; }
		});
	}

	// 状态标记映射：图标 + 标签样式
	const icons = { done: '✓', active: '◐', todo: '○', failed: '✗', skipped: '—' };
	const tagCls = { done: 'tag-success', active: 'tag-info', todo: 'tag-default', failed: 'tag-danger', skipped: 'tag-default' };

	const titleEl = document.getElementById('docProgressModalTitle');
	if (titleEl) titleEl.textContent = '处理进度 · ' + (doc.title || doc.file_name || '');
	const container = document.getElementById('docProgressSteps');
	if (!container) return;
	container.innerHTML = '';
	steps.forEach(function (step) {
		const div = document.createElement('div');
		div.className = 'progress-step' + (step.state === 'active' ? ' is-active' : '');
		div.innerHTML =
			'<div class="progress-step-icon ' + step.state + '">' + (icons[step.state] || '○') + '</div>' +
			'<div class="progress-step-name">' + escapeHtml(step.name) + '</div>' +
			'<span class="tag ' + (tagCls[step.state] || 'tag-default') + '">' + escapeHtml(step.text) + '</span>';
		container.appendChild(div);
	});
	showModal('docProgressModal');
}

/* ============ 文档预览（预览弹窗由公共模块 preview-doc.js 实现） ============ */
// 预览元信息来源：上传历史列表（currentDocs）中按 id 查找，找不到返回 null
function getDocForPreview(id) {
	return Promise.resolve((currentDocs || []).find(function (x) { return x.id === id; }) || null);
}

async function reparseDocument(docId) {
	try {
		// 乐观更新：立即将本地状态更新为 pending，避免用户看到标签仍然是失败
		const docIdx = currentDocs.findIndex(d => d.id === docId);
		if (docIdx !== -1) {
			currentDocs[docIdx].status = 'pending';
			currentDocs[docIdx].error_message = '';
			// 同步更新 DOM 中的状态标签
			const row = document.querySelector(`#uploadHistoryBody tr[data-doc-id="${docId}"]`);
			if (row) {
				const statusEl = row.querySelector('.up-row-status');
				if (statusEl) statusEl.innerHTML = pipelineStatusTag({ status: 'pending' });
			}
		}
		// 立即重启轮询（确保不被 hasProcessingDocuments 判断停止）
		startUploadPolling();

		await api.postJson(`/api/v1/knowledge/documents/${docId}/reparse/`, {});
		toast('已触发重新解析', 'success');
		// 最终用服务端数据覆盖一次，保证一致性
		loadUploadHistory(uploadHistoryCurrentPage);
	} catch (e) {
		// 失败时回滚乐观更新
		if (docIdx !== -1) {
			const row = document.querySelector(`#uploadHistoryBody tr[data-doc-id="${docId}"]`);
			if (row) {
				const statusEl = row.querySelector('.up-row-status');
				if (statusEl) statusEl.innerHTML = pipelineStatusTag(currentDocs[docIdx]);
			}
		}
		toast(e.message || '操作失败', 'error');
	}
}

async function restoreDocument(docId) {
	showConfirmDialog({
		title: '恢复文档',
		bannerType: 'warning',
		bannerIcon: '↺',
		bannerText: '确定恢复此文档？',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认恢复', type: 'primary', onClick: async function (ctx) {
					ctx.close();
					try {
						await api.postJson(`/api/v1/knowledge/documents/${docId}/restore/`, {});
						toast('文档已恢复', 'success');
						loadUploadHistory();
					} catch (e) {
						toast(e.message || '操作失败', 'error');
					}
				}
			}
		]
	});
}

async function hardDeleteDocument(docId) {
	showConfirmDialog({
		title: '物理删除文档',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: '⚠️ 警告：物理删除后无法恢复，确定继续？',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除', type: 'danger', onClick: async function (ctx) {
					ctx.close();
					try {
						await api.postJson(`/api/v1/knowledge/documents/${docId}/hard_delete/`, {});
						toast('物理删除成功', 'success');
						loadUploadHistory();
					} catch (e) {
						toast(e.message || '操作失败', 'error');
					}
				}
			}
		]
	});
}

async function deleteDocument(docId) {
	showConfirmDialog({
		title: '删除文档',
		bannerType: 'danger',
		bannerIcon: '🗑',
		bannerText: '确定删除此文档？删除后不可恢复。',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除', type: 'danger', onClick: async function (ctx) {
					ctx.close();
					try {
						await api.deleteJson(`/api/v1/knowledge/documents/${docId}/`);
						toast('文档已删除', 'success');
						loadUploadHistory();
					} catch (e) {
						toast(e.message || '删除失败', 'error');
					}
				}
			}
		]
	});
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
		const myTeamIds = u.team ? [u.team.id] : [];

		// 可管理文档的角色：超级管理员 / 文档管理员
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

		sel.innerHTML = '<option value="">-- 请选择归属文件夹 --</option>';
		folderNodeIds.clear();

		// 判断节点子树中是否存在文件夹（FOLDER），用于决定组织节点是否展示为灰色分支标题
		function hasFolder(n) {
			if (n.node_kind === 'FOLDER') return true;
			return (n.children || []).some(hasFolder);
		}

		// 树形层级缩进（每层 3 个全角空格）
		function indent(level) { return '&nbsp;'.repeat((level - 1) * 3); }

		// 归属节点以树形结构展示（从知识库根开始），避免层级过深时路径拼接难以辨认：
		// - 知识库根 / 部门 / 团队：作为灰色不可选的层级标题，仅当其子树内含文件夹时展示
		// - 文件夹（FOLDER）：可选，文档只能上传到文件夹
		function walk(nodes, level) {
			nodes.forEach(n => {
				if (n.node_level === 1) {
					// 根节点：仅当子树中存在文件夹时展示为分支标题，否则整棵树无可用文件夹
					if (hasFolder(n)) {
						const rootOpt = document.createElement('option');
						rootOpt.disabled = true;
						rootOpt.innerHTML = indent(level) + '📚 ' + escapeHtml(n.name || '知识库');
						sel.appendChild(rootOpt);
					}
					if (n.children && n.children.length) walk(n.children, level + 1);
					return;
				}
				if (n.node_kind === 'FOLDER') {
					const opt = document.createElement('option');
					opt.value = n.id;
					opt.innerHTML = indent(level) + '📁 ' + escapeHtml(n.name);
					folderNodeIds.add(String(n.id));
					if (n.id === defaultNodeId) {
						opt.selected = true;
					}
					sel.appendChild(opt);
				} else {
					// 组织节点（部门/团队）无文件夹后代时直接隐藏，避免空分支干扰选择；
					// 有文件夹后代的组织节点保持可选，选中时提示"不可选择节点"并重置（见 change 监听）
					if (hasFolder(n)) {
						const orgOpt = document.createElement('option');
						orgOpt.value = 'org:' + n.id;
						orgOpt.innerHTML = indent(level) + (n.node_level === 2 ? '🏢 ' : '👥 ') + escapeHtml(n.name);
						sel.appendChild(orgOpt);
					}
				}
				if (n.children && n.children.length) walk(n.children, level + 1);
			});
		}
		walk(filteredNodes, 1);

		// 组织节点（value 前缀 org:）虽可选但不可作为归属，选中即提示并重置回占位
		sel.addEventListener('change', function () {
			if (String(sel.value).startsWith('org:')) {
				toast('不可选择节点，请先在节点上创建文件夹', 'warning');
				sel.value = '';
			}
		});
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
	'csv', 'xlsx', 'xls',
	'ppt', 'pptx',
	'wps', 'et', 'dps',
	'py', 'js', 'ts', 'jsx', 'tsx', 'java', 'go', 'rs', 'c', 'cpp', 'h',
	'yml', 'yaml', 'json', 'xml', 'toml', 'ini', 'conf', 'cfg',
	'sh', 'bat', 'ps1', 'css',
	'jpg', 'jpeg', 'png', 'bmp', 'webp'
]);
const MAX_FILE_SIZE_MB = 100;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024;

function isAllowedFile(name) {
	const ext = name.split('.').pop().toLowerCase();
	return ALLOWED_EXTS.has(ext);
}

/* ============ 添加文件到列表 ============ */
function addFiles(fileList) {
	// 单批次文件数上限：允许最多 100 个文件同时排队上传（每个文件独立请求，后端无批次限制）
	const maxFiles = 100;
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
	// 本次上传面板与上传历史互斥（共用中部滚动区）：显示面板时隐藏上传历史
	const history = document.getElementById('uploadHistorySection');
	if (history) history.classList.add('hidden');
}

function hideUploadPanels() {
	const panel = document.getElementById('uploadPanel');
	const opts = document.getElementById('uploadOptions');
	if (panel) panel.classList.add('hidden');
	if (opts) opts.classList.add('hidden');
	// 面板隐藏后恢复上传历史展示
	const history = document.getElementById('uploadHistorySection');
	if (history) history.classList.remove('hidden');
}

/* ============ 上传完成收尾 ============ */
function finishUpload() {
	hideUploadPanels();
	pendingFiles = [];
	$('#fileList').innerHTML = '';
	updateFileCount();
	loadUploadHistory();
	// 上传完成后立即刷新队列积压，让用户直观看到有多少任务在排队
	refreshQueueDepth();
}

/* ============ 清空列表 ============ */
function clearFileList() {
	pendingFiles = [];
	$('#fileList').innerHTML = '';
	updateFileCount();
	toast('已清空', '');
}

/* ============ 单文件上传（自动作为新版本） ============ */
async function uploadSingleFile(info, nodeId, visibility, token, depts = [], teams = [], forceNewVersion = false) {
	const bar = document.querySelector('#' + info.id + ' .file-item-progress-bar');

	const formData = new FormData();
	formData.append('file', info.file, info.name);
	formData.append('node_id', nodeId);
	formData.append('visible_scope', visibility);
	// 相同内容文件默认被后端拦截（409 duplicate_file），用户选择"强制新建版本"时携带该标记跳过拦截
	formData.append('force_new_version', forceNewVersion ? 'true' : 'false');
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
				let data = {};
				try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* 解析失败按空对象处理 */ }
				// 同内容重复上传：返回标记由调用方弹窗选择（取消/查看现有/强制新建版本）
				if (xhr.status === 409 && data.code === 'duplicate_file') {
					resolve({ duplicate: true, existing: data.existing || {} });
					return;
				}
				if (xhr.status >= 200 && xhr.status < 300) {
					if (data.status === 'failed') {
						reject(new Error(data.detail || '上传失败'));
					} else {
						resolve(data);
					}
				} else {
					reject(new Error(xhr.status + ' ' + (data.detail || xhr.statusText)));
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
	// 前端二次拦截：仅文件夹（FOLDER）可直接上传文档，组织节点（部门/团队）需先选其下文件夹
	if (!folderNodeIds.has(String(nodeId))) {
		toast('文档只能上传到文件夹中，请选择文件夹节点', 'warning');
		return;
	}

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

	const total = pendingFiles.length;

	// 瞬时反馈用 info（蓝色 3 秒自动关闭），避免无类型 toast 永久停留；
	// 上传进度由全局进度条持续反馈，不需要常驻提示
	toast('正在上传 ' + total + ' 个文件', 'info');

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

	let completedCount = 0;
	let successCount = 0;
	let failCount = 0;
	const failReasons = [];   // 失败原因列表，最终 toast 中展示第一条
	const uploadedDocIds = [];
	const maxConcurrent = 3;

	showGlobalProgress();

	const uploadPromises = pendingFiles.map(info => async () => {
		const bar = document.querySelector('#' + info.id + ' .file-item-progress-bar');
		const statusEl = document.querySelector('#' + info.id + ' .file-item-status');
		const metaEl = document.querySelector('#' + info.id + ' .file-item-meta');
		if (statusEl) statusEl.innerHTML = '<span class="tag tag-info">上传中</span>';

		try {
			let responseData = await uploadSingleFile(info, nodeId, visibility, token, depts, teams);

			// 同内容重复上传：弹窗让用户选择（取消 / 查看现有文件 / 强制新建版本）
			if (responseData && responseData.duplicate) {
				const choice = await showDuplicateFileDialog(responseData.existing || {});
				if (choice === 'cancel') {
					if (statusEl) statusEl.innerHTML = '<span class="tag tag-default">已跳过</span>';
					if (metaEl) metaEl.innerHTML = info.type + ' · ' + formatSize(info.size) + ' · 已跳过（内容重复）';
					return 'skipped';
				}
				// force：携带 force_new_version=true 重新提交，绕过同内容拦截
				responseData = await uploadSingleFile(info, nodeId, visibility, token, depts, teams, true);
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
			if (metaEl) metaEl.innerHTML = escapeHtml(info.type) + ' · ' + formatSize(info.size) + ' · ' + escapeHtml(errorMsg);
			// 记录失败原因（含文件名），最终汇总 toast 展示，避免用户只看到"x 失败"不明原因
			if (failReasons.length < 3) {
				failReasons.push((info.name || '文件') + '：' + errorMsg);
			}
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
			// 告警 toast（黄色 5s 自动关闭），附第一条失败原因，让用户知道为什么失败
			const reasonText = failReasons.length ? '：' + failReasons[0] : '';
			toast('上传完成：' + successCount + ' 成功，' + failCount + ' 失败' + reasonText, 'warning');
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
	} catch (e) {
		updateCeleryStatusUI(false, '状态检查失败');
	}
}

function updateCeleryStatusUI(ok, detail) {
	const iconEl = $('#celeryIcon');
	const textEl = $('#celeryText');
	const tagEl = iconEl?.parentElement;

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
}

/* ============ 队列深度展示（页头 tag，低成本 Redis 读取，任何登录用户可访问） ============ */
async function refreshQueueDepth() {
	try {
		const data = await api.getJson('/api/v1/knowledge/queues/depth/');
		renderQueueDepthBrief(data.queues || {});
	} catch (e) {
		// 队列深度非关键信息，失败时隐藏且不打扰用户
		console.warn('queue depth refresh failed:', e);
		const el = document.getElementById('queueDepthBrief');
		if (el) el.classList.add('hidden');
	}
}

function renderQueueDepthBrief(queues) {
	const el = document.getElementById('queueDepthBrief');
	if (!el) return;
	const names = Object.keys(queues);
	if (!names.length) {
		el.classList.add('hidden');
		return;
	}
	const parseSize = Number((queues.parse || {}).size) || 0;
	const total = names.reduce(function (acc, n) { return acc + (Number(queues[n].size) || 0); }, 0);
	el.classList.remove('hidden');
	if (total > 0) {
		el.innerHTML = '📥 队列积压 <b>' + total + '</b>' + (parseSize ? '（解析 ' + parseSize + '）' : '');
		el.className = 'tag tag-warning flex items-center gap-6';
	} else {
		el.textContent = '📥 队列空闲';
		el.className = 'tag tag-success flex items-center gap-6';
	}
}

/* ============ Celery 状态检查 ============ */
async function checkCeleryStatusBeforeUpload() {
	try {
		const data = await api.getJson('/api/v1/knowledge/celery/status/');
		if (!data.celery_ok) {
			// 使用二次确认弹窗（common.css 样式）替代原生 confirm
			const confirmed = await new Promise(function (resolve) {
				showConfirmDialog({
					title: '解析服务未就绪',
					bannerType: 'warning',
					bannerIcon: '⚠',
					bannerText: '检测到文档解析服务未启动或连接失败，文档上传后将无法自动解析。',
					bodyHtml: '<p class="form-hint">是否继续上传？上传后可在历史列表中点击"重传"按钮手动触发解析。</p>',
					buttons: [
						{ text: '取消上传', type: 'cancel', onClick: function () { resolve(false); } },
						{
							text: '继续上传', type: 'primary', onClick: function (ctx) {
								ctx.close();
								resolve(true);
							}
						}
					]
				});
			});
			if (!confirmed) {
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
// 图谱/wiki 构建阶段仍在进行的状态（解析完成后继续轮询直至全部完成/失败/跳过）
const BUILDING_STATUSES = new Set(['pending', 'extracting']);
let uploadPollingInterval = null;

function hasProcessingDocuments(docs) {
	const targetDocs = docs || currentDocs;
	return targetDocs?.some(d =>
		PROCESSING_STATUSES.has(d.status) ||
		(d.status === 'done' && (BUILDING_STATUSES.has(d.graph_status) || BUILDING_STATUSES.has(d.wiki_status)))
	);
}

function startUploadPolling() {
	if (uploadPollingInterval) clearInterval(uploadPollingInterval);

	uploadPollingInterval = setInterval(() => {
		try {
			if (hasProcessingDocuments()) {
				loadUploadHistory(uploadHistoryCurrentPage);
				// 处理期间同步刷新队列积压（低成本 Redis 读取）
				refreshQueueDepth();
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
	const myTeamIds = u.team ? [u.team.id] : [];

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
		pdf: 'PDF', doc: 'Word', docx: 'Word', wps: 'WPS文字',
		md: 'Markdown', txt: 'TXT', rst: 'TXT',
		csv: 'CSV', xlsx: 'Excel', xls: 'Excel', et: 'WPS表格',
		ppt: 'PPT', pptx: 'PPT', dps: 'WPS演示',
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
	if (['doc', 'docx', 'wps'].includes(ext)) return '📄';
	if (ext === 'md') return '📝';
	if (ext === 'txt' || ext === 'rst') return '📃';
	if (['csv', 'xlsx', 'xls', 'et'].includes(ext)) return '📊';
	if (['ppt', 'pptx', 'dps'].includes(ext)) return '📽️';
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
	const tagMap = { 'team': 'default', 'dept': 'info', 'public': 'primary' };
	return `<span class="tag tag-${tagMap[v] || 'default'}">${escapeHtml(map[v] || v)}</span>`;
}

function statusTag(s) {
	// 兼容旧调用：仅传状态字符串时构造最小对象交给共享流水线渲染
	return pipelineStatusTag(typeof s === 'string' ? { status: s } : s);
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

/* ============ 同内容重复上传对话框 ============ */
function showDuplicateFileDialog(existing) {
	return new Promise((resolve) => {
		const tpl = document.getElementById('tmpl-duplicate-dialog').content;
		const overlay = document.importNode(tpl, true).querySelector('.conflict-overlay');
		const dialog = overlay.querySelector('.conflict-dialog');

		dialog.querySelector('.con-filename').textContent = existing.file_name || '';
		dialog.querySelector('.con-owner').textContent = existing.owner_name || '未知';
		dialog.querySelector('.con-time').textContent = formatDate(existing.created_at) || '';

		// 点击"查看现有文件"只打开预览弹窗，不关闭本对话框：
		// 用户查看完内容后可继续决定"取消"或"强制新建版本"，
		// 因此本 Promise 只在取消/强制两个动作时 resolve
		dialog.querySelector('.dup-btn-view').addEventListener('click', function () {
			previewDoc(existing.id);
		});
		dialog.querySelector('.dup-btn-cancel').addEventListener('click', function () { closeAndResolve('cancel'); });
		dialog.querySelector('.dup-btn-force').addEventListener('click', function () { closeAndResolve('force'); });

		const escHandler = (e) => {
			if (e.key === 'Escape') {
				closeAndResolve('cancel');
			}
		};
		document.addEventListener('keydown', escHandler);

		function closeAndResolve(value) {
			document.body.removeChild(overlay);
			document.removeEventListener('keydown', escHandler);
			resolve(value);
		}

		document.body.appendChild(overlay);
	});
}

