/* ============ 节点管理（API 版） ============ */
const NODE_API = '/api/v1/knowledge/nodes';
let nodeTree = [];         // 树形结构
let selectedNodeId = null; // 当前选中的节点 ID

// 动态加载的根类型映射
let ROOT_TYPE_MAP = {};
let ROOT_ICON_MAP = {};

// 默认图标列表（按顺序分配给新根类型）
const DEFAULT_ROOT_ICONS = ['📁', '💻', '🧠', '🛠️', '📚', '🔧', '📋', '📊', '🔍', '⭐'];

/* ── 权限辅助 ── */
function isTeamLeader() {
	return hasAnyRole('team_leader');
}

function canManageNodes() {
	return isAdminOrOps() || isTeamLeader();
}

/** 获取团队组长可管理的团队节点 ID 列表（从 nodeTree 中查找 node_level=3 且 ref_id 匹配的节点） */
function getTeamLeaderTeamNodeIds() {
	if (!isTeamLeader()) return [];
	try {
		var u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		var teamIds = u.team ? [u.team.id] : [];
	} catch (e) { return []; }
	if (!teamIds.length) return [];
	var ids = [];
	function walk(nodes) {
		nodes.forEach(function (n) {
			if (n.node_level === 3 && teamIds.indexOf(n.ref_id) !== -1) {
				ids.push(n.id);
			}
			if (n.children) walk(n.children);
		});
	}
	walk(nodeTree);
	return ids;
}

/** 判断节点是否在团队组长的团队子树内 */
function isNodeInTeam(n, teamNodeIds) {
	if (!teamNodeIds.length) return false;
	for (var i = 0; i < teamNodeIds.length; i++) {
		if (n.path) {
			var found = findNodeById(teamNodeIds[i]);
			if (found && found.path && (n.path === found.path || n.path.indexOf(found.path) === 0)) {
				return true;
			}
		}
	}
	return false;
}

function findNodeById(id) {
	var result = null;
	function walk(nodes) {
		for (var i = 0; !result && i < nodes.length; i++) {
			if (nodes[i].id === id) { result = nodes[i]; return; }
			if (nodes[i].children) walk(nodes[i].children);
		}
	}
	walk(nodeTree);
	return result;
}


function closeModal(id) {
	var el = document.getElementById(id);
	if (el) el.classList.remove('show');
	// 检查是否还有其他弹窗打开
	var activeModals = document.querySelectorAll('.modal.show');
	if (activeModals.length === 0) {
		var mask = document.getElementById('mask');
		if (mask) mask.classList.remove('show');
	}
}

/* ============ 动态加载根类型 ============ */
function loadRootTypes() {
	return api.getJson(NODE_API + '/root_types/').then(function (res) {
		var types = res.root_types || [];
		ROOT_TYPE_MAP = {};
		ROOT_ICON_MAP = {};
		types.forEach(function (t, index) {
			ROOT_TYPE_MAP[t.code] = t.name;
			ROOT_ICON_MAP[t.code] = DEFAULT_ROOT_ICONS[index % DEFAULT_ROOT_ICONS.length];
		});
	}).catch(function () {
		// 降级为默认值（防止 API 不可用时页面完全崩溃）
		ROOT_TYPE_MAP = { company_doc: '企业文档' };
		ROOT_ICON_MAP = { company_doc: '📁' };
	});
}

/* ============ 页面初始化 ============ */
function initNodesPage() {
	// 控制新增节点按钮可见性
	var btn = document.getElementById('btnNewNode');
	if (btn) btn.style.display = canManageNodes() ? '' : 'none';
	loadRootTypes().then(function () {
		loadTree();
	});
}

/* ============ 加载节点树 ============ */
function loadTree() {
	return api.getJson(NODE_API + '/tree/').then(function (res) {
		// 后端已返回嵌套树结构，直接使用
		nodeTree = res.tree || [];
		renderTree(nodeTree);
	}).catch(function (e) {
		document.getElementById('nodeMgrTree').innerHTML =
			'<div style="padding:20px;text-align:center;color:var(--danger)">加载失败: ' + escapeHtml(e.message || '未知错误') + '</div>';
	});
}

/* ============ 渲染节点树 ============ */
function renderTree(treeData) {
	var container = document.getElementById('nodeMgrTree');
	if (!treeData || treeData.length === 0) {
		container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-sub)">暂无节点，请先创建</div>';
		return;
	}
	container.innerHTML = renderTreeHTML(treeData, 0);
}

function renderTreeHTML(items, depth) {
	return items.map(function (n) {
		// n 已经是后端返回的节点对象，children 直接挂在节点上
		var hasChild = n.children && n.children.length > 0;
		var isRoot = n.node_type === 'root';
		var icon = getNodeIcon(n);
		var selClass = (selectedNodeId === n.id) ? ' selected' : '';

		var html = '<div class="tree-node">';
		html += '<div class="tree-item' + (isRoot ? ' tree-root' : '') + selClass + '" onclick="selectNode(' + n.id + ', this, event)">';
		html += '<span class="tree-toggle ' + (hasChild ? 'expanded' : 'leaf') + '" onclick="event.stopPropagation();toggleTreeOnly(this)">\u25b6</span>';
		html += '<span class="tree-icon">' + icon + '</span>';
		html += '<span class="tree-label">' + escapeHtml(n.name) + '</span>';
		html += '<span class="tree-count">' + (n.document_count || 0) + '</span>';
		html += '</div>';
		if (hasChild) {
			html += '<div class="tree-children expanded">' + renderTreeHTML(n.children, depth + 1) + '</div>';
		}
		html += '</div>';
		return html;
	}).join('');
}

function getNodeIcon(n) {
	// 根节点：按领域图标
	if (n.node_kind === 'ROOT' || n.node_type === 'root') return ROOT_ICON_MAP[n.root_type] || '📁';
	// 组织节点（部门/团队，由组织架构同步创建）
	if (n.node_kind === 'ORG') return '🏢';
	// 叶子/文件夹
	if (n.node_type === 'leaf') return '📄';
	return '📂';
}

/* ============ 树的折叠/展开 ============ */
function toggleTreeOnly(toggle) {
	var item = toggle.parentElement;
	var children = item.parentElement.querySelector('.tree-children');
	if (!children) return;
	var exp = toggle.classList.toggle('expanded');
	children.classList.toggle('expanded', exp);
}

function expandAllTree() {
	document.querySelectorAll('.tree-toggle').forEach(function (t) {
		if (!t.classList.contains('leaf')) t.classList.add('expanded');
	});
	document.querySelectorAll('.tree-children').forEach(function (c) {
		c.classList.add('expanded');
	});
}

function collapseAllTree() {
	document.querySelectorAll('.tree-toggle').forEach(function (t) {
		t.classList.remove('expanded');
	});
	document.querySelectorAll('.tree-children').forEach(function (c) {
		c.classList.remove('expanded');
	});
}

/* ============ 选中节点 ============ */
function selectNode(id, elm, e) {
	selectedNodeId = id;
	document.querySelectorAll('.tree-item.selected').forEach(function (x) { x.classList.remove('selected'); });
	elm.classList.add('selected');

	// 同步文档列表的节点筛选（不自动展开）
	setDocNodeFilter(id);

	// 从后端加载节点详情
	api.getJson(NODE_API + '/' + id + '/').then(function (node) {
		renderNodeDetail(node);
	}).catch(function (e) {
		document.getElementById('nodeDetail').innerHTML =
			'<div style="padding:40px;text-align:center;color:var(--danger)">加载节点详情失败</div>';
	});
}

/* ============ 渲染节点详情（使用 tmpl-node-detail 模板） ============ */
function renderNodeDetail(n) {
	var rootTypeName = ROOT_TYPE_MAP[n.root_type] || n.root_type;
	var icon = getNodeIcon(n);
	var nodeKindLabels = { ROOT: '根节点', ORG: '组织节点', FOLDER: '文件夹' };
	var visLabels = { TEAM_ONLY: '仅团队', DEPT_ONLY: '仅部门', PUBLIC: '全局公开' };
	var parentInfo = n.parent_id ? '节点#' + n.parent_id : '（根节点）';
	var parentPath = n.path ? n.path.replace(/\/$/, '').replace(/\//g, ' / ').trim() : '/';

	var tmpl = document.getElementById('tmpl-node-detail');
	var clone = tmpl.content.cloneNode(true);

	clone.querySelector('.nd-icon').textContent = icon;
	clone.querySelector('.nd-name').textContent = n.name;
	clone.querySelector('.nd-id').textContent = 'node-' + n.id;
	clone.querySelector('.nd-path').textContent = parentPath;

	// 操作按钮：admin/ops 全可见，团队组长仅本团队范围内可见（仅文件夹可编辑/删除）
	var actionsEl = clone.querySelector('.nd-actions');
	var showActions = (isAdminOrOps() || (isTeamLeader() && isNodeInTeam(n, getTeamLeaderTeamNodeIds())))
		&& n.node_kind !== 'ROOT' && n.node_kind !== 'ORG';
	if (showActions) {
		actionsEl.innerHTML =
			'<button class="btn btn-sm" onclick="editNode(' + n.id + ')">\uD83D\uDCDD 编辑</button>' +
			'<button class="btn btn-sm btn-danger" onclick="deleteNode(' + n.id + ')">\uD83D\uDDD1 删除文件夹</button>';
	}

	// 统计卡片
	clone.querySelector('.nd-doc-card').onclick = function () { viewNodeDocs(n.id); };
	clone.querySelector('.nd-doc-count').textContent = (n.document_count || 0).toLocaleString();
	clone.querySelector('.nd-children-count').textContent = (n.children_count || 0).toLocaleString();
	clone.querySelector('.nd-node-type-label').textContent = nodeKindLabels[n.node_kind] || n.node_type || '—';

	// 基础信息
	clone.querySelector('.nd-info-name').textContent = n.name;
	clone.querySelector('.nd-info-root-type').textContent = rootTypeName;
	clone.querySelector('.nd-info-creator').textContent = n.created_by_name || '—';
	clone.querySelector('.nd-info-created').textContent = formatDate(n.created_at);
	clone.querySelector('.nd-info-updated').textContent = formatDate(n.updated_at);
	clone.querySelector('.nd-info-parent').textContent = parentInfo;
	clone.querySelector('.nd-info-visibility').textContent =
		n.visibility_level ? (visLabels[n.visibility_level] || n.visibility_level) : '继承父级';
	clone.querySelector('.nd-info-order').textContent = n.order_no || 0;
	clone.querySelector('.nd-info-depth').textContent = '第' + n.depth + '层';

	// 描述（合并在基础信息卡片内）
	if (n.description) {
		clone.querySelector('.nd-desc-section').classList.remove('hidden');
		clone.querySelector('.nd-desc-text').textContent = n.description;
	}

	var detail = document.getElementById('nodeDetail');
	detail.innerHTML = '';
	detail.appendChild(clone);
}


/* ============ 新增节点弹窗 ============ */
function openNodeModal() {
	document.getElementById('nodeId').value = '';
	document.getElementById('nodeName').value = '';
	document.getElementById('nodeDesc').value = '';
	document.getElementById('nodeOrder').value = '0';
	document.getElementById('nodeVisibility').value = '';
	document.getElementById('nodeModalTitle').textContent = '新增文件夹';

	var isTL = isTeamLeader();

	// 手动创建的一律是文件夹；所有用户都必选上级节点
	var hint = document.getElementById('parentHint');
	if (isTL) {
		hint.textContent = '必须选择一个团队范围内的上级节点';
	} else {
		hint.textContent = '超管/文档管理员可在知识库根下创建文件夹（与部门同级）；其他角色选择自己范围内的上级节点';
	}

	// 复用已加载的 nodeTree 构建父节点列表
	buildParentOptions(null);
	showModal('nodeModal');
}

/* ============ 编辑节点弹窗 ============ */
function editNode(id) {
	api.getJson(NODE_API + '/' + id + '/').then(function (node) {
		document.getElementById('nodeId').value = node.id;
		document.getElementById('nodeName').value = node.name;
		document.getElementById('nodeDesc').value = node.description || '';
		document.getElementById('nodeOrder').value = node.order_no || 0;
		document.getElementById('nodeVisibility').value = node.visibility_level || '';
		document.getElementById('nodeModalTitle').textContent = '编辑文件夹';

		// 编辑时不允许修改父节点
		buildParentOptions(node.parent_id);
		document.getElementById('nodeParent').disabled = true;

		document.getElementById('parentHint').textContent =
			'修改可见范围需走工单审批，由两位管理员先后审核';
		showModal('nodeModal');
	}).catch(function (e) {
		toast('加载节点详情失败: ' + e.message, 'error');
	});
}

/* 从 nodeTree 构建可选父节点列表（复用已加载数据；若未加载则回退 API 请求） */
function buildParentOptions(selectedParentId, cb) {
	var parentSelect = document.getElementById('nodeParent');
	var options = [];

	// 团队组长：只展示本团队范围内的节点
	var teamNodeIds = getTeamLeaderTeamNodeIds();
	var teamPaths = teamNodeIds.map(function (id) {
		var found = findNodeById(id);
		return found ? found.path : '';
	}).filter(Boolean);

	function isInTeam(n) {
		if (!isTeamLeader()) return true; // 非组长不过滤
		for (var i = 0; i < teamPaths.length; i++) {
			var tp = teamPaths[i];
			// tp 本身以 / 结尾；子节点 path 以 tp 开头即为团队子树内
			if (n.path === tp || n.path.indexOf(tp) === 0) {
				return true;
			}
		}
		return false;
	}

	function flatten(items, depth) {
		items.forEach(function (n) {
			// 团队组长：只展示团队范围内的非叶子节点
			if (isTeamLeader() && !isInTeam(n)) {
				// 不展开但允许遍历子节点（子节点可能在后代中匹配）
				if (n.children && n.children.length > 0) {
					flatten(n.children, depth);
				}
				return;
			}
			if (n.node_type !== 'leaf') {
				var indent = '';
				for (var i = 0; i < depth; i++) {
					indent += '\u00A0\u00A0\u00A0\u00A0\u00A0\u00A0';
				}
				var prefix = depth > 0 ? '\u2514 ' : '';
				options.push({
					id: n.id,
					name: indent + prefix + n.name
				});
			}
			if (n.children && n.children.length > 0) {
				flatten(n.children, depth + 1);
			}
		});
	}

	function renderOptions() {
		// 编辑模式：如果当前节点的父节点不在 options 中，手动添加
		if (selectedParentId && !options.some(function (o) { return o.id === selectedParentId; })) {
			options.unshift({ id: selectedParentId, name: '(上级节点#' + selectedParentId + ')' });
		}

		var html = '';
		options.forEach(function (opt) {
			var sel = (selectedParentId === opt.id) ? ' selected' : '';
			html += '<option value="' + opt.id + '"' + sel + '>' + escapeHtml(opt.name) + '</option>';
		});
		parentSelect.innerHTML = html;
		if (cb) cb();
	}

	if (nodeTree.length > 0) {
		flatten(nodeTree, 0);
		renderOptions();
	} else {
		// 树尚未加载完成，回退 API 请求
		parentSelect.innerHTML = '<option value="">加载中...</option>';
		api.getJson(NODE_API + '/tree/').then(function (data) {
			var tree = data.tree || [];
			// 回退时也更新 nodeTree（确保 team leader 权限辅助可用）
			if (isTeamLeader()) {
				nodeTree = tree;
				teamNodeIds = getTeamLeaderTeamNodeIds();
				teamPaths = teamNodeIds.map(function (id) {
					var found = findNodeById(id);
					return found ? found.path : '';
				}).filter(Boolean);
			}
			flatten(tree, 0);
			renderOptions();
		}).catch(function () {
			parentSelect.innerHTML = '<option value="">加载失败</option>';
		});
	}
}

/* ============ 保存节点（新增/编辑） ============ */
function saveNode() {
	var id = document.getElementById('nodeId').value;
	var parentId = document.getElementById('nodeParent').value;
	var name = document.getElementById('nodeName').value.trim();
	var desc = document.getElementById('nodeDesc').value.trim();
	var orderNo = parseInt(document.getElementById('nodeOrder').value) || 0;
	var visibility = document.getElementById('nodeVisibility').value;

	if (!name) { toast('请输入节点名称', 'warning'); return; }

	// 文件夹必须选择上级节点
	if (!parentId) {
		toast('文件夹必须选择上级节点', 'warning');
		return;
	}

	var body = {
		node_type: 'folder',
		name: name,
		description: desc,
		order_no: orderNo,
	};

	if (id) {
		// 编辑模式：PATCH 提交可见范围（空值表示继承父级，写回 null），变更走工单审批
		body.visibility_level = visibility || null;
	} else if (visibility) {
		// 新建模式：初始可见范围直接生效，无需审批
		body.visibility_level = visibility;
	}

	// 仅新建时发送 parent，编辑时不修改归属
	if (!id && parentId) {
		body.parent = parentId;
	}

	var method = id ? 'PATCH' : 'POST';
	var url = id ? NODE_API + '/' + id + '/' : NODE_API + '/';

	var promise = id ? api.patchJson(url, body) : api.postJson(url, body);
	promise.then(function () {
		toast(id ? '节点已更新' : '节点已创建', 'success');
		selectedNodeId = null;
		// 等树刷新完再关闭弹窗，确保下次打开时父节点列表是最新的
		loadTree().then(function () {
			closeModal('nodeModal');
		});
		document.getElementById('nodeDetail').innerHTML =
			'<div style="padding:40px;text-align:center;color:var(--text-sub)">' +
			'<div style="font-size:48px;margin-bottom:12px">🗂️</div>' +
			'<div>请在左侧选择或创建一个节点</div></div>';
	}).catch(function (e) {
		// 403 + 审批提示 = 可见范围变更已自动提交审批工单（非失败），以成功提示告知用户
		if (e && e.status === 403 && e.message && e.message.indexOf('已自动提交审批工单') !== -1) {
			toast('可见范围变更已提交审批，审批通过后生效', 'success');
			return;
		}
		toast('保存失败: ' + e.message, 'error');
	});
}

/* ============ 删除文件夹 ============ */
function deleteNode(id) {
	// 使用 common.css 的二次确认弹窗替代原生 confirm
	showConfirmDialog({
		title: '删除文件夹',
		bannerType: 'danger',
		bannerIcon: '🗑',
		bannerText: '确认删除该文件夹？',
		bodyHtml: '<p class="form-hint">注意：文件夹下存在子文件夹或文档时无法删除。</p>',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除', type: 'danger', onClick: function (ctx) {
					ctx.close();
					api.deleteJson(NODE_API + '/' + id + '/').then(function () {
						toast('文件夹已删除', 'success');
						selectedNodeId = null;
						loadTree();
						document.getElementById('nodeDetail').innerHTML =
							'<div style="padding:40px;text-align:center;color:var(--text-sub)">' +
							'<div style="font-size:48px;margin-bottom:12px">🗂️</div>' +
							'<div>请在左侧选择或创建一个节点</div></div>';
					}).catch(function (e) {
						toast(e.message, 'error');
					});
				}
			}
		]
	});
}


/* ================================================================
 * 文档列表（展示 / 分页 / 筛选 / 搜索 / 预览 / 下载 / 分享 /
 *        申请权限 / 访问管理 / 编辑可视范围 / 删除）
 * ================================================================ */
var DOC_API = '/api/v1/knowledge/documents';
var docListPage = 1;
var docListTotal = 0;
var docListNodeFilter = null;   // 当前节点筛选（null=全部）
var docListCurrentDocs = [];    // 当前页文档数据（用于权限判定）
var docListSearchTimeout = null;  // 搜索防抖定时器

/* ---- 搜索输入防抖 ---- */
function onDocSearchInput() {
	if (docListSearchTimeout) clearTimeout(docListSearchTimeout);
	docListSearchTimeout = setTimeout(function () {
		loadDocList(1);
	}, 300);
}

/* ---- 文档列表弹窗 ---- */

function closeDocListModal() {
	closeModal('docListModal');
}

/* ---- 模态框 z-index 堆叠管理（防止背景穿透） ---- */
var _modalZStack = [];
var _MODAL_Z_BASE = 10000;
var _origShowModal = showModal;
var _origCloseModal = closeModal;

window.showModal = function (id) {
	var idx = _modalZStack.indexOf(id);
	if (idx !== -1) _modalZStack.splice(idx, 1);
	_modalZStack.push(id);
	_origShowModal(id);
	var m = document.getElementById(id);
	if (m) m.style.zIndex = _MODAL_Z_BASE + _modalZStack.length;
	var mask = document.getElementById('mask');
	if (mask) mask.style.zIndex = _MODAL_Z_BASE + _modalZStack.length - 1;
};

window.closeModal = function (id) {
	var idx = _modalZStack.indexOf(id);
	if (idx !== -1) _modalZStack.splice(idx, 1);
	_origCloseModal(id);
	if (_modalZStack.length > 0) {
		var mask = document.getElementById('mask');
		if (mask) mask.style.zIndex = _MODAL_Z_BASE + _modalZStack.length - 1;
	}
};

/* ---- 从节点详情页点击"查看本节点文档" ---- */
function viewNodeDocs(nodeId) {
	setDocNodeFilter(nodeId);
	showModal('docListModal');
	loadDocList(1);
}

/* ---- 设置节点筛选 ---- */
function setDocNodeFilter(nodeId) {
	docListNodeFilter = nodeId;
}

/* ---- 加载文档列表 ---- */
function loadDocList(page) {
	docListPage = page || 1;
	var params = ['discover=1', 'page=' + docListPage, 'page_size=20'];

	var search = (document.getElementById('docSearch').value || '').trim();
	if (search) params.push('search=' + encodeURIComponent(search));

	var statusFilter = document.getElementById('docStatusFilter').value;
	if (statusFilter) params.push('status=' + encodeURIComponent(statusFilter));

	var visFilter = document.getElementById('docVisFilter').value;
	if (visFilter) params.push('visibility=' + encodeURIComponent(visFilter));

	var typeFilter = document.getElementById('docTypeFilter').value;
	if (typeFilter) params.push('file_type=' + encodeURIComponent(typeFilter));

	// 含旧版本：勾选后列表展示全部版本（含被新版本替换的非活跃版本），用于回溯与切换
	var showAll = document.getElementById('docShowAll');
	if (showAll && showAll.checked) params.push('version=all');

	if (docListNodeFilter) params.push('node=' + docListNodeFilter);

	var tbody = document.getElementById('docListTbody');
	tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-sub);padding:24px">加载中...</td></tr>';

	api.getJson(DOC_API + '/?' + params.join('&')).then(function (data) {
		var docs = data.results || data || [];
		docListCurrentDocs = docs;
		docListTotal = data.count || docs.length || 0;
		renderDocList(docs);
		renderDocPagination();
		// 更新计数
		var countEl = document.getElementById('docListCount');
		if (countEl) countEl.textContent = '（共 ' + docListTotal + ' 条）';
	}).catch(function (e) {
		tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--danger);padding:24px">加载失败：' + escapeHtml(e.message || '') + '</td></tr>';
	});
}

/* ---- 渲染文档表格（使用 tmpl-doc-row 模板） ---- */
function renderDocList(docs) {
	var tbody = document.getElementById('docListTbody');
	if (!docs || docs.length === 0) {
		tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-sub);padding:24px">暂无文档</td></tr>';
		return;
	}

	tbody.innerHTML = '';
	var tmpl = document.getElementById('tmpl-doc-row');

	docs.forEach(function (d) {
		var clone = tmpl.content.cloneNode(true);
		var fileEl = clone.querySelector('.dr-file');
		// 活跃标记：有多版本时标注当前生效版本；非活跃版本（?version=all 可见）标注旧版本
		var versionMarker = '';
		if (d.version_count > 1) {
			versionMarker = d.is_active
				? ' <span class="tag tag-success" style="margin-left:4px">活跃</span>'
				: ' <span class="tag" style="margin-left:4px;background:#eee;color:#888">旧版本</span>';
		}
		fileEl.innerHTML = fileTypeIcon(d.file_type) + ' ' + escapeHtml(d.file_name) + versionMarker;
		fileEl.title = d.file_name;
		clone.querySelector('.dr-type').textContent = d.file_type || '-';
		clone.querySelector('.dr-owner').textContent = d.owner_name || '-';
		clone.querySelector('.dr-team').textContent = getDocTeamName(d);
		clone.querySelector('.dr-vis').innerHTML = visTagHtml(d.visible_scope);
		clone.querySelector('.dr-status').innerHTML = statusTagHtml(d);
		var dateEl = clone.querySelector('.dr-date');
		dateEl.textContent = formatDateShort(d.created_at);
		dateEl.title = formatDate(d.created_at);
		clone.querySelector('.dr-actions').innerHTML = renderDocActions(d);
		tbody.appendChild(clone);
	});
}

/* ---- 渲染操作按钮（按权限） ---- */
function isPreviewable(fileType) {
	// 复用 common.js 共享判断（版本历史弹窗等场景保持一致）
	return isPreviewableFileType(fileType);
}

function renderDocActions(d) {
	var actions = [];
	// 预览：支持预览的文件类型且用户有阅读权限才展示（无权限时隐藏，后端仍会二次校验）
	if (isPreviewable(d.file_type) && d.can_read !== false) {
		actions.push('<button class="btn-link btn-sm" onclick="previewDoc(' + d.id + ')">预览</button>');
	}
	// 版本切换：同组存在多个版本时展示入口（活跃/旧版本均可打开版本历史弹窗）
	if (d.version_count > 1) {
		actions.push('<button class="btn-link btn-sm" onclick="showVersionModal(' + d.id + ')">版本</button>');
	}
	// 权限区分
	if (d.is_owner || d.is_manager) {
		actions.push('<button class="btn-link btn-sm" onclick="openAccessModal(' + d.id + ')">访问管理</button>');
		actions.push('<button class="btn-link btn-sm" onclick="openVisModal(' + d.id + ')">设置</button>');
		actions.push('<button class="btn-link btn-sm" style="color:var(--danger)" onclick="deleteDoc(' + d.id + ')">删除</button>');
	} else {
		actions.push('<button class="btn-link btn-sm" onclick="openReqModal(' + d.id + ')" style="color:var(--warning)">申请权限</button>');
	}
	return '<div class="table-actions">' + actions.join('') + '</div>';
}

/* ---- 分页（使用 tmpl-doc-pagination 模板） ---- */
function renderDocPagination() {
	var container = document.getElementById('docPagination');
	if (!container) return;
	var pageSize = 20;
	var totalPages = Math.max(1, Math.ceil(docListTotal / pageSize));
	var page = docListPage;

	if (docListTotal === 0) { container.innerHTML = ''; return; }

	var tmpl = document.getElementById('tmpl-doc-pagination');
	var clone = tmpl.content.cloneNode(true);

	clone.querySelector('.pg-total').textContent = docListTotal;

	var prev = clone.querySelector('.pg-prev');
	if (page > 1) {
		prev.onclick = function () { loadDocList(page - 1); };
	} else {
		prev.disabled = true;
	}

	var numbersEl = clone.querySelector('.pg-numbers');
	var numsHtml = '';
	for (var i = 1; i <= totalPages; i++) {
		if (totalPages <= 7 || i === 1 || i === totalPages || (i >= page - 2 && i <= page + 2)) {
			numsHtml += i === page
				? '<button class="page-btn active">' + i + '</button>'
				: '<button class="page-btn" onclick="loadDocList(' + i + ')">' + i + '</button>';
		} else if (i === page - 3 || i === page + 3) {
			numsHtml += '<span>...</span>';
		}
	}
	numbersEl.innerHTML = numsHtml;

	var next = clone.querySelector('.pg-next');
	if (page < totalPages) {
		next.onclick = function () { loadDocList(page + 1); };
	} else {
		next.disabled = true;
	}

	container.innerHTML = '';
	container.appendChild(clone);
}

/* ================================================================
 * 文档预览（预览弹窗由公共模块 preview-doc.js 实现）
 * ================================================================ */
// 预览元信息来源：当前文档列表（docListCurrentDocs）中按 id 查找，找不到返回 null
function getDocForPreview(id) {
	return Promise.resolve((docListCurrentDocs || []).find(function (x) { return x.id === id; }) || null);
}

/* ================================================================
 * 分享弹窗
 * ================================================================ */
function openShareModal(id) {
	document.getElementById('docShareId').value = id;
	document.getElementById('docShareUser').value = '';
	document.getElementById('docShareType').value = 'read';
	document.getElementById('docShareExpiry').value = '';
	showModal('docShareModal');
}

function submitShare() {
	var id = document.getElementById('docShareId').value;
	var toUser = document.getElementById('docShareUser').value.trim();
	var accessType = document.getElementById('docShareType').value;
	var expiry = document.getElementById('docShareExpiry').value;

	if (!toUser) { toast('请输入目标用户名', 'warning'); return; }

	var body = { to_username: toUser, access_type: accessType };
	if (expiry) body.expires_at = expiry;

	api.postJson(DOC_API + '/' + id + '/share/', body).then(function (res) {
		closeModal('docShareModal');
		toast('已分享给 ' + escapeHtml(res.to_user || '') + '（' + escapeHtml(res.access_type || '') + '）', 'success');
	}).catch(function (e) {
		toast(e.message || '分享失败', 'error');
	});
}

/* ================================================================
 * 申请权限弹窗
 * ================================================================ */
function openReqModal(id) {
	document.getElementById('docReqId').value = id;
	document.getElementById('docReqType').value = 'read';
	document.getElementById('docReqReason').value = '';
	showModal('docReqModal');
}

function submitRequest() {
	var id = document.getElementById('docReqId').value;
	var accessType = document.getElementById('docReqType').value;
	var reason = document.getElementById('docReqReason').value.trim();

	api.postJson(DOC_API + '/' + id + '/request_access/', {
		access_type: accessType, reason: reason
	}).then(function () {
		closeModal('docReqModal');
		toast('申请已提交，等待审批', 'success');
	}).catch(function (e) {
		toast(e.message || '申请失败', 'error');
	});
}

/* ================================================================
 * 访问管理弹窗（已授权 + 待审批）— 使用模板
 * ================================================================ */
function openAccessModal(id) {
	document.getElementById('docAccessId').value = id;
	showModal('docAccessModal');
	loadDocGrants(id);
	loadDocRequests(id);
}

function loadDocGrants(id) {
	var tbody = document.getElementById('docGrantsBody');
	tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-sub)">加载中...</td></tr>';

	api.getJson(DOC_API + '/' + id + '/access_grants/').then(function (data) {
		if (!data) {
			tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-sub)">暂无授权记录</td></tr>';
			return;
		}

		var actionMap = { read: '读取', download: '下载', share: '分享', edit: '编辑', export: '导出' };
		var sourceMap = { direct: '直接', share: '分享', request: '申请' };
		tbody.innerHTML = '';

		// 显示部门授权
		if (data.dept_grants && data.dept_grants.length > 0) {
			var deptTpl = document.getElementById('tmpl-auth-dept-row');
			data.dept_grants.forEach(function (dept) {
				var clone = deptTpl.content.cloneNode(true);
				clone.querySelector('.at-dept-name').textContent = dept.name;
				tbody.appendChild(clone);
			});
		}

		// 显示团队授权
		if (data.team_grants && data.team_grants.length > 0) {
			var teamTpl = document.getElementById('tmpl-auth-team-row');
			data.team_grants.forEach(function (team) {
				var clone = teamTpl.content.cloneNode(true);
				clone.querySelector('.at-team-name').textContent = team.name;
				tbody.appendChild(clone);
			});
		}

		// 显示直接授权的用户
		if (data.direct_grants && data.direct_grants.length > 0) {
			var directTpl = document.getElementById('tmpl-auth-direct-row');
			data.direct_grants.forEach(function (g) {
				var clone = directTpl.content.cloneNode(true);
				clone.querySelector('.at-user-name').textContent = g.granted_to_name || '-';
				clone.querySelector('.at-action').textContent = actionMap[g.action] || g.action;
				clone.querySelector('.at-source').textContent = sourceMap[g.source] || g.source || '-';
				clone.querySelector('.at-expires').textContent = g.expires_at ? formatDate(g.expires_at) : '永久';
				clone.querySelector('.at-active-tag').innerHTML = g.is_active
					? '<span class="tag tag-success">有效</span>'
					: '<span class="tag tag-danger">已撤销</span>';
				clone.querySelector('.at-revoke-btn').innerHTML = g.is_active
					? '<button class="btn-link btn-sm" style="color:var(--danger)" onclick="revokeGrant(' + id + ',' + g.id + ')">撤销</button>'
					: '-';
				tbody.appendChild(clone);
			});
		}

		if (tbody.children.length === 0) {
			tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-sub)">暂无授权记录</td></tr>';
		}
	}).catch(function (e) {
		tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--danger)">加载失败</td></tr>';
	});
}

function loadDocRequests(id) {
	var tbody = document.getElementById('docReqsBody');
	tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-sub)">加载中...</td></tr>';

	// 拉取待审批申请（仅该文档）
	api.getJson(DOC_API + '/pending_access_requests/').then(function (reqs) {
		reqs = (reqs || []).filter(function (r) { return r.document_id == id && r.status === 'pending'; });
		if (reqs.length === 0) {
			tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-sub)">暂无待审批申请</td></tr>';
			return;
		}
		var typeMap = { read: '读取', download: '下载', share: '分享' };
		tbody.innerHTML = '';
		var tpl = document.getElementById('tmpl-approval-row');
		reqs.forEach(function (r) {
			var clone = tpl.content.cloneNode(true);
			clone.querySelector('.ar-requester').textContent = r.requester_name || '-';
			clone.querySelector('.ar-type').textContent = typeMap[r.access_type] || r.access_type;
			clone.querySelector('.ar-reason').textContent = r.reason || '-';
			clone.querySelector('.ar-date').textContent = formatDate(r.created_at);
			clone.querySelector('.ar-approve').onclick = function () { approveReq(r.id); };
			clone.querySelector('.ar-reject').onclick = function () { rejectReq(r.id); };
			tbody.appendChild(clone);
		});
	}).catch(function (e) {
		tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--danger)">加载失败</td></tr>';
	});
}

function revokeGrant(docId, grantId) {
	showConfirmDialog({
		title: '撤销访问权限',
		bannerType: 'warning',
		bannerIcon: '⚠',
		bannerText: '确认撤销该用户的访问权限？',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认撤销', type: 'primary', onClick: function (ctx) {
					ctx.close();
					api.postJson(DOC_API + '/' + docId + '/revoke_grant/', { grant_id: grantId }).then(function () {
						toast('已撤销', 'success');
						loadDocGrants(docId);
					}).catch(function (e) {
						toast(e.message || '撤销失败', 'error');
					});
				}
			}
		]
	});
}

function approveReq(reqId) {
	var docId = document.getElementById('docAccessId').value;
	api.postJson(DOC_API + '/approve_access_request/', { request_id: reqId }).then(function () {
		toast('已批准', 'success');
		loadDocGrants(docId);
		loadDocRequests(docId);
	}).catch(function (e) {
		toast(e.message || '操作失败', 'error');
	});
}

function rejectReq(reqId) {
	var docId = document.getElementById('docAccessId').value;
	showConfirmDialog({
		title: '驳回申请',
		bannerType: 'warning',
		bannerIcon: '⚠',
		bannerText: '确认驳回该申请？',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认驳回', type: 'primary', onClick: function (ctx) {
					ctx.close();
					api.postJson(DOC_API + '/reject_access_request/', { request_id: reqId }).then(function () {
						toast('已驳回', 'success');
						loadDocRequests(docId);
					}).catch(function (e) {
						toast(e.message || '操作失败', 'error');
					});
				}
			}
		]
	});
}

/* ================================================================
 * 文档设置弹窗 — 可见范围调整
 * ================================================================ */
var visDeptList = [];
var visTeamList = [];
var docVisOldScope = '';       // 打开弹窗时的可见范围
var docVisOwnershipType = '';  // 'team' / 'dept' / 'public'
var docVisDocData = null;      // 当前文档完整数据
var docVisNarrowMulti = null;  // 缩小范围时的 multi-select 实例

var SCOPE_LABELS = { 'team': '团队', 'dept': '部门', 'public': '全公司公开' };
var SCOPE_ORDER = { 'team': 0, 'dept': 1, 'public': 2 };

function loadDeptTeamOptions(cb) {
	if (visDeptList.length > 0 && visTeamList.length > 0) {
		if (cb) cb();
		return;
	}
	api.getJson('/api/v1/knowledge/documents/allowed_visibility/').then(function (res) {
		visDeptList = res.departments || [];
		visTeamList = res.teams || [];
		if (cb) cb();
	}).catch(function () {
		visDeptList = [];
		visTeamList = [];
		if (cb) cb();
	});
}

function openVisModal(id) {
	var doc = docListCurrentDocs.find(function (d) { return d.id === id; });
	if (!doc) { toast('文档信息缺失', 'error'); return; }
	docVisDocData = doc;
	docVisOldScope = doc.visible_scope || 'team';
	docVisOwnershipType = docVisOldScope;

	document.getElementById('docVisId').value = id;

	// ── 归属信息 ──
	document.getElementById('docVisFileName').textContent = doc.file_name || doc.title || '—';
	document.getElementById('docVisUploader').textContent = doc.owner_name || '—';
	document.getElementById('docVisCurrentLabel').textContent = SCOPE_LABELS[docVisOldScope] || docVisOldScope;

	// 归属路径: 从 nodeTree 中查找 dept/team 名称
	var ownershipPath = buildDocOwnershipPath(doc);
	document.getElementById('docVisOwnership').textContent = ownershipPath;

	// ── 根据归属类型构建下拉选项 ──
	var sel = document.getElementById('docVisSelect');
	sel.innerHTML = '';

	if (docVisOldScope === 'team') {
		// 团队文档: 本团队 → 本部门 → 全公司公开
		sel.appendChild(createOption('team', '本团队（当前）'));
		sel.appendChild(createOption('dept', '本部门'));
		sel.appendChild(createOption('public', '全公司公开'));
	} else if (docVisOldScope === 'dept') {
		// 部门文档: 指定团队(缩小) → 本部门 → 全公司公开
		sel.appendChild(createOption('narrow_teams', '指定团队（缩小范围）'));
		sel.appendChild(createOption('dept', '本部门（当前）'));
		sel.appendChild(createOption('public', '全公司公开'));
	} else {
		// 公开文档 (仅管理员可操作缩小)
		sel.appendChild(createOption('narrow_depts', '指定部门（缩小范围）'));
		sel.appendChild(createOption('narrow_teams', '指定团队（缩小范围）'));
		sel.appendChild(createOption('public', '全公司公开（当前）'));
	}
	sel.value = docVisOldScope;

	// ── 加载部门/团队列表并初始化缩小范围面板 ──
	loadDeptTeamOptions(function () {
		initNarrowMultiSelect();
		onDocVisChange();
	});
	showModal('docVisModal');
}

/** 构建文档归属路径显示 */
function buildDocOwnershipPath(doc) {
	var parts = [];
	if (doc.dept_node_id) {
		var deptNode = findNodeById(doc.dept_node_id);
		if (deptNode) parts.push(deptNode.name);
	}
	if (doc.team_node_id) {
		var teamNode = findNodeById(doc.team_node_id);
		if (teamNode) parts.push(teamNode.name);
	}
	return parts.length > 0 ? parts.join(' / ') : '公司';
}

function createOption(value, text) {
	var opt = document.createElement('option');
	opt.value = value;
	opt.textContent = text;
	return opt;
}

/** 初始化缩小范围的多选组件 */
function initNarrowMultiSelect() {
	docVisNarrowMulti = createDeptTeamMultiSelect({
		prefix: 'docVisNarrow',
		deptList: visDeptList,
		teamList: visTeamList
	});
	docVisNarrowMulti.renderDeptList([]);
	docVisNarrowMulti.renderTeamList([], []);
}

function onDocVisChange() {
	var vis = document.getElementById('docVisSelect').value;
	var upgradeHint = document.getElementById('docVisUpgradeHint');
	var narrowPanel = document.getElementById('docVisNarrowPanel');

	// 扩大范围提示
	var isUpgrade = (SCOPE_ORDER[vis] || 0) > (SCOPE_ORDER[docVisOldScope] || 0);
	// narrow_* 选项本身就是缩小
	var isNarrowOption = vis === 'narrow_teams' || vis === 'narrow_depts';
	upgradeHint.classList.toggle('hidden', !(isUpgrade && !isNarrowOption));

	// 缩小范围面板
	if (isNarrowOption) {
		narrowPanel.classList.remove('hidden');
		var deptSelect = document.getElementById('docVisNarrowDeptSelect');
		if (vis === 'narrow_teams' && docVisOldScope === 'dept') {
			// 部门文档 → 指定团队：仅显示本部门下的团队
			deptSelect.classList.add('hidden');
			document.getElementById('docVisNarrowHint').textContent = '选择可见的团队，未选择的团队将失去访问权限';
			var docDeptId = getDocDeptId();
			if (docDeptId) {
				docVisNarrowMulti.renderDeptList([]);
				// 传递 department 模型 ID（而非 node_id）用于过滤 teams
				docVisNarrowMulti.renderTeamList([], [docDeptId]);
			} else {
				docVisNarrowMulti.renderTeamList([], []);
			}
		} else if (vis === 'narrow_teams') {
			// 公开文档 → 指定团队，需要先选部门再选团队
			deptSelect.classList.remove('hidden');
			document.getElementById('docVisNarrowHint').textContent = '先选择部门，再选择该部门下的可见团队';
			docVisNarrowMulti.renderDeptList([]);
			docVisNarrowMulti.renderTeamList([], []);
		} else if (vis === 'narrow_depts') {
			// 公开文档 → 指定部门
			deptSelect.classList.remove('hidden');
			document.getElementById('docVisNarrowHint').textContent = '选择可见的部门，未选择的部门将失去访问权限';
			docVisNarrowMulti.renderDeptList([]);
			docVisNarrowMulti.renderTeamList([], []);
		}
	} else {
		narrowPanel.classList.add('hidden');
	}
}

/** 获取文档所属的部门模型 ID（从 dept_node_id 查找节点 ref_id） */
function getDocDeptId() {
	if (!docVisDocData || !docVisDocData.dept_node_id) return null;
	var deptNode = findNodeById(docVisDocData.dept_node_id);
	return deptNode ? deptNode.ref_id : null;
}

function getSelectedCheckboxValues(panelId) {
	var values = [];
	document.querySelectorAll('#' + panelId + ' input:checked').forEach(function (cb) {
		values.push(parseInt(cb.value));
	});
	return values;
}

function saveDocVis() {
	var id = document.getElementById('docVisId').value;
	var vis = document.getElementById('docVisSelect').value;

	// narrowing 选项映射
	var isNarrow = vis === 'narrow_teams' || vis === 'narrow_depts';
	var actualScope = vis;
	var narrowTeams = [];
	var narrowDepts = [];

	if (vis === 'narrow_teams') {
		actualScope = 'team';
		narrowTeams = getSelectedCheckboxValues('docVisNarrowTeamPanel');
		if (!narrowTeams.length) {
			toast('请至少选择一个团队', 'warning');
			return;
		}
	} else if (vis === 'narrow_depts') {
		actualScope = 'dept';
		narrowDepts = getSelectedCheckboxValues('docVisNarrowDeptPanel');
		// 同时收集选中的部门下的团队
		narrowTeams = getSelectedCheckboxValues('docVisNarrowTeamPanel');
		if (!narrowDepts.length && !narrowTeams.length) {
			toast('请至少选择一个部门或团队', 'warning');
			return;
		}
	}

	// 无变化
	if (!isNarrow && vis === docVisOldScope) {
		closeModal('docVisModal');
		return;
	}

	var body = { visible_scope: actualScope };

	// 向上调整 / 向下调整
	var isUpgrade = !isNarrow && (SCOPE_ORDER[actualScope] || 0) > (SCOPE_ORDER[docVisOldScope] || 0);

	api.patchJson(DOC_API + '/' + id + '/', body).then(function () {
		// 缩小范围：创建跨团队授权
		if (isNarrow && narrowTeams.length > 0) {
			return createNarrowGrants(id, narrowTeams);
		}
		return Promise.resolve();
	}).then(function () {
		closeModal('docVisModal');
		toast(isNarrow ? '可见范围已缩小' : (isUpgrade ? '设置已保存' : '可见范围已缩小'), 'success');
		loadDocList(docListPage);
	}).catch(function (e) {
		var msg = e.message || '保存失败';
		if (isUpgrade) {
			closeModal('docVisModal');
			toast('可见范围扩大需双层审批，已自动提交申请，需两位管理员先后审批', 'warning');
			return;
		}
		toast(msg, 'error');
	});
}

/** 缩小范围时，创建跨团队授权 */
function createNarrowGrants(docId, teamIds) {
	var promises = teamIds.map(function (teamId) {
		// 从 visTeamList 中查找 team_code
		var team = visTeamList.find(function (t) { return t.id === teamId; });
		var teamCode = team ? (team.code || '') : '';
		if (!teamCode) return Promise.resolve();
		return api.postJson(DOC_API + '/' + docId + '/grant_access/', {
			grant_type: 'cross_team',
			team_code: teamCode
		}).catch(function () {
			// 跨团队授权可能因已存在而失败，忽略
		});
	});
	return Promise.all(promises);
}

/* ================================================================
 * 删除文档
 * ================================================================ */
function deleteDoc(id) {
	showConfirmDialog({
		title: '删除文档',
		bannerType: 'danger',
		bannerIcon: '🗑',
		bannerText: '确认删除此文档？删除后不可恢复。',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除', type: 'danger', onClick: function (ctx) {
					ctx.close();
					api.deleteJson(DOC_API + '/' + id + '/').then(function () {
						toast('文档已删除', 'success');
						loadDocList(docListPage);
					}).catch(function (e) {
						toast(e.message || '删除失败', 'error');
					});
				}
			}
		]
	});
}

/* ================================================================
 * 辅助：标签渲染
 * ================================================================ */
function fileTypeIcon(t) {
	var map = { pdf: '📕', docx: '📄', markdown: '📝', txt: '📃', code: '💻', config: '⚙️', other: '📄' };
	return map[t] || '📄';
}

/** 从 nodeTree 查找文档的团队名称 */
function getDocTeamName(d) {
	if (!d.team_node_id) return '—';
	var node = findNodeById(d.team_node_id);
	return node ? node.name : '—';
}

function visTagHtml(v) {
	var map = { 'team': '团队', 'dept': '部门', 'public': '公开' };
	var cls = { 'team': 'default', 'dept': 'info', 'public': 'primary' };
	return '<span class="tag tag-' + (cls[v] || 'default') + '">' + escapeHtml(map[v] || v) + '</span>';
}

function statusTagHtml(doc) {
	// 复用共享流水线状态（主解析 + 图谱/wiki 阶段）
	return pipelineStatusTag(doc || {});
}
