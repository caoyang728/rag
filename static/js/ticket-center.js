/* ==========================================================
   知库 Agent · 工单中心公共组件 (ticket-center.js)
   功能：跨页面统一管理工单列表与审批操作，任意页面一行调用即可复用。
   覆盖工单类型：配置工单 / 定时任务 / 模型工单（类型注册表可扩展）。

   核心机制：
   - TICKET_TYPES 注册表：每类工单注册 API、归类判断、卡片/详情渲染器，
     未来新增类型只需追加一项，容器零改动。
   - 数据获取：统一调用 /api/v1/system/tickets/（后端按类型/状态/创建人筛选），
     前端直接使用返回的 ticket_type 字段分类。
   - 视图：待我处理（跨类型聚合 pending+pending_review 且非本人创建）/
     全部工单（类型下拉 + 状态 tab 筛选）。
   - 详情：统一二级弹窗（showConfirmDialog，840px），按类型渲染内容。

   依赖：common.js（showModal/showConfirmDialog/toast/escapeHtml/formatDate）、
         api.js、layout.js（hasAnyRole）。
   用法：
     TicketCenter.open({ defaultView: 'todo', onChanged: fn });
     TicketCenter.close();
   ========================================================== */
(function (global) {
	'use strict';

	// 工单状态中文名 + 样式类映射（与后端 choices 对应）
	const STATUS_MAP = {
		pending: { label: '待审核', cls: 'tc-status-pending' },
		pending_review: { label: '待复核', cls: 'tc-status-first' },
		approved: { label: '已通过', cls: 'tc-status-approved' },
		rejected: { label: '已驳回', cls: 'tc-status-rejected' },
		withdrawn: { label: '已撤回', cls: 'tc-status-withdrawn' },
	};

	// 筛选 tab（合并视图切换与状态筛选为统一 tab，全部在一行展示）
	// view 为 'todo'/'approved'/'rejected'/'withdrawn'/'mine' 时，按对应条件过滤；
	// view 为 'all' 时，通过 _statusFilter 进一步按状态筛选
	const FILTER_TABS = [
		{ id: 'todo', label: '待我处理', view: 'todo' },
		{ id: 'approved', label: '已通过', view: 'approved' },
		{ id: 'rejected', label: '已驳回', view: 'rejected' },
		{ id: 'withdrawn', label: '已撤回', view: 'withdrawn' },
		{ id: 'all', label: '全部工单', view: 'all' },
		{ id: 'mine', label: '我的工单', view: 'mine' },
	];

	// 调度类配置 key 前缀（与后端 scheduler_registry.SCHEDULE_KEY_PREFIX 一致）。
	const SCHEDULE_PREFIX = 'SCHEDULE_';

	// ===== 类型注册表 =====
	// 每项：label 展示名 / api 创建接口 / classify 归类判断（返回是否属于本类）/
	//       renderCard 卡片摘要 / renderDetail 详情渲染
	const UNIFIED_API = '/api/v1/system/tickets/';
	const TICKET_TYPES = {
		config: {
			label: '配置工单',
			api: UNIFIED_API,
			classify: (t) => t.ticket_type === 'config',
			renderCard: renderConfigCard,
			renderDetail: renderConfigDetail,
		},
		schedule: {
			label: '定时任务',
			api: UNIFIED_API,
			classify: (t) => t.ticket_type === 'schedule',
			renderCard: renderScheduleCard,
			renderDetail: renderScheduleDetail,
		},
		model: {
			label: '模型工单',
			api: UNIFIED_API,
			classify: (t) => t.ticket_type === 'model',
			renderCard: renderModelCard,
			renderDetail: renderModelDetail,
		},
	};

	// ===== 组件内部状态 =====
	let _tickets = [];        // 合并后的全量工单（每项附加 _type 字段）
	let _filterTab = 'all';   // 当前筛选 tab id（对应 FILTER_TABS[].id）
	let _typeFilter = 'all';  // 类型筛选：all / config / schedule / model
	let _statusFilter = '';   // '全部工单'视图下的状态筛选（approved/rejected/withdrawn），空=全部
	let _searchQuery = '';    // 搜索关键词（匹配 id/创建人/名称/key）
	let _searchTotal = 0;     // 后端搜索结果总数（搜索时用于分页计算）
	let _page = 1;            // 当前页码
	let _loadVersion = 0;     // 请求版本号，防止竞态：旧请求返回时丢弃
	const _PAGE_SIZE = 10;    // 每页条数
	let _onChanged = null;    // 审批/驳回/撤回成功后的回调（页面刷新自身数据用）

	// ===== 弹窗 DOM 懒初始化 =====
	let _modalEl = null;

	function ensureModal() {
		if (_modalEl) return;
		_modalEl = document.createElement('div');
		_modalEl.className = 'modal';
		_modalEl.id = 'ticketCenterModal';
		_modalEl.style.width = '960px';
		_modalEl.style.height = '580px';
		_modalEl.style.maxWidth = '95vw';
		_modalEl.innerHTML = `
			<div class="modal-header">
				<div class="modal-title">工单中心</div>
				<button class="modal-close" onclick="TicketCenter.close()">&times;</button>
			</div>
			<div class="modal-body" style="padding:12px 20px;overflow:hidden;display:flex;flex-direction:column">
				<!-- 筛选工具栏：类型下拉 + 筛选 tab（含搜索框）+ 我的工单 -->
				<div class="tc-toolbar">
					<select class="tc-type-select" id="tcTypeSelect" onchange="TicketCenter.switchType(this.value)">
						<option value="all">全部类型</option>
						${Object.keys(TICKET_TYPES).map(k => `<option value="${k}">${TICKET_TYPES[k].label}</option>`).join('')}
					</select>
					<div class="tc-filter-tabs" id="tcFilterTabs"></div>
				</div>
				<div id="tcList" class="tc-list"></div>
			</div>
			<div class="modal-footer" style="height:52px">
				<div id="tcPagination" class="tc-pagination"></div>
			</div>`;
		document.body.appendChild(_modalEl);
	}

	/* ============ 打开工单中心 ============
	 * @param {Object} opts
	 *   - defaultView: 'todo' | 'all'    初始视图，默认 todo
	 *   - onChanged: Function            审批/驳回/撤回成功后回调（页面刷新自身数据）
	 */
	async function open(opts) {
		opts = opts || {};
		_filterTab = opts.defaultView === 'all' ? 'all' : 'todo';
		_typeFilter = 'all';
		_statusFilter = '';
		_searchQuery = '';
		_page = 1;
		_onChanged = opts.onChanged || null;
		ensureModal();
		// 重置类型下拉
		const typeEl = $('#tcTypeSelect');
		if (typeEl) typeEl.value = 'all';
		showModal('ticketCenterModal');
		await loadTickets();
	}

	/* ============ 关闭工单中心 ============ */
	function close() {
		closeModal('ticketCenterModal');
	}

	/* ============ 切换筛选 tab ============ */
	function switchFilterTab(tabId) {
		_filterTab = tabId;
		_statusFilter = '';
		_page = 1;
		$$('#tcFilterTabs .tc-filter-tab').forEach(btn =>
			btn.classList.toggle('active', btn.dataset.tab === tabId));
		loadTickets();
	}

	/* ============ 切换类型筛选（下拉框） ============ */
	function switchType(type) {
		_typeFilter = type || 'all';
		_page = 1;
		loadTickets();
	}

	/* ============ 根据当前 tab 构建后端筛选参数 ============ */
	function _buildFilterParams() {
		// tab → status/creator 参数映射
		if (_filterTab === 'mine') return 'creator=me';
		if (_filterTab === 'todo') return 'status=pending,pending_review';
		if (_filterTab === 'approved') return 'status=approved';
		if (_filterTab === 'rejected') return 'status=rejected';
		if (_filterTab === 'withdrawn') return 'status=withdrawn';
		return ''; // 'all' tab：无额外筛选
	}

	/* ============ 加载工单（统一工单 API，单次请求） ============ */
	async function loadTickets() {
		const body = $('#tcList');
		if (body) body.innerHTML = '<div class="tc-empty">加载中...</div>';
		const ver = ++_loadVersion;
		try {
			// 搜索参数
			const sp = _searchQuery ? `search=${encodeURIComponent(_searchQuery)}` : '';
			// 分页参数
			const pg = `page=${_page}&page_size=${_PAGE_SIZE * 5}`;
			// tab 筛选参数（status/creator）
			const fp = _buildFilterParams();
			// 类型筛选参数
			const tp = _typeFilter !== 'all' ? `ticket_type=${_typeFilter}` : '';
			const parts = [sp, pg, fp, tp].filter(Boolean);
			// 统一工单 API：后端已按类型/状态/创建人筛选，前端无需再合并
			const url = parts.length ? `/api/v1/system/tickets/?${parts.join('&')}` : '/api/v1/system/tickets/';
			const result = await api.getJson(url);
			// 旧请求返回时丢弃，避免竞态覆盖
			if (ver !== _loadVersion) return;
			_searchTotal = result.total || 0;
			// 后端已返回 ticket_type 字段，直接映射为 _type 供前端渲染使用
			_tickets = (result.tickets || []).map(t => ({ ...t, _type: t.ticket_type }));
			renderAll();
		} catch (e) {
			if (body) body.innerHTML = `<div class="tc-empty">加载失败：${escapeHtml(e.message)}</div>`;
		}
	}

	/* ============ 渲染总入口（tab + 列表 + 计数） ============ */
	function renderAll() {
		renderFilterTabs();
		renderList();
	}

	/* ============ 渲染筛选 tab（待我处理带红色计数） ============ */
	function renderFilterTabs() {
		const el = $('#tcFilterTabs');
		if (!el) return;
		// 待办计数：跨类型聚合待审核/待复核，且非本人创建、非本人已审核
		const me = getCurrentUsername();
		const todoCount = _tickets.filter(t =>
			(t.status === 'pending' || t.status === 'pending_review') && t.creator !== me && t.auditor !== me).length;
		const countHtml = (tab) => tab.id === 'todo' && todoCount > 0
			? `<span class="tc-todo-count">${todoCount}</span>` : '';
		// 左侧 tab 组（待我处理 + 已通过 + 已驳回 + 已撤回 + 全部工单）+ 搜索框 + 右侧固定"我的工单"
		const leftTabs = FILTER_TABS.filter(t => t.id !== 'mine').map(tab =>
			`<button class="tc-filter-tab${tab.id === _filterTab ? ' active' : ''}" data-tab="${tab.id}" onclick="TicketCenter.switchFilterTab('${tab.id}')">${tab.label}${countHtml(tab)}</button>`
		).join('');
		const mineTab = FILTER_TABS.find(t => t.id === 'mine');
		const mineHtml = `<button class="tc-filter-tab tc-filter-tab-mine${'mine' === _filterTab ? ' active' : ''}" data-tab="mine" onclick="TicketCenter.switchFilterTab('mine')">${mineTab.label}</button>`;
		const searchHtml = `<input class="tc-search" id="tcSearch" type="text" placeholder="搜索 ID / 创建人 / 名称" value="${escapeHtml(_searchQuery)}" oninput="TicketCenter.onSearch(this.value)" />`;
		el.innerHTML = `<div class="tc-filter-left">${leftTabs}</div>${searchHtml}${mineHtml}`;
	}

	/* ============ 当前筛选条件下的工单 ============ */
	function filteredTickets() {
		let list = _tickets;
		// 类型筛选：config/schedule 细分（后端已按 tab 的 status/creator 筛选）
		if (_typeFilter !== 'all') {
			list = list.filter(t => t._type === _typeFilter);
		}
		return list;
	}

	/* 搜索输入处理：防抖 500ms，触发后端搜索
	 * TODO: 搜索工单的比例极小且是管理员操作，当前直接实时查询。
	 *       如果查询量增大，改为回车触发搜索（移除 oninput，改用 onkeydown+Enter）。
	 *       后端 icontains 全表扫描的性能拐点约 5万~10万条（PostgreSQL LIKE '%xxx%' 无法走 B-tree），
	 *       此时可加 pg_trgm + GIN 索引延缓到 50万条；超过该量级应引入 ES/Meilisearch。
	 */
	let _searchTimer = null;
	function onSearch(val) {
		if (_searchTimer) clearTimeout(_searchTimer);
		_searchTimer = setTimeout(async () => {
			_searchQuery = (val || '').trim();
			_page = 1;
			await loadTickets();
		}, 500);
	}

	/* ============ 渲染列表（方案 C：无边框行式 + 分页） ============ */
	function renderList() {
		const body = $('#tcList');
		if (!body) return;
		const list = filteredTickets();
		// 始终用前端过滤后的实际条数（后端 total 未经前端 tab/类型过滤，直接用会导致分页不准）
		const total = list.length;
		if (list.length === 0) {
			const emptyMsg = _searchQuery
				? `<div class="tc-empty">未搜索到相关的工单</div>`
				: '<div class="tc-empty">暂无工单</div>';
			body.innerHTML = emptyMsg;
			const pag = $('#tcPagination');
			if (pag) pag.innerHTML = '';
			return;
		}
		const totalPages = Math.ceil(total / _PAGE_SIZE);
		if (_page > totalPages) _page = 1;
		const start = (_page - 1) * _PAGE_SIZE;
		const pageItems = list.slice(start, start + _PAGE_SIZE);
		body.innerHTML = pageItems.map(t => {
			const type = TICKET_TYPES[t._type];
			return type.renderCard(t);
		}).join('');
		// 分页 + 总数
		const pagination = $('#tcPagination');
		if (!pagination) return;
		pagination.innerHTML = `
			${totalPages > 1 ? `<button class="btn btn-sm btn-outline" ${_page <= 1 ? 'disabled' : ''} onclick="TicketCenter.goPage(${_page - 1})">上一页</button>` : ''}
			<span class="pagination-info">第 ${_page} / ${totalPages} 页（共 ${total} 条）</span>
			${totalPages > 1 ? `<button class="btn btn-sm btn-outline" ${_page >= totalPages ? 'disabled' : ''} onclick="TicketCenter.goPage(${_page + 1})">下一页</button>` : ''}`;
	}

	/* ============ 分页跳转 ============ */
	async function goPage(page) {
		_page = page;
		await loadTickets();
		const body = $('#tcList');
		if (body) body.scrollTop = 0;
	}

	/* ============ 通用工具：状态徽标 / 操作标签 ============ */
	function statusBadge(t) {
		const s = STATUS_MAP[t.status] || { label: t.status || '', cls: '' };
		return `<span class="tc-status ${s.cls}">${s.label}</span>`;
	}

	function typeBadge(t) {
		const cls = { config: 'tc-type-config', schedule: 'tc-type-schedule', model: 'tc-type-model' }[t._type] || 'tc-type-config';
		return `<span class="tc-type-badge ${cls}">${TICKET_TYPES[t._type].label}</span>`;
	}

	/** 操作动作标签：紧随类型徽标后显示"修改/删除/停用"，颜色区分操作类型 */
	function opLabel(t) {
		if (t._type === 'model') {
			const a = t.action || '';
			if (a === 'delete')    return '<span class="tc-op-tag tc-op-delete">删除</span>';
			if (a === 'deactivate') return '<span class="tc-op-tag tc-op-deactivate">停用</span>';
			return '<span class="tc-op-tag tc-op-modify">修改</span>';
		}
		return '<span class="tc-op-tag tc-op-modify">修改</span>';
	}

	// ===== 配置工单：卡片摘要 + 详情 =====

	// --- 操作摘要辅助函数 ---

	/**
	 * 配置工单操作摘要：配置项始终为修改操作
	 * 格式："修改了 [配置名] 参数"
	 */
	function configActionSummary(t) {
		return '修改了 ' + escapeHtml(t.config_label || t.config_key) + ' 参数';
	}

	/**
	 * 定时任务操作摘要：列出本次变更的具体内容
	 * cron 变更 → "修改了定时任务执行时间"；启停变更 → "修改了启停状态"；两者皆改则逗号拼接
	 */
	function scheduleActionSummary(t) {
		const oldP = parseScheduleValue(t.old_value);
		const newP = parseScheduleValue(t.new_value);
		if (!oldP || !newP) return '';
		const parts = [];
		if (oldP.cron !== newP.cron) parts.push('定时任务执行时间');
		if (oldP.enabled !== newP.enabled) parts.push('启停状态');
		if (!parts.length) return '';
		return parts.length === 1
			? '修改了' + parts[0]
			: '修改了' + parts[0] + '、' + parts[1];
	}

	/**
	 * 模型工单操作摘要：按操作类型差异化展示
	 * 修改 → "修改了 [字段1、字段2等N个参数]"
	 * 停用 → "停用了 [模型名]"
	 * 删除 → "删除了 [模型名]"
	 */
	function modelActionSummary(t) {
		const action = t.action || '';
		const snap = t.snapshot_data || {};
		const name = escapeHtml(snap.name || t.model_name || snap.model_name || '-');
		if (action === 'deactivate') return '停用了 ' + name;
		if (action === 'delete') return '删除了 ' + name;
		// 修改：字段列表，多字段时收起为"N个参数"
		if (t.changed_fields && t.changed_fields.length) {
			const fields = t.changed_fields.map(f => MODEL_FIELD_LABELS[f] || f);
			if (fields.length === 1) return '修改了' + escapeHtml(fields[0]);
			return '修改了' + escapeHtml(fields[0]) + '等' + fields.length + '个参数';
		}
		return '修改了模型配置';
	}

	// --- 卡片渲染 ---

	/**
	 * 配置工单卡片：标题行 = 类型徽标 + 配置名 + key + 高风险标记 + 操作摘要 + 状态
	 *             meta 行  = 申请原因（2行截断/hover展开5行）+ 创建人 + 时间
	 */
	function renderConfigCard(t) {
		return `
		<div class="tc-item" onclick="TicketCenter.openDetail('${t._type}', ${t.id})">
			<div class="tc-item-title">
				${typeBadge(t)}
				${opLabel(t)}
				<span class="tc-item-name">${escapeHtml(t.config_label || t.config_key)}</span>
				<span class="tc-item-key">${escapeHtml(t.config_key)}</span>
				${t.risk_level === 'high' ? '<span class="tc-item-risk">⚠️ 高风险</span>' : ''}
				${statusBadge(t)}
			</div>
			<div class="tc-item-meta">
				<div class="tc-meta-left">
					${t.reason ? `<div class="tc-meta-reason" title="${escapeHtml(t.reason)}">${escapeHtml(t.reason)}</div>` : ''}
					<span class="tc-action-label">${configActionSummary(t)}</span>
				</div>
				<div class="tc-meta-info">
					<span>创建人：${escapeHtml(t.creator || '-')}</span>
					<span>${formatDate(t.created_at)}</span>
				</div>
			</div>
		</div>`;
	}

	function renderConfigDetail(t) {
		const riskBadge = t.risk_level === 'high' ? '<span class="tc-item-key" style="color:#dc2626">⚠️ 高风险</span>' : '';
		return `
		<div class="tc-detail-card">
			<div class="tc-detail-header">
				${typeBadge(t)}
				<span class="tc-detail-title">${escapeHtml(t.config_label || t.config_key)}</span>
				<span class="tc-item-key">${escapeHtml(t.config_key)}</span>
				${riskBadge}
				${statusBadge(t)}
			</div>
			<div class="tc-detail-meta">
				<span>提交人：${escapeHtml(t.creator || '-')}</span>
				<span>提交时间：${formatDate(t.created_at)}</span>
			</div>
		</div>
		<div class="tc-detail-card">
			<div class="tc-diff-label">变更对比</div>
			<div class="tc-diff-row">
				<div class="tc-diff-side tc-diff-side-old">
					<div class="tc-diff-side-label">原值</div>
					<div class="tc-diff-side-value">${escapeHtml(t.old_value)}</div>
				</div>
				<div class="tc-diff-arrow">→</div>
				<div class="tc-diff-side tc-diff-side-new">
					<div class="tc-diff-side-label">新值</div>
					<div class="tc-diff-side-value">${escapeHtml(t.new_value)}</div>
				</div>
			</div>
			${renderChangeSummary(t.change_summary)}
		</div>
		${t.reason ? `<div class="tc-detail-card">
			<div class="tc-reason">
				<div class="tc-reason-label">变更原因</div>
				<div class="tc-reason-value">${escapeHtml(t.reason)}</div>
			</div>
		</div>` : ''}
		${renderTimeline(t)}
		`;
	}

	// ===== 定时任务：卡片摘要 + 详情（cron 变更用块状对比 + 中文解释） =====

	function parseScheduleValue(value) {
		try {
			const data = typeof value === 'string' ? JSON.parse(value) : value;
			return { cron: data.cron, enabled: !!data.enabled };
		} catch (e) {
			return null;
		}
	}

	/**
	 * 定时任务卡片：标题行 = 类型徽标 + 任务名 + key + 操作摘要 + 状态
	 *             meta 行  = 申请原因（2行截断/hover展开5行）+ 创建人 + 时间
	 */
	function renderScheduleCard(t) {
		return `
		<div class="tc-item" onclick="TicketCenter.openDetail('${t._type}', ${t.id})">
			<div class="tc-item-title">
				${typeBadge(t)}
				${opLabel(t)}
				<span class="tc-item-name">${escapeHtml(t.config_label || t.config_key)}</span>
				<span class="tc-item-key">${escapeHtml(t.config_key)}</span>
				${statusBadge(t)}
			</div>
			<div class="tc-item-meta">
				<div class="tc-meta-left">
					${t.reason ? `<div class="tc-meta-reason" title="${escapeHtml(t.reason)}">${escapeHtml(t.reason)}</div>` : ''}
					<span class="tc-action-label">${scheduleActionSummary(t)}</span>
				</div>
				<div class="tc-meta-info">
					<span>创建人：${escapeHtml(t.creator || '-')}</span>
					<span>${formatDate(t.created_at)}</span>
				</div>
			</div>
		</div>`;
	}

	function renderScheduleDetail(t) {
		const oldP = parseScheduleValue(t.old_value);
		const newP = parseScheduleValue(t.new_value);
		const summary = t.change_summary || {};
		// cron 中文解释：变更对比块下方各附 humanize 描述，审批人无需解析 cron 即可看懂改动
		const cronSummary = summary.schedule && summary.schedule.cron;
		let diffHtml = '';
		if (oldP && newP) {
			const rows = [];
			if (oldP.cron !== newP.cron) {
				rows.push(`
				<div class="tc-diff-row">
					<div class="tc-diff-side tc-diff-side-old">
						<div class="tc-diff-side-label">原 Cron</div>
						<div class="tc-diff-side-value">${escapeHtml(oldP.cron)}</div>
						${cronSummary && cronSummary.old_desc ? `<div class="tc-diff-side-hint">${escapeHtml(cronSummary.old_desc)}</div>` : ''}
					</div>
					<div class="tc-diff-arrow">→</div>
					<div class="tc-diff-side tc-diff-side-new">
						<div class="tc-diff-side-label">新 Cron</div>
						<div class="tc-diff-side-value">${escapeHtml(newP.cron)}</div>
						${cronSummary && cronSummary.new_desc ? `<div class="tc-diff-side-hint">${escapeHtml(cronSummary.new_desc)}</div>` : ''}
					</div>
				</div>`);
			}
			if (oldP.enabled !== newP.enabled) {
				rows.push(`
				<div class="tc-diff-row">
					<div class="tc-diff-side tc-diff-side-old">
						<div class="tc-diff-side-label">原状态</div>
						<div class="tc-diff-side-value">${oldP.enabled ? '启用' : '停用'}</div>
					</div>
					<div class="tc-diff-arrow">→</div>
					<div class="tc-diff-side tc-diff-side-new">
						<div class="tc-diff-side-label">新状态</div>
						<div class="tc-diff-side-value">${newP.enabled ? '启用' : '停用'}</div>
					</div>
				</div>`);
			}
			if (rows.length) diffHtml = rows.join('');
		}
		return `
		<div class="tc-detail-card">
			<div class="tc-detail-header">
				${typeBadge(t)}
				<span class="tc-detail-title">${escapeHtml(t.config_label || t.config_key)}</span>
				<span class="tc-item-key">${escapeHtml(t.config_key)}</span>
				${statusBadge(t)}
			</div>
			<div class="tc-detail-meta">
				<span>提交人：${escapeHtml(t.creator || '-')}</span>
				<span>提交时间：${formatDate(t.created_at)}</span>
			</div>
		</div>
		${diffHtml ? `<div class="tc-detail-card">
			<div class="tc-diff-label">调度变更</div>
			${diffHtml}
		</div>` : ''}
		${t.reason ? `<div class="tc-detail-card">
			<div class="tc-reason">
				<div class="tc-reason-label">变更原因</div>
				<div class="tc-reason-value">${escapeHtml(t.reason)}</div>
			</div>
		</div>` : ''}
		${renderTimeline(t)}
		`;
	}

	// ===== 模型工单：卡片摘要 + 详情（按 action 区分删除/停用/修改） =====

	const MODEL_ACTION_LABELS = { update_normal: '修改', update: '修改', deactivate: '停用', delete: '删除' };
	const MODEL_FIELD_LABELS = { base_url: 'Base URL', timeout: '超时时间', model_name: '模型名称', display_name: '显示名', api_key: 'API Key', name: '名称', model_type: '模型类型', is_active: '状态', provider: '服务商' };

	/**
	 * 模型工单卡片：标题行 = 类型徽标 + 模型名 + 操作标签（修改/删除/停用）+ 删除风险 + 操作摘要 + 状态
	 *             meta 行  = 申请原因（2行截断/hover展开5行）+ 创建人 + 时间
	 */
	function renderModelCard(t) {
		// 模型名：优先用 snapshot_data.name（名称），回退 model_name（模型名称）
		const snap = t.snapshot_data || {};
		const displayName = snap.name || t.model_name || snap.model_name || '-';
		return `
		<div class="tc-item" onclick="TicketCenter.openDetail('${t._type}', ${t.id})">
			<div class="tc-item-title">
				${typeBadge(t)}
				${opLabel(t)}
				<span class="tc-item-name">${escapeHtml(displayName)}</span>
				${t.action === 'delete' ? '<span class="tc-item-risk">⚠️ 高风险</span>' : ''}
				${statusBadge(t)}
			</div>
			<div class="tc-item-meta">
				<div class="tc-meta-left">
					${t.reason ? `<div class="tc-meta-reason" title="${escapeHtml(t.reason)}">${escapeHtml(t.reason)}</div>` : ''}
					<span class="tc-action-label">${modelActionSummary(t)}</span>
				</div>
				<div class="tc-meta-info">
					<span>创建人：${escapeHtml(t.creator || '-')}</span>
					<span>${formatDate(t.created_at)}</span>
				</div>
			</div>
		</div>`;
	}

	function renderModelDetail(t) {
		const actionLabel = MODEL_ACTION_LABELS[t.action] || t.action || '';
		const snapshot = t.snapshot_data || {};
		const riskNote = t.action === 'delete'
			? '<span class="tc-item-key" style="color:#dc2626">⚠️ 高风险</span>'
			: (t.action === 'deactivate' ? '<span class="tc-item-key">普通审批</span>' : '');
		let bodyHtml = '';
		if (t.action === 'delete') {
			// 删除：警示 + 模型当前信息列表（不做 diff）
			const fields = [
				{ key: 'name', label: '名称', value: snapshot.name },
				{ key: 'model_name', label: '模型名称', value: snapshot.model_name },
				{ key: 'model_type', label: '模型类型', value: snapshot.model_type },
				{ key: 'provider', label: '服务商', value: snapshot.provider },
				{ key: 'base_url', label: 'Base URL', value: snapshot.base_url },
				{ key: 'timeout', label: '超时时间', value: snapshot.timeout ? snapshot.timeout + 's' : '-' },
			].filter(f => f.value !== undefined && f.value !== null && f.value !== '');
			bodyHtml = `
			<div class="tc-warning tc-warning-danger">
				<div class="tc-warning-icon">🗑️</div>
				<div class="tc-warning-text"><strong>确认删除此模型？</strong>删除后该模型将不可恢复，且引用该模型的配置项将失效。</div>
			</div>
			<div class="tc-info-card">
				<div class="tc-info-title">模型当前信息</div>
				${fields.map(f => `<div class="tc-info-row"><span class="tc-info-label">${escapeHtml(f.label)}</span><span class="tc-info-value">${escapeHtml(String(f.value))}</span></div>`).join('')}
			</div>`;
		} else if (t.action === 'deactivate') {
			// 停用：警示 + 状态对照
			bodyHtml = `
			<div class="tc-warning tc-warning-warn">
				<div class="tc-warning-icon">⏸️</div>
				<div class="tc-warning-text"><strong>停用后该模型将不可用</strong>，引用该模型的配置项将受影响。</div>
			</div>
			<div class="tc-state-grid">
				<div class="tc-state-item tc-state-old">
					<div class="tc-state-label">当前状态</div>
					<div class="tc-state-value">● 启用中</div>
				</div>
				<div class="tc-state-item tc-state-new">
					<div class="tc-state-label">变更为</div>
					<div class="tc-state-value">● 已停用</div>
				</div>
			</div>`;
		} else if (t.changed_fields && t.changed_fields.length) {
			// 修改：字段级 diff 对比
			const rows = t.changed_fields.map(f => {
				const label = MODEL_FIELD_LABELS[f] || f;
				let oldV = '-', newV = '-';
				if (t.change_data && t.change_data[f]) {
					oldV = t.change_data[f].old ?? '-';
					newV = t.change_data[f].new ?? '-';
				}
				oldV = normalizeModelValue(oldV);
				newV = normalizeModelValue(newV);
				return `
				<div class="tc-diff-row">
					<div class="tc-diff-side tc-diff-side-old">
						<div class="tc-diff-side-label">${escapeHtml(label)} 原值</div>
						<div class="tc-diff-side-value">${escapeHtml(String(oldV))}</div>
					</div>
					<div class="tc-diff-arrow">→</div>
					<div class="tc-diff-side tc-diff-side-new">
						<div class="tc-diff-side-label">${escapeHtml(label)} 新值</div>
						<div class="tc-diff-side-value">${escapeHtml(String(newV))}</div>
					</div>
				</div>`;
			}).join('');
			bodyHtml = rows;
		}
		// 依赖引用警示（删除/停用受影响项）
		const depHtml = (t.dependency_refs && t.dependency_refs.length)
			? `<div class="tc-warning tc-warning-danger" style="margin-top:12px">
				<div class="tc-warning-icon">⚠️</div>
				<div class="tc-warning-text"><strong>依赖引用</strong>${escapeHtml(t.dependency_refs.join(', '))}</div>
			</div>` : '';
		return `
		<div class="tc-detail-card">
			<div class="tc-detail-header">
				${typeBadge(t)}
				<span class="tc-detail-title">${escapeHtml(t.model_name || '-')}</span>
				<span class="tc-item-key">${escapeHtml(actionLabel)}</span>
				${riskNote}
				${statusBadge(t)}
			</div>
			<div class="tc-detail-meta">
				<span>模型 ID：${escapeHtml(String(t.model_id ?? '-'))}</span>
				<span>提交人：${escapeHtml(t.creator || '-')}</span>
				<span>提交时间：${formatDate(t.created_at)}</span>
			</div>
		</div>
		${bodyHtml ? `<div class="tc-detail-card">
			<div class="tc-diff-label">${t.action === 'delete' ? '模型信息' : t.action === 'deactivate' ? '停用信息' : '变更详情'}</div>
			${bodyHtml}
		</div>` : ''}
		${depHtml}
		${t.reason ? `<div class="tc-detail-card">
			<div class="tc-reason">
				<div class="tc-reason-label">变更原因</div>
				<div class="tc-reason-value">${escapeHtml(t.reason)}</div>
			</div>
		</div>` : ''}
		${renderTimeline(t)}
		`;
	}

	// 模型值统一：布尔转中文，空值兜底
	function normalizeModelValue(v) {
		if (v === true) return '启用';
		if (v === false) return '停用';
		if (v === null || v === undefined) return '-';
		return v;
	}

	// ===== 多值类配置变更摘要（added 绿 / removed 红） =====
	function renderChangeSummary(summary) {
		if (!summary || (!summary.added && !summary.removed) ||
			(!summary.added?.length && !summary.removed?.length)) return '';
		const added = (summary.added || []).length
			? `<div class="tc-change-added">+ 新增：${summary.added.map(v => `<code>${escapeHtml(v)}</code>`).join(' ')}</div>` : '';
		const removed = (summary.removed || []).length
			? `<div class="tc-change-removed">- 移除：${summary.removed.map(v => `<code>${escapeHtml(v)}</code>`).join(' ')}</div>` : '';
		return `<div class="tc-change-summary">${added}${removed}</div>`;
	}

	/* ============ 审批时间线（方案 B） ============
	 * 根据工单状态把平铺字段（auditor/reviewer/applied_at 等）拼成
	 * 提交 → 审核 → 复核 → 生效 的流转时间线；驳回/撤回作为终止节点展示。
	 * 未进入对应阶段的节点不渲染，避免空节点占位噪声。
	 */
	function renderTimeline(t) {
		const nodes = [];
		// 提交节点：始终存在；待审核时标记为当前节点
		const isCurrentPending = t.status === 'pending';
		nodes.push(tlNode({
			dot: isCurrentPending ? 'current' : 'done',
			title: '提交工单',
			actor: t.creator,
			time: t.created_at,
		}));
		if (t.status === 'withdrawn') {
			// 撤回终止：无审批节点
			nodes.push(tlNode({ dot: 'withdrawn', title: '已撤回', actor: t.creator, time: t.created_at }));
		} else if (t.status === 'rejected') {
			// 驳回终止：展示驳回人（审核或复核阶段均可驳回）
			const reviewer = t.auditor || t.reviewer;
			const comment = t.audit_comment || t.review_comment;
			const time = t.audited_at || t.reviewed_at;
			nodes.push(tlNode({ dot: 'rejected', title: '已驳回', actor: reviewer, time, comment }));
		} else {
			// 审批流转中：按状态推进展示已完成的阶段
			if (t.status === 'pending_review' || t.status === 'approved') {
				nodes.push(tlNode({
					dot: 'done',
					title: '审核通过',
					actor: t.auditor,
					time: t.audited_at,
					comment: t.audit_comment,
				}));
			}
			if (t.status === 'pending_review') {
				// 待复核：当前节点，无具体审批人
				nodes.push(tlNode({ dot: 'current', title: '待复核', actor: '', time: '' }));
			}
			if (t.status === 'approved') {
				nodes.push(tlNode({
					dot: 'done',
					title: '复核通过',
					actor: t.reviewer,
					time: t.reviewed_at,
					comment: t.review_comment,
				}));
				if (t.applied_at) {
					nodes.push(tlNode({ dot: 'done', title: '已生效', actor: '', time: t.applied_at }));
				}
			}
		}
		return `<div class="tc-detail-card"><div class="tc-diff-label">流转进度</div><div class="tc-timeline">${nodes.join('')}</div></div>`;
	}

	// 拼装单个时间线节点 HTML
	// 有备注时展示备注内容，无备注（含"提交工单"等系统节点）显示"无备注"占位
	function tlNode({ dot, title, actor, time, comment }) {
		const actorHtml = actor ? `<span class="tc-tl-actor">${escapeHtml(actor)}</span>` : '';
		const timeHtml = time ? `<span class="tc-tl-time">${formatDate(time)}</span>` : '';
		const commentHtml = `<div class="tc-tl-comment">${comment ? escapeHtml(comment) : '无备注'}</div>`;
		return `
		<div class="tc-tl-item">
			<div class="tc-tl-dot tc-tl-dot-${dot}"></div>
			<div class="tc-tl-body">
				<div class="tc-tl-head"><span class="tc-tl-title">${escapeHtml(title)}</span>${actorHtml}${timeHtml}</div>
				${commentHtml}
			</div>
		</div>`;
	}

	/* ============ 打开详情二级弹窗（统一 840px） ============
	 * 按工单类型调用注册表里的 renderDetail，按钮逻辑统一：
	 * - 待审核：非创建人可 通过/驳回
	 * - 待复核：非创建人且超管可 复核通过/驳回
	 * - 创建人可撤回未完成的工单
	 */
	function openDetail(type, id) {
		const t = _tickets.find(x => x._type === type && x.id === id);
		if (!t) return;
		const me = getCurrentUsername();
		const isCreator = t.creator && t.creator === me;
		const isAuditor = t.auditor && t.auditor === me;
		const isSuperAdmin = hasAnyRole('super_admin');
		const isPending = t.status === 'pending' || t.status === 'pending_review';
		const canApprove = isPending && !isCreator;
		const canReview = t.status === 'pending_review' && isSuperAdmin && !isCreator && !isAuditor;

		const typeDef = TICKET_TYPES[type];
		const bodyHtml = typeDef.renderDetail(t);
		const buttons = [];
		if (t.status === 'pending' && canApprove) {
			buttons.push({ text: '✓ 通过', type: 'primary', onClick: (ctx) => { ctx.close(); approveTicket(t); } });
			buttons.push({ text: '✗ 驳回', type: 'danger', onClick: (ctx) => { ctx.close(); rejectTicket(t); } });
		} else if (canReview) {
			buttons.push({ text: '✓ 复核通过', type: 'primary', onClick: (ctx) => { ctx.close(); approveTicket(t); } });
			buttons.push({ text: '✗ 驳回', type: 'danger', onClick: (ctx) => { ctx.close(); rejectTicket(t); } });
		}
		// 创建人可撤回未完成工单
		if (isCreator && isPending) {
			buttons.push({ text: '↩ 撤回', className: 'btn btn-withdraw', onClick: (ctx) => { ctx.close(); withdrawTicket(t); } });
		}
		buttons.push({ text: '关闭', type: 'cancel', onClick: (ctx) => ctx.close() });

		showConfirmDialog({
			title: `${typeDef.label}详情 #${t.id}`,
			bodyHtml: bodyHtml,
			buttons: buttons,
			onShow: (ctx) => {
				ctx.el.style.width = '840px';
				ctx.el.style.maxWidth = '95vw';
			},
		});
	}

	/* ============ 审批通过 ============ */
	function approveTicket(t) {
		if (t.status === 'pending_review' && !hasAnyRole('super_admin')) {
			toast('复核仅超级管理员可操作', 'error');
			return;
		}
		const title = t.config_label || t.config_key || t.model_name || '-';
		const isReviewing = t.status === 'pending_review';
		showConfirmDialog({
			title: '审批通过',
			bannerType: 'success',
			bannerIcon: '✓',
			bannerText: isReviewing
				? `确认通过工单 #${t.id}（${title}）？复核通过后将立即生效。`
				: `确认通过工单 #${t.id}（${title}）？通过后${t._type === 'model' ? '操作将立即生效' : '配置将立即生效'}。`,
			bodyHtml: `<div class="form-item" style="margin-top:12px">
				<label class="form-label">审批意见（可选）</label>
				<textarea id="approveCommentInput" class="input" rows="2" placeholder="填写审批意见" style="max-width:100%"></textarea>
			</div>`,
			buttons: [
				{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
				{
					text: '确认通过',
					type: 'primary',
					onClick: async (ctx) => {
						try {
							const comment = ($('#approveCommentInput').value || '').trim();
							await api.postJson(`/api/v1/system/tickets/${t.id}/approve/`, { comment });
							ctx.close();
							toast('审批通过', 'success');
							await loadTickets();
							if (typeof _onChanged === 'function') _onChanged();
						} catch (e) {
							ctx.setError(`审批失败：${e.message}`);
						}
					}
				}
			]
		});
	}

	/* ============ 驳回 ============ */
	function rejectTicket(t) {
		if (t.status === 'pending_review' && !hasAnyRole('super_admin')) {
			toast('复核仅超级管理员可操作', 'error');
			return;
		}
		const title = t.config_label || t.config_key || t.model_name || '-';
		showConfirmDialog({
			title: '驳回工单',
			bannerType: 'danger',
			bannerIcon: '⚠',
			bannerText: `确认驳回工单 #${t.id}（${title}）？`,
			bodyHtml: `<div class="form-item" style="margin-top:12px">
				<label class="form-label">驳回原因 <span class="required">*</span></label>
				<textarea id="rejectCommentInput" class="input" rows="2" placeholder="请填写驳回原因" style="max-width:100%"></textarea>
			</div>`,
			buttons: [
				{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
				{
					text: '确认驳回',
					type: 'danger',
					onClick: async (ctx) => {
						const comment = ($('#rejectCommentInput').value || '').trim();
						if (!comment) {
							ctx.setError('请填写驳回原因');
							return;
						}
						try {
							await api.postJson(`/api/v1/system/tickets/${t.id}/reject/`, { comment });
							ctx.close();
							toast('已驳回', 'success');
							await loadTickets();
							if (typeof _onChanged === 'function') _onChanged();
						} catch (e) {
							ctx.setError(`驳回失败：${e.message}`);
						}
					}
				}
			]
		});
	}

	/* ============ 撤回（仅创建人） ============ */
	function withdrawTicket(t) {
		const title = t.config_label || t.config_key || t.model_name || '-';
		showConfirmDialog({
			title: '撤回工单',
			bannerType: 'info',
			bannerIcon: '↩',
			bannerText: `确认撤回工单 #${t.id}（${title}）？撤回后该工单将作废。`,
			bodyHtml: `<div class="form-item" style="margin-top:12px">
				<label class="form-label">撤回原因（可选）</label>
				<textarea id="withdrawCommentInput" class="input" rows="2" placeholder="填写撤回原因" style="max-width:100%"></textarea>
			</div>`,
			buttons: [
				{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
				{
					text: '确认撤回',
					type: 'primary',
					onClick: async (ctx) => {
						try {
							const comment = ($('#withdrawCommentInput').value || '').trim();
							await api.postJson(`/api/v1/system/tickets/${t.id}/withdraw/`, { comment });
							ctx.close();
							toast('已撤回', 'success');
							await loadTickets();
							if (typeof _onChanged === 'function') _onChanged();
						} catch (e) {
							ctx.setError(`撤回失败：${e.message}`);
						}
					}
				}
			]
		});
	}

	// ===== 当前用户工具（与原页面实现保持一致，组件自包含避免依赖页面全局函数） =====
	function getCurrentUsername() {
		try {
			const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
			return u.username || '';
		} catch (e) {
			return '';
		}
	}

	// 暴露公共 API（内部方法需全局调用，供内联 onclick 使用）
	global.TicketCenter = {
		open,
		close,
		switchFilterTab,
		switchType,
		goPage,
		openDetail,
		onSearch,
	};
})(window);
