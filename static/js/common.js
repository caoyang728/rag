/* ==========================================================
   知库 Agent · 公共 JS (MPA 版)
   包含：全局状态、工具函数、顶栏/侧栏渲染、auth守卫
   ========================================================== */

/* ============ 全局状态 ============ */
const STATE = {
	user: (() => {
		try {
			const saved = localStorage.getItem('rag_user');
			if (saved) {
				const u = JSON.parse(saved);
				return {
					name: u.real_name || u.username || '用户',
					role: (u.roles && u.roles.length > 0) ? (u.roles[0].role__name || '用户') : '用户',
					dept: u.department_name || '',
					team: '',
					email: u.email || '',
					avatar: (u.real_name || u.username || '?').charAt(0)
				};
			}
		} catch (e) { }
		return { name: '用户', role: '用户', dept: '', team: '', email: '', avatar: '?' };
	})(),
	currentSession: 'sess-001',
	currentAdminMenu: '',
	currentProfileMenu: 'basic',
	currentAuditTab: 'audit',
	currentFeedback: {}
};

/* ============ 工具函数 ============ */
function $(sel, parent) { return (parent || document).querySelector(sel); }
function $$(sel, parent) { return Array.from((parent || document).querySelectorAll(sel)); }
function el(tag, attrs, children) {
	const e = document.createElement(tag);
	if (attrs) for (const k in attrs) {
		if (k === 'class') e.className = attrs[k];
		else if (k === 'style') e.setAttribute('style', attrs[k]);
		else if (k.startsWith('on')) e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
		else if (k === 'html') e.innerHTML = attrs[k];
		else e.setAttribute(k, attrs[k]);
	}
	if (children) {
		(Array.isArray(children) ? children : [children]).forEach(c => {
			if (c == null || c === false) return;
			e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
		});
	}
	return e;
}
function html(strs, ...vals) {
	let s = ''; strs.forEach((str, i) => { s += str; if (i < vals.length) s += (vals[i] == null ? '' : vals[i]); });
	return s;
}
function toast(msg, type) {
	let wrap = $('#toast-wrap');
	if (!wrap) {
		wrap = document.createElement('div');
		wrap.id = 'toast-wrap';
		wrap.className = 'toast-wrap';
		document.body.appendChild(wrap);
	}
	const t = el('div', { class: 'toast ' + (type || '') }, msg);
	const remove = () => { t.style.opacity = '0'; setTimeout(() => t.remove(), 200); };
	t.addEventListener('click', () => { if (t._timer) clearTimeout(t._timer); remove(); });
	if (type === 'success') {
		t._timer = setTimeout(remove, 5000);
	}
	wrap.appendChild(t);
}
function showMask(show) {
	let m = $('#mask');
	if (!m) {
		m = document.createElement('div');
		m.id = 'mask';
		m.className = 'mask';
		document.body.appendChild(m);
	}
	m.classList.toggle('show', !!show);
}
function closeAllOverlays() {
	$$('.modal').forEach(m => m.classList.remove('show'));
	$$('.drawer').forEach(d => d.classList.remove('show'));
	showMask(false);
}

/**
 * 显示模态框
 * @param {string} id - 模态框元素ID
 */
function showModal(id) {
	const m = document.getElementById(id);
	if (m) m.classList.add('show');
	showMask(true);
}

/**
 * 关闭模态框
 * @param {string} id - 模态框元素ID
 */
function closeModal(id) {
	var el = document.getElementById(id);
	if (el) el.classList.remove('show');
	var activeModals = document.querySelectorAll('.modal.show');
	if (activeModals.length === 0) {
		showMask(false);
	}
}

/* ============ 统一 API 请求服务 ============ */
const api = {
	baseUrl: '/api/v1',
	isRefreshing: false,
	refreshSubscribers: [],

	getToken() {
		return localStorage.getItem('rag_access');
	},

	getRefreshToken() {
		return localStorage.getItem('rag_refresh');
	},

	async refreshToken() {
		const refresh = this.getRefreshToken();
		if (!refresh) {
			throw new Error('No refresh token');
		}

		const response = await fetch('/api/v1/auth/token/refresh/', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ refresh })
		});

		if (!response.ok) {
			throw new Error('Refresh failed');
		}

		const data = await response.json();
		localStorage.setItem('rag_access', data.access);
		if (data.refresh) {
			localStorage.setItem('rag_refresh', data.refresh);
		}
		return data.access;
	},

	enqueueRefresh(callback) {
		return new Promise((resolve, reject) => {
			this.refreshSubscribers.push({ resolve, reject, callback });
		});
	},

	async handleRefresh() {
		try {
			const newToken = await this.refreshToken();
			this.refreshSubscribers.forEach(sub => {
				try {
					sub.resolve(newToken);
				} catch (e) {
					sub.reject(e);
				}
			});
		} catch (e) {
			this.refreshSubscribers.forEach(sub => sub.reject(e));
			this.logout();
		} finally {
			this.isRefreshing = false;
			this.refreshSubscribers = [];
		}
	},

	logout() {
		toast('登录已过期，请重新登录', 'error');
		localStorage.removeItem('rag_access');
		localStorage.removeItem('rag_refresh');
		localStorage.removeItem('rag_user');
		setTimeout(() => { window.location.href = '/login/'; }, 1500);
	},

	_formatError(data) {
		// 如果有 details（字段校验错误），转为友好提示
		if (data && data.details && typeof data.details === 'object') {
			const msgs = [];
			for (const [field, errors] of Object.entries(data.details)) {
				const errList = Array.isArray(errors) ? errors : [errors];
				for (const e of errList) {
					const key = `${field}:${e}`;
					// 常见字段错误映射
					const map = {
						'email:具有 email 的 user 已存在。': '该邮箱已被使用',
						'username:具有 username 的 user 已存在。': '该用户名已被使用',
					};
					msgs.push(map[key] || `${field}: ${e}`);
				}
			}
			return msgs.join('；');
		}
		return data ? (data.detail || data.message || '请求失败') : '请求失败';
	},

	async handleError(res) {
		const status = res.status;
		if (status === 403) {
			toast('无权限访问此资源', 'error');
			throw new Error('Forbidden');
		}
		if (!res.ok) {
			let detail = '请求失败';
			try {
				const data = await res.json();
				detail = this._formatError(data);
			} catch (e) { }
			throw new Error(detail);
		}
		return res;
	},

	async fetchWithAuth(method, url, options = {}) {
		const token = this.getToken();
		const headers = {
			'Content-Type': 'application/json',
			...options.headers
		};
		if (token) {
			headers['Authorization'] = `Bearer ${token}`;
		}

		return fetch(url, {
			method: method.toUpperCase(),
			headers,
			body: options.body,
			...options
		});
	},

	async request(method, url, options = {}) {
		let response = await this.fetchWithAuth(method, url, options);

		if (response.status === 401) {
			if (!this.isRefreshing) {
				this.isRefreshing = true;
				this.handleRefresh();
			}

			await new Promise((resolve, reject) => {
				this.refreshSubscribers.push({
					resolve: () => resolve(),
					reject: (err) => reject(err)
				});
			});

			response = await this.fetchWithAuth(method, url, options);
		}

		return this.handleError(response);
	},

	async get(url, options = {}) {
		return this.request('GET', url, options);
	},

	async post(url, data, options = {}) {
		return this.request('POST', url, {
			...options,
			body: typeof data === 'string' ? data : JSON.stringify(data)
		});
	},

	async put(url, data, options = {}) {
		return this.request('PUT', url, {
			...options,
			body: typeof data === 'string' ? data : JSON.stringify(data)
		});
	},

	async patch(url, data, options = {}) {
		return this.request('PATCH', url, {
			...options,
			body: typeof data === 'string' ? data : JSON.stringify(data)
		});
	},

	async delete(url, options = {}) {
		return this.request('DELETE', url, options);
	},

	async stream(url, data, onChunk, options = {}) {
		let token = this.getToken();
		const headers = {
			'Content-Type': 'application/json',
			...options.headers
		};
		if (token) {
			headers['Authorization'] = `Bearer ${token}`;
		}

		let response = await fetch(url, {
			method: 'POST',
			headers,
			body: typeof data === 'string' ? data : JSON.stringify(data),
			...options
		});

		if (response.status === 401) {
			if (!this.isRefreshing) {
				this.isRefreshing = true;
				this.handleRefresh();
			}

			token = await new Promise((resolve, reject) => {
				this.refreshSubscribers.push({
					resolve: (newToken) => resolve(newToken),
					reject: (err) => reject(err)
				});
			});

			headers['Authorization'] = `Bearer ${token}`;
			response = await fetch(url, {
				method: 'POST',
				headers,
				body: typeof data === 'string' ? data : JSON.stringify(data),
				...options
			});
		}

		if (!response.ok) {
			await this.handleError(response);
			return;
		}

		const reader = response.body.getReader();
		const decoder = new TextDecoder('utf-8');
		let buffer = '';

		while (true) {
			const { done, value } = await reader.read();
			if (done) break;

			buffer += decoder.decode(value, { stream: true });
			const lines = buffer.split('\n');
			buffer = lines.pop() || '';

			for (const line of lines) {
				if (line.trim().startsWith('data: ')) {
					const jsonStr = line.slice(6);
					if (jsonStr.trim() === '[DONE]') continue;
					try {
						const chunk = JSON.parse(jsonStr);
						onChunk(chunk);
					} catch (e) {
						console.warn('Failed to parse SSE chunk:', e);
					}
				}
			}
		}
	},

	async getJson(url, options = {}) {
		const res = await this.get(url, options);
		const ct = res.headers.get('content-type') || '';
		if (ct.includes('text/csv')) return res.blob();
		if (res.status === 204) return null;
		return res.json();
	},

	async postJson(url, data, options = {}) {
		const res = await this.post(url, data, options);
		if (res.status === 204) return null;
		return res.json();
	},

	async patchJson(url, data, options = {}) {
		const res = await this.patch(url, data, options);
		if (res.status === 204) return null;
		return res.json();
	},

	async deleteJson(url, options = {}) {
		const res = await this.delete(url, options);
		if (res.status === 204) return null;
		return res.json();
	}
};

/* ============ MPA 页面跳转 ============ */
// 页面 key => 文件名映射
const PAGE_MAP = {
	'chat': '/chat/',
	'upload': '/upload/',
	'admin-users': '/admin-users/',
	'admin-nodes': '/admin-nodes/',
	'admin-analytics': '/admin-analytics/',
	'admin-audit': '/admin-audit/',
	'admin-rbac': '/admin-rbac/',
	'admin-org': '/admin-org/',
	'profile': '/profile/',
	'login': '/login/',
	'reset-password': '/reset-password/'
};
function goto(page) {
	const path = PAGE_MAP[page] || ('/' + page + '/');
	window.location.href = path;
}
window.goto = goto;

/* ============ 布局：顶部导航 ============ */
function renderTopNav(active) {
	return `
  <nav class="topnav">
    <div class="topnav-logo">
      <div class="topnav-logo-icon">知</div>
      <span>知库 Agent</span>
    </div>
    <div class="topnav-search" id="topnavSearchWrap">
      <input type="text" id="globalSearchInput" placeholder="全局搜索：文档、代码、会话…（Ctrl+K）" autocomplete="off">
      <div class="topnav-search-dropdown" id="globalSearchDropdown"></div>
    </div>
    <div id="scopeNavWrap" class="topnav-scope-wrap" style="display:none">
      <button class="topnav-scope-btn" id="scopeTrigger" onclick="toggleScopePicker()">
        📚 知识库范围 · <span id="scopeBadge">已全选</span> ▾
      </button>
    </div>
    <div class="topnav-right">
      <button class="topnav-icon-btn" title="通知" onclick="loadNotifications()">
        <span id="notificationSummary" style="font-size:12px;margin-right:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:80px"></span>🔔<span class="badge-dot"></span>
      </button>
      <div class="dropdown">
        <div class="topnav-user" onclick="toggleUserMenu(event)">
          <div class="avatar avatar-sm">${STATE.user.avatar}</div>
          <span class="topnav-user-name">${STATE.user.name}</span>
          <span style="font-size:10px;color:var(--text-sub)">▼</span>
        </div>
        <div id="userMenu" class="dropdown-menu">
          <div class="dropdown-item" onclick="goto('profile')">👤 我的资料</div>
          <div class="dropdown-item" onclick="toast('设置页占位','')">⚙️ 系统设置</div>
          <div class="dropdown-divider"></div>
          <div class="dropdown-item" onclick="doLogout()">🚪 退出登录</div>
        </div>
      </div>
    </div>
  </nav>`;
}

/* ---- 全局搜索 ---- */
let _globalSearchTimer = null;
function initGlobalSearch() {
	const input = document.getElementById('globalSearchInput');
	if (!input) return;
	const dropdown = document.getElementById('globalSearchDropdown');

	input.addEventListener('input', () => {
		if (_globalSearchTimer) clearTimeout(_globalSearchTimer);
		_globalSearchTimer = setTimeout(() => doGlobalSearch(), 280);
	});
	input.addEventListener('focus', () => {
		if (input.value.trim()) doGlobalSearch();
	});
	input.addEventListener('keydown', (e) => {
		if (e.key === 'Escape') { input.blur(); hideGlobalSearchDropdown(); }
		if (e.key === 'Enter') {
			const first = dropdown?.querySelector('.gs-item');
			if (first) first.click();
		}
	});

	// 点击外部关闭下拉
	document.addEventListener('click', (e) => {
		if (!e.target.closest('#topnavSearchWrap')) hideGlobalSearchDropdown();
	});

	// Ctrl+K 快捷键
	document.addEventListener('keydown', (e) => {
		if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
			e.preventDefault();
			input.focus();
			input.select();
		}
	});
}

async function doGlobalSearch() {
	const input = document.getElementById('globalSearchInput');
	const dropdown = document.getElementById('globalSearchDropdown');
	if (!input || !dropdown) return;
	const q = input.value.trim();
	if (!q) { hideGlobalSearchDropdown(); return; }

	dropdown.innerHTML = '<div class="gs-loading">🔍 搜索中...</div>';
	dropdown.classList.add('show');

	try {
		const data = await api.getJson(`/api/v1/system/search/?q=${encodeURIComponent(q)}`);
		renderGlobalSearchResults(data, q);
	} catch (e) {
		console.error('global search failed:', e);
		dropdown.innerHTML = '<div class="gs-empty">搜索失败，请重试</div>';
	}
}

function renderGlobalSearchResults(data, q) {
	const dropdown = document.getElementById('globalSearchDropdown');
	if (!dropdown) return;
	const groups = data.groups || {};
	const docs = groups.documents || [];
	const sessions = groups.sessions || [];
	const nodes = groups.nodes || [];
	const total = data.total || 0;

	if (total === 0) {
		dropdown.innerHTML = `<div class="gs-empty">无匹配结果："<b>${escapeHtml(q)}</b>"</div>`;
		return;
	}

	const groupHtml = (title, icon, items) => items.length === 0 ? '' : `
    <div class="gs-group">
      <div class="gs-group-title">${icon} ${title} <span class="gs-count">${items.length}</span></div>
      ${items.map(it => `
        <a class="gs-item" href="${it.url}" data-type="${it.type}">
          <span class="gs-item-icon">${it.icon}</span>
          <div class="gs-item-body">
            <div class="gs-item-title">${highlightKeyword(it.title, q)}</div>
            <div class="gs-item-sub">${escapeHtml(it.subtitle || '')}</div>
          </div>
        </a>
      `).join('')}
    </div>`;

	dropdown.innerHTML =
		groupHtml('文档', '📄', docs) +
		groupHtml('会话', '💬', sessions) +
		groupHtml('知识节点', '🗂️', nodes);
}

function hideGlobalSearchDropdown() {
	const dropdown = document.getElementById('globalSearchDropdown');
	if (dropdown) dropdown.classList.remove('show');
}

function highlightKeyword(text, q) {
	if (!text || !q) return escapeHtml(text);
	const escaped = escapeHtml(text);
	const qEscaped = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	return escaped.replace(new RegExp(qEscaped, 'gi'), m => `<mark>${m}</mark>`);
}

/* ============ 模板工具函数 ============ */
function tpl(id) { return document.getElementById(id); }
function htmlFromTpl(id, fillFn) {
	const frag = tpl(id).content.cloneNode(true);
	if (fillFn) fillFn(frag);
	const wrapper = document.createElement('div');
	wrapper.appendChild(frag);
	return wrapper.innerHTML;
}
function escapeHtml(s) {
	if (s == null) return '';
	return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function formatDate(dt) {
	if (!dt) return '-';
	const d = new Date(dt);
	return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function updatePwdStrength(v) {
	const s = $('#pwdStrength'), h = $('#pwdHint');
	if (!s || !h) return;
	s.classList.remove('weak', 'medium', 'strong');
	if (!v) { h.textContent = '密码强度：待输入'; return; }
	let score = 0;
	if (v.length >= 8) score++;
	if (/[A-Z]/.test(v) && /[a-z]/.test(v)) score++;
	if (/\d/.test(v) && /[^\w]/.test(v)) score++;
	if (score === 1) { s.classList.add('weak'); h.textContent = '密码强度：弱（建议添加大小写和特殊字符）'; h.style.color = 'var(--danger)'; }
	else if (score === 2) { s.classList.add('medium'); h.textContent = '密码强度：中'; h.style.color = 'var(--warning)'; }
	else if (score >= 3) { s.classList.add('strong'); h.textContent = '密码强度：强'; h.style.color = 'var(--success)'; }
}
function toggleUserMenu(e) {
	e.stopPropagation();
	const m = $('#userMenu');
	if (m) m.classList.toggle('show');
}
document.addEventListener('click', () => { $$('.dropdown-menu.show').forEach(m => m.classList.remove('show')); });

function doLogout() {
	const refresh = localStorage.getItem('rag_refresh');
	if (refresh) {
		fetch('/api/v1/auth/logout/', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ refresh })
		}).catch(() => { });
	}
	localStorage.removeItem('rag_access');
	localStorage.removeItem('rag_refresh');
	localStorage.removeItem('rag_user');
	toast('已退出登录', 'success');
	setTimeout(() => { window.location.href = '/login/'; }, 600);
}

async function loadNotifications() {
	try {
		const data = await api.getJson('/api/v1/notification/send-logs/');
		const logs = data.rows || [];

		if (logs.length === 0) {
			toast('暂无通知', '');
			return;
		}

		const latest = logs[0];
		const summary = latest.subject ? latest.subject.substring(0, 10) : '新通知';
		const summaryEl = $('#notificationSummary');
		if (summaryEl) {
			summaryEl.textContent = summary;
		}

		let msg = `📮 ${logs.length} 条通知\n`;
		logs.slice(0, 5).forEach(l => {
			msg += `• ${l.subject || '通知'} · ${formatDate(l.created_at)}\n`;
		});
		toast(msg, '');
	} catch (e) {
		console.error('load notifications failed:', e);
		toast('加载通知失败', 'error');
	}
}

/* ============ 布局：侧边导航（管理页） ============ */
function getUserRoles() {
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		return (u.roles || []).map(r => r.role__code);
	} catch (e) { return []; }
}

function hasAnyRole(...codes) {
	const userRoles = getUserRoles();
	return codes.some(c => userRoles.includes(c));
}

function isSuperAdmin() {
	return hasAnyRole('super_admin');
}

function isAdminOrOps() {
	return hasAnyRole('super_admin', 'kb_admin');
}

function renderSidebar(active) {
	// readonly 角色：隐藏文档上传
	const isReadonly = hasAnyRole('readonly');
	// 拥有管理权限的角色可见全部管理后台项
	const isManagerRole = hasAnyRole('super_admin', 'kb_admin', 'kb_ops', 'dept_manager', 'team_leader');

	const adminItems = [];
	// 用户与角色、反馈与报表、审计与安全：仅管理角色可见
	if (isManagerRole) {
		adminItems.push({ icon: '👥', name: '用户与角色', page: 'admin-users', key: 'admin-users' });
	}
	// 知识库：所有登录用户可浏览文档；节点增删改仅管理员可用（页面内控制）
	adminItems.push({ icon: '🗂️', name: '知识库', page: 'admin-nodes', key: 'admin-nodes' });
	if (isManagerRole) {
		adminItems.push(
			{ icon: '📊', name: '反馈与报表', page: 'admin-analytics', key: 'admin-analytics' },
			{ icon: '🛡️', name: '审计与安全', page: 'admin-audit', key: 'admin-audit' },
		);
	}
	// 组织架构 & RBAC 权限配置：仅超级管理员和kb_admin可见
	if (isAdminOrOps()) {
		adminItems.push(
			{ icon: '🏢', name: '组织架构', page: 'admin-org', key: 'admin-org' },
			{ icon: '&#9881;&#65039;', name: 'RBAC 权限配置', page: 'admin-rbac', key: 'admin-rbac' },
		);
	}
	const items = [
		{
			group: '工作台', items: [
				{ icon: '💬', name: '智能聊天', page: 'chat', key: 'chat' },
				...(isReadonly ? [] : [{ icon: '📤', name: '文档上传', page: 'upload', key: 'upload' }])
			]
		},
		{
			group: '个人', items: [
				{ icon: '👤', name: '个人资料', page: 'profile', key: 'profile' },
				{ icon: '⚙️', name: '系统设置', page: null, key: 'system-settings' }
			]
		},
		{ group: '管理后台', items: adminItems }
	];
	return `
  <aside class="sidebar">
    ${items.map(g => `
      <div class="sidebar-group">
        <div class="sidebar-group-title">${g.group}</div>
        ${g.items.map(it => {
		if (it.page) {
			return `
              <a class="sidebar-item ${it.key === active ? 'active' : ''}" href="${PAGE_MAP[it.page]}">
                <span class="sidebar-item-icon">${it.icon}</span>
                <span>${it.name}</span>
              </a>`;
		}
		return `
              <div class="sidebar-item sidebar-item-placeholder" style="cursor:not-allowed;opacity:0.5" title="功能预留，即将上线">
                <span class="sidebar-item-icon">${it.icon}</span>
                <span>${it.name}</span>
                <span style="margin-left:auto;font-size:10px;color:var(--text-sub);padding:2px 8px;border:1px solid #e5e7eb;border-radius:10px">即将上线</span>
              </div>`;
	}).join('')}
      </div>
    `).join('')}
  </aside>`;
}

/* ============ Auth 守卫 ============ */
function authGuard() {
	const token = localStorage.getItem('rag_access');
	const currentPage = (window.location.pathname.replace(/\/$/, '').split('/').pop() || '').toLowerCase();
	const publicPages = ['login', 'reset-password', ''];
	if (!token && !publicPages.includes(currentPage)) {
		window.location.href = '/login/';
	}
}

/* ============ 页面初始化：注入顶栏 + 侧栏 ============ */
document.addEventListener('DOMContentLoaded', () => {
	authGuard();
	// 挂 mask 和 toast 容器（若 HTML 未提供也自动兜底）
	if (!$('#mask')) {
		const m = document.createElement('div');
		m.id = 'mask';
		m.className = 'mask';
		document.body.appendChild(m);
	}
	if (!$('#toast-wrap')) {
		const w = document.createElement('div');
		w.id = 'toast-wrap';
		w.className = 'toast-wrap';
		document.body.appendChild(w);
	}

	const pathPart = window.location.pathname.replace(/\/$/, '').split('/').pop() || '';
	const currentPage = pathPart || 'index';  // 首页 / 时 currentPage = 'index'

	const topnavEl = document.getElementById('topnav-container');
	if (topnavEl) topnavEl.innerHTML = renderTopNav(currentPage);

	const sidebarEl = document.getElementById('sidebar-container');
	if (sidebarEl) sidebarEl.innerHTML = renderSidebar(currentPage);

	// 初始化全局搜索（顶栏）
	initGlobalSearch();
});
