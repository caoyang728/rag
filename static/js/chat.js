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
let scopeOpen = false;
let allScopeIds = [];
let selectedScopeIds = new Set();  // 统一存储字符串 ID，避免与 API 数字 ID 混淆
let scopeFlatList = [];  // 缓存扁平化节点，避免重复遍历
let currentSessionId = null;
let isSending = false;
let currentAbortController = null;  // 当前流式请求的 AbortController，供 stopChat 中断
let userAborted = false;  // 标记是否用户主动终止（区分超时中断）
let heartbeatTimer = null;
let isOnline = true;
// 问答模式：auto（LLM 自主决定是否调用工具）/ rag（传统 RAG）/ agent（强制 Agent）
let currentMode = 'auto';

document.addEventListener('DOMContentLoaded', () => {
	initChatPage();
	initScopePicker();
	initSessionList();
	initModeSwitcher();
	const wrap = $('#scopeNavWrap');
	if (wrap) wrap.style.display = '';
	document.addEventListener('click', (e) => {
		if (scopeOpen && !e.target.closest('#scopeDropdown') && !e.target.closest('#scopeTrigger')) {
			closeScopePicker();
		}
	});
	startHeartbeat();
});

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

function startHeartbeat() {
	if (heartbeatTimer) clearInterval(heartbeatTimer);
	heartbeatTimer = setInterval(async () => {
		try {
			const res = await api.get('/healthz', { cache: 'no-cache' });
			if (res.ok) {
				if (!isOnline) {
					isOnline = true;
					toast('连接已恢复', 'success');
				}
			} else {
				handleHeartbeatFailure();
			}
		} catch (e) {
			handleHeartbeatFailure();
		}
	}, 30000);
}

function handleHeartbeatFailure() {
	if (isOnline) {
		isOnline = false;
		toast('连接已断开，正在尝试重新连接...', 'error');
	}
}

function stopHeartbeat() {
	if (heartbeatTimer) {
		clearInterval(heartbeatTimer);
		heartbeatTimer = null;
	}
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

async function initScopePicker() {
	const saved = localStorage.getItem(SCOPE_STORAGE_KEY);
	if (saved) {
		try { selectedScopeIds = new Set(JSON.parse(saved).map(String)); } catch (e) { selectedScopeIds = new Set(); }
	}

	try {
		const data = await api.getJson('/api/v1/knowledge/nodes/tree/');
		scopeTreeData = data.tree || [];
		// 扁平化并构建 ID->节点映射，同时收集父子关系
		scopeFlatList = flattenScopeNodes(scopeTreeData, 0);
		allScopeIds = scopeFlatList.map(n => String(n.id));

		if (!saved || selectedScopeIds.size === 0) {
			selectedScopeIds = new Set(allScopeIds);
			saveScopeState();
		}

		renderScopeList(scopeFlatList);
		updateScopeBadge();
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

/* ---- 构建溯源来源 HTML ---- */
function buildSourceHtml(citations) {
	if (!citations || citations.length === 0) return '';

	return htmlFromTpl('tmpl-source-block', (frag) => {
		frag.querySelector('.source-header').textContent = '📎 溯源来源 · ' + citations.length + ' 个文档';
		const list = frag.querySelector('.source-list');
		list.innerHTML = citations.map(c => {
			return htmlFromTpl('tmpl-source-card', (cardFrag) => {
				const titleEl = cardFrag.querySelector('.source-card-title');
				titleEl.innerHTML = escapeHtml(c.doc_title || '未知文档') + ' <span class="source-score">80%</span>';

				// 元信息（章节/页码/引用数）放到底部 meta 行，inline 排列节省纵向空间
				let meta = '';
				if (c.section) {
					meta += '<span class="source-card-section">章节: ' + escapeHtml(c.section) + '</span>';
				}
				if (c.page && Array.isArray(c.page)) {
					meta += '<span class="source-card-page">页码: P' + c.page.join(', P') + '</span>';
				}
				if (c.chunk_ids && c.chunk_ids.length > 0) {
					meta += '<span class="source-card-count">引用 ' + c.chunk_ids.length + ' 处</span>';
				}
				if (meta) {
					const metaEl = document.createElement('div');
					metaEl.className = 'source-card-meta';
					metaEl.innerHTML = meta;
					cardFrag.querySelector('.source-card').appendChild(metaEl);
				}
			});
		}).join('');
	});
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

/* 切换发送按钮状态：idle=发送，stopping=终止 */
function setSendButtonState(state) {
	const btn = $('#chatSendBtn');
	if (!btn) return;
	if (state === 'stopping') {
		btn.textContent = '⏹ 终止';
		btn.classList.add('stopping');
		btn.setAttribute('onclick', 'stopChat()');
	} else {
		btn.textContent = '发送 ↵';
		btn.classList.remove('stopping');
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
				answerTextEl.innerHTML = formatAnswer(displayText);
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
			answerTextEl.innerHTML = formatAnswer(displayText);
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
	// 发送中按钮切换为"终止"
	setSendButtonState('stopping');
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
					// 切换"思考中"占位 → 回答骨架
					if (chunk.session_id) {
						currentSessionId = chunk.session_id;
						localStorage.setItem('rag_current_session', currentSessionId);
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
					const sourceHtml = buildSourceHtml(citations);
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

					// 命中审查拦截时跳过 flushDisplayText：content_filtered 事件已清空
					// answerText 并渲染拦截卡片，flush 会用 formatAnswer('') 覆盖卡片为"暂无回答"
					if (!chunk.is_filtered) {
						flushDisplayText();
					}

					if (answerContentEl) {
						// 刷新溯源区
						const sourceArea = answerContentEl.querySelector('.ai-source-area');
						if (sourceArea) {
							const sourceHtml = buildSourceHtml(citations);
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
					initSessionList(true);
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
				answerTextEl.innerHTML = formatAnswer(answerText);
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

function formatAnswer(text) {
	if (!text) return '<p>暂无回答</p>';

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
			result.push('<h5>' + escapeHtml(line.slice(4)) + '</h5>');
			continue;
		}

		if (line.startsWith('## ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push('<h4>' + escapeHtml(line.slice(3)) + '</h4>');
			continue;
		}

		if (line.startsWith('# ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push('<h3>' + escapeHtml(line.slice(2)) + '</h3>');
			continue;
		}

		if (line.startsWith('- ') || line.startsWith('* ') || line.match(/^\d+\./)) {
			if (!inList) { result.push('<ul>'); inList = true; }
			const content = line.replace(/^(- |\* |\d+\.\s*)/, '');
			result.push('<li>' + escapeHtml(content) + '</li>');
			continue;
		}

		if (inList) { result.push('</ul>'); inList = false; }

		if (line.startsWith('`') && line.endsWith('`')) {
			result.push('<p><code>' + escapeHtml(line.slice(1, -1)) + '</code></p>');
		} else if (line.trim()) {
			result.push('<p>' + escapeHtml(line) + '</p>');
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
		frag.querySelector('.ai-answer-text').innerHTML = formatAnswer(answer || '');
		const sourceHtml = buildSourceHtml(citations);
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

async function loadSessionMessages(id) {
	try {
		const data = await api.getJson('/api/v1/chat/sessions/' + id + '/qa/');
		const records = Array.isArray(data) ? data : (data.records || []);

		const msgs = $('#chatMessages');
		if (msgs) {
			msgs.innerHTML = renderMessagesFromRecords(records);
			scrollChatBottom();
		}
	} catch (e) {
		console.error('load records failed:', e);
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

async function initSessionList(skipLoadMessages = false) {
	const el = $('#sessionList');
	if (!el) return;

	try {
		let url = '/api/v1/chat/sessions/';
		if (currentSearchKeyword) {
			url += '?search=' + encodeURIComponent(currentSearchKeyword);
		}
		const data = await api.getJson(url);
		const sessions = data.results || data;

		const currentSession = localStorage.getItem('rag_current_session');
		currentSessionId = currentSessionId || currentSession;

		if (!currentSessionId && sessions.length > 0) {
			currentSessionId = sessions[0].id;
			localStorage.setItem('rag_current_session', currentSessionId);
		}

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
					loadSessionMessages(currentSessionId);
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
	localStorage.setItem('rag_current_session', id);

	const chatTitle = $('#chatTitle');
	if (chatTitle) {
		const titleEl = elm.querySelector('.session-title');
		chatTitle.textContent = titleEl ? titleEl.textContent : '新会话';
		chatTitle.classList.remove('hidden');
		delete chatTitle.dataset.hasTitle;
	}

	try {
		const data = await api.getJson('/api/v1/chat/sessions/' + id + '/qa/');
		const records = Array.isArray(data) ? data : (data.records || []);

		const msgs = $('#chatMessages');
		if (msgs) {
			msgs.innerHTML = renderMessagesFromRecords(records);
			scrollChatBottom();
		}
		toast('已切换会话', 'success');
	} catch (e) {
		console.error('load records failed:', e);
		toast('加载会话记录失败', 'error');
	}
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
		icon.closest('.session-item').remove();
		toast('会话已删除', 'success');
		if (currentSessionId == id) {
			currentSessionId = null;
			localStorage.removeItem('rag_current_session');
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
		localStorage.setItem('rag_current_session', currentSessionId);

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
