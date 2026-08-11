/* ==========================================================
   Wiki 知识库页面 (wiki.js)
   包含：列表浏览/搜索/过滤、详情查看（Markdown 渲染）、
   手动生成/刷新/标记过期
   依赖：common.js（STATE/$/toast/escapeHtml/formatDate）、api.js、layout.js
   ========================================================== */

const WIKI_API = '/api/v1/wiki';
const NODE_API = '/api/v1/knowledge/nodes';

let wikiPage = 1;         // 当前列表页码
let wikiDetailId = null;  // 当前详情页 ID
let generateNodes = [];   // 生成弹窗的扁平化节点列表
let selectedGenerateNodeId = null; // 生成弹窗选中的节点 ID

/* ============ 轻量级 Markdown 渲染（无外部依赖，防 XSS） ============ */
// 只允许 http/https/mailto 与站内相对路径，杜绝 javascript: 等危险协议
function safeLink(url) {
	if (!url) return '';
	if (/^(https?:\/\/|mailto:|\/|\.\/|\.\.\/|#)/i.test(url)) return url;
	return '';
}

// 行内格式化：链接 / 图片 / 粗体 / 斜体（入参已是 HTML 转义后的文本）
function mdInline(text) {
	return text
		.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (m, alt, url) => {
			const safeUrl = safeLink(url);
			return safeUrl
				? `<img src="${escapeHtml(safeUrl)}" alt="${escapeHtml(alt)}" loading="lazy">`
				: escapeHtml(m);
		})
		.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (m, text, url) => {
			const safeUrl = safeLink(url);
			return safeUrl
				? `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${mdInline(text)}</a>`
				: escapeHtml(m);
		})
		.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
		.replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
		.replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
		.replace(/_([^_\n]+)_/g, '<em>$1</em>');
}

// 块级解析：先整体转义，再处理代码块/列表/表格/引用等结构
function renderMarkdown(src) {
	if (!src) return '';
	const codeBlocks = [];
	let s = String(src);

	// 保护围栏代码块，内容原样展示（避免与后续行内规则冲突）
	s = s.replace(/```(\w*)[ \t]*\r?\n?([\s\S]*?)\r?\n?```/g, (m, lang, code) => {
		const idx = codeBlocks.length;
		codeBlocks.push(`<pre class="md-code"><code>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
		return `\u0000MDCODE${idx}\u0000`;
	});
	// 行内代码（单行，防跨行误匹配）
	s = s.replace(/`([^`\n]+)`/g, (m, code) => `<code class="md-inline-code">${escapeHtml(code)}</code>`);

	const lines = s.split('\n');
	let html = '';
	let paraBuf = [];      // 段落缓冲（未闭合的连续文本行）
	let listStack = [];    // 列表缩进栈 [{indent, tag}]
	let quoteBuf = [];     // 引用缓冲
	let tableRows = [];    // 表格缓冲

	const flushPara = () => {
		if (paraBuf.length) {
			html += `<p>${mdInline(paraBuf.join(' '))}</p>\n`;
			paraBuf = [];
		}
	};
	const closeLists = () => {
		while (listStack.length) html += `</${listStack.pop().tag}>\n`;
	};
	const flushQuote = () => {
		if (quoteBuf.length) {
			html += `<blockquote>${quoteBuf.join('<br>')}</blockquote>\n`;
			quoteBuf = [];
		}
	};
	const flushTable = () => {
		if (tableRows.length < 2) { tableRows = []; return; }
		const head = tableRows[0];
		let t = '<table><thead><tr>';
		head.forEach(c => { t += `<th>${mdInline(c)}</th>`; });
		t += '</tr></thead><tbody>';
		for (let i = 1; i < tableRows.length; i++) {
			t += '<tr>';
			tableRows[i].forEach(c => { t += `<td>${mdInline(c)}</td>`; });
			t += '</tr>';
		}
		html += t + '</tbody></table>\n';
		tableRows = [];
	};
	const flushAll = () => {
		flushPara(); closeLists(); flushQuote(); flushTable();
	};

	for (const raw of lines) {
		const line = raw.replace(/\s+$/, '');
		// 代码块占位符直接输出
		if (line.startsWith('\u0000MDCODE')) {
			flushAll();
			html += codeBlocks[parseInt(line.slice(9), 10)] + '\n';
			continue;
		}
		// 表格：以 | 开头的连续行组成表格块，分隔行（---）表示表头结束
		if (/^\s*\|/.test(line)) {
			const cells = line.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
			// 分隔行：单元格均为 :---: / --- 形式，仅作为表头结束标志
			if (cells.every(c => /^:?-{3,}:?$/.test(c))) {
				flushPara(); flushQuote();
				continue;
			}
			flushPara(); flushQuote(); closeLists();
			tableRows.push(cells);
			continue;
		}
		if (tableRows.length) { flushTable(); }

		const indent = (line.match(/^\s*/) || [''])[0].length;

		// 引用块
		if (/^\s*>\s?/.test(line)) {
			flushPara(); closeLists(); flushTable();
			quoteBuf.push(mdInline(escapeHtml(line.replace(/^\s*>\s?/, ''))));
			continue;
		}
		if (quoteBuf.length) flushQuote();

		// 标题
		const h = line.match(/^(#{1,6})\s+(.*)$/);
		if (h) {
			flushAll();
			html += `<h${h[1].length}>${mdInline(escapeHtml(h[2]))}</h${h[1].length}>\n`;
			continue;
		}

		// 分隔线
		if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
			flushAll();
			html += '<hr>\n';
			continue;
		}

		// 列表（支持缩进嵌套的有序/无序列表）
		const ol = line.match(/^(\s*)(\d+)\.\s+(.*)$/);
		const ul = line.match(/^(\s*)[-*+]\s+(.*)$/);
		if (ol || ul) {
			flushPara(); flushQuote(); flushTable();
			const tag = ol ? 'ol' : 'ul';
			const itemIndent = (ol ? ol[1] : ul[1]).length;
			const content = escapeHtml(ol ? ol[3] : ul[2]);
			// 缩进增大 → 嵌套列表；减小 → 回退到对应层级
			while (listStack.length && itemIndent < listStack[listStack.length - 1].indent) {
				html += `</${listStack.pop().tag}>\n`;
			}
			if (!listStack.length || itemIndent > listStack[listStack.length - 1].indent) {
				listStack.push({ indent: itemIndent, tag });
				html += `<${tag}>\n`;
			}
			html += `<li>${mdInline(content)}</li>\n`;
			continue;
		}
		if (listStack.length && !/^\s*$/.test(line)) {
			// 列表中间的非空行按列表项内容追加（宽松处理）
			flushPara(); closeLists();
		}

		// 空行：结束当前段落/列表
		if (!line.trim()) { flushAll(); continue; }

		// 普通段落行（合并为一段）
		closeLists();
		paraBuf.push(escapeHtml(line));
	}
	flushAll();

	return html;
}

/* ============ 状态标签 ============ */
function wikiStatusBadge(status) {
	const map = {
		published: { cls: 'wiki-status-published', label: '已发布' },
		expired: { cls: 'wiki-status-expired', label: '已过期' },
		draft: { cls: 'wiki-status-draft', label: '草稿' },
	};
	const s = map[status] || map.draft;
	return `<span class="wiki-status ${s.cls}">${s.label}</span>`;
}

/* ============ 列表 ============ */
async function loadWikiList(page) {
	wikiPage = page;
	const q = $('#wikiSearchInput').value.trim();
	const status = $('#wikiStatusFilter').value;
	const rootType = $('#wikiRootTypeFilter').value;

	const params = new URLSearchParams({ page });
	if (q) params.set('q', q);
	if (status) params.set('status', status);
	if (rootType) params.set('root_type', rootType);

	const box = $('#wikiListContainer');
	box.innerHTML = '<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">📚</div><div>加载中...</div></div>';

	try {
		const data = await api.getJson(`${WIKI_API}/pages/?${params.toString()}`);
		const count = data.count || 0;
		const totalPages = Math.max(1, Math.ceil(count / 20));
		renderWikiList(data.results || []);

		// 使用公共 Pagination 组件渲染分页
		const pgnState = { page: wikiPage, totalPages, total: count, pageSize: 20 };
		if (wikiPage > 1) {
			Pagination.update(pgnState);
		} else {
			Pagination.render({
				container: '#wikiPagination',
				...pgnState,
				align: 'center',
				onPageChange: (p) => loadWikiList(p)
			});
		}
	} catch (e) {
		box.innerHTML = `<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">😥</div><div>加载失败：${escapeHtml(e.message)}</div></div>`;
	}
}

function renderWikiList(rows) {
	const box = $('#wikiListContainer');
	if (!rows.length) {
		box.innerHTML = '<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">📄</div><div>暂无 Wiki 页面，点击右上角"生成 Wiki"创建</div></div>';
		return;
	}
	box.innerHTML = rows.map(r => `
    <div class="wiki-card" onclick="openWikiDetail(${r.id})">
      <div class="wiki-card-head">
        <span class="wiki-card-title">${escapeHtml(r.title)}</span>
        ${wikiStatusBadge(r.status)}
      </div>
      <div class="wiki-card-summary">${escapeHtml(r.summary || '暂无摘要')}</div>
      <div class="wiki-card-meta">
        ${r.node_path ? `<span class="wiki-node-path" title="${escapeHtml(r.node_path)}">📁 ${escapeHtml(r.node_name || r.node_path)}</span>` : ''}
        ${r.root_type ? `<span>🏷️ ${escapeHtml(r.root_type)}</span>` : ''}
        <span>👁️ ${r.view_count || 0}</span>
        <span>🕐 ${formatDate(r.updated_at)}</span>
      </div>
    </div>`).join('');
}

/* ============ 详情 ============ */
async function openWikiDetail(id) {
	const listView = $('#wikiListView');
	const detailView = $('#wikiDetailView');
	const body = $('#wikiDetailBody');
	listView.style.display = 'none';
	detailView.style.display = '';
	body.innerHTML = '<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">📚</div><div>加载中...</div></div>';

	try {
		const d = await api.getJson(`${WIKI_API}/pages/${id}/`);
		wikiDetailId = id;
		renderWikiDetail(d);
		window.scrollTo(0, 0);
	} catch (e) {
		body.innerHTML = `<div class="empty" style="padding:60px 0"><div class="empty-icon" style="font-size:48px">😥</div><div>加载失败：${escapeHtml(e.message)}</div></div>`;
	}
}

function showWikiList() {
	wikiDetailId = null;
	$('#wikiDetailView').style.display = 'none';
	$('#wikiListView').style.display = '';
	loadWikiList(wikiPage);
}

function renderWikiDetail(d) {
	const body = $('#wikiDetailBody');

	// 操作按钮：仅当前用户对该节点有管理权限时展示（can_manage 由后端判定）
	const actions = [];
	if (d.can_manage) {
		actions.push(`<button class="btn btn-sm" onclick="expireWiki(${d.id})">标记过期</button>`);
		actions.push(`<button class="btn btn-sm btn-primary" onclick="refreshWiki(${d.id})">🔄 刷新</button>`);
	}
	$('#wikiDetailActions').innerHTML = actions.join('');

	const tags = (d.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');
	const sections = (d.sections || []).map(s => `
    <div class="wiki-section">
      <div class="wiki-section-title">${escapeHtml(s.title)}</div>
      <div class="wiki-section-content">${escapeHtml(s.content)}</div>
    </div>`).join('');
	const outgoing = (d.outgoing_links || []).map(l => `
    <a class="wiki-link-item" onclick="openWikiDetail(${l.target_page_id})">→ ${escapeHtml(l.link_text || l.target_title)}</a>`).join('') || '<div class="text-sub">暂无</div>';
	const incoming = (d.incoming_links || []).map(l => `
    <a class="wiki-link-item" onclick="openWikiDetail(${l.target_page_id})">← ${escapeHtml(l.link_text || l.target_title)}</a>`).join('') || '<div class="text-sub">暂无</div>';

	body.innerHTML = `
    <div class="wiki-detail">
      <div class="wiki-detail-head">
        <span class="wiki-detail-title">${escapeHtml(d.title)}</span>
        ${wikiStatusBadge(d.status)}
      </div>
      <div class="wiki-detail-meta">
        ${d.node_path ? `<span title="${escapeHtml(d.node_path)}">📁 ${escapeHtml(d.node_name || d.node_path)}</span>` : ''}
        <span>👁️ 浏览 ${d.view_count || 0}</span>
        <span>🕐 更新于 ${formatDate(d.updated_at)}</span>
        ${d.root_type ? `<span>🏷️ ${escapeHtml(d.root_type)}</span>` : ''}
      </div>
      ${tags ? `<div class="wiki-detail-tags">${tags}</div>` : ''}
      ${d.summary ? `<div class="wiki-detail-summary">${escapeHtml(d.summary)}</div>` : ''}
      <hr class="wiki-detail-divider">
      <div class="wiki-md">${renderMarkdown(d.content)}</div>
      ${sections ? `<hr class="wiki-detail-divider"><div class="wiki-sections"><div class="wiki-sections-title">📑 结构化章节</div>${sections}</div>` : ''}
      <hr class="wiki-detail-divider">
      <div class="wiki-links">
        <div class="wiki-links-block">
          <div class="wiki-links-block-title">关联页面（本页指向）</div>
          ${outgoing}
        </div>
        <div class="wiki-links-block">
          <div class="wiki-links-block-title">被引用（其他页指向本页）</div>
          ${incoming}
        </div>
      </div>
    </div>`;
}

/* ============ 操作：刷新 / 标记过期 ============ */
async function refreshWiki(id) {
	try {
		const res = await api.postJson(`${WIKI_API}/pages/${id}/refresh/`, {});
		toast(res.detail || '刷新任务已提交', 'success');
		// 异步任务生成期间展示加载态，稍后自动刷新详情
		setTimeout(() => openWikiDetail(id), 3000);
	} catch (e) {
		toast('刷新失败：' + escapeHtml(e.message), 'error');
	}
}

async function expireWiki(id) {
	if (!confirm('确认将该 Wiki 页面标记为过期？过期后仍可浏览，建议随后刷新以重新生成。')) return;
	try {
		const res = await api.postJson(`${WIKI_API}/pages/${id}/expire/`, {});
		toast(res.detail || '已标记过期', 'success');
		openWikiDetail(id);
	} catch (e) {
		toast('操作失败：' + escapeHtml(e.message), 'error');
	}
}

/* ============ 生成弹窗 ============ */
async function openGenerateModal() {
	const list = $('#generateNodeList');
	const btn = $('#btnConfirmGenerate');
	list.innerHTML = '<div class="text-sub text-center" style="padding:16px">加载中...</div>';
	btn.disabled = true;
	selectedGenerateNodeId = null;
	$('#generateNodeSearch').value = '';
	showModal('generateModal');

	try {
		const res = await api.getJson(`${NODE_API}/tree/`);
		generateNodes = flattenNodes(res.tree || []);
		renderGenerateNodes();
	} catch (e) {
		list.innerHTML = `<div class="text-center text-danger" style="padding:16px">节点加载失败：${escapeHtml(e.message)}</div>`;
	}
}

// 将树形节点扁平化，保留层级缩进信息用于展示；
// doc_count 自底向上汇总为"该节点及全部子节点的文档总数"（子树文档量，一次遍历 O(n)）
function flattenNodes(nodes, depth = 0) {
	const acc = [];
	const byId = new Map();
	function walk(ns, d) {
		for (const n of ns) {
			const item = {
				id: n.id,
				parent_id: n.parent_id,
				name: n.name,
				path: n.path,
				root_type: n.root_type,
				node_level: n.node_level,
				depth: d,
				doc_count: n.document_count || 0,
			};
			byId.set(n.id, item);
			acc.push(item);
			if (n.children && n.children.length) walk(n.children, d + 1);
		}
	}
	walk(nodes, depth);
	// 深度优先先父后子，逆序即自底向上：子节点计数累加到父节点
	for (let i = acc.length - 1; i >= 0; i--) {
		const item = acc[i];
		if (item.parent_id && byId.has(item.parent_id)) {
			byId.get(item.parent_id).doc_count += item.doc_count;
		}
	}
	return acc;
}

function filterGenerateNodes() {
	const q = $('#generateNodeSearch').value.trim().toLowerCase();
	if (!q) { renderGenerateNodes(); return; }
	// 过滤出名称匹配的节点及其全部祖先（保证层级上下文可见）
	const matched = new Set(generateNodes.filter(n => n.name.toLowerCase().includes(q)).map(n => n.id));
	const ids = new Set();
	generateNodes.forEach(n => {
		if (matched.has(n.id)) {
			ids.add(n.id);
			// 依据 path 前缀找出祖先
			generateNodes.forEach(p => {
				if (p.path && n.path && n.path !== p.path && n.path.startsWith(p.path)) ids.add(p.id);
			});
		}
	});
	renderGenerateNodes([...ids]);
}

function renderGenerateNodes(onlyIds) {
	const list = $('#generateNodeList');
	const showAll = !onlyIds;
	const nodes = generateNodes.filter(n => showAll || onlyIds.includes(n.id));
	if (!nodes.length) {
		list.innerHTML = '<div class="text-sub text-center" style="padding:16px">无可选节点</div>';
		return;
	}
	list.innerHTML = nodes.map(n => `
    <div class="generate-node-item ${n.id === selectedGenerateNodeId ? 'selected' : ''}"
         data-id="${n.id}" onclick="selectGenerateNode(${n.id})">
      <span class="gni-name" style="padding-left:${n.depth * 18}px">${'▸ '.repeat(n.depth > 0 ? 1 : 0)}${escapeHtml(n.name)}</span>
      <span class="gni-docs">📄 ${n.doc_count} 篇文档</span>
    </div>`).join('');
}

function selectGenerateNode(id) {
	selectedGenerateNodeId = id;
	$('#btnConfirmGenerate').disabled = false;
	document.querySelectorAll('#generateNodeList .generate-node-item').forEach(el => {
		el.classList.toggle('selected', parseInt(el.getAttribute('data-id'), 10) === id);
	});
}

async function doGenerate() {
	if (!selectedGenerateNodeId) { toast('请先选择一个知识节点', 'warning'); return; }
	const btn = $('#btnConfirmGenerate');
	btn.disabled = true;
	try {
		const res = await api.postJson(`${WIKI_API}/pages/generate/`, { node_id: selectedGenerateNodeId });
		toast(res.detail || '生成任务已提交', 'success');
		closeGenerateModal();
		loadWikiList(1);
	} catch (e) {
		toast('生成失败：' + escapeHtml(e.message), 'error');
		btn.disabled = false;
	}
}

function closeGenerateModal() {
	closeModal('generateModal');
	selectedGenerateNodeId = null;
}

/* ============ 初始化 ============ */
async function loadRootTypeFilter() {
	const select = $('#wikiRootTypeFilter');
	try {
		const res = await api.getJson(`${NODE_API}/root_types/`);
		(res.root_types || []).forEach(t => {
			select.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(t.code)}">${escapeHtml(t.name)}</option>`);
		});
	} catch (e) {
		// 根类型加载失败不阻塞页面，仅无法按领域过滤
		console.error('load root types failed:', e);
	}
}

function initWikiPage() {
	// 生成按钮：无管理角色（纯查看者）隐藏，避免无权限操作
	if (!hasAnyRole('contributor', 'super_admin', 'kb_admin', 'dept_manager', 'team_leader')) {
		const btn = $('#btnGenerateWiki');
		if (btn) btn.style.display = 'none';
	}
	loadRootTypeFilter().then(() => loadWikiList(1));
}

document.addEventListener('DOMContentLoaded', initWikiPage);
