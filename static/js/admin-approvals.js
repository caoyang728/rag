/* ============================================================================
 * admin-approvals.js —— 权限审批中心
 *
 * 四视角架构：
 * 1. 待我审批：当前用户可处理的 PENDING 工单（共享审批池）
 * 2. 我已审批：当前用户已处理过的工单（含通过/驳回记录）
 * 3. 我发起的：当前用户作为申请人提交的工单（所有状态）
 * 4. 全部工单：全局视角，仅 super_admin / compliance_admin 可见
 *
 * 页面访问权限：super_admin / user_admin / dept_manager / team_leader / kb_admin
 * ============================================================================ */

// 当前视角：pending / processed / mine / all
let _currentView = 'pending';
// 当前列表数据缓存（供行点击回查用）
let _currentRows = [];
// 当前打开的审批对象（供通过/驳回按钮使用）
let _currentApproval = null;
// 提交防重锁（防止审批通过/驳回重复提交）
let _submitting = false;

/* ============ 页面启动 ============ */
document.addEventListener('DOMContentLoaded', () => {
	if (!_canAccessPage()) {
		toast('您没有权限访问审批中心', 'error');
		setTimeout(() => { window.location.href = '/chat/'; }, 800);
		return;
	}
	// 超管/合规管理员可见"全部工单"Tab
	if (_canViewAll()) {
		$('#tab-all').classList.remove('hidden');
	}
	// 合规管理员默认看"全部工单"（审计视角，不参与审批）
	const isPureCompliance = hasAnyRole('compliance_admin')
		&& !hasAnyRole('super_admin', 'user_admin', 'dept_manager', 'team_leader', 'kb_admin');
	if (isPureCompliance) {
		switchView('all');
	} else {
		loadPendingList();
	}
});

/* ---------- 页面级权限判断 ---------- */
function _canAccessPage() {
	return hasAnyRole('super_admin', 'user_admin', 'dept_manager', 'team_leader', 'kb_admin', 'compliance_admin');
}

function _canViewAll() {
	// 全部工单视角：仅超管/合规管理员
	return hasAnyRole('super_admin', 'compliance_admin');
}

/* ============================================================================
 * 视角切换
 * ============================================================================ */
function switchView(view) {
	_currentView = view;
	// Tab 样式
	document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-active'));
	$('#tab-' + view).classList.add('tab-active');
	// 状态筛选下拉框：仅"全部工单"视角显示
	$('#allStatusFilter').classList.toggle('hidden', view !== 'all');
	// 时间列标题：我已审批=我的审批时间，其他=申请时间
	const thTime = $('#th-time');
	if (thTime) thTime.textContent = view === 'processed' ? '处理时间' : '申请时间';
	// 加载对应列表
	refreshCurrent();
}

function refreshCurrent() {
	if (_currentView === 'pending') loadPendingList();
	else if (_currentView === 'processed') loadProcessedList();
	else if (_currentView === 'mine') loadMineList();
	else if (_currentView === 'all') loadAllList();
}

/* ============================================================================
 * 待我审批 —— 共享审批池中的 PENDING 工单
 * ============================================================================ */
function loadPendingList() {
	_currentView = 'pending';
	const tbody = $('#ticketTable');
	tbody.innerHTML = _loadingRow(10);
	api.getJson('/api/v1/auth/permissions/pending-approvals/')
		.then(res => {
			const rows = res?.rows || [];
			_currentRows = rows;
			_setBadge('pending', rows.length);
			_renderTable(rows, 'pending');
		})
		.catch(err => {
			_setBadge('pending', 0);
			_renderTableError('加载待审批工单失败');
			console.error(err);
		});
}

/* ============================================================================
 * 我已审批 —— 当前用户处理过的工单
 * ============================================================================ */
function loadProcessedList() {
	const tbody = $('#ticketTable');
	tbody.innerHTML = _loadingRow(10);
	api.getJson('/api/v1/auth/permissions/processed-tickets/')
		.then(res => {
			const rows = res?.rows || [];
			_currentRows = rows;
			_renderTable(rows, 'processed');
		})
		.catch(err => {
			_renderTableError('加载已审批工单失败');
			console.error(err);
		});
}

/* ============================================================================
 * 我发起的 —— 当前用户作为申请人的工单
 * ============================================================================ */
function loadMineList() {
	const tbody = $('#ticketTable');
	tbody.innerHTML = _loadingRow(10);
	api.getJson('/api/v1/auth/permissions/my-tickets/')
		.then(res => {
			const rows = res?.rows || [];
			_currentRows = rows;
			_renderTable(rows, 'mine');
		})
		.catch(err => {
			_renderTableError('加载我的申请失败');
			console.error(err);
		});
}

/* ============================================================================
 * 全部工单 —— 全局视角（仅超管/合规）
 * ============================================================================ */
function loadAllList() {
	const tbody = $('#ticketTable');
	tbody.innerHTML = _loadingRow(10);
	const statusFilter = $('#allStatusFilter')?.value || '';
	const url = '/api/v1/auth/permissions/all-tickets/' + (statusFilter ? `?status=${statusFilter}` : '');
	api.getJson(url)
		.then(res => {
			const rows = res?.rows || [];
			_currentRows = rows;
			_renderTable(rows, 'all');
		})
		.catch(err => {
			_renderTableError('加载工单失败');
			console.error(err);
		});
}

/* ============================================================================
 * 表格渲染
 * ============================================================================ */
function _renderTable(rows, view) {
	const tbody = $('#ticketTable');
	if (!rows.length) {
		const emptyText = {
			'pending': '暂无待审批工单',
			'processed': '暂无已审批记录',
			'mine': '暂无申请记录',
			'all': '暂无工单',
		}[view] || '暂无数据';
		tbody.innerHTML = `<tr><td colspan="10" class="text-sub text-sm text-center" style="padding:30px">${emptyText}</td></tr>`;
		return;
	}
	tbody.innerHTML = rows.map(t => _renderTicketRow(t, view)).join('');
	// 绑定行点击
	tbody.querySelectorAll('[data-ticket-id]').forEach(tr => {
		tr.addEventListener('click', () => {
			const id = +tr.getAttribute('data-ticket-id');
			const data = _currentRows.find(r => r.id === id);
			if (data) openTicketModal(data, view);
		});
	});
}

function _renderTableError(msg) {
	$('#ticketTable').innerHTML =
		`<tr><td colspan="10" class="text-sub text-sm text-center" style="padding:30px;color:var(--danger)">${msg}，请稍后重试</td></tr>`;
}

function _loadingRow(colspan) {
	return `<tr><td colspan="${colspan}" class="text-sub text-sm text-center" style="padding:30px">加载中...</td></tr>`;
}

function _renderTicketRow(t, view) {
	const changeTypeMap = {
		'GRANT': '<span class="badge badge-success">授予</span>',
		'REVOKE': '<span class="badge badge-warn">撤销</span>',
		'ROLE_CHANGE': '<span class="badge badge-info">角色变更</span>',
		'SCOPE_CHANGE': '<span class="badge badge-info">范围变更</span>',
		'EXPIRE_EXTEND': '<span class="badge badge-info">延期</span>',
	};
	const ct = changeTypeMap[t.change_type] || t.change_type;
	const step = (t.current_step || 0) + 1;
	const total = t.total_steps || 1;
	const scopeTxt = t.scope_name || (_scopeTypeLabel(t.scope_type) + (t.scope_id ? ` #${t.scope_id}` : ''));
	const targetPerm = [
		t.role_name ? `<strong>${t.role_name}</strong>` : '',
		scopeTxt ? `<span class="text-sub text-sm">(${scopeTxt})</span>` : ''
	].filter(Boolean).join(' ');

	// 状态列：待我审批视角固定显示"待审批"，其他视角显示实际状态
	const statusBadge = view === 'pending'
		? '<span class="badge badge-warn">待审批</span>'
		: _ticketStatusBadge(t.status);

	// 时间列：待我审批=申请时间，我已审批=我的审批时间，我发起的=申请时间，全部=申请时间
	const timeField = (view === 'processed' && t.my_approved_at) ? t.my_approved_at : t.created_at;

	// 操作按钮：待我审批=处理，其他=查看（点击由行事件统一处理，避免 onclick 内联 XSS 风险）
	const actionBtn = view === 'pending'
		? `<button class="btn btn-sm btn-primary">处理</button>`
		: `<button class="btn btn-sm btn-outline">查看</button>`;

	// 申请人列：我发起的视角显示"我"，其他视角显示申请人
	const applicantCell = view === 'mine'
		? '<span class="text-sub">我</span>'
		: `<div>${escapeHtml(t.applicant_name || '—')}</div>
		   <div class="text-sub text-xs">${escapeHtml(t.applicant_email || '')}</div>`;

	// 进度展示：待我审批显示"第N步"，其他显示审批状态
	const progressCell = view === 'pending'
		? `<span class="badge badge-info">第 ${step}/${total} 步</span>`
		: `<span class="badge ${total > 0 && t.current_step >= total ? 'badge-success' : 'badge-info'}">${step}/${total}</span>`;

	return `
<tr class="table-row-hover" data-ticket-id="${t.id}" style="cursor:pointer">
	<td><span class="mono text-sm">${t.ticket_no}</span></td>
	<td>${ct}</td>
	<td>${applicantCell}</td>
	<td>
		<div>${escapeHtml(t.target_user_name || '—')}</div>
		<div class="text-sub text-xs">${escapeHtml(t.target_user_email || '')}</div>
	</td>
	<td>${targetPerm || '—'}</td>
	<td class="text-sm">${escapeHtml(scopeTxt) || '—'}</td>
	<td>${statusBadge}</td>
	<td><span class="text-sm text-sub">${formatDate(timeField)}</span></td>
	<td>${progressCell}</td>
	<td>${actionBtn}</td>
</tr>`;
}

function _scopeTypeLabel(st) {
	return { 'GLOBAL': '全局', 'DEPT': '部门', 'TEAM': '团队', 'NONE': '—' }[st] || st || '—';
}

function _ticketStatusBadge(s) {
	const map = {
		'PENDING': '<span class="badge badge-warn">待审批</span>',
		'APPROVED': '<span class="badge badge-info">已通过</span>',
		'EXECUTED': '<span class="badge badge-success">已执行</span>',
		'REJECTED': '<span class="badge badge-danger">已驳回</span>',
		'CANCELLED': '<span class="badge">已撤回</span>',
	};
	return map[s] || s;
}

/* 审批链节点 SVG 图标（18×18，viewBox="0 0 18 18"） */
const CHAIN_ICON_APPROVED = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#22c55e" stroke="#22c55e" stroke-width="2"/><path d="M5 9.5l3 3L13 6" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CHAIN_ICON_REJECTED = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#ef4444" stroke="#ef4444" stroke-width="2"/><path d="M5.5 5.5l7 7M12.5 5.5l-7 7" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>';
const CHAIN_ICON_CURR = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#2563eb" stroke="#2563eb" stroke-width="2"/><circle cx="9" cy="9" r="3.5" fill="#fff"/></svg>';
const CHAIN_ICON_PENDING = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#fff" stroke="#cbd5e1" stroke-width="2"/></svg>';

/* ============================================================================
 * 详情弹窗
 * ============================================================================ */
function openTicketModal(t, view) {
	_currentApproval = { type: 'ticket', data: t, view: view };
	$('#approvalModalTitle').textContent = '权限变更审批 · ' + t.ticket_no;

	// 审批链渲染
	const chain = (t.approval_chain || []).map((n, i) => {
		// 优先用节点自身 status 判定状态：驳回节点停在 current_step 但 status=REJECTED，
		// 不能仅凭位置判定为"待审批"，否则已驳回的步骤会错误显示为待审批
		const ns = n.status || '';
		const isRejected = ns === 'REJECTED';
		const isApproved = ns === 'APPROVED' || i < t.current_step;
		const cls = isRejected ? 'step-rejected'
			: isApproved ? 'step-done'
			: i === t.current_step ? 'step-curr' : 'step-pending';
		const statusLabel = isRejected ? '已驳回'
			: isApproved ? '已通过'
			: i === t.current_step ? '待审批' : '待处理';
		const statusCls = isRejected ? 'rejected'
			: isApproved ? 'done'
			: i === t.current_step ? 'curr' : 'pending';
		// 已通过/已驳回节点都回填了 approver_id，均展示审批人
		const approverLine = (isApproved || isRejected)
			? (n.approver_name ? `<div class="chain-node-approver">审批人：${escapeHtml(n.approver_name)}</div>` : '')
			: '';
		// 内联 SVG 图标：所有状态统一 18×18
		const iconSvg = isApproved ? CHAIN_ICON_APPROVED
			: isRejected ? CHAIN_ICON_REJECTED
			: i === t.current_step ? CHAIN_ICON_CURR
			: CHAIN_ICON_PENDING;
		return `
		<li class="${cls}">
			${iconSvg}
			<div class="chain-node-role">${_approverRoleLabel(n.approver_role)}
				<span class="chain-node-status ${statusCls}">${statusLabel}</span>
			</div>
			${approverLine}
			${n.comment ? `<div class="chain-node-comment">${escapeHtml(n.comment)}</div>` : ''}
			${n.approved_at ? `<div class="chain-node-time">${formatDate(n.approved_at)}</div>` : ''}
		</li>`;
	}).join('');

	const scopeTxt = t.scope_name || (_scopeTypeLabel(t.scope_type) + (t.scope_id ? ` #${t.scope_id}` : ''));
	const avatarChar = (t.applicant_name || '?').charAt(0).toUpperCase();
	const isSelfApply = t.applicant_id && t.target_user_id && t.applicant_id === t.target_user_id;
	const targetUserCell = isSelfApply
		? `<div class="detail-cell-value">本人申请</div>`
		: `<div class="detail-cell-value">${escapeHtml(t.target_user_name || '—')}</div>
		   <div class="detail-cell-sub">${escapeHtml(t.target_user_email || '')}</div>`;

	// "我已审批"视角追加展示当前用户的审批记录
	const myApprovalHtml = (view === 'processed' && t.my_approver_role)
		? `<div class="detail-section-title">我的审批记录</div>
		   <div class="my-approval-box">
			 <div class="detail-grid">
				<div class="detail-cell">
					<div class="detail-cell-label">审批角色</div>
					<div class="detail-cell-value">${_approverRoleLabel(t.my_approver_role)}</div>
				</div>
				<div class="detail-cell">
					<div class="detail-cell-label">处理时间</div>
					<div class="detail-cell-value">${t.my_approved_at ? formatDate(t.my_approved_at) : '—'}</div>
				</div>
				<div class="detail-cell" style="grid-column:1/-1">
					<div class="detail-cell-label">审批意见</div>
					<div class="detail-cell-value">${escapeHtml(t.my_comment || '—')}</div>
				</div>
			 </div>
		   </div>`
		: '';

	$('#approvalModalBody').innerHTML = `
		<div class="applicant-card">
			<div class="applicant-avatar">${escapeHtml(avatarChar)}</div>
			<div class="applicant-info">
				<div class="applicant-name">${escapeHtml(t.applicant_name || '—')}</div>
				<div class="applicant-meta">${escapeHtml(t.applicant_email || '')}</div>
			</div>
			<div class="applicant-time">
				<div class="applicant-time-label">申请时间</div>
				${formatDate(t.created_at)}
			</div>
		</div>

		<div class="detail-section-title">变更内容</div>
		<div class="detail-grid">
			<div class="detail-cell">
				<div class="detail-cell-label">变更类型</div>
				<div class="detail-cell-value">${_changeTypeLabel(t.change_type)}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">目标用户</div>
				${targetUserCell}
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">${t.change_type === 'ROLE_CHANGE' ? '角色变更' : '目标角色'}</div>
				<div class="detail-cell-value">${_renderRoleChange(t)}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">权限范围</div>
				<div class="detail-cell-value">${escapeHtml(scopeTxt) || '—'}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">生效时间</div>
				<div class="detail-cell-value">${t.effective_from ? formatDate(t.effective_from) : '立即生效'}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">截至日期</div>
				<div class="detail-cell-value">${t.expires_at ? formatDate(t.expires_at) : '长期有效'}</div>
			</div>
		</div>

		<div class="detail-section-title">申请理由</div>
		<div class="reason-box">${escapeHtml(t.reason) || '—'}</div>

		<div class="detail-section-title">审批链进度</div>
		<ol class="chain-timeline">${chain}</ol>
		${t.status === 'PENDING' ? `<div class="current-approver-bar">
			<span>📋</span>
			<span>当前待审批人：${_approverRoleLabel(t.approver_role || '')}</span>
		</div>` : ''}
		${myApprovalHtml}
	`;

	// 操作按钮显隐：仅"待我审批"视角显示通过/拒绝按钮，其他视角只显示关闭
	const showActions = (view === 'pending' && t.status === 'PENDING');
	$('#btnApproveOk').classList.toggle('hidden', !showActions);
	$('#btnApproveReject').classList.toggle('hidden', !showActions);

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
	}[r] || r || '—';
}

function _changeTypeLabel(ct) {
	return {
		'GRANT': '<span class="badge badge-success">授予权限</span>',
		'REVOKE': '<span class="badge badge-warn">撤销权限</span>',
		'ROLE_CHANGE': '<span class="badge badge-info">角色变更</span>',
		'SCOPE_CHANGE': '<span class="badge badge-info">范围变更</span>',
		'EXPIRE_EXTEND': '<span class="badge badge-info">延期</span>',
	}[ct] || ct || '—';
}

function _renderRoleChange(t) {
	if (!t.role_name && !t.previous_role_name) return '—';
	if (t.change_type === 'ROLE_CHANGE' && t.previous_role_name) {
		return `<span class="badge badge-warn">${escapeHtml(t.previous_role_name)}</span>` +
			` <span class="text-sub">→</span> ` +
			`<span class="badge badge-info">${escapeHtml(t.role_name || '—')}</span>`;
	}
	return t.role_name ? `<span class="badge badge-info">${escapeHtml(t.role_name)}</span>` : '—';
}

/* ============================================================================
 * 审批动作（仅"待我审批"视角可用）
 * ============================================================================ */
function onApproveClick() {
	if (!_currentApproval || _currentApproval.view !== 'pending') return;
	const a = _currentApproval;
	showConfirmDialog({
		title: '确认通过审批',
		bannerType: 'success',
		bannerIcon: '✓',
		bannerText: `确认通过工单 ${a.data.ticket_no}？通过后将按审批链流转。`,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">审批意见<span class="form-hint-inline">（选填）</span></label>' +
			'<textarea id="confirmDialogComment" class="input" rows="3" placeholder="可填写备注说明，记录审批意见..."></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: '确认通过', type: 'primary', onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				ctx.close();
				_submitTicketApprove(a.data.id, comment);
			}}
		]
	});
}

function onRejectClick() {
	if (!_currentApproval || _currentApproval.view !== 'pending') return;
	const a = _currentApproval;
	showConfirmDialog({
		title: '驳回理由',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: `确认驳回工单 ${a.data.ticket_no}？驳回后工单将终止流转。`,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">驳回理由<span class="required">*</span></label>' +
			'<textarea id="confirmDialogComment" class="input" rows="4" placeholder="必填，请说明驳回原因，便于申请人了解问题..."></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: '确认驳回', type: 'danger', onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				if (!comment) { ctx.setError('驳回理由不能为空'); return; }
				ctx.close();
				_submitTicketReject(a.data.id, comment);
			}}
		],
		onShow: ctx => {
			const ta = ctx.el.querySelector('#confirmDialogComment');
			if (ta) {
				ta.focus();
				ta.addEventListener('keydown', (e) => {
					if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
						ctx.el.querySelector('.btn-reject')?.click();
					}
				});
			}
		}
	});
}

function _submitTicketApprove(id, comment) {
	if (_submitting) return;
	_submitting = true;
	api.postJson(`/api/v1/auth/permissions/tickets/${id}/approve/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast(`工单已通过，状态：${_ticketStatusLabel(res.status)}`, 'success');
				closeModal('approvalModal');
				_currentApproval = null;
				loadPendingList();
			} else {
				toast(res?.detail || '审批失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '审批失败'), 'error');
		console.error(err);
	}).finally(() => { _submitting = false; });
}

function _submitTicketReject(id, comment) {
	if (_submitting) return;
	_submitting = true;
	api.postJson(`/api/v1/auth/permissions/tickets/${id}/reject/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast('工单已驳回', 'success');
				closeModal('approvalModal');
				_currentApproval = null;
				loadPendingList();
			} else {
				toast(res?.detail || '驳回失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '驳回失败'), 'error');
		console.error(err);
	}).finally(() => { _submitting = false; });
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
