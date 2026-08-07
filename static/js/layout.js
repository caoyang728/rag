/* ==========================================================
   知库 Agent · 应用壳布局 (layout.js)
   包含：顶栏渲染、侧栏渲染、用户菜单、登出、通知、角色判断
   依赖：common.js（STATE/$/$$/toast/escapeHtml/formatDate/PAGE_MAP/goto）、api.js
   需在 common.js、api.js 之后，页面 js 之前加载
   仅带顶栏/侧栏的页面引入；login / reset-password 不引入
   ========================================================== */

/* ============ 布局：顶部导航 ============ */
function renderTopNav(active) {
	return `
  <nav class="topnav">
    <div class="topnav-logo">
      <div class="topnav-logo-icon">知</div>
      <span>知库 Agent</span>
    </div>
    <div id="scopeNavWrap" class="topnav-scope-wrap" style="display:none">
      <button class="topnav-scope-btn" id="scopeTrigger" onclick="toggleScopePicker()">
        📚 知识库范围 · <span id="scopeBadge">已全选</span> ▾
      </button>
    </div>
    <div class="topnav-right">
      <button class="topnav-icon-btn" title="通知" onclick="loadNotifications()">
        <span id="notificationSummary" style="font-size:12px;margin-right:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:80px"></span>🔔<span class="badge-dot"></span>
      </button>
      <div class="dropdown">
        <div class="topnav-user" onclick="toggleUserMenu(event)">
          <div class="avatar avatar-sm">${STATE.user.avatar}</div>
          <span class="topnav-user-name">${STATE.user.name}</span>
          <span style="font-size:10px;color:var(--text-sub)">▼</span>
        </div>
        <div id="userMenu" class="dropdown-menu">
          <div class="dropdown-item" onclick="goto('profile')">👤 我的资料</div>
          <div class="dropdown-divider"></div>
          <div class="dropdown-item" onclick="doLogout()">🚪 退出登录</div>
        </div>
      </div>
    </div>
  </nav>`;
}

/* ============ 用户菜单 / 登出 / 通知 ============ */
function toggleUserMenu(e) {
	e.stopPropagation();
	const m = $('#userMenu');
	if (m) m.classList.toggle('show');
}
// 点击页面其他位置关闭用户下拉菜单
document.addEventListener('click', () => { $$('.dropdown-menu.show').forEach(m => m.classList.remove('show')); });

function doLogout() {
	const refresh = localStorage.getItem('rag_refresh');
	const access = localStorage.getItem('rag_access');
	if (refresh) {
		const headers = { 'Content-Type': 'application/json' };
		if (access) headers['Authorization'] = 'Bearer ' + access;
		fetch('/api/v1/auth/logout/', {
			method: 'POST',
			headers: headers,
			body: JSON.stringify({ refresh })
		}).catch(() => { });
	}
	localStorage.removeItem('rag_access');
	localStorage.removeItem('rag_refresh');
	localStorage.removeItem('rag_user');
	toast('已退出登录', 'success');
	setTimeout(() => { window.location.href = '/login/'; }, 600);
}

async function loadNotifications() {
	try {
		const data = await api.getJson('/api/v1/notification/send-logs/');
		const logs = data.rows || [];

		if (logs.length === 0) {
			toast('暂无通知', '');
			return;
		}

		const latest = logs[0];
		const summary = latest.subject ? latest.subject.substring(0, 10) : '新通知';
		const summaryEl = $('#notificationSummary');
		if (summaryEl) {
			summaryEl.textContent = summary;
		}

		let msg = `📮 ${logs.length} 条通知\n`;
		logs.slice(0, 5).forEach(l => {
			msg += `• ${l.subject || '通知'} · ${formatDate(l.created_at)}\n`;
		});
		toast(msg, '');
	} catch (e) {
		console.error('load notifications failed:', e);
		toast('加载通知失败', 'error');
	}
}

/* ============ 布局：侧边导航（管理页） ============ */
function getUserRoles() {
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		return (u.roles || []).map(r => r.code);
	} catch (e) { return []; }
}

function hasAnyRole(...codes) {
	const userRoles = getUserRoles();
	return codes.some(c => userRoles.includes(c));
}

function isSuperAdmin() {
	return hasAnyRole('super_admin');
}

function isAdminOrOps() {
	// 可管理文档的角色：超级管理员 / 文档管理员
	return hasAnyRole('super_admin', 'kb_admin');
}

function isSystemMaintainer() {
	// 可查看/修改系统配置的角色：超级管理员 / 维护管理员
	return hasAnyRole('super_admin', 'system_maintainer');
}

function getSidebarGroups() {
	// 返回按业务场景分组的菜单（会话/知识库/账户/管理）
	// 侧边栏与首页功能入口共用，保证两处可见性一致
	// 非 contributor 且无管理角色 = viewer 只读准入，隐藏上传
	// 管理角色（team_leader/dept_manager/*_admin）即使 viewer 兜底也可操作上传
	const isReadonly = !hasAnyRole('contributor', 'super_admin', 'kb_admin', 'user_admin', 'dept_manager', 'team_leader');
	// 拥有管理权限的角色可见全部管理后台项
	// 包含：超级管理员 / 文档管理员 / 人员管理员 / 部门经理 / 团队组长
	const isManagerRole = hasAnyRole('super_admin', 'kb_admin', 'user_admin', 'dept_manager', 'team_leader');
	// 合规管理员：审计视角，仅可见"权限审批"（看全部工单，不参与审批）
	const isComplianceOnly = hasAnyRole('compliance_admin') && !isManagerRole;

	const adminItems = [];
	// 用户与角色、反馈与报表、审计与安全：仅管理角色可见
	if (isManagerRole) {
		adminItems.push(
			{ icon: '✅', name: '权限审批', page: 'admin-approvals', key: 'admin-approvals', desc: '权限配置变更审批与复核' },
			{ icon: '📄', name: '文档审核', page: 'admin-docs', key: 'admin-docs', desc: '文档发布双审与合规复核' },
			{ icon: '👥', name: '用户与角色', page: 'admin-users', key: 'admin-users', desc: '管理用户、角色与 RBAC 权限' },
		);
	} else if (isComplianceOnly) {
		// 合规管理员仅可见"权限审批"（审计视角，查看全部工单）
		adminItems.push(
			{ icon: '✅', name: '权限审批', page: 'admin-approvals', key: 'admin-approvals', desc: '权限配置变更审批与复核' },
		);
	}
	if (isManagerRole) {
		adminItems.push(
			{ icon: '📊', name: '反馈与报表', page: 'admin-analytics', key: 'admin-analytics', desc: '用户反馈收集与准确率分析' },
			{ icon: '🎯', name: '质量评估', page: 'admin-eval', key: 'admin-eval', desc: 'RAG 质量评估与回归分析' },
			{ icon: '🛡️', name: '审计与安全', page: 'admin-audit', key: 'admin-audit', desc: '操作审计日志与安全策略' },
		);
	}
	// 组织架构 & RBAC 权限配置：仅超级管理员和文档管理员可见
	if (isAdminOrOps()) {
		adminItems.push(
			{ icon: '🏢', name: '组织架构', page: 'admin-org', key: 'admin-org', desc: '部门与团队结构管理' },
			{ icon: '&#9881;&#65039;', name: 'RBAC 权限配置', page: 'admin-rbac', key: 'admin-rbac', desc: '角色权限矩阵配置' },
		);
	}
	// 系统配置：超级管理员 / 维护管理员可见（运行期 KV 配置项管理）
	if (isSystemMaintainer()) {
		adminItems.push(
			{ icon: '🔧', name: '系统配置', page: 'admin-system-config', key: 'admin-system-config', desc: '系统运行参数与模型管理' },
			{ icon: '⏰', name: '定时任务', page: 'admin-scheduler', key: 'admin-scheduler', desc: 'Beat 调度时间与启停配置（需审批）' },
		);
	}

	// 按业务场景分组（去掉原"工作台/个人/管理后台"命名，仅影响命名与布局，各角色可见项不变）
	const kbItems = [];
	// 文档上传：只读角色隐藏；知识库：所有登录用户可见
	if (!isReadonly) kbItems.push({ icon: '📤', name: '文档上传', page: 'upload', key: 'upload', desc: '上传 PDF / Word / MD，自动解析与向量化' });
	kbItems.push({ icon: '🗂️', name: '知识库', page: 'admin-nodes', key: 'admin-nodes', desc: '知识库树形结构与文档维护' });
	// Wiki 知识库：所有登录用户可见（浏览权限由后端按来源节点对齐判定）
	kbItems.push({ icon: '📚', name: 'Wiki 知识库', page: 'wiki', key: 'wiki', desc: '浏览 LLM 自动生成的 Wiki 页面' });
	// 知识图谱：所有登录用户可见（实体可见性由后端按来源文档权限过滤）
	kbItems.push({ icon: '🕸️', name: '知识图谱', page: 'graph', key: 'graph', desc: '图谱可视化、实体检索与社区浏览' });

	const groups = [
		{ group: '会话', icon: '💬', items: [{ icon: '💬', name: '智能聊天', page: 'chat', key: 'chat', desc: '基于 RAG 的多轮问答，支持多知识库检索' }] },
		{ group: '知识库', icon: '🗂️', items: kbItems },
		{ group: '账户', icon: '👤', items: [{ icon: '👤', name: '个人资料', page: 'profile', key: 'profile', desc: '查看与维护个人账号信息' }] },
	];
	// 管理分组仅在存在可见项时渲染（权限判定逻辑与重构前一致）
	if (adminItems.length) groups.push({ group: '管理', icon: '⚙️', items: adminItems });

	return groups;
}

function renderSidebar(active) {
	// 复用 getSidebarGroups，确保侧边栏与首页功能入口的可见性一致
	const groups = getSidebarGroups();
	const collapsed = isSidebarCollapsed();
	return `
  <aside class="sidebar">
    <div class="sidebar-head">
      <button type="button" class="sidebar-collapse-btn" id="sidebarCollapseBtn"
        onclick="toggleSidebarCollapse()" title="${collapsed ? '展开侧栏' : '折叠侧栏'}">${collapsed ? '▶' : '◀'}</button>
    </div>
    <nav class="sidebar-nav">
      ${groups.map(g => `
        <div class="sidebar-group">
          <div class="sidebar-group-title">${g.group}</div>
          ${g.items.map(it => {
		if (it.page) {
			return `
              <a class="sidebar-item ${it.key === active ? 'active' : ''}" href="${PAGE_MAP[it.page]}" data-tip="${escapeHtml(it.name)}">
                <span class="sidebar-item-icon">${it.icon}</span>
                <span class="sidebar-item-text">${it.name}</span>
              </a>`;
		}
		return `
              <div class="sidebar-item sidebar-item-placeholder" style="cursor:not-allowed;opacity:0.5" title="功能预留，即将上线" data-tip="${escapeHtml(it.name)}">
                <span class="sidebar-item-icon">${it.icon}</span>
                <span class="sidebar-item-text">${it.name}</span>
                <span class="sidebar-item-badge" style="margin-left:auto;font-size:10px;color:var(--text-sub);padding:2px 8px;border:1px solid #e5e7eb;border-radius:10px">即将上线</span>
              </div>`;
	}).join('')}
        </div>
      `).join('')}
    </nav>
  </aside>`;
}

/* ============ 侧栏折叠 ============ */
function isSidebarCollapsed() {
	// 折叠状态持久化到 localStorage，所有带壳页面统一生效
	return localStorage.getItem('rag_sidebar_collapsed') === '1';
}

function toggleSidebarCollapse() {
	// 切换折叠：仅显示图标、宽度变窄，hover 显示悬浮提示；刷新后保持，各页一致生效
	const collapsed = !isSidebarCollapsed();
	localStorage.setItem('rag_sidebar_collapsed', collapsed ? '1' : '0');
	document.body.classList.toggle('sidebar-collapsed', collapsed);
	const btn = document.getElementById('sidebarCollapseBtn');
	if (btn) {
		btn.textContent = collapsed ? '▶' : '◀';
		btn.title = collapsed ? '展开侧栏' : '折叠侧栏';
	}
}

/* ============ 页面初始化：注入顶栏 + 侧栏 ============ */
// 与 common.js 的 DOMContentLoaded 分离：此处仅负责渲染带壳页面的顶栏/侧栏与全局搜索
// login / reset-password 不引入 layout.js，自然不会执行此段
document.addEventListener('DOMContentLoaded', () => {
	const pathPart = window.location.pathname.replace(/\/$/, '').split('/').pop() || '';
	const currentPage = pathPart || 'index';  // 首页 / 时 currentPage = 'index'

	const topnavEl = document.getElementById('topnav-container');
	if (topnavEl) topnavEl.innerHTML = renderTopNav(currentPage);

	const sidebarEl = document.getElementById('sidebar-container');
	if (sidebarEl) {
		sidebarEl.innerHTML = renderSidebar(currentPage);
		// 恢复侧栏折叠状态（刷新后保持）
		if (isSidebarCollapsed()) document.body.classList.add('sidebar-collapsed');
	}
});
