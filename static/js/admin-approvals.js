/* ============================================================================
 * admin-approvals.js —— 权限与文档审批页面
 *
 * 页面访问权限（前后端双重校验）：
 * - 仅 super_admin / user_admin / dept_manager / team_leader 可见
 * - 普通用户直接跳回首页并提示无权限
 *
 * 功能模块：
 * 1. 权限审批工单：待我审批列表 → 查看详情 → 通过 / 拒绝（拒绝必填理由）
 * 2. 文档审核（双审）：待一审 / 待二审 文档 → 查看详情 → 通过 / 驳回（驳回必填理由）
 * ============================================================================ */

// 当前选中的审批对象：{type: 'ticket'|'doc', data: {...}}
let _currentApproval = null;
let confirmCallback = null;
let _currentTab = 'ticket';

/* ============ 页面启动 ============ */
document.addEventListener('DOMContentLoaded', () => {
	// 1. 页面级权限校验：仅管理角色可进入
	// （common.js 的 authGuard 已校验登录态，此处额外做角色权限拦截）
	if (!_canAccessPage()) {
		toast('您没有权限访问审批中心', 'error');
		setTimeout(() => { window.location.href = '/chat/'; }, 800);
		return;
	}

	// 2. 顶栏 / 侧栏 / 全局搜索 已由 common.js 的 DOMContentLoaded 注入，无需重复

	// 3. 加载两个列表（并行）
	refreshAll();

	// 4. 全局 Enter 快捷键支持（驳回弹窗）
	const rejectInput = document.getElementById('rejectComment');
	if (rejectInput) {
		rejectInput.addEventListener('keydown', (e) => {
			if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitReject();
		});
	}
});

/* ---------- 页面级权限判断 ---------- */
function _canAccessPage() {
	// 对齐需求：超级管理员、用户管理员、部门经理、团队组长可见
	// kb_admin 同样拥有管理权限，也允许进入（知识管理员也需要审批文档）
	return hasAnyRole('super_admin', 'user_admin', 'dept_manager', 'team_leader', 'kb_admin');
}

/* ============================================================================
 * 标签页切换与数据刷新
 * ============================================================================ */
function switchTab(tab) {
	_currentTab = tab;
	// 样式
	document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-active'));
	$('#tab-' + tab).classList.add('tab-active');
	// 列表显隐
	$('#list-ticket').classList.toggle('hidden', tab !== 'ticket');
	$('#list-doc').classList.toggle('hidden', tab !== 'doc');
}

function refreshAll() {
	loadTicketList();
	loadDocList();
}

/* ============================================================================
 * 权限审批工单 —— 列表加载
 * ============================================================================ */
function loadTicketList() {
	const tbody = $('#ticketTable');
	tbody.innerHTML = `<tr><td colspan="9" class="text-sub text-sm text-center" style="padding:30px">加载中...</td></tr>`;
	api.getJson('/api/v1/auth/permissions/pending-approvals/')
		.then(res => {
			const rows = res?.rows || [];
			_setBadge('ticket', rows.length);
			if (!rows.length) {
				tbody.innerHTML = `<tr><td colspan="9" class="text-sub text-sm text-center" style="padding:30px">暂无待审批工单</td></tr>`;
				return;
			}
			tbody.innerHTML = rows.map(_renderTicketRow).join('');
			// 绑定行点击
			tbody.querySelectorAll('[data-ticket-id]').forEach(tr => {
				tr.addEventListener('click', () => {
					const id = +tr.getAttribute('data-ticket-id');
					const data = rows.find(r => r.id === id);
					if (data) openTicketModal(data);
				});
			});
		})
		.catch(err => {
			toast('加载待审批工单失败', 'error');
			console.error(err);
			tbody.innerHTML = `<tr><td colspan="9" class="text-sub text-sm text-center" style="padding:30px;color:var(--danger)">加载失败，请稍后重试</td></tr>`;
		});
}

function _renderTicketRow(t) {
	const changeTypeMap = {
		'GRANT': '<span class="badge badge-success">授予</span>',
		'REVOKE': '<span class="badge badge-warn">撤销</span>',
		'ROLE_CHANGE': '<span class="badge badge-info">角色变更</span>',
		'SCOPE_CHANGE': '<span class="badge badge-info">范围变更</span>',
		'EXPIRE_EXTEND': '<span class="badge badge-info">延期</span>',
	};
	const ct = changeTypeMap[t.change_type] || t.change_type;
	// 进度：第 N 步 / 共 M 步
	const step = (t.current_step || 0) + 1;
	const total = t.total_steps || 1;
	// 目标权限描述：角色 + 范围
	const scopeTxt = t.scope_name || (_scopeTypeLabel(t.scope_type) + (t.scope_id ? ` #${t.scope_id}` : ''));
	const targetPerm = [
		t.role_name ? `<strong>${t.role_name}</strong>` : '',
		scopeTxt ? `<span class="text-sub text-sm">(${scopeTxt})</span>` : ''
	].filter(Boolean).join(' ');
	return `
	<tr class="table-row-hover" data-ticket-id="${t.id}" style="cursor:pointer">
		<td><span class="mono text-sm">${t.ticket_no}</span></td>
		<td>${ct}</td>
		<td>
			<div>${escapeHtml(t.applicant_name)}</div>
			<div class="text-sub text-xs">${escapeHtml(t.applicant_email || '')}</div>
		</td>
		<td>
			<div>${escapeHtml(t.target_user_name)}</div>
			<div class="text-sub text-xs">${escapeHtml(t.target_user_email || '')}</div>
		</td>
		<td>${targetPerm}</td>
		<td class="text-sm">${escapeHtml(t.expires_at ? '至 ' + formatDate(t.expires_at) : '长期有效')}</td>
		<td><span class="text-sm text-sub">${formatDate(t.created_at)}</span></td>
		<td><span class="badge badge-info">第 ${step}/${total} 步</span></td>
		<td>
			<button class="btn btn-sm btn-primary" onclick="event.stopPropagation();openTicketModal(${JSON.stringify(t).replace(/"/g, '&quot;')})">处理</button>
		</td>
	</tr>`;
}

function _scopeTypeLabel(st) {
	return { 'GLOBAL': '全局', 'DEPT': '部门', 'TEAM': '团队', 'NONE': '—' }[st] || st;
}

/* ============================================================================
 * 权限审批工单 —— 详情弹窗 & 审批动作
 * ============================================================================ */
function openTicketModal(t) {
	_currentApproval = { type: 'ticket', data: t };
	$('#approvalModalTitle').textContent = '权限变更审批 - ' + t.ticket_no;
	const chain = (t.approval_chain || []).map((n, i) => {
		const cls = i < t.current_step ? 'step-done'
			: i === t.current_step ? 'step-curr' : 'step-pending';
		const statusLabel = i < t.current_step ? '已通过'
			: i === t.current_step ? '待审批' : '待处理';
		return `
		<li class="${cls}">
			<div class="step-role">${_approverRoleLabel(n.approver_role)}</div>
			<div class="step-status">${statusLabel}</div>
			${n.comment ? `<div class="step-comment">${escapeHtml(n.comment)}</div>` : ''}
			${n.approved_at ? `<div class="text-xs text-sub">${formatDate(n.approved_at)}</div>` : ''}
		</li>`;
	}).join('');
	const scopeTxt = t.scope_name || (_scopeTypeLabel(t.scope_type) + (t.scope_id ? ` #${t.scope_id}` : ''));

	$('#approvalModalBody').innerHTML = `
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">申请人</div>
				<div class="form-value"><strong>${escapeHtml(t.applicant_name)}</strong></div>
				<div class="text-sub text-sm">${escapeHtml(t.applicant_email || '')}</div>
			</div>
			<div class="form-item">
				<div class="form-label">申请时间</div>
				<div class="form-value text-sm">${formatDate(t.created_at)}</div>
			</div>
		</div>
		<div class="form-section-title mt-16">变更内容</div>
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">变更类型</div>
				<div class="form-value">${_changeTypeLabel(t.change_type)}</div>
			</div>
			<div class="form-item">
				<div class="form-label">目标用户</div>
				<div class="form-value"><strong>${escapeHtml(t.target_user_name)}</strong></div>
				<div class="text-sub text-sm">${escapeHtml(t.target_user_email || '')}</div>
			</div>
		</div>
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">${t.change_type === 'ROLE_CHANGE' ? '角色变更' : '目标角色'}</div>
				<div class="form-value">${_renderRoleChange(t)}</div>
			</div>
			<div class="form-item">
				<div class="form-label">权限范围</div>
				<div class="form-value">${escapeHtml(scopeTxt)}</div>
			</div>
		</div>
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">生效时间</div>
				<div class="form-value text-sm">${t.effective_from ? formatDate(t.effective_from) : '立即生效'}</div>
			</div>
			<div class="form-item">
				<div class="form-label">截至日期</div>
				<div class="form-value text-sm">${t.expires_at ? formatDate(t.expires_at) : '长期有效'}</div>
			</div>
		</div>
		<div class="form-item mt-16">
			<div class="form-label">申请理由</div>
			<div class="form-value" style="background:var(--bg);padding:12px;border-radius:8px;line-height:1.6">${escapeHtml(t.reason) || '—'}</div>
		</div>
		<div class="form-section-title mt-20">审批链进度</div>
		<ol class="timeline">${chain}</ol>
		<div class="text-sub text-xs mt-8">当前审批人：${_approverRoleLabel(t.approver_role)}</div>
	`;
	showModal('approvalModal');
}

function _approverRoleLabel(r) {
	return {
		'TEAM_LEADER': '团队组长',
		'DEPT_LEADER': '部门经理',
		'DEPT_MANAGER': '部门经理',
		'USER_ADMIN': '用户管理员',
		'KB_ADMIN': '知识管理员',
		'SUPER_ADMIN': '超级管理员',
	}[r] || r;
}

function _changeTypeLabel(ct) {
	return {
		'GRANT': '<span class="badge badge-success">授予权限</span>',
		'REVOKE': '<span class="badge badge-warn">撤销权限</span>',
		'ROLE_CHANGE': '<span class="badge badge-info">角色变更</span>',
		'SCOPE_CHANGE': '<span class="badge badge-info">范围变更</span>',
		'EXPIRE_EXTEND': '<span class="badge badge-info">延期</span>',
	}[ct] || ct;
}

// 渲染角色变更展示:ROLE_CHANGE 显示 旧角色 → 新角色;其他显示新角色
function _renderRoleChange(t) {
	if (!t.role_name && !t.previous_role_name) return '—';
	if (t.change_type === 'ROLE_CHANGE' && t.previous_role_name) {
		return `<span class="badge badge-warn">${escapeHtml(t.previous_role_name)}</span>` +
			` <span class="text-sub">→</span> ` +
			`<span class="badge badge-info">${escapeHtml(t.role_name || '—')}</span>`;
	}
	return t.role_name ? `<span class="badge badge-info">${escapeHtml(t.role_name)}</span>` : '—';
}

/* ---------- 审批通过 ---------- */
function onApproveClick() {
	if (!_currentApproval) return;
	const a = _currentApproval;
	$('#confirmTitle').textContent = '确认通过审批';
	$('#confirmMessage').innerHTML = a.type === 'ticket'
		? `确认通过工单 <strong>${a.data.ticket_no}</strong>？通过后将按审批链流转。`
		: `确认通过文档 <strong>${escapeHtml(a.data.title)}</strong>？`;
	$('#confirmCommentWrap').classList.remove('hidden');
	$('#confirmComment').value = '';
	$('#confirmOkBtn').className = 'btn-save';
	$('#confirmOkBtn').textContent = '确认通过';
	confirmCallback = () => {
		const comment = ($('#confirmComment').value || '').trim();
		if (a.type === 'ticket') _submitTicketApprove(a.data.id, comment);
		else _submitDocApprove(a.data.id, comment);
	};
	showModal('confirmModal');
}

/**
 * 关闭 confirmModal，confirmed=true 时调用并清空 confirmCallback
 * @param {boolean} confirmed - true 表示用户点了确认
 */
async function closeConfirmModal(confirmed) {
	const cb = confirmCallback;
	confirmCallback = null;
	closeModal('confirmModal');
	if (confirmed && typeof cb === 'function') {
		try {
			await cb();
		} catch (e) {
			toast('操作失败: ' + escapeHtml(e.message || String(e)), 'error');
		}
	}
}

/* ---------- 审批拒绝 ---------- */
function onRejectClick() {
	if (!_currentApproval) return;
	$('#rejectComment').value = '';
	$('#rejectCommentErr').classList.add('hidden');
	$('#rejectCommentErr').textContent = '';
	showModal('rejectModal');
}

function submitReject() {
	const comment = ($('#rejectComment').value || '').trim();
	const errEl = $('#rejectCommentErr');
	if (!comment) {
		errEl.textContent = '驳回理由不能为空';
		errEl.classList.remove('hidden');
		return;
	}
	if (!_currentApproval) return;
	closeModal('rejectModal');
	if (_currentApproval.type === 'ticket') {
		_submitTicketReject(_currentApproval.data.id, comment);
	} else {
		_submitDocReject(_currentApproval.data.id, comment);
	}
}

/* ---------- 提交：工单通过/驳回 ---------- */
function _submitTicketApprove(id, comment) {
	api.postJson(`/api/v1/auth/permissions/tickets/${id}/approve/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast(`工单已通过，状态：${_ticketStatusLabel(res.status)}`, 'success');
				closeModal('approvalModal');
				_currentApproval = null;
				loadTicketList();
			} else {
				toast(res?.detail || '审批失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '审批失败'), 'error');
		console.error(err);
	});
}

function _submitTicketReject(id, comment) {
	api.postJson(`/api/v1/auth/permissions/tickets/${id}/reject/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast('工单已驳回', 'success');
				closeModal('approvalModal');
				_currentApproval = null;
				loadTicketList();
			} else {
				toast(res?.detail || '驳回失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '驳回失败'), 'error');
		console.error(err);
	});
}

function _ticketStatusLabel(s) {
	return {
		'PENDING': '待审批',
		'APPROVED': '已通过',
		'EXECUTED': '已执行',
		'REJECTED': '已驳回',
		'CANCELLED': '已撤回',
	}[s] || s;
}

/* ============================================================================
 * 文档审核 —— 列表加载
 * ============================================================================ */
function loadDocList() {
	const tbody = $('#docTable');
	tbody.innerHTML = `<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px">加载中...</td></tr>`;
	api.getJson('/api/v1/knowledge/documents/pending-audits/')
		.then(res => {
			const rows = res?.rows || [];
			_setBadge('doc', rows.length);
			if (!rows.length) {
				tbody.innerHTML = `<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px">暂无待审核文档</td></tr>`;
				return;
			}
			tbody.innerHTML = rows.map(_renderDocRow).join('');
			tbody.querySelectorAll('[data-doc-id]').forEach(tr => {
				tr.addEventListener('click', () => {
					const id = +tr.getAttribute('data-doc-id');
					const data = rows.find(r => r.id === id);
					if (data) openDocModal(data);
				});
			});
		})
		.catch(err => {
			toast('加载待审核文档失败', 'error');
			console.error(err);
			tbody.innerHTML = `<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px;color:var(--danger)">加载失败，请稍后重试</td></tr>`;
		});
}

function _renderDocRow(d) {
	const secLvMap = { 1: '公开', 2: '内部', 3: '秘密', 4: '绝密' };
	const secBadge = { 1: '', 2: 'badge-info', 3: 'badge-warn', 4: 'badge-danger' }[d.secret_level] || '';
	const auditBadge = d.audit_status === 'pending_team'
		? '<span class="badge badge-warn">待一审</span>'
		: '<span class="badge badge-info">待二审</span>';
	const belong = [d.dept_name, d.team_name].filter(Boolean).join(' / ');
	return `
	<tr class="table-row-hover" data-doc-id="${d.id}" style="cursor:pointer">
		<td>
			<div class="flex items-center gap-8">
				<span style="font-size:16px">${_iconForFileType(d.file_type)}</span>
				<div>
					<div class="text-strong">${escapeHtml(d.title)}</div>
					<div class="text-sub text-xs">${escapeHtml(d.file_name || '')}</div>
				</div>
			</div>
		</td>
		<td class="text-sm">${escapeHtml(d.file_type || '—')}</td>
		<td>${secLvMap[d.secret_level] ? `<span class="badge ${secBadge}">${secLvMap[d.secret_level]}</span>` : '—'}</td>
		<td>
			<div>${escapeHtml(d.owner_name)}</div>
			<div class="text-sub text-xs">${escapeHtml(d.owner_email || '')}</div>
		</td>
		<td class="text-sm">${escapeHtml(belong || '—')}</td>
		<td>
			${auditBadge}
			<div class="text-sub text-xs mt-4">${escapeHtml(d.audit_step || '')}</div>
		</td>
		<td class="text-sm text-sub">${formatDate(d.created_at)}</td>
		<td>
			<button class="btn btn-sm btn-primary" onclick="event.stopPropagation();openDocModal(${JSON.stringify(d).replace(/"/g, '&quot;')})">处理</button>
		</td>
	</tr>`;
}

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

/* ============================================================================
 * 文档审核 —— 详情弹窗 & 审核动作
 * ============================================================================ */
function openDocModal(d) {
	_currentApproval = { type: 'doc', data: d };
	$('#approvalModalTitle').textContent = '文档审核 - ' + (d.audit_status === 'pending_team' ? '团队组长一审' : '合规二审');
	const visMap = { 1: '全局公开', 2: '部门内可见', 3: '团队内可见', 4: '私有' };
	const secLvMap = { 1: '公开', 2: '内部', 3: '秘密', 4: '绝密' };
	const belong = [d.dept_name, d.team_name].filter(Boolean).join(' / ');
	const fileSizeTxt = d.file_size ? formatFileSize(d.file_size) : '—';

	$('#approvalModalBody').innerHTML = `
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">文档标题</div>
				<div class="form-value text-strong">${escapeHtml(d.title)}</div>
				<div class="text-sub text-sm">${escapeHtml(d.file_name || '')}</div>
			</div>
			<div class="form-item">
				<div class="form-label">当前阶段</div>
				<div class="form-value">
					${d.audit_status === 'pending_team'
						? '<span class="badge badge-warn">待一审（团队组长）</span>'
						: '<span class="badge badge-info">待二审（合规/部门经理）</span>'}
				</div>
			</div>
		</div>
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">上传人</div>
				<div class="form-value"><strong>${escapeHtml(d.owner_name)}</strong></div>
				<div class="text-sub text-sm">${escapeHtml(d.owner_email || '')}</div>
			</div>
			<div class="form-item">
				<div class="form-label">上传时间</div>
				<div class="form-value text-sm">${formatDate(d.created_at)}</div>
			</div>
		</div>
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">文件类型</div>
				<div class="form-value text-sm">${escapeHtml(d.file_type || '—')} · ${fileSizeTxt}</div>
			</div>
			<div class="form-item">
				<div class="form-label">版本</div>
				<div class="form-value text-sm">v${d.version || 1}${d.version_tag ? ' · ' + escapeHtml(d.version_tag) : ''}</div>
			</div>
		</div>
		<div class="two-col">
			<div class="form-item">
				<div class="form-label">可见性</div>
				<div class="form-value">${visMap[d.visibility_level] || '—'}</div>
			</div>
			<div class="form-item">
				<div class="form-label">密级</div>
				<div class="form-value">${secLvMap[d.secret_level] || '—'}</div>
			</div>
		</div>
		<div class="form-item mt-16">
			<div class="form-label">归属路径</div>
			<div class="form-value text-sm">
				${belong ? escapeHtml(belong) : '—'}
				${d.node_name ? `<span class="text-sub">（节点：${escapeHtml(d.node_name)}）</span>` : ''}
			</div>
		</div>
		<div class="flex mt-20">
			<a class="btn btn-sm btn-outline" href="/admin-nodes/?doc_id=${d.uuid}" target="_blank" rel="noopener">
				🔗 在知识库中查看
			</a>
		</div>
	`;
	showModal('approvalModal');
}

/* ---------- 提交：文档通过/驳回 ---------- */
function _submitDocApprove(id, comment) {
	api.postJson(`/api/v1/knowledge/documents/${id}/audit-approve/`, { comment })
		.then(res => {
			if (res?.ok) {
				const nextLabel = res.audit_status === 'passed'
					? '审核通过（已发布）'
					: `一审通过，流转至：${_auditStatusLabel(res.audit_status)}`;
				toast(nextLabel, 'success');
				closeModal('approvalModal');
				_currentApproval = null;
				loadDocList();
			} else {
				toast(res?.detail || '审核失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '审核失败'), 'error');
		console.error(err);
	});
}

function _submitDocReject(id, comment) {
	api.postJson(`/api/v1/knowledge/documents/${id}/audit-reject/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast('文档已驳回', 'success');
				closeModal('approvalModal');
				_currentApproval = null;
				loadDocList();
			} else {
				toast(res?.detail || '驳回失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '驳回失败'), 'error');
		console.error(err);
	});
}

function _auditStatusLabel(s) {
	return {
		'pending_team': '待一审',
		'pending_compliance': '待二审',
		'passed': '已通过',
		'rejected': '已驳回',
		'archived': '已归档',
		'deleted': '已删除',
	}[s] || s;
}

/* ============================================================================
 * 通用辅助
 * ============================================================================ */
function _setBadge(type, count) {
	const el = $('#badge-' + type);
	if (!el) return;
	if (count > 0) {
		el.textContent = count;
		el.classList.remove('hidden');
	} else {
		el.classList.add('hidden');
	}
}

function _errMsg(err, fallback) {
	try {
		if (typeof err === 'string') return err;
		if (err?.detail) return err.detail;
		if (err?.message) return err.message;
		if (err?.error) return err.error;
	} catch (_) {}
	return fallback;
}
