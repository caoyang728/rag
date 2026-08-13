/* ============================================================================
 * ticket.js —— 工单中心
 *
 * 全部类型工单（权限审批/配置变更/定时任务/模型变更）统一在一页展示，
 * 用筛选器区分，数据源为统一工单中心 API /api/v1/auth/tickets/。
 *
 * 四视角：
 * 1. 待我审批：当前用户可处理的 PENDING 工单（权限域共享审批池 + 系统域审核/复核）
 * 2. 我已审批：当前用户已处理过的工单（含通过/驳回记录）
 * 3. 我的工单：当前用户作为申请人提交的工单（所有状态，PENDING 可撤回）
 * 4. 全部工单：按角色可见范围展示（超管全量 / 管理员按权限域 / 部门经理/组长按归属 / 个人仅自己）
 *
 * 页面访问：所有登录用户开放，可见范围由后端 _ticket_visible_scope 按角色过滤
 * ============================================================================ */

// 当前视角：pending / processed / mine / all
let _currentView = 'pending';
// 当前列表数据缓存（供行点击回查用）
let _currentRows = [];
// 当前打开的审批对象（供通过/驳回/撤回按钮使用）
let _currentApproval = null;
// 提交防重锁（防止审批通过/驳回/撤回重复提交）
let _submitting = false;
// 当前分页页码（每页 _PAGE_SIZE 条，由后端 page_size 控制）
let _currentPage = 1;
let _PAGE_SIZE = 20;

// 统一工单中心 API 前缀
const _TICKET_API = '/api/v1/auth/tickets/';

/* ============ 页面启动 ============ */
document.addEventListener('DOMContentLoaded', () => {
	// 行点击事件委托：绑定在 tbody 上一次，翻页/刷新后无需重复绑定
	$('#ticketTable').addEventListener('click', e => {
		const tr = e.target.closest('tr[data-ticket-id]');
		if (!tr) return;
		const id = +tr.dataset.ticketId;
		const data = _currentRows.find(r => r.id === id);
		if (data) openTicketModal(data, _currentView);
	});
	// 合规管理员默认看"全部工单"（审计视角，不参与审批）
	const isPureCompliance = hasAnyRole('compliance_admin')
		&& !hasAnyRole('super_admin', 'user_admin', 'dept_manager', 'team_leader', 'kb_admin');
	if (isPureCompliance) {
		switchView('all');
	} else {
		loadList();
	}
	// 10 分钟自动轮询：页面可见时定时刷新列表与红点；切到后台暂停，回到前台立即刷新并恢复。
	// MPA 整页跳转时页面卸载，定时器随之销毁，无需额外处理"切页"场景。
	startTicketPolling();
	document.addEventListener('visibilitychange', () => {
		if (document.hidden) {
			stopTicketPolling();
		} else {
			_pollRefresh();
			startTicketPolling();
		}
	});
});

/* ============================================================================
 * 视角切换与统一加载
 * ============================================================================ */
function switchView(view) {
	_currentView = view;
	_currentPage = 1; // 切换视角回到第一页
	document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('tab-active'));
	$('#tab-' + view).classList.add('tab-active');
	// 状态筛选下拉框：仅"全部工单"视角显示
	$('#allStatusFilter').classList.toggle('hidden', view !== 'all');
	// 时间列标题：我已审批=处理时间，其他=申请时间
	const thTime = $('#th-time');
	if (thTime) thTime.textContent = view === 'processed' ? '处理时间' : '申请时间';
	loadList();
}

function refreshCurrent() {
	_currentPage = 1; // 筛选/搜索条件变化后回到第一页
	loadList();
}

// 请求序号：loadList 每次调用自增，只有最新请求的响应才会被应用。
// 防止快速连续操作（切条数后立即翻页、连续点页码等）时旧请求后返回覆盖新状态，
// 导致表格数据与分页状态错乱。
let _requestSeq = 0;

/* ============================================================================
 * 统一工单列表加载（全类型）
 * ============================================================================ */
function loadList(opts) {
	// 静默刷新（自动轮询）：不显示"加载中"占位，保留旧数据直至新数据返回，避免列表闪烁
	const silent = !!(opts && opts.silent);
	const seq = ++_requestSeq;
	const tbody = $('#ticketTable');
	if (!silent) tbody.innerHTML = _loadingRow(9);

	// 组装查询参数：视角 + 类型 + 状态 + 搜索 + 分页
	const params = new URLSearchParams();
	params.set('view', _currentView);
	params.set('page', _currentPage);
	params.set('page_size', _PAGE_SIZE);
	const type = ($('#typeFilter')?.value || '').trim();
	if (type) params.set('type', type);
	const status = ($('#allStatusFilter')?.value || '').trim();
	if (status) params.set('status', status);
	const search = ($('#searchInput')?.value || '').trim();
	if (search) params.set('search', search);

	api.getJson(_TICKET_API + '?' + params.toString())
		.then(res => {
			if (seq !== _requestSeq) return; // 已有更新的请求发出，丢弃本次旧响应
			const count = res?.count || 0;
			const totalPages = Math.max(1, Math.ceil(count / _PAGE_SIZE));
			// 数据量减少（如工单被处理/撤回）导致当前页越界时，回退到最后一页重新加载
			if (_currentPage > totalPages) {
				_currentPage = totalPages;
				loadList();
				return;
			}
			const rows = res?.rows || [];
			_currentRows = rows;
			if (_currentView === 'pending') _setBadge('pending', count || rows.length);
			_renderTable(rows, _currentView);
			_renderPagination(count);
		})
		.catch(err => {
			if (seq !== _requestSeq) return;
			// 静默刷新失败：保留现有列表与红点，仅告警日志，不打扰用户
			if (silent) { console.warn('工单轮询刷新失败:', err); return; }
			_setBadge('pending', 0);
			_renderPagination(0);
			_renderTableError('加载工单失败');
			console.error(err);
		});
}

/* ============================================================================
 * 表格渲染（全类型）
 * ============================================================================ */
function _renderTable(rows, view) {
	const tbody = $('#ticketTable');
	if (!rows.length) {
		const emptyText = {
			'pending': '暂无待审批工单',
			'processed': '暂无已审批记录',
			'mine': '暂无工单记录',
			'all': '暂无工单',
		}[view] || '暂无数据';
		tbody.innerHTML = `<tr><td colspan="9" class="text-sub text-sm text-center" style="padding:30px">${emptyText}</td></tr>`;
		return;
	}
	tbody.innerHTML = rows.map(t => _renderTicketRow(t, view)).join('');
	// 行点击已委托到 tbody（DOMContentLoaded 绑定一次），翻页/刷新无需重复绑定
}

/* Pagination 组件是否已初始化（首次用 render，后续用 update） */
let _paginationInited = false;

/**
 * 分页渲染：委托给通用 Pagination 组件。
 * 首次调用时创建组件并绑定回调，后续仅更新状态。
 * 组件提供居中对齐 + 每页条数切换（10/20/50）。
 */
function _renderPagination(count) {
	const totalPages = Math.max(1, Math.ceil((count || 0) / _PAGE_SIZE));
	if (!_paginationInited) {
		// 首次渲染：创建组件，绑定翻页和每页条数回调
		Pagination.render({
			container: '#ticketPagination',
			page: _currentPage,
			totalPages: totalPages,
			total: count,
			pageSize: _PAGE_SIZE,
			align: 'center',
			// pageSizeOptions: [10, 20, 50],
			onPageChange(p) { _currentPage = p; loadList(); },
			onPageSizeChange(size) { _PAGE_SIZE = size; _currentPage = 1; loadList(); },
		});
		_paginationInited = true;
	} else {
		// 后续刷新：仅更新状态，复用已有 DOM 和回调
		Pagination.update({ page: _currentPage, totalPages: totalPages, total: count, pageSize: _PAGE_SIZE });
	}
}

function _renderTableError(msg) {
	$('#ticketTable').innerHTML =
		`<tr><td colspan="9" class="text-sub text-sm text-center" style="padding:30px;color:var(--danger)">${msg}，请稍后重试</td></tr>`;
}

function _loadingRow(colspan) {
	return `<tr><td colspan="${colspan}" class="text-sub text-sm text-center" style="padding:30px">加载中...</td></tr>`;
}

// 类型显示映射
const _bizTypeMap = {
	'permission': '<span class="badge badge-info">权限审批</span>',
	'config': '<span class="badge badge-warn">配置变更</span>',
	'schedule': '<span class="badge badge-default">定时任务</span>',
	'model': '<span class="badge badge-danger">模型变更</span>',
	'org': '<span class="badge badge-info">组织变更</span>',
	'security': '<span class="badge badge-warn">安全设置</span>',
};

function _renderTicketRow(t, view) {
	// 类型列
	const typeBadge = _bizTypeMap[t.biz_type] || t.biz_type || '—';
	// 目标列：permission=目标用户+角色；config/schedule=配置项；model=模型名
	let targetHtml = '—';
	if (t.biz_type === 'permission') {
		const scopeTxt = t.scope_name || '';
		const permParts = [
			t.role_name ? `<strong>${escapeHtml(t.role_name)}</strong>` : '',
			scopeTxt ? `<span class="text-sub text-sm">(${escapeHtml(scopeTxt)})</span>` : ''
		].filter(Boolean).join(' ');
		targetHtml = `
			<div>${escapeHtml(t.target_user_name || '—')}</div>
			<div class="text-sub text-xs">${permParts || (escapeHtml(t.target_user_email || ''))}</div>`;
	} else if (t.biz_type === 'config' || t.biz_type === 'schedule') {
		targetHtml = `
			<div>${escapeHtml(t.config_label || t.config_key || '—')}</div>
			<div class="text-sub text-xs mono">${escapeHtml(t.config_key || '')}</div>`;
	} else if (t.biz_type === 'model') {
		targetHtml = `<div>${escapeHtml(t.model_name || '—')}</div>`;
	} else if (t.biz_type === 'org') {
		// org=目标部门/团队名 + 操作类型(部门新增/编辑/删除等)
		targetHtml = `
			<div>${escapeHtml(t.org_name || '—')}</div>
			<div class="text-sub text-xs">${escapeHtml(t.operation_display || '')}</div>`;
	} else if (t.biz_type === 'security') {
		// security=目标 IP/敏感词 + 类型/操作(如 IP白名单 · 新增)
		targetHtml = `
			<div class="mono">${escapeHtml(t.security_target || '—')}</div>
			<div class="text-sub text-xs">${escapeHtml(t.security_type_display || '')} · ${escapeHtml(t.operation_display || '')}</div>`;
	}

	// 状态列：待我审批视角固定显示"待审批"，其他视角显示实际状态
	const statusBadge = view === 'pending'
		? '<span class="badge badge-warn">待审批</span>'
		: _ticketStatusBadge(t.status);

	// 时间列：我已审批=处理时间，其他=申请时间
	const timeField = (view === 'processed' && t.approved_at) ? t.approved_at : t.created_at;

	// 进度展示
	const step = (t.current_step || 0) + 1;
	const total = t.total_steps || 1;
	const progressCell = view === 'pending'
		? `<span class="badge badge-info">第 ${step}/${total} 步</span>`
		: `<span class="badge ${total > 0 && t.current_step >= total ? 'badge-success' : 'badge-info'}">${step}/${total}</span>`;

	// 操作按钮：待我审批=处理，我的工单(PENDING)=撤回，其他=查看
	let actionBtn;
	if (view === 'pending') {
		actionBtn = `<button class="btn btn-sm btn-primary">处理</button>`;
	} else if (view === 'mine' && t.status === 'PENDING') {
		actionBtn = `<button class="btn btn-sm btn-outline">撤回</button>`;
	} else {
		actionBtn = `<button class="btn btn-sm btn-outline">查看</button>`;
	}

	// 申请人列：我的工单视角显示"我"，其他视角显示申请人
	const applicantCell = view === 'mine'
		? '<span class="text-sub">我</span>'
		: `<div>${escapeHtml(t.applicant_name || '—')}</div>
		   <div class="text-sub text-xs">${escapeHtml(t.applicant_username || '')}</div>`;

	return `
<tr class="table-row-hover" data-ticket-id="${t.id}" style="cursor:pointer">
	<td><span class="mono text-sm">${t.ticket_no}</span></td>
	<td>${typeBadge}</td>
	<td><span class="text-sm">${escapeHtml(t.title || '—')}</span></td>
	<td>${applicantCell}</td>
	<td>${targetHtml}</td>
	<td>${statusBadge}</td>
	<td><span class="text-sm text-sub">${formatDate(timeField)}</span></td>
	<td>${progressCell}</td>
	<td>${actionBtn}</td>
</tr>`;
}

/* 工单状态文案：badge 与提示消息共用同一份映射，避免两处口径漂移 */
function _statusText(s) {
	return {
		'PENDING': '待审批',
		'APPROVED': '已通过',
		'EXECUTED': '已执行',
		'REJECTED': '已驳回',
		'CANCELLED': '已撤回',
	}[s] || s;
}

function _ticketStatusBadge(s) {
	const cls = {
		'PENDING': 'badge-warn',
		'APPROVED': 'badge-info',
		'EXECUTED': 'badge-success',
		'REJECTED': 'badge-danger',
		'CANCELLED': 'badge',
	}[s] || 'badge';
	return `<span class="badge ${cls}">${_statusText(s)}</span>`;
}

/* 审批链节点 SVG 图标（18×18，viewBox="0 0 18 18"） */
const CHAIN_ICON_APPROVED = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#22c55e" stroke="#22c55e" stroke-width="2"/><path d="M5 9.5l3 3L13 6" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CHAIN_ICON_REJECTED = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#ef4444" stroke="#ef4444" stroke-width="2"/><path d="M5.5 5.5l7 7M12.5 5.5l-7 7" fill="none" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>';
const CHAIN_ICON_CURR = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#2563eb" stroke="#2563eb" stroke-width="2"/><circle cx="9" cy="9" r="3.5" fill="#fff"/></svg>';
const CHAIN_ICON_PENDING = '<svg class="chain-node-icon" viewBox="0 0 18 18"><circle cx="9" cy="9" r="8" fill="#fff" stroke="#cbd5e1" stroke-width="2"/></svg>';

/* ============================================================================
 * 详情弹窗（按类型渲染）
 * ============================================================================ */
function openTicketModal(t, view) {
	_currentApproval = { data: t, view: view };
	$('#approvalModalTitle').textContent = '工单详情 · ' + t.ticket_no;

	// 审批链渲染
	const chain = (t.approval_chain || []).map((n, i) => {
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
		const approverLine = (isApproved || isRejected)
			? (n.approver_name ? `<div class="chain-node-approver">审批人：${escapeHtml(n.approver_name)}</div>` : '')
			: '';
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

	// 业务详情区（按类型渲染）
	const bizHtml = _renderBizDetail(t);

	// "我已审批"视角追加展示当前用户的审批记录
	const myApprovalHtml = (view === 'processed' && t.approval_chain || []).filter(n => n.approver_id && n.approver_name)
		.length ? `<div class="detail-section-title">我的审批记录</div>
			${(t.approval_chain || []).filter(n => n.approver_id).map(n => `
				<div class="my-approval-box mb-8">
					<div class="detail-grid">
						<div class="detail-cell">
							<div class="detail-cell-label">审批角色</div>
							<div class="detail-cell-value">${_approverRoleLabel(n.approver_role)}</div>
						</div>
						<div class="detail-cell">
							<div class="detail-cell-label">处理时间</div>
							<div class="detail-cell-value">${n.approved_at ? formatDate(n.approved_at) : '—'}</div>
						</div>
						<div class="detail-cell" style="grid-column:1/-1">
							<div class="detail-cell-label">审批意见</div>
							<div class="detail-cell-value">${escapeHtml(n.comment || '—')}</div>
						</div>
					</div>
				</div>`).join('')}` : '';

	const avatarChar = (t.applicant_name || '?').charAt(0).toUpperCase();
	const riskLabel = { 'normal': '普通', 'high': '高风险' }[t.risk_level] || t.risk_level || '—';

	$('#approvalModalBody').innerHTML = `
		<div class="applicant-card">
			<div class="applicant-avatar">${escapeHtml(avatarChar)}</div>
			<div class="applicant-info">
				<div class="applicant-name">${escapeHtml(t.applicant_name || '—')}</div>
				<div class="applicant-meta">${escapeHtml(t.applicant_username || '')} · ${escapeHtml(t.applicant_email || '')}</div>
			</div>
			<div class="applicant-time">
				<div class="applicant-time-label">申请时间</div>
				${formatDate(t.created_at)}
			</div>
		</div>

		<div class="detail-section-title">工单信息</div>
		<div class="detail-grid">
			<div class="detail-cell">
				<div class="detail-cell-label">工单类型</div>
				<div class="detail-cell-value">${_bizTypeMap[t.biz_type] || t.biz_type}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">任务名</div>
				<div class="detail-cell-value">${escapeHtml(t.title || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">风险等级</div>
				<div class="detail-cell-value">${riskLabel}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">当前状态</div>
				<div class="detail-cell-value">${_ticketStatusBadge(t.status)}</div>
			</div>
		</div>

		${bizHtml}

		<div class="detail-section-title">审批链进度</div>
		<ol class="chain-timeline">${chain}</ol>
		${t.status === 'PENDING' ? `<div class="current-approver-bar">
			<span>📋</span>
			<span>当前待审批：${_currentApproverLabel(t)}</span>
		</div>` : ''}
		${myApprovalHtml}
	`;

	// 操作按钮显隐：
	// - 待我审批视角 + PENDING：显示通过/驳回
	// - 我的工单视角 + PENDING：显示撤回
	const showActions = (view === 'pending' && t.status === 'PENDING');
	const showWithdraw = (view === 'mine' && t.status === 'PENDING');
	$('#btnApproveOk').classList.toggle('hidden', !showActions);
	$('#btnApproveReject').classList.toggle('hidden', !showActions);
	$('#btnTicketWithdraw').classList.toggle('hidden', !showWithdraw);

	showModal('approvalModal');
}

/* 业务详情区：按 biz_type 渲染 */
function _renderBizDetail(t) {
	if (t.biz_type === 'permission') {
		return _renderPermDetail(t);
	}
	if (t.biz_type === 'config' || t.biz_type === 'schedule') {
		return _renderConfigDetail(t);
	}
	if (t.biz_type === 'model') {
		return _renderModelDetail(t);
	}
	if (t.biz_type === 'org') {
		return _renderOrgDetail(t);
	}
	if (t.biz_type === 'security') {
		return _renderSecurityDetail(t);
	}
	return '';
}

// 安全配置详情:配置类型/操作/目标/申请理由(target_data 快照由后端透传)
function _renderSecurityDetail(t) {
	return `
		<div class="detail-section-title">变更内容</div>
		<div class="detail-grid">
			<div class="detail-cell">
				<div class="detail-cell-label">安全配置类型</div>
				<div class="detail-cell-value">${escapeHtml(t.security_type_display || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">操作</div>
				<div class="detail-cell-value">${escapeHtml(t.operation_display || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">目标</div>
				<div class="detail-cell-value mono">${escapeHtml(t.security_target || '—')}</div>
			</div>
		</div>
		<div class="detail-section-title">申请理由</div>
		<div class="reason-box">${escapeHtml(t.reason) || '—'}</div>`;
}

// 组织变更详情:组织类型/操作/目标/变更前后内容/申请理由
// old_data/new_data 为后端序列化透传的 JSON 快照(如 {name, code, department_id})
function _renderOrgDetail(t) {
	const oldData = t.old_data || {};
	const newData = t.new_data || {};
	// 变更字段并集:新增场景只有 new,删除场景只有 old,编辑场景两者都有
	const keys = t.changed_fields && t.changed_fields.length
		? t.changed_fields
		: (Object.keys(newData).length ? Object.keys(newData) : Object.keys(oldData));
	const fieldLabels = {
		'name': '名称',
		'code': '编码',
		'description': '描述',
		'department_id': '所属部门',
		'department_name': '所属部门',
	};
	const diffRows = keys.map(k => {
		const label = fieldLabels[k] || k;
		const oldV = oldData[k] !== undefined ? String(oldData[k]) : '';
		const newV = newData[k] !== undefined ? String(newData[k]) : '';
		// 新值列:删除场景显示"（删除）";原值列:字段有变化才显示旧值
		const newHtml = newV === '' ? '<span class="text-sub">（删除）</span>' : escapeHtml(newV);
		const oldHtml = (oldV !== '' && String(oldV) !== newV) ? `原值: ${escapeHtml(oldV)}` : '';
		return `
			<div class="detail-cell">
				<div class="detail-cell-label">${escapeHtml(label)}</div>
				<div class="detail-cell-value">${newHtml}</div>
				${oldHtml ? `<div class="detail-cell-sub">${oldHtml}</div>` : ''}
			</div>`;
	}).join('');
	return `
		<div class="detail-section-title">变更内容</div>
		<div class="detail-grid">
			<div class="detail-cell">
				<div class="detail-cell-label">组织类型</div>
				<div class="detail-cell-value">${escapeHtml(t.org_type_display || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">操作</div>
				<div class="detail-cell-value">${escapeHtml(t.operation_display || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">目标</div>
				<div class="detail-cell-value">${escapeHtml(t.org_name || '—')}</div>
			</div>
		</div>
		${diffRows ? `<div class="detail-section-title">变更前后</div><div class="detail-grid">${diffRows}</div>` : ''}
		<div class="detail-section-title">申请理由</div>
		<div class="reason-box">${escapeHtml(t.reason) || '—'}</div>`;
}

function _renderPermDetail(t) {
	const scopeTxt = t.scope_name || (_scopeTypeLabel(t.scope_type) + (t.scope_id ? ` #${t.scope_id}` : ''));
	const isSelfApply = t.applicant_id && t.target_user_id && t.applicant_id === t.target_user_id;
	const targetUserCell = isSelfApply
		? `<div class="detail-cell-value">本人申请</div>`
		: `<div class="detail-cell-value">${escapeHtml(t.target_user_name || '—')}</div>
		   <div class="detail-cell-sub">${escapeHtml(t.target_user_email || '')}</div>`;
	return `
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
		<div class="reason-box">${escapeHtml(t.reason) || '—'}</div>`;
}

function _renderConfigDetail(t) {
	// 变更摘要（BUSINESS_DB_TABLES 等多值项）：added/removed 高亮展示
	let summaryHtml = '';
	if (t.change_summary && (t.change_summary.added || t.change_summary.removed)) {
		const parts = [];
		if (t.change_summary.added && t.change_summary.added.length) {
			parts.push(`<div class="text-sm">新增：<span style="color:var(--success)">${escapeHtml(t.change_summary.added.join('、'))}</span></div>`);
		}
		if (t.change_summary.removed && t.change_summary.removed.length) {
			parts.push(`<div class="text-sm">移除：<span style="color:var(--danger)">${escapeHtml(t.change_summary.removed.join('、'))}</span></div>`);
		}
		summaryHtml = parts.join('');
	}
	const isSecret = t.old_value === '***' && t.new_value === '***';
	return `
		<div class="detail-section-title">变更内容</div>
		<div class="detail-grid">
			<div class="detail-cell">
				<div class="detail-cell-label">配置项</div>
				<div class="detail-cell-value">${escapeHtml(t.config_label || t.config_key || '—')}</div>
				<div class="detail-cell-sub mono">${escapeHtml(t.config_key || '')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">${t.biz_type === 'schedule' ? '操作' : '变更类型'}</div>
				<div class="detail-cell-value">${escapeHtml(t.operation_display || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">原值</div>
				<div class="detail-cell-value" style="word-break:break-all">${escapeHtml(t.old_value === undefined ? '—' : (t.old_value === '' ? '（空）' : t.old_value))}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">新值</div>
				<div class="detail-cell-value" style="word-break:break-all">${escapeHtml(t.new_value === undefined ? '—' : (t.new_value === '' ? '（空）' : t.new_value))}</div>
			</div>
			${isSecret ? '' : (summaryHtml ? `<div class="detail-cell" style="grid-column:1/-1"><div class="detail-cell-label">变更摘要</div><div class="detail-cell-value">${summaryHtml}</div></div>` : '')}
		</div>
		${isSecret ? '<div class="text-sub text-sm" style="margin-top:8px">⚠ 敏感配置项，旧值/新值已掩码</div>' : ''}
		<div class="detail-section-title">变更原因</div>
		<div class="reason-box">${escapeHtml(t.reason) || '—'}</div>`;
}

function _renderModelDetail(t) {
	const fields = (t.changed_fields || []).map(f => {
		const labels = { 'name': '显示名', 'provider': 'Provider', 'model_type': '类型', 'base_url': '接口地址', 'model_name': '模型名', 'timeout': '超时(秒)', 'is_active': '启用状态' };
		return `<span class="badge badge-info">${labels[f] || f}</span>`;
	}).join(' ');
	return `
		<div class="detail-section-title">变更内容</div>
		<div class="detail-grid">
			<div class="detail-cell">
				<div class="detail-cell-label">目标模型</div>
				<div class="detail-cell-value">${escapeHtml(t.model_name || '—')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">操作类型</div>
				<div class="detail-cell-value">${escapeHtml(t.operation_display || '—')}</div>
			</div>
			<div class="detail-cell" style="grid-column:1/-1">
				<div class="detail-cell-label">变更字段</div>
				<div class="detail-cell-value">${fields || '—'}</div>
			</div>
		</div>
		<div class="detail-section-title">变更原因</div>
		<div class="reason-box">${escapeHtml(t.reason) || '—'}</div>`;
}

function _currentApproverLabel(t) {
	const chain = t.approval_chain || [];
	const node = chain[t.current_step] || {};
	if (node.approver_role) {
		return _approverRoleLabel(node.approver_role);
	}
	// 无 approver_role 的旧结构工单：按类型给提示
	return _bizTypeMap[t.biz_type]?.replace(/<[^>]+>/g, '') || '待审批';
}

function _approverRoleLabel(r) {
	return {
		'TEAM_LEADER': '团队组长',
		'DEPT_LEADER': '部门经理',
		'DEPT_MANAGER': '部门经理',
		'USER_ADMIN': '用户管理员',
		'KB_ADMIN': '知识管理员',
		'SUPER_ADMIN': '超级管理员',
		'SYSTEM_AUDITOR': '系统审核员',
	}[r] || r || '—';
}

function _scopeTypeLabel(st) {
	return { 'GLOBAL': '全局', 'DEPT': '部门', 'TEAM': '团队', 'NONE': '—' }[st] || st || '—';
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
 * 审批动作（通过/驳回/撤回）
 * ============================================================================ */

/* 统一意见输入确认弹窗：通过/驳回/撤回三者共用（仅文案与校验不同） */
function _openCommentDialog(opts) {
	// opts: { title, bannerType, bannerIcon, bannerText, commentLabel, placeholder,
	//         required, requiredMsg, rows, confirmText, confirmType, onSubmit(comment) }
	showConfirmDialog({
		title: opts.title,
		bannerType: opts.bannerType,
		bannerIcon: opts.bannerIcon,
		bannerText: opts.bannerText,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">' + opts.commentLabel +
			(opts.required ? '<span class="required">*</span>' : '<span class="form-hint-inline">（选填）</span>') +
			'</label>' +
			'<textarea id="confirmDialogComment" class="input" rows="' + (opts.rows || 3) +
			'" placeholder="' + opts.placeholder + '"></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: opts.confirmText, type: opts.confirmType, onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				if (opts.required && !comment) { ctx.setError(opts.requiredMsg || '必填项不能为空'); return; }
				ctx.close();
				opts.onSubmit(comment);
			}}
		],
		onShow: ctx => {
			// 弹窗打开即聚焦输入框；Ctrl/Cmd+Enter 快捷提交，减少键盘操作
			const ta = ctx.el.querySelector('#confirmDialogComment');
			if (ta) {
				ta.focus();
				ta.addEventListener('keydown', e => {
					if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
						ctx.el.querySelector('.btn-reject, .btn-save')?.click();
					}
				});
			}
		}
	});
}

function onApproveClick() {
	if (!_currentApproval || _currentApproval.view !== 'pending') return;
	const a = _currentApproval;
	_openCommentDialog({
		title: '确认通过审批',
		bannerType: 'success',
		bannerIcon: '✓',
		bannerText: `确认通过工单 ${a.data.ticket_no}？通过后将按审批链流转。`,
		commentLabel: '审批意见',
		placeholder: '可填写备注说明，记录审批意见...',
		confirmText: '确认通过',
		confirmType: 'primary',
		onSubmit: comment => _submitTicketAction('approve', a.data.id, comment),
	});
}

function onRejectClick() {
	if (!_currentApproval || _currentApproval.view !== 'pending') return;
	const a = _currentApproval;
	_openCommentDialog({
		title: '驳回理由',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: `确认驳回工单 ${a.data.ticket_no}？驳回后工单将终止流转。`,
		commentLabel: '驳回理由',
		placeholder: '必填，请说明驳回原因，便于申请人了解问题...',
		required: true,
		requiredMsg: '驳回理由不能为空',
		rows: 4,
		confirmText: '确认驳回',
		confirmType: 'danger',
		onSubmit: comment => _submitTicketAction('reject', a.data.id, comment),
	});
}

function onWithdrawClick() {
	if (!_currentApproval || _currentApproval.view !== 'mine') return;
	const a = _currentApproval;
	_openCommentDialog({
		title: '确认撤回工单',
		bannerType: 'warn',
		bannerIcon: '↩',
		bannerText: `确认撤回工单 ${a.data.ticket_no}？撤回后工单将终止流转。`,
		commentLabel: '撤回原因',
		placeholder: '可填写撤回原因...',
		confirmText: '确认撤回',
		confirmType: 'primary',
		onSubmit: comment => _submitTicketAction('withdraw', a.data.id, comment),
	});
}

function _submitTicketAction(action, id, comment) {
	if (_submitting) return;
	_submitting = true;
	const url = _TICKET_API + id + '/' + action + '/';
	// api.postJson 非 2xx 已抛错（err.status），此处 then 必然成功，无需再判 res 业务字段
	api.postJson(url, { comment })
		.then(res => {
			const msg = {
				// 多节点审批链首次通过时 status 仍为 PENDING，此时提示等待后续节点而非"待审批"
				'approve': (res?.status && res.status !== 'PENDING')
					? `工单已通过，状态：${_statusText(res.status)}`
					: '工单已通过，等待后续节点处理',
				'reject': '工单已驳回',
				'withdraw': '工单已撤回',
			}[action];
			toast(msg, 'success');
			closeModal('approvalModal');
			_currentApproval = null;
			loadList();
		}).catch(err => {
			toast(_errMsg(err, '操作失败'), 'error');
			console.error(err);
		}).finally(() => { _submitting = false; });
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

/* ============================================================================
 * 自动轮询刷新
 * ============================================================================ */

// 统一轮询间隔：10 分钟（需求要求固定间隔，不做角色区分）
const _TICKET_POLL_INTERVAL = 10 * 60 * 1000;
let _pollTimer = null;

function startTicketPolling() {
	stopTicketPolling();
	_pollTimer = setInterval(_pollRefresh, _TICKET_POLL_INTERVAL);
}

function stopTicketPolling() {
	if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// 轮询刷新：静默刷新当前列表（不闪"加载中"）；非待办视角下额外轻量刷新待办红点，
// 保证切到"我已审批/我的工单/全部"视角时红点仍能随轮询自动更新
function _pollRefresh() {
	loadList({ silent: true });
	if (_currentView !== 'pending') _refreshPendingBadge();
}

// 轻量获取待办数：view=pending + page_size=1 仅取 count（1 条返回体），不扰动当前列表
function _refreshPendingBadge() {
	const params = new URLSearchParams({ view: 'pending', page: '1', page_size: '1' });
	api.getJson(_TICKET_API + '?' + params.toString())
		.then(res => _setBadge('pending', res?.count || 0))
		.catch(err => console.warn('待办红点刷新失败:', err));
}
