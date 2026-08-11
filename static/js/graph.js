/* ==========================================================
   知识图谱页面 (graph.js)
   包含：语义检索实体、ECharts 关系子图渲染、点击扩展邻居、
   实体详情面板、社区列表浏览、手动触发社区检测
   依赖：common.js（$/$/toast/escapeHtml/formatDate/hasAnyRole）、api.js、layout.js、ECharts
   ========================================================== */

const GRAPH_API = '/api/v1/graph';

const TYPE_LABELS = { PERSON: '人物', ORG: '组织', CONCEPT: '概念', TERM: '术语', PRODUCT: '产品' };
const TYPE_COLORS = { PERSON: '#f97316', ORG: '#3b82f6', CONCEPT: '#10b981', TERM: '#8b5cf6', PRODUCT: '#ec4899' };

let chart = null;                 // ECharts 实例
let graphNodes = new Map();       // 节点 id -> 节点数据
let graphEdges = new Map();       // 边 id -> 边数据
let expandedSet = new Set();      // 已展开过邻居的节点 id（避免重复请求）
let graphCenterId = null;         // 当前中心实体 id

let communityPage = 1;
const communityDetailCache = {};  // 社区详情缓存（卡片展开只请求一次）

/* ============ 初始化 ============ */
function initGraphPage() {
	// 社区检测：仅知识库管理员 / 超管可见（后端另有权限校验）
	if (hasAnyRole('super_admin', 'kb_admin')) {
		$('#btnCommunityDetect').style.display = '';
	}

	if (!window.echarts) {
		$('#graphContainer').innerHTML = `
      <div class="empty" style="padding:80px 0">
        <div class="empty-icon" style="font-size:48px">⚠️</div>
        <div class="empty-text">图谱渲染组件加载失败（ECharts CDN 不可达），请检查网络后刷新页面。<br>社区列表功能不受影响。</div>
      </div>`;
		return;
	}

	loadCommunities(1);
}

function switchTab(tab) {
	document.querySelectorAll('.tab-item').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
	// 两个 Tab 共用 .toolbar / .content-body 容器，内部按 Tab 显隐控件行与内容面板
	$('#tab-graph').style.display = tab === 'graph' ? '' : 'none';
	$('#tab-communities').style.display = tab === 'communities' ? '' : 'none';
	$('#toolbar-graph').style.display = tab === 'graph' ? '' : 'none';
	$('#toolbar-community').style.display = tab === 'communities' ? '' : 'none';
	// 切回图谱时重新布局（图表容器宽度可能变化）
	if (tab === 'graph' && chart) setTimeout(() => chart.resize(), 50);
}

/* ============ 语义检索实体 ============ */
async function doEntitySearch() {
	const q = $('#entitySearchInput').value.trim();
	if (!q) { toast('请输入要检索的实体关键词', 'warning'); return; }

	const type = $('#entityTypeFilter').value;
	$('#graphSearchHint').textContent = '🔍 检索中...';
	$('#graphSearchResults').style.display = 'none';

	try {
		const params = new URLSearchParams({ q, top_k: 10 });
		if (type) params.set('type', type);
		const data = await api.getJson(`${GRAPH_API}/entities/search/?${params.toString()}`);
		const results = data.results || [];

		if (!results.length) {
			// 语义检索无命中：回退到名称模糊检索，保证"实体存在但无向量"场景可用
			const fb = await nameSearchFallback(q, type);
			if (!fb.length) {
				$('#graphSearchHint').textContent = `未找到与"${escapeHtml(q)}"匹配的实体`;
				return;
			}
			renderSearchResults(fb, true);
			loadSubgraph(fb[0].id);
			return;
		}

		$('#graphSearchHint').textContent = `命中 ${results.length} 个实体，已渲染"${escapeHtml(results[0].name)}"的关系子图，点击其他结果可切换`;
		renderSearchResults(results, false);
		loadSubgraph(results[0].entity_id || results[0].id);
	} catch (e) {
		$('#graphSearchHint').textContent = `检索失败：${escapeHtml(e.message)}`;
	}
}

async function nameSearchFallback(q, type) {
	const params = new URLSearchParams({ q, page_size: 10 });
	if (type) params.set('type', type);
	const data = await api.getJson(`${GRAPH_API}/entities/?${params.toString()}`);
	return (data.results || []).map(r => ({ id: r.id, name: r.name, type: r.type, description: r.description }));
}

function renderSearchResults(results, isNameFallback) {
	const box = $('#graphSearchResults');
	box.style.display = '';
	box.innerHTML = (results || []).map((r, i) => `
    <span class="gsr-item ${i === 0 ? 'gsr-active' : ''}" title="${isNameFallback ? '名称匹配' : `相似度 ${(r.score || 0).toFixed(2)}`}"
      onclick="switchCenter(${r.entity_id || r.id})">
      ${escapeHtml(r.name)} <span class="gsr-score">${TYPE_LABELS[r.type] || r.type}${isNameFallback ? '' : ' · ' + (r.score || 0).toFixed(2)}</span>
    </span>`).join('');
}

function switchCenter(id) {
	document.querySelectorAll('#graphSearchResults .gsr-item').forEach(el => el.classList.remove('gsr-active'));
	loadSubgraph(id);
}

/* ============ 子图加载 / 邻居扩展 ============ */
async function loadSubgraph(entityId, depth = 2) {
	if (!window.echarts) return;
	showGraphLoading();
	try {
		const data = await api.getJson(`${GRAPH_API}/entities/${entityId}/neighbors/?depth=${depth}`);
		mergeSubgraph(data, true);
		renderGraph();
		showEntityDetail(entityId);
	} catch (e) {
		$('#graphSearchHint').textContent = `子图加载失败：${escapeHtml(e.message)}`;
		renderGraph();
	}
}

async function expandNode(entityId) {
	if (expandedSet.has(entityId)) {
		// 已展开过：仅刷新详情面板
		showEntityDetail(entityId);
		return;
	}
	expandedSet.add(entityId);
	try {
		const data = await api.getJson(`${GRAPH_API}/entities/${entityId}/neighbors/?depth=1`);
		mergeSubgraph(data, false);
		renderGraph();
		showEntityDetail(entityId);
	} catch (e) {
		expandedSet.delete(entityId);
		toast('邻居扩展失败：' + e.message, 'error');
	}
}

function mergeSubgraph(data, isNewCenter) {
	const newNodes = data.nodes || [];
	const newEdges = data.edges || [];

	if (isNewCenter) {
		// 切换中心实体：保留旧图但更新中心标记（不清空，便于回溯）
		graphCenterId = data.center;
	}
	newNodes.forEach(n => {
		const existing = graphNodes.get(n.id);
		graphNodes.set(n.id, {
			id: n.id,
			name: n.name,
			type: n.type,
			type_label: n.type_label,
			description: n.description,
			is_center: isNewCenter ? n.is_center : (existing ? existing.is_center : false),
		});
	});
	// 新中心实体强制标记为中心
	if (isNewCenter && graphCenterId != null && graphNodes.has(graphCenterId)) {
		graphNodes.get(graphCenterId).is_center = true;
	}
	newEdges.forEach(e => graphEdges.set(e.id, e));
}

function renderGraph() {
	if (!window.echarts) return;

	if (!graphNodes.size) {
		disposeChart();
		$('#graphContainer').innerHTML = `
      <div class="empty" style="padding:80px 0">
        <div class="empty-icon" style="font-size:48px">🕸️</div>
        <div class="empty-text">输入关键词检索实体，或点击社区中的实体查看其关系子图</div>
      </div>`;
		return;
	}

	if (!chart) {
		$('#graphContainer').innerHTML = '';
		chart = echarts.init($('#graphContainer'));
		// 点击节点：展开其邻居并展示详情
		chart.on('click', params => {
			if (params.dataType === 'node' && params.data) expandNode(parseInt(params.data.id, 10));
		});
		window.addEventListener('resize', () => chart && chart.resize());
	}

	// 节点度数（用于节点大小），边标签仅在关系较少或与中心相连时展示避免遮挡
	const degree = {};
	graphEdges.forEach(e => {
		degree[e.source] = (degree[e.source] || 0) + 1;
		degree[e.target] = (degree[e.target] || 0) + 1;
	});
	const showAllLabels = graphEdges.size <= 60;

	const nodes = [...graphNodes.values()].map(n => ({
		id: String(n.id),
		name: n.name,
		value: n.name,
		category: TYPE_LABELS[n.type] || n.type,
		// 中心实体放大并描边突出，其余按度数自适应
		symbolSize: n.is_center ? 34 : 18 + Math.min(degree[n.id] || 0, 8) * 3,
		itemStyle: {
			color: TYPE_COLORS[n.type] || '#64748b',
			borderColor: n.is_center ? '#f59e0b' : 'transparent',
			borderWidth: n.is_center ? 3 : 0,
		},
		label: { show: true, fontSize: 12, color: '#334155' },
	}));

	const links = [...graphEdges.values()].map(e => ({
		source: String(e.source),
		target: String(e.target),
		relation_type: e.relation_type,
		// 中心关联边或小图时显示关系标签，大图隐藏避免视觉噪声
		label: { show: showAllLabels || e.source === graphCenterId || e.target === graphCenterId, fontSize: 10, color: '#94a3b8' },
	}));

	// 渲染前 resize：搜索结果条显隐 / Tab 切换等会导致容器尺寸变化，先让图表自适应再重绘
	chart.resize();

	chart.setOption({
		tooltip: {
			trigger: 'item',
			formatter: params => {
				if (params.dataType === 'edge') return `${escapeHtml(params.data.relation_type)}`;
				const n = graphNodes.get(parseInt(params.data.id, 10));
				if (!n) return '';
				return `<b>${escapeHtml(n.name)}</b> <span style="color:#94a3b8">${TYPE_LABELS[n.type] || n.type}</span><br>${escapeHtml((n.description || '暂无描述').slice(0, 120))}`;
			},
		},
		legend: {
			data: Object.values(TYPE_LABELS),
			top: 8,
			textStyle: { fontSize: 12 },
		},
		series: [{
			type: 'graph',
			layout: 'force',
			roam: true,
			draggable: true,
			categories: Object.values(TYPE_LABELS).map(n => ({ name: n })),
			data: nodes,
			links,
			force: { repulsion: 280, edgeLength: [60, 140], gravity: 0.08 },
			emphasis: { focus: 'adjacency', lineStyle: { width: 3 }, label: { fontWeight: 600 } },
			lineStyle: { color: '#cbd5e1', width: 1.5, curveness: 0.15 },
			label: { show: true },
		}],
	});
}

function showGraphLoading() {
	$('#graphSearchHint').textContent = '🕸️ 子图加载中...';
}

function disposeChart() {
	if (chart) { chart.dispose(); chart = null; }
}

/* ============ 实体详情面板 ============ */
async function showEntityDetail(id) {
	const panel = $('#entityPanel');
	panel.style.display = '';
	panel.innerHTML = '<div class="text-sub">加载中...</div>';
	try {
		const d = await api.getJson(`${GRAPH_API}/entities/${id}/`);
		const aliases = (d.aliases || []).map(a => `<span class="tag">${escapeHtml(a)}</span>`).join(' ') || '无';
		const docs = (d.source_docs || []).map(doc => `<li>📄 ${escapeHtml(doc.title)}</li>`).join('') || '<li class="text-sub">无可显示来源文档</li>';
		panel.innerHTML = `
      <div class="entity-panel-title">${escapeHtml(d.name)} <span class="tag tag-info">${escapeHtml(d.type_label)}</span></div>
      <div class="ep-meta">🕐 ${formatDate(d.updated_at)} · 📄 来源文档 ${d.source_doc_count} 篇（可见 ${d.source_docs.length}）</div>
      <div class="ep-section">
        <div class="ep-section-title">描述</div>
        <div class="ep-desc">${escapeHtml(d.description || '暂无')}</div>
      </div>
      <div class="ep-section">
        <div class="ep-section-title">别名</div>
        <div>${aliases}</div>
      </div>
      <div class="ep-section">
        <div class="ep-section-title">可见来源文档</div>
        <ul class="ep-docs">${docs}</ul>
      </div>
      <div class="ep-section" style="display:flex;gap:8px">
        <button class="btn btn-sm btn-primary" onclick="expandNode(${d.id})">＋ 展开邻居</button>
        <button class="btn btn-sm" onclick="loadSubgraph(${d.id}, 2)">↻ 以此为中心</button>
      </div>`;
	} catch (e) {
		panel.innerHTML = `<div class="text-danger">详情加载失败：${escapeHtml(e.message)}</div>`;
	}
}

function resetGraph() {
	graphNodes.clear();
	graphEdges.clear();
	expandedSet.clear();
	graphCenterId = null;
	$('#graphSearchResults').style.display = 'none';
	$('#graphSearchResults').innerHTML = '';
	$('#graphSearchHint').textContent = '';
	$('#entityPanel').style.display = 'none';
	disposeChart();
	renderGraph();
}

/* ============ 社区列表 ============ */
async function loadCommunities(page) {
	communityPage = page;
	const level = $('#communityLevelFilter').value;
	const q = ($('#communitySearchInput').value || '').trim();
	const params = new URLSearchParams({ page });
	if (level) params.set('level', level);
	if (q) params.set('q', q);

	const list = $('#communityList');
	list.innerHTML = '<div class="community-loading">🗂️ 加载中...</div>';
	try {
		const data = await api.getJson(`${GRAPH_API}/communities/?${params.toString()}`);
		const count = data.count || 0;
		const totalPages = Math.max(1, Math.ceil(count / 20));
		renderCommunities(data.results || []);

		// 使用公共 Pagination 组件渲染分页
		const pgnState = { page: communityPage, totalPages, total: count, pageSize: 20 };
		if (communityPage > 1) {
			Pagination.update(pgnState);
		} else {
			Pagination.render({
				container: '#communityPagination',
				...pgnState,
				align: 'center',
				onPageChange: (p) => loadCommunities(p)
			});
		}
	} catch (e) {
		list.innerHTML = `<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">😥</div><div>加载失败：${escapeHtml(e.message)}</div></div>`;
	}
}

function renderCommunities(rows) {
	const list = $('#communityList');
	if (!rows.length) {
		list.innerHTML = '<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">🗂️</div><div>暂无社区数据，可点击右上角"重新检测社区"生成</div></div>';
		return;
	}
	list.innerHTML = rows.map(c => `
    <div class="community-card" id="communityCard${c.id}" onclick="toggleCommunityDetail(${c.id})">
      <div class="community-card-head">
        <span class="community-card-topic">${escapeHtml(c.topic || `社区 #${c.community_id}`)}</span>
        <span class="tag tag-info">L${c.level}</span>
        <span class="badge badge-default">${c.entity_count} 实体</span>
        <span style="font-size:12px;color:var(--text-sub)">🕐 ${formatDate(c.updated_at)}</span>
      </div>
      <div class="community-card-summary">${escapeHtml(c.summary || '暂无摘要（触发社区检测后自动生成）')}</div>
      <div class="community-card-meta">
        ${(c.keywords || []).map(k => `<span class="tag">${escapeHtml(k)}</span>`).join('')}
        <span class="text-sub">▾ 点击查看社区实体</span>
      </div>
      <div class="community-entities" id="communityEntities${c.id}" style="display:none"></div>
    </div>`).join('');
}

async function toggleCommunityDetail(id) {
	const box = $(`#communityEntities${id}`);
	const isHidden = box.style.display === 'none';
	box.style.display = isHidden ? '' : 'none';
	if (!isHidden) return;

	// 详情缓存：同一社区只请求一次
	if (communityDetailCache[id]) {
		box.innerHTML = communityDetailCache[id];
		return;
	}

	box.innerHTML = '<div class="community-loading">加载社区实体...</div>';
	try {
		const d = await api.getJson(`${GRAPH_API}/communities/${id}/`);
		const entities = (d.entities || []).map(e => `
      <span class="community-entity-item" onclick="openEntityFromCommunity(${e.id})" title="${escapeHtml(e.description || '')}">
        ${escapeHtml(e.name)} <span style="font-size:11px;color:var(--text-sub)">${TYPE_LABELS[e.type] || e.type}</span>
      </span>`).join('') || '<span class="text-sub">无可见实体</span>';
		const html = `<div class="community-entities-title">社区实体（${(d.entities || []).length}/${d.entity_count}）</div><div>${entities}</div>`;
		communityDetailCache[id] = html;
		box.innerHTML = html;
	} catch (e) {
		box.innerHTML = `<div class="text-danger">加载失败：${escapeHtml(e.message)}</div>`;
	}
}

function openEntityFromCommunity(id) {
	// 跳到图谱 Tab 并渲染该实体的关系子图
	switchTab('graph');
	loadSubgraph(id, 2);
}

/* ============ 手动触发社区检测 ============ */
function confirmDetect() {
	// 关键操作二次确认：复用 common.js 的 showConfirmDialog（模糊背景，层级高于普通弹窗）
	showConfirmDialog({
		title: '重新检测社区',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: '将重建全部社区并调用 LLM 生成摘要，耗时较长',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{
				text: '确认提交',
				type: 'danger',
				onClick: async (ctx) => {
					try {
						const res = await api.postJson(`${GRAPH_API}/communities/detect/`, {});
						ctx.close();
						toast(res.detail || '任务已提交', 'success');
						// 异步任务完成后刷新社区列表
						setTimeout(() => loadCommunities(1), 5000);
					} catch (e) {
						// 提交失败在弹窗内提示，保留弹窗便于用户重试
						ctx.setError('提交失败：' + e.message);
					}
				}
			}
		]
	});
}

document.addEventListener('DOMContentLoaded', initGraphPage);
