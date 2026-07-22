/* ============ 用户与角色管理（弹窗版） ============ */
let currentPage = 1, totalPages = 1, pageSize = 10;
let filterOptions = {};
let tempCrossScopes = [];
let tempScopePerms = {};  // { 'dept_5': ['read','upload'], 'team_3': ['read','edit'] }

const $id = id => document.getElementById(id);

const ALL_ACTIONS = ['read', 'upload', 'edit', 'delete', 'export', 'share'];
const ACTION_LABELS = { read: '读', upload: '上传', edit: '编辑', delete: '删除', export: '导出', share: '分享' };

function closeModal(id) {
	$id(id).style.display = 'none';
	$id('mask').style.display = 'none';
}

function showModal(id) {
	$id(id).style.display = 'flex';
	$id('mask').style.display = 'block';
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

async function loadFilterOptions() {
	try {
		const savedDept = $id('userDept').value;  // 保存当前部门值
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
		if (savedDept) $id('userDept').value = savedDept;  // 恢复之前的值
		populateRoleRadios(null);
		populateTeamCheckboxes([]);
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
		totalPages = Math.ceil(data.count / pageSize) || 1;
		renderTable(data.results || []);
		renderPagination();
	} catch (e) {
		$id('userTable').innerHTML = '<tr><td colspan="8" class="text-sub" style="text-align:center;padding:30px">加载失败</td></tr>';
		console.error(e);
	}
}

function renderTable(users) {
	const tbody = $id('userTable');
	if (users.length === 0) {
		tbody.innerHTML = '<tr><td colspan="8" class="text-sub text-sm" style="text-align:center;padding:30px">暂无用户</td></tr>';
		return;
	}
	tbody.innerHTML = users.map(u => {
		const roleNames = (u.roles || []).map(r => escapeHtml(r.role__name)).join(', ') || '—';
		const deptName = escapeHtml(u.department_name) || '—';
		const statusTag = u.status === 'active'
			? '<span class="tag tag-sm" style="background:#e8f5e9;color:#2e7d32">启用</span>'
			: '<span class="tag tag-sm" style="background:#fce4ec;color:#c62828">禁用</span>';
		const toggleLabel = u.status === 'active' ? '禁用' : '启用';
		return `<tr>
      <td><input type="checkbox" class="user-check" value="${u.id}"></td>
      <td><strong>${escapeHtml(u.username)}</strong></td>
      <td>${escapeHtml(u.real_name) || '—'}</td>
      <td>${escapeHtml(u.email) || '—'}</td>
      <td>${deptName}</td>
      <td><span class="text-sm">${roleNames}</span></td>
      <td>${statusTag}</td>
      <td>
        <div style="display:flex;gap:4px">
          <button class="btn btn-sm btn-outline" onclick="openUserModal(${u.id})">编辑</button>
          <button class="btn btn-sm btn-outline" onclick="toggleUserStatus(${u.id})">${toggleLabel}</button>
          <button class="btn btn-sm btn-outline" onclick="exportUser(${u.id})">导出</button>
          <button class="btn btn-sm btn-outline" style="color:var(--danger)" onclick="deleteUser(${u.id})">删除</button>
        </div>
      </td>
    </tr>`;
	}).join('');
}

function renderPagination() {
	$id('paginationInfo').textContent = `共 ${(currentPage - 1) * pageSize + 1}-${Math.min(currentPage * pageSize, totalPages * pageSize)} 条，第 ${currentPage}/${totalPages} 页`;
	let btns = '';
	const maxVisible = 7;
	if (totalPages <= maxVisible) {
		for (let i = 1; i <= totalPages; i++) {
			btns += `<button class="btn btn-sm ${i === currentPage ? 'btn-primary' : ''}" onclick="goPage(${i})" style="min-width:36px">${i}</button>`;
		}
	} else {
		if (currentPage <= 3) {
			for (let i = 1; i <= 4; i++) {
				btns += `<button class="btn btn-sm ${i === currentPage ? 'btn-primary' : ''}" onclick="goPage(${i})" style="min-width:36px">${i}</button>`;
			}
			btns += `<span class="text-sub" style="padding:0 4px">...</span>`;
			btns += `<button class="btn btn-sm" onclick="goPage(${totalPages})" style="min-width:36px">${totalPages}</button>`;
		} else if (currentPage >= totalPages - 2) {
			btns += `<button class="btn btn-sm" onclick="goPage(1)" style="min-width:36px">1</button>`;
			btns += `<span class="text-sub" style="padding:0 4px">...</span>`;
			for (let i = totalPages - 3; i <= totalPages; i++) {
				btns += `<button class="btn btn-sm ${i === currentPage ? 'btn-primary' : ''}" onclick="goPage(${i})" style="min-width:36px">${i}</button>`;
			}
		} else {
			btns += `<button class="btn btn-sm" onclick="goPage(1)" style="min-width:36px">1</button>`;
			btns += `<span class="text-sub" style="padding:0 4px">...</span>`;
			for (let i = currentPage - 1; i <= currentPage + 1; i++) {
				btns += `<button class="btn btn-sm ${i === currentPage ? 'btn-primary' : ''}" onclick="goPage(${i})" style="min-width:36px">${i}</button>`;
			}
			btns += `<span class="text-sub" style="padding:0 4px">...</span>`;
			btns += `<button class="btn btn-sm" onclick="goPage(${totalPages})" style="min-width:36px">${totalPages}</button>`;
		}
	}
	$id('paginationBtns').innerHTML = btns;
}

function goPage(p) { currentPage = p; loadUsers(); }

function toggleCheckAll() {
	const checked = $id('checkAll').checked;
	document.querySelectorAll('.user-check').forEach(c => c.checked = checked);
}

async function exportUser(id) {
	const blob = await api.getJson(`/api/v1/auth/users/${id}/export/`);
	downloadBlob(blob, `user_${id}.csv`);
}

async function batchExport() {
	const ids = [...document.querySelectorAll('.user-check:checked')].map(c => c.value);
	if (ids.length === 0) { toast('请先勾选用户', 'warning'); return; }
	const blob = await api.post('/api/v1/auth/users/batch_export/', JSON.stringify({ ids })).then(r => r.blob());
	downloadBlob(blob, 'users_export.csv');
}

async function exportAll() {
	const blob = await api.post('/api/v1/auth/users/batch_export/', JSON.stringify({ ids: [] })).then(r => r.blob());
	downloadBlob(blob, 'users_export.csv');
}

function downloadBlob(blob, filename) {
	const url = URL.createObjectURL(blob);
	const a = document.createElement('a');
	a.href = url; a.download = filename; a.click();
	URL.revokeObjectURL(url);
}

async function deleteUser(id) {
	if (!confirm('确认删除该用户？此操作为软删除。')) return;
	try { await api.deleteJson(`/api/v1/auth/users/${id}/`); loadUsers(); toast('已删除', 'success'); }
	catch (e) { toast('删除失败: ' + e.message, 'error'); }
}

async function toggleUserStatus(id) {
	try {
		const data = await api.postJson(`/api/v1/auth/users/${id}/toggle_status/`, {});
		toast(data.status === 'disabled' ? '已禁用' : '已启用', 'success');
		loadUsers();
	} catch (e) { toast('操作失败: ' + e.message, 'error'); }
}

// ====================== 弹窗：新建/编辑用户 ======================
function openUserModal(id) {
	$id('editUserId').value = '';
	$id('userUsername').value = '';
	$id('userRealName').value = '';
	$id('userEmail').value = '';
	$id('userPassword').value = '';
	$id('userDept').value = '';
	$id('userStatus').value = 'active';
	$id('pwdLabel').innerHTML = '密码 <span class="required">*</span>';
	$id('userPassword').style.display = '';
	$id('userModalTitle').textContent = '新建用户';
	$id('crossScopeSearch').value = '';
	$id('crossScopeActions').style.display = 'none';
	$id('crossScopeResults').style.display = 'none';
	tempCrossScopes = [];
	tempScopePerms = {};
	populateRoleRadios(null);
	populateTeamCheckboxes([]);
	renderScopePerms();
	renderCrossScopes();

	if (!filterOptions.roles) loadFilterOptions().then(() => {
		$id('editUserId').value = '';
		populateRoleRadios(null);
		populateTeamCheckboxes([]);
		renderScopePerms();
	});

	if (id) {
		$id('userModalTitle').textContent = '编辑用户';
		$id('pwdLabel').textContent = '密码（留空不修改）';
		$id('userPassword').placeholder = '留空不修改';
		api.getJson('/api/v1/auth/users/?page_size=200').then(data => {
			const users = data.results || [];
			const u = users.find(x => x.id === id);
			if (u) {
				$id('editUserId').value = u.id;
				$id('userUsername').value = u.username;
				$id('userRealName').value = u.real_name || '';
				$id('userEmail').value = u.email || '';
				$id('userDept').value = u.department_id || '';
				$id('userStatus').value = u.status || 'active';
				// 单选角色：取第一个角色 ID
				const roleId = (u.roles && u.roles.length > 0) ? u.roles[0].role__id : null;
				populateRoleRadios(roleId);
				renderRolePermSummary();
				const selectedTeamIds = (u.teams || []).map(t => t.team__id);
				populateTeamCheckboxes(selectedTeamIds);
				// 加载跨域授权
				tempCrossScopes = (u.cross_scope_access || []).map(cs => ({
					scope_type: cs.scope_type,
					scope_id: cs.scope_id,
					name: cs.department_name || cs.team_name || `${cs.scope_type}:${cs.scope_id}`,
					actions: cs.actions ? cs.actions.split(',').map(s => s.trim()).filter(Boolean) : ['read']
				}));
				// 加载本域文档操作权限
				tempScopePerms = {};
				(u.scope_permissions || []).forEach(sp => {
					const key = `${sp.scope_type}_${sp.scope_id}`;
					tempScopePerms[key] = sp.actions ? sp.actions.split(',').map(s => s.trim()).filter(Boolean) : ['read'];
				});
				renderScopePerms();
				renderCrossScopes();
			}
		}).catch(e => console.error('获取用户详情失败:', e));
	}
	showModal('userModal');
}

function populateRoleRadios(selectedId) {
	const roles = filterOptions.roles || [];
	$id('userRolesCheckbox').innerHTML = [
		`<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">
      <input type="radio" name="userRole" value="" ${!selectedId ? 'checked' : ''} class="role-rb" style="accent-color:var(--primary)" onchange="renderRolePermSummary()"> 无
    </label>`,
		...roles.map(r => {
			const checked = selectedId === r.id ? 'checked' : '';
			return `<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">
        <input type="radio" name="userRole" value="${r.id}" ${checked} class="role-rb" style="accent-color:var(--primary)" onchange="renderRolePermSummary()"> ${escapeHtml(r.name)}
      </label>`;
		})
	].join('');
	renderRolePermSummary();
}

function renderRolePermSummary() {
	const checked = document.querySelector('.role-rb:checked');
	const roleId = checked ? parseInt(checked.value) : 0;
	if (!roleId) {
		$id('rolePermSummary').textContent = '';
		return;
	}
	const roles = filterOptions.roles || [];
	const r = roles.find(r => r.id === roleId);
	if (!r) { $id('rolePermSummary').textContent = ''; return; }
	const desc = {
		super_admin: '全部文档+人员管理',
		kb_ops: '知识库全部操作（无人员管理）',
		dept_manager: '本部门文档全部操作',
		team_leader: '本团队文档全部操作',
		employee: '编辑个人文档，检索本部门/团队',
		readonly: '仅检索和在线预览',
	};
	$id('rolePermSummary').innerHTML = `« ${r.name}: ${desc[r.code] || '自定义权限'}`;
}

function populateTeamCheckboxes(selectedIds) {
	const teams = filterOptions.teams || [];
	$id('userTeamsCheckbox').innerHTML = teams.length === 0
		? '<span class="text-sub text-sm">暂无团队</span>'
		: teams.map(t => {
			const checked = selectedIds.includes(t.id) ? 'checked' : '';
			return `<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">
        <input type="checkbox" value="${t.id}" ${checked} class="team-cb" style="width:16px;height:16px;accent-color:var(--primary)" onchange="onTeamChange()"> ${escapeHtml(t.name)}
      </label>`;
		}).join('');
}

// ====================== 文档操作权限（本部门/团队） ======================
function onDeptChange() {
	// 部门变更时，重置对应权限为默认 (read)
	const deptId = parseInt($id('userDept').value) || 0;
	// 清理旧部门 key
	Object.keys(tempScopePerms).forEach(k => {
		if (k.startsWith('department_')) delete tempScopePerms[k];
	});
	if (deptId && !tempScopePerms[`department_${deptId}`]) {
		tempScopePerms[`department_${deptId}`] = ['read'];
	}
	renderScopePerms();
}

function onTeamChange() {
	// 团队勾选变更时，为新团队加默认 read，移除取消勾选的团队
	const checkedIds = [...document.querySelectorAll('.team-cb:checked')].map(c => parseInt(c.value));
	const teams = filterOptions.teams || [];
	// 为新勾选的团队加默认权限
	checkedIds.forEach(tid => {
		const key = `team_${tid}`;
		if (!tempScopePerms[key]) tempScopePerms[key] = ['read'];
	});
	// 移除已取消勾选的团队权限
	Object.keys(tempScopePerms).forEach(k => {
		if (k.startsWith('team_')) {
			const tid = parseInt(k.replace('team_', ''));
			if (!checkedIds.includes(tid)) delete tempScopePerms[k];
		}
	});
	renderScopePerms();
}

function toggleScopeAction(scopeKey, action) {
	if (!tempScopePerms[scopeKey]) tempScopePerms[scopeKey] = [];
	const idx = tempScopePerms[scopeKey].indexOf(action);
	if (idx > -1) {
		tempScopePerms[scopeKey].splice(idx, 1);
	} else {
		tempScopePerms[scopeKey].push(action);
	}
	if (tempScopePerms[scopeKey].length === 0) tempScopePerms[scopeKey] = ['read'];
}

function renderScopePerms() {
	const deptId = parseInt($id('userDept').value) || 0;
	const checkedTeamIds = [...document.querySelectorAll('.team-cb:checked')].map(c => parseInt(c.value));
	const depts = filterOptions.departments || [];
	const teams = filterOptions.teams || [];

	let rows = [];
	// 部门行
	if (deptId) {
		const dept = depts.find(d => d.id === deptId);
		const deptName = dept ? escapeHtml(dept.name) : `部门#${deptId}`;
		const key = `department_${deptId}`;
		const actions = tempScopePerms[key] || ['read'];
		rows.push({ label: `部门: ${deptName}`, key, type: 'dept', actions });
	}
	// 团队行
	checkedTeamIds.forEach(tid => {
		const team = teams.find(t => t.id === tid);
		const teamName = team ? escapeHtml(team.name) : `团队#${tid}`;
		const key = `team_${tid}`;
		const actions = tempScopePerms[key] || ['read'];
		rows.push({ label: `团队: ${teamName}`, key, type: 'team', actions });
	});

	if (rows.length === 0) {
		$id('scopePermList').innerHTML = '<span class="text-sub text-sm">请先在上方选择部门或团队，然后定义操作权限</span>';
		return;
	}

	$id('scopePermList').innerHTML = rows.map(r => {
		const actionBoxes = ALL_ACTIONS.map(a => {
			const checked = r.actions.includes(a) ? 'checked' : '';
			return `<label style="display:inline-flex;align-items:center;gap:3px;font-size:12px;cursor:pointer;white-space:nowrap">
        <input type="checkbox" ${checked} onchange="toggleScopeAction('${r.key}','${a}');renderScopePerms()" style="accent-color:var(--primary);width:14px;height:14px">
        ${ACTION_LABELS[a]}
      </label>`;
		}).join('');
		return `<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;padding:6px 10px;background:var(--bg-sub);border-radius:6px">
      <span style="font-weight:500;font-size:13px;min-width:90px">${r.label}</span>
      <div style="display:flex;gap:10px;flex-wrap:wrap">${actionBoxes}</div>
    </div>`;
	}).join('');
}

// ====================== 跨域访问授权（搜索方案） ======================
function searchCrossScope() {
	const q = ($id('crossScopeSearch').value || '').trim().toLowerCase();
	const resultsDiv = $id('crossScopeResults');
	if (!q) { resultsDiv.style.display = 'none'; return; }

	const depts = (filterOptions.departments || []).filter(d => {
		if (tempCrossScopes.some(cs => cs.scope_type === 'department' && cs.scope_id === d.id)) return false;
		return d.name.toLowerCase().includes(q) || (d.code || '').toLowerCase().includes(q);
	});
	// 构建 dept_id → dept_name 映射，用于团队显示
	const deptMap = {};
	(filterOptions.departments || []).forEach(d => { deptMap[d.id] = d.name; });
	const teams = (filterOptions.teams || []).filter(t => {
		if (tempCrossScopes.some(cs => cs.scope_type === 'team' && cs.scope_id === t.id)) return false;
		return t.name.toLowerCase().includes(q) || (t.code || '').toLowerCase().includes(q);
	});

	let html = '';
	if (depts.length === 0 && teams.length === 0) {
		html = '<div style="padding:10px 14px;font-size:13px;color:var(--text-sub)">无匹配结果</div>';
	} else {
		depts.forEach(d => {
			html += `<div style="padding:8px 14px;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:8px"
                        onmousedown="selectCrossScopeTarget('department',${d.id},'${d.name.replace(/'/g, "\\'")}')"
                        onmouseover="this.style.background='var(--primary-light)'" onmouseout="this.style.background=''">
            <span class="tag tag-sm" style="background:var(--primary-lighter);color:var(--primary)">部门</span> ${escapeHtml(d.name)}
          </div>`;
		});
		teams.forEach(t => {
			const deptName = deptMap[t.department_id] || '';
			const displayName = deptName ? `${deptName}-${t.name}` : t.name;
			html += `<div style="padding:8px 14px;font-size:13px;cursor:pointer;display:flex;align-items:center;gap:8px"
                        onmousedown="selectCrossScopeTarget('team',${t.id},'${displayName.replace(/'/g, "\\'")}')"
                        onmouseover="this.style.background='var(--primary-light)'" onmouseout="this.style.background=''">
            <span class="tag tag-sm" style="background:#d1fae5;color:#065f46">团队</span> ${escapeHtml(displayName)}
          </div>`;
		});
	}
	resultsDiv.innerHTML = html;
	resultsDiv.style.display = 'block';
}

function selectCrossScopeTarget(type, id, name) {
	$id('crossScopePendingType').value = type;
	$id('crossScopePendingId').value = id;
	$id('crossScopePendingName').value = name;
	$id('crossScopeSelectedLabel').textContent = `[${type === 'department' ? '部门' : '团队'}] ${name}`;
	$id('crossScopeResults').style.display = 'none';
	$id('crossScopeSearch').value = name;
	// 渲染动作勾选框
	$id('crossScopeActionBoxes').innerHTML = ALL_ACTIONS.map(a => {
		const checked = a === 'read' ? 'checked' : '';
		return `<label style="display:inline-flex;align-items:center;gap:3px;font-size:13px;cursor:pointer;white-space:nowrap">
      <input type="checkbox" value="${a}" ${checked} class="csa-check" style="accent-color:var(--primary)">
      ${ACTION_LABELS[a]}
    </label>`;
	}).join('');
	$id('crossScopeActions').style.display = 'block';
}

function addCrossScope() {
	const type = $id('crossScopePendingType').value;
	const id = parseInt($id('crossScopePendingId').value);
	const name = $id('crossScopePendingName').value;
	if (!id) { toast('请先选择目标', 'warning'); return; }
	const actions = [...document.querySelectorAll('.csa-check:checked')].map(c => c.value);
	if (actions.length === 0) { toast('请至少选择一个动作', 'warning'); return; }
	tempCrossScopes.push({ scope_type: type, scope_id: id, name, actions });
	$id('crossScopeActions').style.display = 'none';
	$id('crossScopeSearch').value = '';
	$id('crossScopeResults').style.display = 'none';
	renderCrossScopes();
}

function removeCrossScope(idx) {
	tempCrossScopes.splice(idx, 1);
	renderCrossScopes();
	$id('crossScopeResults').style.display = 'none';
}

function renderCrossScopes() {
	$id('crossScopeList').innerHTML = tempCrossScopes.length === 0
		? '<span class="text-sub text-sm">暂无跨域授权</span>'
		: tempCrossScopes.map((cs, i) => {
			const actionsStr = (cs.actions || ['read']).map(a => ACTION_LABELS[a] || a).join(', ');
			return `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;padding:6px 10px;background:var(--bg-sub);border-radius:6px;font-size:13px">
        <span style="font-weight:500">[${cs.scope_type === 'department' ? '部门' : '团队'}] ${escapeHtml(cs.name)}</span>
        <span class="text-sub" style="font-size:12px">${actionsStr}</span>
        <button class="tag-close-btn" style="color:var(--danger);margin-left:auto" onclick="removeCrossScope(${i})">×</button>
      </div>`;
		}).join('');
}

async function saveUser() {
	const id = $id('editUserId').value;
	const scpPerms = Object.entries(tempScopePerms).map(([key, actions]) => {
		const [type, sid] = key.split('_');
		return { scope_type: type, scope_id: parseInt(sid), actions: actions.join(',') };
	});
	const roleRb = document.querySelector('.role-rb:checked');
	const selectedRoleId = roleRb ? parseInt(roleRb.value) || null : null;
	const base = {
		username: $id('userUsername').value.trim(),
		real_name: $id('userRealName').value.trim(),
		email: $id('userEmail').value.trim(),
		department_id: $id('userDept').value ? parseInt($id('userDept').value) : null,
		status: $id('userStatus').value,
		role_ids: selectedRoleId ? [selectedRoleId] : [],
		team_ids: [...document.querySelectorAll('.team-cb:checked')].map(c => parseInt(c.value)),
		cross_scope_access: tempCrossScopes.map(cs => ({
			scope_type: cs.scope_type, scope_id: cs.scope_id,
			actions: (cs.actions || ['read']).join(',')
		})),
		scope_permissions: scpPerms,
	};
	if (!base.username || !base.real_name) { toast('用户名和姓名为必填', 'warning'); return; }
	try {
		if (id) {
			await api.patchJson(`/api/v1/auth/users/${id}/`, base);
			toast('用户已更新', 'success');
		} else {
			base.password = $id('userPassword').value;
			if (!base.password) { toast('请输入密码', 'warning'); return; }
			if (base.password.length < 8) { toast('密码至少8位', 'warning'); return; }
			if (!/[A-Za-z]/.test(base.password) || !/[0-9]/.test(base.password)) {
				toast('密码需包含字母和数字', 'warning');
				return;
			}
			await api.postJson('/api/v1/auth/users/', base);
			toast('用户已创建', 'success');
		}
		closeModal('userModal');
		loadUsers();
	} catch (e) { toast('保存失败: ' + e.message, 'error'); }
}

// 页面加载时自动初始化
document.addEventListener('DOMContentLoaded', () => {
	initUsersPage();
});

// 点击页面其他地方关闭跨域搜索下拉
document.addEventListener('click', function (e) {
	const srch = $id('crossScopeSearch');
	const results = $id('crossScopeResults');
	if (srch && results && !srch.contains(e.target) && !results.contains(e.target)) {
		results.style.display = 'none';
	}
});
