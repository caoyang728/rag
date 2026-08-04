/* ==========================================================
   知库 Agent · 系统配置页面 (admin-system-config.js)
   功能：加载 KV 配置项、按分类分组展示、行内编辑保存
   依赖：common.js（STATE/$/$$/toast/escapeHtml）、api.js、layout.js
   ========================================================== */

// 配置分类中文名 + 图标映射（与后端 SystemConfig.CATEGORY_CHOICES 对应）
const CATEGORY_MAP = {
	llm: { label: 'LLM 模型', icon: '🤖' },
	embedding: { label: 'Embedding/Rerank', icon: '🧮' },
	retrieval: { label: '检索参数', icon: '🔍' },
	storage: { label: '存储', icon: '💾' },
	email: { label: '邮件 SMTP', icon: '📧' },
	agent: { label: 'Agent', icon: '🧩' },
	security: { label: '安全', icon: '🛡️' },
	memory: { label: '记忆', icon: '🧠' },
	analytics: { label: 'Analytics', icon: '📊' },
	eval: { label: '评估', icon: '🎯' },
};

// 当前页面状态
let _allConfigs = {};       // 全部配置，按 category 分组：{ llm: [...], embedding: [...], ... }
let _currentCategory = '';  // 当前选中的 category
let _originalValues = {};   // 各配置项的原始值（用于对比变化和重置）：{ KEY: 'value', ... }
let _saving = false;        // 防止重复提交

/* ============ 初始化 ============ */
document.addEventListener('DOMContentLoaded', async () => {
	// 权限检查：仅超级管理员 / 维护管理员可访问
	if (!isSystemMaintainer()) {
		const tmpl = document.getElementById('tmpl-no-permission');
		document.querySelector('.layout').innerHTML = tmpl.innerHTML;
		return;
	}
	await loadConfigs();
});

/* ============ 加载配置列表 ============ */
async function loadConfigs() {
	try {
		const data = await api.getJson('/api/v1/system/configs/');
		_allConfigs = data.groups || {};
		// 缓存原始值用于对比变化和重置
		_originalValues = {};
		Object.values(_allConfigs).flat().forEach(c => {
			_originalValues[c.key] = c.value;
		});
		renderCategoryNav();
		// 默认选中第一个有数据的分类
		const firstCat = Object.keys(_allConfigs).find(k => _allConfigs[k].length > 0);
		if (firstCat) {
			selectCategory(firstCat);
		} else {
			$('#configList').innerHTML = '<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">暂无配置项</div></div>';
		}
	} catch (e) {
		console.error('load configs failed:', e);
		$('#configList').innerHTML = `<div class="empty"><div class="empty-icon">❌</div><div class="empty-text">加载失败：${escapeHtml(e.message)}</div></div>`;
	}
}

/* ============ 渲染分类导航 ============ */
function renderCategoryNav() {
	const navEl = $('#categoryNav');
	const cats = Object.keys(_allConfigs).sort();
	if (cats.length === 0) {
		navEl.innerHTML = '<div class="text-sub text-sm" style="padding:12px">暂无分类</div>';
		return;
	}
	navEl.innerHTML = cats.map(cat => {
		const info = CATEGORY_MAP[cat] || { label: cat, icon: '📁' };
		const count = _allConfigs[cat].length;
		const active = cat === _currentCategory ? 'active' : '';
		const itemTmpl = document.getElementById('tmpl-category-item').innerHTML;
		return itemTmpl
			.replace('__ACTIVE__', active)
			.replace(/__KEY__/g, cat)
			.replace('__ICON__', info.icon)
			.replace('__LABEL__', info.label)
			.replace('__COUNT__', count);
	}).join('');
}

/* ============ 选择分类 ============ */
function selectCategory(cat) {
	_currentCategory = cat;
	renderCategoryNav();
	renderConfigList(cat);
}

/* ============ 渲染配置项列表 ============ */
function renderConfigList(cat) {
	const listEl = $('#configList');
	const configs = _allConfigs[cat] || [];
	const info = CATEGORY_MAP[cat] || { label: cat };

	$('#currentCategoryName').textContent = info.label;
	$('#configCount').textContent = `（${configs.length} 项）`;

	if (configs.length === 0) {
		listEl.innerHTML = '<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">该分类下暂无配置项</div></div>';
		return;
	}

	const itemTmpl = document.getElementById('tmpl-config-item').innerHTML;
	listEl.innerHTML = configs.map(c => {
		// 只读 / 敏感 / 高风险标签直接构建，避免冗余的数组 + filter
		const badgeReadonly = c.is_readonly
			? '<span class="config-badge config-badge-readonly" title="修改需重建索引或影响路由，仅限 .env 修改">🔒 只读</span>'
			: '';
		const badgeSecret = c.is_secret
			? '<span class="config-badge config-badge-secret" title="敏感项，值已掩码">🔐 敏感</span>'
			: '';
		// 高风险项标识：变更需超管终审，提示用户该工单走二审流程
		const badgeRisk = c.risk_level === 'high'
			? '<span class="config-badge config-badge-risk" title="高风险项，工单需超管终审">⚠️ 高风险</span>'
			: '';

		// __KEY_ESC__ 用于 HTML 文本/属性值，__KEY_ATTR__ 用于 id/data-key 属性
		// 统一经 escapeHtml 处理，防止属性被引号闭合或注入脚本
		const keyEscaped = escapeHtml(c.key);
		return itemTmpl
			.replace(/__KEY_ESC__/g, keyEscaped)
			.replace(/__KEY_ATTR__/g, keyEscaped)
			.replace('__LABEL__', escapeHtml(c.label || c.key))
			.replace('__DESC__', escapeHtml(c.description || ''))
			.replace('__READONLY_CLASS__', c.is_readonly ? 'readonly' : '')
			.replace('__BADGE_READONLY__', badgeReadonly)
			.replace('__BADGE_SECRET__', badgeSecret)
			.replace('__BADGE_RISK__', badgeRisk)
			.replace('__CONTROL__', renderControl(c));
	}).join('');
}

/* ============ 渲染编辑控件（按 value_type）============ */
function renderControl(c) {
	const val = c.value || '';
	const disabled = c.is_readonly ? 'disabled' : '';
	// 有单位时在控件后面追加灰色标签
	const unitSuffix = c.unit ? `<span class="config-unit">${escapeHtml(c.unit)}</span>` : '';

	let control = '';
	if (c.value_type === 'bool') {
		const checked = val === 'true' ? 'checked' : '';
		control = `<label class="switch"><input type="checkbox" id="cfg-${c.key}" ${checked} ${disabled} onchange="onConfigChange('${c.key}')"><span class="slider"></span></label>`;
	} else if (c.value_type === 'int') {
		control = `<input type="number" class="input" id="cfg-${c.key}" value="${escapeHtml(val)}" step="1" ${disabled} oninput="onConfigChange('${c.key}')" style="max-width:200px">${unitSuffix}`;
	} else if (c.value_type === 'float') {
		control = `<input type="number" class="input" id="cfg-${c.key}" value="${escapeHtml(val)}" step="0.01" ${disabled} oninput="onConfigChange('${c.key}')" style="max-width:200px">${unitSuffix}`;
	} else if (c.value_type === 'json') {
		control = `<textarea class="input" id="cfg-${c.key}" rows="3" ${disabled} oninput="onConfigChange('${c.key}')">${escapeHtml(val)}</textarea>`;
	} else if (c.options && c.options.length > 0) {
		// 有可选值列表时渲染 select；description 含"多选"时渲染自定义多选组件
		const isMulti = c.description && c.description.includes('多选');
		if (isMulti) {
			// 自定义多选组件：紧凑显示 + 点击展开 + 搜索过滤 + 复选框
			control = renderMultiSelect(c);
		} else {
			const opts = c.options.map(o =>
				`<option value="${escapeHtml(o.value)}" ${o.value === val ? 'selected' : ''}>${escapeHtml(o.label)}</option>`
			).join('');
			control = `<select class="input" id="cfg-${c.key}" ${disabled} onchange="onConfigChange('${c.key}')" style="max-width:240px">${opts}</select>`;
		}
	} else {
		// string 或其他
		if (c.is_secret) {
			// 敏感项：显示掩码，需点击"修改"才能输入
			control = `
				<div id="cfg-secret-wrap-${c.key}">
					<div class="secret-edit-toggle" onclick="enableSecretEdit('${c.key}')">✏️ 点击修改</div>
					<input type="text" class="input" id="cfg-${c.key}" value="***" disabled style="max-width:480px">
				</div>`;
		} else {
			control = `<input type="text" class="input" id="cfg-${c.key}" value="${escapeHtml(val)}" ${disabled} oninput="onConfigChange('${c.key}')" style="max-width:480px">${unitSuffix}`;
		}
	}

	return control;
}

/* ============ 敏感项启用编辑 ============ */
function enableSecretEdit(key) {
	const wrap = $(`#cfg-secret-wrap-${key}`);
	if (!wrap) return;
	const origVal = _originalValues[key] || '';
	wrap.innerHTML = `
		<input type="text" class="input" id="cfg-${key}" value="${escapeHtml(origVal)}" oninput="onConfigChange('${key}')" style="max-width:480px" placeholder="输入新值">`;
	// 聚焦并选中
	const input = $(`#cfg-${key}`);
	input.focus();
	input.select();
	// 标记为已修改
	onConfigChange(key);
}

/* ============ 监听配置项变化 ============ */
function onConfigChange(key) {
	const input = $(`#cfg-${key}`);
	if (!input) return;
	const saveBtn = document.querySelector(`#cfg-row-${key} .btn-save`);
	if (!saveBtn) return;
	// 对比当前值与原始值，决定是否启用保存按钮
	const currentVal = getControlValue(key);
	const origVal = _originalValues[key] || '';
	const changed = currentVal !== origVal;
	saveBtn.disabled = !changed;
}

/* ============ 自定义多选组件（紧凑显示 + 搜索过滤 + 复选框）============ */
function renderMultiSelect(c) {
	const val = c.value || '';
	const disabled = c.is_readonly ? 'disabled' : '';
	const selectedVals = val ? val.split(',').map(v => v.trim()).filter(Boolean) : [];
	const totalOptions = c.options.length;
	// 紧凑显示：选中数量或 "全部"（空值=全部）
	const displayText = selectedVals.length === 0
		? `全部表（共 ${totalOptions}）`
		: `已选 ${selectedVals.length} 项 / ${totalOptions}`;

	return `
		<div class="multi-select-wrap" id="ms-wrap-${c.key}">
			<input type="hidden" id="cfg-${c.key}" value="${escapeHtml(val)}" />
			<div class="multi-select-display" id="ms-display-${c.key}" ${disabled}
				 onclick="toggleMultiSelect('${c.key}')">
				<span class="multi-select-text">${displayText}</span>
				<span class="multi-select-arrow">▾</span>
			</div>
			<div class="multi-select-dropdown" id="ms-dropdown-${c.key}" style="display:none">
				<div class="multi-select-search">
					<input type="text" class="input" placeholder="搜索表名..."
						   id="ms-search-${c.key}" oninput="filterMultiSelect('${c.key}')">
				</div>
				<div class="multi-select-actions">
					<button type="button" onclick="selectAllMulti('${c.key}', true)">全选</button>
					<button type="button" onclick="selectAllMulti('${c.key}', false)">清空</button>
				</div>
				<div class="multi-select-list" id="ms-list-${c.key}">
					${c.options.map(o => {
						const checked = selectedVals.includes(o.value) ? 'checked' : '';
						return `<label class="ms-item"><input type="checkbox" value="${escapeHtml(o.value)}" ${checked} onchange="onMultiSelectChange('${c.key}')"><span>${escapeHtml(o.label)}</span></label>`;
					}).join('')}
				</div>
			</div>
		</div>`;
}

function toggleMultiSelect(key) {
	const dropdown = $(`#ms-dropdown-${key}`);
	if (!dropdown) return;
	if (dropdown.style.display === 'none') {
		dropdown.style.display = 'block';
		// 聚焦搜索框
		setTimeout(() => {
			const search = $(`#ms-search-${key}`);
			if (search) search.focus();
		}, 0);
	} else {
		dropdown.style.display = 'none';
	}
}

function filterMultiSelect(key) {
	const keyword = $(`#ms-search-${key}`).value.toLowerCase();
	const items = document.querySelectorAll(`#ms-list-${key} .ms-item`);
	items.forEach(item => {
		const text = item.textContent.toLowerCase();
		item.style.display = text.includes(keyword) ? '' : 'none';
	});
}

function selectAllMulti(key, selectAll) {
	const checkboxes = document.querySelectorAll(`#ms-list-${key} input[type="checkbox"]`);
	checkboxes.forEach(cb => {
		// 过滤掉搜索隐藏的项
		const item = cb.closest('.ms-item');
		if (item.style.display !== 'none') {
			cb.checked = selectAll;
		}
	});
	onMultiSelectChange(key);
}

function onMultiSelectChange(key) {
	// 收集选中的值
	const checkboxes = document.querySelectorAll(`#ms-list-${key} input[type="checkbox"]`);
	const selected = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
	// 更新 hidden input 的值
	const hidden = $(`#cfg-${key}`);
	if (hidden) hidden.value = selected.join(',');
	// 更新显示文本
	const displayText = $(`#ms-display-${key} .multi-select-text`);
	if (displayText) {
		const total = checkboxes.length;
		displayText.textContent = selected.length === 0
			? `全部表（共 ${total}）`
			: `已选 ${selected.length} 项 / ${total}`;
	}
	onConfigChange(key);
}

// 点击外部关闭多选下拉框
document.addEventListener('click', (e) => {
	const wrap = e.target.closest('.multi-select-wrap');
	if (wrap) return;
	document.querySelectorAll('.multi-select-dropdown').forEach(dd => {
		dd.style.display = 'none';
	});
});

/* ============ 获取控件当前值 ============ */
function getControlValue(key) {
	const input = $(`#cfg-${key}`);
	if (!input) return '';
	if (input.type === 'checkbox') {
		return input.checked ? 'true' : 'false';
	}
	return input.value;
}

/* ============ 提交变更工单（替代原 saveConfig 直改）============
 * 配置修改不再直接落库，而是创建一份 ConfigChangeTicket 等待审批：
 * - 普通项：一审通过后生效
 * - 高风险项：一审 + 超管终审通过后生效
 * 提交时需填写变更原因，便于审批人判断是否通过。
 */
async function submitTicket(key) {
	if (_saving) return;
	const config = findConfig(key);
	if (!config) {
		toast('配置项不存在', 'error');
		return;
	}

	// 获取当前控件值，敏感项未启用编辑时跳过（值为 *** 不允许提交）
	const currentValue = getControlValue(key);
	if (config.is_secret && currentValue === '***') {
		toast('敏感项请先点击"修改"输入新值', 'error');
		return;
	}

	// 弹出确认框填写变更原因（必填），二次确认避免误提交
	showConfirmDialog({
		title: '提交配置变更工单',
		bannerType: 'info',
		bannerIcon: '📝',
		bannerText: `配置项：${config.label || key}（${key}）${config.risk_level === 'high' ? '，⚠️ 高风险项需超管终审' : ''}`,
		bodyHtml: `<div class="form-item" style="margin-top:12px">
			<label class="form-label">变更原因 <span class="required">*</span></label>
			<textarea id="ticketReasonInput" class="input" rows="3" placeholder="请说明本次配置变更的原因，便于审批人判断" style="max-width:100%"></textarea>
		</div>`,
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '提交工单',
				type: 'primary',
				onClick: async (ctx) => {
					const reason = $('#ticketReasonInput').value.trim();
					if (!reason) {
						ctx.setError('请填写变更原因');
						return;
					}
					try {
						_saving = true;
						// 提交工单：POST /api/v1/system/config-tickets/
						const ticket = await api.postJson('/api/v1/system/config-tickets/', {
							config_key: key,
							new_value: currentValue,
							reason: reason,
						});
						ctx.close();
						// 提交成功后恢复控件为原值（工单未通过前配置未变）
						resetConfig(key);
						toast(`工单已提交（#${ticket.id}），等待审批`, 'success');
					} catch (e) {
						ctx.setError(`提交失败：${e.message}`);
					} finally {
						_saving = false;
					}
				}
			}
		],
		onShow: (ctx) => {
			// 自动聚焦原因输入框
			const input = ctx.el.querySelector('#ticketReasonInput');
			if (input) input.focus();
		}
	});
}

/* ============ 重置配置（恢复当前值）============ */
function resetConfig(key) {
	const config = findConfig(key);
	if (!config) return;
	const controlEl = $(`#cfg-control-${key}`);
	if (!controlEl) return;
	// 重新渲染控件（恢复原始值）
	controlEl.innerHTML = renderControl(config);
	// 禁用保存按钮
	const saveBtn = document.querySelector(`#cfg-row-${key} .btn-save`);
	if (saveBtn) saveBtn.disabled = true;
}

/* ============ 查找配置对象 ============ */
function findConfig(key) {
	for (const cat of Object.keys(_allConfigs)) {
		const found = _allConfigs[cat].find(c => c.key === key);
		if (found) return found;
	}
	return null;
}

/* ==========================================================
   模型管理弹窗
   - openModelModal()    打开弹窗并加载模型列表
   - loadModels()        拉取后端按 model_type 分组的模型数据
   - switchModelTab(t)   切换 LLM / Embedding / Rerank tab
   - showModelForm(m)    显示新增/编辑表单（m=null 为新增）
   - saveModel()         提交新增/编辑
   - deleteModel(id)     删除指定模型
   - closeModelModal()   关闭模型管理弹窗
   ========================================================== */

// 模型类型中文名映射，用于 tab 切换与空态提示
const MODEL_TYPE_LABELS = {
	llm: 'LLM 对话模型',
	embedding: 'Embedding 向量模型',
	rerank: 'Rerank 重排序模型',
};

// 模型管理弹窗状态
let _modelGroups = {};          // 后端返回的按 model_type 分组的模型列表
let _currentModelType = 'llm';  // 当前选中的 tab 类型
let _modelSaving = false;       // 防止重复提交

/* ============ 打开模型管理弹窗 ============ */
async function openModelModal() {
	showModal('modelManageModal');
	// 默认选中 LLM tab
	switchModelTab('llm');
	await loadModels();
}

/* ============ 关闭模型管理弹窗 ============ */
function closeModelModal() {
	closeModal('modelManageModal');
	// 同时关闭可能残留的表单弹窗，避免下次打开时表单还在
	closeModelForm();
}

/* ============ 加载模型列表 ============ */
async function loadModels() {
	const tbody = $('#modelTableBody');
	if (!tbody) return;
	tbody.innerHTML = '<tr><td colspan="7" class="model-empty">加载中...</td></tr>';
	try {
		const data = await api.getJson('/api/v1/system/llm-models/');
		_modelGroups = data.groups || {};
		renderModelTable();
	} catch (e) {
		tbody.innerHTML = `<tr><td colspan="7" class="model-empty">加载失败：${escapeHtml(e.message)}</td></tr>`;
	}
}

/* ============ 切换类型 tab ============ */
function switchModelTab(type) {
	_currentModelType = type;
	// 更新 tab 激活态
	$$('.model-tab').forEach(btn => {
		btn.classList.toggle('active', btn.dataset.type === type);
	});
	// 仅在已加载数据后渲染，未加载时 loadModels 会触发渲染
	if (Object.keys(_modelGroups).length > 0) {
		renderModelTable();
	}
}

/* ============ 渲染模型表格 ============ */
function renderModelTable() {
	const tbody = $('#modelTableBody');
	if (!tbody) return;
	const list = _modelGroups[_currentModelType] || [];
	if (list.length === 0) {
		const label = MODEL_TYPE_LABELS[_currentModelType] || '';
		tbody.innerHTML = `<tr><td colspan="7" class="model-empty">暂无${escapeHtml(label)}，点击左下角"新增模型"添加</td></tr>`;
		return;
	}
	tbody.innerHTML = list.map(m => {
		// 状态徽标：启用绿色 / 停用红色
		const status = m.is_active
			? '<span class="model-status on">● 启用</span>'
			: '<span class="model-status off">○ 停用</span>';
		// name/provider/model_name/base_url 可能含特殊字符，统一转义防 XSS
		// 单元格用 title 保留完整内容，悬浮可查看被省略的长文本
		return `<tr>
			<td title="${escapeHtml(m.name)}">${escapeHtml(m.name)}</td>
			<td title="${escapeHtml(m.provider)}">${escapeHtml(m.provider)}</td>
			<td title="${escapeHtml(m.base_url)}">${escapeHtml(m.base_url || '-')}</td>
			<td title="${escapeHtml(m.model_name)}">${escapeHtml(m.model_name)}</td>
		<td>${m.timeout != null ? m.timeout + '秒' : '-'}</td>
		<td>${status}</td>
			<td>
				<div class="model-row-actions">
					<button class="btn btn-sm btn-outline" onclick="showModelForm(${m.id})">编辑</button>
					<button class="btn btn-sm btn-danger" onclick="deleteModel(${m.id})">删除</button>
				</div>
			</td>
		</tr>`;
	}).join('');
}

/* ============ 根据 id 查找模型对象 ============ */
function findModel(id) {
	for (const type of Object.keys(_modelGroups)) {
		const found = _modelGroups[type].find(m => m.id === id);
		if (found) return found;
	}
	return null;
}

/* ============ 显示新增/编辑表单 ============ */
function showModelForm(id) {
	const isEdit = id != null && id !== '';
	const m = isEdit ? findModel(id) : null;
	// 编辑时若找不到对象（已被删除等），直接忽略
	if (isEdit && !m) {
		toast('模型不存在，可能已被删除', 'error');
		return;
	}
	// 一次性缓存所有表单字段引用，避免 8 次全局 querySelector 扫描
	const f = {
		title:       $('#modelFormTitle'),
		id:          $('#modelFormFieldId'),
		name:        $('#modelFormFieldName'),
		provider:    $('#modelFormFieldProvider'),
		type:        $('#modelFormFieldType'),
		base_url:    $('#modelFormFieldBaseUrl'),
		model_name:  $('#modelFormFieldModelName'),
		timeout:     $('#modelFormFieldTimeout'),
		active:      $('#modelFormFieldActive'),
	};
	f.title.textContent = isEdit ? '编辑模型' : '新增模型';
	f.id.value          = isEdit ? m.id : '';
	f.name.value        = isEdit ? m.name : '';
	f.provider.value    = isEdit ? m.provider : '';
	f.type.value        = isEdit ? m.model_type : _currentModelType;
	f.base_url.value    = isEdit ? (m.base_url || '') : '';
	f.model_name.value  = isEdit ? m.model_name : '';
	f.timeout.value     = isEdit ? (m.timeout != null ? m.timeout : '') : '';
	f.active.checked    = isEdit ? m.is_active : true;
	showModal('modelFormModal');
}

/* ============ 关闭表单弹窗 ============ */
function closeModelForm() {
	closeModal('modelFormModal');
}

/* ============ 保存模型（新增/编辑）============ */
async function saveModel() {
	if (_modelSaving) return;
	// 一次性取表单字段，带空引用保护；DOM 不存在时友好提示而不是抛 TypeError
	const f = {
		name:     $('#modelFormFieldName'),
		provider: $('#modelFormFieldProvider'),
		type:     $('#modelFormFieldType'),
		base_url: $('#modelFormFieldBaseUrl'),
		model:    $('#modelFormFieldModelName'),
		timeout:  $('#modelFormFieldTimeout'),
		active:   $('#modelFormFieldActive'),
		id:       $('#modelFormFieldId'),
	};
	const missing = Object.entries(f).find(([, el]) => !el);
	if (missing) {
		toast(`表单元素缺失: ${missing[0]}，请刷新页面重试`, 'error');
		return;
	}
	const name = f.name.value.trim();
	const provider = f.provider.value.trim();
	const model_type = f.type.value;
	const model_name = f.model.value.trim();
	if (!name) { toast('请填写显示名称', 'error'); return; }
	if (!provider) { toast('请填写提供商', 'error'); return; }
	if (!model_name) { toast('请填写模型标识', 'error'); return; }

	const payload = {
		name,
		provider,
		model_type,
		base_url: f.base_url.value.trim(),
		model_name,
		// 超时为空时传 null，后端存 None，业务读取时回退到全局 LLM_TIMEOUT
		timeout: f.timeout.value.trim() || null,
		is_active: f.active.checked,
	};
	const id = f.id.value;
	_modelSaving = true;
	try {
		if (id) {
			await api.patchJson(`/api/v1/system/llm-models/${id}/`, payload);
			toast('模型已更新', 'success');
		} else {
			await api.postJson('/api/v1/system/llm-models/', payload);
			toast('模型已新增', 'success');
		}
		closeModelForm();
		// 刷新列表，跳回新增模型所在类型的 tab，便于用户确认结果
		await loadModels();
		// 新增/编辑后切到该模型所在 tab
		switchModelTab(payload.model_type);
	} catch (e) {
		toast(`保存失败：${e.message}`, 'error');
	} finally {
		_modelSaving = false;
	}
}

/* ============ 删除模型 ============ */
function deleteModel(id) {
	const m = findModel(id);
	if (!m) {
		toast('模型不存在，可能已被删除', 'error');
		return;
	}
	// 复用 common.js 的二次确认弹窗，避免误删
	showConfirmDialog({
		title: '删除模型',
		bannerText: `确认删除模型「${m.name}」吗？此操作不可恢复。`,
		bannerType: 'danger',
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认删除',
				type: 'danger',
				onClick: async (ctx) => {
					try {
						await api.deleteJson(`/api/v1/system/llm-models/${id}/`);
						ctx.close();
						toast('模型已删除', 'success');
						await loadModels();
					} catch (e) {
						// 删除失败保留确认弹窗，提示错误原因
						ctx.setError(`删除失败：${e.message}`);
					}
				}
			}
		]
	});
}

/* ==========================================================
   配置变更工单列表
   - openTicketModal()    打开工单列表弹窗并加载
   - closeTicketModal()   关闭弹窗
   - switchTicketTab(s)   切换状态筛选 tab
   - loadTickets()        拉取后端工单列表（带状态筛选）
   - renderTicketList()   渲染工单卡片
   - toggleTicketDetail(id)  展开/收起工单详情 + 审批操作
   - approveTicket(id)    审批通过
   - rejectTicket(id)     驳回
   - withdrawTicket(id)   撤回
   ========================================================== */

// 工单状态中文名映射，用于 tab 与卡片状态徽标
const TICKET_STATUS_LABELS = {
	pending: '待审批',
	first_approved: '待终审',
	approved: '已通过',
	rejected: '已驳回',
	withdrawn: '已撤回',
};

// 工单列表弹窗状态
let _ticketStatus = 'all';   // 当前选中的状态筛选
let _tickets = [];           // 当前加载的工单列表

/* ============ 打开工单列表弹窗 ============ */
async function openTicketModal() {
	showModal('ticketListModal');
	// 默认选中"全部" tab
	switchTicketTab('all');
	await loadTickets();
}

/* ============ 关闭工单列表弹窗 ============ */
function closeTicketModal() {
	closeModal('ticketListModal');
}

/* ============ 切换状态筛选 tab ============ */
function switchTicketTab(status) {
	_ticketStatus = status;
	$$('.ticket-tab').forEach(btn => {
		btn.classList.toggle('active', btn.dataset.status === status);
	});
	// 仅在已加载数据后渲染，避免空数据覆盖"加载中"提示
	if (_tickets.length > 0 || status !== 'all') {
		renderTicketList();
	}
}

/* ============ 加载工单列表 ============ */
async function loadTickets() {
	const body = $('#ticketListBody');
	if (!body) return;
	body.innerHTML = '<div class="ticket-empty">加载中...</div>';
	try {
		// 带 status 筛选参数：all 时不传，让后端返回全部
		const url = _ticketStatus && _ticketStatus !== 'all'
			? `/api/v1/system/config-tickets/?status=${_ticketStatus}`
			: '/api/v1/system/config-tickets/';
		const data = await api.getJson(url);
		_tickets = data.tickets || [];
		renderTicketList();
	} catch (e) {
		body.innerHTML = `<div class="ticket-empty">加载失败：${escapeHtml(e.message)}</div>`;
	}
}

/* ============ 渲染工单列表 ============ */
function renderTicketList() {
	const body = $('#ticketListBody');
	if (!body) return;
	if (_tickets.length === 0) {
		body.innerHTML = '<div class="ticket-empty">暂无工单</div>';
		return;
	}
	// 当前登录用户名，用于判断是否为工单创建人（决定是否显示"撤回"按钮）
	const currentUsername = getCurrentUsername();
	body.innerHTML = _tickets.map(t => renderTicketCard(t, currentUsername)).join('');
}

/* ============ 渲染单个工单卡片 ============ */
function renderTicketCard(t, currentUsername) {
	const statusLabel = TICKET_STATUS_LABELS[t.status] || t.status;
	// 状态颜色：待办类橙/红，已通过绿，已驳回红，已撤回灰
	const statusClass = {
		pending: 'ticket-status-pending',
		first_approved: 'ticket-status-first',
		approved: 'ticket-status-approved',
		rejected: 'ticket-status-rejected',
		withdrawn: 'ticket-status-withdrawn',
	}[t.status] || '';
	// 高风险项特殊标识，提示审批人需走二审流程
	const riskBadge = t.risk_level === 'high'
		? '<span class="ticket-badge ticket-badge-risk">⚠️ 高风险</span>'
		: '';
	// 创建人是否为当前用户，决定是否显示"撤回"按钮
	const isCreator = t.creator && t.creator === currentUsername;
	// 是否可审批：状态为待审批/待终审，且当前用户不是创建人（防自审）
	const canApprove = (t.status === 'pending' || t.status === 'first_approved') && !isCreator;
	// 待终审状态仅超管可审批
	const isSuperAdmin = isSuperAdminRole();
	const canSuperReview = t.status === 'first_approved' && isSuperAdmin;
	// 撤回：仅创建人可操作，且状态为待审批/待终审
	const canWithdraw = isCreator && (t.status === 'pending' || t.status === 'first_approved');

	return `<div class="ticket-card" id="ticket-card-${t.id}">
		<div class="ticket-card-header" onclick="toggleTicketDetail(${t.id})">
			<div class="ticket-card-title">
				<span class="ticket-config-label">${escapeHtml(t.config_label || t.config_key)}</span>
				<span class="ticket-config-key">${escapeHtml(t.config_key)}</span>
				${riskBadge}
				<span class="ticket-status ${statusClass}">${escapeHtml(statusLabel)}</span>
			</div>
			<div class="ticket-card-meta">
				<span class="ticket-value-diff">${escapeHtml(t.old_value)} → ${escapeHtml(t.new_value)}</span>
				<span class="ticket-creator">${escapeHtml(t.creator || '-')}</span>
				<span class="ticket-time">${formatDate(t.created_at)}</span>
				<span class="ticket-toggle">▾</span>
			</div>
		</div>
		<div class="ticket-card-detail" id="ticket-detail-${t.id}" style="display:none">
		<div class="ticket-detail-row">
			<span class="ticket-detail-label">变更原因：</span>
			<span class="ticket-detail-value">${escapeHtml(t.reason || '-')}</span>
		</div>
		${renderChangeSummary(t.change_summary)}
		${t.review_comment ? `<div class="ticket-detail-row"><span class="ticket-detail-label">一审意见：</span><span class="ticket-detail-value">${escapeHtml(t.review_comment)}</span></div>` : ''}
		${t.reviewer ? `<div class="ticket-detail-row"><span class="ticket-detail-label">一审人：</span><span class="ticket-detail-value">${escapeHtml(t.reviewer)}（${formatDate(t.reviewed_at)}）</span></div>` : ''}
		${t.super_admin_comment ? `<div class="ticket-detail-row"><span class="ticket-detail-label">超管意见：</span><span class="ticket-detail-value">${escapeHtml(t.super_admin_comment)}</span></div>` : ''}
		${t.super_admin_reviewer ? `<div class="ticket-detail-row"><span class="ticket-detail-label">超管终审：</span><span class="ticket-detail-value">${escapeHtml(t.super_admin_reviewer)}（${formatDate(t.super_admin_reviewed_at)}）</span></div>` : ''}
		${t.applied_at ? `<div class="ticket-detail-row"><span class="ticket-detail-label">生效时间：</span><span class="ticket-detail-value">${formatDate(t.applied_at)}</span></div>` : ''}
		${(canApprove || canSuperReview || canWithdraw) ? `
		<div class="ticket-actions">
			${(canApprove || canSuperReview) ? `<button class="btn btn-sm btn-primary" onclick="approveTicket(${t.id})">审批通过</button>` : ''}
			${(canApprove || canSuperReview) ? `<button class="btn btn-sm btn-danger" onclick="rejectTicket(${t.id})">驳回</button>` : ''}
			${canWithdraw ? `<button class="btn btn-sm btn-outline" onclick="withdrawTicket(${t.id})">撤回</button>` : ''}
		</div>` : ''}
	</div>
	</div>`;
}

/* ============ 展开/收起工单详情 ============ */
function toggleTicketDetail(id) {
	const detail = $(`#ticket-detail-${id}`);
	if (!detail) return;
	detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
	// 切换箭头方向，便于用户感知展开状态
	const toggle = $(`#ticket-card-${id} .ticket-toggle`);
	if (toggle) toggle.textContent = detail.style.display === 'none' ? '▾' : '▴';
}

/* ============ 渲染变更摘要（多值类配置的差异）============
 * 仅 BUSINESS_DB_TABLES 等多值配置工单携带 change_summary {added:[], removed:[]}
 * 单值配置 change_summary 为 null，不渲染该行，避免噪声
 */
function renderChangeSummary(summary) {
	if (!summary || (!summary.added?.length && !summary.removed?.length)) return '';
	const addedHtml = summary.added?.length
		? `<div class="change-summary-item change-summary-added">+ 新增：${summary.added.map(v => `<code>${escapeHtml(v)}</code>`).join(' ')}</div>`
		: '';
	const removedHtml = summary.removed?.length
		? `<div class="change-summary-item change-summary-removed">- 移除：${summary.removed.map(v => `<code>${escapeHtml(v)}</code>`).join(' ')}</div>`
		: '';
	return `<div class="ticket-detail-row ticket-change-summary">
		<span class="ticket-detail-label">变更摘要：</span>
		<span class="ticket-detail-value">${addedHtml}${removedHtml}</span>
	</div>`;
}

/* ============ 审批通过 ============ */
function approveTicket(id) {
	const t = _tickets.find(x => x.id === id);
	if (!t) return;
	// 高风险项待终审时提示仅超管可操作（前端预检，后端会再次校验）
	if (t.status === 'first_approved' && !isSuperAdminRole()) {
		toast('高风险项终审仅超级管理员可操作', 'error');
		return;
	}
	showConfirmDialog({
		title: '审批通过',
		bannerType: 'success',
		bannerIcon: '✓',
		bannerText: `确认通过工单 #${id}（${t.config_label || t.config_key}）？${t.risk_level === 'high' && t.status === 'pending' ? '该高风险项通过后将进入超管终审。' : t.status === 'first_approved' ? '该高风险项终审通过后配置将立即生效。' : '通过后配置将立即生效。'}`,
		bodyHtml: `<div class="form-item" style="margin-top:12px">
			<label class="form-label">审批意见（可选）</label>
			<textarea id="approveCommentInput" class="input" rows="2" placeholder="填写审批意见" style="max-width:100%"></textarea>
		</div>`,
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认通过',
				type: 'primary',
				onClick: async (ctx) => {
					try {
						const comment = $('#approveCommentInput').value.trim();
						await api.postJson(`/api/v1/system/config-tickets/${id}/approve/`, { comment });
						ctx.close();
						toast('审批通过', 'success');
						await loadTickets();
						// 工单生效后刷新配置列表，让前端展示最新值
						await loadConfigs();
					} catch (e) {
						ctx.setError(`审批失败：${e.message}`);
					}
				}
			}
		]
	});
}

/* ============ 驳回 ============ */
function rejectTicket(id) {
	const t = _tickets.find(x => x.id === id);
	if (!t) return;
	if (t.status === 'first_approved' && !isSuperAdminRole()) {
		toast('高风险项终审仅超级管理员可操作', 'error');
		return;
	}
	showConfirmDialog({
		title: '驳回工单',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: `确认驳回工单 #${id}（${t.config_label || t.config_key}）？`,
		bodyHtml: `<div class="form-item" style="margin-top:12px">
			<label class="form-label">驳回原因 <span class="required">*</span></label>
			<textarea id="rejectCommentInput" class="input" rows="2" placeholder="请填写驳回原因" style="max-width:100%"></textarea>
		</div>`,
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认驳回',
				type: 'danger',
				onClick: async (ctx) => {
					const comment = $('#rejectCommentInput').value.trim();
					if (!comment) {
						ctx.setError('请填写驳回原因');
						return;
					}
					try {
						await api.postJson(`/api/v1/system/config-tickets/${id}/reject/`, { comment });
						ctx.close();
						toast('已驳回', 'success');
						await loadTickets();
					} catch (e) {
						ctx.setError(`驳回失败：${e.message}`);
					}
				}
			}
		]
	});
}

/* ============ 撤回（仅创建人）============ */
function withdrawTicket(id) {
	showConfirmDialog({
		title: '撤回工单',
		bannerType: 'info',
		bannerIcon: '↩',
		bannerText: `确认撤回工单 #${id}？撤回后该工单将作废。`,
		bodyHtml: `<div class="form-item" style="margin-top:12px">
			<label class="form-label">撤回原因（可选）</label>
			<textarea id="withdrawCommentInput" class="input" rows="2" placeholder="填写撤回原因" style="max-width:100%"></textarea>
		</div>`,
		buttons: [
			{ text: '取消', type: 'cancel' },
			{
				text: '确认撤回',
				type: 'primary',
				onClick: async (ctx) => {
					try {
						const comment = $('#withdrawCommentInput').value.trim();
						await api.postJson(`/api/v1/system/config-tickets/${id}/withdraw/`, { comment });
						ctx.close();
						toast('已撤回', 'success');
						await loadTickets();
					} catch (e) {
						ctx.setError(`撤回失败：${e.message}`);
					}
				}
			}
		]
	});
}

/* ============ 获取当前登录用户名 ============ */
function getCurrentUsername() {
	try {
		const u = JSON.parse(localStorage.getItem('rag_user') || '{}');
		return u.username || '';
	} catch (e) {
		return '';
	}
}

/* ============ 判断当前用户是否为超管（用于待终审工单的审批按钮可见性）============ */
function isSuperAdminRole() {
	return hasAnyRole('super_admin');
}
