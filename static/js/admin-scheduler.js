/* ==========================================================
   知库 Agent · 定时任务调度配置页面 (admin-scheduler.js)
   功能：加载 Beat 定时任务清单、按 cron 分字段展示、编辑调度时间/启停
   审批流：修改调度需提交变更工单（高风险项需"审核 + 超管复核"），
   审批通过后由 SystemConfigScheduler 热更新（≤30s 生效），无需重启 beat。
   依赖：common.js（showModal/showConfirmDialog/toast/escapeHtml）、api.js、layout.js
   ========================================================== */

// 当前任务清单（缓存，供编辑弹窗回显与工单列表过滤使用）
let _tasks = [];
// 当前编辑中的任务（供弹窗回显与"值未变化"判断）
let _editingTask = null;
// cron 字段中文名与取值范围，编辑弹窗提示用
const CRON_FIELDS = [
	{ key: 'minute', label: '分', range: '0-59' },
	{ key: 'hour', label: '时', range: '0-23' },
	{ key: 'day_of_month', label: '日', range: '1-31' },
	{ key: 'month', label: '月', range: '1-12' },
	{ key: 'day_of_week', label: '周', range: '0-6 (0=周日)' },
];
// 各段取值范围，与后端 scheduler_registry._CRON_RANGES 一致，前端先行校验
const CRON_RANGES = { minute: [0, 59], hour: [0, 23], day_of_month: [1, 31], month: [1, 12], day_of_week: [0, 6] };


/* ============ 初始化 ============ */
document.addEventListener('DOMContentLoaded', async () => {
	// 权限检查：仅超级管理员 / 维护管理员可访问
	if (!isSystemMaintainer()) {
		const tmpl = document.getElementById('tmpl-no-permission');
		document.querySelector('.layout').innerHTML = tmpl.innerHTML;
		return;
	}
	initEditModal();
	await loadTasks();
});

/* ============ 加载任务清单 ============ */
async function loadTasks() {
	const listEl = $('#taskList');
	if (!listEl) return;
	try {
		const data = await api.getJson('/api/v1/system/scheduler/tasks/');
		_tasks = data.tasks || [];
		$('#taskCount').textContent = `（${data.total || _tasks.length} 个任务）`;
		renderTasks();
		// 忙闲视图依赖任务清单（cron 分字段 + 预估工时），任务加载完成后一并渲染
		renderBusyViews();
	} catch (e) {
		listEl.innerHTML = `<div class="empty"><div class="empty-icon">❌</div><div class="empty-text">加载失败：${escapeHtml(e.message)}</div></div>`;
	}
}

/* ============ 渲染任务卡片列表 ============ */
function renderTasks() {
	const listEl = $('#taskList');
	if (!listEl) return;
	if (_tasks.length === 0) {
		listEl.innerHTML = '<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">暂无定时任务</div></div>';
		return;
	}
	const tmpl = document.getElementById('tmpl-task-item').innerHTML;
	listEl.innerHTML = _tasks.map(t => {
		const keyEscaped = escapeHtml(t.key);
		// 待审批工单 badge：有未完成工单时提示，点击打开公共工单中心（待我处理视图）
		const pendingBadge = t.pending_ticket_count > 0
			? `<span class="task-pending-badge" onclick="TicketCenter.open({ defaultView: 'todo' })" title="该任务有待审批工单">⏳ 待审批 ${t.pending_ticket_count}</span>`
			: '';
		// cron 分字段展示：每段一个灰底小框，未启用时整体置灰
		const cronHtml = CRON_FIELDS.map(f =>
			`<span class="cron-field"><em>${f.label}</em>${escapeHtml(t.cron_fields[f.key] || '*')}</span>`
		).join('');
		// humanize 中文解释：把 cron 翻译成人话（如"每天 02:00 执行一次"），
		// 与任务描述解耦——desc 只讲"做什么"，cron 区讲"什么时候做"
		const humanized = t.humanized || humanizeCron(t.cron);
	return tmpl
		.replace(/__KEY_ATTR__/g, keyEscaped)
		.replace(/__KEY_ESC__/g, keyEscaped)
		.replace(/__NAME_ATTR__/g, escapeHtml(t.name))
		.replace('__LABEL__', escapeHtml(t.label))
		.replace('__PENDING_BADGE__', pendingBadge)
		.replace('__DESC__', escapeHtml(t.description || ''))
		.replace('__CRON_HTML__', cronHtml)
		.replace('__HUMANIZED__', escapeHtml(humanized))
		.replace('__STATUS_CLASS__', t.enabled ? 'task-status-on' : 'task-status-off')
		.replace('__STATUS_TEXT__', t.enabled ? '运行中' : '已停用');
	}).join('');
}

/* ============ 打开编辑弹窗（common.js 弹窗框架）============
 * 使用 HTML 中的 #editTaskModal（showModal/closeModal），内容布局与原先一致：
 * 5 个 cron 字段输入 + 实时解释 + 启停开关 + 变更原因。
 * 打开时回显当前值，随后由 updateEditFormState 实时校验并控制提交按钮。
 */
function openEditModal(name) {
	const task = _tasks.find(x => x.name === name);
	if (!task) {
		toast('任务不存在', 'error');
		return;
	}
	_editingTask = task;
	$('#editTaskLabel').textContent = task.label;
	$('#editTaskBanner').textContent = `调度键：${task.key}（高风险项，工单需复核）`;
	// 回显 cron 分字段（预设当前值，便于微调）
	CRON_FIELDS.forEach(f => {
		const input = document.getElementById(`cron-${f.key}`);
		if (input) input.value = task.cron_fields[f.key] || '*';
	});
	const toggle = $('#cron-enabled');
	if (toggle) toggle.checked = task.enabled;
	$('#cronEnabledHint').textContent = task.enabled ? '停用后任务将不再触发' : '启用后按新调度时间触发';
	$('#ticketReasonInput').value = '';
	$('#editTaskErr').classList.add('hidden');
	updateEditFormState();
	showModal('editTaskModal');
	const reason = $('#ticketReasonInput');
	if (reason) reason.focus();
}

/* ============ 关闭编辑弹窗 ============ */
function closeEditModal() {
	closeModal('editTaskModal');
}

/* ============ 初始化编辑弹窗：生成 cron 字段输入框并绑定实时刷新 ============ */
function initEditModal() {
	const row = $('#cronFieldsRow');
	if (row) {
		row.innerHTML = CRON_FIELDS.map(f => `
			<div class="cron-form-item">
				<label class="cron-form-label">${f.label}<span class="cron-form-range">${f.range}</span></label>
				<input type="text" class="input cron-form-input" id="cron-${f.key}" value="*" placeholder="*" autocomplete="off">
			</div>`).join('');
	}
	CRON_FIELDS.forEach(f => {
		const input = document.getElementById(`cron-${f.key}`);
		if (input) input.addEventListener('input', updateEditFormState);
	});
	const toggle = document.getElementById('cron-enabled');
	if (toggle) toggle.addEventListener('change', updateEditFormState);
}

/* ============ 刷新表达式预览 / 中文解释 / 错误提示 / 提交按钮状态 ============
 * 任一 cron 字段输入或启停开关变化时触发：
 * - 表达式不合法 → 红色提示"表达式错误" + 禁用提交按钮
 * - 合法但值未变化（cron 与启停均一致）→ 禁用提交按钮，避免无效工单
 * - 合法且有变化 → 展示中文解释，可提交
 */
function updateEditFormState() {
	const preview = $('#cronPreviewText');
	if (preview) preview.textContent = buildRawCron();
	const explain = $('#cronExplainText');
	const submitBtn = $('#editSubmitBtn');
	if (!_editingTask || !submitBtn) return;
	const cron = buildCronFromInputs();
	if (cron === null) {
		if (explain) {
			explain.textContent = '表达式错误';
			explain.classList.add('cron-explain-error');
		}
		submitBtn.disabled = true;
		return;
	}
	if (explain) {
		explain.classList.remove('cron-explain-error');
		explain.textContent = humanizeCron(cron);
	}
	const enabled = $('#cron-enabled').checked;
	submitBtn.disabled = (cron === _editingTask.cron && enabled === _editingTask.enabled);
}

/* ============ 提交调度变更工单（校验 + 值变化检查兜底）============ */
async function submitEditTicket() {
	const task = _editingTask;
	if (!task) return;
	const cron = buildCronFromInputs();
	if (cron === null) {
		setEditError('cron 表达式不合法，请检查各字段取值范围');
		return;
	}
	const reason = $('#ticketReasonInput').value.trim();
	if (!reason) {
		setEditError('请填写变更原因');
		return;
	}
	const enabled = $('#cron-enabled').checked;
	// 值未变化时不创建工单（按钮已禁用，此处兜底防止绕过 UI）
	if (cron === task.cron && enabled === task.enabled) {
		setEditError('调度时间与启停状态均未变化，无需提交工单');
		return;
	}
	try {
		await api.postJson('/api/v1/system/tickets/', {
			ticket_type: 'schedule',
			config_key: task.key,
			new_value: JSON.stringify({ cron, enabled }),
			reason: reason,
		});
		closeEditModal();
		toast('工单已提交，等待审批（高风险需复核）', 'success');
		await loadTasks();
	} catch (e) {
		setEditError(`提交失败：${e.message}`);
	}
}

/* ============ 在编辑弹窗底部展示错误提示 ============ */
function setEditError(msg) {
	const errEl = $('#editTaskErr');
	if (errEl) {
		errEl.textContent = msg;
		errEl.classList.toggle('hidden', !msg);
	}
}

/* ============ 从输入框拼装并校验 cron 表达式 ============
 * Returns: 合法的 5 段 cron 字符串；非法返回 null
 */
function buildCronFromInputs() {
	const parts = [];
	for (const f of CRON_FIELDS) {
		const el = document.getElementById(`cron-${f.key}`);
		const val = el ? el.value.trim() : '';
		const range = CRON_RANGES[f.key];
		if (!validateCronField(val, range[0], range[1])) {
			return null;
		}
		parts.push(val || '*');
	}
	return parts.join(' ');
}

/* ============ 拼装 cron 表达式（不做校验，仅用于预览展示）============ */
function buildRawCron() {
	return CRON_FIELDS.map(f => {
		const el = document.getElementById(`cron-${f.key}`);
		return el ? (el.value.trim() || '*') : '*';
	}).join(' ');
}

/* ============ 校验 cron 单段字段 ============
 * 支持 *、固定值、区间(a-b)、步长（斜杠 n 前缀形式）及逗号组合；
 * 与后端 scheduler_registry._validate_field 的语义保持一致（前端先行提示）。
 */
function validateCronField(value, lo, hi) {
	if (!value) return true; // 空值按 * 处理
	for (const part of value.split(',')) {
		const p = part.trim();
		if (!p) return false;
		if (/[^0-9*,\-/]/.test(p)) return false; // 非法字符
		let base = p;
		if (p.includes('/')) {
			const seg = p.split('/');
			if (seg.length !== 2 || !/^\d+$/.test(seg[1]) || parseInt(seg[1], 10) < 1) return false;
			base = seg[0];
		}
		if (base === '*') continue;
		if (base.includes('-')) {
			const seg = base.split('-');
			if (seg.length !== 2) return false;
			const a = parseInt(seg[0], 10), b = parseInt(seg[1], 10);
			if (isNaN(a) || isNaN(b)) return false;
			if (!(lo <= a && a <= b && b <= hi)) return false;
		} else {
			if (!/^\d+$/.test(base)) return false;
			const v = parseInt(base, 10);
			if (isNaN(v) || v < lo || v > hi) return false;
		}
	}
	return true;
}

/* ============ cron 中文解释（与后端 scheduler_registry.humanize_cron 语义一致）
 * 把 5 段 cron 翻译成人话，如"0 2 * * *" → "每天 02:00 执行一次"；
 * 无法归类的复杂表达式原样返回 cron，保证展示不丢失信息。
 */
const WEEKDAY_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function isFixedField(v) { return /^\d+$/.test(v); }
function isStepField(v) { return v.includes('/'); }
function stepValue(v) { return parseInt(v.split('/')[1], 10); }
function fmtHHMM(hour, minute) {
	return String(parseInt(hour, 10)).padStart(2, '0') + ':' + String(parseInt(minute, 10)).padStart(2, '0');
}
function fmtWeekdays(dow) {
	// 周字段 → 中文星期列表（支持固定值/区间/逗号列表）
	const names = [];
	for (const item of dow.split(',')) {
		const p = item.trim();
		if (p.includes('-')) {
			const [a, b] = p.split('-');
			for (let i = parseInt(a, 10); i <= parseInt(b, 10); i++) names.push(WEEKDAY_NAMES[i % 7]);
		} else if (p !== '*') {
			names.push(WEEKDAY_NAMES[parseInt(p, 10) % 7]);
		}
	}
	return names.length ? names.join('、') : dow;
}
function humanizeCron(cron) {
	const fields = String(cron || '').trim().split(/\s+/);
	if (fields.length !== 5) return String(cron || '');
	const [minute, hour, dom, month, dow] = fields;
	// 每 N 分钟：*/N * * * *
	if (isStepField(minute) && hour === '*' && dom === '*' && month === '*' && dow === '*')
		return `每 ${stepValue(minute)} 分钟执行一次`;
	// 每天 H 点内每 N 分钟：*/N H * * *
	if (isStepField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow === '*')
		return `每天 ${String(parseInt(hour, 10)).padStart(2, '0')} 点内每 ${stepValue(minute)} 分钟执行一次`;
	// 每周 X 点内每 N 分钟：*/N H * * DOW
	if (isStepField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow !== '*')
		return `每周${fmtWeekdays(dow)} ${String(parseInt(hour, 10)).padStart(2, '0')} 点内每 ${stepValue(minute)} 分钟执行一次`;
	// 每 N 小时（整点）：0 */N * * *
	if (minute === '0' && isStepField(hour) && dom === '*' && month === '*' && dow === '*')
		return `每 ${stepValue(hour)} 小时执行一次`;
	// 每 N 小时的第 M 分钟：M */N * * *
	if (isFixedField(minute) && isStepField(hour) && dom === '*' && month === '*' && dow === '*')
		return `每 ${stepValue(hour)} 小时的第 ${parseInt(minute, 10)} 分钟执行一次`;
	// 每小时的第 M 分钟：M * * * *
	if (isFixedField(minute) && hour === '*' && dom === '*' && month === '*' && dow === '*')
		return `每小时的第 ${parseInt(minute, 10)} 分钟执行一次`;
	// 每天固定时间：M H * * *
	if (isFixedField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow === '*')
		return `每天 ${fmtHHMM(hour, minute)} 执行一次`;
	// 每周：M H * * DOW
	if (isFixedField(minute) && isFixedField(hour) && dom === '*' && month === '*' && dow !== '*')
		return `每周${fmtWeekdays(dow)} ${fmtHHMM(hour, minute)} 执行一次`;
	// 每月：M H D * *
	if (isFixedField(minute) && isFixedField(hour) && isFixedField(dom) && month === '*' && dow === '*')
		return `每月 ${parseInt(dom, 10)} 日 ${fmtHHMM(hour, minute)} 执行一次`;
	// 每年：M H D MO *
	if (isFixedField(minute) && isFixedField(hour) && isFixedField(dom) && isFixedField(month) && dow === '*')
		return `每年 ${parseInt(month, 10)} 月 ${parseInt(dom, 10)} 日 ${fmtHHMM(hour, minute)} 执行一次`;
	// 每年固定日期 + 星期限定：M H D MO DOW（如 "0 2 1 1 1" → 每年 1 月 1 日且为周一）
	if (isFixedField(minute) && isFixedField(hour) && isFixedField(dom) && isFixedField(month) && isFixedField(dow))
		return `每年 ${parseInt(month, 10)} 月 ${parseInt(dom, 10)} 日 且为${fmtWeekdays(dow)} ${fmtHHMM(hour, minute)} 执行一次`;
	// 兜底：保留原始 cron，避免复杂表达式被错误简化
	return `cron 表达式：${cron}`;
}

/* ==========================================================
   忙闲视图：Outlook 风格日程（周视图 / 日视图）
   根据各任务 cron 分字段 + estimated_minutes（含 20% 缓冲）在日历上按时间段摆放任务块：
   - 周视图：周一~周日 7 列 × 24 小时时间轴，任务块按起止时间着色摆放
   - 日视图：选中星期后展示该天 24 小时时间轴，任务块 + 时段明细
   每个任务使用固定颜色（按 name 哈希取色），重叠时段并排展示（类似 Outlook 日程）。
   步长/每小时类任务全天运行，纳入会让所有时段都变忙、失去错峰参考意义，故不展示；
   每月/每年固定日期任务无法确定落在周内哪天，同样不纳入（当前注册表无此类任务）。
   ========================================================== */
// 展示顺序：周一~周日；cron 周字段 0=周日，映射为数组下标对应 _BUSY_CRON_DAY_ORDER
const _BUSY_DAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const _BUSY_CRON_DAY_ORDER = [1, 2, 3, 4, 5, 6, 0];
let _busyView = 'week'; // 当前视图（week/day）
let _busyDayIndex = 0;  // 日视图选中的星期（0=周一）

/* ============ 切换任务调度 / 忙闲视图 sheet ============ */
function switchSheet(sheet) {
	$$('.sheet-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.sheet === sheet));
	const tasksEl = $('#sheet-tasks');
	const busyEl = $('#sheet-busy');
	if (tasksEl) tasksEl.classList.toggle('hidden', sheet !== 'tasks');
	if (busyEl) busyEl.classList.toggle('hidden', sheet !== 'busy');
	if (sheet === 'busy') renderBusyViews();
}

/* ============ 切换周视图 / 日视图 ============ */
function switchBusyView(view) {
	_busyView = view;
	$$('.busy-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.view === view));
	const weekEl = $('#busyWeekView');
	const dayEl = $('#busyDayView');
	if (weekEl) weekEl.classList.toggle('hidden', view !== 'week');
	if (dayEl) dayEl.classList.toggle('hidden', view !== 'day');
	if (view === 'day') renderBusyDay();
}

/* ============ 渲染周视图与日视图（任务清单加载后调用）============ */
function renderBusyViews() {
	renderBusyWeek();
	if (_busyView === 'day') renderBusyDay();
}

/* ============ 把 cron 周字段解析为"周内星期值列表"（0=周日）============ */
function parseCronDowList(value) {
	const days = [];
	for (const item of String(value).split(',')) {
		const p = item.trim();
		if (p === '*') continue;
		if (p.includes('-')) {
			const [a, b] = p.split('-');
			for (let i = parseInt(a, 10); i <= parseInt(b, 10); i++) days.push(i % 7);
		} else {
			days.push(parseInt(p, 10) % 7);
		}
	}
	return days;
}

/* ============ 计算单个任务的忙碌信息 ============
 * TODO: 预估工时后续基于近一周/一个月实际执行耗时均值 + 10% 余量动态估算，
 *       替代当前静态的 estimated_minutes（当前仅作展示用估算）。
 * Returns: { days, startMin, endMin } 或 null（不纳入视图）
 *   - days: 生效的 cron 星期值列表（0=周日）
 *   - startMin/endMin: 当天内的起止分钟（含 20% 缓冲）
 * 不纳入视图的任务：
 *   - 步长/每小时类任务（如每 5 分钟、每小时、每 2 小时）近似全天运行，
 *     纳入会让所有时段都变忙、失去错峰参考意义，故直接排除
 *   - 每月/每年固定日期任务无法确定落在周内哪天
 */
function computeTaskBusy(task) {
	const durMin = Math.ceil((task.estimated_minutes || 0) * 1.2); // 预估工时 + 20% 缓冲
	if (!durMin) return null;
	const f = task.cron_fields || {};
	// 步长（*/N）或每小时执行（15 * * * *）类任务全天运行，不纳入视图
	if (isStepField(f.minute) || isStepField(f.hour) || f.hour === '*') return null;
	if (!isFixedField(f.minute) || !isFixedField(f.hour)) return null;
	const startMin = parseInt(f.hour, 10) * 60 + parseInt(f.minute, 10);
	const endMin = Math.min(24 * 60, startMin + durMin);
	let days;
	if (f.day_of_week !== '*') {
		days = parseCronDowList(f.day_of_week);
	} else if (f.day_of_month !== '*' || f.month !== '*') {
		return null; // 每月/每年固定日期：无法确定落在周内哪天
	} else {
		days = [0, 1, 2, 3, 4, 5, 6]; // 每天执行
	}
	return { days, startMin, endMin };
}

/* ============ 分钟 → HH:MM ============ */
function fmtMin(m) {
	return String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
}

/* ============ 任务固定配色（按 name 哈希取色，同一任务始终同色）============ */
const _BUSY_COLORS = ['#4f46e5', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316', '#6366f1'];

function taskColor(name) {
	let h = 0;
	for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
	return _BUSY_COLORS[h % _BUSY_COLORS.length];
}

/* ============ 排布一天内的任务块（智能重叠处理，类似 Outlook 日程）============
 * 输入需按 startMin 升序。
 * 使用区间图着色算法：将时间上有重叠的任务组成独立的"冲突组"，
 * 每个组内根据最大重叠数计算列数，同组任务按泳道错开排列，
 * 不同组的任务互不影响，可以各自占据整列宽度。
 */
function layoutDayBlocks(blocks) {
	if (blocks.length === 0) return [];

	// 第 1 步：将任务划分为独立的冲突组
	// 定义：如果两个任务时间重叠，则它们属于同一组
	const groups = [];
	const visited = new Set();

	for (let i = 0; i < blocks.length; i++) {
		if (visited.has(i)) continue;
		// BFS 找到所有与当前任务（直接或间接）重叠的任务
		const group = [i];
		visited.add(i);
		let queue = [i];
		while (queue.length > 0) {
			const curr = queue.shift();
			for (let j = 0; j < blocks.length; j++) {
				if (visited.has(j)) continue;
				// 检查是否与当前组任务重叠
				if (blocks[curr].startMin < blocks[j].endMin && blocks[j].startMin < blocks[curr].endMin) {
					visited.add(j);
					group.push(j);
					queue.push(j);
				}
			}
		}
		groups.push(group.sort((a, b) => blocks[a].startMin - blocks[b].startMin));
	}

	// 第 2 步：对每个组进行列分配
	const result = [];
	for (const group of groups) {
		const groupBlocks = group.map(i => blocks[i]);

		// 计算组内任意时刻的最大重叠数（即所需列数）
		// 使用 sweep-line 算法
		const events = [];
		for (const b of groupBlocks) {
			events.push({ time: b.startMin, type: 'start', idx: groupBlocks.indexOf(b) });
			events.push({ time: b.endMin, type: 'end', idx: groupBlocks.indexOf(b) });
		}
		events.sort((a, b) => a.time - b.time || (a.type === 'end' ? -1 : 1));

		let maxOverlap = 0;
		let currentOverlap = 0;
		const activeSet = new Set();
		for (const e of events) {
			if (e.type === 'start') {
				activeSet.add(e.idx);
				currentOverlap = activeSet.size;
				maxOverlap = Math.max(maxOverlap, currentOverlap);
			} else {
				activeSet.delete(e.idx);
			}
		}

		// 如果最大重叠为 1，所有任务各占整列
		if (maxOverlap <= 1) {
			for (const idx of group) {
				result.push({
					b: blocks[idx],
					lane: 0,
					width: 100,
					left: 0
				});
			}
			continue;
		}

		// 最大重叠 > 1，需要分配泳道
		const laneCount = maxOverlap;
		const width = 100 / laneCount;

		// 贪心算法分配泳道：按开始时间排序，每个任务分配第一个可用的泳道
		const laneEndTimes = new Array(laneCount).fill(0);
		const sortedGroup = group.map(i => ({ idx: i, block: blocks[i] }))
			.sort((a, b) => a.block.startMin - b.block.startMin);

		for (const { idx, block } of sortedGroup) {
			// 找到第一个可用的泳道
			let lane = 0;
			while (lane < laneCount && laneEndTimes[lane] > block.startMin) {
				lane++;
			}
			if (lane >= laneCount) lane = 0; // 理论上不会发生

			laneEndTimes[lane] = block.endMin;

			result.push({
				b: blocks[idx],
				lane,
				width,
				left: lane * width
			});
		}
	}

	// 保持与输入 blocks 相同的顺序
	return result.sort((a, b) => {
		const ai = blocks.indexOf(a.b);
		const bi = blocks.indexOf(b.b);
		return ai - bi;
	});
}

/* ============ 渲染周视图（Outlook 风格：7 天 × 24 小时时间轴，15 分钟一格）============ */
function renderBusyWeek() {
	const container = $('#busyWeekView');
	if (!container) return;
	// 收集每个星期当天的任务块
	const dayBlocks = [0, 1, 2, 3, 4, 5, 6].map(() => []);
	_tasks.forEach(t => {
		const b = computeTaskBusy(t);
		if (!b) return;
		for (const d of b.days) dayBlocks[d].push({ t, startMin: b.startMin, endMin: b.endMin });
	});
	const pct = m => (m / (24 * 60)) * 100;
	let html = `<div class="cal-week">`;
	html += `<div class="cal-corner">时/日</div>`;
	for (const label of _BUSY_DAY_LABELS) html += `<div class="cal-day-head">${label}</div>`;
	// 24 小时背景格线（每小时 4 格 = 96 行），时间标签仅在整点显示
	for (let h = 0; h < 24; h++) {
		for (let q = 0; q < 4; q++) {
			html += q === 0
				? `<div class="cal-time">${String(h).padStart(2, '0')}:00</div>`
				: `<div class="cal-time cal-time-quarter"></div>`;
			html += q === 0
				? `<div class="cal-hour-cell cal-hour-mark"></div>`
				: `<div class="cal-hour-cell cal-hour-quarter"></div>`;
			for (let d = 1; d < 7; d++) html += `<div class="cal-hour-cell"></div>`;
		}
	}
	// 每个星期列叠加任务块（绝对定位，重叠并排）
	for (let di = 0; di < 7; di++) {
		const d = _BUSY_CRON_DAY_ORDER[di]; // 该列对应的 cron 周值（0=周日）
		const list = dayBlocks[d].sort((a, b) => a.startMin - b.startMin);
		// 在 style 中使用 calc，但直接替换 --di 变量为数值，避免兼容性问题
		html += `<div class="cal-day-body" style="left: calc(56px + (100% - 56px) * ${di} / 7); width: calc((100% - 56px) / 7)">`;
		layoutDayBlocks(list).forEach(p => {
			const color = taskColor(p.b.t.name);
			const durMin = p.b.endMin - p.b.startMin;
			const heightPct = pct(durMin);
			const minHeightPct = Math.max(0.5, heightPct);
			// 高度阈值：≥ 3%（约 43 分钟）显示名称+时间两行，否则只显示名称
			const showDuration = heightPct >= 3;
			html += `<div class="cal-block" style="top:${pct(p.b.startMin)}%;height:${minHeightPct}%;left:${p.left}%;width:${p.width}%;background:${color}" title="${escapeHtml(p.b.t.label)}（${fmtMin(p.b.startMin)} - ${fmtMin(p.b.endMin)}，${durMin} 分钟）">`;
			html += `<span class="cal-block-name">${escapeHtml(p.b.t.label)}</span>`;
			if (showDuration) {
				html += `<span class="cal-block-duration">${durMin} 分钟</span>`;
			}
			html += `</div>`;
		});
		html += `</div>`;
	}
	html += `</div>`;
	container.innerHTML = html;
}

/* ============ 渲染日视图（Outlook 风格：选中星期的 24 小时时间轴 + 时段明细）============ */
function renderBusyDay() {
	const container = $('#busyDayView');
	if (!container) return;
	const dow = _BUSY_CRON_DAY_ORDER[_busyDayIndex]; // 当前选中星期的 cron 周值
	// 收集该天所有任务块（全天运行类任务已被 computeTaskBusy 排除）
	const blocks = [];
	_tasks.forEach(t => {
		const b = computeTaskBusy(t);
		if (!b) return;
		if (b.days.includes(dow)) blocks.push({ t, startMin: b.startMin, endMin: b.endMin });
	});
	blocks.sort((a, b) => a.startMin - b.startMin);
	const pct = m => (m / (24 * 60)) * 100;
	// 星期选择
	let html = `<div class="busy-day-select">`;
	_BUSY_DAY_LABELS.forEach((label, i) => {
		html += `<button class="busy-day-btn ${i === _busyDayIndex ? 'active' : ''}" onclick="selectBusyDay(${i})">${label}</button>`;
	});
	html += `</div>`;
	// 24 小时时间轴：15 分钟一个格线（共 96 行，每小时 4 行），时间标签仍按 1 小时间隔展示
	html += `<div class="cal-day-grid"><div class="cal-corner"></div><div class="cal-day-head">${_BUSY_DAY_LABELS[_busyDayIndex]}</div>`;
	for (let h = 0; h < 24; h++) {
		for (let q = 0; q < 4; q++) {
			// 每小时第 1 行显示整点标签，其余 3 行为 15 分钟格线留白
			html += q === 0
				? `<div class="cal-time">${String(h).padStart(2, '0')}:00</div>`
				: `<div class="cal-time cal-time-quarter"></div>`;
			html += q === 0
				? `<div class="cal-hour-cell cal-hour-mark"></div>`
				: `<div class="cal-hour-cell cal-hour-quarter"></div>`;
		}
	}
	html += `<div class="cal-day-body">`;
	layoutDayBlocks(blocks).forEach(p => {
		const color = taskColor(p.b.t.name);
		const durMin = p.b.endMin - p.b.startMin;
		const heightPct = pct(durMin);
		const minHeightPct = Math.max(0.5, heightPct);
		// 高度阈值：≥ 3%（约 43 分钟）显示名称+时间两行，否则只显示名称
		const showDuration = heightPct >= 3;
		html += `<div class="cal-block" style="top:${pct(p.b.startMin)}%;height:${minHeightPct}%;left:${p.left}%;width:${p.width}%;background:${color}" title="${escapeHtml(p.b.t.label)}（${fmtMin(p.b.startMin)} - ${fmtMin(p.b.endMin)}，${durMin} 分钟）">`;
		html += `<span class="cal-block-name">${escapeHtml(p.b.t.label)}</span>`;
		if (showDuration) {
			html += `<span class="cal-block-duration">${durMin} 分钟</span>`;
		}
		html += `</div>`;
	});
	html += `</div></div>`;
	// 时段明细（可读性兜底：任务块太小时也能看清时间段）
	if (blocks.length === 0) {
		html += `<div class="busy-day-empty">${_BUSY_DAY_LABELS[_busyDayIndex]}无定时任务</div>`;
	} else {
		html += `<div class="busy-day-list">`;
		blocks.forEach(b => {
			const durMin = b.endMin - b.startMin;
			html += `<div class="busy-day-item"><span class="busy-day-dot" style="background:${taskColor(b.t.name)}"></span><span class="busy-day-time">${fmtMin(b.startMin)} - ${fmtMin(b.endMin)}</span><span class="busy-day-name">${escapeHtml(b.t.label)}</span><span class="busy-day-duration">（${durMin} 分钟）</span></div>`;
		});
		html += `</div>`;
	}
	container.innerHTML = html;
}

/* ============ 切换日视图选中的星期 ============ */
function selectBusyDay(i) {
	_busyDayIndex = i;
	renderBusyDay();
}
