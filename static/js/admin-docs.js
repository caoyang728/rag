/* ============================================================================
 * admin-docs.js —— 文档审核页面
 *
 * 页面访问权限（前后端双重校验）：
 * - 仅 super_admin / kb_admin / dept_manager / team_leader 可见
 * - 普通用户直接跳回首页并提示无权限
 *
 * 功能模块：
 * 1. Tab 列表：待审核（pending_team/pending_compliance）/ 已驳回（rejected）/ 审核记录（doc_audit 操作日志）
 * 2. 列表分页：复用公共 Pagination 组件（common.js），翻页/切条数走请求序号守卫
 * 3. 详情弹窗：展示文档元信息 + 驳回理由（已驳回时）+ 摘要预览入口
 * 4. 摘要预览：点击「文档摘要」区域打开二级预览弹窗（公共模块 preview-doc.js）
 * 5. 审核动作：通过（备注选填）/ 驳回（理由必填，支持 Ctrl+Enter 提交）
 * ============================================================================ */

// 当前正在审核的文档对象
let _currentDoc = null;
// 提交防重锁（防止审核通过/驳回重复提交）
let _submitting = false;

/* ============ Tab 列表状态 ============ */
// 当前 tab：pending=待审核 / rejected=已驳回 / records=审核记录
let _auditTab = 'pending';
// 分页状态
let _auditPage = 1;
let _auditPageSize = 20;
let _auditTotal = 0;
let _paginationInited = false;
// 当前列表数据（供弹窗与预览元信息查找）
let _currentDocs = [];
// 请求序号守卫：异步响应返回时丢弃过期数据，防止旧响应覆盖新状态
let _requestSeq = 0;
// 搜索关键字（所有 tab 生效）
let _auditKeyword = '';
// 阶段筛选：pending_team=待审核 / pending_compliance=待复核（仅待审核 tab 生效）
let _auditStage = '';
// 搜索输入防抖定时器（300ms 后自动触发搜索）
let _auditSearchTimer = null;

/* ============ 页面启动 ============ */
document.addEventListener('DOMContentLoaded', () => {
	// 页面级权限校验：仅管理角色可进入
	if (!_canAccessPage()) {
		toast('您没有权限访问文档审核', 'error');
		setTimeout(() => { window.location.href = '/chat/'; }, 800);
		return;
	}

	// 顶栏 / 侧栏 / 全局搜索 已由 common.js 的 DOMContentLoaded 注入，无需重复

	// 同步阶段筛选可见性（默认待审核 tab 展示）
	_syncStageFilterVisibility();

	// 加载待审核文档列表
	loadAuditList();
});

/* ---------- 阶段筛选可见性：仅待审核 tab 展示 ---------- */
function _syncStageFilterVisibility() {
	const stageFilter = $('#docStageFilter');
	if (stageFilter) {
		stageFilter.style.display = _auditTab === 'pending' ? '' : 'none';
	}
}

/* ---------- 页面级权限判断 ---------- */
function _canAccessPage() {
	// 对齐需求：超级管理员、知识管理员、部门经理、团队组长可见
	return hasAnyRole('super_admin', 'kb_admin', 'dept_manager', 'team_leader');
}

/* ============================================================================
 * Tab 列表 —— 切换 / 加载 / 渲染
 * ============================================================================ */

/* ---------- 切换 tab（重置页码并加载） ---------- */
function switchAuditTab(tab) {
	if (!['pending', 'rejected', 'records'].includes(tab)) return;
	_auditTab = tab;
	_auditPage = 1;
	// 切换 tab 后需重建分页（容器可能已被 Pagination.destroy 清空）
	_paginationInited = false;
	// 切换 tab 时清空搜索与阶段筛选，避免旧条件串到其他列表
	_auditKeyword = '';
	_auditStage = '';
	const searchInput = $('#docSearchInput');
	if (searchInput) searchInput.value = '';
	const stageFilter = $('#docStageFilter');
	if (stageFilter) stageFilter.value = '';
	_syncStageFilterVisibility();
	document.querySelectorAll('.tab-item').forEach(el => {
		el.classList.toggle('active', el.getAttribute('data-tab') === tab);
	});
	loadAuditList();
}

/* ---------- 当前 tab 的接口地址 ---------- */
function _auditApiUrl() {
	return {
		pending: '/api/v1/knowledge/documents/pending-audits/',
		rejected: '/api/v1/knowledge/documents/audit-rejected/',
		records: '/api/v1/knowledge/documents/audit-records/',
	}[_auditTab];
}

/* ---------- 列表加载（带分页参数与请求序号守卫） ---------- */
function loadAuditList(page) {
	const seq = ++_requestSeq;
	// 页码越界（如删除后回退）时由调用方传入修正后的页码
	if (page) _auditPage = page;

	const tbody = $('#docTable');
	const head = $('#docTableHead');
	_renderTableHead(head);
	tbody.innerHTML = `<tr><td colspan="${_auditTab === 'records' ? 5 : 8}" class="text-sub text-sm text-center" style="padding:30px">加载中...</td></tr>`;

	// 组装查询参数：分页 + 搜索关键字 + 阶段筛选（阶段筛选仅待审核 tab 生效）
	const params = new URLSearchParams({ page: _auditPage, page_size: _auditPageSize });
	if (_auditKeyword) params.set('keyword', _auditKeyword);
	if (_auditTab === 'pending' && _auditStage) params.set('status', _auditStage);

	api.getJson(`${_auditApiUrl()}?${params.toString()}`)
		.then(res => {
			// 过期响应丢弃
			if (seq !== _requestSeq) return;
			const rows = res?.rows || [];
			_currentDocs = rows;
			_auditTotal = res?.count || 0;

			// 数据量减少导致当前页越界时，回退到最后一页重新加载
			const totalPages = Math.max(1, Math.ceil(_auditTotal / _auditPageSize));
			if (_auditPage > totalPages) {
				loadAuditList(totalPages);
				return;
			}

			if (!rows.length) {
				tbody.innerHTML = `<tr><td colspan="${_auditTab === 'records' ? 5 : 8}" class="text-sub text-sm text-center" style="padding:30px">${_emptyTip()}</td></tr>`;
				renderDocPagination();
				return;
			}
			tbody.innerHTML = (_auditTab === 'records' ? rows.map(_renderRecordRow) : rows.map(_renderDocRow)).join('');
			// 绑定行点击事件：打开审核详情弹窗（审核记录行不弹窗）
			if (_auditTab !== 'records') {
				tbody.querySelectorAll('[data-doc-id]').forEach(tr => {
					tr.addEventListener('click', () => {
						const id = +tr.getAttribute('data-doc-id');
						const data = rows.find(r => r.id === id);
						if (data) openDocModal(data);
					});
				});
			}
			renderDocPagination();
		})
		.catch(err => {
			if (seq !== _requestSeq) return;
			toast('加载文档列表失败', 'error');
			console.error(err);
			tbody.innerHTML = `<tr><td colspan="${_auditTab === 'records' ? 5 : 8}" class="text-sub text-sm text-center" style="padding:30px;color:var(--danger)">加载失败，请稍后重试</td></tr>`;
		});
}

/* ============================================================================
 * 搜索 / 阶段筛选 —— 变化后重置到第 1 页并重新加载
 * ============================================================================ */

/* ---------- 搜索输入（300ms 防抖，避免每次按键都发请求） ---------- */
function onAuditSearchInput() {
	clearTimeout(_auditSearchTimer);
	_auditSearchTimer = setTimeout(() => onAuditSearchCommit(), 300);
}

/* ---------- 搜索框回车：立即触发，同时清掉未执行的防抖 ---------- */
function onAuditSearchKeydown(e) {
	if (e.key === 'Enter') {
		clearTimeout(_auditSearchTimer);
		onAuditSearchCommit();
	}
}

/* ---------- 提交搜索条件并重新加载 ---------- */
function onAuditSearchCommit() {
	_auditKeyword = ($('#docSearchInput')?.value || '').trim();
	_auditPage = 1;
	_paginationInited = false;
	loadAuditList(1);
}

/* ---------- 阶段筛选变化（待审核 / 待复核，仅待审核 tab 展示） ---------- */
function onAuditFilterChange() {
	_auditStage = ($('#docStageFilter')?.value || '').trim();
	_auditPage = 1;
	_paginationInited = false;
	loadAuditList(1);
}

/* ---------- 空列表提示 ---------- */
function _emptyTip() {
	return {
		pending: '暂无待审核文档',
		rejected: '暂无已驳回文档',
		records: '暂无审核记录',
	}[_auditTab];
}

/* ---------- 表头渲染（审核记录与其他 tab 列不同） ---------- */
function _renderTableHead(head) {
	if (!head) return;
	head.innerHTML = _auditTab === 'records'
		? `<tr>
			<th>文档标题</th>
			<th style="width:120px">操作</th>
			<th style="width:120px">操作人</th>
			<th>审批意见</th>
			<th style="width:160px">时间</th>
		</tr>`
		: `<tr>
			<th>文档标题</th>
			<th style="width:90px">类型</th>
			<th style="width:90px">密级</th>
			<th>上传人</th>
			<th style="width:220px">归属</th>
			<th style="width:120px">阶段</th>
			<th style="width:160px">上传时间</th>
			<th style="width:100px">操作</th>
		</tr>`;
}

/* ---------- 审核记录行 ---------- */
function _renderRecordRow(r) {
	const actionBadge = {
		'审核通过': 'badge-info',
		'复核通过': 'badge-success',
		'驳回': 'badge-danger',
	}[r.action_label] || 'badge-default';
	return `
	<tr class="table-row-hover">
		<td>
			<div class="flex items-center gap-8">
				<div>
					<div class="text-strong">${escapeHtml(r.document_title || '—')}</div>
				</div>
			</div>
		</td>
		<td><span class="badge ${actionBadge}">${escapeHtml(r.action_label)}</span></td>
		<td class="text-sm">${escapeHtml(r.operator_name || '—')}</td>
		<td class="text-sm text-sub">${escapeHtml(r.comment || '—')}</td>
		<td class="text-sm text-sub">${formatDate(r.created_at)}</td>
	</tr>`;
}

/* ============================================================================
 * 待审核 / 已驳回 —— 行渲染
 * ============================================================================ */
function _renderDocRow(d) {
	// 密级映射：1=普通, 2=内部, 3=机密, 4=绝密
	const secLvMap = { 1: '普通', 2: '内部', 3: '机密', 4: '绝密' };
	const secBadge = { 1: '', 2: 'badge-info', 3: 'badge-warn', 4: 'badge-danger' }[d.secret_level] || '';
	const belong = [d.dept_name, d.team_name].filter(Boolean).join(' / ');
	// 阶段：待审核(橙)/待复核(蓝)/已驳回(红) 用不同颜色徽章区分，便于一眼识别当前流程阶段
	const stageText = d.audit_status === 'rejected' ? '已驳回'
		: d.audit_status === 'pending_compliance' ? '待复核' : '待审核';
	const stageBadge = d.audit_status === 'rejected' ? 'badge-danger'
		: d.audit_status === 'pending_compliance' ? 'badge-info' : 'badge-warn';
	const stageHtml = `<span class="badge ${stageBadge}">${stageText}</span>`;
	// 文件名与标题相同时不重复展示（避免标题下方文件名重复出现）
	const fileSub = d.file_name && d.file_name !== d.title
		? `<div class="text-sub text-xs">${escapeHtml(d.file_name)}</div>` : '';
	return `
	<tr class="table-row-hover" data-doc-id="${d.id}" style="cursor:pointer">
		<td>
			<div class="flex items-center gap-8">
				<span style="font-size:16px">${_iconForFileType(d.file_type)}</span>
				<div>
					<div class="text-strong">${escapeHtml(d.title)}</div>
					${fileSub}
					${d.reject_comment ? `<div class="text-xs" style="color:var(--danger)">驳回：${escapeHtml(d.reject_comment)}</div>` : ''}
				</div>
			</div>
		</td>
		<td class="text-sm">${escapeHtml(d.file_type || '—')}</td>
		<td>${secLvMap[d.secret_level] ? `<span class="badge ${secBadge}">${secLvMap[d.secret_level]}</span>` : '—'}</td>
		<td class="text-sm">${escapeHtml(d.owner_username || d.owner_name || '—')}</td>
		<td class="text-sm" style="white-space:nowrap">${escapeHtml(belong || '—')}</td>
		<td>${stageHtml}</td>
		<td class="text-sm text-sub">${formatDate(d.created_at)}</td>
		<td>
			<button class="btn btn-sm btn-primary">处理</button>
		</td>
	</tr>`;
}

/* ============================================================================
 * 分页：复用公共 Pagination 组件（common.js）。
 * 首次 render 绑定回调，后续 update 仅刷新页码状态；切换每页条数后重置回第 1 页
 * ============================================================================ */
function renderDocPagination() {
	const totalPages = Math.max(1, Math.ceil(_auditTotal / _auditPageSize));
	if (!_paginationInited) {
		Pagination.render({
			container: '#docPagination',
			page: _auditPage,
			totalPages: totalPages,
			total: _auditTotal,
			pageSize: _auditPageSize,
			align: 'right',
			// pageSizeOptions: [10, 20, 50],
			onPageChange(p) { loadAuditList(p); },
			onPageSizeChange(size) { _auditPageSize = size; loadAuditList(1); },
		});
		_paginationInited = true;
	} else {
		Pagination.update({
			page: _auditPage,
			totalPages: totalPages,
			total: _auditTotal,
			pageSize: _auditPageSize,
		});
	}
}

/* ============================================================================
 * 文档详情弹窗
 * ============================================================================ */
function openDocModal(d) {
	_currentDoc = d;
	// 弹窗标题：按状态区分（已驳回/待审核/待复核）
	$('#docModalTitle').textContent = _docModalTitle(d);
	// 可见性 / 密级 文案映射（可见性为字符串枚举 TEAM_ONLY/DEPT_ONLY/PUBLIC）
	const visMap = { 'PUBLIC': '全局公开', 'DEPT_ONLY': '部门内可见', 'TEAM_ONLY': '团队内可见' };
	const secLvMap = { 1: '普通', 2: '内部', 3: '机密', 4: '绝密' };
	const belong = [d.dept_name, d.team_name].filter(Boolean).join(' / ');
	const fileSizeTxt = d.file_size ? formatFileSize(d.file_size) : '—';
	// 版本：version_tag 已含 v 前缀（如 v1），无标签时兜底 v{version}
	const versionTxt = d.version_tag || ('v' + (d.version || 1));
	const isRejected = d.audit_status === 'rejected';
	// 文件名与标题相同时不重复展示（避免标题下方文件名重复出现）
	const fileSub = d.file_name && d.file_name !== d.title
		? `<div class="detail-cell-sub">${escapeHtml(d.file_name)}</div>` : '';
	// 取上传人姓名首字作为头像占位
	const avatarChar = (d.owner_name || '?').charAt(0).toUpperCase();

	$('#docModalBody').innerHTML = `
		<div class="applicant-card">
			<div class="applicant-avatar">${escapeHtml(avatarChar)}</div>
			<div class="applicant-info">
				<div class="applicant-name">${escapeHtml(d.owner_name)}</div>
				<div class="applicant-meta">账号：${escapeHtml(d.owner_username || '—')}</div>
			</div>
			<div class="applicant-time">
				<div class="applicant-time-label">上传时间</div>
				${formatDate(d.created_at)}
			</div>
		</div>

		<div class="detail-section-title">文档信息</div>
		<div class="detail-grid">
			<div class="detail-cell" style="grid-column:1/-1">
				<div class="detail-cell-label">文档标题</div>
				<div class="doc-title-row">
					<div class="doc-title-main">
						<div class="detail-cell-value">${escapeHtml(d.title)}</div>
						${fileSub}
					</div>
					<button class="btn btn-sm btn-outline doc-preview-btn" onclick="previewDoc(${d.id})">👁 预览</button>
				</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">当前阶段</div>
				<div class="detail-cell-value">
					${isRejected
						? '<span class="badge badge-danger">已驳回</span>'
						: d.audit_status === 'pending_compliance'
							? '<span class="badge badge-info">待复核</span>'
							: '<span class="badge badge-warn">待审核</span>'}
				</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">版本</div>
				<div class="detail-cell-value">${escapeHtml(versionTxt)}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">文件类型</div>
				<div class="detail-cell-value">${escapeHtml(d.file_type || '—')} · ${fileSizeTxt}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">可见性</div>
				<div class="detail-cell-value">${visMap[d.visibility_level] || '—'}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">密级</div>
				<div class="detail-cell-value">${secLvMap[d.secret_level] || '—'}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">归属路径</div>
				<div class="detail-cell-value">
					${belong ? escapeHtml(belong) : '—'}
					${d.node_name ? `<span class="detail-cell-sub">（节点：${escapeHtml(d.node_name)}）</span>` : ''}
				</div>
			</div>
			${isRejected ? `
			<div class="detail-cell" style="grid-column:1/-1">
				<div class="detail-cell-label">驳回理由</div>
				<div class="detail-cell-value" style="color:var(--danger)">${escapeHtml(d.reject_comment || '—')}</div>
				<div class="detail-cell-sub">驳回时间：${d.rejected_at ? formatDate(d.rejected_at) : '—'}</div>
			</div>` : ''}
		</div>

		<div class="detail-section-title">敏感内容检测</div>
		<div id="docScanArea">
			<div class="doc-scan-loading">检测中...</div>
		</div>
	`;
	// 已驳回文档不可再审核：隐藏通过/拒绝按钮
	$('#btnDocApprove').style.display = isRejected ? 'none' : '';
	$('#btnDocReject').style.display = isRejected ? 'none' : '';
	showModal('docModal');
	// 弹窗打开后异步加载敏感内容检测结果（不阻塞审核操作）
	_loadSensitiveScan(d.id);
}

/* ---------- 弹窗标题：按审核状态生成 ---------- */
function _docModalTitle(d) {
	const title = escapeHtml(d.title);
	if (d.audit_status === 'rejected') return '文档详情 · ' + title;
	return '文档审核 · ' + title;
}

/* ============================================================================
 * 文档预览（二级弹窗由公共模块 preview-doc.js 实现）
 * ============================================================================ */
// 预览元信息来源：当前列表数据中按 id 查找，找不到返回 null
function getDocForPreview(id) {
	return Promise.resolve((_currentDocs || []).find(x => x.id === id) || null);
}

/* ============================================================================
 * 敏感内容检测 —— 弹窗内自动扫描（敏感词/手机号/邮箱/IP/身份证/银行卡）
 * ============================================================================ */

// 打开弹窗后异步加载检测结果：不阻塞审核操作，失败仅展示占位提示
function _loadSensitiveScan(docId) {
	const area = $('#docScanArea');
	if (!area) return;
	api.getJson(`/api/v1/knowledge/documents/${docId}/sensitive-scan/`)
		.then(res => {
			if (res?.ok !== true) { area.innerHTML = _scanErrorHtml(res?.detail || '检测失败'); return; }
			area.innerHTML = res.total > 0 ? _scanResultHtml(res) : _scanCleanHtml();
		})
		.catch(err => {
			area.innerHTML = _scanErrorHtml(_errMsg(err, '检测服务异常'));
		});
}

/* 有命中：统计（上方）与详细片段（下方）上下排列，片段上下文保留原文换行/空格 */
function _scanResultHtml(res) {
	const cats = (res.categories || []).map(c => `
		<li class="doc-scan-stats-item">
			<span>${escapeHtml(c.label)}</span>
			<span class="doc-scan-stats-count">${c.count}</span>
		</li>
	`).join('');
	// ctx 为 pre-wrap，插值必须紧贴标签，否则模板缩进/换行会被原样渲染成片段首行空白
	const frags = (res.fragments || []).map(f => `
		<div class="doc-scan-frag">
			<div class="doc-scan-frag-head">
				<span class="doc-scan-cat">${escapeHtml(f.label)}</span>
				<span class="doc-scan-frag-count">${f.count > 1 ? `共 ${f.count} 处` : ''}</span>
			</div>
			<div class="doc-scan-frag-ctx">${escapeHtml(f.context_before)}<mark class="doc-scan-mark">${escapeHtml(f.matched)}</mark>${escapeHtml(f.context_after)}</div>
		</div>
	`).join('');
	return `
		<div class="doc-scan-summary">
			<div class="doc-scan-summary-title">⚠ 共检测到 ${res.total} 处敏感内容</div>
			<ul class="doc-scan-stats-list">${cats}</ul>
		</div>
		<div class="doc-scan-detail">
			<div class="doc-scan-detail-title">详细片段</div>
			<div class="doc-scan-frags">${frags}</div>
			${res.truncated ? '<div class="text-sub text-xs" style="margin-top:8px">片段较多，仅展示前 30 条</div>' : ''}
		</div>
	`;
}

/* 无命中：绿色提示 */
function _scanCleanHtml() {
	return `<div class="doc-scan-clean"><span class="doc-scan-clean-icon">✓</span><span>未检测到敏感内容</span></div>`;
}

/* 检测失败：非阻断提示（不影响审核操作） */
function _scanErrorHtml(msg) {
	return `<div class="doc-scan-todo"><span class="doc-scan-todo-icon">⚠</span><span>敏感内容检测失败：${escapeHtml(msg)}</span></div>`;
}

/* ============================================================================
 * 审核动作 —— 通过 / 驳回
 * ============================================================================ */

/* ---------- 审核通过（二次确认，备注选填） ---------- */
function onDocApproveClick() {
	if (!_currentDoc) return;
	const d = _currentDoc;
	showConfirmDialog({
		title: '确认通过审核',
		bannerType: 'success',
		bannerIcon: '✓',
		bannerText: `确认通过文档《${d.title}》？`,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">审批意见<span class="form-hint-inline">（选填）</span></label>' +
			'<textarea id="confirmDialogComment" class="input" rows="3" placeholder="可填写备注说明，记录审批意见..."></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: '确认通过', type: 'primary', onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				ctx.close();
				_submitDocApprove(d.id, comment);
			}}
		]
	});
}

/* ---------- 审核驳回（二次确认，理由必填） ---------- */
function onDocRejectClick() {
	if (!_currentDoc) return;
	const d = _currentDoc;
	showConfirmDialog({
		title: '驳回理由',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: `确认驳回文档《${d.title}》？驳回后需上传人重新提交。`,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">驳回理由<span class="required">*</span></label>' +
			'<textarea id="confirmDialogComment" class="input" rows="4" placeholder="必填，请说明驳回原因，便于申请人了解问题..."></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: '确认驳回', type: 'danger', onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				// 驳回理由必填，空则拦截并提示
				if (!comment) { ctx.setError('驳回理由不能为空'); return; }
				ctx.close();
				_submitDocReject(d.id, comment);
			}}
		],
		onShow: ctx => {
			const ta = ctx.el.querySelector('#confirmDialogComment');
			if (ta) {
				ta.focus();
				// Ctrl/Cmd + Enter 快捷提交驳回
				ta.addEventListener('keydown', (e) => {
					if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
						ctx.el.querySelector('.btn-reject')?.click();
					}
				});
			}
		}
	});
}

/* ---------- 提交：文档通过 ---------- */
function _submitDocApprove(id, comment) {
	if (_submitting) return;
	_submitting = true;
	api.postJson(`/api/v1/knowledge/documents/${id}/audit-approve/`, { comment })
		.then(res => {
			if (res?.ok) {
				// 复核通过 → 已发布；审核通过 → 流转复核
				const nextLabel = res.audit_status === 'passed'
					? '审核通过（已发布）'
					: `审核通过，流转至：${_auditStatusLabel(res.audit_status)}`;
				toast(nextLabel, 'success');
				closeModal('docModal');
				_currentDoc = null;
				loadAuditList();
			} else {
				toast(res?.detail || '审核失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '审核失败'), 'error');
		console.error(err);
	}).finally(() => { _submitting = false; });
}

/* ---------- 提交：文档驳回 ---------- */
function _submitDocReject(id, comment) {
	if (_submitting) return;
	_submitting = true;
	api.postJson(`/api/v1/knowledge/documents/${id}/audit-reject/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast('文档已驳回', 'success');
				closeModal('docModal');
				_currentDoc = null;
				loadAuditList();
			} else {
				toast(res?.detail || '驳回失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '驳回失败'), 'error');
		console.error(err);
	}).finally(() => { _submitting = false; });
}

/* ============================================================================
 * 通用辅助
 * ============================================================================ */

/* ---------- 审核状态文案映射 ---------- */
function _auditStatusLabel(s) {
	return {
		'pending_team': '待审核',
		'pending_compliance': '待复核',
		'passed': '已通过',
		'rejected': '已驳回',
		'archived': '已归档',
		'deleted': '已删除',
	}[s] || s;
}

/* ---------- 文件类型 → emoji 图标 ---------- */
function _iconForFileType(ft) {
	const f = (ft || '').toLowerCase();
	if (f === 'pdf') return '📄';
	if (['doc', 'docx'].includes(f)) return '📝';
	if (['xls', 'xlsx'].includes(f)) return '📊';
	if (['ppt', 'pptx'].includes(f)) return '📽️';
	if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(f)) return '🖼️';
	if (['zip', 'rar', '7z', 'tar', 'gz'].includes(f)) return '🗜️';
	if (['txt', 'md'].includes(f)) return '📃';
	return '📁';
}
