/* ============ 反馈与准确率报表 ============ */

let currentTimeRange = 'week';
let showAccuracy = true;
let showLatency = true;
let customDateStart = null;  // ISO 字符串 'YYYY-MM-DD'
let customDateEnd = null;

document.addEventListener('DOMContentLoaded', () => {
	initAnalyticsPage();
});

async function initAnalyticsPage() {
	await loadRootTypes();
	await loadOverview();
	await loadTrend();
	await loadKeywords();
	await loadBadFeedbacks();
}

/* ---- 通用弹窗 ---- */
function showModal(id) {
	const m = document.getElementById(id);
	if (m) m.classList.add('show');
	let mask = document.getElementById('mask');
	if (mask) mask.classList.add('show');
}

/* ---- 概览统计 ---- */
async function loadOverview() {
	const kpiValues = $$('.kpi-value');
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/overview/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);

		kpiValues[0].textContent = data.total_qa.toLocaleString();
		kpiValues[1].innerHTML = (data.accuracy * 100).toFixed(1) + '<span class="text-sm text-sub">%</span>';
		kpiValues[2].innerHTML = (data.avg_latency_ms / 1000).toFixed(2) + '<span class="text-sm text-sub">s</span>';
		kpiValues[3].textContent = data.active_users;
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
		if (chart) chart.innerHTML = renderTrendChart(trend);
	} catch (e) {
		const chart = $('#trendChart');
		if (chart) chart.innerHTML = '<div style="text-align:center;padding:40px;color:var(--danger)">加载趋势数据失败</div>';
		toast('加载趋势数据失败', 'error');
		console.error('load trend failed:', e);
	}
}

function renderTrendChart(trend) {
  if (!trend || trend.length === 0) {
    return '<div style="text-align:center;padding:40px;color:var(--text-sub)">暂无数据</div>';
  }
  if (trend.length === 1) {
    return '<div style="text-align:center;padding:40px;color:var(--text-sub)">仅 1 天数据，暂无法绘制趋势图</div>';
  }

  const w = 900, h = 260, pad = 40;
  const days = trend.map(t => t.date.slice(5));
  const sat = trend.map(t => t.accuracy * 100);
  const rt = trend.map(t => t.avg_latency_ms / 1000);

  const xStep = (w - 2 * pad) / (days.length - 1);
  const yMin = 70, yMax = 100;
  let y2Min = Math.min(...rt) - 0.5;
  let y2Max = Math.max(...rt) + 0.5;
  if (Math.abs(y2Max - y2Min) < 0.01) {
    y2Min -= 0.5;
    y2Max += 0.5;
  }

	const yPos = (v, mi, ma) => h - pad - ((v - mi) / (ma - mi)) * (h - 2 * pad);
	const p1 = showAccuracy ? sat.map((v, i) => `${pad + i * xStep},${yPos(v, yMin, yMax)}`).join(' ') : '';
	const p2 = showLatency ? rt.map((v, i) => `${pad + i * xStep},${yPos(v, y2Min, y2Max)}`).join(' ') : '';

	let grid = '';
	for (let i = 0; i <= 5; i++) {
		const y = pad + (h - 2 * pad) * i / 5;
		grid += `<line x1="${pad}" y1="${y}" x2="${w - pad}" y2="${y}" stroke="#e5e7eb" stroke-dasharray="3 3"/>`;
		grid += `<text x="${pad - 6}" y="${y + 4}" text-anchor="end" font-size="10" fill="#9ca3af">${(100 - i * 6).toFixed(0)}</text>`;
	}

	const xLabels = days.map((d, i) => `<text x="${pad + i * xStep}" y="${h - pad + 16}" text-anchor="middle" font-size="11" fill="#6b7280">${d}</text>`).join('');
	const dots1 = showAccuracy ? sat.map((v, i) => `<circle cx="${pad + i * xStep}" cy="${yPos(v, yMin, yMax)}" r="3.5" fill="#2563eb"/><text x="${pad + i * xStep}" y="${yPos(v, yMin, yMax) - 8}" text-anchor="middle" font-size="10" fill="#2563eb" font-weight="600">${v.toFixed(1)}%</text>`).join('') : '';
	const dots2 = showLatency ? rt.map((v, i) => `<circle cx="${pad + i * xStep}" cy="${yPos(v, y2Min, y2Max)}" r="3.5" fill="#f59e0b"/>`).join('') : '';

	return `<svg class="chart-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="xMidYMid meet">
    ${grid}${xLabels}
    ${showAccuracy ? `<polyline points="${p1}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>` : ''}
    ${showLatency ? `<polyline points="${p2}" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="6 3"/>` : ''}
    ${dots1}${dots2}
  </svg>`;
}

/* ---- 关键词表格 ---- */
async function loadKeywords() {
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/keywords/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const keywords = data.rows || [];

		const kwBody = $('#keywordsTableBody');
		if (kwBody) {
			kwBody.innerHTML = keywords.map(k => `
        <tr>
          <td class="fw-500">${escapeHtml(k.keyword)}</td>
          <td><span class="tag ${k.weight_score > 1 ? 'tag-success' : (k.weight_score < 1 ? 'tag-warning' : '')}">×${k.weight_score.toFixed(1)}</span></td>
          <td class="text-sub text-sm">${k.hit_count || 0} 次命中 · ${k.good_feedback || 0} 好评 · ${k.bad_feedback || 0} 差评</td>
          <td>
            <div class="table-actions">
              <button class="btn-link btn-sm" onclick="adjustKeywordWeight(${k.id}, 0.1)">+0.1</button>
              <button class="btn-link btn-sm" onclick="adjustKeywordWeight(${k.id}, -0.1)">-0.1</button>
            </div>
          </td>
        </tr>
      `).join('');
		}
	} catch (e) {
		const kwBody = $('#keywordsTableBody');
		if (kwBody) kwBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--danger);padding:20px">加载关键词数据失败</td></tr>';
		toast('加载关键词数据失败', 'error');
		console.error('load keywords failed:', e);
	}
}

async function adjustKeywordWeight(id, delta) {
	try {
		await api.put(`/api/v1/analytics/keywords/${id}/`, { delta: delta });
		toast(delta > 0 ? '已加权 +0.1' : '已降权 -0.1', 'success');
		loadKeywords();
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

	if (!keyword) {
		toast('请输入关键词', 'error');
		return;
	}
	if (isNaN(weight) || weight < 0.1 || weight > 5.0) {
		toast('权重范围 0.1 ~ 5.0', 'error');
		return;
	}

	try {
		await api.postJson('/api/v1/analytics/keywords/', { keyword: keyword, weight_score: weight, root_type: rootType });
		toast('已新增关键词', 'success');
		closeAllOverlays();
		loadKeywords();
	} catch (e) {
		toast(e.message || '添加失败', 'error');
	}
}

/* ---- 差评反馈列表 ---- */
async function loadBadFeedbacks() {
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/bad-feedbacks/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const feedbacks = data.rows || [];

		const fbList = $('#feedbackList');
		if (fbList) {
			fbList.innerHTML = feedbacks.map(f => {
				const isResolved = f.status === 'resolved';
				return `
        <div style="padding:12px;background:#fef2f2;border-left:3px solid var(--danger);border-radius:var(--radius);font-size:13px">
          <div class="fw-500 mb-8">Q: ${escapeHtml(f.question)}</div>
          <div class="text-sub text-sm mb-8">A（摘要）: ${escapeHtml(f.answer)}</div>
          <div style="padding:6px 8px;background:var(--white);border-radius:var(--radius-sm);color:var(--text)"><b>反馈：</b>${escapeHtml(f.comment || '无详细反馈')}</div>
          <div class="flex justify-between mt-8">
            <span class="text-sub text-sm">${escapeHtml(f.user)} · ${formatDate(f.created_at)}${isResolved ? ' · <span class="tag tag-success">已处理</span>' : ''}</span>
            <div class="flex gap-4">
              <button class="btn-link btn-sm" onclick="adjustKeywordWeightByFeedback(${f.id})">调整权重</button>
              ${isResolved ? '' : `<button class="btn-link btn-sm" onclick="markFeedbackProcessed(${f.id})">已处理</button>`}
            </div>
          </div>
        </div>
      `}).join('');
		}
	} catch (e) {
		const fbList = $('#feedbackList');
		if (fbList) fbList.innerHTML = '<div style="text-align:center;color:var(--danger);padding:20px">加载反馈数据失败</div>';
		toast('加载反馈数据失败', 'error');
		console.error('load bad feedbacks failed:', e);
	}
}

function adjustKeywordWeightByFeedback(fbId) {
	toast('请在关键词列表中手动调整相关关键词权重', '');
	const kwTable = $('#keywordsTableBody');
	if (kwTable) kwTable.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function markFeedbackProcessed(fbId) {
	try {
		await api.put(`/api/v1/analytics/bad-feedbacks/${fbId}/`, { status: 'resolved' });
		toast('已标记为已处理', 'success');
		loadBadFeedbacks();
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
	const timeBtns = $$('.page-actions .btn');
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
	const titleEl = document.querySelector('.chart-wrap .text-lg');
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

/* ---- 自定义日期范围弹窗 ---- */
function showCustomDateRange() {
	const today = new Date().toISOString().slice(0, 10);
	const weekAgo = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10);
	const startInput = document.getElementById('customDateStart');
	const endInput = document.getElementById('customDateEnd');
	if (startInput) {
		startInput.value = customDateStart || weekAgo;
		startInput.max = today;
	}
	if (endInput) {
		endInput.value = customDateEnd || today;
		endInput.max = today;
	}
	showModal('modal-date-range');
}

function applyCustomDateRange() {
	const start = document.getElementById('customDateStart')?.value;
	const end = document.getElementById('customDateEnd')?.value;
	if (!start || !end) {
		toast('请选择开始日期和结束日期', 'error');
		return;
	}
	if (start > end) {
		toast('开始日期不能晚于结束日期', 'error');
		return;
	}
	customDateStart = start;
	customDateEnd = end;
	currentTimeRange = 'custom';
	closeAllOverlays();
	updateTimeButtons('custom');
	updateChartTitle('custom');
	loadTrend();
	toast(`已切换至自定义范围：${start} ~ ${end}`, 'success');
}

/* ---- 图表显示切换 ---- */
function toggleChartDisplay(type) {
	if (type === 'accuracy') {
		showAccuracy = !showAccuracy;
	} else if (type === 'latency') {
		showLatency = !showLatency;
	}
	loadTrend();
}

/* ---- 导出报表 ---- */
async function exportReport() {
	try {
		let url;
		if (currentTimeRange === 'custom' && customDateStart && customDateEnd) {
			url = `/api/v1/analytics/trend/?start_date=${customDateStart}&end_date=${customDateEnd}`;
		} else {
			const days = currentTimeRange === 'today' ? 1 : (currentTimeRange === 'week' ? 7 : 30);
			url = `/api/v1/analytics/trend/?days=${days}`;
		}
		const data = await api.getJson(url);

		let csv = '日期,问答数,好评数,差评数,准确率(%),平均耗时(ms)\n';
		data.trend.forEach(t => {
			csv += `${t.date},${t.qa_count},${t.good},${t.bad},${(t.accuracy * 100).toFixed(2)},${t.avg_latency_ms}\n`;
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

