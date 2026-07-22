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
			const html = await renderAuditTab(tab);
			body.innerHTML = html;
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

			return `
        <div class="flex gap-8 mb-16" style="flex-wrap:wrap;align-items:center;padding:12px;background:var(--primary-light);border-radius:var(--radius);border:1px solid var(--border)">
          <input class="input" style="width:180px" placeholder="🔍 用户名" value="${escapeHtml(auditFilter.username || '')}" onchange="auditFilter.username = this.value">
          <select class="select" style="width:150px" onchange="auditFilter.action = this.value">
            <option value="">全部操作类型</option><option value="login" ${auditFilter.action === 'login' ? 'selected' : ''}>登录</option><option value="upload_document" ${auditFilter.action === 'upload_document' ? 'selected' : ''}>上传</option><option value="delete_document" ${auditFilter.action === 'delete_document' ? 'selected' : ''}>删除</option><option value="update_user" ${auditFilter.action === 'update_user' ? 'selected' : ''}>用户变更</option><option value="toggle_user_status" ${auditFilter.action === 'toggle_user_status' ? 'selected' : ''}>启禁用</option><option value="export" ${auditFilter.action === 'export' ? 'selected' : ''}>导出</option><option value="create_node" ${auditFilter.action === 'create_node' ? 'selected' : ''}>知识库</option><option value="chat_ask" ${auditFilter.action === 'chat_ask' ? 'selected' : ''}>问答</option>
          </select>
          <input class="input" style="width:200px" placeholder="🌐 IP 地址" value="${escapeHtml(auditFilter.ip || '')}" onchange="auditFilter.ip = this.value">
          <input class="input" type="date" style="width:150px" value="${auditFilter.startDate || ''}" onchange="auditFilter.startDate = this.value">
          <span style="align-self:center">至</span>
          <input class="input" type="date" style="width:150px" value="${auditFilter.endDate || ''}" onchange="auditFilter.endDate = this.value">
          <button class="btn btn-primary btn-sm" onclick="loadAuditLogs()" style="width:72px">查询</button>
          <button class="btn btn-sm" onclick="resetAuditFilter()" style="width:72px">重置</button>
        </div>
        <table class="table" style="border:1px solid var(--border);border-radius:var(--radius)">
          <thead>
            <tr><th>时间</th><th>用户</th><th>操作类型</th><th>资源</th><th>IP 地址</th><th>结果</th><th>详情</th></tr>
          </thead>
          <tbody>
            ${logs.length === 0 ? '<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--text-sub)">暂无审计日志</td></tr>' : logs.map((l, i) => `
              <tr>
                <td class="text-sub">${formatDate(l.created_at)}</td>
                <td class="fw-500">${escapeHtml(l.actor_username || '-')}</td>
                <td>${opTag(l.action)}</td>
                <td>${formatResource(l.target_type, l.target_id)}</td>
                <td><code style="background:var(--hover);padding:1px 5px;border-radius:3px;font-size:12px">${escapeHtml(l.ip_address || '-')}</code></td>
                <td>${resultTag(l.result)}</td>
                <td><button class="btn-link btn-sm" onclick="showAuditDetail(${i})">展开 ›</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
        <div class="pagination">${buildPagination(data, 'loadAuditPage')}</div>`;
		} catch (e) {
			console.error('load audit logs failed:', e);
			return '<div style="padding:20px;text-align:center;color:var(--text-sub)">加载失败</div>';
		}
	}

	if (tab === 'white') {
		try {
			const data = await api.getJson('/api/v1/security/ip-whitelist/');
			const items = data.rows || [];
			_whitelistCache = items;

			return `
        <div class="flex justify-between mb-16">
          <div class="text-sub text-sm">白名单外 IP 将直接返回 403，共 ${items.length} 条规则</div>
          <button class="btn btn-primary btn-sm" onclick="showAddWhitelist()">＋ 新增白名单</button>
        </div>
        <table class="table" style="border:1px solid var(--border);border-radius:var(--radius)">
          <thead><tr><th>IP / CIDR</th><th>说明</th><th>添加人</th><th>添加时间</th><th style="width:140px">操作</th></tr></thead>
          <tbody>
            ${items.length === 0 ? '<tr><td colspan="5" style="text-align:center;padding:20px;color:var(--text-sub)">暂无白名单</td></tr>' : items.map((x, i) => `
              <tr>
                <td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip_or_cidr)}</code></td>
                <td>${escapeHtml(x.description || '-')}</td>
                <td>${escapeHtml(x.creator || '-')}</td>
                <td class="text-sub">${formatDate(x.created_at)}</td>
                <td><div class="table-actions"><button class="btn-link btn-sm" onclick="editWhitelist(${i})">编辑</button><button class="btn-link btn-sm" style="color:var(--danger)" onclick="deleteWhitelist(${x.id})">删除</button></div></td>
              </tr>
            `).join('')}
          </tbody>
        </table>`;
		} catch (e) {
			console.error('load whitelist failed:', e);
			return '<div style="padding:20px;text-align:center;color:var(--text-sub)">加载失败</div>';
		}
	}

	if (tab === 'black') {
		try {
			const data = await api.getJson('/api/v1/security/ip-blacklist/');
			const items = data.rows || [];

			return `
        <div class="flex justify-between mb-16">
          <div class="text-sub text-sm">黑名单 IP 将被永久拒绝访问，登录失败 5 次自动封禁 15 分钟</div>
          <button class="btn btn-primary btn-sm" onclick="showAddBlacklist()">＋ 手动封禁</button>
        </div>
        <table class="table" style="border:1px solid var(--border);border-radius:var(--radius)">
          <thead><tr><th>IP 地址</th><th>封禁原因</th><th>操作人</th><th>封禁时间</th><th>解封时间</th><th style="width:120px">操作</th></tr></thead>
          <tbody>
            ${items.length === 0 ? '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-sub)">暂无黑名单</td></tr>' : items.map(x => `
              <tr>
                <td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip)}</code></td>
                <td>${escapeHtml(x.reason === 'login_fail' ? '登录连续失败' : (x.reason === 'manual' ? '人工封禁' : escapeHtml(x.reason || '-')))}</td>
                <td>${escapeHtml(x.detail || '系统自动')}</td>
                <td class="text-sub">${formatDate(x.created_at)}</td>
                <td class="text-sub">${x.expires_at ? formatDate(x.expires_at) : '<span class="tag tag-danger">永久</span>'}</td>
                <td><button class="btn-link btn-sm" onclick="unblockIp(${x.id})">解封</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>`;
		} catch (e) {
			console.error('load blacklist failed:', e);
			return '<div style="padding:20px;text-align:center;color:var(--text-sub)">加载失败</div>';
		}
	}

	if (tab === 'login') {
		try {
			let url = '/api/v1/security/login-attempts/';
			const resultFilter = window._loginResultFilter || '';
			if (resultFilter) url += '?result=' + encodeURIComponent(resultFilter);
			const data = await api.getJson(url);
			const items = data.rows || [];

			return `
        <div class="flex justify-between mb-16">
          <div class="text-sub text-sm">近 24 小时登录尝试记录，异常尝试将自动加入黑名单</div>
          <div class="flex gap-8">
            <select class="select" style="width:140px" onchange="filterLoginAttempts(this.value)">
              <option value="" ${!window._loginResultFilter ? 'selected' : ''}>全部结果</option><option value="success" ${window._loginResultFilter === 'success' ? 'selected' : ''}>成功</option><option value="wrong_password" ${window._loginResultFilter === 'wrong_password' ? 'selected' : ''}>失败</option>
            </select>
            <button class="btn btn-sm" onclick="loadLoginAttempts()">刷新</button>
          </div>
        </div>
        <table class="table" style="border:1px solid var(--border);border-radius:var(--radius)">
          <thead><tr><th>时间</th><th>用户</th><th>IP 地址</th><th>User-Agent</th><th>结果</th><th>失败原因</th></tr></thead>
          <tbody>
            ${items.length === 0 ? '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-sub)">暂无登录记录</td></tr>' : items.map(x => `
              <tr>
                <td class="text-sub">${formatDate(x.created_at)}</td>
                <td class="fw-500">${escapeHtml(x.username || '-')}</td>
                <td><code style="background:var(--hover);padding:2px 6px;border-radius:3px">${escapeHtml(x.ip)}</code></td>
                <td class="text-sub text-sm" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(x.user_agent || '-')}</td>
                <td>${x.result === 'success' ? '<span class="tag tag-success">✓ 成功</span>' : '<span class="tag tag-danger">✕ 失败</span>'}</td>
                <td class="text-sub">${escapeHtml(x.result === 'wrong_password' ? '密码错误' : (x.result === 'user_not_found' ? '用户不存在' : (x.result === 'locked' ? '账户锁定' : '-')))}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>`;
		} catch (e) {
			console.error('load login attempts failed:', e);
			return '<div style="padding:20px;text-align:center;color:var(--text-sub)">加载失败</div>';
		}
	}
	return '';
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
	$('#modal-audit-body').innerHTML = `
    <div class="grid-2" style="gap:8px 20px;font-size:13px">
      <div class="text-sub">时间</div><div>${formatDate(l.created_at)}</div>
      <div class="text-sub">用户</div><div>${escapeHtml(l.actor_username || '-')}</div>
      <div class="text-sub">操作</div><div>${escapeHtml(l.action || '-')}</div>
      <div class="text-sub">资源</div><div>${formatResource(l.target_type, l.target_id)}</div>
      <div class="text-sub">IP 地址</div><div><code>${escapeHtml(l.ip_address || '-')}</code></div>
      <div class="text-sub">结果</div><div>${escapeHtml(l.result || '-')}</div>
    </div>
    <div class="mt-16">
      <div class="text-sub text-sm mb-8">上下文 JSON</div>
      <pre style="background:#0f172a;color:#e2e8f0;padding:12px 14px;border-radius:var(--radius);font-family:'Courier New',monospace;font-size:12px;line-height:1.6;overflow-x:auto" id="audit-json-block">${escapeHtml(JSON.stringify(l, null, 2))}</pre>
    </div>`;
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
