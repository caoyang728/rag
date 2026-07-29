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

document.addEventListener('DOMContentLoaded', () => {
	initChatPage();
	initScopePicker();
	initSessionList();
	const wrap = $('#scopeNavWrap');
	if (wrap) wrap.style.display = '';
	document.addEventListener('click', (e) => {
		if (scopeOpen && !e.target.closest('#scopeDropdown') && !e.target.closest('#scopeTrigger')) {
			closeScopePicker();
		}
	});
	startHeartbeat();
});

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
	let answerText = '';
	let ttfbMs = 0;
	let totalMs = 0;
	let messageId = null;
	let citations = [];
	let answerContentEl = null;   // .msg-ai-content 容器
	let answerTextEl = null;      // .ai-answer-text 文本节点
	let renderPending = false;    // rAF 节流标志，避免高频 delta 卡顿

	// 用 rAF 节流 markdown 重渲染：delta 高频到达时只在每帧合并渲染一次
	const flushRender = () => {
		renderPending = false;
		if (answerTextEl) {
			answerTextEl.innerHTML = formatAnswer(answerText);
			scrollChatBottom();
		}
	};
	const scheduleRender = () => {
		if (!renderPending) {
			renderPending = true;
			requestAnimationFrame(flushRender);
		}
	};

	// 动态获取根类型（如果没有选中节点，由后端动态返回默认根类型）
	const rootTypes = [];

	const body = {
		question: text,
		root_types: rootTypes,
		node_ids: [...selectedScopeIds].map(Number),
		use_cache: true,
		do_task_split: false
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
				case 'first_token': {
					ttfbMs = chunk.ttfb_ms || 0;
					if (answerContentEl) {
						const latencyEl = answerContentEl.querySelector('.feedback-latency');
						if (latencyEl) {
							latencyEl.textContent = '首字 ' + (ttfbMs / 1000).toFixed(2) + 's · 生成中...';
						}
					}
					break;
				}
				case 'delta': {
					answerText += chunk.delta || '';
					scheduleRender();
					break;
				}
				case 'done': {
					messageId = chunk.message_id;
					totalMs = chunk.stats?.total_ms || 0;
					ttfbMs = chunk.stats?.ttfb_ms || ttfbMs;
					citations = chunk.citations || citations;

					// 最终再渲染一次（确保完整 markdown）
					if (answerTextEl) {
						answerTextEl.innerHTML = formatAnswer(answerText);
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
					}
					scrollChatBottom();
					initSessionList(true);
					break;
				}
				case 'error': {
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
			// 用户主动终止：保留已生成的部分回答，标注"已终止"
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

function renderAIMessageHTML(answer, citations, messageId, stats) {
	return htmlFromTpl('tmpl-ai-msg', (frag) => {
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
			});
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
