/* ============ 聊天页面 ============ */

/* ---- 知识库范围选择器状态 ---- */
const SCOPE_STORAGE_KEY = 'rag_chat_scope';
let scopeOpen = false;
let allScopeIds = [];
let selectedScopeIds = new Set();  // 统一存储字符串 ID，避免与 API 数字 ID 混淆
let scopeFlatList = [];  // 缓存扁平化节点，避免重复遍历
let currentSessionId = null;
let isSending = false;
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
	return `
    <div class="msg-wrap">
      <div class="empty-state">
        <div style="text-align:center;padding:60px 20px">
          <div style="font-size:48px;margin-bottom:16px">💬</div>
          <div style="font-size:18px;font-weight:600;margin-bottom:8px">欢迎使用智能聊天</div>
          <div style="color:var(--text-sub);font-size:14px">选择知识库范围，开始提问吧</div>
        </div>
      </div>
    </div>`;
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
		const cls = n.depth === 1 ? 'child' : (n.depth >= 2 ? 'grandchild' : '');
		const checked = selectedScopeIds.has(n.id) ? 'checked' : '';
		return `
      <label class="scope-item ${cls}">
        <input type="checkbox" ${checked} value="${n.id}" onchange="onScopeChange(this, '${n.id}')">
        <span class="scope-label">${escapeHtml(n.name)}</span>
      </label>
    `;
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
		badge.textContent = `已选 ${cnt}`;
	}
}

/* ---- 发送消息 ---- */
function handleChatKey(e) {
	if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
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
	const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
	const uMsg = renderUserMessageElement(text, time);
	msgs.appendChild(uMsg);
	inp.value = '';
	scrollChatBottom();

	const mid = 'm' + Date.now();
	const aMsg = document.createElement('div');
	aMsg.className = 'msg msg-ai';
	aMsg.innerHTML = `
    <div class="msg-ai-avatar">AI</div>
    <div class="msg-ai-content">
      <div style="display:flex;align-items:center;gap:8px;color:var(--text-sub)">
        <div class="spinner"></div>正在检索知识库并思考中...
      </div>
    </div>`;
	msgs.appendChild(aMsg);
	scrollChatBottom();

	try {
		const body = {
			question: text,
			session_id: currentSessionId || undefined,
			root_types: ['company_doc'],
			node_ids: [...selectedScopeIds].map(Number),
			use_cache: true,
			do_task_split: false
		};

		const abortController = new AbortController();
		const timeoutId = setTimeout(() => abortController.abort(), 60000);

		const data = await api.postJson('/api/v1/chat/ask/', body, { signal: abortController.signal });
		clearTimeout(timeoutId);

		if (data.session_id) {
			currentSessionId = data.session_id;
			localStorage.setItem('rag_current_session', currentSessionId);
			const chatTitle = $('#chatTitle');
			if (chatTitle && !chatTitle.dataset.hasTitle) {
				const title = text.slice(0, 30) + (text.length > 30 ? '...' : '');
				chatTitle.textContent = title;
				chatTitle.style.display = 'block';
				chatTitle.dataset.hasTitle = 'true';
				updateSessionTitle(data.session_id, title);
			}
		}

		const citations = data.citations || [];
		const sourceHtml = citations.length > 0 ? `
      <div class="source-block">
        <div class="source-header">📎 溯源来源 · ${citations.length} 条引用</div>
        <div class="source-list">
          ${citations.map(c => `
            <div class="source-card"><div class="source-card-title">${escapeHtml(c.doc_title || '文档')} <span class="source-score">${(c.score || 80).toFixed(0)}%</span></div></div>
          `).join('')}
        </div>
      </div>` : '';

		aMsg.querySelector('.msg-ai-content').innerHTML = `
      <div>${formatAnswer(data.answer || '')}</div>
      ${sourceHtml}
      <div class="feedback-bar">
        <button class="feedback-btn" onclick="submitFeedback('${mid}', ${data.message_id}, 1)">👍 满意</button>
        <button class="feedback-btn" onclick="submitFeedback('${mid}', ${data.message_id}, -1)">👎 不满意</button>
        <button class="feedback-btn" onclick="toggleFeedbackDetail(this,'${mid}',${data.message_id})">💬 详细反馈</button>
        <span style="flex:1"></span>
        <span style="font-size:11px;color:var(--text-sub)">生成耗时 ${((data.stats?.total_ms || 0) / 1000).toFixed(2)}s</span>
      </div>
      <div class="feedback-detail" id="fbd-${mid}"></div>`;
		scrollChatBottom();
		initSessionList(true);
		isSending = false;
	} catch (e) {
		console.error('send chat failed:', e);
		const isTimeout = e.name === 'AbortError';
		const btn = document.createElement('button');
		btn.className = 'btn btn-sm btn-primary';
		btn.textContent = '🔄 重试发送';
		btn.dataset.retryText = text;
		btn.addEventListener('click', retrySendChat);
		const container = document.createElement('div');
		container.innerHTML = `
      <div style="color:var(--danger)">${isTimeout ? '请求超时，请稍后重试' : '发送失败：' + e.message}</div>
      <div style="font-size:12px;color:var(--text-sub);margin-top:8px;margin-bottom:12px">${isTimeout ? '服务器响应时间过长，请检查网络或缩短提问内容' : '请检查网络连接或重试'}</div>`;
		container.appendChild(btn);
		aMsg.querySelector('.msg-ai-content').innerHTML = '';
		aMsg.querySelector('.msg-ai-content').appendChild(container);
		scrollChatBottom();
		isSending = false;
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
				result.push(`<pre><code class="${codeLang}">`);
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
			result.push(`<h5>${escapeHtml(line.slice(4))}</h5>`);
			continue;
		}

		if (line.startsWith('## ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push(`<h4>${escapeHtml(line.slice(3))}</h4>`);
			continue;
		}

		if (line.startsWith('# ')) {
			if (inList) { result.push('</ul>'); inList = false; }
			result.push(`<h3>${escapeHtml(line.slice(2))}</h3>`);
			continue;
		}

		if (line.startsWith('- ') || line.startsWith('* ') || line.match(/^\d+\./)) {
			if (!inList) { result.push('<ul>'); inList = true; }
			const content = line.replace(/^(- |\* |\d+\.\s*)/, '');
			result.push(`<li>${escapeHtml(content)}</li>`);
			continue;
		}

		if (inList) { result.push('</ul>'); inList = false; }

		if (line.startsWith('`') && line.endsWith('`')) {
			result.push(`<p><code>${escapeHtml(line.slice(1, -1))}</code></p>`);
		} else if (line.trim()) {
			result.push(`<p>${escapeHtml(line)}</p>`);
		}
	}

	if (inList) result.push('</ul>');
	if (inCodeBlock) result.push('</code></pre>');

	return result.join('\n');
}

/* ---- 消息渲染工具函数 ---- */
function renderUserMessageElement(text, time) {
	const uMsg = document.createElement('div');
	uMsg.className = 'msg msg-user';
	const avatarText = STATE.user.name.slice(0, 2);
	uMsg.innerHTML = `
    <div class="msg-user-content">
      <div class="msg-user-bubble">${escapeHtml(text)}</div>
    </div>
    <div class="msg-user-side">
      <div class="msg-user-avatar">${escapeHtml(avatarText)}</div>
      <div class="msg-time">${time}</div>
    </div>`;
	return uMsg;
}

function renderUserMessageHTML(text, time) {
	const avatarText = STATE.user.name.slice(0, 2);
	return `
    <div class="msg msg-user">
      <div class="msg-user-content">
        <div class="msg-user-bubble">${escapeHtml(text)}</div>
      </div>
      <div class="msg-user-side">
        <div class="msg-user-avatar">${escapeHtml(avatarText)}</div>
        <div class="msg-time">${time}</div>
      </div>
    </div>`;
}

function renderAIMessageHTML(answer, citations, messageId, stats) {
	const sourceHtml = citations && citations.length > 0 ? `
    <div class="source-block">
      <div class="source-header">📎 溯源来源 · ${citations.length} 条引用</div>
      <div class="source-list">
        ${citations.map(c => `
          <div class="source-card"><div class="source-card-title">${escapeHtml(c.doc_title || '文档')} ${c.score !== undefined ? `<span class="source-score">${c.score.toFixed(0)}%</span>` : ''}</div></div>
        `).join('')}
      </div>
    </div>` : '';

	const latency = stats?.total_ms || stats?.latency_total_ms || 0;

	return `
    <div class="msg msg-ai">
      <div class="msg-ai-avatar">AI</div>
      <div class="msg-ai-content">
        <div>${formatAnswer(answer || '')}</div>
        ${sourceHtml}
        <div class="feedback-bar">
          <button class="feedback-btn" onclick="submitFeedback('m${messageId}', ${messageId}, 1)">👍 满意</button>
          <button class="feedback-btn" onclick="submitFeedback('m${messageId}', ${messageId}, -1)">👎 不满意</button>
          <button class="feedback-btn" onclick="toggleFeedbackDetail(this,'m${messageId}',${messageId})">💬 详细反馈</button>
          <span style="flex:1"></span>
          <span style="font-size:11px;color:var(--text-sub)">耗时 ${(latency / 1000).toFixed(2)}s</span>
        </div>
        <div class="feedback-detail" id="fbd-m${messageId}"></div>
      </div>
    </div>`;
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
		box.innerHTML = `
      <textarea placeholder="请描述具体的问题或建议..."></textarea>
      <div style="text-align:right;margin-top:6px">
        <button class="btn btn-sm" onclick="closeFeedback('${mid}')">取消</button>
        <button class="btn btn-sm btn-primary" onclick="submitDetailedFeedback('${mid}', ${qaId})">提交反馈</button>
      </div>`;
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
		const data = await api.getJson(`/api/v1/chat/records/?session_id=${id}`);
		const records = data.records || [];

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
		await api.patchJson(`/api/v1/chat/sessions/${sessionId}/`, { title: title });
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
			url += `?search=${encodeURIComponent(currentSearchKeyword)}`;
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

		el.innerHTML = Object.entries(grouped).map(([group, items]) => `
      <div class="session-group-title">${group}</div>
      ${items.map(s => `
        <div class="session-item ${s.id == currentSessionId ? 'active' : ''}" onclick="switchSession(${s.id},this)" data-id="${s.id}">
          <div class="session-title" id="sessionTitle-${s.id}" onblur="saveSessionTitle(${s.id}, this)" onkeydown="if(event.key==='Enter'){this.blur();event.preventDefault()}" title="点击编辑标题">${escapeHtml(s.title)}</div>
          <div class="session-preview">${escapeHtml(s.preview || '')}</div>
          <div class="session-time">${formatSessionTime(s.last_active_at || s.created_at)}</div>
          <div class="session-actions">
              <button class="session-icon-btn btn-edit" onclick="event.stopPropagation();editSessionTitle(${s.id})" title="编辑标题">✏️</button>
              <button class="session-icon-btn btn-del" onclick="event.stopPropagation();delSession(this,${s.id})" title="删除会话">✕</button>
            </div>
        </div>
      `).join('')}
    `).join('');

		if (currentSessionId) {
			const activeItem = el.querySelector(`.session-item[data-id="${currentSessionId}"]`);
			if (activeItem) {
				const titleEl = activeItem.querySelector('.session-title');
				const chatTitle = $('#chatTitle');
				if (chatTitle) {
					chatTitle.textContent = titleEl ? titleEl.textContent : '新会话';
					chatTitle.style.display = 'block';
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
	const titleEl = document.getElementById(`sessionTitle-${id}`);
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
		await api.patchJson(`/api/v1/chat/sessions/${sessionId}/`, { title: newTitle });
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
		chatTitle.style.display = 'block';
		delete chatTitle.dataset.hasTitle;
	}

	try {
		const data = await api.getJson(`/api/v1/chat/records/?session_id=${id}`);
		const records = data.records || [];

		const msgs = $('#chatMessages');
		if (msgs) {
			msgs.innerHTML = renderMessagesFromRecords(records);
			scrollChatBottom();
		}
		toast('已切换会话', '');
	} catch (e) {
		console.error('load records failed:', e);
		toast('加载会话记录失败', 'error');
	}
}

function renderMessagesFromRecords(records) {
	if (!records || records.length === 0) {
		return renderEmptyState();
	}
	return `
    <div class="msg-wrap">
      ${records.map(r => `
        ${renderUserMessageHTML(r.question, formatSessionTime(r.created_at))}
        ${renderAIMessageHTML(r.answer, r.citations, r.id, { latency_total_ms: r.latency_total_ms })}
      `).join('')}
    </div>`;
}

async function delSession(icon, id) {
	if (!confirm('确定删除此会话？删除后不可恢复。')) return;

	try {
		await api.deleteJson(`/api/v1/chat/sessions/${id}/`);
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
			chatTitle.style.display = 'block';
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