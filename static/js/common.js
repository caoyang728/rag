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
					role: (u.roles && u.roles.length > 0) ? (u.roles[0].name || '用户') : '用户',
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
	// 自动消除时长：成功 3s、警告 6s、错误/失败不自动消除（需手动点击）
	const durations = { success: 3000, warning: 6000, info: 3000 };
	const duration = durations[type];
	if (duration) {
		t._timer = setTimeout(remove, duration);
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
	$$('.confirm-overlay').forEach(o => o.classList.remove('show'));
	showMask(false);
}

/* ============ 二次确认弹窗(带模糊背景,层级高于普通弹窗) ============ */
/**
 * 显示二次确认弹窗
 * 在普通弹窗之上叠加一层模糊背景 + 确认弹窗,用于通过/驳回等关键操作的二次确认。
 *
 * @param {Object} opts
 *   - title: string                 弹窗标题
 *   - bannerType: string           'success'|'danger'|'info'(默认 info)
 *   - bannerIcon: string           横幅图标文字(如 '✓' '⚠')
 *   - bannerText: string           横幅提示文字
 *   - bodyHtml: string             自定义 body 内容(如备注 textarea)
 *   - buttons: Array               底部按钮 [{text, type:'cancel'|'primary'|'danger', onClick: (ctx)=>void}]
 *   - onShow: (ctx)=>void          弹窗显示后的回调(如聚焦输入框)
 * @returns {Object} ctx            上下文 { el, close, setError }
 *   - el: HTMLElement              弹窗根元素(可 querySelector 获取 body 内的输入框)
 *   - close(): void                关闭弹窗
 *   - setError(msg): void          在 body 底部显示错误提示
 */
function showConfirmDialog(opts) {
	opts = opts || {};
	// 懒初始化:首次调用时创建 overlay + dialog 骨架
	let overlay = document.getElementById('confirmOverlay');
	if (!overlay) {
		overlay = document.createElement('div');
		overlay.id = 'confirmOverlay';
		overlay.className = 'confirm-overlay';
		overlay.innerHTML =
			'<div class="confirm-dialog">' +
			'  <div class="modal-header">' +
			'    <div class="modal-title" id="confirmDialogTitle"></div>' +
			'    <button class="modal-close" id="confirmDialogClose">&times;</button>' +
			'  </div>' +
			'  <div class="modal-body" id="confirmDialogBody"></div>' +
			'  <div class="modal-footer" id="confirmDialogFooter"></div>' +
			'</div>';
		document.body.appendChild(overlay);
	}

	const dialog = overlay.querySelector('.confirm-dialog');
	const titleEl = overlay.querySelector('#confirmDialogTitle');
	const bodyEl = overlay.querySelector('#confirmDialogBody');
	const footerEl = overlay.querySelector('#confirmDialogFooter');
	const closeBtn = overlay.querySelector('#confirmDialogClose');

	// 填充标题
	titleEl.textContent = opts.title || '确认操作';

	// 填充 body:可选横幅 + 自定义内容 + 错误提示容器
	let bodyHtml = '';
	if (opts.bannerText) {
		const bType = opts.bannerType || 'info';
		const bIcon = opts.bannerIcon || (bType === 'success' ? '✓' : bType === 'danger' ? '⚠' : 'i');
		bodyHtml += '<div class="confirm-banner confirm-banner-' + bType + '">' +
			'<span class="confirm-banner-icon">' + bIcon + '</span>' +
			'<span>' + opts.bannerText + '</span>' +
			'</div>';
	}
	if (opts.bodyHtml) bodyHtml += opts.bodyHtml;
	bodyHtml += '<div class="field-error hidden" id="confirmDialogErr"></div>';
	bodyEl.innerHTML = bodyHtml;

	// 填充底部按钮
	footerEl.innerHTML = '';
	const buttons = opts.buttons || [{ text: '确认', type: 'primary' }];
	buttons.forEach(btn => {
		const b = document.createElement('button');
		b.textContent = btn.text;
		if (btn.className) {
			b.className = btn.className;
		} else if (btn.type === 'primary') b.className = 'btn-save';
		else if (btn.type === 'danger') b.className = 'btn btn-reject';
		else b.className = 'btn-cancel';
		b.onclick = () => { if (typeof btn.onClick === 'function') btn.onClick(ctx); };
		footerEl.appendChild(b);
	});

	// 上下文对象:供按钮回调操作弹窗
	const ctx = {
		el: dialog,
		close() { overlay.classList.remove('show'); },
		setError(msg) {
			const errEl = bodyEl.querySelector('#confirmDialogErr');
			if (errEl) {
				errEl.textContent = msg;
				errEl.classList.toggle('hidden', !msg);
			}
		}
	};

	// 关闭按钮关闭；不允许点击背景遮罩关闭（避免误触丢失输入内容）
	closeBtn.onclick = () => ctx.close();

	overlay.classList.add('show');
	if (typeof opts.onShow === 'function') opts.onShow(ctx);
	return ctx;
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

/* 统一 API 请求服务已迁至 api.js（内部 logout 依赖 toast，需在 common.js 之后加载） */

/* ============ MPA 页面跳转 ============ */
// 页面 key => 文件名映射
const PAGE_MAP = {
	'chat': '/chat/',
	'upload': '/upload/',
	'admin-users': '/admin-users/',
	'admin-nodes': '/admin-nodes/',
	'admin-approvals': '/admin-approvals/',
	'admin-docs': '/admin-docs/',
	'admin-analytics': '/admin-analytics/',
	'admin-eval': '/admin-eval/',
	'admin-audit': '/admin-audit/',
	'admin-rbac': '/admin-rbac/',
	'admin-org': '/admin-org/',
	'admin-system-config': '/admin-system-config/',
	'admin-scheduler': '/admin-scheduler/',
	'wiki': '/wiki/',
	'graph': '/graph/',
	'profile': '/profile/',
	'login': '/login/',
	'reset-password': '/reset-password/'
};
function goto(page) {
	const path = PAGE_MAP[page] || ('/' + page + '/');
	window.location.href = path;
}
window.goto = goto;

/* 顶栏渲染与全局搜索已迁至 layout.js（带壳页面引入，依赖 common.js + api.js） */

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
function _errMsg(err, fallback) {
	try {
		if (typeof err === 'string') return err;
		if (err?.detail) return err.detail;
		if (err?.message) return err.message;
		if (err?.error) return err.error;
	} catch (_) {}
	return fallback;
}
function formatDate(dt) {
	if (!dt) return '-';
	const d = new Date(dt);
	return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function formatFileSize(bytes) {
	if (bytes == null || isNaN(bytes)) return '-';
	if (bytes < 1024) return bytes + ' B';
	if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
	if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
	return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
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
/* 用户菜单/登出/通知/角色判断/侧栏渲染已迁至 layout.js */

/* ============ 组织架构筛选公共组件 ============ */
/* 部门/团队级联下拉,多页面复用(admin-eval / admin-users / admin-nodes 等)。
 * 用法:
 *   HTML:  <select id="xxxDept"></select>
 *          <select id="xxxTeam" onchange="loadXxx()"></select>
 *   JS:    await OrgFilter.init('xxxDept', 'xxxTeam', () => loadXxx());
 *          // 取值: OrgFilter.getDeptId('xxxDept'), OrgFilter.getTeamId('xxxTeam')
 *          // 描述: OrgFilter.describeScope(deptId, teamId)  → "技术部 / 前端组"
 *          // 注意: 部门下拉的 change 事件由 init() 内部绑定,HTML 无需写 onchange。
 *          //       团队下拉的 change 仍由 HTML onchange 触发(调各页面自己的 load 函数)。
 */
const OrgFilter = (function () {
	// 全局缓存:所有页面共享同一份数据,只拉取一次
	// depts: [{id,name,sort_order,...}], teams: [{id,name,department_id,...}]
	// deptMap/teamMap: {id: obj}, teamsByDept: {dept_id: [team...]}
	let cache = { loaded: false, depts: [], teams: [], deptMap: {}, teamMap: {}, teamsByDept: {} };
	// Promise 缓存:并发 init() 时复用同一个加载 Promise,避免重复发请求
	let loadPromise = null;

	/** 从后端拉取部门+团队列表(只执行一次,后续调用直接返回缓存)。
	 *  部门 API: /api/v1/auth/departments/?page_size=100
	 *  团队 API: /api/v1/auth/teams/?page_size=100
	 *  失败时标记 loaded 避免重复报错,下拉保持"全部"默认项。
	 */
	async function load() {
		if (cache.loaded) return;
		if (loadPromise) return loadPromise; // 复用加载中的 Promise,去重并发
		loadPromise = (async () => {
			try {
				const [deptResp, teamResp] = await Promise.all([
					api.getJson('/api/v1/auth/departments/?page_size=100'),
					api.getJson('/api/v1/auth/teams/?page_size=100'),
				]);
				// 兼容 DRF 分页 {results:[...]} 和裸数组两种返回格式
				const depts = Array.isArray(deptResp) ? deptResp : (deptResp.results || []);
				const teams = Array.isArray(teamResp) ? teamResp : (teamResp.results || []);
				cache.depts = depts.sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0) || String(a.name).localeCompare(String(b.name)));
				cache.teams = teams.sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0) || String(a.name).localeCompare(String(b.name)));
				cache.deptMap = Object.fromEntries(cache.depts.map(d => [d.id, d]));
				cache.teamMap = Object.fromEntries(cache.teams.map(t => [t.id, t]));
				// 按部门分组团队,department_id 为 null 的归入 "__orphan__"
				cache.teamsByDept = {};
				for (const t of cache.teams) {
					const key = t.department_id ?? '__orphan__';
					(cache.teamsByDept[key] ||= []).push(t);
				}
			} catch (e) {
				console.warn('[OrgFilter] 加载组织架构失败,将降级为"全部":', e);
			} finally {
				cache.loaded = true;
				loadPromise = null; // 本轮加载结束,下次若需重试需手动清缓存
			}
		})();
		return loadPromise;
	}

	/** 填充单个部门下拉的 option 列表(数据就绪后调用)。
	 *  固定首项 "全部部门"(value=""),后续按 sort_order + name 排序。
	 */
	function _fillDeptSelect(selEl) {
		if (!selEl) return;
		const opts = ['<option value="">全部部门</option>',
			...cache.depts.map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`),
		].join('');
		selEl.innerHTML = opts;
	}

	/** 根据选中的 deptId 刷新团队下拉。
	 *  deptId 为空 → 只保留"全部团队"(无法选具体团队,与"全部部门"对应)。
	 *  deptId 有值 → "全部团队" + 该部门下所有团队。
	 *  刷新后尽量保持原选中值,若不在新列表中则回退到""。
	 */
	function _fillTeamSelect(selEl, deptId) {
		if (!selEl) return;
		const prev = selEl.value;
		let opts = ['<option value="">全部团队</option>'];
		if (deptId) {
			const teams = cache.teamsByDept[Number(deptId)] || [];
			opts.push(...teams.map(t => `<option value="${t.id}">${escapeHtml(t.name)}</option>`));
		}
		selEl.innerHTML = opts.join('');
		selEl.value = prev && Array.from(selEl.options).some(o => o.value === prev) ? prev : '';
	}

	/** 初始化一对部门/团队级联下拉(可多次调用,每对独立管理)。
	 * @param {string} deptSelId  部门 <select> 的 DOM ID
	 * @param {string} teamSelId  团队 <select> 的 DOM ID
	 * @param {function} [onTeamChange]  团队下拉变更时的回调(通常触发数据加载)
	 * @returns {Promise<void>}
	 * 部门下拉变更时会自动刷新团队下拉并调用 onTeamChange。
	 */
	async function init(deptSelId, teamSelId, onTeamChange) {
		await load();
		const deptEl = document.getElementById(deptSelId);
		const teamEl = document.getElementById(teamSelId);
		if (!deptEl || !teamEl) return;

		// 填充部门下拉,绑定 change 事件(刷新团队 + 触发回调)
		_fillDeptSelect(deptEl);
		deptEl.onchange = () => {
			_fillTeamSelect(teamEl, deptEl.value);
			if (onTeamChange) onTeamChange();
		};
		// 初始填充团队下拉(默认"全部部门" → 只有"全部团队")
		_fillTeamSelect(teamEl, deptEl.value);
	}

	/** 获取部门下拉当前选中值(空字符串表示"全部") */
	function getDeptId(deptSelId) {
		const el = document.getElementById(deptSelId);
		return el ? el.value : '';
	}

	/** 获取团队下拉当前选中值(空字符串表示"全部") */
	function getTeamId(teamSelId) {
		const el = document.getElementById(teamSelId);
		return el ? el.value : '';
	}

	/** 将 dept_id / team_id 转为人类可读的范围描述。
	 *  teamId 优先: "部门名 / 团队名"
	 *  仅 deptId:  "部门名"
	 *  都为空:     "全部"
	 */
	function describeScope(deptId, teamId) {
		const tId = Number(teamId);
		const dId = Number(deptId);
		if (teamId && cache.teamMap && cache.teamMap[tId]) {
			const t = cache.teamMap[tId];
			const d = cache.deptMap[t.department_id];
			return `${d ? escapeHtml(d.name) + ' / ' : ''}${escapeHtml(t.name)}`;
		}
		if (deptId && cache.deptMap[dId]) {
			return escapeHtml(cache.deptMap[dId].name);
		}
		return '全部';
	}

	return { init, load, getDeptId, getTeamId, describeScope };
})();

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
	// favicon：统一注入（幂等），避免浏览器请求 /favicon.ico 产生 404 告警
	if (!document.querySelector('link[rel="icon"]')) {
		const icon = document.createElement('link');
		icon.rel = 'icon';
		icon.type = 'image/svg+xml';
		icon.href = '/static/favicon.svg';
		document.head.appendChild(icon);
	}
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
});
