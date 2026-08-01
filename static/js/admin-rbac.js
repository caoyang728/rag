/* ============ RBAC 权限配置页（角色管理 + 权限分配） ============ */
const API_BASE = '/api/v1/auth';
let allRoles = [];
let allPermissions = [];
let _selectedRoleId = null;
let _savingRole = false;
let _savingPerms = false;

// 权限模块中文名映射
const MODULE_LABELS = {
	knowledge: '知识库',
	user: '用户管理',
	system: '系统配置',
	audit: '审计',
};

document.addEventListener('DOMContentLoaded', async () => {
	// RBAC 权限配置页：超级管理员可访问（持有 '*' 全权限）
	if (!hasAnyRole('super_admin')) {
		document.body.innerHTML = document.getElementById('tmpl-no-permission').innerHTML;
		return;
	}
	// 加载状态
	document.getElementById('roleList').innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-text">加载中...</div></div>';
	document.getElementById('permPanel').innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-text">加载中...</div></div>';
	// 一次性全量加载角色（含 permission_ids）和权限列表
	try {
		const [rolesData, permsData] = await Promise.all([
			api.getJson(`${API_BASE}/roles/`),
			api.getJson(`${API_BASE}/permissions/`),
		]);
		allRoles = Array.isArray(rolesData) ? rolesData : (rolesData.results || []);
		allPermissions = Array.isArray(permsData) ? permsData : (permsData.results || []);
		renderRoles();
		document.getElementById('permPanel').innerHTML = '<div class="empty"><div class="empty-icon">👈</div><div class="empty-text">请从左侧选择一个角色</div></div>';
	} catch (e) {
		document.getElementById('roleList').innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">加载失败</div></div>';
		console.error(e);
	}
});

// ====================== 角色列表 ======================
function renderRoles() {
	const c = document.getElementById('roleList');
	const scrollTop = c.scrollTop;
	if (allRoles.length === 0) {
		c.innerHTML = '<div class="empty"><div class="empty-icon">🎭</div><div class="empty-text">暂无角色</div></div>';
		return;
	}
	const tmpl = document.getElementById('tmpl-role-card').innerHTML;
	c.innerHTML = allRoles.map(r => {
		const badge = r.is_builtin ? '<span class="tag tag-sm" style="background:var(--primary-light);color:var(--primary)">内置</span>' : '';
		return tmpl
			.replace(/__ID__/g, r.id)
			.replace(/__NAME__/g, escapeHtml(r.name))
			.replace(/__CODE__/g, escapeHtml(r.code))
			.replace(/__DESC__/g, escapeHtml(r.description || '—'))
			.replace(/__BUILTIN__/g, badge)
			.replace(/__NAME_ESC__/g, escapeQuote(r.name));
	}).join('');
	if (_selectedRoleId) {
		const card = document.getElementById(`roleCard-${_selectedRoleId}`);
		if (card) card.classList.add('role-card-active');
	}
	c.scrollTop = scrollTop;
}

// ====================== 选择角色 → 渲染权限 ======================
function selectRole(roleId) {
	_selectedRoleId = roleId;
	document.querySelectorAll('.role-card').forEach(c => c.classList.remove('role-card-active'));
	const card = document.getElementById(`roleCard-${roleId}`);
	if (card) card.classList.add('role-card-active');

	const role = allRoles.find(r => r.id === roleId);
	document.getElementById('currentRoleName').textContent = role ? `- ${role.name}` : '';
	renderPermissionPanel(role ? (role.permission_ids || []) : []);
	const btn = document.getElementById('btnSavePerms');
	btn.disabled = false;
	btn.textContent = '保存权限';
}

function renderPermissionPanel(checkedIds) {
	if (allPermissions.length === 0) {
		document.getElementById('permPanel').innerHTML = '<div class="empty"><div class="empty-text">暂无权限项</div></div>';
		return;
	}
	// 按模块分组
	const groups = {};
	allPermissions.forEach(p => {
		const mod = p.module || 'other';
		if (!groups[mod]) groups[mod] = [];
		groups[mod].push(p);
	});

	const groupTmpl = document.getElementById('tmpl-perm-group').innerHTML;
	const itemTmpl = document.getElementById('tmpl-perm-item').innerHTML;

	const html = Object.entries(groups).map(([mod, perms]) => {
		const label = MODULE_LABELS[mod] || mod;
		const permsHtml = perms.map(p => {
			const checked = checkedIds.includes(p.id) ? 'checked' : '';
			return itemTmpl
				.replace(/__perm_id__/g, p.id)
				.replace(/__perm_name__/g, escapeHtml(p.name))
				.replace(/__perm_code__/g, escapeHtml(p.code))
				.replace(/__checked__/g, checked);
		}).join('');
		return groupTmpl
			.replace(/__module_label__/g, label)
			.replace(/__perms_html__/g, permsHtml);
	}).join('');

	document.getElementById('permPanel').innerHTML = html;
}

// ====================== 保存权限 ======================
async function saveRolePermissions() {
	if (!_selectedRoleId || _savingPerms) return;
	const btn = document.getElementById('btnSavePerms');
	_savingPerms = true;
	btn.disabled = true;
	btn.textContent = '保存中...';
	const checkboxes = document.querySelectorAll('#permPanel input[type="checkbox"]');
	const permIds = Array.from(checkboxes).filter(cb => cb.checked).map(cb => parseInt(cb.value));
	try {
		await api.postJson(`${API_BASE}/roles/${_selectedRoleId}/assign-permissions/`, { permission_ids: permIds });
		const role = allRoles.find(r => r.id === _selectedRoleId);
		if (role) role.permission_ids = permIds;
		toast('权限已更新', 'success');
	} catch (e) { toast('保存失败: ' + e.message, 'error'); }
	finally {
		_savingPerms = false;
		btn.disabled = false;
		btn.textContent = '保存权限';
	}
}

// ====================== 角色增删改 ======================
function openRoleModal(id) {
	document.getElementById('roleId').value = '';
	document.getElementById('roleName').value = '';
	document.getElementById('roleCode').value = '';
	document.getElementById('roleDesc').value = '';
	document.getElementById('roleCode').readOnly = false;
	document.getElementById('roleModalTitle').textContent = '新增角色';
	const saveBtn = document.getElementById('btnSaveRole');
	saveBtn.disabled = false;
	saveBtn.textContent = '保存';

	if (id) {
		const r = allRoles.find(x => x.id === id);
		if (r) {
			document.getElementById('roleId').value = r.id;
			document.getElementById('roleName').value = r.name;
			document.getElementById('roleCode').value = r.code;
			document.getElementById('roleDesc').value = r.description || '';
			document.getElementById('roleModalTitle').textContent = '编辑角色';
			// 内置角色 code 不可修改，防止权限判定失效
			if (r.is_builtin) {
				document.getElementById('roleCode').readOnly = true;
			}
		}
	}
	showModal('roleModal');
}

async function saveRole() {
	if (_savingRole) return;
	const id = document.getElementById('roleId').value;
	const name = document.getElementById('roleName').value.trim();
	const code = document.getElementById('roleCode').value.trim();
	const desc = document.getElementById('roleDesc').value.trim();
	if (!name) { toast('请输入角色名称', 'warning'); return; }
	if (!code) { toast('请输入角色编码', 'warning'); return; }
	if (!/^[a-z][a-z0-9_]*$/.test(code)) { toast('角色编码只能包含小写字母、数字和下划线，且以字母开头', 'warning'); return; }
	const btn = document.getElementById('btnSaveRole');
	_savingRole = true;
	btn.disabled = true;
	btn.textContent = '保存中...';
	try {
		const body = { name, description: desc };
		if (id) {
			// 编辑时：内置角色不提交 code（后端也会拦截）
			const r = allRoles.find(x => x.id === parseInt(id));
			if (!r || !r.is_builtin) {
				body.code = code;
			}
			await api.patchJson(`${API_BASE}/roles/${id}/`, body);
			toast('角色已更新', 'success');
		} else {
			body.code = code;
			await api.postJson(`${API_BASE}/roles/`, body);
			toast('角色已添加', 'success');
		}
		closeModal('roleModal');
		const data = await api.getJson(`${API_BASE}/roles/`);
		allRoles = Array.isArray(data) ? data : (data.results || []);
		renderRoles();
	} catch (e) { toast('保存失败: ' + e.message, 'error'); }
	finally {
		_savingRole = false;
		btn.disabled = false;
		btn.textContent = '保存';
	}
}

async function deleteRole(id, name) {
	if (!confirm(`确认删除角色"${name}"？内置角色不可删除。`)) return;
	try {
		await api.deleteJson(`${API_BASE}/roles/${id}/`);
		toast('角色已删除', 'success');
		if (_selectedRoleId === id) {
			_selectedRoleId = null;
			document.getElementById('currentRoleName').textContent = '';
			document.getElementById('permPanel').innerHTML = '<div class="empty"><div class="empty-icon">👈</div><div class="empty-text">请从左侧选择一个角色</div></div>';
			document.getElementById('btnSavePerms').disabled = true;
		}
		const data = await api.getJson(`${API_BASE}/roles/`);
		allRoles = Array.isArray(data) ? data : (data.results || []);
		renderRoles();
	} catch (e) { toast('删除失败: ' + e.message, 'error'); }
}
