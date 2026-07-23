/* ============ 节点管理（API 版） ============ */
const NODE_API = '/api/v1/knowledge/nodes';
let nodeTree = [];         // 树形结构
let selectedNodeId = null; // 当前选中的节点 ID

// 动态加载的根类型映射
let ROOT_TYPE_MAP = {};
let ROOT_ICON_MAP = {};

// 默认图标列表（按顺序分配给新根类型）
const DEFAULT_ROOT_ICONS = ['📁', '💻', '🧠', '🛠️', '📚', '🔧', '📋', '📊', '🔍', '⭐'];


function closeModal(id) {
	var el = document.getElementById(id);
	if (el) el.style.display = 'none';
	var mask = document.getElementById('mask');
	if (mask) mask.style.display = 'none';
}

function showModal(id) {
	var el = document.getElementById(id);
	if (el) el.style.display = 'flex';
	var mask = document.getElementById('mask');
	if (mask) mask.style.display = 'block';
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
	loadRootTypes().then(function () {
		loadTree();
	});
}

/* ============ 加载节点树 ============ */
function loadTree() {
	api.getJson(NODE_API + '/tree/').then(function (res) {
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
	if (n.node_type === 'root') return ROOT_ICON_MAP[n.root_type] || '📁';
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

	// 从后端加载节点详情
	api.getJson(NODE_API + '/' + id + '/').then(function (node) {
		renderNodeDetail(node);
	}).catch(function (e) {
		document.getElementById('nodeDetail').innerHTML =
			'<div style="padding:40px;text-align:center;color:var(--danger)">加载节点详情失败</div>';
	});
}

/* ============ 渲染节点详情 ============ */
function renderNodeDetail(n) {
	var rootTypeName = ROOT_TYPE_MAP[n.root_type] || n.root_type;
	var icon = getNodeIcon(n);
	var nodeTypeLabels = { root: '根节点', folder: '文件夹', leaf: '叶子节点' };
	var parentInfo = n.parent_id ? '节点#' + n.parent_id : '（根节点）';
	var parentPath = n.path ? n.path.replace(/\/$/, '').replace(/\//g, ' / ').trim() : '/';

	document.getElementById('nodeDetail').innerHTML =
		'<div class="flex justify-between items-center mb-16">' +
		'<div>' +
		'<div class="text-xl" style="display:flex;align-items:center;gap:8px">' + icon + ' ' + escapeHtml(n.name) + '</div>' +
		'<div class="text-sub text-sm mt-8">节点 ID：node-' + n.id + ' · 路径：' + escapeHtml(parentPath) + '</div>' +
		'</div>' +
		'<div class="flex gap-8">' +
		'<button class="btn btn-sm" onclick="editNode(' + n.id + ')">\uD83D\uDCDD 编辑</button>' +
		'<button class="btn btn-sm btn-danger" onclick="deleteNode(' + n.id + ')">\uD83D\uDDD1 删除节点</button>' +
		'</div>' +
		'</div>' +
		'<div class="grid-3 mb-24">' +
		'<div class="card" style="padding:14px 16px">' +
		'<div class="text-sub text-sm">文档总数</div>' +
		'<div class="text-2xl mt-8">' + (n.document_count || 0).toLocaleString() + '</div>' +
		'</div>' +
		'<div class="card" style="padding:14px 16px">' +
		'<div class="text-sub text-sm">子节点数</div>' +
		'<div class="text-2xl mt-8">' + (n.children_count || 0).toLocaleString() + '</div>' +
		'</div>' +
		'<div class="card" style="padding:14px 16px">' +
		'<div class="text-sub text-sm">节点类型</div>' +
		'<div class="text-2xl mt-8">' + escapeHtml(nodeTypeLabels[n.node_type] || n.node_type) + '</div>' +
		'</div>' +
		'</div>' +
		'<div class="card">' +
		'<div class="card-title">基础信息</div>' +
		'<div class="grid-2" style="gap:14px 24px">' +
		'<div><div class="text-sub text-sm">节点名称</div><div class="fw-500 mt-4">' + escapeHtml(n.name) + '</div></div>' +
		'<div><div class="text-sub text-sm">根类型</div><div class="fw-500 mt-4">' + escapeHtml(rootTypeName) + '</div></div>' +
		'<div><div class="text-sub text-sm">创建人</div><div class="fw-500 mt-4">' + escapeHtml(n.created_by_name || '—') + '</div></div>' +
		'<div><div class="text-sub text-sm">创建时间</div><div class="fw-500 mt-4">' + formatDate(n.created_at) + '</div></div>' +
		'<div><div class="text-sub text-sm">最后更新</div><div class="fw-500 mt-4">' + formatDate(n.updated_at) + '</div></div>' +
		'<div><div class="text-sub text-sm">上级节点</div><div class="fw-500 mt-4">' + escapeHtml(parentInfo) + '</div></div>' +
		'<div><div class="text-sub text-sm">排序号</div><div class="fw-500 mt-4">' + (n.order_no || 0) + '</div></div>' +
		'<div><div class="text-sub text-sm">深度</div><div class="fw-500 mt-4">第' + n.depth + '层</div></div>' +
		'</div>' +
		'</div>' +
		(n.description ?
			'<div class="card mt-16">' +
			'<div class="card-title">描述</div>' +
			'<div class="text-sm" style="line-height:1.6;white-space:pre-wrap">' + escapeHtml(n.description) + '</div>' +
			'</div>' : '');
}


/* ============ 新增节点弹窗 ============ */
function openNodeModal() {
	document.getElementById('nodeId').value = '';
	document.getElementById('nodeType').value = 'root';
	document.getElementById('nodeType').disabled = false;
	document.getElementById('nodeParent').disabled = true;
	document.getElementById('nodeParent').value = '';
	document.getElementById('nodeName').value = '';
	document.getElementById('nodeDesc').value = '';
	document.getElementById('nodeOrder').value = '0';
	document.getElementById('nodeModalTitle').textContent = '新增节点';
	document.getElementById('parentRequired').style.display = 'none';
	document.getElementById('parentHint').textContent = '根节点无需上级节点；知识库分类从数据库动态获取';

	// 复用已加载的 nodeTree 构建父节点列表
	buildParentOptions(null);

	// 节点类型切换：根节点禁父节点，文件夹/叶子必选父节点
	document.getElementById('nodeType').onchange = function () {
		var ntype = document.getElementById('nodeType').value;
		var parentSel = document.getElementById('nodeParent');
		var reqStar = document.getElementById('parentRequired');
		var hint = document.getElementById('parentHint');
		if (ntype === 'root') {
			parentSel.disabled = true;
			parentSel.value = '';
			reqStar.style.display = 'none';
			hint.textContent = '根节点无需上级节点；知识库分类默认为"企业文档"';
		} else {
			parentSel.disabled = false;
			reqStar.style.display = 'inline';
			hint.textContent = '必须选择一个上级节点；知识库分类将从上级节点继承';
		}
	};
	showModal('nodeModal');
}

/* ============ 编辑节点弹窗 ============ */
function editNode(id) {
	api.getJson(NODE_API + '/' + id + '/').then(function (node) {
		document.getElementById('nodeId').value = node.id;
		document.getElementById('nodeType').value = node.node_type;
		document.getElementById('nodeName').value = node.name;
		document.getElementById('nodeDesc').value = node.description || '';
		document.getElementById('nodeOrder').value = node.order_no || 0;
		document.getElementById('nodeModalTitle').textContent = '编辑节点';

		// 编辑时不允许改节点类型和父节点
		document.getElementById('nodeType').disabled = true;
		document.getElementById('parentRequired').style.display = 'none';

		// 父节点改为只读文本展示
		buildParentOptions(node.parent_id);
		document.getElementById('nodeParent').disabled = true;

		document.getElementById('parentHint').textContent =
			'知识库分类：' + (ROOT_TYPE_MAP[node.root_type] || node.root_type);
		showModal('nodeModal');
	}).catch(function (e) {
		toast('加载节点详情失败: ' + e.message, 'error');
	});
}

/* 从 nodeTree 构建可选父节点列表（复用已加载数据；若未加载则回退 API 请求） */
function buildParentOptions(selectedParentId, cb) {
	var parentSelect = document.getElementById('nodeParent');
	var options = [];

	function flatten(items, depth) {
		items.forEach(function (n) {
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
		if (!selectedParentId) {
			html += '<option value="">— 无（作为根节点）—</option>';
		}
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
	var nodeType = document.getElementById('nodeType').value;
	var name = document.getElementById('nodeName').value.trim();
	var desc = document.getElementById('nodeDesc').value.trim();
	var orderNo = parseInt(document.getElementById('nodeOrder').value) || 0;

	if (!name) { toast('请输入节点名称', 'warning'); return; }

	// 文件夹/叶子节点必须选择上级节点
	if (nodeType !== 'root' && !parentId) {
		toast('文件夹和叶子节点必须选择上级节点', 'warning');
		return;
	}

	var body = {
		node_type: nodeType,
		name: name,
		description: desc,
		order_no: orderNo,
	};

	// 仅新建时发送 parent，编辑时不修改归属
	if (!id && parentId) {
		body.parent = parentId;
	}

	var method = id ? 'PATCH' : 'POST';
	var url = id ? NODE_API + '/' + id + '/' : NODE_API + '/';

	var promise = id ? api.patchJson(url, body) : api.postJson(url, body);
	promise.then(function () {
		closeModal('nodeModal');
		toast(id ? '节点已更新' : '节点已创建', 'success');
		selectedNodeId = null;
		loadTree();
		document.getElementById('nodeDetail').innerHTML =
			'<div style="padding:40px;text-align:center;color:var(--text-sub)">' +
			'<div style="font-size:48px;margin-bottom:12px">🗂️</div>' +
			'<div>请在左侧选择或创建一个节点</div></div>';
	}).catch(function (e) {
		toast('保存失败: ' + e.message, 'error');
	});
}

/* ============ 删除节点 ============ */
function deleteNode(id) {
	if (!confirm('确认删除该节点？\n\n注意：节点下存在子节点或文档时无法删除。')) return;

	api.deleteJson(NODE_API + '/' + id + '/').then(function () {
		toast('节点已删除', 'success');
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
