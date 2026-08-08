/* ============ 组织架构管理页 ============ */
const API_BASE = '/api/v1/auth';
let allDepts = [];
let _saving = false;
let _nominateSearchTimer = null;
let _nominateSearchSeq = 0;
let _currentManageDeptId = null;
// 当前用户管辖范围(从 profile 实时拉取,避免 localStorage.rag_user 过期):
// _managedTeamIds: 可授权成员的团队(组长/部门经理属地授权/本团队)
// _managedDeptIds: 可授权成员的部门(部门经理属地授权)
let _managedTeamIds = new Set();
let _managedDeptIds = new Set();

function isKbAdmin() {
	return hasAnyRole('kb_admin');
}

// 组织架构管理功能(部门/团队 CRUD + 任命管理岗):仅超级管理员 / 文档管理员
function isOrgManager() {
	return isSuperAdmin() || isKbAdmin();
}

// 页面可访问:管理端 + 团队组长 + 部门经理(协作角色授权入口)
function canAccessOrgPage() {
	return isSuperAdmin() || isKbAdmin() || hasAnyRole('team_leader', 'dept_manager');
}

document.addEventListener('DOMContentLoaded', async () => {
	// 可访问组织架构页:超级管理员 / 文档管理员 / 团队组长 / 部门经理
	if (!canAccessOrgPage()) {
		document.body.innerHTML = document.getElementById('tmpl-no-permission').innerHTML;
		return;
	}
	// 组长/部门经理的管辖范围实时刷新(本地登录态可能过期),失败时按无管辖范围降级
	try {
		const p = await api.getJson('/api/v1/auth/profile/');
		_managedTeamIds = new Set(p.managed_team_ids || []);
		_managedDeptIds = new Set(p.managed_dept_ids || []);
	} catch (e) {
		console.error('加载管辖范围失败:', e);
	}
	// 新增部门/新增团队仅管理端可见(组长/部门经理只做成员授权)
	if (!isOrgManager()) {
		const addBtn = document.getElementById('btnAddDept');
		if (addBtn) addBtn.style.display = 'none';
		const addTeamBtn = document.getElementById('btnAddTeam');
		if (addTeamBtn) addTeamBtn.style.display = 'none';
	}
	loadDepts();
	document.addEventListener('click', function (e) {
		// 点击任命弹窗外部时收起用户搜索结果下拉
		const nominateResults = document.getElementById('nominateUserResults');
		const nominateSearch = document.getElementById('nominateUserSearch');
		if (nominateResults && nominateSearch && !nominateSearch.contains(e.target) && !nominateResults.contains(e.target)) {
			nominateResults.classList.remove('show');
		}
	});
});

// 当前用户可见的部门列表:管理端全量;组长/部门经理仅管辖部门
// (部门经理属地授权部门 + 包含管辖团队的部门,保证组长能找到本团队所在部门)
function _visibleDepts() {
	if (isOrgManager()) return allDepts;
	return allDepts.filter(d => {
		if (_managedDeptIds.has(d.id)) return true;
		return (d.teams || []).some(t => _managedTeamIds.has(t.id));
	});
}

// 部门卡片操作按钮组:管理功能(编辑/管理团队/任命/删除)仅管理端;
// 授权成员按钮按"能否提单"控制:超管全量兜底 + 部门经理管辖部门
// (kb_admin 仅参与审核不直接提单,除非其兼任组长/部门经理)
function _deptActions(d) {
	const parts = [];
	const canManage = isOrgManager();
	const canGrant = isSuperAdmin() || _managedDeptIds.has(d.id);
	if (canManage) {
		parts.push(`<button class="btn btn-sm btn-outline" onclick="openDeptModal(${d.id})">编辑</button>`);
	}
	if (canManage || _managedDeptIds.has(d.id) || (d.teams || []).some(t => _managedTeamIds.has(t.id))) {
		parts.push(`<button class="btn btn-sm btn-outline" onclick="openTeamManageModal(${d.id})">管理团队</button>`);
	}
	if (canGrant) {
		parts.push(`<button class="btn btn-sm btn-outline" onclick="openGrantModal('dept',${d.id},'${escapeQuote(d.name)}')">授权成员</button>`);
	}
	if (canManage) {
		parts.push(`<button class="btn btn-sm btn-primary" onclick="openNominateModal('dept',${d.id},'dept_manager','${escapeQuote(d.name)}')">任命经理</button>`);
		parts.push(`<button class="btn btn-sm btn-danger" onclick="deleteDept(${d.id},'${escapeQuote(d.name)}')">删除</button>`);
	}
	return parts.join('');
}

// 团队卡片操作按钮组:管理功能(编辑/任命/删除)仅管理端;
// 授权成员按能否提单控制:超管全量兜底 + 管辖团队(get_user_managed_teams 已含
// 组长本团队/团队属地授权/部门经理属地授权部门下的全部团队)
function _teamActions(t) {
	const parts = [];
	const canManage = isOrgManager();
	const canGrant = isSuperAdmin() || _managedTeamIds.has(t.id);
	if (canManage) {
		parts.push(`<button class="btn btn-sm btn-outline" onclick="editTeam(${t.id})">编辑</button>`);
	}
	if (canGrant) {
		parts.push(`<button class="btn btn-sm btn-outline" onclick="openGrantModal('team',${t.id},'${escapeQuote(t.name)}')">授权成员</button>`);
	}
	if (canManage) {
		parts.push(`<button class="btn btn-sm btn-primary" onclick="openNominateModal('team',${t.id},'team_leader','${escapeQuote(t.name)}')">任命组长</button>`);
		parts.push(`<button class="btn btn-sm btn-danger" onclick="deleteTeam(${t.id},'${escapeQuote(t.name)}')">删除</button>`);
	}
	return parts.join('');
}

async function loadDepts() {
	const c = document.getElementById('deptList');
	c.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-text">加载中...</div></div>';
	try {
		const data = await api.getJson(`${API_BASE}/departments/`);
		allDepts = Array.isArray(data) ? data : (data.results || []);
		renderDepts();
	} catch (e) {
		c.innerHTML = '<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-text">加载失败：' + escapeHtml(e.message) + '</div></div>';
		console.error(e);
	}
}

function renderDepts() {
	const c = document.getElementById('deptList');
	const visible = _visibleDepts();
	if (visible.length === 0) {
		c.innerHTML = '<div class="empty"><div class="empty-icon">🏢</div><div class="empty-text">暂无部门</div></div>';
		return;
	}
	const tmpl = document.getElementById('tmpl-dept-card').innerHTML;
	c.innerHTML = visible.map(d => {
		const leaderInfo = d.leader_name ? `<span class="text-sub text-sm">· 经理: ${escapeHtml(d.leader_name)}</span>` : '';
		const teamsHtml = (d.teams || []).length > 0
			? `<div class="dept-card-teams">${d.teams.map(t => {
				const tLeader = t.leader_name ? ` (${escapeHtml(t.leader_name)})` : '';
				return `<span class="tag tag-sm" style="background:var(--primary-light)">${escapeHtml(t.name)}${tLeader}</span>`;
			}).join('')}</div>`
			: '';
		return tmpl
			.replace(/__NAME__/g, escapeHtml(d.name))
			.replace(/__CODE__/g, escapeHtml(d.code || '—'))
			.replace(/__USER_COUNT__/g, d.user_count || 0)
			.replace(/__LEADER_INFO__/g, leaderInfo)
			.replace(/__ID__/g, d.id)
			.replace(/__NAME_ESC__/g, escapeQuote(d.name))
			.replace(/__TEAMS_HTML__/g, teamsHtml)
			.replace(/__ACTIONS__/g, _deptActions(d));
	}).join('');
}

// 管理岗名额唯一预检:任命前检查目标团队/部门是否已有现任组长/经理
// (后端 create_ticket 校验 5 为权威兜底,此处提前提示避免无效提单;
//  换人需先撤销现任,现任本人续期不拦截 —— 与后端一致)
function _findExistingLeader(targetType, targetId) {
	const id = String(targetId);
	if (targetType === 'dept') {
		const d = allDepts.find(x => String(x.id) === id);
		if (d && d.leader_id) return d.leader_name || '现任经理';
		return '';
	}
	for (const d of allDepts) {
		const t = (d.teams || []).find(x => String(x.id) === id);
		if (t && t.leader_id) return t.leader_name || '现任组长';
	}
	return '';
}

// ====================== 任命管理岗 ======================
// applicant=当前操作者,target_user=被任命者,审批通过后由工单执行同步 leader_id + 角色授权
function openNominateModal(targetType, targetId, roleKey, targetName) {
	// 名额唯一预检:已有现任组长/经理时提示先撤销(与后端校验 5 对齐)
	if (roleKey === 'team_leader' || roleKey === 'dept_manager') {
		const existing = _findExistingLeader(targetType, targetId);
		if (existing) {
			toast(`该${targetType === 'team' ? '团队' : '部门'}已有${existing},如需更换请先撤销现任后再任命`, 'warning');
			return;
		}
	}
	document.getElementById('nominateMode').value = 'nominate';
	document.getElementById('nominateTargetType').value = targetType;
	document.getElementById('nominateTargetId').value = targetId;
	document.getElementById('nominateRoleKey').value = roleKey;
	document.getElementById('nominateUserId').value = '';
	document.getElementById('nominateUserSearch').value = '';
	document.getElementById('nominateReason').value = '';
	document.getElementById('nominateUserResults').classList.remove('show');
	// 管理岗模式:隐藏协作角色的角色/范围选择控件
	document.getElementById('grantRoleItem').style.display = 'none';
	document.getElementById('grantScopeItem').style.display = 'none';
	document.getElementById('nominateUserSearch').placeholder = '搜索用户姓名...';
	document.getElementById('nominateSubmitBtn').textContent = '提交任命';
	document.getElementById('nominateRoleDisplay').textContent =
		(roleKey === 'team_leader' ? '团队组长' : '部门经理') + ' · ' + targetName;
	document.getElementById('nominateModalTitle').textContent =
		roleKey === 'team_leader' ? '任命团队组长' : '任命部门经理';
	document.getElementById('nominateChainHint').textContent = '';
	// 加载审批链概要(提示本任命走哪些审批环节)
	loadNominateChainHint(roleKey);
	showModal('nominateModal');
}

// ====================== 协作角色授权 ======================
// 团队卡片入口范围固定为当前团队;部门卡片入口可选部门级或该部门下任一团队。
function openGrantModal(targetType, targetId, targetName) {
	document.getElementById('nominateMode').value = 'grant';
	document.getElementById('nominateTargetType').value = targetType;
	document.getElementById('nominateTargetId').value = targetId;
	document.getElementById('nominateRoleKey').value = '';
	document.getElementById('nominateUserId').value = '';
	document.getElementById('nominateUserSearch').value = '';
	document.getElementById('nominateReason').value = '';
	document.getElementById('nominateUserResults').classList.remove('show');
	// 协作模式:显示角色与范围选择,岗位名称固定显示授权目标
	document.getElementById('grantRoleItem').style.display = '';
	document.getElementById('grantScopeItem').style.display = '';
	document.getElementById('nominateUserSearch').placeholder = '搜索被授权人姓名...';
	document.getElementById('nominateSubmitBtn').textContent = '提交授权';
	document.getElementById('nominateRoleDisplay').textContent = '查看者 / 贡献者 · ' + targetName;
	document.getElementById('nominateModalTitle').textContent = '授权成员';
	document.getElementById('grantRoleSelect').value = 'viewer';
	// 填充授权范围:团队入口仅当前团队;部门入口为部门级 + (仅超管兜底)部门下各团队
	// 部门经理在部门卡片只走部门级授权(覆盖部门下全部团队),团队级由组长在团队卡片提单
	const scopeSel = document.getElementById('grantScopeSelect');
	scopeSel.innerHTML = '';
	if (targetType === 'team') {
		scopeSel.innerHTML = `<option value="TEAM:${targetId}">团队 · ${escapeHtml(targetName)}</option>`;
	} else {
		scopeSel.innerHTML = `<option value="DEPT:${targetId}">部门级 · ${escapeHtml(targetName)}</option>`;
		if (isSuperAdmin()) {
			const dept = allDepts.find(d => String(d.id) === String(targetId));
			((dept && dept.teams) || []).forEach(t => {
				const opt = document.createElement('option');
				opt.value = `TEAM:${t.id}`;
				opt.textContent = `团队 · ${t.name}`;
				scopeSel.appendChild(opt);
			});
		}
	}
	document.getElementById('nominateChainHint').textContent = '';
	loadGrantChainHint();
	showModal('nominateModal');
}

// 协作角色审批链提示:viewer/contributor 的审批流在 assignable-roles?purpose=self 下返回
async function loadGrantChainHint() {
	const roleKey = document.getElementById('grantRoleSelect').value;
	try {
		const data = await api.getJson('/api/v1/auth/permissions/assignable-roles/?purpose=self');
		const role = (data.rows || []).find(r => r.role_key === roleKey);
		if (role && role.approval_desc) {
			document.getElementById('nominateChainHint').textContent = '审批流: ' + role.approval_desc;
		}
	} catch (e) { /* 审批流提示加载失败不阻断授权 */ }
}

async function loadNominateChainHint(roleKey) {
	try {
		const data = await api.getJson('/api/v1/auth/permissions/assignable-roles/?purpose=management');
		const role = (data.rows || []).find(r => r.role_key === roleKey);
		if (role && role.approval_desc) {
			document.getElementById('nominateChainHint').textContent = '审批流: ' + role.approval_desc;
		}
	} catch (e) { /* 审批流提示加载失败不阻断任命 */ }
}

function searchNominateUser() {
	clearTimeout(_nominateSearchTimer);
	_nominateSearchTimer = setTimeout(() => _doSearchNominateUser(), 300);
}

async function _doSearchNominateUser() {
	const seq = ++_nominateSearchSeq;
	const q = (document.getElementById('nominateUserSearch').value || '').trim();
	const resultsDiv = document.getElementById('nominateUserResults');
	if (!q) {
		resultsDiv.classList.remove('show');
		return;
	}
	try {
		const data = await api.getJson(`${API_BASE}/users/search/?q=${encodeURIComponent(q)}`);
		// 竞态检查:若有更新的请求已发出,丢弃本次结果
		if (seq !== _nominateSearchSeq) return;
		const users = data.users || [];
		if (users.length === 0) {
			resultsDiv.innerHTML = '<div style="padding:10px 14px;font-size:13px;color:var(--text-sub)">无匹配用户</div>';
		} else {
			const tmpl = document.getElementById('tmpl-user-search-result').innerHTML;
			resultsDiv.innerHTML = users.map(u => tmpl
				.replace(/__ONCLICK__/g, `selectNominateUser(${u.id},'${escapeQuote(u.real_name || u.username)}')`)
				.replace(/__NAME__/g, escapeHtml(u.real_name || u.username))
				.replace(/__EMAIL__/g, escapeHtml(u.email || ''))
			).join('');
		}
		resultsDiv.classList.add('show');
	} catch (e) {
		console.error('搜索用户失败:', e);
	}
}

function selectNominateUser(id, name) {
	document.getElementById('nominateUserId').value = id;
	document.getElementById('nominateUserSearch').value = name;
	document.getElementById('nominateUserResults').classList.remove('show');
}

async function submitNominate() {
	if (_saving) return;
	const mode = document.getElementById('nominateMode').value;
	const targetType = document.getElementById('nominateTargetType').value;
	const targetId = document.getElementById('nominateTargetId').value;
	const userId = document.getElementById('nominateUserId').value;
	const reason = document.getElementById('nominateReason').value.trim();
	if (!userId) { toast('请选择被授权人', 'warning'); return; }
	if (!reason) { toast('请填写授权理由', 'warning'); return; }
	// 协作角色授权模式:角色与范围由下拉决定;管理岗任命模式沿用隐藏字段
	let roleKey, scopeType, scopeId;
	if (mode === 'grant') {
		roleKey = document.getElementById('grantRoleSelect').value;
		const scopeVal = document.getElementById('grantScopeSelect').value || '';
		const idx = scopeVal.indexOf(':');
		if (idx === -1) { toast('请选择授权范围', 'warning'); return; }
		scopeType = scopeVal.slice(0, idx);
		scopeId = parseInt(scopeVal.slice(idx + 1));
	} else {
		roleKey = document.getElementById('nominateRoleKey').value;
		scopeType = targetType === 'dept' ? 'DEPT' : 'TEAM';
		scopeId = parseInt(targetId);
	}
	_saving = true;
	try {
		const body = {
			role_key: roleKey,
			scope_type: scopeType,
			scope_id: scopeId,
			change_type: 'GRANT',
			target_user_id: parseInt(userId),
			reason: reason,
		};
		const resp = await api.postJson('/api/v1/auth/permissions/applications/', body);
		toast((resp.detail || '申请已提交') + (resp.ticket_no ? `（${resp.ticket_no}）` : ''), 'success');
		closeModal('nominateModal');
		await loadDepts();
	} catch (e) {
		toast('提交失败: ' + escapeHtml(e.message), 'error');
	}
	finally { _saving = false; }
}

// ====================== 部门管理 ======================
function openDeptModal(id) {
	document.getElementById('deptId').value = '';
	document.getElementById('deptName').value = '';
	document.getElementById('deptCode').value = '';
	document.getElementById('deptLeaderDisplay').textContent = '';
	document.getElementById('deptModalTitle').textContent = '新增部门';

	if (id) {
		const d = allDepts.find(x => x.id === id);
		if (d) {
			document.getElementById('deptId').value = d.id;
			document.getElementById('deptName').value = d.name;
			document.getElementById('deptCode').value = d.code || '';
			// 部门经理由任命工单设置,编辑弹窗仅展示当前经理
			document.getElementById('deptLeaderDisplay').textContent =
				d.leader_name ? `当前经理: ${d.leader_name}` : '当前无经理,可通过"任命经理"发起工单';
			document.getElementById('deptModalTitle').textContent = '编辑部门';
		}
	}
	showModal('deptModal');
}

async function saveDept() {
	if (_saving) return;
	const id = document.getElementById('deptId').value;
	const name = document.getElementById('deptName').value.trim();
	const code = document.getElementById('deptCode').value.trim();
	if (!name) { toast('请输入部门名称', 'warning'); return; }
	const body = { name, code: code || undefined };
	_saving = true;
	try {
		if (id) {
			await api.patchJson(`${API_BASE}/departments/${id}/`, body);
			toast('部门已更新', 'success');
		} else {
			await api.postJson(`${API_BASE}/departments/`, body);
			toast('部门已添加', 'success');
		}
		closeModal('deptModal');
		await loadDepts();
	} catch (e) { toast('保存失败: ' + escapeHtml(e.message), 'error'); }
	finally { _saving = false; }
}

async function deleteDept(id, name) {
	if (!confirm(`确认删除部门"${name}"？仅当该部门下无用户且无团队时才能删除。`)) return;
	try {
		await api.deleteJson(`${API_BASE}/departments/${id}/`);
		toast('部门已删除', 'success');
		await loadDepts();
	} catch (e) { toast('删除失败: ' + escapeHtml(e.message), 'error'); }
}

// ====================== 团队管理 ======================
function openTeamManageModal(deptId) {
	_currentManageDeptId = deptId;
	const dept = allDepts.find(d => d.id === deptId);
	document.getElementById('teamManageModalTitle').textContent = `管理团队 - ${dept ? dept.name : ''}`;
	cancelTeamEdit();
	renderTeamManageList(dept);
	showModal('teamManageModal');
}

function renderTeamManageList(dept) {
	const teams = dept ? (dept.teams || []) : [];
	const c = document.getElementById('teamManageList');
	if (teams.length === 0) {
		c.innerHTML = '<div class="empty"><div class="empty-icon">👥</div><div class="empty-text">暂无团队</div></div>';
		return;
	}
	const tmpl = document.getElementById('tmpl-team-row').innerHTML;
	c.innerHTML = teams.map(t => {
		const tLeader = t.leader_name ? `<span>TL: ${escapeHtml(t.leader_name)}</span>` : '';
		return tmpl
			.replace(/__NAME__/g, escapeHtml(t.name))
			.replace(/__LEADER__/g, tLeader)
			.replace(/__TEAM_ID__/g, t.id)
			.replace(/__NAME_ESC__/g, escapeQuote(t.name))
			.replace(/__ACTIONS__/g, _teamActions(t));
	}).join('');
}

function cancelTeamEdit() {
	const allForms = document.querySelectorAll('.team-form');
	allForms.forEach(f => {
		f.classList.add('hidden');
		const idInput = f.querySelector('.team-form-id');
		const nameInput = f.querySelector('.team-form-name');
		const codeInput = f.querySelector('.team-form-code');
		const descInput = f.querySelector('.team-form-desc');
		if (idInput) idInput.value = '';
		if (nameInput) nameInput.value = '';
		if (codeInput) codeInput.value = '';
		if (descInput) descInput.value = '';
	});
	const addForm = document.getElementById('teamAddForm');
	if (addForm) {
		addForm.remove();
	}
	const listDiv = document.getElementById('teamManageList');
	if (listDiv) {
		listDiv.classList.remove('team-manage-editing');
	}
	const allCards = document.querySelectorAll('.team-card');
	allCards.forEach(c => c.classList.remove('team-card-editing'));
}

function openTeamForm(teamId) {
	cancelTeamEdit();

	if (teamId) {
		editTeam(teamId);
	} else {
		const list = document.getElementById('teamManageList');
		if (list) {
			list.classList.add('team-manage-editing');
		}
		const addForm = document.createElement('div');
		addForm.id = 'teamAddForm';
		addForm.className = 'team-form';
		addForm.innerHTML = `
			<div class="team-form-editing">
				<div class="flex items-center justify-between mb-12">
					<div class="text-sm font-medium">新增团队</div>
					<button class="btn btn-sm btn-ghost" onclick="cancelTeamEdit()" style="font-size:18px">&times;</button>
				</div>
				<input type="hidden" class="team-form-id">
				<div class="form-item">
					<label class="form-label">团队名称 <span class="required">*</span></label>
					<input class="team-form-name input" placeholder="如: AI 平台组">
				</div>
				<div class="form-item">
					<label class="form-label">编码 <span class="form-hint">（留空自动生成，含部门前缀）</span></label>
					<input class="team-form-code input" placeholder="如: yfzx_aiptz">
				</div>
				<div class="form-item">
				<label class="form-label">描述</label>
				<input class="team-form-desc input" placeholder="团队描述">
			</div>
			<div class="flex justify-end gap-8 mt-16">
				<button class="btn btn-sm btn-outline" onclick="cancelTeamEdit()">取消</button>
				<button class="btn btn-sm btn-primary" onclick="saveTeam()">保存</button>
			</div>
		</div>
	`;
		list.appendChild(addForm);
	}
}

function editTeam(teamId) {
	cancelTeamEdit();
	const formDiv = document.getElementById(`teamForm-${teamId}`);
	if (!formDiv) return;

	const dept = allDepts.find(d => d.id === _currentManageDeptId);
	const team = dept ? (dept.teams || []).find(t => t.id === teamId) : null;
	if (!team) return;

	const listDiv = document.getElementById('teamManageList');
	if (listDiv) {
		listDiv.classList.add('team-manage-editing');
	}

	const cardDiv = document.getElementById(`teamCard-${teamId}`);
	if (cardDiv) {
		cardDiv.classList.add('team-card-editing');
	}

	const titleSpan = document.getElementById(`teamFormTitle-${teamId}`);
	if (titleSpan) {
		titleSpan.textContent = team.name;
	}

	formDiv.classList.remove('hidden');
	formDiv.querySelector('.team-form-id').value = team.id;
	formDiv.querySelector('.team-form-name').value = team.name;
	formDiv.querySelector('.team-form-code').value = team.code || '';
	formDiv.querySelector('.team-form-desc').value = team.description || '';
}

async function saveTeam(teamId) {
	if (_saving) return;

	let form;
	if (teamId) {
		form = document.getElementById(`teamForm-${teamId}`);
	} else {
		form = document.getElementById('teamAddForm');
	}

	if (!form) return;

	const id = form.querySelector('.team-form-id').value;
	const deptId = _currentManageDeptId;
	const name = form.querySelector('.team-form-name').value.trim();
	const code = form.querySelector('.team-form-code').value.trim();
	const desc = form.querySelector('.team-form-desc').value.trim();

	if (!name) { toast('请输入团队名称', 'warning'); return; }
	if (!deptId) { toast('请先选择所属部门', 'warning'); return; }
	const deptIdNum = parseInt(deptId);
	if (isNaN(deptIdNum)) { toast('部门ID无效', 'warning'); return; }

	// 团队组长通过"任命组长"发起工单设置,不随组织基本信息一起提交
	const body = { name, code: code || undefined, description: desc || undefined, department_id: deptIdNum };

	_saving = true;
	try {
		if (id) {
			await api.patchJson(`${API_BASE}/teams/${id}/`, body);
			toast('团队已更新', 'success');
		} else {
			await api.postJson(`${API_BASE}/teams/`, body);
			toast('团队已添加', 'success');
		}
		cancelTeamEdit();
		await loadDepts();
		const dept = allDepts.find(d => d.id === _currentManageDeptId);
		renderTeamManageList(dept);
	} catch (e) { toast('保存失败: ' + escapeHtml(e.message), 'error'); }
	finally { _saving = false; }
}

async function deleteTeam(id, name) {
	if (!confirm(`确认删除团队"${name}"？仅当该团队下无用户时才能删除。`)) return;
	try {
		await api.deleteJson(`${API_BASE}/teams/${id}/`);
		toast('团队已删除', 'success');
		await loadDepts();
		const dept = allDepts.find(d => d.id === _currentManageDeptId);
		renderTeamManageList(dept);
	} catch (e) { toast('删除失败: ' + escapeHtml(e.message), 'error'); }
}