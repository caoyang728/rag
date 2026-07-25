/* ============ 用户与角色管理（弹窗版） ============ */
let currentPage = 1, totalPages = 1, totalCount = 0, pageSize = 10;
let filterOptions = {};
let tempCrossScopes = [];
let tempScopePerms = {};  // { 'department_5': ['read','upload'], 'team_3': ['read','edit'] }

const $id = id => document.getElementById(id);

const ALL_ACTIONS = ['read', 'upload', 'edit', 'delete', 'export', 'share'];
const ACTION_LABELS = { read: '读', upload: '上传', edit: '编辑', delete: '删除', export: '导出', share: '分享' };

/** 从 <template> 中获取 HTML 并替换 {{key}} 占位符 */
function fillTemplate(templateId, data) {
	const tpl = $id(templateId);
	if (!tpl) return '';
	let html = tpl.innerHTML;
	for (const [key, value] of Object.entries(data)) {
		html = html.replaceAll('{{' + key + '}}', value != null ? String(value) : '');
	}
	return html;
}

function initUsersPage() {
	const searchInput = $id('searchInput');
	if (searchInput) {
		searchInput.value = '';
		searchInput.setAttribute('value', '');
	}
	loadFilterOptions();
	loadUsers();
}

async function loadFilterOptions(force = false) {
	if (filterOptions.roles && !force) return;
	try {
		filterOptions = await api.getJson('/api/v1/auth/users/form_options/');
		const fDept = $id('filterDept');
		fDept.innerHTML = '<option value="">全部部门</option>' +
			(filterOptions.departments || []).map(d => `<option value="${d.id}">${d.name}</option>`).join('');
		const fRole = $id('filterRole');
		fRole.innerHTML = '<option value="">全部角色</option>' +
			(filterOptions.roles || []).map(r => `<option value="${r.id}">${r.name}</option>`).join('');
		const uDept = $id('userDept');
		uDept.innerHTML = '<option value="">— 无 —</option>' +
			(filterOptions.departments || []).map(d => `<option value="${d.id}">${d.name}</option>`).join('');
		populateRoleSelect(null);
		populateTeamSelect(0, null);
		renderScopePerms();
	} catch (e) { console.error('加载筛选项失败:', e); }
}

async function searchUsers() {
	currentPage = 1;
	loadUsers();
}

async function loadUsers() {
	const params = new URLSearchParams({ page: currentPage, page_size: pageSize });
	const q = $id('searchInput').value.trim();
	if (q) params.set('search', q);
	const fd = $id('filterDept').value;
	if (fd) params.set('department_id', fd);
	const fr = $id('filterRole').value;
	if (fr) params.set('role_id', fr);
	const fs = $id('filterStatus').value;
	if (fs) params.set('status', fs);
	try {
		const data = await api.getJson(`/api/v1/auth/users/?${params}`);
		totalCount = data.count || 0;
		totalPages = Math.ceil(totalCount / pageSize) || 1;
		renderTable(data.results || []);
		renderPagination();
	} catch (e) {
		$id('userTable').innerHTML = '<tr><td colspan="8" class="text-sub" style="text-align:center;padding:28px">加载失败</td></tr>';
		console.error(e);
	}
}

function renderTable(users) {
	const tbody = $id('userTable');
	if (users.length === 0) {
		tbody.innerHTML = '<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px">暂无用户</td></tr>';
		return;
	}
	tbody.innerHTML = users.map(u => {
		const roleNames = (u.roles || []).map(r => escapeHtml(r.role__name)).join(', ') || '—';
		const deptName = escapeHtml(u.department_name) || '—';
		const statusTag = u.status === 'active'
			? '<span class="tag tag-sm" style="background:#e8f5e9;color:#2e7d32">启用</span>'
			: '<span class="tag tag-sm" style="background:#fce4ec;color:#c62828">禁用</span>';
		const toggleLabel = u.status === 'active' ? '禁用' : '启用';
		return fillTemplate('tmpl-user-row', {
			id: u.id,
			username: escapeHtml(u.username),
			real_name: escapeHtml(u.real_name) || '—',
			email: escapeHtml(u.email) || '—',
			dept_name: deptName,
			role_names: roleNames,
			status_tag: statusTag,
			toggle_label: toggleLabel,
		});
	}).join('');
}

function renderPagination() {
	$id('paginationInfo').textContent = `共 ${totalCount} 条，第 ${currentPage}/${totalPages} 页`;
	let btns = '';
	const maxVisible = 7;
	if (totalPages <= maxVisible) {
		for (let i = 1; i <= totalPages; i++) {
			btns += fillTemplate('tmpl-pagination-btn', {
				page: i,
				active: i === currentPage ? 'btn-primary' : '',
			});
		}
	} else {
		if (currentPage <= 3) {
			for (let i = 1; i <= 4; i++) {
				btns += fillTemplate('tmpl-pagination-btn', { page: i, active: i === currentPage ? 'btn-primary' : '' });
			}
			btns += $id('tmpl-pagination-ellipsis').innerHTML;
			btns += fillTemplate('tmpl-pagination-btn', { page: totalPages, active: '' });
		} else if (currentPage >= totalPages - 2) {
			btns += fillTemplate('tmpl-pagination-btn', { page: 1, active: '' });
			btns += $id('tmpl-pagination-ellipsis').innerHTML;
			for (let i = totalPages - 3; i <= totalPages; i++) {
				btns += fillTemplate('tmpl-pagination-btn', { page: i, active: i === currentPage ? 'btn-primary' : '' });
			}
		} else {
			btns += fillTemplate('tmpl-pagination-btn', { page: 1, active: '' });
			btns += $id('tmpl-pagination-ellipsis').innerHTML;
			for (let i = currentPage - 1; i <= currentPage + 1; i++) {
				btns += fillTemplate('tmpl-pagination-btn', { page: i, active: i === currentPage ? 'btn-primary' : '' });
			}
			btns += $id('tmpl-pagination-ellipsis').innerHTML;
			btns += fillTemplate('tmpl-pagination-btn', { page: totalPages, active: '' });
		}
	}
	$id('paginationBtns').innerHTML = btns;
}

function goPage(p) { currentPage = p; loadUsers(); }

function toggleCheckAll() {
	const checked = $id('checkAll').checked;
	document.querySelectorAll('.user-check').forEach(c => c.checked = checked);
}

async function batchExport() {
	const ids = [...document.querySelectorAll('.user-check:checked')].map(c => c.value);
	if (ids.length === 0) { toast('请先勾选用户', 'warning'); return; }
	const blob = await api.post('/api/v1/auth/users/batch_export/', JSON.stringify({ ids })).then(r => r.blob());
	downloadBlob(blob, 'users_export.csv');
	toast('导出成功', 'success');
}

async function exportAll() {
	const blob = await api.post('/api/v1/auth/users/batch_export/', JSON.stringify({ ids: [] })).then(r => r.blob());
	downloadBlob(blob, 'users_export.csv');
	toast('导出成功', 'success');
}

function downloadBlob(blob, filename) {
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url; a.download = filename; a.click();
	URL.revokeObjectURL(url);
}

async function deleteUserFromModal() {
	const id = $id('editUserId').value;
	const username = $id('userUsername').value;
	if (!id) return;
	if (!confirm(`确认删除用户 "${username}"？此操作为软删除。`)) return;
	try {
		await api.deleteJson(`/api/v1/auth/users/${id}/`);
		closeModal('userModal');
		loadUsers();
		toast('已删除', 'success');
	} catch (e) { toast('删除失败: ' + escapeHtml(e.message), 'error'); }
}

async function toggleUserStatus(id) {
	try {
		const data = await api.postJson(`/api/v1/auth/users/${id}/toggle_status/`, {});
		toast(data.status === 'disabled' ? '已禁用' : '已启用', 'success');
		loadUsers();
	} catch (e) { toast('操作失败: ' + escapeHtml(e.message), 'error'); }
}

// ====================== 团队下拉框（参考部门样式） ======================
function populateTeamSelect(deptId, selectedTeamId) {
	const teams = filterOptions.teams || [];
	const filtered = deptId ? teams.filter(t => t.department_id === deptId) : [];
	const sel = $id('userDeptTeam');
	if (!deptId) {
		sel.innerHTML = '<option value="">请先选择部门</option>';
		return;
	}
	if (filtered.length === 0) {
		sel.innerHTML = '<option value="">该部门暂无团队</option>';
		return;
	}
	sel.innerHTML = '<option value="">— 未选择 —</option>' +
		filtered.map(t => `<option value="${t.id}" ${selectedTeamId === t.id ? 'selected' : ''}>${escapeHtml(t.name)}</option>`).join('');
}

function onTeamSelectChange() {
	if ($id('userDeptTeam').disabled) return;
	const teamId = parseInt($id('userDeptTeam').value) || 0;
	// 清理旧团队 key
	Object.keys(tempScopePerms).forEach(k => {
		if (k.startsWith('team_')) delete tempScopePerms[k];
	});
	if (teamId) {
		const roleId = parseInt($id('userRoleSelect').value) || 0;
		const roles = filterOptions.roles || [];
		const r = roles.find(r => r.id === roleId);
		tempScopePerms[`team_${teamId}`] = getRoleTeamDefaultActions(r ? r.code : '');
	}
	renderScopePerms();
}

// ====================== 弹窗：新建/编辑用户 ======================
function openUserModal(id) {
	$id('editUserId').value = '';
	$id('userUsername').value = '';
	$id('userRealName').value = '';
	$id('userEmail').value = '';
	$id('userPassword').value = '';
	$id('userDept').value = '';
	$id('userDeptTeam').innerHTML = '<option value="">请先选择部门</option>';
	$id('userRoleSelect').value = '';
	$id('userStatus').value = 'active';
	$id('pwdLabel').innerHTML = '密码 <span class="required">*</span>';
	$id('userPassword').style.display = '';
	$id('userModalTitle').textContent = '新建用户';
	$id('crossScopeSearch').value = '';
	$id('crossScopeActions').classList.add('hidden');
	$id('crossScopeResults').classList.add('hidden');
	$id('rolePermSummary').textContent = '';
	tempCrossScopes = [];
	tempScopePerms = {};
	$id('modalDeleteBtn').classList.add('hidden');

	// 用户名：新建时可编辑，编辑时禁用
	const usernameInput = $id('userUsername');
	usernameInput.disabled = !!id;

	if (id) {
		usernameInput.style.background = 'var(--bg)';
		usernameInput.style.color = 'var(--text-sub)';
	} else {
		usernameInput.style.background = '';
		usernameInput.style.color = '';
	}

	populateRoleSelect(null);
	populateTeamSelect(0, null);
	renderScopePerms();
	renderCrossScopes();

	if (id) {
		$id('userModalTitle').textContent = '编辑用户';
		$id('modalDeleteBtn').classList.remove('hidden');
		$id('pwdLabel').textContent = '密码（留空不修改）';
		$id('userPassword').placeholder = '留空不修改';
		const loadUserData = () => api.getJson(`/api/v1/auth/users/${id}/`).then(u => {
			$id('editUserId').value = u.id;
			$id('userUsername').value = u.username;
			$id('userRealName').value = u.real_name || '';
			$id('userEmail').value = u.email || '';
			const deptId = u.department_id || 0;
			$id('userDept').value = deptId;
			$id('userStatus').value = u.status || 'active';
			const roleId = (u.roles && u.roles.length > 0) ? u.roles[0].role__id : null;
			$id('userRoleSelect').value = roleId || '';
			onRoleChange();
			// 团队
			const selectedTeamId = (u.teams && u.teams.length > 0) ? u.teams[0].team__id : null;
			populateTeamSelect(deptId, selectedTeamId);

			tempCrossScopes = (u.cross_scope_access || []).map(cs => ({
				scope_type: cs.scope_type,
				scope_id: cs.scope_id,
				name: cs.department_name || cs.team_name || `${cs.scope_type}:${cs.scope_id}`,
				actions: cs.actions ? cs.actions.split(',').map(s => s.trim()).filter(Boolean) : ['read']
			}));
			tempScopePerms = {};
			(u.scope_permissions || []).forEach(sp => {
				const key = `${sp.scope_type}_${sp.scope_id}`;
				tempScopePerms[key] = sp.actions ? sp.actions.split(',').map(s => s.trim()).filter(Boolean) : ['read'];
			});
			renderScopePerms();
			renderCrossScopes();
		}).catch(e => console.error('获取用户详情失败:', e));
		if (!filterOptions.roles) {
			loadFilterOptions().then(loadUserData);
		} else {
			loadUserData();
		}
	} else {
		if (!filterOptions.roles) {
			loadFilterOptions();
		}
	}
	showModal('userModal');
}

// ====================== 角色下拉框 ======================
function populateRoleSelect(selectedId) {
	const roles = filterOptions.roles || [];
	$id('userRoleSelect').innerHTML = '<option value="">— 无 —</option>' +
		roles.map(r => `<option value="${r.id}" ${selectedId === r.id ? 'selected' : ''}>${escapeHtml(r.name)}</option>`).join('');
}

function onRoleChange() {
	renderRolePermSummary();
	updateTeamDisabledState();
	// 新建用户时自动应用角色默认权限；编辑时不覆盖已保存权限
	if (!$id('editUserId').value) {
		applyRoleDefaultPerms();
	}
}

function renderRolePermSummary() {
	const roleId = parseInt($id('userRoleSelect').value) || 0;
	if (!roleId) {
		$id('rolePermSummary').textContent = '';
		return;
	}
	const roles = filterOptions.roles || [];
	const r = roles.find(r => r.id === roleId);
	if (!r) { $id('rolePermSummary').textContent = ''; return; }
	const desc = {
		super_admin: '全部文档+人员管理',
		kb_admin: '知识库管理+人员管理',
		kb_ops: '知识库全部操作（无人员管理）',
		dept_manager: '本部门文档全部操作',
		team_leader: '本团队文档全部操作',
		employee: '编辑个人文档，检索本部门/团队',
		readonly: '仅检索和在线预览',
	};
	$id('rolePermSummary').innerHTML = `${r.name}: ${desc[r.code] || '自定义权限'}`;
}

function getRoleDefaultActions(roleCode) {
	switch (roleCode) {
		case 'dept_manager': return [...ALL_ACTIONS];
		case 'team_leader': return ['read', 'upload'];
		case 'employee': return ['read', 'upload'];
		case 'readonly': return ['read'];
		default: return ['read'];
	}
}

function getRoleTeamDefaultActions(roleCode) {
	if (roleCode === 'team_leader') return [...ALL_ACTIONS];
	return getRoleDefaultActions(roleCode);
}

function applyRoleDefaultPerms() {
	const roleId = parseInt($id('userRoleSelect').value) || 0;
	const roles = filterOptions.roles || [];
	const r = roles.find(r => r.id === roleId);
	if (!r) return;

	const deptId = parseInt($id('userDept').value) || 0;
	const teamId = parseInt($id('userDeptTeam').value) || 0;

	// 部门权限
	if (deptId) {
		tempScopePerms[`department_${deptId}`] = getRoleDefaultActions(r.code);
	}

	// 团队权限（部门经理禁用）
	if (r.code === 'dept_manager') {
		Object.keys(tempScopePerms).forEach(k => {
			if (k.startsWith('team_')) delete tempScopePerms[k];
		});
	} else if (teamId) {
		tempScopePerms[`team_${teamId}`] = getRoleTeamDefaultActions(r.code);
	}
	renderScopePerms();
}

function updateTeamDisabledState() {
	const roleId = parseInt($id('userRoleSelect').value) || 0;
	const roles = filterOptions.roles || [];
	const r = roles.find(r => r.id === roleId);
	const isDisabled = r && r.code === 'dept_manager';

	$id('userDeptTeam').disabled = isDisabled;

	const teamTrigger = document.querySelector('#permTeamSelect .multi-select-trigger');
	if (teamTrigger) {
		if (isDisabled) {
			teamTrigger.classList.add('disabled');
			const panel = document.querySelector('#permTeamSelect .multi-select-panel');
			if (panel) panel.classList.remove('show');
			teamTrigger.classList.remove('open');
		} else {
			teamTrigger.classList.remove('disabled');
		}
	}

	if (isDisabled) {
		const teamLabel = document.querySelector('#permTeamSelect .multi-select-label');
		if (teamLabel) teamLabel.textContent = '不适用';
	}
}

// ====================== 部门变更 ======================
function onDeptChange() {
	const deptId = parseInt($id('userDept').value) || 0;
	// 重置团队下拉
	populateTeamSelect(deptId, null);
	// 清理旧部门/团队 key
	Object.keys(tempScopePerms).forEach(k => {
		if (k.startsWith('department_') || k.startsWith('team_')) delete tempScopePerms[k];
	});
	if (deptId) {
		const roleId = parseInt($id('userRoleSelect').value) || 0;
		const roles = filterOptions.roles || [];
		const r = roles.find(r => r.id === roleId);
		tempScopePerms[`department_${deptId}`] = getRoleDefaultActions(r ? r.code : '');
	}
	renderScopePerms();
}

// ====================== 文档权限渲染（multi-select 下拉框直接展示操作权限） ======================
function renderScopePerms() {
	const deptId = parseInt($id('userDept').value) || 0;
	renderPermDeptPanel(deptId);
	renderPermTeamPanel(deptId);
}

function renderPermDeptPanel(deptId) {
	const panel = $id('permDeptPanel');

	if (!deptId) {
		panel.innerHTML = $id('tmpl-perm-dept-placeholder').innerHTML;
		updatePermDeptCount();
		return;
	}

	const key = `department_${deptId}`;
	const actions = tempScopePerms[key] || ['read'];

	panel.innerHTML = ALL_ACTIONS.map(a => {
		return fillTemplate('tmpl-perm-dept-option', {
			action: a,
			checked: actions.includes(a) ? 'checked' : '',
			label: ACTION_LABELS[a],
		});
	}).join('');
	updatePermDeptCount();
}

function renderPermTeamPanel(deptId) {
	const panel = $id('permTeamPanel');
	const teamId = parseInt($id('userDeptTeam').value) || 0;

	if (!deptId) {
		panel.innerHTML = $id('tmpl-perm-team-placeholder-dept').innerHTML;
		updatePermTeamCount();
		return;
	}

	if (!teamId) {
		panel.innerHTML = $id('tmpl-perm-team-placeholder-team').innerHTML;
		updatePermTeamCount();
		return;
	}

	const key = `team_${teamId}`;
	const actions = tempScopePerms[key] || ['read'];

	panel.innerHTML = ALL_ACTIONS.map(a => {
		return fillTemplate('tmpl-perm-team-option', {
			action: a,
			checked: actions.includes(a) ? 'checked' : '',
			label: ACTION_LABELS[a],
		});
	}).join('');
	updatePermTeamCount();
}

// 部门权限 action checkbox 变化
function onPermDeptActionChg(cb, action) {
	const deptId = parseInt($id('userDept').value) || 0;
	if (!deptId) return;
	const key = `department_${deptId}`;
	if (!tempScopePerms[key]) tempScopePerms[key] = ['read'];
	if (cb.checked) {
		if (!tempScopePerms[key].includes(action)) tempScopePerms[key].push(action);
	} else {
		tempScopePerms[key] = tempScopePerms[key].filter(a => a !== action);
	}
	if (tempScopePerms[key].length === 0) tempScopePerms[key] = ['read'];
	updatePermDeptCount();
}

function onPermDeptActionClick(row, e, action) {
	if (e.target.tagName === 'INPUT') return;
	const cb = row.querySelector('input');
	cb.checked = !cb.checked;
	onPermDeptActionChg(cb, action);
}

// 团队权限 action checkbox 变化
function onPermTeamActionChg(cb, action) {
	const teamId = parseInt($id('userDeptTeam').value) || 0;
	if (!teamId) return;
	const key = `team_${teamId}`;
	if (!tempScopePerms[key]) tempScopePerms[key] = ['read'];
	if (cb.checked) {
		if (!tempScopePerms[key].includes(action)) tempScopePerms[key].push(action);
	} else {
		tempScopePerms[key] = tempScopePerms[key].filter(a => a !== action);
	}
	if (tempScopePerms[key].length === 0) tempScopePerms[key] = ['read'];
	updatePermTeamCount();
}

function onPermTeamActionClick(row, e, action) {
	if (e.target.tagName === 'INPUT') return;
	const cb = row.querySelector('input');
	cb.checked = !cb.checked;
	onPermTeamActionChg(cb, action);
}

function updatePermDeptCount() {
	const deptId = parseInt($id('userDept').value) || 0;
	const key = `department_${deptId}`;
	const actions = tempScopePerms[key] || [];
	const count = actions.length;
	const countEl = $id('permDeptCount');
	const labelEl = document.querySelector('#permDeptSelect .multi-select-label');
	if (!deptId) {
		countEl.classList.add('hidden');
		labelEl.textContent = '请先选择部门';
	} else if (count > 0) {
		countEl.textContent = count;
		countEl.classList.remove('hidden');
		labelEl.textContent = '已选 ' + count + ' 个权限';
	} else {
		countEl.classList.add('hidden');
		labelEl.textContent = '选择操作权限';
	}
}

function updatePermTeamCount() {
	const teamId = parseInt($id('userDeptTeam').value) || 0;
	const key = `team_${teamId}`;
	const actions = tempScopePerms[key] || [];
	const count = actions.length;
	const countEl = $id('permTeamCount');
	const labelEl = document.querySelector('#permTeamSelect .multi-select-label');
	const trigger = document.querySelector('#permTeamSelect .multi-select-trigger');
	const deptId = parseInt($id('userDept').value) || 0;
	if (!deptId) {
		countEl.classList.add('hidden');
		labelEl.textContent = '请先选择部门';
		trigger.classList.add('disabled');
	} else if (!teamId) {
		countEl.classList.add('hidden');
		labelEl.textContent = '请先选择团队';
		trigger.classList.add('disabled');
	} else if (count > 0) {
		countEl.textContent = count;
		countEl.classList.remove('hidden');
		labelEl.textContent = '已选 ' + count + ' 个权限';
		trigger.classList.remove('disabled');
	} else {
		countEl.classList.add('hidden');
		labelEl.textContent = '选择操作权限';
		trigger.classList.remove('disabled');
	}
}

// ====================== 跨域访问授权（搜索方案） ======================
function searchCrossScope() {
	const q = ($id('crossScopeSearch').value || '').trim().toLowerCase();
	const resultsDiv = $id('crossScopeResults');
	if (!q) { resultsDiv.classList.add('hidden'); return; }

	const depts = (filterOptions.departments || []).filter(d => {
		if (tempCrossScopes.some(cs => cs.scope_type === 'department' && cs.scope_id === d.id)) return false;
		return d.name.toLowerCase().includes(q) || (d.code || '').toLowerCase().includes(q);
	});
	const deptMap = {};
	(filterOptions.departments || []).forEach(d => { deptMap[d.id] = d.name; });
	const teams = (filterOptions.teams || []).filter(t => {
		if (tempCrossScopes.some(cs => cs.scope_type === 'team' && cs.scope_id === t.id)) return false;
		return t.name.toLowerCase().includes(q) || (t.code || '').toLowerCase().includes(q);
	});

	let html = '';
	if (depts.length === 0 && teams.length === 0) {
		html = $id('tmpl-cross-result-empty').innerHTML;
	} else {
		depts.forEach(d => {
			html += fillTemplate('tmpl-cross-result-dept', {
				id: d.id,
				name: escapeHtml(d.name),
			});
		});
		teams.forEach(t => {
			const deptName = deptMap[t.department_id] || '';
			const displayName = deptName ? `${deptName}-${t.name}` : t.name;
			html += fillTemplate('tmpl-cross-result-team', {
				id: t.id,
				name: escapeHtml(displayName),
			});
		});
	}
	resultsDiv.innerHTML = html;
	resultsDiv.classList.remove('hidden');
}

function selectCrossScopeTarget(type, id, name) {
	$id('crossScopePendingType').value = type;
	$id('crossScopePendingId').value = id;
	$id('crossScopePendingName').value = name;
	$id('crossScopeSelectedLabel').textContent = `[${type === 'department' ? '部门' : '团队'}] ${name}`;
	$id('crossScopeResults').classList.add('hidden');
	$id('crossScopeSearch').value = name;
	$id('crossScopeActionBoxes').innerHTML = ALL_ACTIONS.map(a => {
		return fillTemplate('tmpl-cross-action-checkbox', {
			action: a,
			checked: a === 'read' ? 'checked' : '',
			label: ACTION_LABELS[a],
		});
	}).join('');
	$id('crossScopeActions').classList.remove('hidden');
}

function addCrossScope() {
	const type = $id('crossScopePendingType').value;
	const id = parseInt($id('crossScopePendingId').value);
	const name = $id('crossScopePendingName').value;
	if (!id) { toast('请先选择目标', 'warning'); return; }
	const actions = [...document.querySelectorAll('.csa-check:checked')].map(c => c.value);
	if (actions.length === 0) { toast('请至少选择一个动作', 'warning'); return; }
	tempCrossScopes.push({ scope_type: type, scope_id: id, name, actions });
	$id('crossScopeActions').classList.add('hidden');
	$id('crossScopeSearch').value = '';
	$id('crossScopeResults').classList.add('hidden');
	renderCrossScopes();
}

function removeCrossScope(idx) {
	tempCrossScopes.splice(idx, 1);
	renderCrossScopes();
	$id('crossScopeResults').classList.add('hidden');
}

function renderCrossScopes() {
	if (tempCrossScopes.length === 0) {
		$id('crossScopeList').innerHTML = $id('tmpl-cross-selected-empty').innerHTML;
		return;
	}
	$id('crossScopeList').innerHTML = tempCrossScopes.map((cs, i) => {
		const actionsStr = (cs.actions || ['read']).map(a => ACTION_LABELS[a] || a).join(', ');
		return fillTemplate('tmpl-cross-selected-item', {
			type_label: cs.scope_type === 'department' ? '部门' : '团队',
			name: escapeHtml(cs.name),
			actions_str: actionsStr,
			index: i,
		});
	}).join('');
}

async function saveUser() {
	const id = $id('editUserId').value;
	const scpPerms = Object.entries(tempScopePerms).map(([key, actions]) => {
		const [type, sid] = key.split('_');
		return { scope_type: type, scope_id: parseInt(sid), actions: actions.join(',') };
	});
	const roleId = parseInt($id('userRoleSelect').value) || null;
	const teamId = parseInt($id('userDeptTeam').value) || 0;
	const base = {
		real_name: $id('userRealName').value.trim(),
		email: $id('userEmail').value.trim(),
		department_id: $id('userDept').value ? parseInt($id('userDept').value) : null,
		status: $id('userStatus').value,
		role_ids: roleId ? [roleId] : [],
		team_ids: teamId ? [teamId] : [],
		cross_scope_access: tempCrossScopes.map(cs => ({
			scope_type: cs.scope_type, scope_id: cs.scope_id,
			actions: (cs.actions || ['read']).join(',')
		})),
		scope_permissions: scpPerms,
	};
	// 新建时需验证用户名
	if (!id) {
		base.username = $id('userUsername').value.trim();
		if (!base.username) { toast('用户名不能为空', 'warning'); return; }
	}
	if (!base.real_name) { toast('姓名为必填', 'warning'); return; }
	try {
		if (id) {
			await api.patchJson(`/api/v1/auth/users/${id}/`, base);
			toast('用户已更新', 'success');
		} else {
			base.password = $id('userPassword').value;
			if (!base.password) { toast('请输入密码', 'warning'); return; }
			if (base.password.length < 8) { toast('密码至少8位', 'warning'); return; }
			if (!/[A-Z]/.test(base.password)) { toast('密码需包含大写字母', 'warning'); return; }
			if (!/[a-z]/.test(base.password)) { toast('密码需包含小写字母', 'warning'); return; }
			if (!/\d/.test(base.password)) { toast('密码需包含数字', 'warning'); return; }
			await api.postJson('/api/v1/auth/users/', base);
			toast('用户已创建', 'success');
		}
		closeModal('userModal');
		loadUsers();
	} catch (e) { toast('保存失败: ' + escapeHtml(e.message), 'error'); }
}

// 页面加载时自动初始化
document.addEventListener('DOMContentLoaded', () => {
	initUsersPage();

	// 跨域搜索结果点击事件委托
	document.getElementById('crossScopeResults').addEventListener('click', function (e) {
		const item = e.target.closest('.cross-scope-item');
		if (item) {
			const type = item.getAttribute('data-type');
			const id = item.getAttribute('data-id');
			const name = item.getAttribute('data-name');
			selectCrossScopeTarget(type, parseInt(id), name);
		}
	});
});

// 点击页面其他地方关闭跨域搜索下拉
document.addEventListener('click', function (e) {
	const srch = $id('crossScopeSearch');
	const results = $id('crossScopeResults');
	if (srch && results && !srch.contains(e.target) && !results.contains(e.target)) {
		results.classList.add('hidden');
	}
});
