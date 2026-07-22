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
	if (box) box.innerHTML = renderProfileContent(m);
	// 权限页加载异步数据
	if (m === 'permissions') {
		loadMyPermissions();
		loadPermissionApplications();
	}
}

/* ============ 基本信息 ============ */
function renderBasicTab() {
	const joinedAt = STATE.user.created_at ? formatDate(STATE.user.created_at) : '';
	return `
      <div class="card-title">基本信息</div>
      <div class="flex gap-24 items-center mb-24" style="padding:16px;background:var(--primary-light);border-radius:var(--radius-lg)">
        <div class="avatar avatar-lg" style="background:linear-gradient(135deg,#2563eb,#1e40af)">${escapeHtml(STATE.user.avatar)}</div>
        <div class="flex-1">
          <div class="text-xl">${escapeHtml(STATE.user.name)}</div>
          <div class="text-sub mt-4">${escapeHtml(STATE.user.role)} · ${escapeHtml(STATE.user.dept)} / ${escapeHtml(STATE.user.team)}</div>
          <div class="text-sub text-sm mt-4">用户ID ${STATE.user.id || ''}${joinedAt ? ' · 加入时间 ' + joinedAt : ''}</div>
        </div>
        <button class="btn" onclick="toast('更换头像功能开发中','')">📷 更换头像</button>
      </div>
      <div class="grid-2" style="gap:16px 24px">
        <div class="form-item">
          <label class="form-label">姓名</label>
          <input class="input" id="profileName" value="${escapeHtml(STATE.user.name)}">
        </div>
        <div class="form-item">
          <label class="form-label">企业邮箱</label>
          <input class="input" value="${escapeHtml(STATE.user.email)}" readonly>
          <div class="form-hint">企业邮箱不可修改，如需变更请联系管理员</div>
        </div>
        <div class="form-item">
          <label class="form-label">部门</label>
          <input class="input" value="${escapeHtml(STATE.user.dept)}" disabled>
        </div>
        <div class="form-item">
          <label class="form-label">团队</label>
          <input class="input" value="${escapeHtml(STATE.user.team)}" disabled>
        </div>
        <div class="form-item">
          <label class="form-label">手机号</label>
          <input class="input" id="profilePhone" value="${escapeHtml(STATE.user.phone || '')}" placeholder="请输入手机号">
        </div>
        <div class="form-item">
          <label class="form-label">角色</label>
          <input class="input" value="${escapeHtml(STATE.user.role)}" disabled>
        </div>
      </div>
      <div class="mt-16">
        <button class="btn btn-primary" onclick="saveProfile()">保存修改</button>
      </div>`;
}

/* ============ 我的记忆 ============ */
function renderMemoryTab() {
	const tags = (STATE.memoryTags || []).map(t =>
		`<span class="tag tag-primary tag-closable" onclick="removeMemoryTag(this)" data-tag="${escapeHtml(t)}">${escapeHtml(t)} <span class="tag-close">×</span></span>`
	).join('');

	const searchTypes = (STATE.memorySearchTypes || []).map(t =>
		`<span class="tag tag-info tag-closable" onclick="removeMemorySearchType(this)" data-tag="${escapeHtml(t)}">${escapeHtml(t)} <span class="tag-close">×</span></span>`
	).join('');

	const outputPref = (STATE.memoryPreferences && STATE.memoryPreferences.output_preference) || '';

	const profileLines = STATE.memoryProfileText
		? STATE.memoryProfileText.split('\n').filter(l => l.trim()).map(l => `• ${escapeHtml(l)}`).join('<br>')
		: '暂无自动提炼的偏好数据，系统每 24 小时基于你的行为异步提炼一次';

	return `
      <div class="flex justify-between items-center mb-16">
        <div class="card-title" style="margin:0">🧠 我的记忆（用户永久层）</div>
        <button class="btn btn-danger btn-sm" onclick="clearAllMemory()">🗑 清空记忆</button>
      </div>
      <div style="padding:12px 14px;background:#fef9c3;border-left:3px solid var(--warning);border-radius:var(--radius);margin-bottom:20px;font-size:13px;line-height:1.6">
        💡 <b>四层记忆机制</b>：短时（Redis）→ 会话（PG）→ <b>用户永久（此处）</b> → 全局系统。个人记忆会影响 AI 对你的回答偏好，仅你本人可见。
      </div>
      <div class="form-item">
        <label class="form-label">职业标签</label>
        <div class="mt-8" id="memoryTagsWrap">
          ${tags}
          <button class="btn btn-sm" style="padding:2px 8px" onclick="addMemoryTag()">+ 添加</button>
        </div>
        <div class="form-hint">AI 会结合职业标签给出更专业的回答</div>
      </div>
      <div class="form-item">
        <label class="form-label">常用检索类型</label>
        <div class="mt-8" id="memorySearchTypesWrap">
          ${searchTypes}
          <button class="btn btn-sm" style="padding:2px 8px" onclick="addMemorySearchType()">+ 添加</button>
        </div>
      </div>
      <div class="form-item">
        <label class="form-label">输出偏好</label>
        <textarea class="textarea" id="memoryOutputPref" style="min-height:100px">${escapeHtml(outputPref)}</textarea>
        <div class="form-hint">用自然语言描述你希望 AI 如何回答问题</div>
      </div>
      <div class="form-item">
        <label class="form-label">已提炼的偏好（系统自动生成）</label>
        <div style="background:var(--hover);padding:12px 14px;border-radius:var(--radius);font-size:13px;line-height:1.7;color:var(--text-sub)" id="profileTextDisplay">
          ${profileLines}
        </div>
        <div class="form-hint">系统每 24 小时基于你的行为异步提炼一次</div>
      </div>
      <div class="mt-16">
        <button class="btn btn-primary" onclick="saveMemory()">保存记忆</button>
      </div>`;
}

/* ============ 权限（已有实现保留不变） ============ */
function renderPermissionsTab() {
	return `
      <div class="flex justify-between items-center mb-16">
        <div class="card-title" style="margin:0">🔑 我的权限</div>
        <button class="btn btn-primary btn-sm" onclick="openPermissionApplyModal()">＋ 申请权限</button>
      </div>
      <div style="padding:12px 14px;background:var(--primary-light);border-radius:var(--radius);margin-bottom:16px;font-size:13px;line-height:1.7">
        💡 <b>权限说明</b><br>
        • 当前你拥有的权限由所分配的角色决定（RBAC）<br>
        • 如需更高或额外权限，请提交申请并选择对应的审批人<br>
        • 团队级 → 团队负责人 / 部门经理；部门级 → 部门经理 / 知识库运维；全平台 → 知识库运维 / 超级管理员
      </div>
      <div class="card-title" style="font-size:14px;margin-bottom:8px">已分配角色</div>
      <div id="myRolesList" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px">
        <span class="text-sub text-sm">加载中...</span>
      </div>
      <div class="card-title" style="font-size:14px;margin-bottom:8px">权限明细（按模块分组）</div>
      <div id="myPermissionsList" style="display:flex;flex-direction:column;gap:12px;margin-bottom:20px">
        <span class="text-sub text-sm">加载中...</span>
      </div>
      <div class="card-title" style="font-size:14px;margin-bottom:8px">我的申请记录</div>
      <div id="myApplicationsList" style="display:flex;flex-direction:column;gap:8px">
        <span class="text-sub text-sm">加载中...</span>
      </div>`;
}

/* ============ 邮件订阅 ============ */
function renderEmailTab() {
	const subs = STATE.subscriptions || {};
	const items = [
		{ key: 'node_update', icon: '📁', title: '订阅知识库节点更新', desc: '当你关注的节点有新文档上传时，每天汇总一次发送邮件' },
		{ key: 'system_notice', icon: '🚨', title: '系统告警通知', desc: '上传失败、账号异常登录、权限变更等重要事件即时告警' },
		{ key: 'daily_report', icon: '📊', title: '每周报表推送', desc: '每周一 09:00 推送上周问答统计、满意率、热门问题' },
		{ key: 'keyword_alert', icon: '🔍', title: '关键词命中通知', desc: '当有新文档包含你关注的关键词时立即通知' },
	];

	const checkboxes = items.map(item => {
		const checked = subs[item.key] && subs[item.key].is_enabled ? 'checked' : '';
		return `
        <label class="checkbox" style="padding:14px;border:1px solid var(--border);border-radius:var(--radius);align-items:flex-start">
          <input type="checkbox" ${checked} style="margin-top:2px" data-subkey="${item.key}">
          <div>
            <div class="fw-500">${item.icon} ${item.title}</div>
            <div class="text-sub text-sm mt-4">${item.desc}</div>
          </div>
        </label>`;
	}).join('');

	return `
      <div class="card-title">📧 邮件订阅偏好</div>
      <div style="padding:12px 14px;background:var(--primary-light);border-radius:var(--radius);margin-bottom:20px;font-size:13px;line-height:1.6">
        📬 订阅内容将发送至 <b>${escapeHtml(STATE.user.email)}</b>，可随时取消订阅
      </div>
      <div style="display:flex;flex-direction:column;gap:14px">
        ${checkboxes}
      </div>
      <div class="mt-16">
        <button class="btn btn-primary" onclick="saveSubscriptions()">保存偏好</button>
      </div>`;
}

/* ============ 修改密码 ============ */
function renderPwdTab() {
	return `
      <div class="card-title">🔐 修改密码</div>
      <div style="max-width:480px">
        <div class="form-item">
          <label class="form-label">当前密码</label>
          <input class="input" type="password" id="oldPwd" placeholder="请输入当前密码">
        </div>
        <div class="form-item">
          <label class="form-label">新密码</label>
          <input class="input" type="password" id="newPwd" placeholder="至少 8 位，包含大小写字母和数字" oninput="updatePwdStrength(this.value)">
          <div class="password-strength" id="pwdStrength"><div class="bar"></div><div class="bar"></div><div class="bar"></div></div>
          <div class="password-hint" id="pwdHint">密码强度：待输入</div>
        </div>
        <div class="form-item">
          <label class="form-label">确认新密码</label>
          <input class="input" type="password" id="confirmPwd" placeholder="再次输入新密码">
        </div>
        <div style="padding:12px;background:var(--hover);border-radius:var(--radius);font-size:12px;line-height:1.7;color:var(--text-sub)">
          <b>密码安全要求：</b><br>
          • 至少 8 位，最多 32 位<br>
          • 必须包含大写字母、小写字母和数字<br>
          • 建议包含特殊字符（! @ # $ % ^ &amp; *）<br>
          • 不能与旧密码相同<br>
          • 修改后需重新登录
        </div>
        <div class="mt-16">
          <button class="btn btn-primary" onclick="changePassword()">确认修改</button>
        </div>
      </div>`;
}

function renderProfileContent(m) {
	if (m === 'basic') return renderBasicTab();
	if (m === 'memory') return renderMemoryTab();
	if (m === 'permissions') return renderPermissionsTab();
	if (m === 'email') return renderEmailTab();
	if (m === 'pwd') return renderPwdTab();
	return '';
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
			if (keys.length === 0) {
				permsBox.innerHTML = '<span class="text-sub text-sm">暂无显式权限（仅继承默认 read 权限）</span>';
			} else {
				permsBox.innerHTML = keys.map(mod => {
					const items = groups[mod];
					const label = MODULE_LABELS[mod] || mod;
					return `
            <div style="padding:12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--white)">
              <div style="font-weight:600;margin-bottom:8px;font-size:13px">${escapeHtml(label)} 模块</div>
              <div style="display:flex;flex-wrap:wrap;gap:6px">
                ${items.map(it => `<span class="tag tag-sm" style="background:var(--primary-light);color:var(--primary)">${escapeHtml(it.action)} · ${(it.scopes || []).map(s => SCOPE_LABELS[s] || s).join(',')}</span>`).join('')}
              </div>
            </div>`;
				}).join('');
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
		if (rows.length === 0) {
			box.innerHTML = '<div style="padding:14px;background:var(--hover);border-radius:var(--radius);font-size:13px;color:var(--text-sub);text-align:center">暂无申请记录</div>';
			return;
		}
		box.innerHTML = rows.map(a => `
      <div style="padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--white)">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
          <div style="font-weight:500;font-size:13px"><code style="background:var(--hover);padding:1px 5px;border-radius:3px;font-size:12px">${escapeHtml(a.permission_code || '—')}</code></div>
          <span class="tag tag-sm ${APP_STATUS_COLORS[a.status] || ''}">${APP_STATUS_LABELS[a.status] || a.status}</span>
        </div>
        <div class="text-sub text-sm" style="margin-bottom:4px">
          范围：${SCOPE_LABELS[a.applied_scope] || a.applied_scope} · 审批人：${escapeHtml(a.approver_name)} · ${formatDate(a.created_at)}
        </div>
        <div class="text-sub text-sm">理由：${escapeHtml(a.reason || '—')}</div>
        ${a.reviewer_comment ? `<div class="text-sub text-sm" style="margin-top:4px;color:var(--primary)">审批意见：${escapeHtml(a.reviewer_comment)}</div>` : ''}
        ${a.status === 'pending' ? `<div style="margin-top:6px"><button class="btn btn-sm" style="color:var(--danger)" onclick="withdrawApplication(${a.id})">撤回申请</button></div>` : ''}
      </div>
    `).join('');
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
