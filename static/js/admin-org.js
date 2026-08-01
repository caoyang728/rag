/* ============ 组织架构管理页 ============ */
const API_BASE = '/api/v1/auth';
let allDepts = [];
let _saving = false;
let _leaderSearchTimer = null;
let _leaderSearchSeq = 0;
let _currentManageDeptId = null;

function isKbAdmin() {
	return hasAnyRole('kb_admin');
}

document.addEventListener('DOMContentLoaded', () => {
	// 可访问组织架构页：超级管理员 / 文档管理员
	const canAccess = isSuperAdmin() || isKbAdmin();
	if (!canAccess) {
		document.body.innerHTML = document.getElementById('tmpl-no-permission').innerHTML;
		return;
	}
	loadDepts();
	document.addEventListener('click', function (e) {
		const deptResults = document.getElementById('deptLeaderResults');
		const deptSearch = document.getElementById('deptLeaderSearch');
		if (deptResults && deptSearch && !deptSearch.contains(e.target) && !deptResults.contains(e.target)) {
			deptResults.classList.remove('show');
		}
		const allTeamResults = document.querySelectorAll('.team-form-leader-results');
		allTeamResults.forEach(resultsDiv => {
			const form = resultsDiv.closest('.team-form');
			if (!form) return;
			const searchInput = form.querySelector('.team-form-leader-search');
			if (searchInput && !searchInput.contains(e.target) && !resultsDiv.contains(e.target)) {
				resultsDiv.classList.remove('show');
			}
		});
	});
});

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
	if (allDepts.length === 0) {
		c.innerHTML = '<div class="empty"><div class="empty-icon">🏢</div><div class="empty-text">暂无部门</div></div>';
		return;
	}
	const tmpl = document.getElementById('tmpl-dept-card').innerHTML;
	c.innerHTML = allDepts.map(d => {
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
			.replace(/__TEAMS_HTML__/g, teamsHtml);
	}).join('');
}

// ====================== 部门经理搜索（300ms 防抖 + 竞态处理） ======================
function searchDeptLeader() {
	clearTimeout(_leaderSearchTimer);
	_leaderSearchTimer = setTimeout(() => _doSearchDeptLeader(), 300);
}

async function _doSearchDeptLeader() {
	const seq = ++_leaderSearchSeq;
	const q = (document.getElementById('deptLeaderSearch').value || '').trim();
	const resultsDiv = document.getElementById('deptLeaderResults');
	if (!q) {
		resultsDiv.classList.remove('show');
		return;
	}
	try {
		const data = await api.getJson(`${API_BASE}/users/search/?q=${encodeURIComponent(q)}`);
		// 竞态检查：如果有更新的请求已发出，丢弃本次结果
		if (seq !== _leaderSearchSeq) return;
		const users = data.users || [];
		if (users.length === 0) {
			resultsDiv.innerHTML = '<div style="padding:10px 14px;font-size:13px;color:var(--text-sub)">无匹配用户</div>';
		} else {
			const tmpl = document.getElementById('tmpl-user-search-result').innerHTML;
			resultsDiv.innerHTML = users.map(u => tmpl
				.replace(/__ONCLICK__/g, `selectDeptLeader(${u.id},'${escapeQuote(u.real_name || u.username)}')`)
				.replace(/__NAME__/g, escapeHtml(u.real_name || u.username))
				.replace(/__EMAIL__/g, escapeHtml(u.email || ''))
			).join('');
		}
		resultsDiv.classList.add('show');
	} catch (e) {
		console.error('搜索用户失败:', e);
	}
}

function selectDeptLeader(id, name) {
	document.getElementById('deptLeaderId').value = id;
	document.getElementById('deptLeaderSearch').value = name;
	document.getElementById('deptLeaderResults').classList.remove('show');
}

// ====================== 部门管理 ======================
function openDeptModal(id) {
	document.getElementById('deptId').value = '';
	document.getElementById('deptName').value = '';
	document.getElementById('deptCode').value = '';
	document.getElementById('deptLeaderId').value = '';
	document.getElementById('deptLeaderSearch').value = '';
	document.getElementById('deptLeaderResults').classList.remove('show');
	document.getElementById('deptModalTitle').textContent = '新增部门';

	if (id) {
		const d = allDepts.find(x => x.id === id);
		if (d) {
			document.getElementById('deptId').value = d.id;
			document.getElementById('deptName').value = d.name;
			document.getElementById('deptCode').value = d.code || '';
			if (d.leader_id) {
				document.getElementById('deptLeaderId').value = d.leader_id;
				document.getElementById('deptLeaderSearch').value = d.leader_name || '';
			}
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
	const leaderId = document.getElementById('deptLeaderId').value;
	if (!name) { toast('请输入部门名称', 'warning'); return; }
	const body = { name, code: code || undefined };
	if (leaderId) {
		body.leader_id = parseInt(leaderId);
	}
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
			.replace(/__NAME_ESC__/g, escapeQuote(t.name));
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
		const leaderIdInput = f.querySelector('.team-form-leader-id');
		const leaderSearchInput = f.querySelector('.team-form-leader-search');
		const leaderSelected = f.querySelector('.team-form-leader-selected');
		const leaderResults = f.querySelector('.team-form-leader-results');
		if (idInput) idInput.value = '';
		if (nameInput) nameInput.value = '';
		if (codeInput) codeInput.value = '';
		if (descInput) descInput.value = '';
		if (leaderIdInput) leaderIdInput.value = '';
		if (leaderSearchInput) leaderSearchInput.value = '';
		if (leaderSelected) {
			leaderSelected.textContent = '';
			leaderSelected.className = 'text-sub text-sm mt-8';
		}
		if (leaderResults) leaderResults.classList.remove('show');
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
				<div class="form-item">
					<label class="form-label">团队组长</label>
					<div class="relative">
						<input class="team-form-leader-search input" placeholder="搜索团队内用户姓名..." autocomplete="off">
						<div class="team-form-leader-results dropdown-menu"></div>
					</div>
					<input type="hidden" class="team-form-leader-id">
					<div class="team-form-leader-selected text-sub text-sm mt-8"></div>
				</div>
				<div class="flex justify-end gap-8 mt-16">
					<button class="btn btn-sm btn-outline" onclick="cancelTeamEdit()">取消</button>
					<button class="btn btn-sm btn-primary" onclick="saveTeam()">保存</button>
				</div>
			</div>
		`;
		list.appendChild(addForm);
		const searchInput = addForm.querySelector('.team-form-leader-search');
		searchInput.oninput = () => searchTeamLeader(null);
		searchInput.onfocus = () => searchTeamLeader(null);
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

	if (team.leader_id) {
		formDiv.querySelector('.team-form-leader-id').value = team.leader_id;
		formDiv.querySelector('.team-form-leader-search').value = team.leader_name || '';
		formDiv.querySelector('.team-form-leader-selected').textContent = '已选择: ' + (team.leader_name || '');
		formDiv.querySelector('.team-form-leader-selected').className = 'text-sm';
	}

	const searchInput = formDiv.querySelector('.team-form-leader-search');
	searchInput.oninput = () => searchTeamLeader(teamId);
	searchInput.onfocus = () => searchTeamLeader(teamId);
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
	const leaderId = form.querySelector('.team-form-leader-id').value;

	if (!name) { toast('请输入团队名称', 'warning'); return; }
	if (!deptId) { toast('请先选择所属部门', 'warning'); return; }
	const deptIdNum = parseInt(deptId);
	if (isNaN(deptIdNum)) { toast('部门ID无效', 'warning'); return; }

	const body = { name, code: code || undefined, description: desc || undefined, department_id: deptIdNum };
	if (leaderId) {
		body.leader_id = parseInt(leaderId);
	}

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

// ====================== 团队 Leader 搜索（300ms 防抖 + 竞态处理） ======================
let _teamSearchTimers = {};
let _teamSearchSeq = {};

function searchTeamLeader(teamId) {
	const key = teamId || 'add';
	if (_teamSearchTimers[key]) clearTimeout(_teamSearchTimers[key]);
	_teamSearchTimers[key] = setTimeout(() => _doSearchTeamLeader(teamId), 300);
}

async function _doSearchTeamLeader(teamId) {
	const key = teamId || 'add';
	const seq = (_teamSearchSeq[key] || 0) + 1;
	_teamSearchSeq[key] = seq;

	let form;
	if (teamId) {
		form = document.getElementById(`teamForm-${teamId}`);
	} else {
		form = document.getElementById('teamAddForm');
	}
	if (!form) return;

	const q = (form.querySelector('.team-form-leader-search').value || '').trim();
	const resultsDiv = form.querySelector('.team-form-leader-results');

	if (!q) {
		resultsDiv.classList.remove('show');
		return;
	}

	const deptId = _currentManageDeptId;
	let url = `${API_BASE}/users/search/?q=${encodeURIComponent(q)}`;
	if (deptId) {
		url += `&department_id=${deptId}`;
	}

	try {
		const data = await api.getJson(url);
		// 竞态检查
		if (seq !== _teamSearchSeq[key]) return;
		const users = data.users || [];
		if (users.length === 0) {
			resultsDiv.innerHTML = '<div style="padding:10px 14px;font-size:13px;color:var(--text-sub)">无匹配用户</div>';
		} else {
			const tmpl = document.getElementById('tmpl-user-search-result').innerHTML;
			resultsDiv.innerHTML = users.map(u => {
				const onclick = teamId
					? `selectTeamLeader(${teamId}, ${u.id},'${escapeQuote(u.real_name || u.username)}')`
					: `selectTeamLeader(null, ${u.id},'${escapeQuote(u.real_name || u.username)}')`;
				return tmpl
					.replace(/__ONCLICK__/g, onclick)
					.replace(/__NAME__/g, escapeHtml(u.real_name || u.username))
					.replace(/__EMAIL__/g, escapeHtml(u.email || ''));
			}).join('');
		}
		resultsDiv.classList.add('show');
	} catch (e) {
		console.error('搜索用户失败:', e);
	}
}

function selectTeamLeader(teamId, userId, userName) {
	let form;
	if (teamId) {
		form = document.getElementById(`teamForm-${teamId}`);
	} else {
		form = document.getElementById('teamAddForm');
	}

	if (!form) return;

	form.querySelector('.team-form-leader-id').value = userId;
	form.querySelector('.team-form-leader-selected').textContent = '已选择: ' + userName;
	form.querySelector('.team-form-leader-selected').className = 'text-sm';
	form.querySelector('.team-form-leader-search').value = userName;
	form.querySelector('.team-form-leader-results').classList.remove('show');
}