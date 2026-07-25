/* ============ 个人中心 ============ */

document.addEventListener('DOMContentLoaded', () => {
	initProfilePage();
});

async function initProfilePage() {
	await loadProfile();
	await loadMemoryData();
	await loadSubscriptionData();
	setProfileMenu(STATE.currentProfileMenu || 'basic');
}

async function loadProfile() {
	try {
		const data = await api.getJson('/api/v1/auth/profile/');
		if (data) {
			STATE.user = {
				...STATE.user,
				id: data.id || STATE.user.id,
				name: data.real_name || data.username || '用户',
				email: data.email || '',
				dept: data.department_name || '',
				team: '',
				role: (data.roles && data.roles.length > 0) ? (data.roles[0].role__name || '用户') : '用户',
				avatar: (data.real_name || data.username || '?').charAt(0),
				phone: data.phone || '',
				created_at: data.created_at || ''
			};
		}
	} catch (e) {
		console.error('load profile failed:', e);
	}
}

async function loadMemoryData() {
	try {
		const data = await api.getJson('/api/v1/memory/user-memory/');
		if (data) {
			STATE.memoryTags = data.domain_tags || [];
			STATE.memorySearchTypes = data.frequent_topics || [];
			STATE.memoryPreferences = data.preferences || {};
			STATE.memoryProfileText = data.profile_text || '';
		}
	} catch (e) {
		console.error('load memory data failed:', e);
		STATE.memoryTags = [];
		STATE.memorySearchTypes = [];
		STATE.memoryPreferences = {};
		STATE.memoryProfileText = '';
	}
}

async function loadSubscriptionData() {
	try {
		const data = await api.getJson('/api/v1/notification/subscriptions/');
		if (data && data.subscriptions) {
			STATE.subscriptions = data.subscriptions;
		}
	} catch (e) {
		console.error('load subscription data failed:', e);
		STATE.subscriptions = {};
	}
}

function setProfileMenu(m) {
	STATE.currentProfileMenu = m;
	const menuKeys = ['basic', 'memory', 'permissions', 'email', 'pwd'];
	$$('.profile-menu-item').forEach((it, i) => {
		it.classList.toggle('active', menuKeys[i] === m);
	});
	const box = $('#profileContent');
	if (box) renderProfileContent(m, box);
	// 权限页加载异步数据
	if (m === 'permissions') {
		loadMyPermissions();
		loadPermissionApplications();
	}
}

/* ============ 基本信息 ============ */
function renderBasicTab(box) {
	const tmpl = document.getElementById('tmpl-profile-basic');
	box.innerHTML = '';
	box.appendChild(tmpl.content.cloneNode(true));

	const u = STATE.user;
	const joinedAt = u.created_at ? formatDate(u.created_at) : '';

	const avatarEl = box.querySelector('#pf-avatar');
	if (avatarEl) avatarEl.textContent = escapeHtml(u.avatar);

	const nameEl = box.querySelector('#pf-name');
	if (nameEl) nameEl.textContent = escapeHtml(u.name);

	const roleDeptEl = box.querySelector('#pf-role-dept');
	if (roleDeptEl) roleDeptEl.textContent = escapeHtml(u.role) + ' · ' + escapeHtml(u.dept) + ' / ' + escapeHtml(u.team);

	const metaEl = box.querySelector('#pf-meta');
	if (metaEl) {
		let meta = '用户ID ' + (u.id || '');
		if (joinedAt) meta += ' · 加入时间 ' + joinedAt;
		metaEl.textContent = meta;
	}

	const nameInput = box.querySelector('#profileName');
	if (nameInput) nameInput.value = u.name || '';

	const emailInput = box.querySelector('#pf-email');
	if (emailInput) emailInput.value = u.email || '';

	const deptInput = box.querySelector('#pf-dept');
	if (deptInput) deptInput.value = u.dept || '';

	const teamInput = box.querySelector('#pf-team');
	if (teamInput) teamInput.value = u.team || '';

	const phoneInput = box.querySelector('#profilePhone');
	if (phoneInput) phoneInput.value = u.phone || '';

	const roleInput = box.querySelector('#pf-role');
	if (roleInput) roleInput.value = u.role || '';
}

/* ============ 我的记忆 ============ */
function renderMemoryTab(box) {
	const tmpl = document.getElementById('tmpl-profile-memory');
	box.innerHTML = '';
	box.appendChild(tmpl.content.cloneNode(true));

	// 职业标签
	const tagsHTML = (STATE.memoryTags || []).map(t =>
		`<span class="tag tag-primary tag-closable" onclick="removeMemoryTag(this)" data-tag="${escapeHtml(t)}">${escapeHtml(t)} <span class="tag-close">×</span></span>`
	).join('');
	const tagsBtn = box.querySelector('#memoryTagsWrap button');
	if (tagsBtn && tagsHTML) tagsBtn.insertAdjacentHTML('beforebegin', tagsHTML);

	// 常用检索类型
	const searchTypesHTML = (STATE.memorySearchTypes || []).map(t =>
		`<span class="tag tag-info tag-closable" onclick="removeMemorySearchType(this)" data-tag="${escapeHtml(t)}">${escapeHtml(t)} <span class="tag-close">×</span></span>`
	).join('');
	const searchBtn = box.querySelector('#memorySearchTypesWrap button');
	if (searchBtn && searchTypesHTML) searchBtn.insertAdjacentHTML('beforebegin', searchTypesHTML);

	// 输出偏好
	const outputPref = (STATE.memoryPreferences && STATE.memoryPreferences.output_preference) || '';
	const outputEl = box.querySelector('#memoryOutputPref');
	if (outputEl) outputEl.value = outputPref;

	// 已提炼的偏好
	const profileLines = STATE.memoryProfileText
		? STATE.memoryProfileText.split('\n').filter(l => l.trim()).map(l => `• ${escapeHtml(l)}`).join('<br>')
		: '暂无自动提炼的偏好数据，系统每 24 小时基于你的行为异步提炼一次';
	const displayEl = box.querySelector('#profileTextDisplay');
	if (displayEl) displayEl.innerHTML = profileLines;
}

/* ============ 权限 ============ */
function renderPermissionsTab(box) {
	const tmpl = document.getElementById('tmpl-profile-perms');
	box.innerHTML = '';
	box.appendChild(tmpl.content.cloneNode(true));
}

/* ============ 邮件订阅 ============ */
function renderEmailTab(box) {
	const tmpl = document.getElementById('tmpl-profile-subscription');
	box.innerHTML = '';
	box.appendChild(tmpl.content.cloneNode(true));

	const subs = STATE.subscriptions || {};
	const hintEl = box.querySelector('#sub-email-hint');
	if (hintEl) hintEl.innerHTML = '📬 订阅内容将发送至 <b>' + escapeHtml(STATE.user.email) + '</b>，可随时取消订阅';

	const items = [
		{ key: 'node_update', icon: '📁', title: '订阅知识库节点更新', desc: '当你关注的节点有新文档上传时，每天汇总一次发送邮件' },
		{ key: 'system_notice', icon: '🚨', title: '系统告警通知', desc: '上传失败、账号异常登录、权限变更等重要事件即时告警' },
		{ key: 'daily_report', icon: '📊', title: '每周报表推送', desc: '每周一 09:00 推送上周问答统计、满意率、热门问题' },
		{ key: 'keyword_alert', icon: '🔍', title: '关键词命中通知', desc: '当有新文档包含你关注的关键词时立即通知' },
	];

	const checkboxesEl = box.querySelector('#sub-checkboxes');
	if (checkboxesEl) {
		checkboxesEl.innerHTML = items.map(item => {
			const checked = subs[item.key] && subs[item.key].is_enabled ? 'checked' : '';
			return '<label class="checkbox" style="padding:14px;border:1px solid var(--border);border-radius:var(--radius);align-items:flex-start">' +
				'<input type="checkbox" ' + checked + ' style="margin-top:2px" data-subkey="' + item.key + '">' +
				'<div><div class="fw-500">' + item.icon + ' ' + item.title + '</div>' +
				'<div class="text-sub text-sm mt-4">' + item.desc + '</div></div></label>';
		}).join('');
	}
}

/* ============ 修改密码 ============ */
function renderPwdTab(box) {
	const tmpl = document.getElementById('tmpl-profile-password');
	box.innerHTML = '';
	box.appendChild(tmpl.content.cloneNode(true));
}

function renderProfileContent(m, box) {
	if (m === 'basic') { renderBasicTab(box); return; }
	if (m === 'memory') { renderMemoryTab(box); return; }
	if (m === 'permissions') { renderPermissionsTab(box); return; }
	if (m === 'email') { renderEmailTab(box); return; }
	if (m === 'pwd') { renderPwdTab(box); return; }
}

/* ============ 保存基本信息 ============ */
async function saveProfile() {
	const name = $('#profileName')?.value?.trim();
	const phone = $('#profilePhone')?.value?.trim();

	if (!name) {
		toast('请输入姓名', 'error');
		return;
	}

	try {
		const data = await api.patchJson('/api/v1/auth/profile/', {
			real_name: name,
			phone: phone
		});
		STATE.user.name = data.real_name || name;
		STATE.user.phone = data.phone || phone;
		toast('保存成功', 'success');
	} catch (e) {
		toast(e.message || '保存失败', 'error');
		console.error('save profile failed:', e);
	}
}

/* ============ 保存记忆 ============ */
async function saveMemory() {
	const outputPref = $('#memoryOutputPref')?.value || '';
	const tags = getCurrentMemoryTags();
	const searchTypes = getCurrentMemorySearchTypes();

	try {
		const payload = {
			domain_tags: tags,
			frequent_topics: searchTypes,
		};
		if (outputPref) {
			payload.output_preference = outputPref;
		}
		await api.patchJson('/api/v1/memory/user-memory/', payload);
		STATE.memoryTags = tags;
		STATE.memorySearchTypes = searchTypes;
		STATE.memoryPreferences = { ...(STATE.memoryPreferences || {}), output_preference: outputPref };
		toast('记忆已更新', 'success');
	} catch (e) {
		toast(e.message || '保存失败', 'error');
		console.error('save memory failed:', e);
	}
}

async function clearAllMemory() {
	if (!confirm('确认清空所有个人记忆？此操作不可恢复。')) return;
	try {
		await api.patchJson('/api/v1/memory/user-memory/', {
			domain_tags: [],
			frequent_topics: [],
			output_preference: '',
		});
		STATE.memoryTags = [];
		STATE.memorySearchTypes = [];
		STATE.memoryPreferences = {};
		toast('已清空', 'success');
		setProfileMenu('memory');
	} catch (e) {
		toast(e.message || '清空失败', 'error');
	}
}

function getCurrentMemoryTags() {
	const wrap = document.getElementById('memoryTagsWrap');
	if (!wrap) return STATE.memoryTags || [];
	const tags = [];
	wrap.querySelectorAll('.tag-primary.tag-closable').forEach(el => {
		const t = el.getAttribute('data-tag') || el.childNodes[0]?.textContent?.trim();
		if (t && el.querySelector('.tag-close')) tags.push(t);
	});
	return tags;
}

function getCurrentMemorySearchTypes() {
	const wrap = document.getElementById('memorySearchTypesWrap');
	if (!wrap) return STATE.memorySearchTypes || [];
	const types = [];
	wrap.querySelectorAll('.tag-info.tag-closable').forEach(el => {
		const t = el.getAttribute('data-tag') || el.childNodes[0]?.textContent?.trim();
		if (t && el.querySelector('.tag-close')) types.push(t);
	});
	return types;
}

/* ---- 记忆标签操作 ---- */
function addMemoryTag() {
	const input = prompt('请输入职业标签：');
	if (!input) return;
	const tag = input.trim();
	if (!tag) { toast('标签不能为空', 'error'); return; }
	const wrap = document.getElementById('memoryTagsWrap');
	if (!wrap) return;
	const btn = wrap.querySelector('button');
	const span = document.createElement('span');
	span.className = 'tag tag-primary tag-closable';
	span.setAttribute('data-tag', tag);
	span.innerHTML = `${escapeHtml(tag)} <span class="tag-close">×</span>`;
	span.onclick = () => removeMemoryTag(span);
	wrap.insertBefore(span, btn);
}

function removeMemoryTag(el) {
	if (confirm('确定删除此标签？')) {
		el.remove();
	}
}

function addMemorySearchType() {
	const input = prompt('请输入常用检索类型：');
	if (!input) return;
	const type = input.trim();
	if (!type) { toast('检索类型不能为空', 'error'); return; }
	const wrap = document.getElementById('memorySearchTypesWrap');
	if (!wrap) return;
	const btn = wrap.querySelector('button');
	const span = document.createElement('span');
	span.className = 'tag tag-info tag-closable';
	span.setAttribute('data-tag', type);
	span.innerHTML = `${escapeHtml(type)} <span class="tag-close">×</span>`;
	span.onclick = () => removeMemorySearchType(span);
	wrap.insertBefore(span, btn);
}

function removeMemorySearchType(el) {
	if (confirm('确定删除此检索类型？')) {
		el.remove();
	}
}

/* ============ 保存邮件订阅 ============ */
async function saveSubscriptions() {
	const checkboxes = document.querySelectorAll('#profileContent input[type="checkbox"][data-subkey]');
	const subscriptions = {};
	checkboxes.forEach(cb => {
		subscriptions[cb.dataset.subkey] = cb.checked;
	});

	try {
		await api.patchJson('/api/v1/notification/subscriptions/', { subscriptions });
		// 更新本地状态
		if (!STATE.subscriptions) STATE.subscriptions = {};
		for (const [key, val] of Object.entries(subscriptions)) {
			STATE.subscriptions[key] = { is_enabled: val, label: STATE.subscriptions[key]?.label || key };
		}
		toast('订阅偏好已更新', 'success');
	} catch (e) {
		toast(e.message || '保存失败', 'error');
	}
}

/* ============ 密码强度 ============ */
function updatePwdStrength(v) {
	const el = $('#pwdStrength');
	const hint = $('#pwdHint');
	if (!el || !hint) return;

	el.className = 'password-strength';
	let score = 0;
	if (v.length >= 8) score++;
	if (/[a-z]/.test(v)) score++;
	if (/[A-Z]/.test(v)) score++;
	if (/\d/.test(v)) score++;
	if (/[!@#$%^&*(),.?":{}|<>]/.test(v)) score++;

	if (score <= 2) {
		el.classList.add('weak');
		hint.textContent = '密码强度：弱';
		hint.style.color = 'var(--danger)';
	} else if (score <= 3) {
		el.classList.add('medium');
		hint.textContent = '密码强度：中';
		hint.style.color = 'var(--warning)';
	} else if (score >= 4) {
		el.classList.add('strong');
		hint.textContent = '密码强度：强';
		hint.style.color = 'var(--success)';
	}
}

/* ============ 修改密码 ============ */
async function changePassword() {
	const oldPwd = $('#oldPwd')?.value;
	const newPwd = $('#newPwd')?.value;
	const confirmPwd = $('#confirmPwd')?.value;

	if (!oldPwd || !newPwd || !confirmPwd) {
		toast('请填写所有密码字段', 'error');
		return;
	}

	if (newPwd !== confirmPwd) {
		toast('两次输入的新密码不一致', 'error');
		return;
	}

	if (newPwd.length < 8) {
		toast('新密码至少需要8位', 'error');
		return;
	}

	try {
		await api.postJson('/api/v1/auth/reset-password/', {
			old_password: oldPwd,
			new_password: newPwd
		});
		toast('密码修改成功，请重新登录', 'success');
		setTimeout(() => {
			localStorage.removeItem('rag_access');
			localStorage.removeItem('rag_refresh');
			window.location.href = '/login/';
		}, 1500);
	} catch (e) {
		toast(e.message || '修改失败', 'error');
		console.error('change password failed:', e);
	}
}

/* ============ 我的权限 / 权限申请 ============ */
const MODULE_LABELS = {
	knowledge: '知识库',
	user: '用户管理',
	audit: '审计',
	system: '系统',
	chat: '对话',
};
const SCOPE_LABELS = { all: '全平台', department: '部门', team: '团队', personal: '个人' };
const APP_STATUS_LABELS = {
	pending: '待审批', approved: '已批准', rejected: '已驳回', withdrawn: '已撤回'
};
const APP_STATUS_COLORS = {
	pending: 'tag-warning', approved: 'tag-success',
	rejected: 'tag-danger', withdrawn: ''
};

async function loadMyPermissions() {
	const rolesBox = document.getElementById('myRolesList');
	const permsBox = document.getElementById('myPermissionsList');
	try {
		const data = await api.getJson('/api/v1/auth/permissions/me/');

		// 渲染角色
		if (rolesBox) {
			if (data.is_super_admin) {
				rolesBox.innerHTML = '<span class="tag tag-danger">👑 超级管理员</span>' +
					(data.roles || []).map(r => `<span class="tag tag-primary">${escapeHtml(r.name)}</span>`).join('');
			} else if ((data.roles || []).length === 0) {
				rolesBox.innerHTML = '<span class="text-sub text-sm">暂无角色</span>';
			} else {
				rolesBox.innerHTML = data.roles.map(r =>
					`<span class="tag ${r.is_builtin ? 'tag-info' : 'tag-primary'}">${escapeHtml(r.name)}</span>`
				).join('');
			}
		}

		// 渲染权限分组
		if (permsBox) {
			const groups = data.permission_groups || {};
			const keys = Object.keys(groups);
			permsBox.innerHTML = '';
			if (keys.length === 0) {
				permsBox.innerHTML = '<span class="text-sub text-sm">暂无显式权限（仅继承默认 read 权限）</span>';
			} else {
				const cardTmpl = document.getElementById('tmpl-perm-module');
				keys.forEach(mod => {
					const items = groups[mod];
					const label = MODULE_LABELS[mod] || mod;
					const clone = cardTmpl.content.cloneNode(true);
					clone.querySelector('.perm-mod-title').textContent = label + ' 模块';
					clone.querySelector('.perm-mod-tags').innerHTML = items.map(it =>
						'<span class="tag tag-sm" style="background:var(--primary-light);color:var(--primary)">' +
						escapeHtml(it.action) + ' · ' + (it.scopes || []).map(s => SCOPE_LABELS[s] || s).join(',') +
						'</span>'
					).join('');
					permsBox.appendChild(clone);
				});
			}
		}
	} catch (e) {
		console.error('load my permissions failed:', e);
		if (rolesBox) rolesBox.innerHTML = '<span class="text-sub text-sm" style="color:var(--danger)">加载失败</span>';
		if (permsBox) permsBox.innerHTML = '<span class="text-sub text-sm" style="color:var(--danger)">加载失败</span>';
	}
}

async function loadPermissionApplications() {
	const box = document.getElementById('myApplicationsList');
	if (!box) return;
	try {
		const data = await api.getJson('/api/v1/auth/permissions/applications/');
		const rows = data.rows || [];
		box.innerHTML = '';
		if (rows.length === 0) {
			box.innerHTML = '<div style="padding:14px;background:var(--hover);border-radius:var(--radius);font-size:13px;color:var(--text-sub);text-align:center">暂无申请记录</div>';
			return;
		}
		const cardTmpl = document.getElementById('tmpl-perm-request');
		rows.forEach(a => {
			const clone = cardTmpl.content.cloneNode(true);

			clone.querySelector('.perm-app-code').innerHTML = '<code style="background:var(--hover);padding:1px 5px;border-radius:3px;font-size:12px">' + escapeHtml(a.permission_code || '—') + '</code>';

			const statusEl = clone.querySelector('.perm-app-status');
			statusEl.textContent = APP_STATUS_LABELS[a.status] || a.status;
			const statusCls = APP_STATUS_COLORS[a.status] || '';
			if (statusCls) statusEl.classList.add(statusCls);

			clone.querySelector('.perm-app-scope').textContent = '范围：' + (SCOPE_LABELS[a.applied_scope] || a.applied_scope) + ' · 审批人：' + escapeHtml(a.approver_name) + ' · ' + formatDate(a.created_at);

			clone.querySelector('.perm-app-reason').textContent = '理由：' + (a.reason || '—');

			if (a.reviewer_comment) {
				const commentEl = clone.querySelector('.perm-app-comment');
				commentEl.textContent = '审批意见：' + escapeHtml(a.reviewer_comment);
				commentEl.style.display = '';
			}

			if (a.status === 'pending') {
				const actionsEl = clone.querySelector('.perm-app-actions');
				actionsEl.innerHTML = '<button class="btn btn-sm" style="color:var(--danger)" onclick="withdrawApplication(' + a.id + ')">撤回申请</button>';
				actionsEl.style.display = '';
			}

			box.appendChild(clone);
		});
	} catch (e) {
		console.error('load applications failed:', e);
		box.innerHTML = '<span class="text-sub text-sm" style="color:var(--danger)">加载失败</span>';
	}
}

function openPermissionApplyModal() {
	document.getElementById('applyPermCode').value = '';
	document.getElementById('applyScope').value = 'team';
	document.getElementById('applyReason').value = '';
	document.getElementById('applyApprover').innerHTML = '<option value="">请选择审批人</option>';
	document.getElementById('approverHint').textContent = '请先选择申请范围';
	const modal = document.getElementById('modal-permission-apply');
	const mask = document.getElementById('mask');
	if (modal) modal.classList.add('show');
	if (mask) mask.classList.add('show');
	// 默认加载 team 范围的审批人
	loadApprovers('team');
}

function closePermissionApplyModal() {
	const modal = document.getElementById('modal-permission-apply');
	const mask = document.getElementById('mask');
	if (modal) modal.classList.remove('show');
	if (mask) mask.classList.remove('show');
}

async function loadApprovers(scope) {
	const select = document.getElementById('applyApprover');
	const hint = document.getElementById('approverHint');
	if (!select) return;
	select.innerHTML = '<option value="">加载中...</option>';
	try {
		const data = await api.getJson(`/api/v1/auth/permissions/approvers/?scope=${scope}`);
		const approvers = data.approvers || [];
		if (approvers.length === 0) {
			select.innerHTML = '<option value="">无可选审批人</option>';
			if (hint) hint.textContent = '当前范围暂无可选审批人，请联系管理员';
			return;
		}
		select.innerHTML = '<option value="">请选择审批人</option>' +
			approvers.map(a => `<option value="${a.id}">${escapeHtml(a.real_name)}（${escapeHtml(a.role_label)}）</option>`).join('');
		if (hint) hint.textContent = `共 ${approvers.length} 位可选审批人`;
	} catch (e) {
		console.error('load approvers failed:', e);
		select.innerHTML = '<option value="">加载失败</option>';
		if (hint) hint.textContent = '加载失败，请重试';
	}
}

async function submitPermissionApplication() {
	const permCode = document.getElementById('applyPermCode').value.trim();
	const scope = document.getElementById('applyScope').value;
	const approverId = document.getElementById('applyApprover').value;
	const reason = document.getElementById('applyReason').value.trim();

	if (!permCode) { toast('请填写权限编码', 'error'); return; }
	if (!approverId) { toast('请选择审批人', 'error'); return; }
	if (!reason) { toast('请填写申请理由', 'error'); return; }

	try {
		await api.postJson('/api/v1/auth/permissions/applications/', {
			permission_code: permCode,
			applied_scope: scope,
			approver_id: parseInt(approverId),
			reason: reason
		});
		toast('申请已提交，等待审批', 'success');
		closePermissionApplyModal();
		loadPermissionApplications();
	} catch (e) {
		console.error('submit application failed:', e);
		toast(e.message || '提交失败', 'error');
	}
}

async function withdrawApplication(id) {
	if (!confirm('确定撤回此申请？')) return;
	try {
		await api.postJson(`/api/v1/auth/permissions/applications/${id}/withdraw/`, {});
		toast('已撤回', 'success');
		loadPermissionApplications();
	} catch (e) {
		toast(e.message || '撤回失败', 'error');
	}
}
