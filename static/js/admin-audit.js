/* ============ 审计与安全 ============ */

let auditFilter = {
	username: '',
	action: '',
	ip: '',
	startDate: '',
	endDate: '',
	page: 1
};
let _auditDetailCache = [];
let _whitelistCache = [];
let _sensitiveCache = [];      // 敏感词列表缓存，供编辑按钮按 idx 取记录
let _sensitiveEditId = null;   // 当前编辑的敏感词 id（null 表示新增模式）
let _auditPgnData = null;      // 审计日志分页数据（供 Pagination 组件使用）
const _TAB_PAGE_SIZE = 20;     // 各 tab 统一每页条数
let _whitePgnData = null;      // IP 白名单分页数据
let _blackPgnData = null;      // IP 黑名单分页数据
let _sensitivePgnData = null;  // 敏感词分页数据
let _loginPgnData = null;      // 登录记录分页数据
let _whitePage = 1;            // 白名单当前页码
let _blackPage = 1;            // 黑名单当前页码
let _sensitivePage = 1;        // 敏感词当前页码
let _loginPage = 1;            // 登录记录当前页码
// 登录尝试筛选条件
let _loginFilter = {
	username: '',
	ip: '',
	result: ''
};
let _auditReqSeq = 0;   // 审计日志翻页请求序号守卫：快速连续翻页时丢弃旧响应
let _loginReqSeq = 0;   // 登录记录翻页请求序号守卫：快速连续翻页时丢弃旧响应

document.addEventListener('DOMContentLoaded', () => {
	initAuditPage();
});

async function initAuditPage() {
	await setAuditTab(STATE.currentAuditTab || 'audit');
}

async function setAuditTab(tab) {
	STATE.currentAuditTab = tab;
	$$('.tab-item').forEach((t, i) => {
		t.classList.toggle('active', ['audit', 'white', 'black', 'sensitive', 'login'][i] === tab);
	});
	const body = $('#auditBody');
	if (body) {
		body.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div> 加载中...</div>';
		Pagination.destroy(); // 切换 tab 时销毁旧分页实例
		try {
			const fragment = await renderAuditTab(tab);
			body.innerHTML = '';
			body.appendChild(fragment);

			// 通用分页渲染：各 tab 在 renderAuditTab 中存储分页数据，此处统一调用 Pagination.render
			const pgnMap = {
				audit: _auditPgnData,
				white: _whitePgnData,
				black: _blackPgnData,
				sensitive: _sensitivePgnData,
				login: _loginPgnData,
			};
			const tabPgnData = pgnMap[tab];
			if (tabPgnData && tabPgnData.total > 0) {
				const containerSelector = '#' + tab + 'PaginationContainer';
				Pagination.render({
					container: containerSelector,
					page: tabPgnData.page,
					totalPages: tabPgnData.totalPages,
					total: tabPgnData.total,
					pageSize: tabPgnData.pageSize,
					align: 'center',
					onPageChange: (p) => {
						// 审计/登录 tab 翻页只刷新表格行 + 分页状态，不重建筛选栏与分页器；
						// 其余 tab 数据量小（前端分页），直接整体重渲染
						if (tab === 'audit') { auditFilter.page = p; loadAuditPage(p); }
						else if (tab === 'white') { _whitePage = p; setAuditTab('white'); }
						else if (tab === 'black') { _blackPage = p; setAuditTab('black'); }
						else if (tab === 'sensitive') { _sensitivePage = p; setAuditTab('sensitive'); }
						else if (tab === 'login') { _loginPage = p; loadLoginPage(p); }
					}
				});
			}
		} catch (e) {
			console.error('render audit tab failed:', e);
			body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-sub)">加载失败，请刷新重试</div>';
		}
	}
}

async function renderAuditTab(tab) {
	if (tab === 'audit') {
		try {
			let url = '/api/v1/audit/logs/';
			let params = [];
			if (auditFilter.username) params.push(`q=${encodeURIComponent(auditFilter.username)}`);
			if (auditFilter.action) params.push(`action=${encodeURIComponent(auditFilter.action)}`);
			if (auditFilter.ip) params.push(`ip=${encodeURIComponent(auditFilter.ip)}`);
			if (auditFilter.startDate) params.push(`start_date=${auditFilter.startDate}`);
			if (auditFilter.endDate) params.push(`end_date=${auditFilter.endDate}`);
			params.push(`page=${auditFilter.page || 1}`);
			params.push(`page_size=20`);
			if (params.length) url += '?' + params.join('&');

			const data = await api.getJson(url);
			const logs = data.rows || [];
			_auditDetailCache = logs;

			const tmpl = document.getElementById('tmpl-audit-tab');
			const frag = tmpl.content.cloneNode(true);

			// 设置筛选条件值
			frag.querySelector('.audit-filter-username').value = auditFilter.username || '';
			frag.querySelector('.audit-filter-username').onchange = function () { auditFilter.username = this.value; };
			frag.querySelector('.audit-filter-action').value = auditFilter.action || '';
			frag.querySelector('.audit-filter-action').onchange = function () { auditFilter.action = this.value; };
			frag.querySelector('.audit-filter-ip').value = auditFilter.ip || '';
			frag.querySelector('.audit-filter-ip').onchange = function () { auditFilter.ip = this.value; };
			frag.querySelector('.audit-filter-start').value = auditFilter.startDate || '';
			frag.querySelector('.audit-filter-start').onchange = function () { auditFilter.startDate = this.value; };
			frag.querySelector('.audit-filter-end').value = auditFilter.endDate || '';
			frag.querySelector('.audit-filter-end').onchange = function () { auditFilter.endDate = this.value; };
			frag.querySelector('.audit-btn-query').onclick = loadAuditLogs;
			frag.querySelector('.audit-btn-reset').onclick = resetAuditFilter;

			// 渲染表格行（与翻页 loadAuditPage 共用渲染逻辑，保证展示一致）
			_renderAuditRows(frag.querySelector('.audit-tbody'), logs);

			// 存储分页数据，供 setAuditTab 中 Pagination.render 使用（fragment 挂载后渲染）
			_auditPgnData = {
				page: data.page || 1,
				totalPages: data.total_pages || 1,
				total: data.total || 0,
				pageSize: data.page_size || 20
			};

			// 清空分页占位（实际分页由 setAuditTab 在 fragment 挂载后调用 Pagination.render 渲染）
			frag.querySelector('.audit-pagination').innerHTML = '<div id="auditPaginationContainer"></div>';

			return frag;
		} catch (e) {
			console.error('load audit logs failed:', e);
			const div = document.createElement('div');
			div.style.cssText = 'padding:20px;text-align:center;color:var(--text-sub)';
			div.textContent = '加载失败';
			return div;
		}
	}

	if (tab === 'white') {
		try {
			const data = await api.getJson('/api/v1/security/ip-whitelist/');
			const items = data.rows || [];
			_whitelistCache = items;

			const tmpl = document.getElementById('tmpl-whitelist-tab');
			const frag = tmpl.content.cloneNode(true);

			// Set info count（模板中已有基础文案，仅更新数量）
			frag.querySelector('.whitelist-info .tab-info-count').textContent = items.length;

			// 前端分页：全量数据按 _TAB_PAGE_SIZE 切片展示当前页
			const totalPages = Math.max(1, Math.ceil(items.length / _TAB_PAGE_SIZE));
			if (_whitePage > totalPages) _whitePage = 1;
			const start = (_whitePage - 1) * _TAB_PAGE_SIZE;
			const pageItems = items.slice(start, start + _TAB_PAGE_SIZE);

			// Generate table rows（索引使用全局 items 下标，供 editWhitelist/deleteWhitelist 按 idx 取 id）
			const tbody = frag.querySelector('.whitelist-tbody');
			if (pageItems.length === 0) {
				tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--text-sub)">暂无白名单</td></tr>';
			} else {
				tbody.innerHTML = pageItems.map((x, i) => {
					const globalIdx = start + i;
					return `
					<tr>
						<td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip_or_cidr)}</code></td>
						<td>${escapeHtml(x.description || '-')}</td>
						<td>${escapeHtml(x.creator || '-')}</td>
						<td class="text-sub">${formatDate(x.created_at)}</td>
						<td><div class="table-actions"><button class="btn-link btn-sm" onclick="editWhitelist(${globalIdx})">编辑</button><button class="btn-link btn-sm" style="color:var(--danger)" onclick="deleteWhitelist(${x.id})">删除</button></div></td>
					</tr>`;
				}).join('');
			}

			// 存储分页数据，供 setAuditTab 中 Pagination.render 使用
			_whitePgnData = { page: _whitePage, totalPages, total: items.length, pageSize: _TAB_PAGE_SIZE };
			frag.querySelector('.whitelist-pagination').innerHTML = '<div id="whitePaginationContainer"></div>';

			return frag;
		} catch (e) {
			console.error('load whitelist failed:', e);
			const div = document.createElement('div');
			div.style.cssText = 'padding:20px;text-align:center;color:var(--text-sub)';
			div.textContent = '加载失败';
			return div;
		}
	}

	if (tab === 'black') {
		try {
			const data = await api.getJson('/api/v1/security/ip-blacklist/');
			const items = data.rows || [];

			const tmpl = document.getElementById('tmpl-blacklist-tab');
			const frag = tmpl.content.cloneNode(true);

			// 前端分页：全量数据按 _TAB_PAGE_SIZE 切片展示当前页
			const totalPages = Math.max(1, Math.ceil(items.length / _TAB_PAGE_SIZE));
			if (_blackPage > totalPages) _blackPage = 1;
			const start = (_blackPage - 1) * _TAB_PAGE_SIZE;
			const pageItems = items.slice(start, start + _TAB_PAGE_SIZE);

			// Generate table rows
			const tbody = frag.querySelector('.blacklist-tbody');
			if (pageItems.length === 0) {
				tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-sub)">暂无黑名单</td></tr>';
			} else {
				tbody.innerHTML = pageItems.map(x => `
					<tr>
						<td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip)}</code></td>
						<td>${escapeHtml(x.reason === 'login_fail' ? '登录连续失败' : (x.reason === 'manual' ? '人工封禁' : escapeHtml(x.reason || '-')))}</td>
						<td>${escapeHtml(x.detail || '系统自动')}</td>
						<td class="text-sub">${formatDate(x.created_at)}</td>
						<td class="text-sub">${x.expires_at ? formatDate(x.expires_at) : '<span class="tag tag-danger">永久</span>'}</td>
						<td><button class="btn-link btn-sm" onclick="unblockIp(${x.id})">解封</button></td>
					</tr>
				`).join('');
			}

			// 存储分页数据，供 setAuditTab 中 Pagination.render 使用
			_blackPgnData = { page: _blackPage, totalPages, total: items.length, pageSize: _TAB_PAGE_SIZE };
			frag.querySelector('.blacklist-pagination').innerHTML = '<div id="blackPaginationContainer"></div>';

			return frag;
		} catch (e) {
			console.error('load blacklist failed:', e);
			const div = document.createElement('div');
			div.style.cssText = 'padding:20px;text-align:center;color:var(--text-sub)';
			div.textContent = '加载失败';
			return div;
		}
	}

	if (tab === 'sensitive') {
		// 敏感词列表 tab：渲染表格 + 启用/正则状态徽章
		try {
			const data = await api.getJson('/api/v1/security/sensitive-words/');
			const items = data.rows || [];
			_sensitiveCache = items;

			const tmpl = document.getElementById('tmpl-sensitive-tab');
			const frag = tmpl.content.cloneNode(true);

			// 前端分页：全量数据按 _TAB_PAGE_SIZE 切片展示当前页
			const totalPages = Math.max(1, Math.ceil(items.length / _TAB_PAGE_SIZE));
			if (_sensitivePage > totalPages) _sensitivePage = 1;
			const start = (_sensitivePage - 1) * _TAB_PAGE_SIZE;
			const pageItems = items.slice(start, start + _TAB_PAGE_SIZE);

			const tbody = frag.querySelector('.sensitive-tbody');
			if (pageItems.length === 0) {
				tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--text-sub)">暂无敏感词，点击右上角新增</td></tr>';
			} else {
				tbody.innerHTML = pageItems.map((x, i) => {
					const globalIdx = start + i;
					return `
					<tr>
						<td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.word)}</code></td>
						<td>${categoryTag(x.category)}</td>
						<td>${actionTag(x.action)}</td>
						<td><span class="text-sub fw-500" style="color:var(--text)">${x.hit_count || 0}</span></td>
						<td>${x.is_regex ? '<span class="tag tag-primary">是</span>' : '<span class="tag tag-default">否</span>'}</td>
						<td>${x.is_enabled ? '<span class="tag tag-success">启用</span>' : '<span class="tag tag-default">禁用</span>'}</td>
						<td class="text-sub">${formatDate(x.created_at)}</td>
						<td><div class="table-actions"><button class="btn-link btn-sm" onclick="editSensitive(${globalIdx})">编辑</button><button class="btn-link btn-sm" style="color:var(--danger)" onclick="deleteSensitive(${x.id})">删除</button></div></td>
					</tr>`;
				}).join('');
			}

			// 存储分页数据，供 setAuditTab 中 Pagination.render 使用
			_sensitivePgnData = { page: _sensitivePage, totalPages, total: items.length, pageSize: _TAB_PAGE_SIZE };
			frag.querySelector('.sensitive-pagination').innerHTML = '<div id="sensitivePaginationContainer"></div>';

			return frag;
		} catch (e) {
			console.error('load sensitive words failed:', e);
			const div = document.createElement('div');
			div.style.cssText = 'padding:20px;text-align:center;color:var(--text-sub)';
			div.textContent = '加载失败';
			return div;
		}
	}

	if (tab === 'login') {
		try {
			const params = new URLSearchParams({ page: _loginPage, page_size: _TAB_PAGE_SIZE });
			if (_loginFilter.username) params.set('username', _loginFilter.username);
			if (_loginFilter.ip) params.set('ip', _loginFilter.ip);
			if (_loginFilter.result) params.set('result', _loginFilter.result);
			const data = await api.getJson('/api/v1/security/login-attempts/?' + params.toString());
			const items = data.rows || [];

			const tmpl = document.getElementById('tmpl-login-tab');
			const frag = tmpl.content.cloneNode(true);

			// 回填筛选条件
			frag.querySelector('.login-filter-username').value = _loginFilter.username;
			frag.querySelector('.login-filter-ip').value = _loginFilter.ip;
			frag.querySelector('.login-filter-result').value = _loginFilter.result;

			// 生成表格行（与翻页 loadLoginPage 共用渲染逻辑，保证展示一致）
			_renderLoginRows(frag.querySelector('.login-tbody'), items);

			// 存储分页数据，供 setAuditTab 中 Pagination.render 使用
			const total = data.total || 0;
			const totalPages = Math.max(1, Math.ceil(total / _TAB_PAGE_SIZE));
			if (_loginPage > totalPages) _loginPage = 1;
			_loginPgnData = { page: _loginPage, totalPages, total, pageSize: _TAB_PAGE_SIZE };
			frag.querySelector('.login-pagination').innerHTML = '<div id="loginPaginationContainer"></div>';

			return frag;
		} catch (e) {
			console.error('load login attempts failed:', e);
			const div = document.createElement('div');
			div.style.cssText = 'padding:20px;text-align:center;color:var(--text-sub)';
			div.textContent = '加载失败';
			return div;
		}
	}

	const div = document.createElement('div');
	return div;
}

/* ---- 表格行渲染辅助（初始渲染与翻页共用，保证两种路径展示一致） ---- */

function _renderAuditRows(tbody, logs) {
	// 审计日志行渲染；i 为当前页内下标，与 _auditDetailCache 对齐供 showAuditDetail 取记录
	if (logs.length === 0) {
		tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--text-sub)">暂无审计日志</td></tr>';
	} else {
		tbody.innerHTML = logs.map((l, i) => `
			<tr>
				<td class="text-sub">${formatDate(l.created_at)}</td>
				<td class="fw-500">${escapeHtml(l.actor_username || '-')}</td>
				<td>${opTag(l.action)}</td>
				<td>${formatResource(l.target_type, l.target_id)}</td>
				<td><code style="background:var(--hover);padding:1px 5px;border-radius:3px;font-size:12px">${escapeHtml(l.ip_address || '-')}</code></td>
				<td>${resultTag(l.result)}</td>
				<td><button class="btn-link btn-sm" onclick="showAuditDetail(${i})">展开 ›</button></td>
			</tr>
		`).join('');
	}
}

function _renderLoginRows(tbody, items) {
	// 登录尝试行渲染
	if (items.length === 0) {
		tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-sub)">暂无登录记录</td></tr>';
	} else {
		tbody.innerHTML = items.map(x => `
			<tr>
				<td class="text-sub">${formatDate(x.created_at)}</td>
				<td class="fw-500">${escapeHtml(x.username || '-')}</td>
				<td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip)}</code></td>
				<td class="text-sub text-sm" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(x.user_agent || '-')}</td>
				<td>${x.result === 'success' ? '<span class="tag tag-success">✓ 成功</span>' : '<span class="tag tag-danger">✕ 失败</span>'}</td>
				<td class="text-sub">${escapeHtml(x.result === 'wrong_password' ? '密码错误' : (x.result === 'user_not_found' ? '用户不存在' : (x.result === 'locked' ? '账户锁定' : '-')))}</td>
			</tr>
		`).join('');
	}
}

/* ---- 审计日志 tab：翻页只刷新表格行 + 分页状态，不重建筛选栏与分页器 ---- */

async function loadAuditPage(page) {
	const seq = ++_auditReqSeq;
	try {
		let url = '/api/v1/audit/logs/';
		let params = [];
		if (auditFilter.username) params.push(`q=${encodeURIComponent(auditFilter.username)}`);
		if (auditFilter.action) params.push(`action=${encodeURIComponent(auditFilter.action)}`);
		if (auditFilter.ip) params.push(`ip=${encodeURIComponent(auditFilter.ip)}`);
		if (auditFilter.startDate) params.push(`start_date=${auditFilter.startDate}`);
		if (auditFilter.endDate) params.push(`end_date=${auditFilter.endDate}`);
		params.push(`page=${page}`);
		params.push(`page_size=20`);
		url += '?' + params.join('&');

		const data = await api.getJson(url);
		// 请求序号守卫：快速连续翻页时丢弃旧响应，避免旧数据覆盖新状态
		if (seq !== _auditReqSeq) return;
		const logs = data.rows || [];
		_auditDetailCache = logs;

		// 已切换到其他 tab（auditBody 被整体替换）时放弃更新
		const tbody = $('#auditBody .audit-tbody');
		if (!tbody) return;
		_renderAuditRows(tbody, logs);

		const total = data.total || 0;
		const totalPages = Math.max(1, data.total_pages || 1);
		const curPage = Math.min(page, totalPages);
		_auditPgnData = { page: curPage, totalPages, total, pageSize: data.page_size || _TAB_PAGE_SIZE };
		Pagination.update({ page: curPage, totalPages, total });
	} catch (e) {
		console.error('load audit page failed:', e);
	}
}

/* ---- 登录记录 tab：翻页只刷新表格行 + 分页状态，不重建筛选栏与分页器 ---- */

async function loadLoginPage(page) {
	const seq = ++_loginReqSeq;
	try {
		const params = new URLSearchParams({ page, page_size: _TAB_PAGE_SIZE });
		if (_loginFilter.username) params.set('username', _loginFilter.username);
		if (_loginFilter.ip) params.set('ip', _loginFilter.ip);
		if (_loginFilter.result) params.set('result', _loginFilter.result);
		const data = await api.getJson('/api/v1/security/login-attempts/?' + params.toString());
		// 请求序号守卫：快速连续翻页时丢弃旧响应，避免旧数据覆盖新状态
		if (seq !== _loginReqSeq) return;
		const items = data.rows || [];

		// 已切换到其他 tab（auditBody 被整体替换）时放弃更新
		const tbody = $('#auditBody .login-tbody');
		if (!tbody) return;
		_renderLoginRows(tbody, items);

		const total = data.total || 0;
		const totalPages = Math.max(1, Math.ceil(total / _TAB_PAGE_SIZE));
		const curPage = Math.min(page, totalPages);
		_loginPgnData = { page: curPage, totalPages, total, pageSize: _TAB_PAGE_SIZE };
		Pagination.update({ page: curPage, totalPages, total });
	} catch (e) {
		console.error('load login page failed:', e);
	}
}

const _OP_TAG_MAP = { 'login': 'info', 'upload_document': 'primary', 'delete_document': 'danger', 'update_user': 'warning', 'toggle_user_status': 'warning', 'export': 'success', 'create_node': 'default', 'chat_ask': 'default', 'manage_whitelist': 'default', 'manage_blacklist': 'danger', 'manage_sensitive_word': 'warning', 'logout': 'info', 'reset_password': 'warning', 'feedback': 'default', 'admin_users': 'warning', 'update_node': 'default', 'token_refresh': 'info' };
const _OP_LABEL_MAP = { 'login': '登录', 'upload_document': '上传', 'delete_document': '删除', 'update_user': '用户变更', 'toggle_user_status': '启禁用', 'export': '导出', 'create_node': '知识库', 'chat_ask': '问答', 'manage_whitelist': '白名单', 'manage_blacklist': '黑名单', 'manage_sensitive_word': '敏感词', 'logout': '登出', 'reset_password': '改密', 'feedback': '反馈', 'admin_users': '用户管理', 'update_node': '节点变更', 'token_refresh': '令牌刷新' };
const _RESULT_TAG_MAP = { 'success': '<span class="tag tag-success">✓ 成功</span>', 'failed': '<span class="tag tag-danger">✕ 失败</span>', 'denied': '<span class="tag tag-warning">⚠ 拒绝</span>' };

function opTag(op) {
	// 未知 op 转义防 XSS（后端可能返回未枚举的 action）
	const label = _OP_LABEL_MAP[op] || escapeHtml(op);
	return `<span class="tag tag-${_OP_TAG_MAP[op] || 'default'}">${label}</span>`;
}

function resultTag(result) {
	return _RESULT_TAG_MAP[result] || _RESULT_TAG_MAP['failed'];
}

function showAuditDetail(idx) {
	const l = _auditDetailCache[idx];
	if (!l) return;
	$('#modal-audit-title').textContent = '审计详情 · ' + (l.action || '审计记录');

	const tmpl = document.getElementById('tmpl-audit-detail');
	const frag = tmpl.content.cloneNode(true);

	frag.querySelector('.audit-detail-time').textContent = formatDate(l.created_at);
	frag.querySelector('.audit-detail-user').textContent = l.actor_username || '-';
	frag.querySelector('.audit-detail-action').textContent = l.action || '-';
	frag.querySelector('.audit-detail-resource').textContent = formatResourceText(l.target_type, l.target_id);
	frag.querySelector('.audit-detail-ip').innerHTML = '<code>' + escapeHtml(l.ip_address || '-') + '</code>';
	frag.querySelector('.audit-detail-result').textContent = l.result || '-';
	frag.querySelector('#audit-json-block').textContent = JSON.stringify(l, null, 2);

	const body = $('#modal-audit-body');
	body.innerHTML = '';
	body.appendChild(frag);

	showMask(true);
	$('#modal-audit').classList.add('show');
}

function copyAuditJson() {
	const pre = $('#audit-json-block');
	if (!pre) return;
	const text = pre.textContent || '';
	if (navigator.clipboard && navigator.clipboard.writeText) {
		navigator.clipboard.writeText(text).then(() => toast('已复制到剪贴板', 'success')).catch(() => toast('复制失败', 'error'));
	} else {
		const ta = document.createElement('textarea');
		ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
		document.body.appendChild(ta); ta.select();
		try { document.execCommand('copy'); toast('已复制到剪贴板', 'success'); } catch (e) { toast('复制失败', 'error'); }
		document.body.removeChild(ta);
	}
}

async function showAddWhitelist() {
	showConfirmDialog({
		title: '新增白名单',
		bannerText: '命中白名单的 IP 直接放行，白名单外的 IP 会继续检查黑名单',
		bannerType: 'info',
		bannerIcon: '✅',
		bodyHtml:
			'<div class="form-item">' +
			'  <label class="form-label">IP / CIDR <span class="required">*</span></label>' +
			'  <input class="input" id="dlg-whitelist-ip" style="width:100%" placeholder="单 IP / CIDR / 通配符 / 范围，如 10.0.0.1、10.0.0.0/24、10.0.*.*">' +
			'  <div class="form-hint">支持格式：单 IP（10.0.0.1）、CIDR（10.0.0.0/24）、通配符（10.0.*.*）、范围（10.0.0.1-10.0.0.100）</div>' +
			'</div>' +
			'<div class="form-item" style="margin-bottom:0">' +
			'  <label class="form-label">说明 <span class="required">*</span></label>' +
			'  <input class="input" id="dlg-whitelist-desc" style="width:100%" placeholder="原因, 便于后续审计追溯">' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认添加', type: 'primary',
				onClick: async (ctx) => {
					const ip = document.getElementById('dlg-whitelist-ip').value.trim();
					if (!ip) { ctx.setError('请输入 IP 或 CIDR'); return; }
					const check = validateIpPattern(ip);
					if (!check.valid) { ctx.setError(check.error); return; }
					const desc = document.getElementById('dlg-whitelist-desc').value.trim();
					if (!desc) { ctx.setError('请输入说明'); return; }
					try {
						const res = await api.postJson('/api/v1/security/ip-whitelist/', { ip_or_cidr: ip, description: desc });
						ctx.close();
						if (res.status === 'executed') {
							toast('白名单新增已立即生效', 'success');
						} else {
							toast(`已创建审批工单 ${res.ticket_no}，需双审后生效`, 'info');
						}
						await setAuditTab('white');
					} catch (e) {
						ctx.setError(e.message || '添加失败');
					}
				}
			}
		],
		onShow: (ctx) => {
			document.getElementById('dlg-whitelist-ip').focus();
			// 回车提交
			ctx.el.addEventListener('keydown', (ev) => {
				if (ev.key === 'Enter') {
					ev.preventDefault();
					ctx.el.querySelector('.btn-save')?.click();
				}
			});
		}
	});
}

async function editWhitelist(idx) {
	const x = _whitelistCache[idx];
	if (!x) return;

	showConfirmDialog({
		title: '编辑白名单',
		bannerText: '修改后立即生效',
		bannerType: 'info',
		bannerIcon: '✏️',
		bodyHtml:
			'<div class="form-item">' +
			'  <label class="form-label">IP / CIDR <span class="required">*</span></label>' +
			`  <input class="input" id="dlg-whitelist-ip" style="width:100%" value="${escapeHtml(x.ip_or_cidr)}">` +
			'  <div class="form-hint">支持格式：单 IP（10.0.0.1）、CIDR（10.0.0.0/24）、通配符（10.0.*.*）、范围（10.0.0.1-10.0.0.100）</div>' +
			'</div>' +
			'<div class="form-item" style="margin-bottom:0">' +
			'  <label class="form-label">说明 <span class="required">*</span></label>' +
			`  <input class="input" id="dlg-whitelist-desc" style="width:100%" value="${escapeHtml(x.description || '')}">` +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '保存修改', type: 'primary',
				onClick: async (ctx) => {
					const ip = document.getElementById('dlg-whitelist-ip').value.trim();
					if (!ip) { ctx.setError('请输入 IP 或 CIDR'); return; }
					const check = validateIpPattern(ip);
					if (!check.valid) { ctx.setError(check.error); return; }
					const desc = document.getElementById('dlg-whitelist-desc').value.trim();
					if (!desc) { ctx.setError('请输入说明'); return; }
					try {
						const res = await api.put(`/api/v1/security/ip-whitelist/${x.id}/`, { ip_or_cidr: ip, description: desc });
						ctx.close();
						if (res.status === 'executed') {
							toast('白名单编辑已立即生效', 'success');
						} else {
							toast(`已创建审批工单 ${res.ticket_no}，需双审后生效`, 'info');
						}
						await setAuditTab('white');
					} catch (e) {
						ctx.setError(e.message || '更新失败');
					}
				}
			}
		],
		onShow: (ctx) => {
			document.getElementById('dlg-whitelist-ip').focus();
			ctx.el.addEventListener('keydown', (ev) => {
				if (ev.key === 'Enter') {
					ev.preventDefault();
					ctx.el.querySelector('.btn-save')?.click();
				}
			});
		}
	});
}

async function deleteWhitelist(id) {
	showConfirmDialog({
		title: '删除白名单',
		bannerText: '删除白名单需双审，审批通过后生效',
		bannerType: 'danger',
		bannerIcon: '⚠',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除', type: 'danger',
				onClick: async (ctx) => {
					try {
						const res = await api.deleteJson(`/api/v1/security/ip-whitelist/${id}/`);
						ctx.close();
						if (res.status === 'executed') {
							toast('白名单已删除', 'success');
						} else {
							toast(`已创建审批工单 ${res.ticket_no}，需双审后生效`, 'info');
						}
						await setAuditTab('white');
					} catch (e) {
						ctx.setError(e.message || '删除失败');
					}
				}
			}
		]
	});
}

async function showAddBlacklist() {
	showConfirmDialog({
		title: '手动封禁 IP',
		bannerText: '黑名单新增将立即生效，无需审批',
		bannerType: 'danger',
		bannerIcon: '🚫',
		bodyHtml:
			'<div class="form-item">' +
			'  <label class="form-label">IP 地址 <span class="required">*</span></label>' +
			'  <input class="input" id="dlg-blacklist-ip" style="width:100%" placeholder="单 IP / 通配符 / 范围，如 10.0.0.1、10.0.*.*、10.0.0.1-10.0.0.100">' +
			'  <div class="form-hint">支持格式：单 IP（10.0.0.1）、通配符（10.0.*.*）、范围（10.0.0.1-10.0.0.100）</div>' +
			'</div>' +
			'<div class="form-item" style="margin-bottom:0">' +
			'  <label class="form-label">封禁原因 <span class="required">*</span></label>' +
			'  <input class="input" id="dlg-blacklist-reason" style="width:100%" placeholder="必填，便于后续审计追溯">' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认封禁', type: 'danger',
				onClick: async (ctx) => {
					const ip = document.getElementById('dlg-blacklist-ip').value.trim();
					if (!ip) { ctx.setError('请输入 IP 地址'); return; }
					const check = validateIpPattern(ip);
					if (!check.valid) { ctx.setError(check.error); return; }
					const reason = document.getElementById('dlg-blacklist-reason').value.trim();
					if (!reason) { ctx.setError('请输入封禁原因'); return; }
					try {
						const res = await api.postJson('/api/v1/security/ip-blacklist/', { ip, reason, detail: '人工封禁' });
						ctx.close();
						if (res.status === 'executed') {
							toast('黑名单新增已立即生效', 'success');
						} else {
							toast(`已创建审批工单 ${res.ticket_no}`, 'info');
						}
						await setAuditTab('black');
					} catch (e) {
						ctx.setError(e.message || '封禁失败');
					}
				}
			}
		],
		onShow: (ctx) => {
			document.getElementById('dlg-blacklist-ip').focus();
			ctx.el.addEventListener('keydown', (ev) => {
				if (ev.key === 'Enter') {
					ev.preventDefault();
					ctx.el.querySelector('.btn-reject')?.click();
				}
			});
		}
	});
}

async function unblockIp(id) {
	showConfirmDialog({
		title: '解封 IP',
		bannerText: '解封需单审，审批通过后生效',
		bannerType: 'info',
		bannerIcon: '🔓',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认解封', type: 'primary',
				onClick: async (ctx) => {
					try {
						const res = await api.put(`/api/v1/security/ip-blacklist/${id}/`, {});
						ctx.close();
						if (res.status === 'executed') {
							toast('已解封', 'success');
						} else {
							toast(`已创建审批工单 ${res.ticket_no}，需单审后生效`, 'info');
						}
						await setAuditTab('black');
					} catch (e) {
						ctx.setError(e.message || '解封失败');
					}
				}
			}
		]
	});
}

/* ---- 敏感词管理 CRUD ----
 * 后端契约：
 *   POST /api/v1/security/sensitive-words/        {word, category, action, is_regex}
 *   PUT  /api/v1/security/sensitive-words/{id}/   {action, is_enabled}（word/category 不可改）
 *   DELETE /api/v1/security/sensitive-words/{id}/
 * CRUD 后后端会自动触发 SensitiveFilter 重建（AC 自动机重载）
 */

// 分类 → 彩色 tag 映射（仅用于展示，不影响处理逻辑）
const _SENSITIVE_CATEGORY_MAP = {
	'phone': { label: '手机号', tag: 'info' },
	'id_card': { label: '身份证', tag: 'warning' },
	'email': { label: '邮箱', tag: 'info' },
	'bank_card': { label: '银行卡', tag: 'warning' },
	'secret': { label: '内部机密', tag: 'danger' },
	'other': { label: '其它', tag: 'default' },
};

// 动作 → 彩色 tag 映射（block=红 / mask=黄 / warn=灰，颜色与拦截卡片视觉一致）
const _SENSITIVE_ACTION_MAP = {
	'mask': { label: '脱敏', tag: 'warning' },
	'block': { label: '拦截', tag: 'danger' },
	'warn': { label: '告警', tag: 'default' },
};

function categoryTag(c) {
	// 未知分类转义防 XSS
	const m = _SENSITIVE_CATEGORY_MAP[c] || { label: escapeHtml(c || '-'), tag: 'default' };
	return `<span class="tag tag-${m.tag}">${m.label}</span>`;
}

function actionTag(a) {
	// 未知动作转义防 XSS
	const m = _SENSITIVE_ACTION_MAP[a] || { label: escapeHtml(a || '-'), tag: 'default' };
	return `<span class="tag tag-${m.tag}">${m.label}</span>`;
}

/* 打开"新增敏感词"弹窗：重置表单 + 启用所有字段 */
function showAddSensitive() {
	_sensitiveEditId = null;
	$('#modal-sensitive-title').textContent = '新增敏感词';
	$('.sensitive-input-word').value = '';
	$('.sensitive-input-word').disabled = false;
	$('.sensitive-input-category').value = 'other';
	$('.sensitive-input-category').disabled = false;
	$('.sensitive-input-action').value = 'mask';
	$('.sensitive-input-regex').checked = false;
	$('.sensitive-input-regex').disabled = false;
	$('.sensitive-input-enabled').checked = true;
	// 新增时强制启用：后端 POST 硬编码 is_enabled=True，不接受该字段
	// 禁用复选框避免用户误以为取消勾选可以创建即禁用
	$('.sensitive-input-enabled').disabled = true;
	showMask(true);
	$('#modal-sensitive').classList.add('show');
	// 延迟聚焦，等 modal 显示后再 focus
	setTimeout(() => $('.sensitive-input-word')?.focus(), 50);
}

/* 打开"编辑敏感词"弹窗：回填数据 + 禁用 word/category/is_regex（后端 PUT 不支持修改）
 * 仅 action 和 is_enabled 可编辑
 */
function editSensitive(idx) {
	const x = _sensitiveCache[idx];
	if (!x) return;
	_sensitiveEditId = x.id;
	$('#modal-sensitive-title').textContent = '编辑敏感词';
	$('.sensitive-input-word').value = x.word || '';
	$('.sensitive-input-word').disabled = true;
	$('.sensitive-input-category').value = x.category || 'other';
	$('.sensitive-input-category').disabled = true;
	$('.sensitive-input-action').value = x.action || 'mask';
	$('.sensitive-input-regex').checked = !!x.is_regex;
	$('.sensitive-input-regex').disabled = true;
	$('.sensitive-input-enabled').checked = x.is_enabled !== false;
	$('.sensitive-input-enabled').disabled = false;
	showMask(true);
	$('#modal-sensitive').classList.add('show');
}

/* 保存敏感词：根据 _sensitiveEditId 区分新增/编辑
 * - 新增：POST，提交 word/category/action/is_regex
 * - 编辑：PUT，仅提交 action/is_enabled（后端限制）
 */
async function saveSensitive() {
	const action = $('.sensitive-input-action').value;
	const isEnabled = $('.sensitive-input-enabled').checked;

	if (_sensitiveEditId === null) {
		// 新增模式
		const word = $('.sensitive-input-word').value.trim();
		if (!word) {
			toast('请输入敏感词', 'error');
			$('.sensitive-input-word')?.focus();
			return;
		}
		const category = $('.sensitive-input-category').value;
		const isRegex = $('.sensitive-input-regex').checked;
		try {
			const res = await api.postJson('/api/v1/security/sensitive-words/', {
				word, category, action, is_regex: isRegex
			});
			if (res.status === 'executed') {
				toast('敏感词新增已立即生效', 'success');
			} else {
				toast(`已创建审批工单 ${res.ticket_no}`, 'info');
			}
			_sensitiveEditId = null;  // 状态收口：关闭前重置，防止残留
			closeAllOverlays();
			await setAuditTab('sensitive');
		} catch (e) {
			toast(e.message || '添加失败', 'error');
		}
	} else {
		// 编辑模式：仅 action 和 is_enabled 可改
		try {
			const res = await api.put(`/api/v1/security/sensitive-words/${_sensitiveEditId}/`, {
				action, is_enabled: isEnabled
			});
			if (res.status === 'executed') {
				toast('敏感词变更已立即生效', 'success');
			} else {
				toast(`已创建审批工单 ${res.ticket_no}，需单审后生效`, 'info');
			}
			_sensitiveEditId = null;  // 状态收口：关闭前重置，防止残留
			closeAllOverlays();
			await setAuditTab('sensitive');
		} catch (e) {
			toast(e.message || '更新失败', 'error');
		}
	}
}

async function deleteSensitive(id) {
	showConfirmDialog({
		title: '删除敏感词',
		bannerText: '删除敏感词需单审，审批通过后生效',
		bannerType: 'danger',
		bannerIcon: '⚠',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除', type: 'danger',
				onClick: async (ctx) => {
					try {
						const res = await api.deleteJson(`/api/v1/security/sensitive-words/${id}/`);
						ctx.close();
						if (res.status === 'executed') {
							toast('敏感词已删除', 'success');
						} else {
							toast(`已创建审批工单 ${res.ticket_no}，需单审后生效`, 'info');
						}
						await setAuditTab('sensitive');
					} catch (e) {
						ctx.setError(e.message || '删除失败');
					}
				}
			}
		]
	});
}

async function loadAuditLogs() {
	auditFilter.page = 1;
	await setAuditTab('audit');
}

async function loadLoginAttempts() {
	// 从筛选栏读取当前筛选条件并刷新
	const body = $('#auditBody');
	if (body) {
		const usernameInput = body.querySelector('.login-filter-username');
		const ipInput = body.querySelector('.login-filter-ip');
		const resultSelect = body.querySelector('.login-filter-result');
		if (usernameInput) _loginFilter.username = usernameInput.value.trim();
		if (ipInput) _loginFilter.ip = ipInput.value.trim();
		if (resultSelect) _loginFilter.result = resultSelect.value;
	}
	_loginPage = 1;
	await setAuditTab('login');
}

function filterLoginAttempts() {
	// 点击"查询"按钮，从筛选栏读取条件
	loadLoginAttempts();
}

function resetLoginFilter() {
	_loginFilter = { username: '', ip: '', result: '' };
	_loginPage = 1;
	setAuditTab('login');
}

function resetAuditFilter() {
	auditFilter = { username: '', action: '', ip: '', startDate: '', endDate: '', page: 1 };
	loadAuditLogs();
}

function formatResource(targetType, targetId) {
	const type = targetType || '';
	const id = targetId ? String(targetId) : '';
	if (!type && !id) return '-';
	if (!id) return escapeHtml(type);
	return escapeHtml(type) + ': ' + escapeHtml(id);
}

function formatResourceText(targetType, targetId) {
	const type = targetType || '';
	const id = targetId ? String(targetId) : '';
	if (!type && !id) return '-';
	if (!id) return type;
	return type + ': ' + id;
}

async function exportAuditLogs() {
	try {
		const baseParams = [];
		if (auditFilter.username) baseParams.push('q=' + encodeURIComponent(auditFilter.username));
		if (auditFilter.action) baseParams.push('action=' + encodeURIComponent(auditFilter.action));
		if (auditFilter.ip) baseParams.push('ip=' + encodeURIComponent(auditFilter.ip));
		if (auditFilter.startDate) baseParams.push('start_date=' + auditFilter.startDate);
		if (auditFilter.endDate) baseParams.push('end_date=' + auditFilter.endDate);

		// 先取第一页获取 total
		let url = '/api/v1/audit/logs/?page=1&page_size=200';
		if (baseParams.length) url += '&' + baseParams.join('&');
		const first = await api.getJson(url);
		let rows = first.rows || [];
		const total = first.total || 0;
		const totalPages = first.total_pages || 1;

		// 分批拉取剩余页
		for (let p = 2; p <= totalPages; p++) {
			url = '/api/v1/audit/logs/?page=' + p + '&page_size=200';
			if (baseParams.length) url += '&' + baseParams.join('&');
			const pageData = await api.getJson(url);
			rows = rows.concat(pageData.rows || []);
		}

		const BOM = '\uFEFF';
		const header = '时间,用户,操作类型,资源,IP地址,结果\n';
		const csv = rows.map(r => [
			formatDate(r.created_at),
			(r.actor_username || '-').replace(/,/g, ' '),
			(r.action || '-').replace(/,/g, ' '),
			(r.target_type || '') + (r.target_id ? ':' + r.target_id : ''),
			(r.ip_address || '-').replace(/,/g, ' '),
			r.result || '-'
		].join(',')).join('\n');
		const blob = new Blob([BOM + header + csv], { type: 'text/csv;charset=utf-8' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = 'audit_logs_' + new Date().toISOString().slice(0, 10) + '.csv';
		a.click();
		URL.revokeObjectURL(a.href);
		toast(`导出 ${rows.length} 条记录`, 'success');
	} catch (e) {
		toast('导出失败: ' + (e.message || '未知错误'), 'error');
	}
}

/**
 * 导出登录尝试记录为 CSV，支持当前筛选条件
 */
async function exportLoginAttempts() {
	try {
		// 从筛选栏读取最新条件
		const body = $('#auditBody');
		if (body) {
			const usernameInput = body.querySelector('.login-filter-username');
			const ipInput = body.querySelector('.login-filter-ip');
			const resultSelect = body.querySelector('.login-filter-result');
			if (usernameInput) _loginFilter.username = usernameInput.value.trim();
			if (ipInput) _loginFilter.ip = ipInput.value.trim();
			if (resultSelect) _loginFilter.result = resultSelect.value;
		}

		const baseParams = [];
		if (_loginFilter.username) baseParams.push('username=' + encodeURIComponent(_loginFilter.username));
		if (_loginFilter.ip) baseParams.push('ip=' + encodeURIComponent(_loginFilter.ip));
		if (_loginFilter.result) baseParams.push('result=' + encodeURIComponent(_loginFilter.result));

		// 先取第一页获取 total
		let url = '/api/v1/security/login-attempts/?page=1&page_size=200';
		if (baseParams.length) url += '&' + baseParams.join('&');
		const first = await api.getJson(url);
		let rows = first.rows || [];
		const totalPages = Math.ceil((first.total || 0) / 200) || 1;

		// 分批拉取剩余页
		for (let p = 2; p <= totalPages; p++) {
			url = '/api/v1/security/login-attempts/?page=' + p + '&page_size=200';
			if (baseParams.length) url += '&' + baseParams.join('&');
			const pageData = await api.getJson(url);
			rows = rows.concat(pageData.rows || []);
		}

		// 结果中文映射
		const resultLabel = { success: '成功', wrong_password: '密码错误', user_not_found: '用户不存在', locked: '账户锁定', captcha_fail: '验证码失败', ip_denied: 'IP 拒绝' };
		const BOM = '\uFEFF';
		const header = '时间,用户,IP地址,User-Agent,结果\n';
		const csv = rows.map(r => [
			formatDate(r.created_at),
			(r.username || '-').replace(/,/g, ' '),
			(r.ip || '-').replace(/,/g, ' '),
			(r.user_agent || '-').replace(/,/g, ' ').replace(/"/g, '""'),
			resultLabel[r.result] || r.result || '-'
		].map(v => `"${v}"`).join(',')).join('\n');
		const blob = new Blob([BOM + header + csv], { type: 'text/csv;charset=utf-8' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = 'login_attempts_' + new Date().toISOString().slice(0, 10) + '.csv';
		a.click();
		URL.revokeObjectURL(a.href);
		toast(`导出 ${rows.length} 条登录记录`, 'success');
	} catch (e) {
		toast('导出失败: ' + (e.message || '未知错误'), 'error');
	}
}
