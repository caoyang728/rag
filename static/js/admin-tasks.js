/* ==========================================================
   知库 Agent · 后台任务看板页面 (admin-tasks.js)
   功能：Celery 任务执行状态统计、队列深度、任务日志列表
   - 状态统计 + 队列深度来自 /api/v1/system/tasks/stats/
   - 任务日志列表来自 /api/v1/system/tasks/（支持状态/任务名过滤 + 分页）
   - 失败任务可一键重试（/tasks/<task_id>/retry/），重新派发不覆盖原记录
   - 每 30 秒自动刷新（页面不可见时跳过，避免无意义轮询）
   依赖：common.js（$/toast/showConfirmDialog/showModal/closeModal/escapeHtml/formatDate）、
        api.js、layout.js（isSystemMaintainer）
   ========================================================== */

// 分页状态缓存
let _state = { page: 1, pageSize: 50, total: 0 };
// 详情弹窗当前任务（供"重试此任务"按钮使用）
let _detailRow = null;
// 自动刷新间隔（毫秒）
const REFRESH_INTERVAL = 30000;

// 状态展示配置：文案 + 徽标样式类
const STATUS_META = {
	success: { text: '成功', cls: 'badge-success' },
	failure: { text: '失败', cls: 'badge-failure' },
	started: { text: '运行中', cls: 'badge-started' },
	pending: { text: '待执行', cls: 'badge-pending' },
	retry: { text: '重试中', cls: 'badge-retry' },
	revoked: { text: '已撤销', cls: 'badge-revoked' },
};


/* ============ 初始化 ============ */
document.addEventListener('DOMContentLoaded', async () => {
	// 权限检查：仅超级管理员 / 维护管理员可访问（与系统配置/定时任务页对齐）
	if (!isSystemMaintainer()) {
		const tmpl = document.getElementById('tmpl-no-permission');
		document.querySelector('.layout').innerHTML = tmpl.innerHTML;
		return;
	}
	// 首屏并行加载统计与列表，避免先后串行等待
	refreshAll();
	// 自动轮询：页面不可见时跳过，减少后台无谓请求
	setInterval(() => {
		if (document.visibilityState === 'visible') refreshAll();
	}, REFRESH_INTERVAL);
});

/* ============ 加载统计 + 队列深度 ============ */
async function loadStats() {
	try {
		const data = await api.getJson('/api/v1/system/tasks/stats/');
		const counts = data.counts || {};
		$('#statSuccess').textContent = counts.success ?? 0;
		$('#statFailure').textContent = counts.failure ?? 0;
		// 运行中 = started + pending（已派发但尚未结束的任务）
		$('#statRunning').textContent = (counts.started ?? 0) + (counts.pending ?? 0);
		$('#statTotal').textContent = data.counts_total || Object.values(counts).reduce((a, b) => a + (b || 0), 0);
		$('#statAvg').textContent = formatDuration(data.avg_duration_ms);
		$('#statMax').textContent = formatDuration(data.max_duration_ms);
		renderQueueDepth(data.queues || {});
	} catch (e) {
		// 统计失败不阻塞列表；静默降级，避免频繁弹错
		console.warn('任务统计加载失败:', e.message);
	}
}

/* ============ 渲染队列深度 ============ */
function renderQueueDepth(queues) {
	const wrap = $('#queueDepth');
	if (!wrap) return;
	const names = Object.keys(queues);
	if (!names.length) {
		wrap.innerHTML = '<div class="empty"><div class="empty-text">队列监控暂不可用（Redis 或监控任务未就绪）</div></div>';
		return;
	}
	wrap.innerHTML = names.map(name => {
		const q = queues[name] || {};
		const size = Number(q.size) || 0;
		// 水位条按 50 为满刻度归一化，超出则 100%（便于一眼看出堆积）
		const pct = Math.min(100, Math.round(size / 50 * 100));
		// 深度分档着色：<10 正常 / <50 偏高 / >=50 堆积告警
		const barCls = size >= 50 ? 'bar-warn' : size >= 10 ? 'bar-mid' : 'bar-ok';
		return `
			<div class="queue-item" title="${escapeHtml(name)}">
				<div class="queue-head">
					<span class="queue-name">${escapeHtml(name)}</span>
					<span class="queue-size">${size}</span>
				</div>
				<div class="queue-bar"><div class="queue-bar-fill ${barCls}" style="width:${pct}%"></div></div>
			</div>`;
	}).join('');
}

/* ============ 加载任务日志列表 ============ */
async function loadTasks() {
	const listEl = $('#taskList');
	if (!listEl) return;
	try {
		const params = new URLSearchParams({ page: _state.page, page_size: _state.pageSize });
		const status = $('#filterStatus')?.value;
		if (status) params.set('status', status);
		const taskName = $('#filterTaskName')?.value.trim();
		if (taskName) params.set('task_name', taskName);

		const data = await api.getJson('/api/v1/system/tasks/?' + params.toString());
		_state.total = data.total || 0;
		$('#taskCount').textContent = `（共 ${_state.total} 条）`;
		renderTasks(data.items || []);

		// 使用公共 Pagination 组件渲染分页
		const totalPages = Math.max(1, Math.ceil(_state.total / _state.pageSize));
		const pgnState = { page: _state.page, totalPages, total: _state.total, pageSize: _state.pageSize };
		if (_state.page > 1) {
			Pagination.update(pgnState);
		} else {
			Pagination.render({
				container: '#taskPagination',
				...pgnState,
				align: 'center',
				onPageChange: (p) => {
					_state.page = p;
					loadTasks();
				}
			});
		}
	} catch (e) {
		listEl.innerHTML = `<tr><td colspan="7"><div class="empty"><div class="empty-icon">❌</div><div class="empty-text">加载失败：${escapeHtml(e.message)}</div></div></td></tr>`;
	}
}

/* ============ 渲染任务表格 ============ */
function renderTasks(rows) {
	const listEl = $('#taskList');
	if (!listEl) return;
	if (!rows.length) {
		listEl.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="empty-icon">📭</div><div class="empty-text">暂无任务日志</div></div></td></tr>';
		return;
	}
	listEl.innerHTML = rows.map(r => {
		const meta = STATUS_META[r.status] || { text: r.status, cls: 'badge-pending' };
		// 任务名通常为模块路径较长，行内截断、悬停显示全名
		const actions = r.status === 'failure'
			? `<button class="btn btn-sm btn-outline" onclick="openDetail('${r.task_id}')">详情</button>
			   <button class="btn btn-sm" style="color:#fff;background:var(--danger,#e5484d)" onclick="retryTask('${r.task_id}')">重试</button>`
			: `<button class="btn btn-sm btn-outline" onclick="openDetail('${r.task_id}')">详情</button>`;
		return `
			<tr>
				<td class="task-name-cell" title="${escapeHtml(r.task_name)}">${escapeHtml(r.task_name)}</td>
				<td><span class="status-badge ${meta.cls}">${meta.text}</span></td>
				<td>${escapeHtml(r.queue)}</td>
				<td>${formatDuration(r.duration_ms)}</td>
				<td>${formatDate(r.started_at || r.created_at)}</td>
				<td>${r.retry_count || 0}</td>
				<td class="task-actions-cell">${actions}</td>
			</tr>`;
	}).join('');
}

/* ============ 详情弹窗 ============ */
async function openDetail(taskId) {
	try {
		// 详情接口与列表共用（带 task_id 精确过滤，取第一条即为目标记录）
		const data = await api.getJson(`/api/v1/system/tasks/?task_id=${encodeURIComponent(taskId)}&page_size=1`);
		const row = (data.items || [])[0];
		if (!row) {
			toast('任务记录不存在', 'error');
			return;
		}
		_detailRow = row;
		const meta = STATUS_META[row.status] || { text: row.status, cls: 'badge-pending' };
		const isFailure = row.status === 'failure';
		// 详情弹窗的"重试"按钮仅失败任务可用，先重置再按需显示
		$('#detailRetryBtn').classList.toggle('hidden', !isFailure);
		$('#taskDetailBody').innerHTML = `
			<div class="detail-grid">
				<div class="detail-item"><div class="detail-label">任务名</div><div class="detail-value">${escapeHtml(row.task_name)}</div></div>
				<div class="detail-item"><div class="detail-label">task_id</div><div class="detail-value mono">${escapeHtml(row.task_id)}</div></div>
				<div class="detail-item"><div class="detail-label">状态</div><div class="detail-value"><span class="status-badge ${meta.cls}">${meta.text}</span></div></div>
				<div class="detail-item"><div class="detail-label">队列</div><div class="detail-value">${escapeHtml(row.queue)}</div></div>
				<div class="detail-item"><div class="detail-label">耗时</div><div class="detail-value">${formatDuration(row.duration_ms)}</div></div>
				<div class="detail-item"><div class="detail-label">重试次数</div><div class="detail-value">${row.retry_count || 0}</div></div>
			</div>
			<div class="detail-block"><div class="detail-label">开始时间</div><div class="detail-value">${formatDate(row.started_at)}</div></div>
			<div class="detail-block"><div class="detail-label">结束时间</div><div class="detail-value">${formatDate(row.finished_at)}</div></div>
			<div class="detail-block"><div class="detail-label">参数 args</div><pre class="detail-pre">${escapeHtml(safeJson(row.args))}</pre></div>
			<div class="detail-block"><div class="detail-label">参数 kwargs</div><pre class="detail-pre">${escapeHtml(safeJson(row.kwargs))}</pre></div>
			<div class="detail-block"><div class="detail-label">执行结果 result</div><pre class="detail-pre">${escapeHtml(row.result || '-')}</pre></div>
			${isFailure ? `<div class="detail-block"><div class="detail-label" style="color:var(--danger,#e5484d)">错误信息</div><pre class="detail-pre detail-error">${escapeHtml(row.error_message || '-')}</pre></div>` : ''}
		`;
		showModal('taskDetailModal');
	} catch (e) {
		toast('加载任务详情失败：' + e.message, 'error');
	}
}

/* ============ 详情弹窗内重试 ============ */
function retryFromDetail() {
	if (!_detailRow) return;
	closeModal('taskDetailModal');
	retryTask(_detailRow.task_id);
}

/* ============ 失败任务重试 ============ */
function retryTask(taskId) {
	showConfirmDialog({
		title: '重试任务',
		bannerText: '将以相同参数重新派发该任务（生成新的 task_id，不影响原记录）',
		bannerType: 'danger',
		bodyHtml: '<div class="confirm-detail">' + escapeHtml(taskId) + '</div>',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{ text: '确认重试', type: 'danger', onClick: async (ctx) => {
				try {
					await api.postJson(`/api/v1/system/tasks/${encodeURIComponent(taskId)}/retry/`, {});
					ctx.close();
					toast('已重新派发，新任务执行后自动入库', 'success');
					refreshAll();
				} catch (e) {
					ctx.setError(e.message);
				}
			} }
		]
	});
}

/* ============ 过滤条件变化时重置到第一页再加载 ============ */
function resetPageLoad() {
	_state.page = 1;
	loadTasks();
}

/* ============ 全量刷新（统计 + 列表） ============ */
function refreshAll() {
	loadStats();
	loadTasks();
}

/* ============ 工具函数 ============ */
// 耗时格式化：不足 1s 显示毫秒，否则显示秒（保留 2 位小数）
function formatDuration(ms) {
	const v = Number(ms) || 0;
	if (v < 1000) return v + ' ms';
	return (v / 1000).toFixed(2) + ' s';
}

// JSON 安全展示：解析失败时原样返回字符串
function safeJson(value) {
	if (value == null) return '-';
	if (typeof value === 'string') return value;
	try { return JSON.stringify(value, null, 2); } catch (e) { return String(value); }
}
