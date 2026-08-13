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

// 角色变更提交成功后统一提示：走工单审批，审批通过后生效
function _roleTicketToast(resp, actionLabel) {
	const risk = resp.risk_level === 'high' ? '（高风险，需双审）' : '';
	toast(`${actionLabel}已提交工单 ${resp.ticket_no}${risk}，审批通过后生效`, 'success');
}

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
// 权限分配走工单审批：提交后仅提示工单号，权限不立即生效，
// 本地角色数据与勾选状态保持不变（审批通过后由审批人刷新页面查看）
async function saveRolePermissions() {
	if (!_selectedRoleId || _savingPerms) return;
	const btn = document.getElementById('btnSavePerms');
	_savingPerms = true;
	btn.disabled = true;
	btn.textContent = '提交中...';
	const checkboxes = document.querySelectorAll('#permPanel input[type="checkbox"]');
	const permIds = Array.from(checkboxes).filter(cb => cb.checked).map(cb => parseInt(cb.value));
	try {
		const resp = await api.postJson(`${API_BASE}/roles/${_selectedRoleId}/assign-permissions/`, { permission_ids: permIds });
		_roleTicketToast(resp, '权限分配');
	} catch (e) { toast('提交失败: ' + e.message, 'error'); }
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
	btn.textContent = '提交中...';
	try {
		const body = { name, description: desc };
		let resp;
		if (id) {
			// 编辑时：内置角色不提交 code（后端也会拦截）
			const r = allRoles.find(x => x.id === parseInt(id));
			if (!r || !r.is_builtin) {
				body.code = code;
			}
			resp = await api.patchJson(`${API_BASE}/roles/${id}/`, body);
		} else {
			body.code = code;
			resp = await api.postJson(`${API_BASE}/roles/`, body);
		}
		// 角色增改走工单审批：审批通过后生效，提交成功仅提示工单号
		_roleTicketToast(resp, id ? '角色编辑' : '角色新增');
		closeModal('roleModal');
		// 重新拉取角色列表（审批通过前列表不变，保持与服务端一致）
		const data = await api.getJson(`${API_BASE}/roles/`);
		allRoles = Array.isArray(data) ? data : (data.results || []);
		renderRoles();
	} catch (e) { toast('提交失败: ' + e.message, 'error'); }
	finally {
		_savingRole = false;
		btn.disabled = false;
		btn.textContent = '保存';
	}
}

// 删除角色走工单审批（高风险双审）：确认后提交删除工单，审批通过后软删
function deleteRole(id, name) {
	showConfirmDialog({
		title: '删除角色',
		bannerText: `删除角色"${escapeHtml(name)}"为高风险操作，需双审，审批通过后生效`,
		bannerType: 'danger',
		bannerIcon: '⚠',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '提交删除工单',
				type: 'danger',
				onClick: async (ctx) => {
					try {
						const resp = await api.deleteJson(`${API_BASE}/roles/${id}/`);
						ctx.close();
						_roleTicketToast(resp, '角色删除');
						const data = await api.getJson(`${API_BASE}/roles/`);
						allRoles = Array.isArray(data) ? data : (data.results || []);
						renderRoles();
					} catch (e) { ctx.setError('提交失败: ' + e.message); }
				}
			}
		]
	});
}
