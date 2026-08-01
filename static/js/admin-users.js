/* ============ 用户与角色管理（弹窗版） ============ */
let currentPage = 1, totalPages = 1, totalCount = 0, pageSize = 20;
let filterOptions = {};
let _loadSeq = 0;       // loadUsers 请求序列号，防止竞态
let _filterLoadSeq = 0; // loadFilterOptions 请求序列号，防止竞态
let sortField = '';      // 当前排序字段（空表示默认 -created_at）
let sortOrder = '';      // '' | 'asc' | 'desc'

const $id = id => document.getElementById(id);

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
	}
	loadFilterOptions();
	loadUsers();
}

async function loadFilterOptions(force = false) {
	if (filterOptions.roles && !force) return;
	const seq = ++_filterLoadSeq;
	try {
		const data = await api.getJson('/api/v1/auth/users/form_options/');
		if (seq !== _filterLoadSeq) return; // 过时请求，忽略
		const currentUserRoles = getUserRoles();

		// 部门/团队筛选：组长和部门经理只看自己的范围
		const isDeptManager = currentUserRoles.includes('dept_manager');
		const isTeamLeader = currentUserRoles.includes('team_leader');
		if (isDeptManager || isTeamLeader) {
			const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
			const myDeptId = u.department_id;
			if (myDeptId) {
				// 仅保留自己的部门
				data.departments = (data.departments || []).filter(d => d.id === myDeptId);
				if (isTeamLeader) {
					// 组长仅保留自己所在的团队
					const myTeamIds = (u.teams || []).map(t => t.id);
					data.teams = (data.teams || []).filter(t => myTeamIds.includes(t.id));
				}
			}
		}

		filterOptions = data;
		const fDept = $id('filterDept');
		fDept.innerHTML = '<option value="">全部部门</option>' +
			(filterOptions.departments || []).map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join('');
		const fRole = $id('filterRole');
		fRole.innerHTML = '<option value="">全部角色</option>' +
			(filterOptions.roles || []).map(r => `<option value="${r.id}">${escapeHtml(r.name)}</option>`).join('');
		const uDept = $id('userDept');
		uDept.innerHTML = '<option value="">— 无 —</option>' +
			(filterOptions.departments || []).map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join('');
		// 初始化时未选择部门，团队筛选应禁用
		$id('filterTeam').disabled = true;
		populateRoleSelect(null);
		populateTeamSelect(0, null);
	} catch (e) { console.error('加载筛选项失败:', e); }
}

function searchUsers() {
	currentPage = 1;
	loadUsers();
}

/** 切换每页条数 */
function onPageSizeChange() {
	pageSize = parseInt($id('pageSizeSelect').value) || 20;
	currentPage = 1;
	loadUsers();
}

let _searchTimer = null;
function onSearchInput() {
	clearTimeout(_searchTimer);
	_searchTimer = setTimeout(() => searchUsers(), 300);
}

function onFilterDeptChange() {
	const deptId = parseInt($id('filterDept').value) || 0;
	const teamSel = $id('filterTeam');
	if (!deptId) {
		// 未选择部门时禁用团队下拉
		teamSel.disabled = true;
		searchUsers();
		return;
	}
	teamSel.disabled = false;
	const teams = filterOptions.teams || [];
	const filtered = teams.filter(t => t.department_id === deptId);
	teamSel.innerHTML = '<option value="">全部团队</option>' +
		filtered.map(t => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join('');
	searchUsers();
}

async function loadUsers() {
	const seq = ++_loadSeq;
	const params = new URLSearchParams({ page: currentPage, page_size: pageSize });
	const q = $id('searchInput').value.trim();
	if (q) params.set('search', q);
	const fd = $id('filterDept').value;
	if (fd) params.set('department_id', fd);
	const ft = $id('filterTeam').value;
	if (ft) params.set('team_id', ft);
	const fr = $id('filterRole').value;
	if (fr) params.set('role_id', fr);
	const fs = $id('filterStatus').value;
	if (fs) params.set('status', fs);
	// 排序参数：DRF OrderingFilter 接受 ordering=field（升序）或 ordering=-field（降序）
	if (sortField) {
		params.set('ordering', (sortOrder === 'desc' ? '-' : '') + sortField);
	}
	try {
		const data = await api.getJson(`/api/v1/auth/users/?${params}`);
		if (seq !== _loadSeq) return;  // 过时请求，忽略
		totalCount = data.count || 0;
		totalPages = Math.ceil(totalCount / pageSize) || 1;
		if (currentPage > totalPages) currentPage = 1;
		renderTable(data.results || []);
		renderPagination();
		renderSortIndicators();
	} catch (e) {
		if (seq !== _loadSeq) return;
		$id('userTable').innerHTML = '<tr><td colspan="9" class="text-sub" style="text-align:center;padding:28px">加载失败</td></tr>';
		console.error(e);
	}
}

/** 点击表头切换排序：同字段切换方向，不同字段切换为该字段升序 */
function onSortChange(field) {
	if (sortField === field) {
		// 同字段：asc → desc → 取消
		if (sortOrder === 'asc') sortOrder = 'desc';
		else if (sortOrder === 'desc') { sortField = ''; sortOrder = ''; }
		else sortOrder = 'asc';
	} else {
		sortField = field;
		sortOrder = 'asc';
	}
	currentPage = 1;
	loadUsers();
}

/** 渲染表头排序指示器（↑/↓） */
function renderSortIndicators() {
	document.querySelectorAll('.sort-indicator').forEach(el => {
		el.textContent = '';
	});
	if (sortField) {
		const indicator = $id('sort-' + sortField);
		if (indicator) indicator.textContent = sortOrder === 'asc' ? '↑' : '↓';
	}
}

function renderTable(users) {
	const tbody = $id('userTable');
	if (users.length === 0) {
		tbody.innerHTML = '<tr><td colspan="9" class="text-sub text-sm text-center" style="padding:30px">暂无用户</td></tr>';
		return;
	}
	tbody.innerHTML = users.map(u => {
		const roleNames = (u.roles || []).map(r => escapeHtml(r.name)).join(', ') || '—';
		const deptName = escapeHtml(u.department_name) || '—';
		const teamName = (u.teams || []).map(t => escapeHtml(t.name)).join(', ') || '—';
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
			team_name: teamName,
			role_names: roleNames,
			status_tag: statusTag,
			toggle_label: toggleLabel,
		});
	}).join('');
}

function renderPagination() {
	$id('paginationInfo').textContent = `共 ${totalCount} 条，第 ${currentPage}/${totalPages} 页`;
	const btns = [];
	// 首页：第 1 页时禁用
	btns.push(`<button class="btn btn-sm page-btn" onclick="goPage(1)" ${currentPage <= 1 ? 'disabled' : ''}>«</button>`);
	// 上一页：第 1 页时禁用
	btns.push(`<button class="btn btn-sm page-btn" onclick="goPage(${currentPage - 1})" ${currentPage <= 1 ? 'disabled' : ''}>‹</button>`);
	// 页码：最多显示 5 个，当前页尽量居中
	if (totalPages <= 5) {
		for (let i = 1; i <= totalPages; i++) btns.push(renderPageBtn(i));
	} else {
		let start = Math.max(1, currentPage - 2);
		const end = Math.min(totalPages, start + 4);
		if (end - start < 4) start = Math.max(1, end - 4);
		for (let i = start; i <= end; i++) btns.push(renderPageBtn(i));
	}
	// 下一页：末页时禁用
	btns.push(`<button class="btn btn-sm page-btn" onclick="goPage(${currentPage + 1})" ${currentPage >= totalPages ? 'disabled' : ''}>›</button>`);
	// 末页：末页时禁用
	btns.push(`<button class="btn btn-sm page-btn" onclick="goPage(${totalPages})" ${currentPage >= totalPages ? 'disabled' : ''}>»</button>`);
	$id('paginationBtns').innerHTML = btns.join('');
}

/** 渲染单个页码按钮 */
function renderPageBtn(page) {
	const active = page === currentPage ? 'btn-primary' : '';
	return `<button class="btn btn-sm ${active} page-btn" onclick="goPage(${page})">${page}</button>`;
}

function goPage(p) {
	if (!Number.isInteger(p) || p < 1 || p > totalPages) return;
	currentPage = p; loadUsers();
}

function toggleCheckAll() {
	const checked = $id('checkAll').checked;
	document.querySelectorAll('.user-check').forEach(c => c.checked = checked);
}

async function batchExport() {
	const ids = [...document.querySelectorAll('.user-check:checked')].map(c => c.value);
	if (ids.length === 0) { toast('请先勾选用户', 'warning'); return; }
	try {
		const blob = await api.post('/api/v1/auth/users/batch_export/', JSON.stringify({ ids })).then(r => r.blob());
		downloadBlob(blob, 'users_export.csv');
		toast('导出成功', 'success');
	} catch (e) { toast('导出失败: ' + escapeHtml(e.message), 'error'); }
}

async function exportAll() {
	try {
		const blob = await api.post('/api/v1/auth/users/batch_export/', JSON.stringify({ ids: [] })).then(r => r.blob());
		downloadBlob(blob, 'users_export.csv');
		toast('导出成功', 'success');
	} catch (e) { toast('导出失败: ' + escapeHtml(e.message), 'error'); }
}

/** 触发隐藏的 file input，选择 CSV 文件 */
function batchImport() {
	$id('importFileInput').click();
}

/** 处理 CSV 文件上传：发送到后端批量导入接口，下载带结果列的 CSV */
async function handleImportFile(event) {
	const file = event.target.files[0];
	if (!file) return;
	// 重置 input value，允许再次选择同一文件
	event.target.value = '';
	if (!file.name.toLowerCase().endsWith('.csv')) {
		toast('请选择 .csv 文件', 'warning');
		return;
	}
	const formData = new FormData();
	formData.append('file', file);
	try {
		// FormData 上传不能通过 api 对象（会强制设 Content-Type: application/json）
		// 直接用 fetch + 手动携带 token，让浏览器自动设置 multipart/form-data; boundary=...
		const token = api.getToken();
		const resp = await fetch('/api/v1/auth/users/batch_import/', {
			method: 'POST',
			headers: token ? { 'Authorization': `Bearer ${token}` } : {},
			body: formData
		});
		if (resp.status === 401) {
			api.logout();
			return;
		}
		if (!resp.ok) {
			let detail = '导入失败';
			try { const data = await resp.json(); detail = data.detail || detail; } catch (e) { }
			toast('导入失败: ' + escapeHtml(detail), 'error');
			return;
		}
		const blob = await resp.blob();
		// 后端通过自定义 header 返回成功/失败计数
		const successCount = resp.headers.get('X-Import-Success') || '0';
		const failCount = resp.headers.get('X-Import-Fail') || '0';
		downloadBlob(blob, 'users_import_result.csv');
		toast(`导入完成：成功 ${successCount} 条，失败 ${failCount} 条`, failCount > 0 ? 'warning' : 'success');
		loadUsers();
	} catch (e) {
		toast('导入失败: ' + escapeHtml(e.message), 'error');
	}
}

/** 下载 CSV 导入模板（含表头和示例行） */
async function downloadImportTemplate() {
	try {
		const blob = await api.get('/api/v1/auth/users/import_template/').then(r => r.blob());
		downloadBlob(blob, 'users_import_template.csv');
		toast('模板已下载', 'success');
	} catch (e) { toast('下载模板失败: ' + escapeHtml(e.message), 'error'); }
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

// ====================== 弹窗：新建/编辑用户 ======================
function _getMyRoleInfo() {
	const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
	const codes = getUserRoles();
	return {
		// 超级管理员可在任意部门/团队创建/编辑用户
		isSuper: codes.includes('super_admin'),
		isDept: codes.includes('dept_manager'),
		isTeam: codes.includes('team_leader'),
		deptId: u.department_id || 0,
		teamIds: (u.teams || []).map(t => t.id),
	};
}

function openUserModal(id) {
	$id('editUserId').value = '';
	$id('userUsername').value = '';
	$id('userRealName').value = '';
	$id('userEmail').value = '';
	$id('userDept').value = '';
	$id('userDeptTeam').innerHTML = '<option value="">请先选择部门</option>';
	$id('userRoleSelect').value = '';
	$id('userStatus').value = 'active';
	$id('userModalTitle').textContent = '新建用户';
	$id('rolePermSummary').textContent = '';
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

	populateRoleSelect(null, true);
	populateTeamSelect(0, null);

	// 非超管创建用户时，锁定部门/团队
	if (!id) {
		const me = _getMyRoleInfo();
		if (!me.isSuper) {
			const deptSel = $id('userDept');
			const teamSel = $id('userDeptTeam');
			if (me.isTeam) {
				// 组长：锁定部门，团队可选（仅限自己的团队）
				deptSel.value = me.deptId;
				deptSel.disabled = true;
				if (me.teamIds.length === 1) {
					populateTeamSelect(me.deptId, me.teamIds[0]);
					teamSel.disabled = true;
				} else {
					populateTeamSelect(me.deptId, null);
				}
			} else if (me.isDept) {
				// 部门经理：锁定部门，团队可选
				deptSel.value = me.deptId;
				deptSel.disabled = true;
				populateTeamSelect(me.deptId, null);
			}
		}
	}

	if (id) {
		$id('userModalTitle').textContent = '编辑用户';
		$id('modalDeleteBtn').classList.remove('hidden');
		const me = _getMyRoleInfo();
		const loadUserData = () => {
			// 重新用 assignable_roles 填充（loadFilterOptions 可能覆盖了）
			populateRoleSelect(null, true);
			return api.getJson(`/api/v1/auth/users/${id}/`).then(u => {
				$id('editUserId').value = u.id;
				$id('userUsername').value = u.username;
				$id('userRealName').value = u.real_name || '';
				$id('userEmail').value = u.email || '';
				const deptId = u.department_id || 0;
				$id('userDept').value = deptId;
				$id('userStatus').value = u.status || 'active';
				const roleId = (u.roles && u.roles.length > 0) ? u.roles[0].role__id : null;
				const selectedTeamId = (u.teams && u.teams.length > 0) ? u.teams[0].id : null;
				$id('userRoleSelect').value = roleId || '';
				// 先填充团队下拉（会默认启用），再调用 onRoleChange 由 updateTeamDisabledState 覆盖禁用状态
				// 顺序不能反过来，否则 populateTeamSelect 的 disabled=false 会覆盖 dept_manager 的禁用
				populateTeamSelect(deptId, selectedTeamId);
				onRoleChange();
				// 防止越权：组长锁定部门和团队，部门经理锁定部门
				const teamSel = $id('userDeptTeam');
				const deptSel = $id('userDept');
				if (!me.isSuper) {
					if (me.isTeam) { deptSel.disabled = true; teamSel.disabled = true; }
					else if (me.isDept) deptSel.disabled = true;
				}
			}).catch(e => console.error('获取用户详情失败:', e));
		};
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
function populateRoleSelect(selectedId, useAssignable = false) {
	const roles = (useAssignable ? filterOptions.assignable_roles : filterOptions.roles) || [];
	$id('userRoleSelect').innerHTML = '<option value="">— 无 —</option>' +
		roles.map(r => `<option value="${r.id}" ${selectedId === r.id ? 'selected' : ''}>${escapeHtml(r.name)}</option>`).join('');
}

function onRoleChange() {
	renderRolePermSummary();
	updateTeamDisabledState();
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
		super_admin: '最高权限（系统级快路径）',
		user_admin: '组织/人员管理，不可操作文档',
		kb_admin: '知识库/文档管理，不可管理人',
		compliance_admin: '审计日志/合规校验（只读）',
		dept_manager: '本部门人员/团队/节点/文档管理',
		team_leader: '本团队人员/节点/文档管理',
		employee: '本团队文档读/上传/下载',
		read_only_employee: '仅可读取文档，无下载/写权限',
	};
	$id('rolePermSummary').innerHTML = `${escapeHtml(r.name)}: ${desc[r.code] || '自定义权限'}`;
}

function updateTeamDisabledState() {
	// 团队下拉的禁用条件：1) 角色为部门经理（不需要团队）；2) 未选择部门
	const roleId = parseInt($id('userRoleSelect').value) || 0;
	const roles = filterOptions.roles || [];
	const r = roles.find(r => r.id === roleId);
	const isDeptManager = r && r.code === 'dept_manager';
	const deptId = parseInt($id('userDept').value) || 0;
	$id('userDeptTeam').disabled = isDeptManager || !deptId;
}

// ====================== 部门变更 ======================
function onDeptChange() {
	const deptId = parseInt($id('userDept').value) || 0;
	populateTeamSelect(deptId, null);
	// 部门变更后需要重新评估团队禁用状态
	updateTeamDisabledState();
}

// ====================== 团队下拉（弹窗内） ======================
function populateTeamSelect(deptId, selectedTeamId) {
	const teams = filterOptions.teams || [];
	const filtered = deptId ? teams.filter(t => t.department_id === deptId) : [];
	const sel = $id('userDeptTeam');
	if (!deptId) {
		// 未选部门时清空并禁用，避免用户误选
		sel.innerHTML = '<option value="">请先选择部门</option>';
		sel.disabled = true;
		return;
	}
	// 选中部门时默认启用团队下拉；dept_manager 角色等场景由 updateTeamDisabledState 二次覆盖
	sel.disabled = false;
	if (filtered.length === 0) {
		sel.innerHTML = '<option value="">该部门暂无团队</option>';
		return;
	}
	sel.innerHTML = '<option value="">— 未选择 —</option>' +
		filtered.map(t => `<option value="${t.id}" ${selectedTeamId === t.id ? 'selected' : ''}>${escapeHtml(t.name)}</option>`).join('');
}

async function saveUser() {
	const id = $id('editUserId').value;
	const roleId = parseInt($id('userRoleSelect').value) || null;
	const teamId = parseInt($id('userDeptTeam').value) || 0;
	const deptId = $id('userDept').value ? parseInt($id('userDept').value) : null;

	const base = {
		real_name: $id('userRealName').value.trim(),
		email: $id('userEmail').value.trim(),
		department_id: deptId,
		status: $id('userStatus').value,
		role_ids: roleId ? [roleId] : [],
		team_ids: teamId ? [teamId] : [],
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
});
