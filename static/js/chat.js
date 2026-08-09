/* ============ 聊天页面 ============ */

/* ---- 模板工具函数 ---- */
function tpl(id) {
	return document.getElementById(id);
}

function htmlFromTpl(id, fillFn) {
	const frag = tpl(id).content.cloneNode(true);
	if (fillFn) fillFn(frag);
	const wrapper = document.createElement('div');
	wrapper.appendChild(frag);
	return wrapper.innerHTML;
}

function elemFromTpl(id, fillFn) {
	const frag = tpl(id).content.cloneNode(true);
	if (fillFn) fillFn(frag);
	return frag.firstElementChild;
}

/* ---- 知识库范围选择器状态 ---- */
const SCOPE_STORAGE_KEY = 'rag_chat_scope';
const MODE_STORAGE_KEY = 'rag_chat_mode';  // 问答模式持久化 key
const DRAFT_KEY = 'rag_chat_draft';  // 输入草稿 key（sessionStorage，标签页关闭即失效）
// 会话详情缓存：localStorage 存储，key 格式 rag_session_cache_{id}
const SESSION_CACHE_PREFIX = 'rag_session_cache_';
const MAX_CACHED_SESSIONS = 20;       // 只缓存最近 20 条会话
const MAX_SESSION_CACHE_SIZE = 51200; // 单条缓存上限 50KB，超长对话不缓存
let scopeOpen = false;
let allScopeIds = [];
let selectedScopeIds = new Set();  // 统一存储字符串 ID，避免与 API 数字 ID 混淆
let scopeFlatList = [];  // 缓存扁平化节点，避免重复遍历
let currentSessionId = null;
let isSending = false;
let currentAbortController = null;  // 当前流式请求的 AbortController，供 stopChat 中断
let userAborted = false;  // 标记是否用户主动终止（区分超时中断）
// 问答模式：auto（LLM 自主决定是否调用工具）/ rag（传统 RAG）/ agent（强制 Agent）
let currentMode = 'auto';

document.addEventListener('DOMContentLoaded', () => {
	initChatPage();
	initScopePicker();
	initSessionList();
	initModeSwitcher();
	initDraftRestore();
	const wrap = $('#scopeNavWrap');
	if (wrap) wrap.style.display = '';
	document.addEventListener('click', (e) => {
		if (scopeOpen && !e.target.closest('#scopeDropdown') && !e.target.closest('#scopeTrigger')) {
			closeScopePicker();
		}
	});
});

/* ---- 输入草稿恢复 ----
 * 页面加载时从 sessionStorage 恢复未发送的输入内容；
 * 输入时实时保存（防抖 300ms），发送成功后清除。
 * 用 sessionStorage 而非 localStorage：标签页关闭即失效，避免跨会话污染。
 */
let draftSaveTimer = null;
function initDraftRestore() {
	const inp = $('#chatInput');
	if (!inp) return;
	// 恢复草稿
	try {
		const raw = sessionStorage.getItem(DRAFT_KEY);
		if (raw) {
			inp.value = raw;
			// 光标移到末尾
			inp.focus();
			inp.setSelectionRange(inp.value.length, inp.value.length);
		}
	} catch (e) { /* sessionStorage 不可用时静默降级 */ }
	// 输入时防抖保存
	inp.addEventListener('input', () => {
		if (draftSaveTimer) clearTimeout(draftSaveTimer);
		draftSaveTimer = setTimeout(() => {
			try {
				const val = inp.value.trim();
				if (val) {
					sessionStorage.setItem(DRAFT_KEY, val);
				} else {
					sessionStorage.removeItem(DRAFT_KEY);
				}
			} catch (e) { /* ignore */ }
		}, 300);
	});
}

/* ---- 问答模式切换器初始化 ----
 * 从 localStorage 恢复上次选择的模式，默认 auto。
 * 模式说明：
 *   - auto: Agent 模式，LLM 通过 tool_choice='auto' 自主决定是否调用工具（推荐）
 *   - rag:  传统 RAG，预检索 + LLM 生成，不调用工具
 *   - agent: 强制 Agent 模式，必定走 ReAct 工具循环
 */
function initModeSwitcher() {
	const saved = localStorage.getItem(MODE_STORAGE_KEY);
	if (saved && ['auto', 'rag', 'agent'].includes(saved)) {
		currentMode = saved;
	}
	// 同步 UI 高亮
	const switcher = $('#modeSwitcher');
	if (switcher) {
		switcher.querySelectorAll('.mode-btn').forEach(btn => {
			btn.classList.toggle('active', btn.dataset.mode === currentMode);
		});
	}
}

/* 切换问答模式（由 UI 按钮点击触发） */
function setChatMode(mode) {
	if (!['auto', 'rag', 'agent'].includes(mode)) return;
	if (mode === currentMode) return;
	currentMode = mode;
	localStorage.setItem(MODE_STORAGE_KEY, mode);
	const switcher = $('#modeSwitcher');
	if (switcher) {
		switcher.querySelectorAll('.mode-btn').forEach(btn => {
			btn.classList.toggle('active', btn.dataset.mode === mode);
		});
	}
	const label = { auto: 'Auto（LLM 自主）', rag: 'RAG（传统检索）', agent: 'Agent（强制工具）' }[mode];
	toast('已切换为 ' + label + ' 模式', 'success');
}

function initChatPage() {
	const msgs = $('#chatMessages');
	if (msgs) msgs.innerHTML = renderEmptyState();
}

function renderEmptyState() {
	return tpl('tmpl-chat-empty').innerHTML;
}

/* ---- 知识库范围选择器 ---- */
let scopeTreeData = [];
// 节点树 localStorage 缓存：TTL 2 小时，节点树变更频率低
const NODES_CACHE_KEY = 'rag_nodes_tree_cache';
const NODES_CACHE_TTL = 2 * 60 * 60 * 1000;  // 2 小时

/* 从 localStorage 读取节点树缓存，过期返回 null */
function getNodesCache() {
	try {
		const raw = localStorage.getItem(NODES_CACHE_KEY);
		if (!raw) return null;
		const data = JSON.parse(raw);
		if (Date.now() - data.timestamp > NODES_CACHE_TTL) return null;
		return data.tree || [];
	} catch (e) { return null; }
}

/* 写入节点树缓存到 localStorage */
function setNodesCache(tree) {
	try {
		localStorage.setItem(NODES_CACHE_KEY, JSON.stringify({
			timestamp: Date.now(),
			tree: tree,
		}));
	} catch (e) { /* localStorage 满或不可用，静默降级 */ }
}

/* 应用节点树数据：扁平化 + 渲染 + 初始化选中状态 */
function applyScopeData(tree) {
	scopeTreeData = tree;
	scopeFlatList = flattenScopeNodes(scopeTreeData, 0);
	allScopeIds = scopeFlatList.map(n => String(n.id));

	const saved = localStorage.getItem(SCOPE_STORAGE_KEY);
	if (!saved || selectedScopeIds.size === 0) {
		selectedScopeIds = new Set(allScopeIds);
		saveScopeState();
	}

	renderScopeList(scopeFlatList);
	updateScopeBadge();
}

async function initScopePicker() {
	const saved = localStorage.getItem(SCOPE_STORAGE_KEY);
	if (saved) {
		try { selectedScopeIds = new Set(JSON.parse(saved).map(String)); } catch (e) { selectedScopeIds = new Set(); }
	}

	// 优先用缓存立即渲染，再后台静默刷新
	const cachedTree = getNodesCache();
	if (cachedTree && cachedTree.length > 0) {
		applyScopeData(cachedTree);
		// 后台静默刷新：不阻塞页面渲染
		api.getJson('/api/v1/knowledge/nodes/tree/').then(data => {
			const freshTree = data.tree || [];
			if (freshTree.length > 0) {
				setNodesCache(freshTree);
				applyScopeData(freshTree);
			}
		}).catch(() => { /* 静默失败，保留缓存数据 */ });
		return;
	}

	// 无缓存或已过期：正常请求
	try {
		const data = await api.getJson('/api/v1/knowledge/nodes/tree/');
		const tree = data.tree || [];
		applyScopeData(tree);
		if (tree.length > 0) setNodesCache(tree);
	} catch (e) {
		console.error('load nodes failed:', e);
	}
}

function flattenScopeNodes(nodes, depth, parentId) {
	const result = [];
	for (const n of nodes) {
		const id = String(n.id);
		result.push({ id, name: n.name, depth, parent_id: parentId ? String(parentId) : null });
		if (n.children && n.children.length) {
			result.push(...flattenScopeNodes(n.children, depth + 1, n.id));
		}
	}
	return result;
}

function renderScopeList(flat) {
	const el = $('#scopeList');
	if (!el) return;
	el.innerHTML = flat.map(n => {
		const indent = 20 + n.depth * 20;
		return htmlFromTpl('tmpl-scope-node', (frag) => {
			const label = frag.querySelector('label');
			label.className = 'scope-item';
			label.style.paddingLeft = indent + 'px';
			const cb = frag.querySelector('input');
			cb.value = n.id;
			cb.setAttribute('onchange', "onScopeChange(this, '" + n.id + "')");
			if (selectedScopeIds.has(n.id)) cb.setAttribute('checked', '');
			frag.querySelector('.scope-label').textContent = n.name;
		});
	}).join('');
}

/**
 * 勾选/取消勾选某个节点时的级联逻辑：
 *  - 勾选父节点：所有子孙节点都勾选
 *  - 取消勾选父节点：所有子孙节点都取消勾选
 *  - 勾选子节点：所有祖先节点都勾选
 *  - 取消勾选子节点：若同级全部未勾选，则祖先节点也取消勾选
 */
function onScopeChange(cb, id) {
	id = String(id);
	if (cb.checked) {
		selectedScopeIds.add(id);
		selectChildNodes(id);     // 选中所有子孙
		selectAncestorNodes(id);  // 选中所有祖先
	} else {
		selectedScopeIds.delete(id);
		unselectChildNodes(id);   // 取消所有子孙
		unselectAncestorIfNoChildSelected(id);  // 必要时取消祖先
	}
	saveScopeState();
	updateScopeBadge();
	// 仅重新渲染列表，不重新请求 API
	renderScopeList(scopeFlatList);
}

function getChildIds(parentId) {
	parentId = String(parentId);
	return scopeFlatList.filter(n => n.parent_id === parentId).map(n => n.id);
}

function selectChildNodes(parentId) {
	const childIds = getChildIds(parentId);
	for (const cid of childIds) {
		selectedScopeIds.add(cid);
		selectChildNodes(cid);  // 递归
	}
}

function unselectChildNodes(parentId) {
	const childIds = getChildIds(parentId);
	for (const cid of childIds) {
		selectedScopeIds.delete(cid);
		unselectChildNodes(cid);  // 递归
	}
}

function selectAncestorNodes(nodeId) {
	const node = scopeFlatList.find(n => n.id === nodeId);
	if (!node || !node.parent_id) return;
	selectedScopeIds.add(node.parent_id);
	selectAncestorNodes(node.parent_id);  // 递归向上
}

function unselectAncestorIfNoChildSelected(nodeId) {
	const node = scopeFlatList.find(n => n.id === nodeId);
	if (!node || !node.parent_id) return;
	const siblings = getChildIds(node.parent_id);
	const anyChecked = siblings.some(sid => selectedScopeIds.has(sid));
	if (!anyChecked) {
		selectedScopeIds.delete(node.parent_id);
		unselectAncestorIfNoChildSelected(node.parent_id);  // 递归向上
	}
}

function selectAllScopes() {
	selectedScopeIds = new Set(allScopeIds);
	saveScopeState();
	renderScopeList(scopeFlatList);
	updateScopeBadge();
}

function clearAllScopes() {
	selectedScopeIds = new Set();
	saveScopeState();
	renderScopeList(scopeFlatList);
	updateScopeBadge();
}

function toggleScopePicker() {
	if (scopeOpen) { closeScopePicker(); return; }
	scopeOpen = true;
	$('#scopeDropdown').classList.add('open');
	$('#scopeTrigger').classList.add('open');
}

function closeScopePicker() {
	scopeOpen = false;
	$('#scopeDropdown').classList.remove('open');
	$('#scopeTrigger').classList.remove('open');
}

function saveScopeState() {
	localStorage.setItem(SCOPE_STORAGE_KEY, JSON.stringify([...selectedScopeIds]));
}

function updateScopeBadge() {
	const cnt = selectedScopeIds.size;
	const total = allScopeIds.length;
	const badge = $('#scopeBadge');
	if (!badge) return;
	if (cnt === total) {
		badge.textContent = '已全选';
	} else if (cnt === 0) {
		badge.textContent = '未选择';
	} else {
		badge.textContent = '已选 ' + cnt;
	}
}

/* ---- 构建溯源来源 HTML ----
 * 按数据来源渲染四类徽标卡片：
 *   - 文档：citations（route_source=wiki/graphrag 或 knowledge_search 工具），标题可点击跳转文档预览
 *   - 数据库：text2sql 工具调用，展示表名 + 行数，可展开查看 SQL（便于复核）
 *   - 网络：web_search 工具调用，展示搜索关键词
 *   - LLM：无任何引用时提示"基于模型知识回答"
 * 历史 citations 缺失 document_id 时标题降级为纯文本（兼容缺失字段，不可点击）。
 * @param {Array} citations - 文档引用列表（可能为空）
 * @param {string|null} routeSource - 路由来源（wiki/graphrag_local/graphrag_global/rag/agent）
 * @param {Array} toolTraces - Agent 工具调用链（含 text2sql/web_search 等）
 * @param {boolean} isPending - Agent 工具尚未返回时先不渲染 LLM 占位，避免闪烁
 */
function buildSourceHtml(citations, routeSource, toolTraces, isPending) {
	const cards = [];
	const traces = Array.isArray(toolTraces) ? toolTraces : [];

	// 1. 文档引用卡片（可点击跳转预览）
	(Array.isArray(citations) ? citations : []).forEach(c => {
		cards.push(buildDocSourceCard(c, routeSource));
	});

	// 2. 数据库来源（text2sql 工具调用）
	traces.forEach(t => {
		if (t.tool_name !== 'text2sql') return;
		// 执行失败的查询不展示为来源，避免误导用户
		if (t.ok === false || t.result_ok === false) return;
		const card = buildDbSourceCard(t);
		if (card) cards.push(card);
	});

	// 3. 网络来源（web_search 工具调用）
	traces.forEach(t => {
		if (t.tool_name !== 'web_search') return;
		if (t.ok === false || t.result_ok === false) return;
		cards.push(buildWebSourceCard(t));
	});

	// 4. 全无引用 → 基于模型知识（Agent 工具未执行完时先留空，等 done 事件再渲染）
	if (cards.length === 0) {
		if (isPending) return '';
		return htmlFromTpl('tmpl-source-llm', () => {});
	}

	return htmlFromTpl('tmpl-source-block', (frag) => {
		frag.querySelector('.source-header').textContent = '📎 溯源来源 · ' + cards.length + ' 项';
		frag.querySelector('.source-list').innerHTML = cards.join('');
	});
}

/* 文档引用卡片：徽标 + 可点击标题（有 document_id 时跳转预览并定位页码） */
function buildDocSourceCard(c, routeSource) {
	return htmlFromTpl('tmpl-source-card', (frag) => {
		const badgeEl = frag.querySelector('.source-card-badge');
		// Wiki / GraphRAG 路由下的引用同属文档来源，细分徽标便于用户识别检索层
		let badgeText = '文档';
		if (routeSource === 'wiki') badgeText = '文档 · Wiki';
		else if (routeSource && String(routeSource).startsWith('graphrag')) badgeText = '文档 · 图谱';
		badgeEl.textContent = badgeText;
		badgeEl.className = 'source-card-badge badge-doc';

		const titleEl = frag.querySelector('.source-card-title');
		titleEl.textContent = c.doc_title || '未知文档';
		// 历史数据可能缺失 document_id → 降级为不可点击纯文本
		const docId = c.document_id || c.doc_id;
		if (docId) {
			titleEl.classList.add('source-card-title-link');
			titleEl.setAttribute('title', '点击预览文档');
			const page = (Array.isArray(c.page) && c.page[0]) || 1;
			titleEl.setAttribute('onclick', 'previewCitation(' + docId + ', ' + page + ')');
		}

		// 元信息（章节/页码/引用数）inline 排列，节省纵向空间
		let meta = '';
		if (c.section) {
			meta += '<span class="source-card-section">章节: ' + escapeHtml(c.section) + '</span>';
		}
		if (c.page && Array.isArray(c.page) && c.page.length) {
			meta += '<span class="source-card-page">页码: ' + c.page.map(p => 'P' + escapeHtml(String(p))).join(', ') + '</span>';
		}
		if (c.chunk_ids && c.chunk_ids.length > 0) {
			meta += '<span class="source-card-count">引用 ' + c.chunk_ids.length + ' 处</span>';
		}
		frag.querySelector('.source-card-meta').innerHTML = meta;
	});
}

/* 数据库来源卡片：表名 + 行数 + 可展开的 SQL（表名/SQL 均从工具调用链推导，历史数据缺失时降级） */
function buildDbSourceCard(t) {
	const args = t.tool_args || {};
	const sql = extractToolSql(t);
	// 表名：优先工具参数 tables，缺失时从 SQL 的 FROM/JOIN 子句提取
	let tables = Array.isArray(args.tables) ? args.tables.slice() : [];
	if (tables.length === 0 && sql) tables = extractTablesFromSql(sql);
	const tableName = tables.length > 0 ? tables.join(' / ') : '业务数据库';

	return htmlFromTpl('tmpl-source-card', (frag) => {
		const badgeEl = frag.querySelector('.source-card-badge');
		badgeEl.textContent = '数据库';
		badgeEl.className = 'source-card-badge badge-db';

		frag.querySelector('.source-card-title').textContent = tableName + ' 表';

		let meta = '';
		const rows = extractRowsFromResult(t);
		if (rows != null) {
			meta += '<span class="source-card-count">查询到 ' + rows + ' 行</span>';
		}
		if (sql) {
			meta += '<span class="source-card-sql-toggle" onclick="toggleSourceSql(this)">查看 SQL ▾</span>';
		}
		frag.querySelector('.source-card-meta').innerHTML = meta;

		if (sql) {
			const sqlEl = document.createElement('div');
			sqlEl.className = 'source-card-sql hidden';
			sqlEl.textContent = sql;
			frag.querySelector('.source-card').appendChild(sqlEl);
		}
	});
}

/* 网络来源卡片：徽标 + 搜索关键词 */
function buildWebSourceCard(t) {
	const args = t.tool_args || {};
	return htmlFromTpl('tmpl-source-card', (frag) => {
		const badgeEl = frag.querySelector('.source-card-badge');
		badgeEl.textContent = '网络';
		badgeEl.className = 'source-card-badge badge-web';
		const titleEl = frag.querySelector('.source-card-title');
		titleEl.textContent = '联网搜索';
		titleEl.setAttribute('title', '外部网络信息，仅供参考');
		frag.querySelector('.source-card-meta').innerHTML =
			'<span class="source-card-section">关键词: ' + escapeHtml(args.query || '-') + '</span>';
	});
}

/* 提取 text2sql 执行的 SQL：流式 tool_traces 带 meta.sql，历史记录从结果文本 "SQL: ..." 解析 */
function extractToolSql(t) {
	const meta = t.meta || {};
	if (meta.sql) return String(meta.sql).trim();
	const text = t.result || t.tool_result || '';
	// 结果文本格式：'SQL: <sql>\n\n查询结果...'，SQL 取到首个空行之前（SQL 本身可含换行）
	const m = String(text).match(/^SQL:\s*([\s\S]*?)(?:\n\s*\n|$)/);
	return m ? m[1].trim() : '';
}

/* 从 SQL 的 FROM/JOIN 子句提取表名（去重；带 schema 前缀时只取表名） */
function extractTablesFromSql(sql) {
	const tables = [];
	const re = /\b(?:FROM|JOIN)\s+([A-Za-z0-9_."]+)/gi;
	let m;
	while ((m = re.exec(sql)) !== null) {
		const name = m[1].replace(/["']/g, '').split('.').pop();
		if (name && !tables.includes(name)) tables.push(name);
	}
	return tables;
}

/* 提取查询返回行数：优先 meta.rows，历史记录从结果文本 "共 N 行" 解析 */
function extractRowsFromResult(t) {
	const meta = t.meta || {};
	if (meta.rows != null) return meta.rows;
	const text = t.result || t.tool_result || '';
	const m = String(text).match(/共\s*(\d+)\s*行/);
	return m ? parseInt(m[1], 10) : null;
}

/* 展开/收起来源卡片中的 SQL（数据库来源，便于用户复核查询语句） */
function toggleSourceSql(btn) {
	const card = btn.closest('.source-card');
	const sqlEl = card && card.querySelector('.source-card-sql');
	if (!sqlEl) return;
	sqlEl.classList.toggle('hidden');
	btn.textContent = sqlEl.classList.contains('hidden') ? '查看 SQL ▾' : '收起 SQL ▴';
}

/* 从引用卡片跳转文档预览并定位页码（复用 preview-doc.js；document_id 由后端在 citations 中带出） */
function previewCitation(docId, page) {
	if (!docId) return;
	previewTargetId = docId;
	previewDocPage(docId, page || 1);
}

/* 预览弹窗元信息条数据源：按文档 ID 拉取元信息；拉取失败返回 null（预览主流程不受影响） */
async function getDocForPreview(id) {
	try {
		return await api.getJson('/api/v1/knowledge/documents/' + id + '/');
	} catch (e) {
		return null;
	}
}

/* ---- 发送消息 ---- */
function handleChatKey(e) {
	if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

/* ---- Agent 思考过程辅助函数 ---- */

/**
 * 格式化工具参数为可读字符串
 * - 对象类型：JSON.stringify 并限制长度
 * - 字符串类型：直接返回（截断）
 */
function formatToolArgs(args) {
	if (args == null) return '';
	if (typeof args === 'string') return args.length > 200 ? args.slice(0, 200) + '...' : args;
	try {
		const json = JSON.stringify(args, null, 2);
		return json.length > 500 ? json.slice(0, 500) + '\n...' : json;
	} catch (e) {
		return String(args);
	}
}

/**
 * 更新思考区摘要文本
 * @param {Element} summaryEl - .thinking-summary 元素
 * @param {number} toolCount - 工具调用总次数
 * @param {number|null} totalMs - 总耗时（ms），null 表示进行中
 */
function updateThinkingSummary(summaryEl, toolCount, totalMs) {
	if (!summaryEl) return;
	let text = toolCount + ' 次工具调用';
	if (totalMs != null) {
		text += ' · 总计 ' + (totalMs / 1000).toFixed(2) + 's';
	} else {
		text += ' · 执行中...';
	}
	summaryEl.textContent = text;
}

/**
 * 折叠/展开思考区（由 .thinking-header 点击触发）
 */
function toggleThinkingArea(headerEl) {
	const area = headerEl.closest('.thinking-area');
	if (!area) return;
	area.classList.toggle('collapsed');
}

/**
 * 从 tool_traces 数组渲染完整思考区 HTML
 * 用于历史会话加载场景：done 事件返回的 tool_traces 或后端历史记录中存储的工具调用链
 * @param {Array} toolTraces - [{tool_name, tool_args, tool_result, result_ok, latency_ms}]
 * @returns {string} 思考区 HTML，空数组返回空字符串
 */
function buildThinkingAreaHtml(toolTraces) {
	if (!toolTraces || !toolTraces.length) return '';
	const cardsHtml = toolTraces.map((t, idx) => {
		return htmlFromTpl('tmpl-tool-call', (frag) => {
			frag.querySelector('.tool-call').dataset.callId = 'hist_' + idx;
			frag.querySelector('.tool-call-name').textContent = t.tool_name || 'unknown';
			const statusEl = frag.querySelector('.tool-call-status');
			const ok = t.result_ok !== false;
			statusEl.textContent = ok ? '成功' : '失败';
			statusEl.className = 'tool-call-status ' + (ok ? 'ok' : 'fail');
			if (t.latency_ms != null) {
				frag.querySelector('.tool-call-latency').textContent = (t.latency_ms / 1000).toFixed(2) + 's';
			}
			frag.querySelector('.tool-call-args').textContent = formatToolArgs(t.tool_args);
			const resultEl = frag.querySelector('.tool-call-result');
			resultEl.textContent = t.tool_result || '';
			resultEl.className = 'tool-call-result ' + (ok ? 'ok' : 'fail');
		});
	}).join('');
	// 思考区默认折叠（历史记录展示时不打扰阅读）
	return htmlFromTpl('tmpl-thinking-area', (frag) => {
		frag.querySelector('.thinking-summary').textContent = toolTraces.length + ' 次工具调用';
		frag.querySelector('.thinking-body').innerHTML = cardsHtml;
		const area = frag.querySelector('.thinking-area');
		if (area) area.classList.add('collapsed');
	});
}

/* 切换发送按钮状态：idle=发送，sending=转圈等待后端响应，stopping=可终止 */
function setSendButtonState(state) {
	const btn = $('#chatSendBtn');
	if (!btn) return;
	if (state === 'sending') {
		btn.innerHTML = '<span class="btn-spinner"></span>';
		btn.classList.add('sending');
		btn.classList.remove('stopping');
		btn.setAttribute('onclick', '');
		btn.disabled = true;
	} else if (state === 'stopping') {
		btn.textContent = '⏹ 终止';
		btn.classList.add('stopping');
		btn.classList.remove('sending');
		btn.disabled = false;
		btn.setAttribute('onclick', 'stopChat()');
	} else {
		btn.textContent = '发送 ↵';
		btn.classList.remove('stopping', 'sending');
		btn.disabled = false;
		btn.setAttribute('onclick', 'sendChat()');
	}
}

/* 用户主动终止流式生成 */
function stopChat() {
	if (currentAbortController) {
		userAborted = true;
		currentAbortController.abort();
	}
}

async function sendChat() {
	if (isSending) return;
	isSending = true;

	const inp = $('#chatInput');
	const text = inp.value.trim();
	if (!text) { isSending = false; return; }

	let msgs = $('#chatMessages .msg-wrap');
	if (!msgs) {
		msgs = document.createElement('div');
		msgs.className = 'msg-wrap';
		$('#chatMessages').appendChild(msgs);
	}
	const emptyState = msgs.querySelector('.empty-state');
	if (emptyState) emptyState.remove();

	const now = new Date();
	const time = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0');
	const uMsg = renderUserMessageElement(text, time);
	msgs.appendChild(uMsg);
	inp.value = '';
	sessionStorage.removeItem(DRAFT_KEY);
	scrollChatBottom();

	const mid = 'm' + Date.now();
	const aMsg = document.createElement('div');
	aMsg.className = 'msg msg-ai';
	aMsg.innerHTML = tpl('tmpl-ai-thinking').innerHTML;
	msgs.appendChild(aMsg);
	scrollChatBottom();

	// 流式状态
	let answerText = '';           // 完整 answer 文本（后端已到达的全部 delta 合并）
	let displayText = '';          // 已展示到前端的文本（打字机效果用，<= answerText）
	let ttfbMs = 0;
	let totalMs = 0;
	let messageId = null;
	let citations = [];
	let answerContentEl = null;   // .msg-ai-content 容器
	let answerTextEl = null;      // .ai-answer-text 文本节点
	let thinkingAreaEl = null;    // .ai-thinking-area 思考过程区容器（Agent 模式）
	let thinkingBodyEl = null;    // .thinking-body 工具调用列表容器
	let thinkingSummaryEl = null; // .thinking-summary 摘要文本节点（工具数 + 总耗时）
	let toolCallEls = {};         // call_id -> tool-call DOM 元素映射，供 tool_result 回填
	let toolCallCount = 0;        // 工具调用总次数（用于摘要展示）
	let typingAnimTimer = null;   // 打字机逐字符补帧的 rAF / interval 引用

	/* ---- 打字机逐字符补帧渲染 ----
	 * 解决两个痛点：
	 *   1) 后端一次吐出大段 delta（段落式）：前端逐字显示，保持视觉连贯
	 *   2) 前端 rAF 节流合并导致的"跳跃式"：去掉节流直接用 16ms interval 逐字补
	 *
	 * displayText 逐步逼近 answerText，每次补 1~3 字符（16ms 约 60fps，视觉≈120 字/秒，
	 * 略快于真实模型 streaming 但用户不会感到卡顿或跳跃）。
	 */
	const TYPING_CHARS_PER_STEP = 3;
	const TYPING_INTERVAL_MS = 16;

	function startTypingAnimation() {
		if (typingAnimTimer) return;
		typingAnimTimer = setInterval(() => {
			if (displayText.length >= answerText.length) {
				// 已追上，停止
				clearInterval(typingAnimTimer);
				typingAnimTimer = null;
				return;
			}
			// 每次补 1~3 个字符，保持节奏
			const end = Math.min(answerText.length, displayText.length + TYPING_CHARS_PER_STEP);
			displayText = answerText.slice(0, end);
			if (answerTextEl) {
				answerTextEl.innerHTML = formatAnswer(displayText, citations);
			}
			scrollChatBottom();
		}, TYPING_INTERVAL_MS);
	}

	function stopTypingAnimation() {
		if (typingAnimTimer) {
			clearInterval(typingAnimTimer);
			typingAnimTimer = null;
		}
	}

	// 强制把 displayText 对齐到 answerText（done / 用户终止等收尾场景）
	function flushDisplayText() {
		stopTypingAnimation();
		displayText = answerText;
		if (answerTextEl) {
			answerTextEl.innerHTML = formatAnswer(displayText, citations);
			scrollChatBottom();
		}
	}

	// 动态获取根类型（如果没有选中节点，由后端动态返回默认根类型）
	const rootTypes = [];

	const body = {
		question: text,
		root_types: rootTypes,
		node_ids: [...selectedScopeIds].map(Number),
		use_cache: true,
		do_task_split: false,
		mode: currentMode  // 问答模式：auto / rag / agent
	};
	if (currentSessionId) {
		body.session_id = currentSessionId;
	}

	// 声明在 try 外部，确保 catch/finally 可访问（否则块级作用域导致 ReferenceError）
	const abortController = new AbortController();
	currentAbortController = abortController;
	// 发送中按钮先显示转圈，等后端响应后再切换为"终止"
	setSendButtonState('sending');
	// 流式可能持续较久，放宽到 120s（超时自动中断）
	const timeoutId = setTimeout(() => abortController.abort(), 120000);

	try {
		await api.stream('/api/v1/chat/ask_stream/', body, (chunk) => {
			if (!chunk) return;
			// 兼容 streamer.py 外层兜底异常（无 type，仅 error/finish 字段）
			if (!chunk.type && chunk.error) {
				const target = answerContentEl || aMsg.querySelector('.msg-ai-content');
				if (target) {
					target.innerHTML = '<div style="color:var(--danger)">生成失败：' + escapeHtml(chunk.error) + '</div>';
				}
				return;
			}
			if (!chunk.type) return;
			switch (chunk.type) {
				case 'start': {
					// 后端已响应：按钮从转圈切换为"终止"
					setSendButtonState('stopping');
					// 切换"思考中"占位 → 回答骨架
					if (chunk.session_id) {
						currentSessionId = chunk.session_id;
						const chatTitle = $('#chatTitle');
						if (chatTitle && !chatTitle.dataset.hasTitle) {
							const title = text.slice(0, 30) + (text.length > 30 ? '...' : '');
							chatTitle.textContent = title;
							chatTitle.classList.remove('hidden');
							chatTitle.dataset.hasTitle = 'true';
							updateSessionTitle(chunk.session_id, title);
						}
					}
					citations = chunk.citations || [];

					answerContentEl = aMsg.querySelector('.msg-ai-content');
					answerContentEl.innerHTML = '';
					const answerFrag = tpl('tmpl-ai-answer').content.cloneNode(true);
					answerTextEl = answerFrag.querySelector('.ai-answer-text');
					// 答案区占位文本：避免 start→delta 间隔显示空白框
					// Agent 模式用更友好的"LLM 分析中..."占位，
					// RAG 模式用"检索并生成中..."占位，直到 first_token 才清空
					if (chunk.is_agent) {
						thinkingAreaEl = answerFrag.querySelector('.ai-thinking-area');
						answerTextEl.innerHTML = '<p style="color:var(--text-sub)">🧠 LLM 正在分析问题，必要时会调用工具... 请稍候</p>';
					} else {
						answerTextEl.innerHTML = '<p style="color:var(--text-sub)">🔎 正在检索知识库并生成答案，请稍候...</p>';
					}
					const sourceArea = answerFrag.querySelector('.ai-source-area');
					// Agent 模式下工具尚未执行完，先不渲染"基于模型知识"占位，避免闪烁
					const sourceHtml = buildSourceHtml(citations, chunk.route_source, undefined, !!chunk.is_agent);
					if (sourceHtml) sourceArea.innerHTML = sourceHtml;
					// message_id 此时未知，先用 0 占位，done 时回填
					answerFrag.querySelector('.feedback-good').setAttribute('onclick', "submitFeedback('" + mid + "', 0, 1)");
					answerFrag.querySelector('.feedback-bad').setAttribute('onclick', "submitFeedback('" + mid + "', 0, -1)");
					answerFrag.querySelector('.feedback-detail-btn').setAttribute('onclick', "toggleFeedbackDetail(this,'" + mid + "',0)");
					answerFrag.querySelector('.feedback-latency').textContent = '生成中...';
					answerFrag.querySelector('.feedback-detail').id = 'fbd-' + mid;
					answerContentEl.appendChild(answerFrag);
					scrollChatBottom();
					break;
				}
				case 'tool_call': {
					// Agent 工具调用开始：在思考区追加一张工具调用卡片
					// 首次 tool_call 时惰性初始化思考区骨架
					if (!thinkingAreaEl) break;
					if (!thinkingBodyEl) {
						const tFrag = tpl('tmpl-thinking-area').content.cloneNode(true);
						thinkingAreaEl.appendChild(tFrag);
						thinkingBodyEl = thinkingAreaEl.querySelector('.thinking-body');
						thinkingSummaryEl = thinkingAreaEl.querySelector('.thinking-summary');
					}
					toolCallCount++;
					const callId = chunk.call_id || ('call_' + toolCallCount);
					const toolName = chunk.tool_name || 'unknown';
					const toolArgs = chunk.tool_args || {};
					const cardEl = elemFromTpl('tmpl-tool-call', (frag) => {
						frag.querySelector('.tool-call').dataset.callId = callId;
						frag.querySelector('.tool-call-name').textContent = toolName;
						const statusEl = frag.querySelector('.tool-call-status');
						statusEl.textContent = '执行中...';
						statusEl.className = 'tool-call-status running';
						frag.querySelector('.tool-call-args').textContent = formatToolArgs(toolArgs);
					});
					// 新工具卡片插入思考区底部，保持调用顺序与 LLM 决策一致
					thinkingBodyEl.appendChild(cardEl);
					toolCallEls[callId] = cardEl;
					updateThinkingSummary(thinkingSummaryEl, toolCallCount, null);
					scrollChatBottom();
					break;
				}
				case 'tool_result': {
					// 工具执行完成：回填对应卡片的 status / latency / result
					const callId = chunk.call_id || ('call_' + toolCallCount);
					const cardEl = toolCallEls[callId];
					if (!cardEl) break;
					const statusEl = cardEl.querySelector('.tool-call-status');
					const ok = !!chunk.ok;
					statusEl.textContent = ok ? '成功' : '失败';
					statusEl.className = 'tool-call-status ' + (ok ? 'ok' : 'fail');
					const latency = chunk.latency_ms;
					if (latency != null) {
						cardEl.querySelector('.tool-call-latency').textContent = (latency / 1000).toFixed(2) + 's';
					}
					const resultEl = cardEl.querySelector('.tool-call-result');
					const preview = chunk.result_preview || '';
					resultEl.textContent = preview;
					resultEl.className = 'tool-call-result ' + (ok ? 'ok' : 'fail');
					updateThinkingSummary(thinkingSummaryEl, toolCallCount, null);
					scrollChatBottom();
					break;
				}
				case 'first_token': {
					ttfbMs = chunk.ttfb_ms || 0;
					// first_token 到达时：清空占位文本，重置 answerText/displayText 为空
					// （真正的文本会由紧随其后的 delta 逐字输出）
					answerText = '';
					displayText = '';
					if (answerTextEl) {
						answerTextEl.innerHTML = '';
					}
					if (answerContentEl) {
						const latencyEl = answerContentEl.querySelector('.feedback-latency');
						if (latencyEl) {
							latencyEl.textContent = '首字 ' + (ttfbMs / 1000).toFixed(2) + 's · 生成中...';
						}
					}
					break;
				}
				case 'delta': {
					// 合并到 answerText，打字机补帧动画自行显示（16ms 间隔逐字补）
					answerText += chunk.delta || '';
					startTypingAnimation();
					break;
				}
				case 'done': {
					messageId = chunk.message_id;
					totalMs = chunk.stats?.total_ms || 0;
					ttfbMs = chunk.stats?.ttfb_ms || ttfbMs;
					citations = chunk.citations || citations;
					// 工具调用链（text2sql/web_search 等来源卡片依赖它渲染）
					const doneToolTraces = chunk.tool_traces || [];

					// 命中审查拦截时跳过 flushDisplayText：content_filtered 事件已清空
					// answerText 并渲染拦截卡片，flush 会用 formatAnswer('') 覆盖卡片为"暂无回答"
					if (!chunk.is_filtered) {
						flushDisplayText();
					}

					if (answerContentEl) {
						// 刷新溯源区
						const sourceArea = answerContentEl.querySelector('.ai-source-area');
						if (sourceArea) {
							const sourceHtml = buildSourceHtml(citations, chunk.route_source, doneToolTraces);
							sourceArea.innerHTML = sourceHtml || '';
						}
						// 回填真实 message_id 到反馈按钮
						answerContentEl.querySelector('.feedback-good').setAttribute('onclick', "submitFeedback('" + mid + "', " + messageId + ", 1)");
						answerContentEl.querySelector('.feedback-bad').setAttribute('onclick', "submitFeedback('" + mid + "', " + messageId + ", -1)");
						answerContentEl.querySelector('.feedback-detail-btn').setAttribute('onclick', "toggleFeedbackDetail(this,'" + mid + "'," + messageId + ")");
						// 展示首字 + 总计耗时
						const latencyEl = answerContentEl.querySelector('.feedback-latency');
						if (latencyEl) {
							const ttfb = (ttfbMs / 1000).toFixed(2);
							const total = (totalMs / 1000).toFixed(2);
							latencyEl.textContent = '首字 ' + ttfb + 's · 总计 ' + total + 's';
						}
						// 内容审查拦截收尾：done 事件先于用户点击反馈按钮到达，
						// 此时 messageId 已就绪，启用"反馈误判"按钮并把 qa_id 写入 dataset
						// 命中 block 时已清空答案区，仅保留拦截卡片，无需再追写文本
						if (chunk.is_filtered) {
							const filteredCard = answerContentEl.querySelector('.content-filtered-card');
							if (filteredCard) {
								filteredCard.dataset.qaId = messageId;
								const fbtn = filteredCard.querySelector('.filtered-feedback-btn');
								if (fbtn) fbtn.disabled = false;
							}
						}
					}
					// 思考区摘要收尾：补全总耗时；若全流程无工具调用，则移除空思考区
					if (thinkingAreaEl) {
						if (toolCallCount === 0) {
							thinkingAreaEl.innerHTML = '';
						} else {
							updateThinkingSummary(thinkingSummaryEl, toolCallCount, totalMs);
						}
					}
					scrollChatBottom();
					// 增量更新会话列表（预览+时间+置顶），不重新请求后端
					if (currentSessionId) {
						updateSessionInCache(currentSessionId, text.slice(0, 50), text);
						// 同步更新会话详情缓存：追加本轮 QaRecord
						const newRecord = {
							id: messageId,
							question: text,
							answer: answerText,
							citations: citations,
							latency_total_ms: totalMs,
							latency_ttfb_ms: ttfbMs,
							created_at: new Date().toISOString(),
							tool_traces: doneToolTraces,
						};
						const cache = getSessionCache(currentSessionId);
						if (cache && cache.records) {
							cache.records.push(newRecord);
							setSessionCache(currentSessionId, cache.records, new Date().toISOString());
						}
					}
					break;
				}
				case 'content_filtered': {
					// 命中敏感词 block：立即停止打字机，清空已展示内容，显示拦截提示卡片
					// 不暴露具体命中词（避免二次传播违规内容），仅提示"违规已拦截"
					stopTypingAnimation();
					answerText = '';
					displayText = '';
					if (answerTextEl) {
						answerTextEl.innerHTML = '';
					}
					const target = answerContentEl || aMsg.querySelector('.msg-ai-content');
					if (target) {
						// 渲染拦截提示卡片：含说明 + 误判反馈按钮
						// 反馈按钮初始 disabled：content_filtered 事件先于 done 到达，
						// 此时 messageId（qa_id）尚未就绪，需等 done 事件回填后才能启用反馈
						const category = chunk.category || 'other';
						const reason = chunk.reason || '检测到违规内容，已拦截';
						const filteredCard = document.createElement('div');
						filteredCard.className = 'content-filtered-card';
						filteredCard.dataset.mid = mid;
						filteredCard.dataset.category = category;
						filteredCard.innerHTML =
							'<div class="filtered-icon">🚫</div>' +
							'<div class="filtered-body">' +
								'<div class="filtered-title">' + escapeHtml(reason) + '</div>' +
								'<div class="filtered-hint">本回答因包含违规内容被系统拦截。' +
									'如果您认为这是误判，请点击反馈，管理员会人工复核。</div>' +
							'</div>' +
							'<button class="btn btn-sm filtered-feedback-btn" disabled onclick="reportFilterFalsePositive(this)">💬 反馈误判</button>';
						// 替换答案区内容（保留 feedback-bar 等其他元素）
						const existingAnswer = target.querySelector('.ai-answer-text');
						if (existingAnswer) {
							existingAnswer.innerHTML = '';
							existingAnswer.appendChild(filteredCard);
						} else {
							target.appendChild(filteredCard);
						}
						// 隐藏溯源区（拦截时无引用）
						const sourceArea = target.querySelector('.ai-source-area');
						if (sourceArea) sourceArea.innerHTML = '';
						// 更新耗时标签
						const latencyEl = target.querySelector('.feedback-latency');
						if (latencyEl) {
							latencyEl.textContent = '已拦截' + (ttfbMs > 0 ? ' · 首字 ' + (ttfbMs / 1000).toFixed(2) + 's' : '');
						}
					}
					scrollChatBottom();
					break;
				}
				case 'error': {
					// 错误时立即停止打字机动画，避免对已脱离 DOM 的节点空转
					stopTypingAnimation();
					const detail = chunk.detail || '未知错误';
					const target = answerContentEl || aMsg.querySelector('.msg-ai-content');
					if (target) {
						target.innerHTML = '<div style="color:var(--danger)">生成失败：' + escapeHtml(detail) + '</div>';
					}
					break;
				}
			}
		}, { signal: abortController.signal });
	} catch (e) {
		// 区分：用户主动终止 vs 网络/超时异常
		if (userAborted) {
			// 用户主动终止：停止打字机动画，保留已生成的部分回答，标注"已终止"
			flushDisplayText();
			if (answerText && answerTextEl) {
				answerTextEl.innerHTML = formatAnswer(answerText, citations);
			}
			if (answerContentEl) {
				const latencyEl = answerContentEl.querySelector('.feedback-latency');
				if (latencyEl) {
					latencyEl.textContent = '已终止' + (ttfbMs > 0 ? ' · 首字 ' + (ttfbMs / 1000).toFixed(2) + 's' : '');
				}
			} else {
				// start 事件未到达就已终止：显示简短提示
				aMsg.querySelector('.msg-ai-content').innerHTML = '<div style="color:var(--text-sub)">已终止</div>';
			}
			scrollChatBottom();
		} else {
			console.error('stream chat failed:', e);
			// 异常时也停止打字机动画，避免空转
			stopTypingAnimation();
			const isTimeout = e.name === 'AbortError';
			const btn = document.createElement('button');
			btn.className = 'btn btn-sm btn-primary';
			btn.textContent = '🔄 重试发送';
			btn.dataset.retryText = text;
			btn.addEventListener('click', retrySendChat);
			const container = document.createElement('div');
			container.innerHTML = htmlFromTpl('tmpl-error-message', (frag) => {
				frag.querySelector('.error-text').textContent = isTimeout ? '请求超时，请稍后重试' : '发送失败：' + e.message;
				frag.querySelector('.error-hint').textContent = isTimeout ? '服务器响应时间过长，请检查网络或缩短提问内容' : '请检查网络连接或重试';
			});
			container.appendChild(btn);
			aMsg.querySelector('.msg-ai-content').innerHTML = '';
			aMsg.querySelector('.msg-ai-content').appendChild(container);
			scrollChatBottom();
		}
	} finally {
		// 保底重置：无论流式成功、异常还是中断，都必须释放发送锁，否则后续无法发送
		clearTimeout(timeoutId);
		currentAbortController = null;
		userAborted = false;
		isSending = false;
		setSendButtonState('idle');
	}
}

function retrySendChat(e) {
	const text = e.target.dataset.retryText;
	const inp = $('#chatInput');
	if (inp) {
		inp.value = text;
	}
	sendChat();
}

/* 把 [n] 引用标记渲染为来源上标：仅当 n 对应实际引用序号时才转换，
 * 避免误伤正文中普通出现的 [数字]（如年份、脚注）。输入须为已转义的 HTML 文本。 */
function renderCiteSup(text, citeIdx) {
	if (!citeIdx || citeIdx.size === 0) return text;
	return text.replace(/\[(\d+)\]/g, function (m, num) {
		return citeIdx.has(parseInt(num, 10)) ? '<sup class="cite-ref">[' + num + ']</sup>' : m;
	});
}

function formatAnswer(text, citations) {
	if (!text) return '<p>暂无回答</p>';

	// 引用序号集合：正文 [n] 上标只对实际存在的引用生效
	const citeIdx = new Set();
	(Array.isArray(citations) ? citations : []).forEach(c => {
		if (c && c.index) citeIdx.add(Number(c.index));
	});

	const lines = text.split('\n');
	const result = [];
	let inCodeBlock = false;
	let inList = false;
	let codeLang = '';

	for (let i = 0; i < lines.length; i++) {
		const line = lines[i];

		if (line.startsWith('```')) {
			if (!inCodeBlock) {
				codeLang = line.slice(3).trim().replace(/[^a-zA-Z0-9_-]/g, '');
				result.push('<pre><code class="' + codeLang + '">');
				inCodeBlock = true;
			} else {
				result.push('</code></pre>');
				inCodeBlock = false;
				codeLang = '';
			}
			continue;
		}

		if (inCodeBlock) {
			result.push(escapeHtml(line));
			continue;
		}

		if (line.startsWith('### ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push('<h5>' + renderCiteSup(escapeHtml(line.slice(4)), citeIdx) + '</h5>');
			continue;
		}

		if (line.startsWith('## ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push('<h4>' + renderCiteSup(escapeHtml(line.slice(3)), citeIdx) + '</h4>');
			continue;
		}

		if (line.startsWith('# ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push('<h3>' + renderCiteSup(escapeHtml(line.slice(2)), citeIdx) + '</h3>');
			continue;
		}

		if (line.startsWith('- ') || line.startsWith('* ') || line.match(/^\d+\./)) {
			if (!inList) { result.push('<ul>'); inList = true; }
			const content = line.replace(/^(- |\* |\d+\.\s*)/, '');
			result.push('<li>' + renderCiteSup(escapeHtml(content), citeIdx) + '</li>');
			continue;
		}

		if (inList) { result.push('</ul>'); inList = false; }

		if (line.startsWith('`') && line.endsWith('`')) {
			result.push('<p><code>' + escapeHtml(line.slice(1, -1)) + '</code></p>');
		} else if (line.trim()) {
			result.push('<p>' + renderCiteSup(escapeHtml(line), citeIdx) + '</p>');
		}
	}

	if (inList) result.push('</ul>');
	if (inCodeBlock) result.push('</code></pre>');

	return result.join('\n');
}

/* ---- 消息渲染工具函数 ---- */
function renderUserMessageElement(text, time) {
	return elemFromTpl('tmpl-user-msg', (frag) => {
		frag.querySelector('.msg-user-bubble').textContent = text;
		frag.querySelector('.msg-user-avatar').textContent = STATE.user.name.slice(0, 2);
		frag.querySelector('.msg-time').textContent = time;
	});
}

function renderUserMessageHTML(text, time) {
	return renderUserMessageElement(text, time).outerHTML;
}

function renderAIMessageHTML(answer, citations, messageId, stats, toolTraces) {
	return htmlFromTpl('tmpl-ai-msg', (frag) => {
		// 思考过程区（Agent 模式历史记录，默认折叠）
		const thinkingHtml = buildThinkingAreaHtml(toolTraces);
		if (thinkingHtml) {
			frag.querySelector('.ai-thinking-area').innerHTML = thinkingHtml;
		}
		frag.querySelector('.ai-answer-text').innerHTML = formatAnswer(answer || '', citations);
		const sourceHtml = buildSourceHtml(citations, undefined, toolTraces);
		if (sourceHtml) {
			frag.querySelector('.ai-source-area').innerHTML = sourceHtml;
		}
		frag.querySelector('.feedback-good').setAttribute('onclick', "submitFeedback('m" + messageId + "', " + messageId + ", 1)");
		frag.querySelector('.feedback-bad').setAttribute('onclick', "submitFeedback('m" + messageId + "', " + messageId + ", -1)");
		frag.querySelector('.feedback-detail-btn').setAttribute('onclick', "toggleFeedbackDetail(this,'m" + messageId + "'," + messageId + ")");
		frag.querySelector('.feedback-latency').textContent = formatLatencyText(stats);
		frag.querySelector('.feedback-detail').id = 'fbd-m' + messageId;
	});
}

/* 格式化耗时展示：有首字耗时则显示"首字 X · 总计 Y"，否则仅显示总计 */
function formatLatencyText(stats) {
	if (!stats) return '';
	const total = stats.total_ms || stats.latency_total_ms || 0;
	const ttfb = stats.ttfb_ms || stats.latency_ttfb_ms || 0;
	if (ttfb > 0) {
		return '首字 ' + (ttfb / 1000).toFixed(2) + 's · 总计 ' + (total / 1000).toFixed(2) + 's';
	}
	if (total > 0) {
		return '总计 ' + (total / 1000).toFixed(2) + 's';
	}
	return '';
}

/* ---- 反馈 ---- */
async function submitFeedback(mid, qaId, rating) {
	try {
		await api.postJson('/api/v1/chat/feedback/', {
			qa_record_id: qaId,
			rating: rating,
			tags: rating === 1 ? ['good'] : ['bad']
		});
		toast(rating === 1 ? '感谢反馈，已记录为满意' : '感谢反馈，将用于优化召回', 'success');

		const bar = document.querySelector('#fbd-' + mid)?.previousElementSibling;
		if (bar) {
			bar.querySelectorAll('.feedback-btn').forEach((b, i) => {
				if (i < 2) b.classList.remove('active', 'active-neg');
			});
			const btn = bar.querySelector(rating === 1 ? '.feedback-btn:nth-child(1)' : '.feedback-btn:nth-child(2)');
			if (btn) btn.classList.add(rating === 1 ? 'active' : 'active-neg');
		}
	} catch (e) {
		console.error('submit feedback failed:', e);
		toast('反馈提交失败', 'error');
	}
}

function toggleFeedbackDetail(btn, mid, qaId) {
	const box = $('#fbd-' + mid);
	if (!box.innerHTML) {
		box.innerHTML = htmlFromTpl('tmpl-feedback-form', (frag) => {
			frag.querySelector('.feedback-cancel-btn').setAttribute('onclick', "closeFeedback('" + mid + "')");
			frag.querySelector('.feedback-submit-btn').setAttribute('onclick', "submitDetailedFeedback('" + mid + "', " + qaId + ")");
		});
	}
	box.classList.toggle('show');
}

async function submitDetailedFeedback(mid, qaId) {
	const box = $('#fbd-' + mid);
	const textarea = box.querySelector('textarea');
	const comment = textarea?.value || '';

	if (!comment.trim()) {
		toast('请填写反馈内容', 'error');
		return;
	}

	try {
		await api.postJson('/api/v1/chat/feedback/', {
			qa_record_id: qaId,
			rating: 0,
			comment: comment,
			tags: ['detailed']
		});
		toast('详细反馈已提交，感谢您的建议', 'success');
		closeFeedback(mid);
	} catch (e) {
		toast('提交失败', 'error');
	}
}

function closeFeedback(mid) { $('#fbd-' + mid).classList.remove('show'); }

/* ---- 内容审查误判反馈 ----
 * content_filtered 事件命中后，用户可点击"反馈误判"提交申诉。
 * 流程：done 事件回填 qaId 到 .content-filtered-card[data-qa-id] →
 *      点击按钮 → 展开内联表单 → 提交到 /api/v1/chat/feedback/（tags 标记 false_positive）
 */
function reportFilterFalsePositive(btn) {
	// 找到所属拦截卡片（按钮在卡片内）
	const card = btn.closest('.content-filtered-card');
	if (!card) return;
	const qaId = card.dataset.qaId;
	// done 事件未到达或后端未落库时 qaId 为空，提示用户稍候
	if (!qaId) {
		toast('记录尚未就绪，请稍后重试', 'error');
		return;
	}
	const category = card.dataset.category || 'other';
	// 已展开过则不重复创建
	if (card.querySelector('.filtered-feedback-form')) return;

	const form = document.createElement('div');
	form.className = 'filtered-feedback-form';
	form.innerHTML =
		'<textarea class="filtered-feedback-text" placeholder="请简要说明为什么认为是误判（可选）..." rows="3"></textarea>' +
		'<div class="filtered-feedback-actions">' +
			'<button class="btn btn-sm filtered-cancel-btn" type="button">取消</button>' +
			'<button class="btn btn-sm btn-primary filtered-submit-btn" type="button">提交反馈</button>' +
		'</div>';
	card.appendChild(form);

	// 绑定事件（避免 inline onclick 字符串拼接 qaId 注入风险）
	form.querySelector('.filtered-cancel-btn').addEventListener('click', () => form.remove());
	form.querySelector('.filtered-submit-btn').addEventListener('click', () => {
		const comment = (form.querySelector('.filtered-feedback-text')?.value || '').trim();
		submitFilterFalsePositive(qaId, category, comment, card);
	});
}

/* 提交误判反馈到后端 FeedbackView
 * rating=0 表示中性（既非满意也非不满意），用 tags 标记为审查误判
 */
async function submitFilterFalsePositive(qaId, category, comment, card) {
	try {
		await api.postJson('/api/v1/chat/feedback/', {
			qa_record_id: qaId,
			rating: 0,
			comment: comment || '内容审查误判反馈',
			tags: ['false_positive', 'filter_' + category]
		});
		toast('反馈已提交，管理员会人工复核', 'success');
		// 提交成功后移除表单和按钮，避免重复提交
		const form = card.querySelector('.filtered-feedback-form');
		if (form) form.remove();
		const btn = card.querySelector('.filtered-feedback-btn');
		if (btn) {
			btn.disabled = true;
			btn.textContent = '✓ 已反馈';
		}
	} catch (e) {
		console.error('submit filter false positive failed:', e);
		toast('反馈提交失败', 'error');
	}
}

/* ---- 会话详情 localStorage 缓存 ----
 * 进入页面时根据会话列表校验缓存（纯本地，零请求）：
 *   - last_active_at 一致 → 保留
 *   - last_active_at 不一致 → 删除（等用户查看时再请求）
 *   - 会话列表中已不存在 → 删除
 *   - 超出 20 条上限 → 按时间排序清理多余的
 * 切换会话时：有缓存先渲染，无缓存才请求后端。
 */

/* 读取单个会话缓存 */
function getSessionCache(sessionId) {
	try {
		const raw = localStorage.getItem(SESSION_CACHE_PREFIX + sessionId);
		if (!raw) return null;
		return JSON.parse(raw);
	} catch (e) { return null; }
}

/* 写入单个会话缓存（超 50KB 跳过） */
function setSessionCache(sessionId, records, lastActiveAt) {
	try {
		const data = { records, last_active_at: lastActiveAt };
		const raw = JSON.stringify(data);
		if (raw.length > MAX_SESSION_CACHE_SIZE) return;
		localStorage.setItem(SESSION_CACHE_PREFIX + sessionId, raw);
	} catch (e) { /* localStorage 满或不可用，静默降级 */ }
}

/* 删除单个会话缓存 */
function removeSessionCache(sessionId) {
	try { localStorage.removeItem(SESSION_CACHE_PREFIX + sessionId); } catch (e) { }
}

/* 校验和清理会话缓存：进入页面时调用，纯本地操作不请求后端 */
function cleanupSessionCache(sessionList) {
	// 收集所有缓存 key
	const cachedIds = [];
	for (let i = 0; i < localStorage.length; i++) {
		const key = localStorage.key(i);
		if (key && key.startsWith(SESSION_CACHE_PREFIX)) {
			cachedIds.push(key.slice(SESSION_CACHE_PREFIX.length));
		}
	}

	// 构建会话列表 lookup：id → last_active_at
	const sessionMap = {};
	for (const s of sessionList) {
		sessionMap[String(s.id)] = s.last_active_at || s.created_at;
	}

	// 校验每个缓存项
	const validCaches = [];  // [{ id, last_active_at }]
	for (const id of cachedIds) {
		const cache = getSessionCache(id);
		if (!cache) {
			removeSessionCache(id);
			continue;
		}
		// 会话列表中已不存在 → 删除
		if (!(id in sessionMap)) {
			removeSessionCache(id);
			continue;
		}
		// last_active_at 不一致 → 删除（等用户查看时再请求）
		if (cache.last_active_at !== sessionMap[id]) {
			removeSessionCache(id);
			continue;
		}
		validCaches.push({ id, last_active_at: cache.last_active_at });
	}

	// 按 last_active_at 降序排序，只保留最近 20 条
	validCaches.sort((a, b) => new Date(b.last_active_at) - new Date(a.last_active_at));
	const keepIds = new Set(validCaches.slice(0, MAX_CACHED_SESSIONS).map(c => c.id));
	for (const c of validCaches) {
		if (!keepIds.has(c.id)) {
			removeSessionCache(c.id);
		}
	}
}

/*
 * 统一的会话消息加载方法
 * - 缓存命中：直接渲染，零请求
 * - 缓存未命中：先显示"加载中"，请求后端后渲染并写入缓存
 * @param id - 会话 ID
 * @param options.skipToast - 初始加载时不显示 toast（默认 false）
 */
async function switchToSession(id, options = {}) {
	const msgs = $('#chatMessages');

	// 先尝试缓存命中
	const cache = getSessionCache(id);
	if (cache && cache.records) {
		if (msgs) {
			msgs.innerHTML = renderMessagesFromRecords(cache.records);
			scrollChatBottom();
		}
		if (!options.skipToast) toast('已切换会话', 'success');
		return;
	}

	// 缓存未命中：先显示加载中
	if (msgs) {
		msgs.innerHTML = '<div class="msg-wrap"><div class="empty-state" style="text-align:center;padding:60px 20px"><div class="spinner" style="margin:0 auto 12px"></div><div style="color:var(--text-sub)">加载会话记录中...</div></div></div>';
	}

	// 请求后端
	try {
		const data = await api.getJson('/api/v1/chat/sessions/' + id + '/qa/');
		const records = Array.isArray(data) ? data : (data.records || []);
		if (msgs) {
			msgs.innerHTML = renderMessagesFromRecords(records);
			scrollChatBottom();
		}
		// 写入缓存
		const session = sessionCache.find(s => s.id == id);
		setSessionCache(id, records, session ? (session.last_active_at || session.created_at) : null);
		if (!options.skipToast) toast('已切换会话', 'success');
	} catch (e) {
		console.error('load records failed:', e);
		if (msgs) msgs.innerHTML = renderEmptyState();
		if (!options.skipToast) toast('加载会话记录失败', 'error');
	}
}

async function updateSessionTitle(sessionId, title) {
	try {
		await api.patchJson('/api/v1/chat/sessions/' + sessionId + '/', { title: title });
	} catch (e) {
		console.error('update session title failed:', e);
	}
}

/* ---- 历史会话 ---- */
let currentSearchKeyword = '';
let searchDebounceTimer = null;
// 会话列表内存缓存：避免发送消息后重新拉取整个列表，改为增量更新
let sessionCache = [];

/* 从内存缓存渲染会话列表（不请求后端） */
function renderSessionList() {
	const el = $('#sessionList');
	if (!el) return;
	const sessions = sessionCache;

	const grouped = sessions.reduce((acc, s) => {
		const date = new Date(s.last_active_at || s.created_at);
		const now = new Date();
		const diff = now.getTime() - date.getTime();
		let group = '更早';
		if (diff < 24 * 60 * 60 * 1000) group = '今天';
		else if (diff < 48 * 60 * 60 * 1000) group = '昨天';
		else if (diff < 7 * 24 * 60 * 60 * 1000) group = '本周';
		if (!acc[group]) acc[group] = [];
		acc[group].push(s);
		return acc;
	}, {});

	el.innerHTML = Object.entries(grouped).map(([group, items]) => {
		return htmlFromTpl('tmpl-session-group', (frag) => {
			frag.querySelector('.session-group-title').textContent = group;
			const itemsWrap = frag.querySelector('.session-items-wrap');
			itemsWrap.innerHTML = items.map(s => {
				return htmlFromTpl('tmpl-session-item', (itemFrag) => {
					const item = itemFrag.querySelector('.session-item');
					item.dataset.id = s.id;
					if (s.id == currentSessionId) item.classList.add('active');
					item.setAttribute('onclick', 'switchSession(' + s.id + ',this)');
					const titleEl = itemFrag.querySelector('.session-title');
					titleEl.id = 'sessionTitle-' + s.id;
					titleEl.setAttribute('onblur', 'saveSessionTitle(' + s.id + ', this)');
					titleEl.setAttribute('onkeydown', "if(event.key==='Enter'){this.blur();event.preventDefault()}");
					titleEl.textContent = s.title;
					itemFrag.querySelector('.session-preview').textContent = s.preview || '';
					itemFrag.querySelector('.session-time').textContent = formatSessionTime(s.last_active_at || s.created_at);
					itemFrag.querySelector('.icon-edit').setAttribute('onclick', 'event.stopPropagation();editSessionTitle(' + s.id + ')');
					itemFrag.querySelector('.icon-del').setAttribute('onclick', 'event.stopPropagation();delSession(this,' + s.id + ')');
				});
			}).join('');
		});
	}).join('');
}

/* 增量更新会话缓存：发送消息后更新预览和时间，移到列表顶部，不请求后端
 * 如果是新会话（后端自动创建，缓存中不存在），则添加到列表顶部 */
function updateSessionInCache(sessionId, preview, questionText) {
	const idx = sessionCache.findIndex(s => s.id == sessionId);
	if (idx === -1) {
		// 新会话：构造最小 session 对象添加到列表顶部
		const now = new Date().toISOString();
		sessionCache.unshift({
			id: sessionId,
			title: questionText ? questionText.slice(0, 32) : '新会话',
			preview: preview || '',
			last_active_at: now,
			created_at: now,
			turn_count: 1,
			is_archived: false,
		});
		renderSessionList();
		return;
	}
	const s = sessionCache[idx];
	s.preview = preview;
	s.last_active_at = new Date().toISOString();
	// 移到列表顶部（最近活跃在前）
	sessionCache.splice(idx, 1);
	sessionCache.unshift(s);
	renderSessionList();
}

async function initSessionList(skipLoadMessages = false) {
	const el = $('#sessionList');
	if (!el) return;

	try {
		let url = '/api/v1/chat/sessions/';
		if (currentSearchKeyword) {
			url += '?search=' + encodeURIComponent(currentSearchKeyword);
		}
		const data = await api.getJson(url);
		sessionCache = data.results || data;

		// 以会话列表为准：有则选最近一条，无则留空（发送时后端自动创建）
		currentSessionId = sessionCache.length > 0 ? sessionCache[0].id : null;

		// 校验和清理会话详情缓存（纯本地操作，零请求）
		cleanupSessionCache(sessionCache);

		renderSessionList();

		if (currentSessionId) {
			const activeItem = el.querySelector('.session-item[data-id="' + currentSessionId + '"]');
			if (activeItem) {
				const titleEl = activeItem.querySelector('.session-title');
				const chatTitle = $('#chatTitle');
				if (chatTitle) {
					chatTitle.textContent = titleEl ? titleEl.textContent : '新会话';
					chatTitle.classList.remove('hidden');
				}
				if (!skipLoadMessages) {
					switchToSession(currentSessionId, { skipToast: true });
				}
			}
		}

	} catch (e) {
		console.error('load sessions failed:', e);
		el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-sub)">加载会话失败</div>';
	}
}

function searchSessions() {
	const input = $('#sessionSearchInput');
	if (!input) return;
	currentSearchKeyword = input.value.trim();
	initSessionList();
}

function debounceSearch() {
	if (searchDebounceTimer) clearTimeout(searchDebounceTimer);
	searchDebounceTimer = setTimeout(searchSessions, 300);
}

function editSessionTitle(id) {
	const titleEl = document.getElementById('sessionTitle-' + id);
	if (!titleEl) return;
	titleEl.contentEditable = true;
	titleEl.focus();
	// 选中所有文字
	try {
		const range = document.createRange();
		range.selectNodeContents(titleEl);
		const sel = window.getSelection();
		sel.removeAllRanges();
		sel.addRange(range);
	} catch (e) { }
}

async function saveSessionTitle(sessionId, el) {
	const newTitle = el.textContent.trim();
	if (!newTitle) {
		el.textContent = '未命名会话';
		el.contentEditable = false;
		return;
	}

	try {
		await api.patchJson('/api/v1/chat/sessions/' + sessionId + '/', { title: newTitle });
		toast('标题已更新', 'success');
	} catch (e) {
		console.error('save title failed:', e);
		toast('保存标题失败', 'error');
	}
	el.contentEditable = false;
}

function formatSessionTime(dt) {
	if (!dt) return '-';
	const d = new Date(dt);
	const now = new Date();
	const diff = now.getTime() - d.getTime();
	if (diff < 60 * 1000) return '刚刚';
	if (diff < 60 * 60 * 1000) return Math.floor(diff / (60 * 1000)) + ' 分钟前';
	if (diff < 24 * 60 * 60 * 1000) return Math.floor(diff / (60 * 60 * 1000)) + ' 小时前';
	return d.getMonth() + 1 + '/' + d.getDate();
}

async function switchSession(id, elm) {
	$$('.session-item').forEach(s => s.classList.remove('active'));
	elm.classList.add('active');
	currentSessionId = id;

	const chatTitle = $('#chatTitle');
	if (chatTitle) {
		const titleEl = elm.querySelector('.session-title');
		chatTitle.textContent = titleEl ? titleEl.textContent : '新会话';
		chatTitle.classList.remove('hidden');
		delete chatTitle.dataset.hasTitle;
	}

	// 统一调用 switchToSession：缓存命中零请求，未命中显示加载中再请求
	await switchToSession(id);
}

function renderMessagesFromRecords(records) {
	if (!records || records.length === 0) {
		return renderEmptyState();
	}
	const frag = tpl('tmpl-message-item').content.cloneNode(true);
	const wrap = frag.querySelector('.msg-wrap');
	wrap.innerHTML = records.map(r => {
		return renderUserMessageHTML(r.question, formatSessionTime(r.created_at)) +
			renderAIMessageHTML(r.answer, r.citations, r.id, {
				latency_total_ms: r.latency_total_ms,
				latency_ttfb_ms: r.latency_ttfb_ms
			}, r.tool_traces);
	}).join('');
	const wrapper = document.createElement('div');
	wrapper.appendChild(frag);
	return wrapper.innerHTML;
}

async function delSession(icon, id) {
	if (!confirm('确定删除此会话？删除后不可恢复。')) return;

	try {
		await api.deleteJson('/api/v1/chat/sessions/' + id + '/');
		// 同步清理内存缓存并重新渲染
		sessionCache = sessionCache.filter(s => s.id != id);
		renderSessionList();
		toast('会话已删除', 'success');
		if (currentSessionId == id) {
			currentSessionId = null;
			const msgs = $('#chatMessages');
			if (msgs) msgs.innerHTML = renderEmptyState();
		}
	} catch (e) {
		toast('删除失败: ' + e.message, 'error');
	}
}

async function newSession() {
	try {
		const data = await api.postJson('/api/v1/chat/sessions/', { title: '新会话' });
		currentSessionId = data.id;

		const msgs = $('#chatMessages');
		if (msgs) msgs.innerHTML = renderEmptyState();

		const chatTitle = $('#chatTitle');
		if (chatTitle) {
			chatTitle.textContent = '新会话';
			chatTitle.classList.remove('hidden');
			delete chatTitle.dataset.hasTitle;
		}

		initSessionList();
		toast('已创建新会话', 'success');
	} catch (e) {
		toast('创建会话失败', 'error');
	}
}

let scrollScheduled = false;
function scrollChatBottom() {
	if (scrollScheduled) return;
	scrollScheduled = true;
	requestAnimationFrame(() => {
		const m = $('#chatMessages');
		if (m) m.scrollTop = m.scrollHeight;
		scrollScheduled = false;
	});
}
