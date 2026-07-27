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

document.addEventListener('DOMContentLoaded', () => {
	initAuditPage();
});

async function initAuditPage() {
	await setAuditTab(STATE.currentAuditTab || 'audit');
}

async function setAuditTab(tab) {
	STATE.currentAuditTab = tab;
	$$('.tab-item').forEach((t, i) => {
		t.classList.toggle('active', ['audit', 'white', 'black', 'login'][i] === tab);
	});
	const body = $('#auditBody');
	if (body) {
		body.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div> 加载中...</div>';
		try {
			const fragment = await renderAuditTab(tab);
			body.innerHTML = '';
			body.appendChild(fragment);
		} catch (e) {
			console.error('render audit tab failed:', e);
			body.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-sub)">加载失败，请刷新重试</div>';
		}
	}
}

function buildPagination(data, loadFn) {
	const totalPages = data.total_pages || 1;
	const page = data.page || 1;
	if (totalPages <= 1 && (data.total || 0) <= (data.page_size || 20)) {
		return `<span>共 ${data.total || 0} 条</span>`;
	}
	let html = `<span>共 ${data.total || 0} 条</span>`;
	html += `<button class="page-btn" ${page <= 1 ? 'disabled' : ''} onclick="${loadFn}(${page - 1})">‹</button>`;
	const maxBtns = 5;
	let start = Math.max(1, page - Math.floor(maxBtns / 2));
	let end = Math.min(totalPages, start + maxBtns - 1);
	if (end - start < maxBtns - 1) start = Math.max(1, end - maxBtns + 1);
	for (let p = start; p <= end; p++) {
		html += `<button class="page-btn ${p === page ? 'active' : ''}" onclick="${loadFn}(${p})">${p}</button>`;
	}
	html += `<button class="page-btn" ${page >= totalPages ? 'disabled' : ''} onclick="${loadFn}(${page + 1})">›</button>`;
	return html;
}

async function loadAuditPage(p) {
	auditFilter.page = p || 1;
	await setAuditTab('audit');
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

			// Set filter values
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

			// Generate table rows
			const tbody = frag.querySelector('.audit-tbody');
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

			// Set pagination
			frag.querySelector('.audit-pagination').innerHTML = buildPagination(data, 'loadAuditPage');

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

			// Set info count
			frag.querySelector('.whitelist-info').textContent = '白名单外 IP 将直接返回 403，共 ' + items.length + ' 条规则';

			// Generate table rows
			const tbody = frag.querySelector('.whitelist-tbody');
			if (items.length === 0) {
				tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--text-sub)">暂无白名单</td></tr>';
			} else {
				tbody.innerHTML = items.map((x, i) => `
					<tr>
						<td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip_or_cidr)}</code></td>
						<td>${escapeHtml(x.description || '-')}</td>
						<td>${escapeHtml(x.creator || '-')}</td>
						<td class="text-sub">${formatDate(x.created_at)}</td>
						<td><div class="table-actions"><button class="btn-link btn-sm" onclick="editWhitelist(${i})">编辑</button><button class="btn-link btn-sm" style="color:var(--danger)" onclick="deleteWhitelist(${x.id})">删除</button></div></td>
					</tr>
				`).join('');
			}

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

			// Generate table rows
			const tbody = frag.querySelector('.blacklist-tbody');
			if (items.length === 0) {
				tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-sub)">暂无黑名单</td></tr>';
			} else {
				tbody.innerHTML = items.map(x => `
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

			return frag;
		} catch (e) {
			console.error('load blacklist failed:', e);
			const div = document.createElement('div');
			div.style.cssText = 'padding:20px;text-align:center;color:var(--text-sub)';
			div.textContent = '加载失败';
			return div;
		}
	}

	if (tab === 'login') {
		try {
			let url = '/api/v1/security/login-attempts/';
			const resultFilter = window._loginResultFilter || '';
			if (resultFilter) url += '?result=' + encodeURIComponent(resultFilter);
			const data = await api.getJson(url);
			const items = data.rows || [];

			const tmpl = document.getElementById('tmpl-login-tab');
			const frag = tmpl.content.cloneNode(true);

			// Set filter select value
			frag.querySelector('.login-filter-result').value = window._loginResultFilter || '';
			frag.querySelector('.login-filter-result').onchange = function () { filterLoginAttempts(this.value); };

			// Generate table rows
			const tbody = frag.querySelector('.login-tbody');
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

const _OP_TAG_MAP = { 'login': 'info', 'upload_document': 'primary', 'delete_document': 'danger', 'update_user': 'warning', 'toggle_user_status': 'warning', 'export': 'success', 'create_node': 'default', 'chat_ask': 'default', 'manage_whitelist': 'default', 'manage_blacklist': 'danger', 'logout': 'info', 'reset_password': 'warning', 'feedback': 'default', 'admin_users': 'warning', 'update_node': 'default', 'token_refresh': 'info' };
const _OP_LABEL_MAP = { 'login': '登录', 'upload_document': '上传', 'delete_document': '删除', 'update_user': '用户变更', 'toggle_user_status': '启禁用', 'export': '导出', 'create_node': '知识库', 'chat_ask': '问答', 'manage_whitelist': '白名单', 'manage_blacklist': '黑名单', 'logout': '登出', 'reset_password': '改密', 'feedback': '反馈', 'admin_users': '用户管理', 'update_node': '节点变更', 'token_refresh': '令牌刷新' };
const _RESULT_TAG_MAP = { 'success': '<span class="tag tag-success">✓ 成功</span>', 'failed': '<span class="tag tag-danger">✕ 失败</span>', 'denied': '<span class="tag tag-warning">⚠ 拒绝</span>' };

function opTag(op) {
	return `<span class="tag tag-${_OP_TAG_MAP[op] || 'default'}">${_OP_LABEL_MAP[op] || op}</span>`;
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
	const ip = prompt('请输入 IP 或 CIDR：');
	if (!ip) return;
	const desc = prompt('请输入说明（可选）：');

	try {
		await api.postJson('/api/v1/security/ip-whitelist/', { ip_or_cidr: ip, description: desc || '' });
		toast('已添加白名单', 'success');
		await setAuditTab('white');
	} catch (e) {
		toast(e.message || '添加失败', 'error');
	}
}

async function editWhitelist(idx) {
	const x = _whitelistCache[idx];
	if (!x) return;
	const ip = prompt('修改 IP 或 CIDR：', x.ip_or_cidr);
	if (!ip) return;
	const desc = prompt('修改说明：', x.description || '');

	try {
		await api.put(`/api/v1/security/ip-whitelist/${x.id}/`, { ip_or_cidr: ip, description: desc || '' });
		toast('已更新白名单', 'success');
		await setAuditTab('white');
	} catch (e) {
		toast(e.message || '更新失败', 'error');
	}
}

async function deleteWhitelist(id) {
	if (!confirm('确定删除此白名单？')) return;
	try {
		await api.deleteJson(`/api/v1/security/ip-whitelist/${id}/`);
		toast('已删除', 'success');
		await setAuditTab('white');
	} catch (e) {
		toast(e.message || '删除失败', 'error');
	}
}

async function showAddBlacklist() {
	const ip = prompt('请输入要封禁的 IP 地址：');
	if (!ip) return;
	const reason = prompt('请输入封禁原因：', 'manual');

	try {
		await api.postJson('/api/v1/security/ip-blacklist/', { ip: ip, reason: reason || 'manual', detail: '人工封禁' });
		toast('已封禁 IP', 'success');
		await setAuditTab('black');
	} catch (e) {
		toast(e.message || '封禁失败', 'error');
	}
}

async function unblockIp(id) {
	if (!confirm('确定解封此 IP？')) return;
	try {
		await api.put(`/api/v1/security/ip-blacklist/${id}/`, {});
		toast('已解封', 'success');
		await setAuditTab('black');
	} catch (e) {
		toast(e.message || '解封失败', 'error');
	}
}

async function loadAuditLogs() {
	auditFilter.page = 1;
	await setAuditTab('audit');
}

async function loadLoginAttempts() {
	await setAuditTab('login');
}

function resetAuditFilter() {
	auditFilter = { username: '', action: '', ip: '', startDate: '', endDate: '', page: 1 };
	loadAuditLogs();
}

function filterLoginAttempts(result) {
	window._loginResultFilter = result;
	setAuditTab('login');
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
