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
	// 表格按钮事件委托：用 data-user-id 取 ID，避免每行内联 onclick 拼接
	const userTable = $id('userTable');
	if (userTable) {
		userTable.addEventListener('click', e => {
			const wrap = e.target.closest('[data-user-id]');
			if (!wrap) return;
			const id = parseInt(wrap.dataset.userId, 10);
			if (!id) return;
			if (e.target.closest('.user-action-perm')) openPermModal(id);
			else if (e.target.closest('.user-action-edit')) openUserModal(id);
			else if (e.target.closest('.user-action-toggle')) toggleUserStatus(id);
		});
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
					const myTeamIds = u.team ? [u.team.id] : [];
					data.teams = (data.teams || []).filter(t => myTeamIds.includes(t.id));
				}
			}
		}

		filterOptions = data;
		const fDept = $id('filterDept');
		fDept.innerHTML = '<option value="">全部部门</option>' +
			(filterOptions.departments || []).map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join('');
		// 角色筛选已移除（页面不再直接展示/按角色筛选）
		const uDept = $id('userDept');
		uDept.innerHTML = '<option value="">— 无 —</option>' +
			(filterOptions.departments || []).map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join('');
		// 初始化时未选择部门，团队筛选应禁用
		$id('filterTeam').disabled = true;
		populateTeamSelect(0, null);
	} catch (e) { console.error('加载筛选项失败:', e); }
}

function searchUsers() {
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
		renderUserPagination();
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
		const deptName = escapeHtml(u.department_name) || '—';
		const teamName = u.team ? escapeHtml(u.team.name) : '—';
		const lastLogin = u.last_login_at ? formatDate(u.last_login_at) : '—';
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
			last_login: escapeHtml(lastLogin),
			status_tag: statusTag,
			toggle_label: toggleLabel,
		});
	}).join('');
}

/* ============================================================================
 * 分页：复用公共 Pagination 组件（common.js）。
 * 首次 render 绑定回调，后续 update 仅刷新页码状态；切换每页条数后重置回第 1 页
 * ============================================================================ */
let _paginationInited = false; // 分页组件是否已初始化

function renderUserPagination() {
	const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
	if (!_paginationInited) {
		Pagination.render({
			container: '#userPagination',
			page: currentPage,
			totalPages: totalPages,
			total: totalCount,
			pageSize: pageSize,
			align: 'right',
			// pageSizeOptions: [20, 50, 100],
			onPageChange(p) { currentPage = p; loadUsers(); },
			onPageSizeChange(size) { pageSize = size; currentPage = 1; loadUsers(); },
		});
		_paginationInited = true;
	} else {
		Pagination.update({
			page: currentPage,
			totalPages: totalPages,
			total: totalCount,
			pageSize: pageSize,
		});
	}
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

// ===== 通用确认弹窗（基于静态 DOM + showModal/closeModal 范式） =====
let confirmCallback = null;

/**
 * 打开静态确认弹窗
 * @param {string} message - 提示消息
 * @param {object} [opts] - 配置
 * @param {string} [opts.title='确认操作'] - 标题
 * @param {string} [opts.confirmText='确认'] - 确认按钮文字
 * @param {string} [opts.cancelText='取消'] - 取消按钮文字
 * @param {boolean} [opts.danger=true] - 确认按钮是否危险样式
 * @param {Function} [opts.onConfirm] - 点击确认时的回调（异步函数也支持）
 */
function openConfirm(message, opts = {}) {
	const {
		title = '确认操作',
		confirmText = '确认',
		cancelText = '取消',
		danger = true,
		onConfirm,
	} = opts;
	$id('confirmTitle').textContent = title;
	$id('confirmMessage').innerHTML = message;
	const okBtn = $id('confirmOkBtn');
	okBtn.textContent = confirmText;
	okBtn.classList.toggle('btn-delete', danger);
	okBtn.classList.toggle('btn-save', !danger);
	$id('confirmCancelBtn').textContent = cancelText;
	confirmCallback = onConfirm || null;
	showModal('confirmModal');
}

/**
 * 关闭确认弹窗，用户点击取消/确认/关闭按钮时触发
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

async function deleteUserFromModal() {
	const id = $id('editUserId').value;
	const username = $id('userUsername').value;
	if (!id) return;
	openConfirm(`确认删除用户 "${username}"？此操作为软删除。`, {
		title: '删除用户',
		confirmText: '删除',
		onConfirm: async () => {
			await api.deleteJson(`/api/v1/auth/users/${id}/`);
			closeModal('userModal');
			loadUsers();
			toast('已删除', 'success');
		},
	});
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
	const isSuper = codes.includes('super_admin');
	const isUserAdmin = codes.includes('user_admin');
	return {
		isSuper,
		isUserAdmin,
		// 超管/用户管理员可管理任意部门/团队
		canManageAll: isSuper || isUserAdmin,
		isDept: codes.includes('dept_manager'),
		isTeam: codes.includes('team_leader'),
		deptId: u.department_id || 0,
		teamIds: u.team ? [u.team.id] : [],
	};
}

function openUserModal(id) {
	$id('editUserId').value = '';
	$id('userUsername').value = '';
	$id('userRealName').value = '';
	$id('userEmail').value = '';
	$id('userDept').value = '';
	$id('userDeptTeam').innerHTML = '<option value="">请先选择部门</option>';
	$id('userStatus').value = 'active';
	$id('userModalTitle').textContent = '新建用户';
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

	populateTeamSelect(0, null);

	if (id) {
		$id('userModalTitle').textContent = '编辑用户';
		$id('modalDeleteBtn').classList.remove('hidden');
		const me = _getMyRoleInfo();
		const loadUserData = () => {
			return api.getJson(`/api/v1/auth/users/${id}/`).then(u => {
				$id('editUserId').value = u.id;
				$id('userUsername').value = u.username;
				$id('userRealName').value = u.real_name || '';
				$id('userEmail').value = u.email || '';
				const deptId = u.department_id || 0;
				// "— 无 —" 的 option value 为空字符串，无部门时需显式置空才能选中
				$id('userDept').value = deptId || '';
				$id('userStatus').value = u.status || 'active';
				const selectedTeamId = u.team ? u.team.id : null;
				// populateTeamSelect 会根据 deptId 启用/禁用团队下拉
				populateTeamSelect(deptId, selectedTeamId);
				// 越权锁定：组长锁部门+团队，部门经理锁部门；超管/用户管理员可自由修改
				const deptSel = $id('userDept');
				const teamSel = $id('userDeptTeam');
				deptSel.disabled = false;
				if (!me.canManageAll) {
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
		// 新建用户：按当前用户权限锁定部门/团队，角色默认 viewer（兜底只读）
		const applyNewUserDefaults = () => {
			const me = _getMyRoleInfo();
			const deptSel = $id('userDept');
			const teamSel = $id('userDeptTeam');
			deptSel.disabled = false;
			if (me.canManageAll) {
				// 超管/用户管理员：部门默认"— 无 —"，可自由选择
				deptSel.value = '';
				populateTeamSelect(0, null);
			} else if (me.isTeam) {
				// 组长：锁定部门/团队
				deptSel.value = me.deptId;
				deptSel.disabled = true;
				if (me.teamIds.length === 1) {
					populateTeamSelect(me.deptId, me.teamIds[0]);
					teamSel.disabled = true;
				} else {
					populateTeamSelect(me.deptId, null);
				}
			} else if (me.isDept) {
				// 部门经理：锁定部门，团队默认"— 无 —"
				deptSel.value = me.deptId;
				deptSel.disabled = true;
				populateTeamSelect(me.deptId, null);
			}
		};
		if (!filterOptions.roles) {
			loadFilterOptions().then(applyNewUserDefaults);
		} else {
			applyNewUserDefaults();
		}
	}
	showModal('userModal');
}

// ====================== 部门变更 ======================
function onDeptChange() {
	const deptId = parseInt($id('userDept').value) || 0;
	populateTeamSelect(deptId, null);
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
	// 选中部门时启用团队下拉；组长单团队等锁定场景由 openUserModal 覆盖
	sel.disabled = false;
	if (filtered.length === 0) {
		sel.innerHTML = '<option value="">该部门暂无团队</option>';
		return;
	}
	sel.innerHTML = '<option value="">— 无 —</option>' +
		filtered.map(t => `<option value="${t.id}" ${selectedTeamId === t.id ? 'selected' : ''}>${escapeHtml(t.name)}</option>`).join('');
}

async function saveUser() {
	const id = $id('editUserId').value;
	const teamId = parseInt($id('userDeptTeam').value) || 0;
	const deptId = $id('userDept').value ? parseInt($id('userDept').value) : null;

	const base = {
		real_name: $id('userRealName').value.trim(),
		email: $id('userEmail').value.trim(),
		department_id: deptId,
		status: $id('userStatus').value,
		team_ids: teamId ? [teamId] : [],
	};
	// 新建时需验证用户名；角色默认 viewer（人事归属兜底只读，写权限需后续申请 contributor）
	if (!id) {
		base.username = $id('userUsername').value.trim();
		if (!base.username) { toast('用户名不能为空', 'warning'); return; }
		const viewerRole = (filterOptions.roles || []).find(r => r.code === 'viewer');
		if (viewerRole) base.role_ids = [viewerRole.id];
	}
	if (!base.real_name) { toast('姓名为必填', 'warning'); return; }
	try {
		if (id) {
			await api.patchJson(`/api/v1/auth/users/${id}/`, base);
			toast('用户已更新', 'success');
			closeModal('userModal');
			loadUsers();
		} else {
			await api.postJson('/api/v1/auth/users/', base);
			toast('用户已创建', 'success');
			closeModal('userModal');
			loadUsers();
		}
	} catch (e) {
		// 409 + USER_REVIVABLE：邮箱命中已删除用户，弹窗询问是否恢复
		// 恢复 → 调用 revive 接口传当前表单数据（覆盖姓名/部门/团队）
		if (e.status === 409 && e.data && e.data.code === 'USER_REVIVABLE' && e.data.revivable_user) {
			const rv = e.data.revivable_user;
			const deletedAt = rv.deleted_at ? new Date(rv.deleted_at).toLocaleString('zh-CN') : '';
			openConfirm(
				`该邮箱曾属于已删除用户 <b>${escapeHtml(rv.real_name || rv.username)}</b>（删除于 ${deletedAt}）。是否恢复原账号？<br><br>恢复后原账号的姓名/部门/团队将被当前表单内容覆盖，权限重置为查看者（需重新申请）。`,
				{
					title: '检测到已删除用户',
					confirmText: '恢复原账号',
					cancelText: '取消',
					danger: false,
					onConfirm: async () => {
						await api.postJson(`/api/v1/auth/users/${rv.id}/revive/`, {
							real_name: base.real_name,
							department_id: base.department_id,
							team_ids: base.team_ids,
							status: base.status,
						});
						toast('用户已恢复', 'success');
						closeModal('userModal');
						loadUsers();
					},
				}
			);
			return;
		}
		toast('保存失败: ' + escapeHtml(e.message), 'error');
	}
}

// ====================== 权限详情弹窗 ======================

async function openPermModal(userId) {
	$id('permModalTitle').textContent = '用户权限详情';
	$id('permModalBody').innerHTML = '<div class="text-sub text-center" style="padding:30px">加载中...</div>';
	showModal('permModal');
	try {
		const data = await api.getJson(`/api/v1/auth/users/${userId}/permission-detail/`);
		const u = data.user || {};
		const rows = data.rows || [];
		$id('permModalTitle').textContent = `权限详情 · ${escapeHtml(u.real_name || u.username || '')}`;

		if (rows.length === 0) {
			$id('permModalBody').innerHTML = '<div class="text-sub text-center" style="padding:30px">该用户暂无任何权限授权</div>';
			return;
		}

		// 简洁表格：部门-团队-权限-截至日期
		const tagStyle = {
			viewer: 'background:#e3f2fd;color:#1565c0',
			contributor: 'background:#e8f5e9;color:#2e7d32',
		};
		const rowsHtml = rows.map(r => {
			const eff = r.effective_from || '—';
			const exp = r.expires_at || '永久';
			const style = tagStyle[r.role_code] || 'background:#fff3e0;color:#ef6c00';
			return `<tr>
				<td style="padding:8px 12px">${escapeHtml(r.dept_name)}</td>
				<td style="padding:8px 12px">${escapeHtml(r.team_name)}</td>
				<td style="padding:8px 12px"><span class="tag tag-sm" style="${style}">${escapeHtml(r.role_name)}</span></td>
				<td style="padding:8px 12px" class="text-sub">${escapeHtml(eff)}</td>
				<td style="padding:8px 12px" class="text-sub">${escapeHtml(exp)}</td>
			</tr>`;
		}).join('');

		$id('permModalBody').innerHTML = `
			<table class="table" style="width:100%">
				<thead>
					<tr>
						<th style="padding:8px 12px">部门</th>
						<th style="padding:8px 12px">团队</th>
						<th style="padding:8px 12px">权限</th>
						<th style="padding:8px 12px">生效时间</th>
						<th style="padding:8px 12px">截至日期</th>
					</tr>
				</thead>
				<tbody>${rowsHtml}</tbody>
			</table>
		`;
	} catch (e) {
		$id('permModalBody').innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败：${escapeHtml(e.message || String(e))}</div>`;
	}
}

// 页面加载时自动初始化
document.addEventListener('DOMContentLoaded', () => {
	initUsersPage();
});
