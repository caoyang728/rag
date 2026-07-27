/* ============ RBAC 权限配置页（弹窗版） ============ */
const API_BASE = '/api/v1/auth';
let allDepts = [];
let _saving = false;
let _leaderSearchTimer = null;

function closeModal(id) {
	document.getElementById(id).style.display = 'none';
	document.getElementById('mask').style.display = 'none';
}

function showModal(id) {
	document.getElementById(id).style.display = 'flex';
	document.getElementById('mask').style.display = 'block';
}

function isKbAdmin() {
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		return (u.roles || []).some(r => r.role__code === 'kb_admin');
	} catch (e) { return false; }
}

function isSuperAdmin() {
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		return (u.roles || []).some(r => r.role__code === 'super_admin');
	} catch (e) { return false; }
}

function isDeptManager() {
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		return (u.roles || []).some(r => r.role__code === 'dept_manager');
	} catch (e) { return false; }
}

document.addEventListener('DOMContentLoaded', () => {
	const canAccess = isSuperAdmin() || isKbAdmin();
	if (!canAccess) {
		document.body.innerHTML = document.getElementById('tmpl-no-permission').innerHTML;
		return;
	}
	loadDepts();
	// 点击外部关闭搜索下拉
	document.addEventListener('click', function (e) {
		const deptResults = document.getElementById('deptLeaderResults');
		const deptSearch = document.getElementById('deptLeaderSearch');
		const teamResults = document.getElementById('teamLeaderResults');
		const teamSearch = document.getElementById('teamLeaderSearch');
		if (deptResults && deptSearch && !deptSearch.contains(e.target) && !deptResults.contains(e.target)) {
			deptResults.classList.add('hidden');
		}
		if (teamResults && teamSearch && !teamSearch.contains(e.target) && !teamResults.contains(e.target)) {
			teamResults.classList.add('hidden');
		}
	});
});

async function loadDepts() {
	try {
		const data = await api.getJson(`${API_BASE}/departments/`);
		allDepts = Array.isArray(data) ? data : (data.results || []);
		renderDepts();
	} catch (e) { console.error(e); }
}

function renderDepts() {
	const c = document.getElementById('deptList');
	if (allDepts.length === 0) {
		c.innerHTML = '<div class="text-sub text-sm" style="padding:20px;text-align:center">暂无部门</div>';
		return;
	}
	const tmpl = document.getElementById('tmpl-dept-card').innerHTML;
	c.innerHTML = allDepts.map(d => {
		const leaderInfo = d.leader_name ? ` · 经理: ${escapeHtml(d.leader_name)}` : '';
		const teamsHtml = (d.teams || []).length > 0
			? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">${d.teams.map(t => {
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

// ====================== 部门经理搜索 ======================
async function searchDeptLeader() {
	const q = (document.getElementById('deptLeaderSearch').value || '').trim();
	const resultsDiv = document.getElementById('deptLeaderResults');
	if (!q) {
		resultsDiv.classList.add('hidden');
		return;
	}
	try {
		const data = await api.getJson(`${API_BASE}/users/search/?q=${encodeURIComponent(q)}`);
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
		resultsDiv.classList.remove('hidden');
	} catch (e) {
		console.error('搜索用户失败:', e);
	}
}

function selectDeptLeader(id, name) {
	document.getElementById('deptLeaderId').value = id;
	document.getElementById('deptLeaderSelected').textContent = '已选择: ' + name;
	document.getElementById('deptLeaderSelected').className = 'text-sm';
	document.getElementById('deptLeaderSearch').value = name;
	document.getElementById('deptLeaderResults').classList.add('hidden');
}

// ====================== 团队 Leader 搜索 ======================
async function searchTeamLeader() {
	const q = (document.getElementById('teamLeaderSearch').value || '').trim();
	const resultsDiv = document.getElementById('teamLeaderResults');
	if (!q) {
		resultsDiv.classList.add('hidden');
		return;
	}
	const deptId = document.getElementById('teamDeptId').value;
	let url = `${API_BASE}/users/search/?q=${encodeURIComponent(q)}`;
	// 如果已知部门，限定搜索该部门下的用户
	if (deptId) {
		url += `&department_id=${deptId}`;
	}
	try {
		const data = await api.getJson(url);
		const users = data.users || [];
		if (users.length === 0) {
			resultsDiv.innerHTML = '<div style="padding:10px 14px;font-size:13px;color:var(--text-sub)">无匹配用户</div>';
		} else {
			const tmpl = document.getElementById('tmpl-user-search-result').innerHTML;
			resultsDiv.innerHTML = users.map(u => tmpl
				.replace(/__ONCLICK__/g, `selectTeamLeader(${u.id},'${escapeQuote(u.real_name || u.username)}')`)
				.replace(/__NAME__/g, escapeHtml(u.real_name || u.username))
				.replace(/__EMAIL__/g, escapeHtml(u.email || ''))
			).join('');
		}
		resultsDiv.classList.remove('hidden');
	} catch (e) {
		console.error('搜索用户失败:', e);
	}
}

function selectTeamLeader(id, name) {
	document.getElementById('teamLeaderId').value = id;
	document.getElementById('teamLeaderSelected').textContent = '已选择: ' + name;
	document.getElementById('teamLeaderSelected').className = 'text-sm';
	document.getElementById('teamLeaderSearch').value = name;
	document.getElementById('teamLeaderResults').classList.add('hidden');
}

// ====================== 部门管理 ======================
function openDeptModal(id) {
	document.getElementById('deptId').value = '';
	document.getElementById('deptName').value = '';
	document.getElementById('deptCode').value = '';
	document.getElementById('deptLeaderId').value = '';
	document.getElementById('deptLeaderSearch').value = '';
	document.getElementById('deptLeaderSelected').textContent = '';
	document.getElementById('deptLeaderResults').classList.add('hidden');
	document.getElementById('deptTeamsSection').classList.add('hidden');
	document.getElementById('deptTeamsList').innerHTML = '';
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
				document.getElementById('deptLeaderSelected').textContent = '部门经理: ' + (d.leader_name || '');
				document.getElementById('deptLeaderSelected').className = 'text-sm';
			}
			document.getElementById('deptModalTitle').textContent = '编辑部门';
			document.getElementById('deptTeamsSection').classList.remove('hidden');
			renderDeptTeams(d);
		}
	}
	showModal('deptModal');
}

function renderDeptTeams(dept) {
	const teams = dept.teams || [];
	const c = document.getElementById('deptTeamsList');
	if (teams.length === 0) {
		c.innerHTML = '<span class="text-sub text-sm">暂无团队</span>';
		return;
	}
	const tmpl = document.getElementById('tmpl-team-tag').innerHTML;
	c.innerHTML = teams.map(t => {
		const tLeader = t.leader_name ? ` · ${escapeHtml(t.leader_name)}` : '';
		return tmpl
			.replace(/__NAME__/g, escapeHtml(t.name))
			.replace(/__LEADER__/g, tLeader)
			.replace(/__DEPT_ID__/g, dept.id)
			.replace(/__TEAM_ID__/g, t.id)
			.replace(/__NAME_ESC__/g, escapeQuote(t.name));
	}).join('');
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
async function openTeamModal(deptId, teamId) {
	document.getElementById('teamId').value = '';
	document.getElementById('teamDeptId').value = deptId || document.getElementById('deptId').value;
	document.getElementById('teamName').value = '';
	document.getElementById('teamCode').value = '';
	document.getElementById('teamDesc').value = '';
	document.getElementById('teamLeaderId').value = '';
	document.getElementById('teamLeaderSearch').value = '';
	document.getElementById('teamLeaderSelected').textContent = '';
	document.getElementById('teamLeaderResults').classList.add('hidden');
	document.getElementById('teamModalTitle').textContent = '新增团队';

	if (teamId) {
		// 直接按 ID 获取单个团队，避免拉取全量列表
		try {
			const t = await api.getJson(`${API_BASE}/teams/${teamId}/`);
			if (t) {
				document.getElementById('teamId').value = t.id;
				document.getElementById('teamName').value = t.name;
				document.getElementById('teamCode').value = t.code || '';
				document.getElementById('teamDesc').value = t.description || '';
				if (t.department_id) {
					document.getElementById('teamDeptId').value = t.department_id;
				}
				if (t.leader_id) {
					document.getElementById('teamLeaderId').value = t.leader_id;
					document.getElementById('teamLeaderSearch').value = t.leader_name || '';
					document.getElementById('teamLeaderSelected').textContent = '团队组长: ' + (t.leader_name || '');
					document.getElementById('teamLeaderSelected').className = 'text-sm';
				}
				document.getElementById('teamModalTitle').textContent = '编辑团队';
			}
		} catch (e) { /* ignore */ }
	}
	showModal('teamModal');
}

function editTeam(deptId, teamId) {
	openTeamModal(deptId, teamId);
}

async function saveTeam() {
	if (_saving) return;
	const id = document.getElementById('teamId').value;
	const deptId = document.getElementById('teamDeptId').value;
	const name = document.getElementById('teamName').value.trim();
	const code = document.getElementById('teamCode').value.trim();
	const desc = document.getElementById('teamDesc').value.trim();
	const leaderId = document.getElementById('teamLeaderId').value;
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
		closeModal('teamModal');
		// 如果部门弹窗还开着，直接刷新当前部门的团队列表
		if (document.getElementById('deptModal').style.display === 'flex') {
			const did = parseInt(document.getElementById('deptId').value);
			if (did) {
				await loadDepts();
				openDeptModal(did);
			}
		} else {
			await loadDepts();
		}
	} catch (e) { toast('保存失败: ' + escapeHtml(e.message), 'error'); }
	finally { _saving = false; }
}

async function deleteTeam(id, name) {
	// confirm() 是纯文本对话框，不需要 HTML 转义
	if (!confirm(`确认删除团队"${name}"？仅当该团队下无用户时才能删除。`)) return;
	try {
		await api.deleteJson(`${API_BASE}/teams/${id}/`);
		toast('团队已删除', 'success');
		const did = parseInt(document.getElementById('deptId').value);
		await loadDepts();
		if (did) openDeptModal(did);
	} catch (e) { toast('删除失败: ' + escapeHtml(e.message), 'error'); }
}

// 转义单引号，用于 onclick 属性中的字符串参数
// 注意：先转义反斜杠，再转义单引号，避免二次转义
function escapeQuote(s) {
	return String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}
