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
		if (btn.type === 'primary') b.className = 'btn-save';
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

	// 关闭按钮 & 点击遮罩关闭
	closeBtn.onclick = () => ctx.close();
	overlay.onclick = (e) => { if (e.target === overlay) ctx.close(); };

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
});
