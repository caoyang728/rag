/**
 * admin-eval.js - RAG 质量评估中心前端逻辑
 *
 * 功能模块:
 * 1. 黄金测试集管理: 创建/导入/删除测试集
 * 2. 检索质量评估: 执行离线检索评估 + 查看历史报告
 * 3. 回答质量评估: 多维度回答评估 + 手动触发
 * 4. 文档质量报告: 文档质量汇总 + 批量评估
 * 5. 覆盖率报告: 热门问题覆盖 + 知识空白 + 领域覆盖
 * 6. 反馈闭环: 差评自动关联问题 chunk
 */

let datasetsCache = [];

/* ============ 通用 ============ */
function switchEvalTab(name) {
	$$('#evalTabs .tab-item').forEach(el => el.classList.toggle('active', el.getAttribute('data-tab') === name));
	$$('.eval-scroll .tab-panel').forEach(p => {
		const isActive = p.getAttribute('data-panel') === name;
		p.classList.toggle('active', isActive);
		if (isActive) {
			// 重新触发 fadeIn 动画
			p.style.animation = 'none';
			p.offsetHeight; // 强制 reflow
			p.style.animation = '';
		}
	});
	loadTabData(name);
}

function loadTabData(name) {
	switch (name) {
		case 'golden': loadDatasets(); break;
		case 'retrieval': loadRetrievalTab(); break;
		case 'answer': loadAnswerScores(); break;
		case 'doc': loadDocQuality(); break;
		case 'coverage': loadCoverage(); break;
		case 'feedback': break;
	}
}

function closeDialog(id) {
	$('#' + id).style.display = 'none';
}

/** 根据分数返回语义化的 CSS 类名 */
function scorePillClass(score) {
	if (score >= 0.8) return 'score-high';
	if (score >= 0.6) return 'score-mid';
	return 'score-low';
}

/** 0-100 分制（如文档质量分）对应的 CSS 类名 */
function qualityScoreClass(score) {
	if (score >= 85) return 'score-high';
	if (score >= 70) return 'score-mid';
	return 'score-low';
}

/** 渲染分数为彩色胶囊 */
function scorePill(score, formatter) {
	const text = formatter ? formatter(score) : score;
	return `<span class="score-pill ${scorePillClass(score)}">${text}</span>`;
}

/* ============ 黄金测试集 ============ */
async function loadDatasets() {
	try {
		const data = await api.getJson('/api/v1/analytics/golden-datasets/');
		datasetsCache = data.rows || [];
		const total = datasetsCache.length;
		const totalQuestions = datasetsCache.reduce((s, d) => s + d.question_count, 0);
		$('#datasetSummary').textContent = `共 ${total} 个测试集，${totalQuestions} 个问题`;

		const tbody = $('#datasetTableBody');
		if (!datasetsCache.length) {
			tbody.innerHTML = `
				<tr>
					<td colspan="8">
						<div class="empty-state">
							<div class="empty-state-icon">📋</div>
							<div>暂无测试集，点击右上角"创建测试集"开始</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = datasetsCache.map(d => `
			<tr>
				<td>${d.id}</td>
				<td>${escapeHtml(d.name)}</td>
				<td><span class="tag">${escapeHtml(d.root_type)}</span></td>
				<td>${d.question_count}</td>
				<td>${escapeHtml(d.version)}</td>
				<td><span class="badge ${badgeClass(d.status)}">${statusLabel(d.status)}</span></td>
				<td>${formatDate(d.updated_at)}</td>
				<td>
					<button class="btn btn-sm" onclick="viewDataset(${d.id})">查看</button>
					<button class="btn btn-sm btn-danger" onclick="deleteDataset(${d.id})">删除</button>
				</td>
			</tr>
		`).join('');
	} catch (e) {
		toast('加载失败: ' + e.message, 'error');
	}
}

function statusLabel(s) {
	return { draft: '草稿', active: '已启用', archived: '已归档' }[s] || s;
}

/** 白名单映射状态到 CSS 类名，防止 status 字段注入 CSS */
function badgeClass(s) {
	return { draft: 'badge-default', active: 'badge-success', archived: 'badge-warning' }[s] || 'badge-default';
}

function showCreateDatasetDialog() {
	$('#createDialog').style.display = 'flex';
	$('#dsName').value = '';
	$('#dsDesc').value = '';
	$('#dsVersion').value = 'v1';
}

async function createDataset() {
	const name = $('#dsName').value.trim();
	if (!name) { toast('请输入测试集名称', 'error'); return; }
	const payload = {
		name,
		root_type: $('#dsRootType').value,
		version: $('#dsVersion').value,
		description: $('#dsDesc').value,
	};
	try {
		await api.post('/api/v1/analytics/golden-datasets/', payload);
		toast('创建成功', 'success');
		closeDialog('createDialog');
		loadDatasets();
	} catch (e) {
		toast('创建失败: ' + e.message, 'error');
	}
}

async function deleteDataset(id) {
	if (!confirm('确定删除此测试集？关联的问题和标注也会被删除。')) return;
	try {
		await api.delete(`/api/v1/analytics/golden-datasets/${id}/`);
		toast('删除成功', 'success');
		loadDatasets();
	} catch (e) {
		toast('删除失败: ' + e.message, 'error');
	}
}

async function viewDataset(id) {
	try {
		const data = await api.getJson(`/api/v1/analytics/golden-datasets/${id}/`);
		const rows = data.questions || [];
		if (!rows.length) {
			toast('此测试集暂无问题，可点击"批量导入"添加', 'info');
			return;
		}
		const lines = [`测试集: ${data.name}`, `共 ${rows.length} 个问题:`, ''];
		rows.slice(0, 5).forEach((q, i) => {
			const question = (q.question || '').substring(0, 50);
			lines.push(`${i + 1}. ${question}... [难度:${q.difficulty}]`);
		});
		if (rows.length > 5) lines.push('', `... 还有 ${rows.length - 5} 个问题`);
		toast(lines.join('\n'), 'info');
	} catch (e) {
		toast('加载失败', 'error');
	}
}

function showImportDialog() {
	if (!datasetsCache.length) {
		toast('请先创建测试集', 'error');
		return;
	}
	const sel = $('#importDatasetSel');
	sel.innerHTML = datasetsCache.map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join('');
	$('#importJson').value = '';
	$('#importDialog').style.display = 'flex';
}

async function importQuestions() {
	const dsId = $('#importDatasetSel').value;
	const jsonText = $('#importJson').value.trim();
	if (!jsonText) { toast('请输入 JSON 数据', 'error'); return; }
	let questions;
	try {
		questions = JSON.parse(jsonText);
	} catch (e) {
		toast('JSON 解析失败: ' + e.message, 'error');
		return;
	}
	// 校验为数组，避免用户输入对象或字符串通过解析但在后端报错
	if (!Array.isArray(questions)) {
		toast('JSON 数据必须是数组格式', 'error');
		return;
	}
	if (!questions.length) {
		toast('JSON 数据不能为空', 'error');
		return;
	}
	try {
		const result = await api.postJson(`/api/v1/analytics/golden-datasets/${dsId}/import/`, { questions });
		toast(`导入成功: 创建 ${result.created}, 更新 ${result.updated}`, 'success');
		closeDialog('importDialog');
		loadDatasets();
	} catch (e) {
		toast('导入失败: ' + e.message, 'error');
	}
}

/* ============ 检索质量 ============ */
function loadRetrievalTab() {
	loadDatasetOptions('#retrievalDatasetSel');
	loadRetrievalReports();
}

function loadDatasetOptions(selId) {
	const sel = $(selId);
	if (!sel || !datasetsCache.length) return;
	sel.innerHTML = '<option value="">选择测试集</option>' +
		datasetsCache.map(d => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join('');
}

async function loadRetrievalReports() {
	try {
		const data = await api.getJson('/api/v1/analytics/eval/retrieval-reports/');
		const rows = data.rows || [];
		const tbody = $('#retrievalReportBody');
		if (!rows.length) {
			tbody.innerHTML = `
				<tr>
					<td colspan="8">
						<div class="empty-state">
							<div class="empty-state-icon">🔍</div>
							<div>暂无评估报告，选择测试集后点击"执行检索评估"</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = rows.map(r => `
			<tr>
				<td>${r.id}</td>
				<td>${r.dataset_id}</td>
				<td>${scorePill(r.recall_at_5, fmtPct)}</td>
				<td>${scorePill(r.recall_at_10, fmtPct)}</td>
				<td>${scorePill(r.recall_at_20, fmtPct)}</td>
				<td>${scorePill(r.mrr, fmtPct)}</td>
				<td>${scorePill(r.ndcg_at_10, fmtPct)}</td>
				<td>${formatDate(r.created_at)}</td>
			</tr>
		`).join('');

		// 展示最新报告的 KPI（带语义着色）
		const latest = rows[0];
		if (latest) {
			$('#retrievalKpi').style.display = 'grid';
			setKpiValue('rAt5', latest.recall_at_5, fmtPct);
			setKpiValue('rAt10', latest.recall_at_10, fmtPct);
			setKpiValue('rAt20', latest.recall_at_20, fmtPct);
			setKpiValue('rMrr', latest.mrr, fmtPct);
			setKpiValue('rNdcg', latest.ndcg_at_10, fmtPct);
			// 数值化兜底，避免 questions_with_hits/total_questions 为 undefined 时得到 NaN
			const totalQ = Number(latest.total_questions) || 0;
			const hitQ = Number(latest.questions_with_hits) || 0;
			const hitRate = totalQ > 0 ? (hitQ / totalQ) : 0;
			setKpiValue('rHitRate', hitRate, v => (v * 100).toFixed(1) + '%');

			drawGainChart(latest);
		}
	} catch (e) {
		toast('加载失败', 'error');
	}
}

/** 设置 KPI 值并根据分数着色 */
function setKpiValue(elId, value, formatter) {
	const el = $('#' + elId);
	if (!el) return;
	const formatted = formatter ? formatter(value) : value;
	el.textContent = formatted;
	el.classList.remove('val-good', 'val-mid', 'val-poor');
	if (typeof value === 'number') {
		el.classList.add(scorePillClass(value));
	}
}

async function runRetrievalEval() {
	const dsId = $('#retrievalDatasetSel').value;
	if (!dsId) { toast('请选择测试集', 'error'); return; }
	toast('正在执行评估，可能需要几分钟...', 'info');
	try {
		const result = await api.postJson('/api/v1/analytics/eval/retrieval/', { dataset_id: parseInt(dsId) });
		toast(`评估完成: Recall@10=${fmtPct(result.recall_at_10)}`, 'success');
		loadRetrievalReports();
	} catch (e) {
		toast('评估失败: ' + e.message, 'error');
	}
}

function drawGainChart(report) {
	const svg = $('#gainChart');
	if (!svg) return;

	// Number() 强制数值化，防止后端字段被污染为字符串导致 SVG 注入
	const num = (v) => Number(v) || 0;
	const data = [
		{ label: '向量', value: num(report.vector_recall_at_10), color: '#3b82f6' },
		{ label: 'BM25', value: num(report.bm25_recall_at_10), color: '#8b5cf6' },
		{ label: '混合', value: num(report.hybrid_recall_at_10), color: '#06b6d4' },
		{ label: 'Rerank', value: num(report.rerank_recall_at_10), color: '#10b981' },
	];

	const w = 500, h = 200, padLeft = 40, padBottom = 30, padTop = 10;
	const barW = (w - padLeft - 20) / data.length - 10;
	const maxVal = Math.max(...data.map(d => d.value), 0.1) * 1.2;

	let html = '';
	// Y轴网格
	for (let i = 0; i <= 4; i++) {
		const y = padTop + (h - padTop - padBottom) * i / 4;
		const val = (maxVal * (1 - i / 4)).toFixed(2);
		html += `<line x1="${padLeft}" y1="${y}" x2="${w - 10}" y2="${y}" stroke="#e5e7eb" stroke-width="1"/>`;
		html += `<text x="${padLeft - 5}" y="${y + 4}" text-anchor="end" fill="#6b7280" font-size="11">${(val * 100).toFixed(0)}%</text>`;
	}

	// 柱子 + 标签
	data.forEach((d, i) => {
		const x = padLeft + 10 + i * (barW + 10);
		const barH = (h - padTop - padBottom) * (d.value / maxVal);
		const y = h - padBottom - barH;
		html += `<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="${d.color}" rx="3" opacity="0.85">
			<animate attributeName="height" from="0" to="${barH}" dur="0.6s" fill="freeze"/>
			<animate attributeName="y" from="${h - padBottom}" to="${y}" dur="0.6s" fill="freeze"/>
		</rect>`;
		html += `<text x="${x + barW / 2}" y="${y - 4}" text-anchor="middle" fill="#374151" font-size="11" font-weight="600">${(d.value * 100).toFixed(1)}%</text>`;
		html += `<text x="${x + barW / 2}" y="${h - padBottom + 16}" text-anchor="middle" fill="#6b7280" font-size="12">${d.label}</text>`;
	});

	svg.innerHTML = html;
}

/* ============ 回答质量 ============ */
function loadAnswerScores() {
	loadDatasetOptions('#answerDatasetSel');
	loadMultiDimScores();
}

async function loadMultiDimScores() {
	try {
		const data = await api.getJson('/api/v1/analytics/multi-dim-scores/?start_date=' + getDaysAgoDate(7));
		const summary = data.dimension_summary || {};
		const rows = data.rows || [];

		// KPI（带语义着色）
		// 6 个维度统一通过 helper 取值，避免重复的三元表达式
		const dimAvg = (dim) => summary[dim] ? summary[dim].avg_score : null;
		setKpiValue('dimFaithfulness', dimAvg('faithfulness'), fmtPct);
		setKpiValue('dimRelevance', dimAvg('relevance'), fmtPct);
		setKpiValue('dimCompleteness', dimAvg('completeness'), fmtPct);
		setKpiValue('dimCorrectness', dimAvg('correctness'), fmtPct);
		setKpiValue('dimHarmlessness', dimAvg('harmlessness'), fmtPct);
		setKpiValue('dimContextRecall', dimAvg('context_recall'), fmtPct);

		const tbody = $('#multiDimBody');
		if (!rows.length) {
			tbody.innerHTML = `
				<tr>
					<td colspan="8">
						<div class="empty-state">
							<div class="empty-state-icon">💬</div>
							<div>暂无评估数据，可执行离线评估或手动触发</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = rows.slice(0, 50).map(r => `
			<tr>
				<td>${r.id}</td>
				<td>${r.qa_record_id}</td>
				<td><span class="tag">${dimLabel(r.dimension)}</span></td>
				<td>${scorePill(r.score, fmtPct)}</td>
				<td title="${escapeHtml(r.reason || '')}">${escapeHtml((r.reason || '').substring(0, 30))}</td>
				<td>${escapeHtml(r.eval_model)}</td>
				<td>¥${(r.eval_cost || 0).toFixed(4)}</td>
				<td>${formatDate(r.created_at)}</td>
			</tr>
		`).join('');
	} catch (e) {
		toast('加载失败', 'error');
	}
}

function dimLabel(d) {
	return {
		faithfulness: '忠实度', relevance: '相关性', completeness: '完整性',
		correctness: '正确性', harmlessness: '无害性', context_recall: '上下文召回率'
	}[d] || d;
}

async function runAnswerEval() {
	const dsId = $('#answerDatasetSel').value;
	if (!dsId) { toast('请选择测试集', 'error'); return; }
	toast('正在执行回答质量评估，可能需要几分钟...', 'info');
	try {
		const result = await api.postJson('/api/v1/analytics/eval/answer/', { dataset_id: parseInt(dsId), max_questions: 20 });
		toast(`评估完成: ${result.evaluated_count} 条`, 'success');
		loadMultiDimScores();
	} catch (e) {
		toast('评估失败: ' + e.message, 'error');
	}
}

async function runManualEval() {
	const qaId = $('#manualQaId').value.trim();
	if (!qaId) { toast('请输入 QA 记录 ID', 'error'); return; }
	try {
		const result = await api.postJson('/api/v1/analytics/multi-dim-eval/', { qa_record_id: parseInt(qaId) });
		toast(`评估完成: ${result.results.length} 个维度`, 'success');
		loadMultiDimScores();
	} catch (e) {
		toast('评估失败: ' + e.message, 'error');
	}
}

/* ============ 文档质量 ============ */
async function loadDocQuality() {
	try {
		// 并行请求两个接口，减少等待时间
		const rootTypeSel = $('#docRootType');
		const rootType = rootTypeSel ? rootTypeSel.value : '';
		const params = rootType ? `?root_type=${encodeURIComponent(rootType)}` : '';
		const [data, summary] = await Promise.all([
			api.getJson(`/api/v1/analytics/doc-quality/reports/${params}`),
			api.getJson(`/api/v1/analytics/doc-quality/${params}`),
		]);
		const total = summary.total_docs || 0;
		const avgScore = summary.avg_score || 0;
		const dist = summary.score_distribution || {};

		$('#docSummary').textContent = `共 ${total} 个文档，平均质量分 ${avgScore}`;
		$('#docTotal').textContent = total;
		$('#docAvgScore').textContent = avgScore;
		$('#docExcellent').textContent = dist.excellent || 0;
		$('#docGood').textContent = dist.good || 0;
		$('#docFair').textContent = dist.fair || 0;
		$('#docPoor').textContent = dist.poor || 0;

		drawDocDistChart(dist);

		// 常见问题
		const commonIssues = summary.common_issues || [];
		// severity 白名单，防止注入非法 CSS 类
		const sevClass = (s) => ({ high: 'sev-high', mid: 'sev-mid', low: 'sev-low' }[s] || 'sev-low');
		$('#commonIssues').innerHTML = commonIssues.length
			? commonIssues.map(i => `
				<div class="issue-item">
					<span class="issue-icon ${sevClass(i.severity)}">${(i.type || '?')[0]}</span>
					<div class="issue-content">${escapeHtml(i.type || i.issue_type || '未知问题')}</div>
					<span class="issue-count">${i.count || 0} 次</span>
				</div>`).join('')
			: '<div class="empty-state"><div class="empty-state-icon">✨</div><div>暂无常见问题</div></div>';

		// 文档质量表
		const rows = data.rows || [];
		const tbody = $('#docQualityBody');
		if (!rows.length) {
			tbody.innerHTML = `
				<tr>
					<td colspan="10">
						<div class="empty-state">
							<div class="empty-state-icon">📄</div>
							<div>暂无文档质量报告，点击"批量评估"</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = rows.map(r => `
			<tr>
				<td>${r.id}</td>
				<td title="${escapeHtml(r.document_name || '')}">${escapeHtml((r.document_name || '').substring(0, 30))}</td>
				<td><span class="score-pill ${qualityScoreClass(r.quality_score)}">${r.quality_score}</span></td>
				<td>${escapeHtml(r.parse_status)}</td>
				<td>${fmtPct(r.text_extraction_rate)}</td>
				<td>${r.chunk_count}</td>
				<td>${r.avg_chunk_chars}</td>
				<td>${fmtPct(r.embedding_success_rate)}</td>
				<td>${(r.quality_issues || []).map(i => `<span class="tag">${escapeHtml(i.type)}</span>`).join('') || '-'}</td>
				<td>${r.evaluated_at ? formatDate(r.evaluated_at) : '-'}</td>
			</tr>
		`).join('');
	} catch (e) {
		toast('加载失败', 'error');
	}
}

function drawDocDistChart(dist) {
	const svg = $('#docDistChart');
	if (!svg) return;
	// Number() 强制数值化，防止 dist 字段被污染为字符串导致 SVG 注入
	const num = (v) => Number(v) || 0;
	const data = [
		{ label: '优秀', value: num(dist.excellent), color: '#10b981' },
		{ label: '良好', value: num(dist.good), color: '#3b82f6' },
		{ label: '及格', value: num(dist.fair), color: '#f59e0b' },
		{ label: '待改进', value: num(dist.poor), color: '#ef4444' },
	];
	const w = 400, h = 160, padBottom = 30, padTop = 10;
	const total = data.reduce((s, d) => s + d.value, 0) || 1;
	const barW = (w - 40) / data.length - 10;

	let html = '';
	data.forEach((d, i) => {
		const x = 20 + i * (barW + 10);
		const barH = (h - padTop - padBottom) * (d.value / total);
		const y = h - padBottom - barH;
		html += `<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="${d.color}" rx="3" opacity="0.85">
			<animate attributeName="height" from="0" to="${barH}" dur="0.5s" fill="freeze"/>
			<animate attributeName="y" from="${h - padBottom}" to="${y}" dur="0.5s" fill="freeze"/>
		</rect>`;
		const pct = total > 0 ? (d.value / total * 100).toFixed(0) : 0;
		html += `<text x="${x + barW / 2}" y="${y - 4}" text-anchor="middle" fill="#374151" font-size="11" font-weight="600">${d.value} (${pct}%)</text>`;
		html += `<text x="${x + barW / 2}" y="${h - padBottom + 16}" text-anchor="middle" fill="#6b7280" font-size="12">${d.label}</text>`;
	});
	svg.innerHTML = html;
}

async function runBatchDocEval() {
	toast('正在评估最近文档质量...', 'info');
	try {
		const result = await api.postJson('/api/v1/analytics/doc-quality/evaluate/', { days: 7 });
		const s = result.summary || {};
		toast(`评估完成: ${s.evaluated} 个文档，平均分 ${s.avg_quality_score}`, 'success');
		loadDocQuality();
	} catch (e) {
		toast('评估失败: ' + e.message, 'error');
	}
}

/* ============ 覆盖率 ============ */
async function loadCoverage() {
	const days = $('#coverageDays').value;
	try {
		const data = await api.getJson(`/api/v1/analytics/coverage/?days=${days}`);
		const cov = data.coverage || {};
		const gaps = data.gaps || [];
		const dup = data.duplicates || {};
		const domain = data.domain || {};

		setKpiValue('covRate', cov.hot_query_coverage_rate, fmtPct);
		$('#covTotal').textContent = cov.total_hot_queries || 0;
		$('#covCovered').textContent = cov.covered_queries || 0;
		$('#covUncovered').textContent = cov.uncovered_queries || 0;
		$('#gapCount').textContent = data.gap_count || 0;
		setKpiValue('dupRate', dup.duplicate_rate || 0, fmtPct);

		// 知识空白
		$('#gapList').innerHTML = gaps.length
			? gaps.map(g => `
				<div class="gap-card">
					<div class="gap-question">${escapeHtml(g.query)}</div>
					<div class="gap-meta">
						<span>📊 出现 ${Number(g.count) || 0} 次</span>
						<span>💡 ${escapeHtml(g.suggestion || '')}</span>
					</div>
				</div>`).join('')
			: '<div class="empty-state"><div class="empty-state-icon">✅</div><div>暂无知识空白</div></div>';

		// 部门/团队覆盖
		const domList = domain.domain_coverage || [];
		if (domList.length) {
			let html = '';
			// 后端返回的部分字段为中文 key（如「占比」），统一数值化兜底，避免 undefined/NaN 显示
			const num = (v) => Number(v) || 0;
			domList.forEach(d => {
				const share = num(d['占比']);
				const pct = (share * 100).toFixed(1);
				const hitRate = num(d.query_hit_rate);
				const hitColor = hitRate >= 0.8 ? 'kpi-green' : hitRate >= 0.6 ? 'kpi-orange' : 'kpi-red';
				const docCount = num(d.doc_count);
				html += `
				<div class="coverage-dept-group">
					<div class="coverage-row coverage-row-dept">
						<div class="coverage-name"><strong>🏢 ${escapeHtml(d.name)}</strong></div>
						<div class="coverage-bar">
							<div class="coverage-bar-fill" style="width:${pct}%" data-value="${pct}%"></div>
						</div>
						<div class="coverage-rate">${docCount} 文档 · <span class="${hitColor}">命中率 ${(hitRate*100).toFixed(1)}%</span></div>
					</div>`;
				if (d.teams && d.teams.length) {
					d.teams.forEach(([teamName, teamData]) => {
						const teamDocCount = num(teamData && teamData.doc_count);
						const teamChunkCount = num(teamData && teamData.chunk_count);
						const teamPct = docCount > 0 ? (teamDocCount / docCount * 100).toFixed(1) : 0;
						html += `
					<div class="coverage-row coverage-row-team">
						<div class="coverage-name">└ ${escapeHtml(teamName)}</div>
						<div class="coverage-bar">
							<div class="coverage-bar-fill" style="width:${teamPct}%" data-value="${teamPct}%"></div>
						</div>
						<div class="coverage-rate">${teamDocCount} 文档 · ${teamChunkCount} 切片</div>
					</div>`;
					});
				}
				html += '</div>';
			});
			$('#domainCoverage').innerHTML = html;
		} else {
			$('#domainCoverage').innerHTML = '<div class="empty-state"><div class="empty-state-icon">📊</div><div>暂无数据，请先上传文档到对应部门/团队</div></div>';
		}

		// 加载历史报告列表
		loadCoverageReports();
	} catch (e) {
		toast('加载失败', 'error');
	}
}

async function generateCoverage() {
	const days = $('#coverageDays').value;
	toast('正在生成覆盖率报告...', 'info');
	try {
		const result = await api.postJson('/api/v1/analytics/coverage/generate/', { days: parseInt(days) });
		toast(`报告已生成 (ID: ${result.report_id})`, 'success');
		// loadCoverage 内部已调用 loadCoverageReports()，无需重复请求
		loadCoverage();
	} catch (e) {
		toast('生成失败: ' + e.message, 'error');
	}
}

/** 加载历史覆盖率报告列表 */
async function loadCoverageReports() {
	const tbody = $('#coverageReportBody');
	if (!tbody) return;
	try {
		const data = await api.getJson('/api/v1/analytics/coverage/reports/');
		const rows = data.rows || [];
		if (!rows.length) {
			tbody.innerHTML = `
				<tr>
					<td colspan="8">
						<div class="empty-state">
							<div class="empty-state-icon">📊</div>
							<div>暂无历史报告，点击"生成报告"创建</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = rows.map(r => `
			<tr>
				<td>${r.id}</td>
				<td>${escapeHtml(r.report_date)}</td>
				<td>${scorePill(r.hot_query_coverage_rate, fmtPct)}</td>
				<td>${r.total_hot_queries || 0}</td>
				<td>${r.gap_count || 0}</td>
				<td>${fmtPct(r.duplicate_chunk_rate)}</td>
				<td>${formatDate(r.created_at)}</td>
				<td>
					<button class="btn btn-sm" onclick="downloadCoverageReport(${r.id})">📥 下载</button>
					<button class="btn btn-sm btn-danger" onclick="deleteCoverageReport(${r.id})">删除</button>
				</td>
			</tr>
		`).join('');
	} catch (e) {
		toast('加载报告列表失败: ' + e.message, 'error');
	}
}

/** 下载覆盖率报告为 Excel
 * 拿到 Response 后转 blob 触发浏览器下载
 */
async function downloadCoverageReport(id) {
	try {
		const resp = await api.get(`/api/v1/analytics/coverage/reports/${id}/export/`);
		if (!resp.ok) throw new Error(`下载失败: ${resp.status}`);
		// 从响应头提取文件名，兼容 RFC 5987 (filename*=UTF-8''xxx) 与传统 filename="xxx"
		// 优先匹配 filename* 编码格式（支持中文等非 ASCII 字符），回退到普通 filename
		const disp = resp.headers.get('Content-Disposition') || '';
		let filename = '';
		const starMatch = disp.match(/filename\*=([^;]+)/i);
		if (starMatch) {
			// 格式: UTF-8''xxx%20yyy
			const raw = starMatch[1].trim().replace(/^UTF-8''/i, '');
			try {
				filename = decodeURIComponent(raw);
			} catch (e) {
				filename = raw;
			}
		} else {
			const match = disp.match(/filename="?([^";]+)"?/i);
			if (match) filename = match[1];
		}
		if (!filename) filename = `coverage_report_${id}.xlsx`;
		const blob = await resp.blob();
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		document.body.appendChild(a);
		a.click();
		a.remove();
		URL.revokeObjectURL(url);
	} catch (e) {
		toast('下载失败: ' + e.message, 'error');
	}
}

/** 删除覆盖率报告 */
async function deleteCoverageReport(id) {
	if (!confirm('确定删除此报告？删除后不可恢复。')) return;
	try {
		await api.delete(`/api/v1/analytics/coverage/reports/${id}/`);
		toast('删除成功', 'success');
		loadCoverageReports();
	} catch (e) {
		toast('删除失败: ' + e.message, 'error');
	}
}

/* ============ 反馈闭环 ============ */
async function runFeedbackLoop() {
	toast('正在分析反馈闭环...', 'info');
	try {
		const data = await api.postJson('/api/v1/analytics/feedback-loop/', { days: 7 });
		const total = data.total_bad_feedbacks || 0;
		const linked = data.linked_count || 0;
		const rate = total > 0 ? (linked / total * 100).toFixed(1) : '0.0';

		$('#fbTotal').textContent = total;
		$('#fbLinked').textContent = linked;
		setKpiValue('fbLinkRate', parseFloat(rate) / 100, v => v.toFixed(1) + '%');

		const issues = data.issue_chunks || [];
		const tbody = $('#feedbackBody');
		if (!issues.length) {
			tbody.innerHTML = `
				<tr>
					<td colspan="7">
						<div class="empty-state">
							<div class="empty-state-icon">🔄</div>
							<div>暂无需要处理的反馈</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = issues.map(f => `
			<tr>
				<td>${f.feedback_id}</td>
				<td title="${escapeHtml(f.question || '')}">${escapeHtml((f.question || '').substring(0, 40))}</td>
				<td>${scorePill(0, () => String(f.rating))}</td>
				<td>${(f.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('')}</td>
				<td>${(f.chunk_ids || []).map(cid => `<span class="tag">#${cid}</span>`).join('') || '-'}</td>
				<td class="fb-suggestion"><strong>💡 建议:</strong> ${escapeHtml(f.suggestion || '')}</td>
				<td><button class="btn btn-sm btn-danger" onclick="markFeedbackResolved(${f.feedback_id})">标记处理</button></td>
			</tr>
		`).join('');
	} catch (e) {
		toast('分析失败: ' + e.message, 'error');
	}
}

async function markFeedbackResolved(id) {
	try {
		await api.put(`/api/v1/analytics/bad-feedbacks/${id}/`, { status: 'resolved' });
		toast('已标记为已处理', 'success');
		runFeedbackLoop();
	} catch (e) {
		toast('操作失败: ' + e.message, 'error');
	}
}

/* ============ 工具函数 ============ */
function fmtPct(v) {
	if (v === null || v === undefined || isNaN(v)) return '--';
	return (Number(v) * 100).toFixed(1) + '%';
}

function getDaysAgoDate(days) {
	const d = new Date();
	d.setDate(d.getDate() - days);
	// 用本地时间格式化，避免 toISOString() 转 UTC 导致日期偏差
	const y = d.getFullYear();
	const m = String(d.getMonth() + 1).padStart(2, '0');
	const day = String(d.getDate()).padStart(2, '0');
	return `${y}-${m}-${day}`;
}

/* ============ 初始化 ============ */
document.addEventListener('DOMContentLoaded', () => {
	switchEvalTab('golden');
});
