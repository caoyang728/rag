/* ============ 个人中心 ============ */

document.addEventListener('DOMContentLoaded', () => {
	initProfilePage();
});

async function initProfilePage() {
	// 三个接口互不依赖,并行加载减少首屏等待
	await Promise.all([
		loadProfile(),
		loadMemoryData(),
		loadSubscriptionData(),
	]);
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
				deptId: data.department_id || null,
				team: data.team ? data.team.name : '',
				teamId: data.team ? data.team.id : null,
				role: (data.roles && data.roles.length > 0) ? (data.roles[0].name || '用户') : '用户',
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
	if (roleDeptEl) roleDeptEl.textContent = escapeHtml(u.dept) + ' / ' + escapeHtml(u.team);

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
		// 同步更新页面显示的名字和头像(避免改名后仍显示旧值)
		const nameEl = $('#pf-name');
		if (nameEl) nameEl.textContent = STATE.user.name;
		const avatarEl = $('#pf-avatar');
		if (avatarEl) avatarEl.textContent = STATE.user.name.charAt(0);
		const nameInput = $('#profileName');
		if (nameInput) nameInput.value = STATE.user.name;
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
			// 无论是否为空都提交,确保用户能清空输出偏好(否则 PATCH 不带该字段,后端保留旧值)
			output_preference: outputPref,
		};
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
		STATE.memoryProfileText = '';
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
	// 去重检查:避免添加重复标签
	const existing = getCurrentMemoryTags();
	if (existing.includes(tag)) { toast('该标签已存在', 'error'); return; }
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
	// 去重检查:避免添加重复检索类型
	const existing = getCurrentMemorySearchTypes();
	if (existing.includes(type)) { toast('该检索类型已存在', 'error'); return; }
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
		console.error('save subscriptions failed:', e);
	}
}

/* ============ 修改密码 ============ */
/* 注:updatePwdStrength 复用 common.js 中的全局实现,此处不重复定义 */
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

	// 新密码不能与旧密码相同(与 profile.html 密码安全要求一致)
	if (newPwd === oldPwd) {
		toast('新密码不能与旧密码相同', 'error');
		return;
	}

	if (newPwd.length < 8) {
		toast('新密码至少需要8位', 'error');
		return;
	}
	if (newPwd.length > 32) {
		toast('新密码最多32位', 'error');
		return;
	}
	// 前端密码规则校验(与 profile.html 密码安全要求一致):必须包含大小写字母和数字
	if (!/[A-Z]/.test(newPwd) || !/[a-z]/.test(newPwd) || !/\d/.test(newPwd)) {
		toast('密码必须包含大写字母、小写字母和数字', 'error');
		return;
	}

	try {
		await api.postJson('/api/v1/auth/reset-password/', {
			old_password: oldPwd,
			new_password: newPwd
		});
		toast('密码修改成功，请重新登录', 'success');
		setTimeout(() => {
			// 清除全部登录态(与 doLogout 对齐,避免残留过期的 rag_user)
			localStorage.removeItem('rag_access');
			localStorage.removeItem('rag_refresh');
			localStorage.removeItem('rag_user');
			window.location.href = '/login/';
		}, 1500);
	} catch (e) {
		toast(e.message || '修改失败', 'error');
		console.error('change password failed:', e);
	}
}

/* ============ 我的权限 ============ */
// 模块标签(权限点分组展示用)
const MODULE_LABELS = {
	kb: '知识库', user: '用户管理', audit: '审计', system: '系统',
	chat: '对话', org: '组织架构', compliance: '合规',
};
// 工单状态标签(对齐 PermissionApprovalTicket.TicketStatus)
const TICKET_STATUS_LABELS = {
	PENDING: '待审批', APPROVED: '已通过', REJECTED: '已驳回',
	CANCELLED: '已撤回', EXECUTED: '已执行',
};
const TICKET_STATUS_COLORS = {
	PENDING: 'tag-warning', APPROVED: 'tag-success', REJECTED: 'tag-danger',
	CANCELLED: '', EXECUTED: 'tag-info',
};
// 变更类型标签(对齐 TicketChangeType)
const CHANGE_TYPE_LABELS = {
	GRANT: '授权', REVOKE: '撤销', ROLE_CHANGE: '角色变更',
	SCOPE_CHANGE: '范围变更', EXPIRE_EXTEND: '延期',
};
// scope 类型标签(对齐 ScopeType)
const SCOPE_TYPE_LABELS = { TEAM: '团队', DEPT: '部门', GLOBAL: '全局', NONE: '全局' };

async function loadMyPermissions() {
	const rolesBox = document.getElementById('myRolesList');
	const permsBox = document.getElementById('myPermissionsList');
	try {
		const data = await api.getJson('/api/v1/auth/permissions/me/');

		// 渲染角色(含 scope 信息:团队/部门属地授权带 scope_name)
		if (rolesBox) {
			const isSuperAdminUser = data.is_super_admin ||
				(data.roles || []).some(r => r.code === 'super_admin');
			const roles = data.roles || [];
			if (isSuperAdminUser) {
				rolesBox.innerHTML = '<span class="tag tag-danger">👑 超级管理员</span>' +
					roles.filter(r => r.code !== 'super_admin').map(r =>
						_renderRoleTag(r)
					).join('');
			} else if (roles.length === 0) {
				rolesBox.innerHTML = '<span class="text-sub text-sm">暂无角色</span>';
			} else {
				rolesBox.innerHTML = roles.map(r => _renderRoleTag(r)).join('');
			}
		}

		// 渲染权限分组(使用后端返回的 label)
		if (permsBox) {
			const groups = data.permission_groups || {};
			const keys = Object.keys(groups);
			permsBox.innerHTML = '';
			if (keys.length === 0) {
				permsBox.innerHTML = '<span class="text-sub text-sm">暂无显式权限(仅继承默认 read 权限)</span>';
			} else {
				const cardTmpl = document.getElementById('tmpl-perm-module');
				keys.forEach(mod => {
					const items = groups[mod];
					const label = MODULE_LABELS[mod] || mod;
					const clone = cardTmpl.content.cloneNode(true);
					clone.querySelector('.perm-mod-title').textContent = label + ' 模块';
					clone.querySelector('.perm-mod-tags').innerHTML = items.map(it =>
						'<span class="tag tag-sm" style="background:var(--primary-light);color:var(--primary)" title="' + escapeHtml(it.code) + '">' +
						escapeHtml(it.label || it.action || it.code) +
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

// 渲染角色标签(带 scope 信息:团队/部门角色显示所属组织)
function _renderRoleTag(r) {
	const cls = r.is_builtin ? 'tag-info' : 'tag-primary';
	let scopeTxt = '';
	if (r.scope_type === 'TEAM' && r.scope_name) scopeTxt = ` @ ${escapeHtml(r.scope_name)}`;
	else if (r.scope_type === 'DEPT' && r.scope_name) scopeTxt = ` @ ${escapeHtml(r.scope_name)}`;
	return `<span class="tag ${cls}">${escapeHtml(r.name)}${scopeTxt}</span>`;
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

			// 角色名 + 变更类型徽章
			const roleTxt = a.previous_role_name
				? `${escapeHtml(a.previous_role_name)} → ${escapeHtml(a.role_name || '—')}`
				: escapeHtml(a.role_name || '—');
			const changeBadge = `<span class="tag tag-sm" style="background:var(--hover);color:var(--text-sub)">${CHANGE_TYPE_LABELS[a.change_type] || a.change_type}</span>`;
			clone.querySelector('.perm-app-role').innerHTML = roleTxt + ' ' + changeBadge;

			// 状态标签
			const statusEl = clone.querySelector('.perm-app-status');
			statusEl.textContent = TICKET_STATUS_LABELS[a.status] || a.status;
			const statusCls = TICKET_STATUS_COLORS[a.status] || '';
			if (statusCls) statusEl.classList.add(statusCls);

			// 元信息:scope + 审批进度 + 审批人 + 时间
			// 全局角色后端返回 scope_name='全局',与 scope 标签重复,此处去重
			const scopeLabel = SCOPE_TYPE_LABELS[a.scope_type] || '';
			const scopeTxt = (a.scope_name && a.scope_name !== scopeLabel)
				? `${scopeLabel} · ${escapeHtml(a.scope_name)}`
				: scopeLabel;
			const stepTxt = a.total_steps > 0 ? ` · 进度 ${a.current_step + 1}/${a.total_steps}` : '';
			const approverTxt = a.approver_name ? ` · 审批人 ${escapeHtml(a.approver_name)}` : '';
			clone.querySelector('.perm-app-meta').textContent = `${scopeTxt}${stepTxt}${approverTxt} · ${formatDate(a.created_at)}`;

			clone.querySelector('.perm-app-reason').textContent = '理由:' + (a.reason || '—');

			if (a.reviewer_comment) {
				const commentEl = clone.querySelector('.perm-app-comment');
				commentEl.textContent = '审批意见:' + escapeHtml(a.reviewer_comment);
				commentEl.style.display = '';
			}

			// 待审批状态可撤回
			if (a.status === 'PENDING') {
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

/* ============ 撤回申请记录 ============ */
async function withdrawApplication(id) {
	if (!confirm('确定撤回此申请?')) return;
	try {
		await api.postJson(`/api/v1/auth/permissions/applications/${id}/withdraw/`, {});
		toast('已撤回', 'success');
		loadPermissionApplications();
	} catch (e) {
		toast(e.message || '撤回失败', 'error');
		console.error('withdraw application failed:', e);
	}
}
