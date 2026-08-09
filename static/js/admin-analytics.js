/* ============ 反馈与准确率报表 ============ */

let currentTimeRange = 'week';
let showAccuracy = true;
let showTtft = true;       // 首字耗时（仅非缓存命中）
let showTotal = true;      // 整体总耗时（仅非缓存命中）
let customDateStart = null;
let customDateEnd = null;

let currentTab = 'overview';
let qaPage = 1;
let qaPageSize = 20;
let qaTotal = 0;

/* 日报趋势图的指标显示开关和缓存数据，勾选 checkbox 时更新开关并重渲染 */
let dailyTrendData = [];
let dailyMetricVisible = { qa: true, good: true, bad: true, accuracy: true };

document.addEventListener('DOMContentLoaded', () => {
	initAnalyticsPage();
});

async function initAnalyticsPage() {
	await loadRootTypes();
	// 默认日期预填（昨日 = 系统报表通常已就绪）
	const yesterday = new Date(Date.now() - 86400000);
	const yStr = yesterday.toISOString().slice(0, 10);
	const sDate = $('#systemMetricsDate');
	if (sDate) { sDate.value = yStr; sDate.max = yStr; }
	const oDate = $('#orgUsageDate');
	if (oDate) { oDate.value = yStr; oDate.max = yStr; }
	// 默认加载概览数据
	loadOverview();
	loadTrend();
}

/* ====== Tab 切换 ====== */
function switchTab(name) {
	currentTab = name;
	$$('#analyticsTabs .tab-item').forEach(el => {
		el.classList.toggle('active', el.getAttribute('data-tab') === name);
	});
	$$('.tab-panel').forEach(p => {
		p.classList.toggle('active', p.getAttribute('data-panel') === name);
	});
	// 切到新 Tab 时懒加载一次数据
	switch (name) {
		case 'overview': break; // 已默认加载
		case 'system': loadSystemMetrics(); break;
		case 'realtime': loadRealtime(); break;
		case 'queue': loadQueueDepth(); break;
		case 'org': loadOrgUsage(); break;
		case 'qa': loadQaRecords(); break;
		case 'daily': loadDailyReport(); break;
		case 'tools':
			loadKeywords('keywordsTableBody2');
			loadBadFeedbacks('feedbackList2');
			loadFeedbackLoopAggs();
			break;
	}
}

function reloadCurrentTab() {
	// 根节点切换时，按当前 Tab 懒加载对应数据
	switch (currentTab) {
		case 'overview':
			loadOverview();
			loadTrend();
			break;
		case 'system': loadSystemMetrics(); break;
		case 'realtime': loadRealtime(); break;
		case 'queue': loadQueueDepth(); break;
		case 'org': loadOrgUsage(); break;
		case 'qa': qaPage = 1; loadQaRecords(); break;
		case 'daily': loadDailyReport(); break;
		case 'tools':
			loadKeywords('keywordsTableBody2');
			loadBadFeedbacks('feedbackList2');
			loadFeedbackLoopAggs();
			break;
	}
}

/* ---- 概览统计 ---- */
async function loadOverview() {
	const kpiValues = $$('.tab-panel[data-panel="overview"] .kpi-value');
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/overview/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);

		// textContent 统一避免 innerHTML，防止数值被恶意注入
		if (kpiValues[0]) kpiValues[0].textContent = (data.total_qa ?? 0).toLocaleString();
		// 满意率 + % 后缀：用 span 子节点而不是 innerHTML
		if (kpiValues[1]) {
			kpiValues[1].textContent = ((data.accuracy ?? 0) * 100).toFixed(1);
			const sp = kpiValues[1].querySelector('.text-sub') || document.createElement('span');
			sp.className = 'text-sm text-sub';
			sp.textContent = '%';
			kpiValues[1].appendChild(sp);
		}
		// 平均响应耗时 + s 后缀
		if (kpiValues[2]) {
			kpiValues[2].textContent = ((data.avg_latency_ms || 0) / 1000).toFixed(2);
			const sp = kpiValues[2].querySelector('.text-sub') || document.createElement('span');
			sp.className = 'text-sm text-sub';
			sp.textContent = 's';
			kpiValues[2].appendChild(sp);
		}
		if (kpiValues[3]) kpiValues[3].textContent = data.active_users ?? 0;
	} catch (e) {
		kpiValues.forEach(el => { if (el) el.textContent = '--'; });
		toast('加载概览数据失败', 'error');
		console.error('load overview failed:', e);
	}
}

/* ---- 趋势图 ---- */
async function loadTrend() {
	try {
		const rootType = getSelectedRootType();
		let url;
		if (currentTimeRange === 'custom' && customDateStart && customDateEnd) {
			url = `/api/v1/analytics/trend/?start_date=${customDateStart}&end_date=${customDateEnd}`;
		} else {
			const days = currentTimeRange === 'today' ? 1 : (currentTimeRange === 'week' ? 7 : 30);
			url = `/api/v1/analytics/trend/?days=${days}`;
		}
		if (rootType) url += '&root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const trend = data.trend || [];

		const chart = $('#trendChart');
		if (chart) {
			chart.innerHTML = renderTrendChart(trend);
			// 趋势图 checkbox 事件委托：只绑一次，避免内联 onclick
			if (!chart._metricListener) {
				chart.addEventListener('change', (evt) => {
					const cb = evt.target.closest('input[data-metric]');
					if (!cb) return;
					toggleOverviewTrend(cb.getAttribute('data-metric'));
				});
				chart._metricListener = true;
			}
		}
	} catch (e) {
		const chart = $('#trendChart');
		if (chart) chart.innerHTML = '<div class="error-block-lg">加载趋势数据失败</div>';
		toast('加载趋势数据失败', 'error');
		console.error('load trend failed:', e);
	}
}

function renderTrendChart(trend) {
	if (!trend || trend.length === 0) {
		return '<div class="empty">暂无数据</div>';
	}
	if (trend.length === 1) {
		return '<div class="empty">仅 1 天数据，暂无法绘制趋势图</div>';
	}

	const w = 740, h = 280, pad = 28, padR = 48;
	const days = trend.map(t => t.date.slice(5));
	const sat = trend.map(t => (t.accuracy || 0) * 100);
	/* 单位 ms → 转换为 s（左轴 ms，右轴 s，统一用 s 比较直观） */
	const ttftSec = trend.map(t => (t.avg_ttft_ms || 0) / 1000);
	const totalSec = trend.map(t => (t.avg_total_ms || 0) / 1000);

	const xStep = (w - pad - padR) / (days.length - 1);
	/* 左轴（满意率百分比）：仅当勾选时计算真实范围，否则占位 0-100 */
	let y1Min = 70, y1Max = 100;
	if (showAccuracy) {
		y1Min = Math.min(...sat) - 3;
		y1Max = Math.max(...sat) + 3;
		y1Min = Math.max(0, y1Min);
		y1Max = Math.min(100, y1Max);
		if (y1Max - y1Min < 5) { y1Min = Math.max(0, y1Max - 5); }
	}
	/* 右轴（响应耗时秒）：仅当首字 / 整体至少勾选其一时计算真实范围 */
	const showLat = showTtft || showTotal;
	let y2Min = 0, y2Max = 5;
	if (showLat) {
		const latVals = [];
		if (showTtft) latVals.push(...ttftSec);
		if (showTotal) latVals.push(...totalSec);
		y2Min = Math.min(...latVals, 0) - 0.2;
		y2Max = Math.max(...latVals, 0.1) + 0.3;
		y2Min = Math.max(0, y2Min);
		if (Math.abs(y2Max - y2Min) < 0.1) { y2Min = 0; y2Max = 1; }
	}

	const yLeft = v => h - pad - ((v - y1Min) / (y1Max - y1Min)) * (h - 2 * pad);
	const yRight = v => h - pad - ((v - y2Min) / (y2Max - y2Min)) * (h - 2 * pad);

	const pSat = showAccuracy ? sat.map((v, i) => `${pad + i * xStep},${yLeft(v)}`).join(' ') : '';
	const pTtft = showTtft ? ttftSec.map((v, i) => `${pad + i * xStep},${yRight(v)}`).join(' ') : '';
	const pTotal = showTotal ? totalSec.map((v, i) => `${pad + i * xStep},${yRight(v)}`).join(' ') : '';

	/* 网格 + 左右轴刻度 */
	let grid = '';
	for (let i = 0; i <= 5; i++) {
		const y = pad + (h - 2 * pad) * i / 5;
		grid += `<line x1="${pad}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="#e5e7eb" stroke-dasharray="3 3"/>`;
		if (showAccuracy) {
			const leftLabel = (y1Max - (y1Max - y1Min) * i / 5).toFixed(0);
			grid += `<text x="${pad - 6}" y="${y + 4}" text-anchor="end" font-size="10" fill="#2563eb">${leftLabel}%</text>`;
		}
		if (showLat) {
			const rightLabel = (y2Max - (y2Max - y2Min) * i / 5).toFixed(2);
			grid += `<text x="${w - padR + 6}" y="${y + 4}" text-anchor="start" font-size="10" fill="#a16207">${rightLabel}s</text>`;
		}
	}

	const xLabels = days.map((d, i) => `<text x="${pad + i * xStep}" y="${h - pad + 14}" text-anchor="middle" font-size="10" fill="#6b7280">${d}</text>`).join('');
	const dotSat = showAccuracy ? sat.map((v, i) => `<circle cx="${pad + i * xStep}" cy="${yLeft(v)}" r="2.5" fill="#2563eb"/>`).join('') : '';
	const dotTtft = showTtft ? ttftSec.map((v, i) => `<circle cx="${pad + i * xStep}" cy="${yRight(v)}" r="2.5" fill="#a16207"/>`).join('') : '';
	const dotTotal = showTotal ? totalSec.map((v, i) => `<circle cx="${pad + i * xStep}" cy="${yRight(v)}" r="2.5" fill="#ef4444"/>`).join('') : '';

	/* 左侧勾选框列 */
	const sidebarHtml = `
    <div class="chart-sidebar">
      <label class="checkbox"><input type="checkbox" ${showAccuracy ? 'checked' : ''} data-metric="accuracy"><span class="metric-dot dot-blue"></span>满意率</label>
      <label class="checkbox"><input type="checkbox" ${showTtft ? 'checked' : ''} data-metric="ttft"><span class="metric-dot dot-yellow"></span>首字耗时</label>
      <label class="checkbox"><input type="checkbox" ${showTotal ? 'checked' : ''} data-metric="total"><span class="metric-dot dot-red"></span>整体耗时</label>
    </div>`;

	const svgHtml = `<svg class="chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
    ${grid}${xLabels}
    ${showAccuracy ? `<polyline points="${pSat}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>` : ''}
    ${showTtft ? `<polyline points="${pTtft}" fill="none" stroke="#a16207" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="6 3"/>` : ''}
    ${showTotal ? `<polyline points="${pTotal}" fill="none" stroke="#ef4444" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>` : ''}
    ${dotSat}${dotTtft}${dotTotal}
  </svg>`;

	/* 顶部 titlebar：标题左，其他右（目前留空只放标题） */
	return `
    <div class="chart-row">
      ${sidebarHtml}
      <div class="chart-container chart-container-flex" style="height:${h + 18}px">
        ${svgHtml}
      </div>
    </div>`;
}

/* ---- 关键词表格 ---- */
async function loadKeywords(tbodyId) {
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/keywords/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const keywords = data.rows || [];
		const actualTbodyId = tbodyId || 'keywordsTableBody';

		const kwBody = document.getElementById(actualTbodyId);
		if (kwBody) {
			const kwTpl = tpl('tmpl-kw-row');
			kwBody.innerHTML = keywords.length === 0
				? '<tr><td colspan="4" class="empty">暂无关键词数据</td></tr>'
				: keywords.map(k => {
					const row = kwTpl.content.cloneNode(true).firstElementChild;
					row.querySelectorAll('td')[0].textContent = escapeHtml(k.keyword);
					const tag = row.querySelector('.tag');
					tag.textContent = '×' + (k.weight_score || 1).toFixed(1);
					if (k.weight_score > 1) tag.classList.add('tag-success');
					else if (k.weight_score < 1) tag.classList.add('tag-warning');
					row.querySelectorAll('td')[2].textContent = (k.hit_count || 0) + ' 次命中 · ' + (k.good_feedback || 0) + ' 好评 · ' + (k.bad_feedback || 0) + ' 差评';
					const incrBtn = row.querySelector('.incr');
					const decrBtn = row.querySelector('.decr');
					incrBtn.setAttribute('data-kw-id', k.id);
					incrBtn.setAttribute('data-kw-delta', '0.1');
					incrBtn.setAttribute('data-tbody-id', actualTbodyId);
					decrBtn.setAttribute('data-kw-id', k.id);
					decrBtn.setAttribute('data-kw-delta', '-0.1');
					decrBtn.setAttribute('data-tbody-id', actualTbodyId);
					return row.outerHTML;
				}).join('');

			// 在 tbody 上绑 click 监听，处理所有 .incr/.decr 按钮
			if (!kwBody._kwListenerAttached) {
				kwBody.addEventListener('click', (evt) => {
					const btn = evt.target.closest('.incr, .decr');
					if (!btn) return;
					const id = parseInt(btn.getAttribute('data-kw-id'), 10);
					const delta = parseFloat(btn.getAttribute('data-kw-delta'));
					const tid = btn.getAttribute('data-tbody-id') || 'keywordsTableBody';
					if (!isNaN(id) && !isNaN(delta)) adjustKeywordWeight(id, delta, tid);
				});
				kwBody._kwListenerAttached = true;
			}
		}
	} catch (e) {
		const kwBody = document.getElementById(tbodyId || 'keywordsTableBody');
		if (kwBody) kwBody.innerHTML = '<tr><td colspan="4" class="error-block">加载关键词数据失败</td></tr>';
		toast('加载关键词数据失败', 'error');
		console.error('load keywords failed:', e);
	}
}

async function adjustKeywordWeight(id, delta, tbodyId) {
	try {
		await api.put(`/api/v1/analytics/keywords/${id}/`, { delta: delta });
		toast(delta > 0 ? '已加权 +0.1' : '已降权 -0.1', 'success');
		// 刷新关键词表 + 自动调整记录（手动调整也写入审计）
		loadKeywords('keywordsTableBody2');
		loadFeedbackLoopAggs();
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ---- 反馈闭环自动调整记录 ---- */
async function loadFeedbackLoopAggs() {
	const body = document.getElementById('feedbackAggBody');
	if (!body) return;
	try {
		const data = await api.getJson('/api/v1/analytics/feedback-loop/aggregations/?limit=100');
		const rows = data.rows || [];
		const kwTpl = tpl('tmpl-fb-agg-row');
		body.innerHTML = rows.length === 0
			? '<tr><td colspan="8" class="empty">暂无自动调整记录（点击/反馈数据不足或尚未聚合）</td></tr>'
			: rows.map(r => {
				const row = kwTpl.content.cloneNode(true).firstElementChild;
				const tds = row.querySelectorAll('td');
				tds[0].textContent = r.report_date;
				tds[1].textContent = escapeHtml(r.keyword);
				tds[2].textContent = `${r.shown_count || 0} / ${r.click_count || 0} / ${r.adopt_count || 0} / ${r.bad_count || 0}`;
				tds[3].textContent = Math.round((r.adopt_rate || 0) * 100) + '%';
				tds[4].textContent = (r.old_score || 1).toFixed(2) + ' → ' + (r.new_score || 1).toFixed(2);
				tds[5].textContent = escapeHtml(r.reason || '-');
				const tag = row.querySelector('.tag');
				const statusMap = { pending: ['待复核', 'tag-warning'], applied: ['已应用', 'tag-success'], ignored: ['已忽略', ''] };
				const st = statusMap[r.status] || [r.status || '', ''];
				tag.textContent = st[0];
				if (st[1]) tag.classList.add(st[1]);
				const actions = row.querySelector('.table-actions');
				if (r.status === 'pending') {
					actions.innerHTML =
						`<button class="btn-link btn-sm" onclick="applyFeedbackAgg(${r.id},'apply')">应用</button>` +
						`<button class="btn-link btn-sm" onclick="applyFeedbackAgg(${r.id},'ignore')">忽略</button>`;
				} else {
					actions.innerHTML = '<span class="text-sub text-sm">' + (r.adjust_type === 'manual' ? '手动' : '自动') + '</span>';
				}
				return row.outerHTML;
			}).join('');
	} catch (e) {
		body.innerHTML = '<tr><td colspan="8" class="error-block">加载自动调整记录失败</td></tr>';
		console.error('load feedback loop aggs failed:', e);
	}
}

/* 手动触发一次反馈闭环聚合（默认聚合昨天，支持运营即时回补） */
async function runFeedbackLoop() {
	try {
		await api.postJson('/api/v1/analytics/feedback-loop/run/', {});
		toast('聚合完成，已刷新记录', 'success');
		loadFeedbackLoopAggs();
		loadKeywords('keywordsTableBody2');
	} catch (e) {
		toast(e.message || '聚合失败', 'error');
	}
}

/* 人工复核：应用/忽略一条待复核的自动调整 */
async function applyFeedbackAgg(id, action) {
	try {
		await api.postJson('/api/v1/analytics/feedback-loop/apply/', { id: id, action: action });
		toast(action === 'apply' ? '已应用调整' : '已忽略', 'success');
		loadFeedbackLoopAggs();
		loadKeywords('keywordsTableBody2');
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ---- 动态加载节点树 ---- */
let nodesCache = [];

function getSelectedRootType() {
	const sel = document.getElementById('reportRootType');
	if (!sel || !sel.value) return '';
	const selectedOption = sel.options[sel.selectedIndex];
	return selectedOption?.getAttribute('data-root-type') || '';
}

async function loadRootTypes() {
	try {
		const data = await api.getJson('/api/v1/knowledge/nodes/tree/');
		const tree = data.tree || [];
		nodesCache = [];
		for (const n of tree) {
			if (n.node_type === 'root') {
				nodesCache.push({ id: n.id, root_type: n.root_type, name: n.name, depth: 0 });
			}
		}
		updateRootTypeSelect();
	} catch (e) {
		toast('加载节点树失败', 'error');
		console.error('load nodes failed:', e);
		nodesCache = [];
		updateRootTypeSelect();
	}
}

function updateRootTypeSelect() {
	const sel1 = document.getElementById('reportRootType');
	const sel2 = document.getElementById('newKeywordRootType');
	if (nodesCache.length === 0) {
		if (sel1) sel1.innerHTML = `<option value="">全部节点</option><option value="" disabled>暂无节点数据</option>`;
		if (sel2) sel2.innerHTML = `<option value="all">全部</option><option value="" disabled>暂无节点数据</option>`;
		return;
	}
	const options = nodesCache.map(n => {
		const indent = n.depth > 0 ? '&nbsp;&nbsp;'.repeat(n.depth) + '└─ ' : '';
		return `<option value="${escapeHtml(String(n.id))}" data-root-type="${escapeHtml(n.root_type)}">${indent}${escapeHtml(n.name)}</option>`;
	}).join('');
	if (sel1) sel1.innerHTML = `<option value="">全部节点</option>` + options;
	if (sel2) sel2.innerHTML = `<option value="all">全部</option>` + options;
}

/* ---- 新增关键词 ---- */
function addKeyword() {
	const textInput = document.getElementById('newKeywordText');
	const weightInput = document.getElementById('newKeywordWeight');
	const rootInput = document.getElementById('newKeywordRootType');
	if (textInput) textInput.value = '';
	if (weightInput) weightInput.value = '1.0';
	if (rootInput) rootInput.value = 'all';
	showModal('modal-add-keyword');
	setTimeout(() => textInput && textInput.focus(), 50);
}

async function submitNewKeyword() {
	const keyword = (document.getElementById('newKeywordText')?.value || '').trim();
	const weight = parseFloat(document.getElementById('newKeywordWeight')?.value || '1.0');
	const rootType = document.getElementById('newKeywordRootType')?.value || 'all';

	if (!keyword) { toast('请输入关键词', 'error'); return; }
	if (isNaN(weight) || weight < 0.1 || weight > 5.0) { toast('权重范围 0.1 ~ 5.0', 'error'); return; }

	try {
		await api.postJson('/api/v1/analytics/keywords/', { keyword: keyword, weight_score: weight, root_type: rootType });
		toast('已新增关键词', 'success');
		closeAllOverlays();
		loadKeywords('keywordsTableBody2');
	} catch (e) {
		toast(e.message || '添加失败', 'error');
	}
}

/* ---- 差评反馈列表 ---- */
async function loadBadFeedbacks(listId) {
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/bad-feedbacks/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const feedbacks = data.rows || [];
		const actualListId = listId || 'feedbackList';

		const fbList = document.getElementById(actualListId);
		if (fbList) {
			fbList.innerHTML = feedbacks.length === 0
				? '<div class="empty">暂无差评反馈</div>'
				: feedbacks.map(f => {
					const isResolved = f.status === 'resolved';
					return htmlFromTpl('tmpl-feedback-card', (frag) => {
						const root = frag.firstElementChild;
						root.querySelector('.fb-question').textContent = 'Q: ' + escapeHtml(f.question || '');
						// fb-answer 纯文本展示用 textContent，避免 innerHTML + escapeHtml 混用,性能差且易出错
						const a = root.querySelector('.fb-answer');
						a.textContent = 'A（摘要）: ' + ((f.answer || '').slice(0, 120) + ((f.answer || '').length > 120 ? '…' : ''));
						// fb-comment 有"<b>反馈：</b>"前缀，后面纯文本部分用 textContent 拼接
						const c = root.querySelector('.fb-comment');
						c.innerHTML = '<b>反馈：</b>';
						c.appendChild(document.createTextNode(escapeHtml(f.comment || '无详细反馈')));
						// fb-meta 有条件的 span，使用文本节点 + createElement 组合避免 innerHTML
						const meta = root.querySelector('.fb-meta');
						meta.textContent = '';
						meta.appendChild(document.createTextNode((f.user || '-') + ' · ' + formatDate(f.created_at)));
						if (isResolved) {
							meta.appendChild(document.createTextNode(' · '));
							const sp = document.createElement('span');
							sp.className = 'tag tag-success';
							sp.textContent = '已处理';
							meta.appendChild(sp);
						}
						
						const adjBtn = root.querySelector('.adjust-btn');
						const procBtn = root.querySelector('.process-btn');
						adjBtn.setAttribute('data-fb-id', f.id);
						adjBtn.setAttribute('data-fb-action', 'adjust');
						if (isResolved) {
							procBtn.style.display = 'none';
						} else {
							procBtn.setAttribute('data-fb-id', f.id);
							procBtn.setAttribute('data-fb-action', 'process');
						}
					});
				}).join('');

			// 差评反馈容器级事件委托
			if (!fbList._fbListenerAttached) {
				fbList.addEventListener('click', (evt) => {
					const btn = evt.target.closest('.adjust-btn, .process-btn');
					if (!btn) return;
					const fbId = parseInt(btn.getAttribute('data-fb-id'), 10);
					const action = btn.getAttribute('data-fb-action');
					if (isNaN(fbId)) return;
					if (action === 'adjust') adjustKeywordWeightByFeedback(fbId);
					else if (action === 'process') markFeedbackProcessed(fbId);
				});
				fbList._fbListenerAttached = true;
			}
		}
	} catch (e) {
		const fbList = document.getElementById(listId || 'feedbackList');
		if (fbList) fbList.innerHTML = '<div class="error-block">加载反馈数据失败</div>';
		toast('加载反馈数据失败', 'error');
		console.error('load bad feedbacks failed:', e);
	}
}

function adjustKeywordWeightByFeedback(fbId) {
	toast('请在关键词列表中手动调整相关关键词权重', '');
	switchTab('tools');
}

async function markFeedbackProcessed(fbId) {
	try {
		await api.put(`/api/v1/analytics/bad-feedbacks/${fbId}/`, { status: 'resolved' });
		toast('已标记为已处理', 'success');
		loadBadFeedbacks('feedbackList2');
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ---- 时间范围切换 ---- */
function setTimeRange(range) {
	if (range !== 'custom') {
		currentTimeRange = range;
		updateTimeButtons(range);
		updateChartTitle(range);
		loadTrend();
	} else {
		showCustomDateRange();
	}
}

function updateTimeButtons(range) {
	const timeBtns = $$('#timeRangeButtons .btn');
	const rangeMap = ['today', 'week', 'month', 'custom'];
	timeBtns.forEach((b, i) => {
		if (rangeMap[i] === range) {
			b.classList.remove('btn-ghost');
			b.classList.add('btn-primary');
		} else if (rangeMap[i]) {
			b.classList.remove('btn-primary');
			b.classList.add('btn-ghost');
		}
	});
}

function updateChartTitle(range) {
	const titleEl = $$('.tab-panel[data-panel="overview"] .chart-wrap .text-lg')[0];
	if (!titleEl) return;
	let label;
	if (range === 'custom' && customDateStart && customDateEnd) {
		label = `${customDateStart} ~ ${customDateEnd}`;
	} else {
		const labels = { 'today': '今日', 'week': '近 7 天', 'month': '近 30 天', 'custom': '自定义' };
		label = labels[range] || '近 7 天';
	}
	titleEl.textContent = `📈 满意率与响应耗时趋势（${label}）`;
}

/* ---- 趋势图指标切换 ---- */
function toggleOverviewTrend(type) {
	if (type === 'accuracy') showAccuracy = !showAccuracy;
	else if (type === 'ttft') showTtft = !showTtft;
	else if (type === 'total') showTotal = !showTotal;
	// 至少保留一项
	if (!showAccuracy && !showTtft && !showTotal) {
		if (type === 'accuracy') showAccuracy = true;
		else if (type === 'ttft') showTtft = true;
		else showTotal = true;
	}
	loadTrend();
}

/* ---- 自定义日期范围弹窗 ---- */
function showCustomDateRange() {
	const today = new Date().toISOString().slice(0, 10);
	const weekAgo = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10);
	const startInput = document.getElementById('customDateStart');
	const endInput = document.getElementById('customDateEnd');
	if (startInput) { startInput.value = customDateStart || weekAgo; startInput.max = today; }
	if (endInput) { endInput.value = customDateEnd || today; endInput.max = today; }
	showModal('modal-date-range');
}

function applyCustomDateRange() {
	const start = document.getElementById('customDateStart')?.value;
	const end = document.getElementById('customDateEnd')?.value;
	if (!start || !end) { toast('请选择开始日期和结束日期', 'error'); return; }
	if (start > end) { toast('开始日期不能晚于结束日期', 'error'); return; }
	customDateStart = start;
	customDateEnd = end;
	currentTimeRange = 'custom';
	closeAllOverlays();
	updateTimeButtons('custom');
	updateChartTitle('custom');
	loadTrend();
	toast(`已切换至自定义范围：${start} ~ ${end}`, 'success');
}

/* ---- 导出报表 ---- */
async function exportReport() {
	try {
		const rootType = getSelectedRootType();
		let url;
		if (currentTimeRange === 'custom' && customDateStart && customDateEnd) {
			url = `/api/v1/analytics/trend/?start_date=${customDateStart}&end_date=${customDateEnd}`;
		} else {
			const days = currentTimeRange === 'today' ? 1 : (currentTimeRange === 'week' ? 7 : 30);
			url = `/api/v1/analytics/trend/?days=${days}`;
		}
		if (rootType) url += '&root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);

		// CSV 加 UTF-8 BOM（EF BB BF），解决 Excel 打开中文乱码
		const BOM = '\uFEFF';
		let csv = BOM + '日期,问答数,好评数,差评数,准确率(%),平均耗时(ms)\n';
		(data.trend || []).forEach(t => {
			// 后端 TrendReportView 返回 avg_total_ms（非缓存命中的整体总耗时），并非 avg_latency_ms
			csv += `${t.date},${t.qa_count},${t.good},${t.bad},${(t.accuracy * 100).toFixed(2)},${t.avg_total_ms || 0}\n`;
		});

		const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
		const link = document.createElement('a');
		const url2 = URL.createObjectURL(blob);
		link.setAttribute('href', url2);
		link.setAttribute('download', `报表_${new Date().toISOString().slice(0, 10)}.csv`);
		link.style.visibility = 'hidden';
		document.body.appendChild(link);
		link.click();
		document.body.removeChild(link);
		URL.revokeObjectURL(url2);

		toast('报表已导出', 'success');
	} catch (e) {
		toast('导出失败', 'error');
		console.error('export failed:', e);
	}
}

/* ---- Tab 2: 系统性能指标报表（P50/P95/P99 / 缓存命中率 / 失败率 / Token / 错误分布） ---- */
async function loadSystemMetrics() {
	const box = $('#systemMetricsBody');
	const date = $('#systemMetricsDate')?.value;
	try {
		let url = '/api/v1/analytics/system-metrics/';
		if (date) url += '?date=' + encodeURIComponent(date);
		const data = await api.getJson(url);

		if (!data.available) {
			box.innerHTML = `
        <div class="card card-empty">
          <div class="empty-emoji">📅</div>
          <div class="text-lg fw-500 mb-8">${escapeHtml(data.message || '暂无数据')}</div>
          <div class="text-sub">报表日期：${escapeHtml(date || data.date || '-')}</div>
        </div>`;
			return;
		}

		// 1. KPI 层：QA 规模 + 比率
		const kpiCards = [
			{ label: '总 QA 数', value: data.total_qa?.toLocaleString() || 0, color: '#1f2937' },
			{ label: '正常请求数', value: data.normal_qa_count?.toLocaleString() || 0, color: '#2563eb' },
			{ label: '缓存命中数', value: data.cache_hit_count?.toLocaleString() || 0, color: '#059669' },
			{ label: '缓存命中率', value: (data.cache_hit_rate || 0) * 100 + '%', color: '#059669' },
			{ label: 'LLM 成功率', value: (data.llm_success_rate || 0) * 100 + '%', color: data.llm_success_rate < 0.9 ? '#dc2626' : '#059669' },
			{ label: 'LLM 超时率', value: (data.llm_timeout_rate || 0) * 100 + '%', color: data.llm_timeout_rate > 0.01 ? '#dc2626' : '#f59e0b' },
			{ label: 'Embedding 错误率', value: (data.embedding_error_rate || 0) * 100 + '%', color: data.embedding_error_rate > 0.01 ? '#dc2626' : '#f59e0b' },
			{ label: '平均 Token/s', value: data.avg_tokens_per_second || 0, color: '#7c3aed' },
		].map(c => `
      <div class="kpi-card">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value kpi-value-dynamic" style="--kpi-color:${c.color}">${c.value}</div>
      </div>`).join('');

	// 2. 延迟对比表：正常 + 缓存命中
	const latencyRows = (fields, title) => `
      <div class="card">
        <div class="card-title">${title}</div>
        <table class="table table-bordered">
          <thead><tr>
            ${fields.map(f => `<th>${f.label}</th>`).join('')}
          </tr></thead>
          <tbody><tr>
            ${fields.map(f => `<td>${f.fmt(f.value)}</td>`).join('')}
          </tr></tbody>
        </table>
      </div>`;
		const msFmt = v => v == null ? '-' : `${v.toLocaleString()} ms`;

		const normalLat = latencyRows([
			{ label: '总延迟 P50', value: data.p50_latency_total, fmt: msFmt },
			{ label: '总延迟 P95', value: data.p95_latency_total, fmt: msFmt },
			{ label: '总延迟 P99', value: data.p99_latency_total, fmt: msFmt },
			{ label: 'LLM P50', value: data.p50_latency_llm, fmt: msFmt },
			{ label: 'LLM P95', value: data.p95_latency_llm, fmt: msFmt },
			{ label: '检索 P50', value: data.p50_latency_retrieval, fmt: msFmt },
			{ label: '检索 P95', value: data.p95_latency_retrieval, fmt: msFmt },
			{ label: 'TTFB P50', value: data.p50_ttfb, fmt: msFmt },
			{ label: 'TTFB P95', value: data.p95_ttfb, fmt: msFmt },
		], '⚡ 正常请求：分位数延迟（ms）');

		const cacheLat = latencyRows([
			{ label: '缓存命中 P50', value: data.cache_hit_p50_latency, fmt: msFmt },
			{ label: '缓存命中 P95', value: data.cache_hit_p95_latency, fmt: msFmt },
		], '💨 缓存命中：分位数延迟（ms）');

		// 3. Token & 成本
	const tokenStr = `
      <div class="card">
        <div class="card-title">🪙 Token 与 成本</div>
        <div class="grid-3">
          <div><div class="text-sub text-sm mb-4">Prompt Token</div><div class="metric-value-lg">${(data.total_tokens_prompt || 0).toLocaleString()}</div></div>
          <div><div class="text-sub text-sm mb-4">Completion Token</div><div class="metric-value-lg">${(data.total_tokens_completion || 0).toLocaleString()}</div></div>
          <div><div class="text-sub text-sm mb-4">预估费用（¥）</div><div class="metric-value-cost">¥ ${(data.total_cost || 0).toFixed(4)}</div></div>
        </div>
      </div>`;

	// 4. 延迟直方图
	const hist = data.latency_histogram || {};
	const histKeys = Object.keys(hist).sort();
	// 直方图总量只算一次，避免每条记录 O(n) reduce 造成的 O(n²)
	const histTotal = histKeys.reduce((s, kk) => s + (hist[kk] || 0), 0);
	/* 直方图每行结构抽到 .hist-row / .hist-label / .hist-track / .hist-bar / .hist-value，
	   .hist-bar 的宽度由下方 setTimeout 动画设置（class __anim 用于选中元素） */
	const histHtml = histKeys.length === 0 ? '<div class="empty">暂无分布数据</div>' : histKeys.map(k => {
		const v = hist[k] || 0;
		const pct = histTotal ? ((v / histTotal) * 100).toFixed(1) : 0;
		return `<div class="hist-row">
        <span class="hist-label">${escapeHtml(k)}</span>
        <div class="hist-track">
          <div class="hist-bar __anim"></div>
        </div>
        <span class="hist-value">${v.toLocaleString()} (${pct}%)</span>
      </div>`;
	}).join('');

	// 5. 错误分布
	const errDist = data.error_distribution || {};
	const errKeys = Object.keys(errDist).sort((a, b) => (errDist[b] || 0) - (errDist[a] || 0));
	const errTotal = errKeys.reduce((s, k) => s + (errDist[k] || 0), 0) || 1;
	/* 错误分布用红色系 */
	const errHtml = errKeys.length === 0 ? '<div class="empty">暂无错误数据 🎉</div>' : errKeys.map(k => {
		const v = errDist[k] || 0;
		const pct = (v / errTotal) * 100;
		return `<div class="hist-row">
        <span class="hist-label-err">${escapeHtml(k || 'unknown')}</span>
        <div class="hist-track-err">
          <div class="hist-bar-err" style="width:${pct.toFixed(1)}%"></div>
        </div>
        <span class="hist-value">${v} (${pct.toFixed(1)}%)</span>
      </div>`;
	}).join('');

	box.innerHTML = `
      <div class="kpi-grid">${kpiCards}</div>
      ${normalLat}
      ${cacheLat}
      ${tokenStr}
      <div class="grid-2 grid-cols-1-1">
        <div class="card"><div class="card-title">📊 延迟分布直方图（ms）</div>${histHtml}</div>
        <div class="card"><div class="card-title">🧨 错误类型分布</div>${errHtml}</div>
      </div>`;

		// 延迟直方图动画填充（逐行递增，让宽度随 0→实际宽度 动画）
		// 复用顶部已计算的 histTotal，避免 forEach 内每次 reduce 造成 O(n²)
		setTimeout(() => {
			$$('#systemMetricsBody .__anim').forEach((el, idx) => {
				const target = histTotal ? ((hist[histKeys[idx]] || 0) / histTotal) * 100 : 0;
				el.style.transition = 'width .5s ease';
				requestAnimationFrame(() => {
					el.style.width = target + '%';
					el.style.background = '#2563eb';
				});
			});
		}, 30);
	} catch (e) {
		box.innerHTML = `<div class="card card-error">加载系统指标失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载系统指标失败', 'error');
		console.error('load system metrics failed:', e);
	}
}

/* ---- Tab 3: 实时监控（Redis 快照） ---- */
async function loadRealtime() {
	const box = $('#realtimeBody');
	try {
		const data = await api.getJson('/api/v1/analytics/realtime/');

		const freshness = data.last_flush_at
			? Math.floor((Date.now() / 1000) - data.last_flush_at)
			: null;
		const isFresh = freshness != null && freshness < 600; // 10 分钟内视为新鲜

		const freshnessBadge = freshness == null
			? '<span class="tag tag-warning">尚未同步</span>'
			: (isFresh
				? `<span class="tag tag-success">数据新鲜（${freshness}s 前同步）</span>`
				: `<span class="tag tag-danger">数据陈旧（${freshness}s 未同步）</span>`);

		const kpiCards = [
		{ label: '今日 QA 总数', value: (data.total_qa || 0).toLocaleString(), color: '#1f2937' },
		{ label: '缓存命中', value: (data.cache_hits || 0).toLocaleString(), color: '#059669' },
		{ label: '正常请求', value: (data.normal_qa || 0).toLocaleString(), color: '#2563eb' },
		{ label: 'LLM 错误', value: (data.llm_errors || 0).toLocaleString(), color: data.llm_errors > 0 ? '#dc2626' : '#059669' },
		{ label: '今日 Prompt Token', value: (data.tokens_prompt || 0).toLocaleString(), color: '#7c3aed' },
		{ label: '今日 Completion Token', value: (data.tokens_completion || 0).toLocaleString(), color: '#7c3aed' },
		{ label: '今日预估费用', value: '¥ ' + (data.cost_estimate || 0).toFixed(4), color: '#dc2626' },
	].map(c => `
      <div class="kpi-card">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value kpi-value-dynamic" style="--kpi-color:${c.color}">${c.value}</div>
      </div>`).join('');

	box.innerHTML = `
      <div class="card mb-16 flex justify-between items-center card-pad-sm">
        <div>📅 数据日期：${escapeHtml(data.date || '-')}　${freshnessBadge}</div>
      </div>
      <div class="kpi-grid">${kpiCards}</div>
      <div class="card mt-16">
        <div class="card-title">💡 数据来源说明</div>
        <div class="text-sub text-sm text-lh-18">
          实时指标直接读取 Redis 计数器（key: <code>analytics:realtime:日期</code>），通过 <code>increment_realtime_metrics()</code>
          在每次 QA 完成时原子自增，<code>flush_realtime_metrics()</code> 每 5 分钟更新时间戳。<br>
          精确的 T+1 聚合报表请参考「系统指标」Tab（凌晨 2 点生成，含 P50/P95/P99 分位数）。
        </div>
      </div>`;
	} catch (e) {
		box.innerHTML = `<div class="card card-error">加载实时指标失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载实时指标失败', 'error');
		console.error('load realtime failed:', e);
	}
}

/* ---- Tab 4: 队列深度监控 ---- */
async function loadQueueDepth() {
	try {
		const hours = $('#queueHours')?.value || 24;
		const data = await api.getJson(`/api/v1/analytics/queue-depth/?hours=${hours}`);

		// 1. 当前实时快照 — 渲染到上方紧凑卡片
		const snapBox = $('#queueSnapshotBody');
		const cur = data.current || {};
		const curKeys = Object.keys(cur);
		/* 队列大小超过 1000 视为危险，用 .cell-danger / .cell-success 切换颜色 */
		const curHtml = curKeys.length === 0
			? '<div class="empty">当前无队列数据（Celery Worker 未启动？）</div>'
			: curKeys.map(q => {
				const d = cur[q] || {};
				const size = d.size || d.length || 0;
				const danger = size > 1000;
				return `<tr>
          <td>${escapeHtml(q)}</td>
          <td class="${danger ? 'cell-danger' : 'cell-success'}">${size.toLocaleString()}</td>
          <td>${d.queued || '-'}</td>
          <td>${d.active || '-'}</td>
          <td>${d.idle || '-'}</td>
          <td>${d.failed != null ? d.failed : '-'}</td>
        </tr>`;
			}).join('');

		snapBox.innerHTML = `
        <table class="table table-bordered">
          <thead><tr>
            <th>队列名</th><th>等待任务数</th><th>已排队</th><th>运行中</th><th>空闲 Worker</th><th>失败</th>
          </tr></thead>
          <tbody>${curHtml || '<tr><td colspan="6" class="empty">无数据</td></tr>'}</tbody>
        </table>`;

		// 2. 历史趋势 — 渲染到下方大卡片，撑满剩余空间
		const histBox = $('#queueDepthHistory');
		const history = data.history || [];
		histBox.innerHTML = history.length === 0
			? '<div class="empty">暂无历史数据（需要等待至少 1 个 5 分钟周期）</div>'
			: renderQueueDepthChart(history);
	} catch (e) {
		$('#queueSnapshotBody').innerHTML = `<div class="error-block">加载队列深度失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载队列深度失败', 'error');
		console.error('load queue depth failed:', e);
	}
}

function renderQueueDepthChart(history) {
	// history: [{queue_name, minute_bucket, queued_size, active_size, ...}]
	// 按 minute_bucket 分组聚合成多条折线
	const buckets = [...new Set(history.map(h => h.minute_bucket))].sort();
	const queues = [...new Set(history.map(h => h.queue_name))].sort();
	const palette = ['#2563eb', '#059669', '#f59e0b', '#dc2626', '#7c3aed', '#0891b2', '#db2777'];

	if (buckets.length < 2) {
		return `<div class="empty">历史数据不足（当前样本数 ${buckets.length}），至少需要 2 个时间槽</div>`;
	}

	// 先构造 (bucket, queue) → size 的 Map，避免 O(n²) 的 history.find 嵌套循环 ——
	// 原实现在 queues × buckets 的双重循环内做 .find()，复杂度 O(Q*B*H)
	// 优化后先做一次 O(H) 建索引，后续 O(Q*B) 直接查 Map
	const depthMap = new Map();
	let globalMax = 1;
	for (const h of history) {
		const key = `${h.minute_bucket}||${h.queue_name}`;
		const total = (h.queued_size || 0) + (h.active_size || 0);
		depthMap.set(key, total);
		if (total > globalMax) globalMax = total;
	}

	const w = Math.max(800, buckets.length * 12), h = 400, pad = 48;
	const xStep = (w - 2 * pad) / (buckets.length - 1);
	const maxY = globalMax;
	const yPos = v => h - pad - (v / maxY) * (h - 2 * pad);

	// 网格
	let grid = '';
	for (let i = 0; i <= 5; i++) {
		const y = pad + (h - 2 * pad) * i / 5;
		const val = Math.round(maxY * (1 - i / 5));
		grid += `<line x1="${pad}" y1="${y}" x2="${w - pad}" y2="${y}" stroke="#e5e7eb" stroke-dasharray="3 3"/>`;
		grid += `<text x="${pad - 6}" y="${y + 4}" text-anchor="end" font-size="10" fill="#9ca3af">${val}</text>`;
	}

	// 折线：每个队列一条，从 Map 查询（O(1)）替代 .find()（O(H)）
	let polylines = '';
	queues.forEach((q, qi) => {
		const color = palette[qi % palette.length];
		const pts = buckets.map((b, bi) => {
			const val = depthMap.get(`${b}||${q}`) || 0;
			return `${pad + bi * xStep},${yPos(val)}`;
		}).join(' ');
		polylines += `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
	});

	// X 轴标签：只显示首尾 + 中间
	const labelIdx = [0, Math.floor(buckets.length / 2), buckets.length - 1].filter((v, i, a) => a.indexOf(v) === i);
	const xLabels = labelIdx.map(i => {
		const b = buckets[i] || '';
		const hm = b.slice(8, 10) + ':' + b.slice(10, 12);
		return `<text x="${pad + i * xStep}" y="${h - pad + 18}" text-anchor="middle" font-size="11" fill="#6b7280">${hm}</text>`;
	}).join('');

	// 图例
	const legend = queues.map((q, qi) =>
		`<div class="legend-item"><span class="legend-dot" style="background:${palette[qi % palette.length]}"></span>${escapeHtml(q)}</div>`
	).join('');

	return `
    <svg class="chart-svg chart-svg-fluid" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
      ${grid}${polylines}${xLabels}
    </svg>
    <div class="chart-legend">${legend}</div>`;
}

/* ---- Tab 5: 部门/团队使用统计 ---- */
async function loadOrgUsage() {
	const box = $('#orgUsageBody');
	try {
		const date = $('#orgUsageDate')?.value;
		const level = $('#orgLevel')?.value || 'team';
		let url = '/api/v1/analytics/org-usage/';
		const params = [];
		if (date) params.push('date=' + encodeURIComponent(date));
		if (level === 'dept') params.push('team_id=-1'); // 部门汇总哨兵值
		const finalUrl = params.length ? url + '?' + params.join('&') : url;

		const data = await api.getJson(finalUrl);
		const rows = data.rows || [];

		if (rows.length === 0) {
			box.innerHTML = `
        <div class="card card-empty">
          <div class="empty-emoji">🧾</div>
          <div class="text-lg fw-500 mb-8">暂无该日期的组织使用报表</div>
          <div class="text-sub">报表日期：${escapeHtml(date || data.date || '-')}（请等待凌晨聚合任务完成或切换到其他日期）</div>
        </div>`;
			return;
		}

		const headers = level === 'dept'
			? ['部门', 'QA 次数', '活跃用户', '总 Token', '预估费用（¥）', '平均延迟 (ms)', 'P95 延迟 (ms)', '好评率 (%)', '缓存命中数', '缓存命中率 (%)']
			: ['部门', '团队', 'QA 次数', '活跃用户', '总 Token', '预估费用（¥）', '平均延迟 (ms)', 'P95 延迟 (ms)', '好评率 (%)', '缓存命中数', '缓存命中率 (%)'];

		const fmtCost = v => '¥ ' + (v || 0).toFixed(4);
		const fmtPct = v => v == null ? '-' : (v * 100).toFixed(2) + '%';

		const tableRows = rows.map(r => {
			const deptCell = `<td>${escapeHtml(r.department_name || '-')}</td>`;
			const teamCell = level === 'team' ? `<td>${escapeHtml(r.team_name || '-')}</td>` : '';
			const cells = [
				(r.qa_count || 0).toLocaleString(),
				(r.user_count || 0).toLocaleString(),
				(r.total_tokens || 0).toLocaleString(),
				fmtCost(r.total_cost),
				(r.avg_latency_ms || 0).toLocaleString(),
				(r.p95_latency_ms || 0).toLocaleString(),
				fmtPct(r.good_feedback_rate),
				(r.cache_hit_count || 0).toLocaleString(),
				fmtPct(r.cache_hit_rate),
			].map(c => `<td>${c}</td>`).join('');
			return `<tr>${deptCell}${teamCell}${cells}</tr>`;
		}).join('');

		box.innerHTML = `
      <div class="card">
        <div class="card-title">🏢 ${level === 'dept' ? '部门' : '团队'}级使用统计 · ${escapeHtml(data.date || date || '-')}</div>
        <div class="table-container">
          <table class="table table-bordered">
            <thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead>
            <tbody>${tableRows}</tbody>
          </table>
        </div>
      </div>`;
	} catch (e) {
		box.innerHTML = `<div class="card card-error">加载组织统计失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载组织统计失败', 'error');
		console.error('load org usage failed:', e);
	}
}

/* ---- Tab 6: QA 记录列表（含分页 + 详情弹窗） ---- */
async function loadQaRecords() {
	const box = $('#qaRecordsBody');
	const pbox = $('#qaPagination');
	try {
		const start = $('#qaStartDate')?.value;
		const end = $('#qaEndDate')?.value;
		const params = [];
		if (start) params.push('start_date=' + encodeURIComponent(start));
		if (end) params.push('end_date=' + encodeURIComponent(end));
		params.push('page=' + qaPage);
		params.push('page_size=' + qaPageSize);
		const data = await api.getJson('/api/v1/analytics/qa-records/?' + params.join('&'));

		qaTotal = data.total || 0;
		const rows = data.rows || [];

		const typeBadge = t => {
		const map = { rag: ['tag-info', 'RAG'], chit_chat: ['tag-primary', '闲聊'], agent: ['tag-success', 'Agent'], cache: ['tag-warning', '缓存'] };
		const [cls, text] = map[t] || ['', t || '-'];
		return `<span class="tag ${cls}">${escapeHtml(text)}</span>`;
	};
		const ratingBadge = r => {
			if (r === 1) return '<span class="tag tag-success">👍 好评</span>';
			if (r === -1) return '<span class="tag tag-danger">👎 差评</span>';
			return '<span class="tag">-</span>';
		};

		const tableHtml = rows.length === 0
			? '<tr><td colspan="9" class="empty">暂无 QA 记录</td></tr>'
			: rows.map(r => `
          <tr class="tr-clickable" data-qa-id="${r.id}">
            <td>${r.id}</td>
            <td class="td-question" title="${escapeHtml(r.question)}">${escapeHtml(r.question)}</td>
            <td>${typeBadge(r.answer_type)}</td>
            <td>${r.is_hit_cache ? '<span class="tag tag-warning">是</span>' : '<span class="tag">-</span>'}</td>
            <td>${ratingBadge(r.rating)}</td>
            <td>${(r.latency_total_ms || 0).toLocaleString()} ms</td>
            <td>${(r.tokens_prompt || 0) + (r.tokens_completion || 0)}</td>
            <td>¥ ${(r.cost_estimate || 0).toFixed(4)}</td>
            <td class="text-sub text-sm">${formatDate(r.created_at)}</td>
          </tr>`).join('');

		box.innerHTML = `
      <table class="table table-bordered">
        <thead><tr>
          <th>ID</th><th>问题</th><th>回答类型</th><th>缓存</th><th>评分</th>
          <th>总延迟</th><th>Token 总数</th><th>预估费用</th><th>时间</th>
        </tr></thead>
        <tbody>${tableHtml}</tbody>
      </table>`;

		// QA 行点击：容器级事件委托，避免每行 setAttribute('onclick', ...) 的 eval 模式
		if (!box._qaRowListener) {
			box.addEventListener('click', (evt) => {
				const tr = evt.target.closest('tr[data-qa-id]');
				if (!tr) return;
				const id = parseInt(tr.getAttribute('data-qa-id'), 10);
				if (!isNaN(id)) showQaDetail(id);
			});
			box._qaRowListener = true;
		}

		renderPagination(pbox, qaPage, Math.ceil(qaTotal / qaPageSize), (p) => { qaPage = p; loadQaRecords(); });
	} catch (e) {
		box.innerHTML = `<div class="card card-error">加载 QA 记录失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载 QA 记录失败', 'error');
		console.error('load qa records failed:', e);
	}
}

/* 通用分页渲染 */
function renderPagination(container, current, totalPages, onClick) {
	if (!container) return;
	if (totalPages <= 1) { container.innerHTML = ''; return; }
	const show = [];
	const add = v => show.push(v);
	add(1);
	if (current - 1 > 2) add('...');
	for (let i = Math.max(2, current - 1); i <= Math.min(totalPages - 1, current + 1); i++) add(i);
	if (current + 1 < totalPages - 1) add('...');
	add(totalPages);

	container.innerHTML = `
    <button class="page-btn" data-page="${current - 1}" ${current <= 1 ? 'disabled' : ''}>上一页</button>
    ${show.map(p => p === '...'
		? `<span class="page-btn page-btn-ellipsis" disabled>…</span>`
		: `<button class="page-btn ${p === current ? 'active' : ''}" data-page="${p}">${p}</button>`).join('')}
    <button class="page-btn" data-page="${current + 1}" ${current >= totalPages ? 'disabled' : ''}>下一页</button>
    <span class="ml-8">第 ${current} / ${totalPages} 页，共 ${qaTotal || 0} 条</span>`;

	// 给所有带 data-page 的按钮绑定事件
	container.querySelectorAll('button[data-page]').forEach(btn => {
		btn.addEventListener('click', () => {
			if (btn.disabled) return;
			const page = parseInt(btn.getAttribute('data-page'), 10);
			if (!isNaN(page) && page >= 1 && page <= totalPages) onClick(page);
		});
	});
}

/* QA 详情弹窗 */
async function showQaDetail(id) {
	const box = $('#qaDetailBody');
	box.innerHTML = '<div class="text-sub text-loading">加载中...</div>';
	showModal('modal-qa-detail');
	try {
		// 调用后端新增的 qa_id 参数接口，直接查询单条（避免 page_size=100 的前 100 条限制）
		const d = await api.getJson(`/api/v1/analytics/qa-records/?qa_id=${encodeURIComponent(id)}`);
		const r = d.row;
		if (!r) { box.innerHTML = '<div class="error-block">未找到该 QA 记录</div>'; return; }
		box.innerHTML = `
      <div class="mb-16"><div class="text-sub text-sm mb-4">问题</div>
        <div class="qa-detail-question">${escapeHtml(r.question)}</div>
      </div>
      <div class="mb-16"><div class="text-sub text-sm mb-4">回答</div>
        <div class="qa-detail-answer">${escapeHtml(r.answer)}</div>
      </div>
      <div class="grid-2">
        <div><div class="text-sub text-sm">回答类型</div><div>${escapeHtml(r.answer_type || '-')}</div></div>
        <div><div class="text-sub text-sm">领域</div><div>${escapeHtml(r.root_type || '-')}</div></div>
        <div><div class="text-sub text-sm">总延迟</div><div>${(r.latency_total_ms || 0).toLocaleString()} ms</div></div>
        <div><div class="text-sub text-sm">缓存命中</div><div>${r.is_hit_cache ? '是' : '否'}</div></div>
        <div><div class="text-sub text-sm">Prompt Token</div><div>${(r.tokens_prompt || 0).toLocaleString()}</div></div>
        <div><div class="text-sub text-sm">Completion Token</div><div>${(r.tokens_completion || 0).toLocaleString()}</div></div>
        <div><div class="text-sub text-sm">预估费用</div><div>¥ ${(r.cost_estimate || 0).toFixed(4)}</div></div>
        <div><div class="text-sub text-sm">时间</div><div>${formatDate(r.created_at)}</div></div>
      </div>`;
	} catch (err) {
		box.innerHTML = `<div class="error-block">加载失败：${escapeHtml(err.message || '')}</div>`;
	}
}

/* ---- Tab 8: 日报详情（今日 vs 昨日对比 + 多日趋势折线图） ---- */
async function loadDailyReport() {
	const box = $('#dailyBody');
	try {
		// 并行拉取日报对比数据和趋势数据，减少等待时间
		const trendDays = $('#dailyTrendDays')?.value || 30;
		const rootType = getSelectedRootType();
		const sep = rootType ? '&' : '?';
		const rtQ = rootType ? `?root_type=${encodeURIComponent(rootType)}` : '';
		const [dailyData, trendData] = await Promise.all([
			api.getJson('/api/v1/analytics/daily/' + rtQ),
			api.getJson(`/api/v1/analytics/trend/?days=${trendDays}${rootType ? sep + 'root_type=' + encodeURIComponent(rootType) : ''}`),
		]);

		const t = dailyData.today || {};
		const y = dailyData.yesterday || {};

		const fields = [
			{ label: '日期', tf: v => v, yf: v => v },
			{ label: 'QA 次数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true },
			{ label: '好评数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true },
			{ label: '差评数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true, warn: true },
			{ label: '准确率', tf: v => (v * 100 || 0).toFixed(2) + '%', yf: v => (v * 100 || 0).toFixed(2) + '%', cmp: true },
		];

		const diff = (tVal, yVal, warn) => {
			if (tVal == null || yVal == null || (yVal === 0 && tVal === 0)) return '';
			let delta, pct;
			if (typeof tVal === 'number' && typeof yVal === 'number') {
				delta = tVal - yVal;
				pct = yVal === 0 ? null : (delta / Math.abs(yVal)) * 100;
			} else { return ''; }
			const arrow = delta > 0 ? '▲' : delta < 0 ? '▼' : '·';
			/* warn=true 表示"差评数"等反向指标：上升为红，下降为绿；其他正向指标则上升绿下降红 */
			let cls = '';
			if (delta !== 0) {
				if (warn) cls = delta > 0 ? 'diff-down' : 'diff-up';
				else cls = delta > 0 ? 'diff-up' : 'diff-down';
			}
			const pctStr = pct == null ? '—' : (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
			return `<span class="text-sm ml-8 ${cls}">${arrow} ${Math.abs(delta).toLocaleString()} (${pctStr})</span>`;
		};

		// 缓存趋势数据，勾选指标时直接重渲染无需重新请求 API
		dailyTrendData = trendData.trend || [];

		// 渲染多日趋势折线图：QA次数 / 好评 / 差评 / 准确率（双 Y 轴）
		const trendChartHtml = renderDailyTrendChart(dailyTrendData);

		box.innerHTML = `
      ${trendChartHtml}
      <div class="card mt-16">
        <div class="card-title">📅 每日摘要对比</div>
        <table class="table table-bordered">
          <thead><tr>
            <th>指标</th><th>今日 (${escapeHtml(t.date || '-')})</th><th>昨日 (${escapeHtml(y.date || '-')})</th><th>环比</th>
          </tr></thead>
          <tbody>
            ${fields.map(f => {
	const tv = t[f.label === '日期' ? 'date' : (f.label === 'QA 次数' ? 'qa_count' : f.label === '好评数' ? 'good' : f.label === '差评数' ? 'bad' : f.label === '准确率' ? 'accuracy' : '')];
	const yv = y[f.label === '日期' ? 'date' : (f.label === 'QA 次数' ? 'qa_count' : f.label === '好评数' ? 'good' : f.label === '差评数' ? 'bad' : f.label === '准确率' ? 'accuracy' : '')];
	const cmpEl = f.cmp ? diff(tv, yv, f.warn) : '';
	return `<tr><td>${f.label}</td><td>${f.tf(tv)}</td><td>${f.yf(yv)}</td><td>${cmpEl}</td></tr>`;
}).join('')}
          </tbody>
        </table>
      </div>`;

		// 日报趋势图 checkbox + 天数选择器事件委托：绑在 #dailyBody 上（不会被 toggleDailyMetric 替换）
		if (!box._dailyListener) {
			box.addEventListener('change', (evt) => {
				const cb = evt.target.closest('input[data-daily-metric]');
				if (cb) { toggleDailyMetric(cb.getAttribute('data-daily-metric')); return; }
				const sel = evt.target.closest('select[data-action="reload-daily"]');
				if (sel) { loadDailyReport(); return; }
			});
			box._dailyListener = true;
		}
	} catch (e) {
		box.innerHTML = `<div class="card card-error">加载日报失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载日报失败', 'error');
		console.error('load daily report failed:', e);
	}
}

/**
 * 渲染日报趋势折线图（双 Y 轴 SVG）
 * - 左轴：QA次数 / 好评数 / 差评数（计数值，共用一个量纲）
 * - 右轴：准确率（百分比，0-100%）
 * - 输入 trend: [{date, qa_count, good, bad, accuracy, avg_total_ms, avg_ttft_ms}, ...]
 */
function renderDailyTrendChart(trend) {
	if (!trend || trend.length === 0) {
		return '<div class="card mb-16"><div class="card-title">📈 多日趋势</div><div class="empty">暂无趋势数据</div></div>';
	}
	if (trend.length === 1) {
		return '<div class="card mb-16"><div class="card-title">📈 多日趋势</div><div class="empty">仅 1 天数据，暂无法绘制趋势图</div></div>';
	}

	/* 读取当前选中的天数，重渲染时保持选中项不变 */
	const curDays = $('#dailyTrendDays')?.value || '30';

	const w = 760, h = 230, pad = 26, padR = 50;
	const days = trend.map(t => t.date.slice(5)); // MM-DD
	const qaCounts = trend.map(t => t.qa_count || 0);
	const goods = trend.map(t => t.good || 0);
	const bads = trend.map(t => t.bad || 0);
	const accs = trend.map(t => (t.accuracy || 0) * 100);

	// 左轴范围：仅统计已勾选指标的最大值，避免未显示指标拉伸 Y 轴
	const leftVals = [];
	if (dailyMetricVisible.qa) leftVals.push(...qaCounts);
	if (dailyMetricVisible.good) leftVals.push(...goods);
	if (dailyMetricVisible.bad) leftVals.push(...bads);
	const leftMax = Math.max(...leftVals, 1);
	const leftMin = 0;

	// 右轴范围（准确率），仅当准确率勾选时才计算
	const showRight = dailyMetricVisible.accuracy;
	let rightMin = 0, rightMax = 100;
	if (showRight) {
		rightMin = Math.min(...accs) - 5;
		rightMax = Math.max(...accs) + 5;
		rightMin = Math.max(0, rightMin);
		rightMax = Math.min(100, rightMax);
		if (rightMax - rightMin < 1) { rightMin = 0; rightMax = 100; }
	}

	const xStep = (w - pad - padR) / (days.length - 1);
	// 左轴坐标映射
	const yLeft = v => h - pad - ((v - leftMin) / (leftMax - leftMin)) * (h - 2 * pad);
	// 右轴坐标映射（准确率）
	const yRight = v => h - pad - ((v - rightMin) / (rightMax - rightMin)) * (h - 2 * pad);

	// 折线点坐标
	const linePts = arr => arr.map((v, i) => `${pad + i * xStep},${yLeft(v)}`).join(' ');
	const accPts = accs.map((v, i) => `${pad + i * xStep},${yRight(v)}`).join(' ');

	// 网格线 + 左轴刻度
	let grid = '';
	for (let i = 0; i <= 5; i++) {
		const yv = pad + (h - 2 * pad) * i / 5;
		const label = Math.round(leftMax - (leftMax - leftMin) * i / 5);
		grid += `<line x1="${pad}" y1="${yv}" x2="${w - padR}" y2="${yv}" stroke="#e5e7eb" stroke-dasharray="3 3"/>`;
		grid += `<text x="${pad - 6}" y="${yv + 4}" text-anchor="end" font-size="10" fill="#9ca3af">${label}</text>`;
	}
	// 右轴刻度（仅当准确率勾选时显示）
	if (showRight) {
		for (let i = 0; i <= 5; i++) {
			const yv = pad + (h - 2 * pad) * i / 5;
			const label = (rightMax - (rightMax - rightMin) * i / 5).toFixed(0) + '%';
			grid += `<text x="${w - padR + 6}" y="${yv + 4}" text-anchor="start" font-size="10" fill="#9ca3af">${label}</text>`;
		}
	}

	// X 轴标签（数据多时隔点显示，避免重叠）
	const xLabels = days.map((d, i) => {
		const showEvery = days.length > 20 ? 5 : (days.length > 10 ? 3 : 1);
		if (i % showEvery !== 0 && i !== days.length - 1) return '';
		return `<text x="${pad + i * xStep}" y="${h - pad + 16}" text-anchor="middle" font-size="10" fill="#6b7280">${d}</text>`;
	}).join('');

	// 数据点圆点
	const dots = (arr, color, useRight) => arr.map((v, i) => {
		const yy = useRight ? yRight(v) : yLeft(v);
		return `<circle cx="${pad + i * xStep}" cy="${yy}" r="2.5" fill="${color}"/>`;
	}).join('');

	// 按勾选状态动态生成折线和数据点
	let polylines = '';
	let dotHtml = '';
	if (dailyMetricVisible.qa) {
		polylines += `<polyline points="${linePts(qaCounts)}" fill="none" stroke="#2563eb" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
		dotHtml += dots(qaCounts, '#2563eb', false);
	}
	if (dailyMetricVisible.good) {
		polylines += `<polyline points="${linePts(goods)}" fill="none" stroke="#059669" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
		dotHtml += dots(goods, '#059669', false);
	}
	if (dailyMetricVisible.bad) {
		polylines += `<polyline points="${linePts(bads)}" fill="none" stroke="#dc2626" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
		dotHtml += dots(bads, '#dc2626', false);
	}
	if (showRight) {
		polylines += `<polyline points="${accPts}" fill="none" stroke="#7c3aed" stroke-width="2" stroke-dasharray="6 3" stroke-linecap="round" stroke-linejoin="round"/>`;
		dotHtml += dots(accs, '#7c3aed', true);
	}

	/* 左侧勾选框列：布局抽到 .chart-sidebar，.daily-sidebar 仅覆盖 min-width */
	const sidebarHtml = `
      <div class="chart-sidebar daily-sidebar">
        <label class="checkbox"><input type="checkbox" ${dailyMetricVisible.qa ? 'checked' : ''} data-daily-metric="qa"><span class="metric-dot dot-blue"></span>QA次数</label>
        <label class="checkbox"><input type="checkbox" ${dailyMetricVisible.good ? 'checked' : ''} data-daily-metric="good"><span class="metric-dot dot-green"></span>好评</label>
        <label class="checkbox"><input type="checkbox" ${dailyMetricVisible.bad ? 'checked' : ''} data-daily-metric="bad"><span class="metric-dot dot-red"></span>差评</label>
        <label class="checkbox"><input type="checkbox" ${dailyMetricVisible.accuracy ? 'checked' : ''} data-daily-metric="accuracy"><span class="metric-dot dot-purple"></span>准确率</label>
      </div>`;

	/* 标题放在顶部，左右两栏：勾选框栏 + SVG图；天数选择器放标题右侧并减小大小 */
	return `
      <div class="chart-wrap">
        <div class="daily-chart-header">
          <div class="text-lg fw-600">📈 最近 ${trend.length} 天趋势</div>
          <select class="select select-xs" data-action="reload-daily" id="dailyTrendDays">
            <option value="7" ${curDays === '7' ? 'selected' : ''}>7 天</option>
            <option value="14" ${curDays === '14' ? 'selected' : ''}>14 天</option>
            <option value="30" ${curDays === '30' ? 'selected' : ''}>30 天</option>
          </select>
        </div>
        <div class="chart-row">
          ${sidebarHtml}
          <div class="chart-container chart-container-flex" style="height:${h + 20}px">
            <svg class="chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
              ${grid}${xLabels}${polylines}${dotHtml}
            </svg>
          </div>
        </div>
      </div>`;
}

/**
 * 切换日报趋势图中某条指标线的显示/隐藏
 * 使用缓存的 dailyTrendData 直接重渲染，无需重新请求 API
 */
function toggleDailyMetric(metric) {
	dailyMetricVisible[metric] = !dailyMetricVisible[metric];
	// 至少保留一条指标，避免全部隐藏后图表空白
	if (!Object.values(dailyMetricVisible).some(v => v)) {
		dailyMetricVisible[metric] = true;
	}
	// 仅替换图表区域，不重载表格
	const dailyBody = $('#dailyBody');
	const chartWrap = dailyBody.querySelector('.chart-wrap');
	if (chartWrap) {
		chartWrap.outerHTML = renderDailyTrendChart(dailyTrendData);
	}
}
