/* ============ RBAC 权限配置页（弹窗版） ============ */
const API_BASE = '/api/v1/auth';
let allDepts = [];
let _saving = false; // 防重复提交

function closeModal(id) {
	document.getElementById(id).style.display = 'none';
	document.getElementById('mask').style.display = 'none';
}

function showModal(id) {
	document.getElementById(id).style.display = 'flex';
	document.getElementById('mask').style.display = 'block';
}

document.addEventListener('DOMContentLoaded', () => {
	let isAdmin = false;
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		isAdmin = (u.roles || []).some(r => r.role__code === 'super_admin');
	} catch (e) { }
	if (!isAdmin) {
		document.body.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;color:var(--text-sub)"><div style="font-size:48px;margin-bottom:16px">🔒</div><div style="font-size:16px">仅超级管理员可访问此页面</div><a href="/chat/" style="margin-top:16px;color:var(--primary)">返回首页</a></div>`;
		return;
	}
	loadDepts();
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
	c.innerHTML = allDepts.map(d => `
    <div class="dept-card" style="border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:8px">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <div>
          <span style="font-weight:600;font-size:15px">${escapeHtml(d.name)}</span>
          <span class="text-sub text-sm" style="margin-left:8px">${escapeHtml(d.code || '—')}</span>
          <span class="tag tag-sm" style="margin-left:8px;background:var(--bg-sub)">${d.user_count || 0} 人</span>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn btn-sm btn-outline" onclick="openDeptModal(${d.id})">编辑</button>
          <button class="btn btn-sm btn-outline" style="color:var(--danger)" onclick="deleteDept(${d.id},'${escapeQuote(d.name)}')">删除</button>
        </div>
      </div>
      ${(d.teams || []).length > 0 ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">${d.teams.map(t => `<span class="tag tag-sm" style="background:var(--primary-light)">${escapeHtml(t.name)}</span>`).join('')}</div>` : ''}
    </div>
  `).join('');
}

function openDeptModal(id) {
	document.getElementById('deptId').value = '';
	document.getElementById('deptName').value = '';
	document.getElementById('deptCode').value = '';
	document.getElementById('deptTeamsSection').style.display = 'none';
	document.getElementById('deptTeamsList').innerHTML = '';
	document.getElementById('deptModalTitle').textContent = '新增部门';

	if (id) {
		const d = allDepts.find(x => x.id === id);
		if (d) {
			document.getElementById('deptId').value = d.id;
			document.getElementById('deptName').value = d.name;
			document.getElementById('deptCode').value = d.code || '';
			document.getElementById('deptModalTitle').textContent = '编辑部门';
			document.getElementById('deptTeamsSection').style.display = 'block';
			renderDeptTeams(d);
		}
	}
	showModal('deptModal');
}

function renderDeptTeams(dept) {
	const teams = dept.teams || [];
	const c = document.getElementById('deptTeamsList');
	c.innerHTML = teams.length === 0
		? '<span class="text-sub text-sm">暂无团队</span>'
		: teams.map(t => `
      <span class="tag tag-closable" style="padding:4px 10px;font-size:13px">
        ${escapeHtml(t.name)}
        <button class="tag-close-btn" onclick="editTeam(${dept.id},${t.id})" title="编辑" style="font-size:12px;margin:0 2px">✎</button>
        <button class="tag-close-btn" onclick="deleteTeam(${t.id},'${escapeQuote(t.name)}')" title="删除" style="color:var(--danger)">×</button>
      </span>
    `).join('');
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
	} catch (e) { toast('保存失败: ' + e.message, 'error'); }
	finally { _saving = false; }
}

async function deleteDept(id, name) {
	// confirm() 是纯文本对话框，不需要 HTML 转义
	if (!confirm(`确认删除部门"${name}"？仅当该部门下无用户且无团队时才能删除。`)) return;
	try {
		await api.deleteJson(`${API_BASE}/departments/${id}/`);
		toast('部门已删除', 'success');
		await loadDepts();
	} catch (e) { toast('删除失败: ' + e.message, 'error'); }
}

// ====================== 团队管理 ======================
async function openTeamModal(deptId, teamId) {
	document.getElementById('teamId').value = '';
	document.getElementById('teamDeptId').value = deptId || document.getElementById('deptId').value;
	document.getElementById('teamName').value = '';
	document.getElementById('teamCode').value = '';
	document.getElementById('teamDesc').value = '';
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
	if (!name) { toast('请输入团队名称', 'warning'); return; }
	if (!deptId) { toast('请先选择所属部门', 'warning'); return; }
	const deptIdNum = parseInt(deptId);
	if (isNaN(deptIdNum)) { toast('部门ID无效', 'warning'); return; }
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
	} catch (e) { toast('保存失败: ' + e.message, 'error'); }
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
	} catch (e) { toast('删除失败: ' + e.message, 'error'); }
}

// 转义单引号，用于 onclick 属性中的字符串参数
// 注意：先转义反斜杠，再转义单引号，避免二次转义
function escapeQuote(s) {
	return String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}
