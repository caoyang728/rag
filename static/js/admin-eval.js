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
// 领域列表缓存(从 /api/v1/knowledge/nodes/root_types/ 动态获取,避免硬编码)
let rootTypesCache = null;

// 手动评估相关的模块级状态
// evalToken: 每次派发评估时递增,用于取消旧轮询(用户切换 QA ID 时)
let evalToken = 0;
// localStorage key:缓存最近评估过的 QA ID,避免重复消耗 LLM 配额
const EVAL_CACHE_KEY = 'rag_manual_eval_ids';
// 缓存有效期(ms):5 分钟内同一 QA ID 重复评估会 toast 提醒
const EVAL_CACHE_TTL = 5 * 60 * 1000;

/** 读取本地缓存的已评估 QA ID 列表(自动清理过期) */
function loadEvalCache() {
	try {
		const raw = localStorage.getItem(EVAL_CACHE_KEY);
		if (!raw) return {};
		const map = JSON.parse(raw);
		const now = Date.now();
		// 清理过期条目并写回
		const cleaned = {};
		for (const [id, ts] of Object.entries(map)) {
			if (now - ts < EVAL_CACHE_TTL) cleaned[id] = ts;
		}
		if (Object.keys(cleaned).length !== Object.keys(map).length) {
			localStorage.setItem(EVAL_CACHE_KEY, JSON.stringify(cleaned));
		}
		return cleaned;
	} catch {
		return {};
	}
}

/** 将 QA ID 写入本地缓存(评估成功后调用) */
function saveEvalCache(qaId) {
	try {
		const map = loadEvalCache();
		map[String(qaId)] = Date.now();
		localStorage.setItem(EVAL_CACHE_KEY, JSON.stringify(map));
	} catch { /* 忽略 localStorage 写入失败(如隐私模式) */ }
}

/** 检查 QA ID 是否在缓存中,返回 true 表示近期已评估过 */
function checkEvalCache(qaId) {
	const map = loadEvalCache();
	return String(qaId) in map;
}

/* ============ 通用 ============ */

// 组织架构筛选使用 common.js 的 OrgFilter 公共组件,3 个 Tab 各自初始化一对级联下拉。
// 页面初始化时并行发起(不阻塞 Tab 切换),数据就绪后自动填充下拉并绑定 change 事件。
function initOrgFilters() {
	OrgFilter.init('evalDept', 'evalTeam', () => loadDashboard());
	OrgFilter.init('docDept',  'docTeam',  () => loadDocQuality());
	OrgFilter.init('attrDept', 'attrTeam', () => loadAttribution());
}

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
		case 'attribution': loadAttribution(); break;
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
					<td colspan="9">
						<div class="empty-state">
							<div class="empty-state-icon">📋</div>
							<div>暂无测试集，点击右上角"创建测试集"或"沉淀低分"开始</div>
						</div>
					</td>
				</tr>`;
			return;
		}
		tbody.innerHTML = datasetsCache.map(d => `
			<tr>
				<td>${d.id}</td>
				<td>${escapeHtml(d.name)}</td>
				<td><span class="tag ${d.dataset_type === 'regression_low_score' ? 'tag-regression' : ''}">${escapeHtml(d.dataset_type_label || d.dataset_type || '自定义')}</span></td>
				<td><span class="tag">${escapeHtml(rootTypeLabel(d.root_type))}</span></td>
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

/** 领域显示名映射:'all' 显示为"全部领域",其余原样返回 */
function rootTypeLabel(rt) {
	return rt === 'all' ? '全部领域' : (rt || '-');
}

/** 加载领域列表(从后端动态获取,避免硬编码与实际节点树脱节)
 * root_type 是知识库根节点的领域标识,按文档域划分(如 company_doc/tech_doc),
 * 不是组织架构维度(部门/团队已由节点树 层级表达)。
 * 首次调用拉取并缓存,后续直接用缓存填充下拉。
 */
async function loadRootTypes() {
	if (rootTypesCache) return rootTypesCache;
	try {
		const data = await api.getJson('/api/v1/knowledge/nodes/root_types/');
		rootTypesCache = data.root_types || [];
	} catch (e) {
		// 接口不可用时返回兜底值但不缓存,下次打开弹窗会重试
		return [{ code: 'company_doc', name: 'company_doc' }];
	}
	return rootTypesCache;
}

async function showCreateDatasetDialog() {
	$('#createDialog').style.display = 'flex';
	$('#dsName').value = '';
	$('#dsDesc').value = '';
	$('#dsVersion').value = 'v1';
	// 动态填充领域下拉:默认"全部领域",后接实际根节点类型
	const sel = $('#dsRootType');
	// 先放占位 option,防止 API 未返回时用户点击创建导致 root_type 为空
	sel.innerHTML = '<option value="all" selected>全部领域</option><option disabled>加载中...</option>';
	const types = await loadRootTypes();
	sel.innerHTML = '<option value="all">全部领域</option>' +
		types.map(t => `<option value="${escapeHtml(t.code)}">${escapeHtml(t.name)}</option>`).join('');
	sel.value = 'all';
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

function deleteDataset(id) {
	showConfirmDialog({
		title: '删除测试集',
		bannerText: '关联的问题和标注也会被删除,此操作不可恢复',
		bannerType: 'danger',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{ text: '确认删除', type: 'danger', onClick: async (ctx) => {
				ctx.close();
				try {
					await api.delete(`/api/v1/analytics/golden-datasets/${id}/`);
					toast('删除成功', 'success');
					loadDatasets();
				} catch (e) {
					toast('删除失败: ' + e.message, 'error');
				}
			} },
		],
	});
}

async function viewDataset(id) {
	try {
		const data = await api.getJson(`/api/v1/analytics/golden-datasets/${id}/`);
		const rows = data.questions || [];
		// 低分回归测试集展示 pass_count/last_eval_at,自定义测试集展示难度
		const isRegression = data.dataset_type === 'regression_low_score';
		// 建议移除阈值从后端获取(默认3),避免前端硬编码与配置不一致
		const suggestPasses = data.suggest_remove_passes || 3;
		if (!rows.length) {
			toast('此测试集暂无问题，可点击"批量导入"或"沉淀低分"添加', 'info');
			return;
		}
		const lines = [
			`测试集: ${data.name}（${data.dataset_type_label || '自定义'}）`,
			`共 ${rows.length} 个问题:`, '',
		];
		rows.slice(0, 5).forEach((q, i) => {
			const question = (q.question || '').substring(0, 50);
			if (isRegression) {
				// 低分回归:展示连续通过次数 + 最近评估时间 + 建议移除标记
				const passInfo = `通过 ${q.pass_count || 0} 次`;
				const evalTime = q.last_eval_at ? formatDate(q.last_eval_at) : '未评估';
				const suggest = (q.pass_count || 0) >= suggestPasses ? ' ⭐建议移除' : '';
				lines.push(`${i + 1}. ${question}... [${passInfo} | ${evalTime}]${suggest}`);
			} else {
				lines.push(`${i + 1}. ${question}... [难度:${q.difficulty}]`);
			}
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

/* ============ 低分回归测试集 ============ */

// 从生产低分对话沉淀到回归测试集(同步,后端直接返回结果)
function siphonRegression() {
	showConfirmDialog({
		title: '沉淀低分对话',
		bannerText: '从生产低分对话中取 top 50 沉淀到回归测试集',
		bodyHtml: '<p class="text-sm text-sub">按 12 维均分升序取最低分,按领域分流</p>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{ text: '开始沉淀', type: 'primary', onClick: async (ctx) => {
				ctx.close();
				toast('正在沉淀低分对话...', 'info');
				try {
					const result = await api.postJson('/api/v1/analytics/regression/siphon/', {});
					const n = result.siphoned || 0;
					if (n === 0) {
						toast(result.reason === 'no_candidates' ? '暂无新的低分对话可沉淀(可能已全部沉淀过)' : '沉淀完成,无新增', 'info');
					} else {
						const byRoot = result.by_root || {};
						const detail = Object.entries(byRoot).map(([k, v]) => `${k}:${v}`).join(' ');
						toast(`沉淀完成: 新增 ${n} 条(${detail})`, 'success');
					}
					loadDatasets();
				} catch (e) {
					toast('沉淀失败: ' + e.message, 'error');
				}
			} },
		],
	});
}

// 对低分回归测试集执行全链路评估(异步派发,前端提示后刷新查看 pass_count)
function runRegressionEval() {
	showConfirmDialog({
		title: '评估回归',
		bannerText: '对所有低分回归测试集执行全链路评估',
		bannerType: 'danger',
		bodyHtml: '<p class="text-sm text-sub">检索→生成→12 维评估,每问题约 90~180s,耗时较长</p>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{ text: '开始评估', type: 'primary', onClick: async (ctx) => {
				ctx.close();
				toast('正在派发回归评估任务...', 'info');
				try {
					const result = await api.postJson('/api/v1/analytics/regression/eval/', {});
					if (result.queued) {
						toast(result.message || '评估已派发,请稍后刷新查看 pass_count 变化', 'info');
					} else {
						toast(`评估完成: 通过 ${result.passed || 0} / 失败 ${result.failed || 0}`, 'success');
						loadDatasets();
					}
				} catch (e) {
					toast('评估失败: ' + e.message, 'error');
				}
			} },
		],
	});
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

/** 0-1 分值映射为 KPI 数值的语义色类(CSS 定义于 .kpi-value.val-good/mid/poor) */
function kpiValueClass(score) {
	if (score >= 0.8) return 'val-good';
	if (score >= 0.6) return 'val-mid';
	return 'val-poor';
}

/** 设置 KPI 值并根据分数着色 */
function setKpiValue(elId, value, formatter) {
	const el = $('#' + elId);
	if (!el) return;
	const formatted = formatter ? formatter(value) : value;
	el.textContent = formatted;
	el.classList.remove('val-good', 'val-mid', 'val-poor');
	if (typeof value === 'number') {
		el.classList.add(kpiValueClass(value));
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

/* ============ 回答质量(评估看板) ============ */
// 维度中文名 + 4 大类分组(与后端 _DIMENSION_GROUPS 保持一致)
const DIM_LABEL = {
	faithfulness: '忠实度', hallucination: '幻觉', answer_relevancy: '回答相关性',
	context_relevancy: '检索相关性', toxicity: '毒性', bias: '偏见',
	completeness: '完整性', conciseness: '简洁性', clarity: '清晰度',
	professionalism: '专业性', helpfulness: '有用性', actionability: '可操作性',
};
const DIM_GROUPS = {
	retrieval: { label: '检索质量', dims: ['context_relevancy'] },
	quality: { label: '答案质量', dims: ['faithfulness', 'hallucination', 'answer_relevancy', 'completeness', 'conciseness', 'clarity'] },
	safety: { label: '安全性', dims: ['toxicity', 'bias'] },
	business: { label: '业务体验', dims: ['professionalism', 'helpfulness', 'actionability'] },
};
// 所有 12 维按分组顺序展开(雷达图用)
const ALL_DIMS_ORDERED = Object.values(DIM_GROUPS).flatMap(g => g.dims);

// 展示维度白名单：由 SystemConfig.EVAL_DISPLAY_DIMENSIONS 控制
// null = 未加载（首次加载前），空数组 = 用户主动清空（不展示任何维度），非空 = 仅展示勾选的维度
let _displayDimensions = null;

/** 判断维度是否允许展示（未加载白名单时默认全部允许，保持向后兼容） */
function isDimVisible(dim) {
	// null/undefined = 配置未加载，默认全部可见（首次渲染或老部署未初始化配置时兜底）
	if (_displayDimensions === null) return true;
	return _displayDimensions.includes(dim);
}

/** 获取按白名单过滤后的分组（用于 sparkline / 雷达图渲染）
 * - 仅保留 dims 中所有 isDimVisible 的维度
 * - 整组维度全部被过滤掉时，该组不返回（前端不渲染空分组） */
function getVisibleDimGroups() {
	const result = {};
	for (const [groupKey, g] of Object.entries(DIM_GROUPS)) {
		const visibleDims = g.dims.filter(d => isDimVisible(d));
		if (visibleDims.length > 0) {
			result[groupKey] = { label: g.label, dims: visibleDims };
		}
	}
	return result;
}

/** 获取按白名单过滤后的维度顺序（雷达图用） */
function getVisibleDimsOrdered() {
	return ALL_DIMS_ORDERED.filter(d => isDimVisible(d));
}

function loadAnswerScores() {
	loadDashboard();
}

async function loadDashboard() {
	const days = $('#evalDays') ? $('#evalDays').value : 7;
	const deptId = OrgFilter.getDeptId('evalDept');
	const teamId = OrgFilter.getTeamId('evalTeam');
	const params = new URLSearchParams();
	params.set('days', days);
	if (deptId) params.set('dept_id', deptId);
	if (teamId) params.set('team_id', teamId);
	const qs = '?' + params.toString();

	// 并行加载 overview + low-score 两个接口(trend 接口当前 UI 未使用,避免无效请求)
	try {
		const [overview, lowScore] = await Promise.all([
			api.getJson('/api/v1/analytics/eval-dashboard/overview/' + qs),
			api.getJson('/api/v1/analytics/eval-dashboard/low-score-qa/' + qs + '&limit=20'),
		]);
		// 缓存展示维度白名单：后端返回 null/undefined 时保持 null（按全部展示兜底），
		// 返回空数组时表示用户主动清空（不展示任何维度）
		_displayDimensions = overview.display_dimensions ?? null;
		renderOverview(overview);
		// 传入 total_evaluated 以区分"无评估数据"和"有评估但无低分"两种空状态
		renderLowScoreTable(lowScore.rows || [], overview.total_evaluated);
		const scopeText = OrgFilter.describeScope(overview.dept_id ?? deptId, overview.team_id ?? teamId);
		$('#evalSummary').textContent = `窗口 ${overview.days} 天 · 范围 ${scopeText} · 阈值 ${overview.threshold}`;
	} catch (e) {
		toast('看板加载失败: ' + (e.message || e), 'error');
	}
}

function renderOverview(data) {
	// KPI 卡片
	$('#kpiEvaluated').textContent = data.total_evaluated || 0;
	$('#kpiCoverage').textContent = `覆盖率 ${fmtPct(data.coverage_rate)} · 总对话 ${data.total_qa || 0}`;
	$('#kpiLowScore').textContent = data.low_score_count || 0;
	$('#kpiLowRate').textContent = `占比 ${fmtPct(data.low_score_rate)}`;
	$('#kpiSafetyAlert').textContent = data.safety_alert_count || 0;

	// 用户主动清空展示维度时（_displayDimensions 为空数组），提示并清空维度画像区域
	// 与"无评估数据"区分，避免用户误以为系统故障
	if (Array.isArray(_displayDimensions) && _displayDimensions.length === 0) {
		$('#kpiOverallAvg').textContent = '--';
		$('#evalDimSparklines').innerHTML = `<div class="empty-state"><div class="empty-state-icon">🚫</div>未选择任何展示维度，请在「系统配置 → 评估 → 评估维度」中勾选</div>`;
		$('#evalRadar').innerHTML = `<text x="170" y="170" text-anchor="middle" fill="#9ca3af" font-size="13">未选择展示维度</text>`;
		return;
	}

	// dimension_groups 为空对象时(后端无评估数据),整体均分显示 -- ,不渲染雷达图/sparkline
	const groups = data.dimension_groups || {};
	const hasData = Object.values(groups).some(g => g.dimensions && g.dimensions.length > 0);
	if (!hasData) {
		$('#kpiOverallAvg').textContent = '--';
		$('#evalDimSparklines').innerHTML = `<div class="empty-state"><div class="empty-state-icon">📊</div>暂无评估数据</div>`;
		$('#evalRadar').innerHTML = `<text x="170" y="170" text-anchor="middle" fill="#9ca3af" font-size="13">暂无评估数据</text>`;
		return;
	}

	// 整体均分 = 4 大类均分的平均
	const groupAvgs = Object.values(groups).map(g => g.avg_score).filter(v => v > 0);
	const overallAvg = groupAvgs.length ? groupAvgs.reduce((s, v) => s + v, 0) / groupAvgs.length : 0;
	$('#kpiOverallAvg').textContent = fmtPct(overallAvg);

	// 每行维度 + sparkline + 环比
	renderDimSparklines(groups);

	// 雷达图
	renderRadarChart(groups);
}

function renderDimSparklines(groups) {
	// 把 12 维按分组展开成一行一维度的表,每行含:维度名 | 均分 | 环比 | sparkline(7天)
	const el = $('#evalDimSparklines');
	if (!el) return;

	// 收集每个维度的趋势数据,用于后续动态生成 sparkline
	const sparkData = {};

	// 按白名单过滤后的分组展开维度，未在白名单中的维度不再渲染
	// getVisibleDimGroups 已自动剔除整组维度全部被过滤掉的分组
	const visibleGroups = getVisibleDimGroups();
	const rows = [];
	for (const [groupKey, g] of Object.entries(visibleGroups)) {
		const gd = groups[groupKey] || { dimensions: [] };
		for (const dimName of g.dims) {
			const info = gd.dimensions.find(x => x.name === dimName);
			if (!info) continue;
			const avg = info.avg || 0;
			const trend = info.trend_7d || [];
			const mom = info.mom_change;

			// 环比箭头
			let momHtml = '';
			if (mom !== null && mom !== undefined) {
				const pct = (mom * 100).toFixed(1) + '%';
				if (mom > 0) momHtml = `<span class="mom-up">↑ ${pct}</span>`;
				else if (mom < 0) momHtml = `<span class="mom-down">↓ ${pct}</span>`;
				else momHtml = `<span class="mom-flat">— 0.0%</span>`;
			} else {
				momHtml = `<span class="text-sub text-sm">环比 —</span>`;
			}

			// 保存趋势数据,用占位符替代 sparkline,等 DOM 渲染后再测量宽度生成
			const sparkId = `spark-${dimName}`;
			sparkData[sparkId] = trend;

			rows.push({
				groupLabel: g.label,
				groupKey: groupKey,
				dimName,
				label: DIM_LABEL[dimName] || dimName,
				avg,
				count: info.count || 0,
				momHtml,
				sparkId,
			});
		}
	}

	// 如果没有数据,显示空状态
	if (!rows.length) {
		el.innerHTML = `<div class="empty-state"><div class="empty-state-icon">📊</div>暂无评估数据</div>`;
		return;
	}

	// 按分组渲染:每组一个小标题 + 组内维度行
	// 先按 groupKey 分组,保持 visibleGroups 顺序（已按 DIM_GROUPS 原始顺序过滤）
	const grouped = {};
	for (const r of rows) {
		if (!grouped[r.groupKey]) grouped[r.groupKey] = { label: r.groupLabel, rows: [] };
		grouped[r.groupKey].rows.push(r);
	}

	const html = Object.keys(visibleGroups).map(gk => {
		const g = grouped[gk];
		if (!g) return '';
		return `<div class="dim-group mb-12">
			<div class="dim-group-title">${g.label}</div>
			<div class="dim-table">
				<div class="dim-row dim-row-head">
					<span class="dim-col-name">维度</span>
					<span class="dim-col-avg">均分</span>
					<span class="dim-col-mom">环比</span>
					<span class="dim-col-spark">7日趋势</span>
					<span class="dim-col-cnt">样本</span>
				</div>
				${g.rows.map(r => `
					<div class="dim-row">
						<span class="dim-col-name">${r.label}</span>
						<span class="dim-col-avg">${scorePill(r.avg, fmtPct)}</span>
						<span class="dim-col-mom">${r.momHtml}</span>
						<span class="dim-col-spark" id="${r.sparkId}">
							<span class="sparkline-placeholder" style="display:block;height:32px"></span>
						</span>
						<span class="dim-col-cnt text-sub text-sm">${r.count}</span>
					</div>
				`).join('')}
			</div>
		</div>`;
	}).join('');

	el.innerHTML = html;

	// DOM 渲染完成后,测量每个 sparkline 容器宽度,用实际宽度生成 sparkline SVG
	requestAnimationFrame(() => {
		for (const [sparkId, trend] of Object.entries(sparkData)) {
			const container = document.getElementById(sparkId);
			if (!container) continue;
			// 获取容器实际宽度,用于生成等比缩放的 SVG
			const rect = container.getBoundingClientRect();
			const width = Math.round(rect.width);
			container.innerHTML = buildSparkline(trend, width);
		}
	});
}

/** 生成带均值虚线的 sparkline SVG
 *  - width: 实际容器宽度(像素), 用于计算 viewBox 实现等比缩放
 *  - 均值虚线: 7 日均值位置的水平虚线
 *  - 折线统一蓝色
 */
function buildSparkline(values, width) {
	if (!values.length) {
		const w = width || 140;
		return `<svg width="100%" height="32" viewBox="0 0 ${w} 32" class="sparkline" preserveAspectRatio="xMidYMid meet"><line x1="0" y1="16" x2="${w}" y2="16" stroke="#e5e7eb" stroke-width="1"/></svg>`;
	}
	const H = 32, pad = 2;
	const containerW = width || 140;
	// viewBox 宽度 = 容器宽度, 保证 preserveAspectRatio="xMidYMid meet" 时等比缩放
	const W = Math.max(containerW, 80);
	const valid = values.filter(v => v > 0);
	const max = valid.length ? Math.max(...valid) : 1;
	const min = valid.length ? Math.min(...valid) : 0;
	const range = max - min || 1;
	const stepX = values.length > 1 ? (W - 2 * pad) / (values.length - 1) : 0;

	// 计算 7 日均值(仅基于有效值)
	const avg = valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : 0;
	const avgY = H - pad - ((avg - min) / range) * (H - 2 * pad);

	// 计算每个点的坐标
	let pts = [];
	values.forEach((v, i) => {
		const x = pad + i * stepX;
		const y = H - pad - ((v - min) / range) * (H - 2 * pad);
		pts.push({ x, y, v });
	});

	// 单点情况: 只画一个圆点 + 均值虚线
	if (pts.length < 2) {
		return `<svg width="100%" height="32" viewBox="0 0 ${W} ${H}" class="sparkline" preserveAspectRatio="xMidYMid meet">
			<line x1="${pad}" y1="${avgY.toFixed(1)}" x2="${W - pad}" y2="${avgY.toFixed(1)}" stroke="#94a3b8" stroke-width="0.8" stroke-dasharray="3,2" opacity="0.6"/>
			<circle cx="${pts[0].x.toFixed(1)}" cy="${pts[0].y.toFixed(1)}" r="1.8" fill="#3b82f6"/>
		</svg>`;
	}

	// 折线点坐标
	const polyline = pts.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ');
	const areaPath = `M${pts[0].x.toFixed(1)},${H - pad} L${polyline.split(' ').join(' L')} L${pts[pts.length - 1].x.toFixed(1)},${H - pad} Z`;

	// 填充区域(淡蓝)
	const areaFill = `<path d="${areaPath}" fill="rgba(59,130,246,0.08)" stroke="none"/>`;

	// 均值虚线
	const avgLine = `<line x1="${pad}" y1="${avgY.toFixed(1)}" x2="${W - pad}" y2="${avgY.toFixed(1)}" stroke="#94a3b8" stroke-width="0.8" stroke-dasharray="3,2" opacity="0.6"/>`;

	// 统一蓝色折线
	const line = `<polyline points="${polyline}" fill="none" stroke="#3b82f6" stroke-width="1.2" stroke-linejoin="miter" stroke-linecap="butt"/>`;

	// 终点圆点
	const lastPt = pts[pts.length - 1];
	const lastCircle = `<circle cx="${lastPt.x.toFixed(1)}" cy="${lastPt.y.toFixed(1)}" r="1.8" fill="#3b82f6"/>`;

	return `<svg width="100%" height="32" viewBox="0 0 ${W} ${H}" class="sparkline" preserveAspectRatio="xMidYMid meet">
		${areaFill}
		${avgLine}
		${line}
		${lastCircle}
	</svg>`;
}

function renderRadarChart(groups) {
	const svg = $('#evalRadar');
	if (!svg) return;
	const cx = 170, cy = 170, R = 130;
	// 使用白名单过滤后的维度顺序：未勾选的维度不绘制到雷达图
	const dims = getVisibleDimsOrdered();
	const n = dims.length;
	// 维度数量少于 3 时雷达图无法成型（至少需要三角形），给出空态提示
	if (n < 3) {
		svg.innerHTML = `<text x="${cx}" y="${cy}" text-anchor="middle" fill="#9ca3af" font-size="13">展示维度不足 3 个，无法绘制雷达图</text>`;
		return;
	}
	const values = dims.map(d => {
		// 找该维度在哪个组
		for (const g of Object.values(groups)) {
			const found = g.dimensions.find(x => x.name === d);
			if (found) return found.avg;
		}
		return 0;
	});

	// 背景网格(4 圈)
	let bg = '';
	for (let r = 1; r <= 4; r++) {
		const rr = R * r / 4;
		const pts = dims.map((_, i) => {
			const angle = -Math.PI / 2 + i * 2 * Math.PI / n;
			return `${cx + rr * Math.cos(angle)},${cy + rr * Math.sin(angle)}`;
		}).join(' ');
		bg += `<polygon points="${pts}" fill="none" stroke="#e5e7eb" stroke-width="1"/>`;
	}
	// 轴线
	let axes = '';
	dims.forEach((d, i) => {
		const angle = -Math.PI / 2 + i * 2 * Math.PI / n;
		axes += `<line x1="${cx}" y1="${cy}" x2="${cx + R * Math.cos(angle)}" y2="${cy + R * Math.sin(angle)}" stroke="#e5e7eb" stroke-width="1"/>`;
		// 标签
		const lx = cx + (R + 18) * Math.cos(angle);
		const ly = cy + (R + 18) * Math.sin(angle);
		axes += `<text x="${lx}" y="${ly}" text-anchor="middle" dominant-baseline="middle" font-size="10" fill="#6b7280">${DIM_LABEL[d] || d}</text>`;
	});
	// 数据多边形
	const dataPts = values.map((v, i) => {
		const angle = -Math.PI / 2 + i * 2 * Math.PI / n;
		const rr = R * Math.max(0, Math.min(1, v));
		return `${cx + rr * Math.cos(angle)},${cy + rr * Math.sin(angle)}`;
	}).join(' ');
	const dataPoly = `<polygon points="${dataPts}" fill="rgba(59,130,246,0.2)" stroke="#3b82f6" stroke-width="2"/>`;
	// 数据点
	let dots = '';
	values.forEach((v, i) => {
		const angle = -Math.PI / 2 + i * 2 * Math.PI / n;
		const rr = R * Math.max(0, Math.min(1, v));
		dots += `<circle cx="${cx + rr * Math.cos(angle)}" cy="${cy + rr * Math.sin(angle)}" r="3" fill="#3b82f6"/>`;
	});
	svg.innerHTML = bg + axes + dataPoly + dots;
}

function renderLowScoreTable(rows, totalEvaluated) {
	const tbody = $('#lowScoreBody');
	if (!rows.length) {
		// 区分两种空状态: 无评估数据 vs 有评估但无低分对话
		const isEmpty = !totalEvaluated;
		tbody.innerHTML = `<tr><td colspan="9">
			<div class="empty-state">
				<div class="empty-state-icon">${isEmpty ? '📊' : '✅'}</div>
				<div>${isEmpty ? '暂无评估数据' : '无低分对话,质量良好'}</div>
			</div>
		</td></tr>`;
		return;
	}
	tbody.innerHTML = rows.map(r => `
		<tr>
			<td>${r.qa_record_id}</td>
			<td title="${escapeHtml(r.question)}">${escapeHtml(r.question.substring(0, 30))}</td>
			<td title="${escapeHtml(r.answer)}">${escapeHtml(r.answer.substring(0, 40))}</td>
			<td>${scorePill(r.avg_score, fmtPct)}</td>
			<td><span class="tag">${escapeHtml(DIM_LABEL[r.min_dimension] || r.min_dimension)}</span></td>
			<td>${scorePill(r.min_score, fmtPct)}</td>
			<td>${escapeHtml(r.root_type)}</td>
			<td>${formatDate(r.created_at)}</td>
			<td><button class="btn btn-sm" onclick="showQaDetail(${r.qa_record_id})">查看明细</button></td>
		</tr>
	`).join('');
}

async function showQaDetail(qaId) {
	$('#qaDetailTitle').textContent = `QA #${qaId}`;
	$('#qaDetailBody').innerHTML = '<div class="text-sub">加载中...</div>';
	document.getElementById('qaDetailDialog').style.display = 'flex';
	try {
		const data = await api.getJson('/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=' + qaId);
		const qa = data.qa;
		const scores = data.scores || [];

		// 对话区
		let html = `
			<div class="mb-16">
				<div class="flex justify-between items-baseline mb-8">
					<strong>对话内容</strong>
					<span class="text-sm text-sub">均分 ${scorePill(data.avg_score, fmtPct)} · 用户 ${escapeHtml(qa.user)} · 领域 ${escapeHtml(qa.root_type)}</span>
				</div>
				<div class="mb-8"><strong class="text-sub">问题:</strong> ${escapeHtml(qa.question)}</div>
				<div><strong class="text-sub">回答:</strong> ${escapeHtml(qa.answer)}</div>
			</div>
			<div class="mb-16 text-sm text-sub">
				耗时 ${qa.latency_total_ms}ms · Token ${qa.tokens_total} · 命中切片 ${qa.retrieval_hits.length} 个 · ${formatDate(qa.created_at)}
			</div>
		`;

		// 12 维明细(按 4 大类分组) - 受展示维度白名单过滤
		// 用户在「系统配置 → 评估 → 评估维度」中未勾选的维度不再展示
		const visibleGroups = getVisibleDimGroups();
		const visibleCount = Object.values(visibleGroups).reduce((s, g) => s + g.dims.length, 0);
		if (visibleCount === 0) {
			html += '<div class="text-sub">未选择任何展示维度</div>';
		} else {
			html += '<div><strong>评估明细</strong></div>';
			Object.entries(visibleGroups).forEach(([key, g]) => {
				const groupScores = scores.filter(s => g.dims.includes(s.dimension));
				if (!groupScores.length) return;
				html += `<div class="mb-16">
				<div class="flex justify-between items-baseline mb-8">
					<strong>${g.label}</strong>
				</div>`;
				groupScores.forEach(s => {
					html += `<div style="padding:8px 0;border-bottom:1px solid #f3f4f6">
					<div class="flex justify-between items-baseline">
						<span>${escapeHtml(DIM_LABEL[s.dimension] || s.dimension)}</span>
						<span>${scorePill(s.score, fmtPct)} <span class="text-sub text-sm">${s.eval_latency_ms}ms</span></span>
					</div>
					<div class="text-sm text-sub" style="margin-top:4px">${escapeHtml(s.reason || '(无理由)')}</div>
				</div>`;
				});
				html += '</div>';
			});
		}

		$('#qaDetailBody').innerHTML = html;
	} catch (e) {
		$('#qaDetailBody').innerHTML = `<div class="text-sub">加载失败: ${escapeHtml(e.message || String(e))}</div>`;
	}
}

async function runManualEval() {
	const qaId = $('#manualQaId').value.trim();
	if (!qaId) { toast('请输入 QA 记录 ID', 'error'); return; }

	// localStorage 缓存检查:5 分钟内同一 QA ID 已评估过则 toast 提醒
	// 不阻止用户,因为可能需要重新评估(如模型升级/内容变更)
	if (checkEvalCache(qaId)) {
		toast(`QA ID ${qaId} 近期已评估过,将覆盖之前的结果`, 'info');
	}

	// 递增 token,使旧轮询(如存在)在下一次迭代时自动取消
	const myToken = ++evalToken;

	// 禁用按钮防止重复点击;用户修改 QA ID 时通过 input 事件自动恢复
	const btn = $('#btnRunManualEval');
	if (btn) { btn.disabled = true; }
	toast('评估已派发,正在后台执行(约 2~3 分钟),请勿重复点击...', 'info');

	try {
		// POST 立即返回 eval_batch_id,实际评估在 Celery 异步执行
		const resp = await api.postJson('/api/v1/analytics/multi-dim-eval/', { qa_record_id: parseInt(qaId) });
		if (!resp || !resp.queued || !resp.eval_batch_id) {
			throw new Error(resp?.detail || '派发评估失败');
		}
		const evalBatchId = resp.eval_batch_id;
		// 轮询 qa-detail 接口,检查本次 batch_id 的评估结果是否落库
		// 12 维评估串行耗时 90~180s+,超时设 5 分钟兜底
		// 传入 myToken,若用户中途切换 QA ID,旧轮询会被自动取消
		await pollManualEvalResult(parseInt(qaId), evalBatchId, myToken);
		// 评估成功后写入 localStorage 缓存,避免短期重复评估
		saveEvalCache(qaId);
		toast('评估完成,已弹出明细', 'success');
		showQaDetail(parseInt(qaId));
		loadDashboard();
	} catch (e) {
		// 若 token 已被更新(用户切换了 QA ID),说明是被主动取消,不算错误
		if (myToken !== evalToken) return;
		toast('评估失败: ' + (e.message || e), 'error');
	} finally {
		// 只有当前 token 仍有效时才恢复按钮(否则是新评估已接管)
		if (myToken === evalToken && btn) { btn.disabled = false; }
	}
}

// 轮询单条 QA 评估结果,直到本次 batch_id 的维度数达标或超时
// 评估维度数由后端 EVAL_DISPLAY_DIMENSIONS 控制(默认 12),用 8 作为最低门槛
// 避免配置变更后前端硬编码 12 导致永远等不到
// token:用于取消旧轮询——当用户切换 QA ID 时,evalToken 递增,旧 token 失效
async function pollManualEvalResult(qaId, evalBatchId, token) {
	const POLL_INTERVAL_MS = 3000;   // 每 3 秒轮询一次
	const MAX_WAIT_MS = 5 * 60 * 1000; // 最长等待 5 分钟
	const MIN_DIMS_THRESHOLD = 8;     // 至少 8 维落库才算完成(兼容分组配置)
	const startedAt = Date.now();

	while (Date.now() - startedAt < MAX_WAIT_MS) {
		// 若 token 已被更新(用户切换了 QA ID),取消本次轮询
		if (token !== evalToken) {
			throw new Error('cancelled');
		}
		await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
		// 睡眠后再次检查 token,避免在取消后仍发请求
		if (token !== evalToken) {
			throw new Error('cancelled');
		}
		try {
			const data = await api.getJson(`/api/v1/analytics/eval-dashboard/qa-detail/?qa_record_id=${qaId}`);
			// 只统计本次 batch_id 的维度,避免被旧评估结果误判为完成
			const currentBatchScores = (data.scores || []).filter(s => s.eval_batch_id === evalBatchId);
			if (currentBatchScores.length >= MIN_DIMS_THRESHOLD) {
				return; // 评估完成
			}
		} catch (e) {
			// 轮询单次失败不中断,继续重试(可能是网络抖动)
			console.warn('[manualEval] 轮询失败,将继续重试:', e);
		}
	}
	throw new Error('评估超时(5 分钟内未完成),请稍后刷新查看结果');
}

/* ============ 文档质量 ============ */
async function loadDocQuality() {
	try {
		const deptId = OrgFilter.getDeptId('docDept');
		const teamId = OrgFilter.getTeamId('docTeam');
		const params = new URLSearchParams();
		if (deptId) params.set('dept_id', deptId);
		if (teamId) params.set('team_id', teamId);
		const qs = params.toString() ? '?' + params.toString() : '';
		// 并行请求两个接口，减少等待时间
		const [data, summary] = await Promise.all([
			api.getJson(`/api/v1/analytics/doc-quality/reports/${qs}`),
			api.getJson(`/api/v1/analytics/doc-quality/${qs}`),
		]);
		const total = summary.total_docs || 0;
		const avgScore = summary.avg_score || 0;
		const dist = summary.score_distribution || {};
		const scopeText = OrgFilter.describeScope(data.dept_id ?? deptId, data.team_id ?? teamId);

		$('#docSummary').textContent = `范围：${scopeText} · 共 ${total} 个文档，平均质量分 ${avgScore}`;
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
					<div class="issue-content">${escapeHtml(i.type || '未知问题')}</div>
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
function deleteCoverageReport(id) {
	showConfirmDialog({
		title: '删除报告',
		bannerText: '删除后不可恢复',
		bannerType: 'danger',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{ text: '确认删除', type: 'danger', onClick: async (ctx) => {
				ctx.close();
				try {
					await api.delete(`/api/v1/analytics/coverage/reports/${id}/`);
					toast('删除成功', 'success');
					loadCoverageReports();
				} catch (e) {
					toast('删除失败: ' + e.message, 'error');
				}
			} },
		],
	});
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

/* ============ 低分归因分析 ============ */

// 归因分类 → 中文标签(与后端 LowScoreAnalysis.CATEGORY_CHOICES 对齐)
const ATTR_CATEGORY_LABEL = {
	retrieval_recall: '检索召回不足',
	retrieval_rank: '检索排序失效',
	content_gap: '知识盲区',
	content_quality: '内容质量差',
	generation_hallucination: '生成幻觉',
	generation_offtopic: '生成跑题',
	generation_incomplete: '生成不完整',
	generation_format: '生成表达差',
	safety: '安全问题',
	question_side: '问题侧',
	unknown: '无法归因',
};
// 影响层级 → 中文标签(与后端 LowScoreAnalysis.LAYER_CHOICES 对齐)
const ATTR_LAYER_LABEL = {
	retrieval: '检索层',
	content: '内容层',
	generation: '生成层',
	safety: '安全层',
	system: '系统层',
	question: '问题侧',
	unknown: '未知',
};

// 加载低分归因列表 + 统计(切换 Tab / 筛选 / 刷新时调用)
// 并行请求列表与统计两个接口,减少串行等待
async function loadAttribution() {
	const days = $('#attrDays') ? $('#attrDays').value : '7';
	const category = $('#attrCategory') ? $('#attrCategory').value : '';
	const layer = $('#attrLayer') ? $('#attrLayer').value : '';
	const status = $('#attrStatus') ? $('#attrStatus').value : '';
	const deptId = OrgFilter.getDeptId('attrDept');
	const teamId = OrgFilter.getTeamId('attrTeam');

	// 列表查询参数
	const listParams = new URLSearchParams();
	listParams.set('days', days);
	listParams.set('limit', '100');
	if (category) listParams.set('category', category);
	if (layer) listParams.set('layer', layer);
	if (status) listParams.set('status', status);
	if (deptId) listParams.set('dept_id', deptId);
	if (teamId) listParams.set('team_id', teamId);

	// 统计查询参数(不需要 category/layer/status 过滤,stats 接口返回全分类聚合)
	const statsParams = new URLSearchParams();
	statsParams.set('days', days);
	if (deptId) statsParams.set('dept_id', deptId);
	if (teamId) statsParams.set('team_id', teamId);

	try {
		const [listData, statsData] = await Promise.all([
			api.getJson(`/api/v1/analytics/low-score-analysis/?${listParams.toString()}`),
			api.getJson(`/api/v1/analytics/low-score-analysis/stats/?${statsParams.toString()}`),
		]);
		renderAttrStats(statsData);
		renderAttrList(listData);
		// summary 行(如有)显示组织范围
		const scopeEl = $('#attrSummary');
		if (scopeEl) {
			const scopeText = OrgFilter.describeScope(
				statsData.dept_id ?? deptId,
				statsData.team_id ?? teamId,
			);
			scopeEl.textContent = `范围 ${scopeText} · 窗口 ${statsData.days || days} 天`;
		}
	} catch (e) {
		toast('加载归因数据失败: ' + (e.message || e), 'error');
		$('#attrTableBody').innerHTML = `<tr><td colspan="11" class="text-center text-sub">加载失败</td></tr>`;
	}
}

// 渲染归因统计 KPI + 分类分布
function renderAttrStats(data) {
	const total = data.total || 0;
	const byLayer = data.by_layer || [];
	const byMethod = data.by_method || { rule: 0, llm: 0, hybrid: 0 };

	// KPI 卡片:总数 + 各层级 + 各方法
	$('#attrKpiTotal').textContent = total;
	const layerCount = (layer) => byLayer.find(l => l.layer === layer)?.count || 0;
	$('#attrKpiRetrieval').textContent = layerCount('retrieval');
	$('#attrKpiContent').textContent = layerCount('content');
	$('#attrKpiGeneration').textContent = layerCount('generation');
	$('#attrKpiRule').textContent = byMethod.rule || 0;
	$('#attrKpiLlm').textContent = (byMethod.llm || 0) + (byMethod.hybrid || 0);

	// 归因分类分布:横向条形图(纯 CSS bar,无需图表库)
	const byCategory = data.by_category || [];
	const distEl = $('#attrCategoryDist');
	if (!byCategory.length) {
		distEl.innerHTML = '<div class="text-sub">暂无归因数据</div>';
		return;
	}
	const maxCount = Math.max(...byCategory.map(c => c.count), 1);
	distEl.innerHTML = byCategory.map(c => {
		const label = ATTR_CATEGORY_LABEL[c.category] || c.category;
		const widthPct = (c.count / maxCount * 100).toFixed(1);
		const avgPct = (c.avg_score * 100).toFixed(1);
		return `<div class="attr-bar-row">
			<div class="attr-bar-label">${escapeHtml(label)}</div>
			<div class="attr-bar-track">
				<div class="attr-bar-fill" style="width:${widthPct}%"></div>
			</div>
			<div class="attr-bar-count">${c.count}</div>
			<div class="attr-bar-avg">均分 ${avgPct}%</div>
		</div>`;
	}).join('');
}

// 渲染归因列表表格
function renderAttrList(data) {
	const rows = data.rows || [];
	const days = data.days || 7;
	$('#attrSummary').textContent = `共 ${rows.length} 条(最近 ${days} 天)`;

	const tbody = $('#attrTableBody');
	if (!rows.length) {
		tbody.innerHTML = `<tr><td colspan="11">
			<div class="empty-state">
				<div class="empty-state-icon">🧪</div>
				<div>暂无归因数据(低分 QA 评估完成后会自动归因)</div>
			</div>
		</td></tr>`;
		return;
	}

	tbody.innerHTML = rows.map(r => {
		const catLabel = r.category_label || ATTR_CATEGORY_LABEL[r.root_cause_category] || r.root_cause_category;
		const layerLabel = r.layer_label || ATTR_LAYER_LABEL[r.affected_layer] || r.affected_layer;
		const methodLabel = r.method_label || r.analysis_method;
		const statusLabel = r.status_label || r.status;
		const statusClass = r.status === 'completed' ? 'tag-success' : (r.status === 'failed' ? 'tag-danger' : 'tag-warning');
		const methodClass = r.analysis_method === 'rule' ? 'tag' : 'tag tag-info';
		return `<tr>
			<td>${r.qa_record_id}</td>
			<td title="${escapeHtml(r.question || '')}">${escapeHtml((r.question || '').substring(0, 40))}${(r.question || '').length > 40 ? '...' : ''}</td>
			<td title="${escapeHtml(r.answer || '')}">${escapeHtml((r.answer || '').substring(0, 50))}${(r.answer || '').length > 50 ? '...' : ''}</td>
			<td>${scorePill(r.avg_score, fmtPct)}</td>
			<td><span class="tag">${escapeHtml(catLabel)}</span></td>
			<td>${escapeHtml(layerLabel)}</td>
			<td><span class="${methodClass}">${escapeHtml(methodLabel)}</span></td>
			<td>${escapeHtml(r.root_type || '-')}</td>
			<td><span class="${statusClass}">${escapeHtml(statusLabel)}</span></td>
			<td class="text-sm text-sub">${formatDate(r.created_at)}</td>
			<td>
				<button class="btn btn-sm" onclick="showAttrDetail(${r.qa_record_id})">详情</button>
				<button class="btn btn-sm btn-primary" onclick="rerunAttr(${r.qa_record_id})">重跑</button>
			</td>
		</tr>`;
	}).join('');
}

// 手动触发单条 QA 归因(异步,派发后立即返回,前端不阻塞轮询)
// 归因任务通常 2~10s 完成(规则归因秒级,LLM 归因取决于模型响应),
// 这里不做轮询,只提示用户稍后刷新,避免复杂的状态机
async function runManualAttribution() {
	const qaId = $('#attrManualQaId').value.trim();
	if (!qaId) { toast('请输入 QA 记录 ID', 'error'); return; }

	const btn = $('#btnRunManualAttr');
	if (btn) { btn.disabled = true; }
	toast('归因已派发,规则归因秒级完成,LLM 归因约 10~30s,请稍后刷新查看', 'info');

	try {
		const resp = await api.postJson('/api/v1/analytics/low-score-analysis/run/', {
			qa_record_id: parseInt(qaId),
		});
		if (!resp || !resp.queued) {
			throw new Error(resp?.detail || '派发归因失败');
		}
		toast('归因已派发,3 秒后自动刷新列表', 'success');
		// 3 秒后自动刷新(规则归因通常已完成,LLM 归因可能仍在跑)
		setTimeout(() => loadAttribution(), 3000);
	} catch (e) {
		toast('归因失败: ' + (e.message || e), 'error');
	} finally {
		if (btn) { btn.disabled = false; }
	}
}

// 列表"重跑"按钮:复用 run 接口
async function rerunAttr(qaId) {
	toast('正在重新归因...', 'info');
	try {
		const resp = await api.postJson('/api/v1/analytics/low-score-analysis/run/', {
			qa_record_id: qaId,
		});
		if (!resp || !resp.queued) {
			throw new Error(resp?.detail || '派发归因失败');
		}
		toast('归因已派发,3 秒后自动刷新', 'success');
		setTimeout(() => loadAttribution(), 3000);
	} catch (e) {
		toast('归因失败: ' + (e.message || e), 'error');
	}
}

// 显示归因详情弹窗(完整对话 + 归因结论 + 低分维度 + 优化建议)
async function showAttrDetail(qaId) {
	$('#attrDetailTitle').textContent = `#${qaId}`;
	$('#attrDetailBody').innerHTML = '<div class="text-sub">加载中...</div>';
	document.getElementById('attrDetailDialog').style.display = 'flex';

	try {
		const data = await api.getJson(`/api/v1/analytics/low-score-analysis/detail/?qa_record_id=${qaId}`);

		const catLabel = data.category_label || ATTR_CATEGORY_LABEL[data.root_cause_category] || data.root_cause_category;
		const layerLabel = data.layer_label || ATTR_LAYER_LABEL[data.affected_layer] || data.affected_layer;
		const methodLabel = data.method_label || data.analysis_method;
		const statusLabel = data.status_label || data.status;

		let html = `
			<div class="mb-16">
				<div class="flex justify-between items-baseline mb-8">
					<strong>对话内容</strong>
					<span class="text-sm text-sub">均分 ${scorePill(data.avg_score, fmtPct)} · 阈值 ${fmtPct(data.threshold)} · 领域 ${escapeHtml(data.root_type || '-')}</span>
				</div>
				<div class="mb-8"><strong class="text-sub">问题:</strong> ${escapeHtml(data.full_question || data.question || '')}</div>
				<div><strong class="text-sub">回答:</strong> ${escapeHtml(data.full_answer || data.answer || '')}</div>
			</div>
		`;

		// 归因结论
		html += `<div class="attr-section mb-16">
			<div class="attr-section-title">归因结论</div>
			<div class="attr-conclusion">
				<div class="attr-meta-row">
					<span class="attr-meta-label">根因分类:</span>
					<span class="tag tag-danger">${escapeHtml(catLabel)}</span>
					<span class="attr-meta-label">影响层级:</span>
					<span class="tag">${escapeHtml(layerLabel)}</span>
					<span class="attr-meta-label">方法:</span>
					<span class="tag tag-info">${escapeHtml(methodLabel)}</span>
					<span class="attr-meta-label">状态:</span>
					<span class="tag">${escapeHtml(statusLabel)}</span>
				</div>
				<div class="attr-detail-text">${escapeHtml(data.root_cause_detail || '(无详细说明)')}</div>
				${data.diagnosis ? `<div class="attr-diagnosis">💡 ${escapeHtml(data.diagnosis)}</div>` : ''}
				${data.error_message ? `<div class="attr-error">⚠️ ${escapeHtml(data.error_message)}</div>` : ''}
			</div>
		</div>`;

		// 低分维度明细
		const lowDims = data.low_dimensions || [];
		if (lowDims.length) {
			html += `<div class="attr-section mb-16">
				<div class="attr-section-title">低分维度明细(均分 < 阈值)</div>
				${lowDims.map(d => {
					const dimLabel = DIM_LABEL[d.dimension] || d.dimension;
					return `<div class="attr-dim-row">
						<div class="flex justify-between items-baseline">
							<span>${escapeHtml(dimLabel)}</span>
							<span>${scorePill(d.score, fmtPct)}</span>
						</div>
						${d.reason ? `<div class="text-sm text-sub" style="margin-top:4px">${escapeHtml(d.reason)}</div>` : ''}
					</div>`;
				}).join('')}
			</div>`;
		}

		// 优化建议
		const suggestions = data.suggestions || [];
		if (suggestions.length) {
			html += `<div class="attr-section">
				<div class="attr-section-title">优化建议</div>
				${suggestions.map(s => {
					const typeLabel = s.type === 'short_term' ? '短期' : (s.type === 'long_term' ? '长期' : s.type);
					const typeClass = s.type === 'short_term' ? 'tag tag-warning' : 'tag tag-info';
					return `<div class="attr-suggestion-row">
						<span class="${typeClass}">${escapeHtml(typeLabel)}</span>
						<span class="attr-suggestion-text">${escapeHtml(s.action || '')}</span>
					</div>`;
				}).join('')}
			</div>`;
		}

		// LLM 调用元信息(仅 hybrid/llm 方法展示,体现成本可追溯)
		if (data.analysis_method !== 'rule' && (data.analysis_tokens_used || data.analysis_latency_ms)) {
			html += `<div class="text-sm text-sub mt-16">
				LLM: ${escapeHtml(data.analysis_model || '-')} ·
				Token ${data.analysis_tokens_used || 0} ·
				耗时 ${data.analysis_latency_ms || 0}ms ·
				${formatDate(data.created_at)}
			</div>`;
		}

		$('#attrDetailBody').innerHTML = html;
	} catch (e) {
		$('#attrDetailBody').innerHTML = `<div class="text-sub">加载失败: ${escapeHtml(e.message || String(e))}</div>`;
	}
}

/* ============ 工具函数 ============ */
function fmtPct(v) {
	if (v === null || v === undefined || isNaN(v)) return '--';
	return (Number(v) * 100).toFixed(1) + '%';
}

/* ============ 初始化 ============ */
document.addEventListener('DOMContentLoaded', () => {
	// 初始化 3 个 Tab 的组织架构级联下拉(内部异步加载数据,不阻塞 Tab 切换)
	initOrgFilters();
	switchEvalTab('golden');
});
