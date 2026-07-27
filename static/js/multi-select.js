/* ============ 通用多选下拉组件 ============ */

/**
 * 切换多选下拉框的展开/收起
 * @param {string} id - multi-select容器的ID
 */
function toggleMultiSelect(id) {
	var trigger = document.getElementById(id).querySelector('.multi-select-trigger');
	if (trigger.classList.contains('disabled')) {
		return;
	}
	
	var panel = document.getElementById(id).querySelector('.multi-select-panel');
	
	// 关闭其他下拉框
	document.querySelectorAll('.multi-select-panel.show').forEach(function (p) {
		if (p !== panel) {
			p.classList.remove('show');
			p.parentElement.querySelector('.multi-select-trigger').classList.remove('open');
		}
	});
	
	panel.classList.toggle('show');
	trigger.classList.toggle('open');
	
	if (panel.classList.contains('show')) {
		// 点击外部关闭
		document.addEventListener('click', function closeHandler(e) {
			if (!e.target.closest('.multi-select')) {
				panel.classList.remove('show');
				trigger.classList.remove('open');
				document.removeEventListener('click', closeHandler);
			}
		});
	}
}

/**
 * 全选/取消全选指定面板中的所有选项
 * @param {string} panelId - multi-select-list的ID（如 'docVisDeptPanel' 或 'uploadTeamPanel'）
 */
function toggleMultiSelectAll(panelId) {
	var checkboxes = document.querySelectorAll('#' + panelId + ' input');
	var allChecked = Array.from(checkboxes).every(function (cb) { return cb.checked; });
	checkboxes.forEach(function (cb) { cb.checked = !allChecked; });
	
	// 根据panelId推断前缀和类型，自动调用正确的回调函数
	var prefix = '';
	var isDept = false;
	
	if (panelId.indexOf('docVis') === 0) {
		prefix = 'docVis';
		isDept = panelId.indexOf('Dept') >= 0;
	} else if (panelId.indexOf('upload') === 0) {
		prefix = 'upload';
		isDept = panelId.indexOf('Dept') >= 0;
	}
	
	if (prefix) {
		if (isDept) {
			// 部门面板：调用对应的OnDeptChange
			var onDeptChangeFn = window[prefix + 'OnDeptChange'];
			if (onDeptChangeFn && checkboxes.length > 0) {
				onDeptChangeFn(checkboxes[0]);
			}
		} else {
			// 团队面板：调用对应的OnTeamChange
			var onTeamChangeFn = window[prefix + 'OnTeamChange'];
			if (onTeamChangeFn && checkboxes.length > 0) {
				onTeamChangeFn(checkboxes[0]);
			}
		}
	}
}

/**
 * 创建部门/团队多选下拉组件的实例
 * @param {Object} options - 配置选项
 * @param {string} options.prefix - 前缀标识（如 'docVis' 或 'upload'）
 * @param {Array} options.deptList - 部门列表数据
 * @param {Array} options.teamList - 团队列表数据
 * @param {Function} options.onDeptChange - 部门变化回调
 * @param {Function} options.onTeamChange - 团队变化回调
 */
function createDeptTeamMultiSelect(options) {
	var prefix = options.prefix;
	var deptList = options.deptList || [];
	var teamList = options.teamList || [];
	var onDeptChange = options.onDeptChange || function () {};
	var onTeamChange = options.onTeamChange || function () {};
	
	// 当前搜索关键词
	var deptSearch = '';
	var teamSearch = '';
	
	function setDeptList(newList) {
		deptList = newList || [];
	}
	
	function setTeamList(newList) {
		teamList = newList || [];
	}
	
	/**
	 * 渲染部门列表
	 * @param {Array} selectedIds - 已选中的部门ID列表
	 */
	function renderDeptList(selectedIds) {
		var panel = document.getElementById(prefix + 'DeptPanel');
		if (!panel) return;
		
		if (deptList.length === 0) {
			panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-sub)">加载中...</div>';
			return;
		}
		
		selectedIds = selectedIds || [];
		
		// 根据搜索词过滤
		var filtered = deptList.filter(function (d) {
			return !deptSearch || d.name.toLowerCase().indexOf(deptSearch.toLowerCase()) >= 0;
		});
		
		if (filtered.length === 0) {
			panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-sub)">未找到匹配的部门</div>';
			return;
		}
		
		panel.innerHTML = filtered.map(function (d) {
			var checked = selectedIds.indexOf(d.id) >= 0 ? ' checked' : '';
			return '<div class="multi-select-option" onclick="window[\'' + prefix + 'OnOptionClick\'](this, event)">' +
				'<input type="checkbox" value="' + d.id + '"' + checked + ' onchange="window[\'' + prefix + 'OnDeptChange\'](this)">' +
				'<span class="multi-select-option-text">' + escapeHtml(d.name) + '</span>' +
				'</div>';
		}).join('');
		
		updateDeptCount();
	}
	
	/**
	 * 渲染团队列表
	 * @param {Array} selectedIds - 已选中的团队ID列表
	 * @param {Array} selectedDeptIds - 已选中的部门ID列表（用于筛选）
	 */
	function renderTeamList(selectedIds, selectedDeptIds) {
		var panel = document.getElementById(prefix + 'TeamPanel');
		if (!panel) return;
		
		if (teamList.length === 0) {
			panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-sub)">加载中...</div>';
			return;
		}
		
		selectedIds = selectedIds || [];
		selectedDeptIds = selectedDeptIds || [];
		
		// 根据选中的部门过滤团队（仅展示选中部门下的团队）
		if (selectedDeptIds.length === 0) {
			panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-sub)">请先选择部门</div>';
			updateTeamCount();
			return;
		}
		
		var filtered = teamList.filter(function (t) {
			var deptId = parseInt(t.department_id || t.department);
			var matchDept = selectedDeptIds.indexOf(deptId) >= 0;
			var matchSearch = !teamSearch || t.name.toLowerCase().indexOf(teamSearch.toLowerCase()) >= 0;
			return matchDept && matchSearch;
		});
		
		if (filtered.length === 0) {
			panel.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-sub)">该部门下没有团队</div>';
		} else {
			panel.innerHTML = filtered.map(function (t) {
				var checked = selectedIds.indexOf(parseInt(t.id)) >= 0 ? ' checked' : '';
				return '<div class="multi-select-option" onclick="window[\'' + prefix + 'OnOptionClick\'](this, event)">' +
					'<input type="checkbox" value="' + t.id + '"' + checked + ' onchange="window[\'' + prefix + 'OnTeamChange\'](this)">' +
					'<span class="multi-select-option-text">' + escapeHtml(t.name) + '</span>' +
					'</div>';
			}).join('');
		}
		
		updateTeamCount();
	}
	
	/**
	 * 部门选择变化处理
	 * @param {HTMLElement} checkbox - 触发变化的checkbox元素
	 */
	function onDeptChangeHandler(checkbox) {
		// 获取所有选中的部门ID
		var selectedDeptIds = [];
		document.querySelectorAll('#' + prefix + 'DeptPanel input:checked').forEach(function (cb) {
			selectedDeptIds.push(parseInt(cb.value));
		});
		
		// 更新部门下拉框计数
		updateDeptCount();
		
		// 获取当前已选中的团队ID（保持现有选择状态）
		var selectedTeamIds = [];
		document.querySelectorAll('#' + prefix + 'TeamPanel input:checked').forEach(function(cb) {
			selectedTeamIds.push(parseInt(cb.value));
		});
		
		// 重新渲染团队列表（仅展示选中部门下的团队）
		renderTeamList(selectedTeamIds, selectedDeptIds);
		
		// 更新团队下拉框的标签提示
		var teamTrigger = document.querySelector('#' + prefix + 'TeamSelect .multi-select-trigger');
		var teamLabel = teamTrigger.querySelector('.multi-select-label');
		if (selectedDeptIds.length > 0) {
			teamLabel.textContent = '请选择团队';
		} else {
			teamLabel.textContent = '请先选择部门';
		}
		
		// 调用外部回调
		onDeptChange(selectedDeptIds);
	}
	
	/**
	 * 团队选择变化处理
	 * @param {HTMLElement} checkbox - 触发变化的checkbox元素
	 */
	function onTeamChangeHandler(checkbox) {
		updateTeamCount();
		onTeamChange();
	}
	
	/**
	 * 更新部门下拉框计数
	 */
	function updateDeptCount() {
		var checkedCbs = document.querySelectorAll('#' + prefix + 'DeptPanel input:checked');
		var labelEl = document.querySelector('#' + prefix + 'DeptSelect .multi-select-label');
		
		if (checkedCbs.length > 0) {
			var names = Array.from(checkedCbs).map(function(cb) {
				var option = cb.closest('.multi-select-option');
				return option ? option.querySelector('.multi-select-option-text').textContent : '';
			}).filter(function(n) { return n; });
			
			var displayText = names.join('，');
			if (displayText.length > 30) {
				displayText = displayText.substring(0, 27) + '...';
			}
			labelEl.textContent = displayText;
		} else {
			labelEl.textContent = '请选择部门';
		}
	}
	
	/**
	 * 更新团队下拉框计数
	 */
	function updateTeamCount() {
		var checkedCbs = document.querySelectorAll('#' + prefix + 'TeamPanel input:checked');
		var labelEl = document.querySelector('#' + prefix + 'TeamSelect .multi-select-label');
		var teamTrigger = document.querySelector('#' + prefix + 'TeamSelect .multi-select-trigger');
		
		var deptCount = document.querySelectorAll('#' + prefix + 'DeptPanel input:checked').length;
		
		if (deptCount === 0) {
			labelEl.textContent = '请先选择部门';
			teamTrigger.classList.add('disabled');
		} else if (checkedCbs.length > 0) {
			var names = Array.from(checkedCbs).map(function(cb) {
				var option = cb.closest('.multi-select-option');
				return option ? option.querySelector('.multi-select-option-text').textContent : '';
			}).filter(function(n) { return n; });
			
			var displayText = names.join('，');
			if (displayText.length > 30) {
				displayText = displayText.substring(0, 27) + '...';
			}
			labelEl.textContent = displayText;
			teamTrigger.classList.remove('disabled');
		} else {
			labelEl.textContent = '请选择团队';
			teamTrigger.classList.remove('disabled');
		}
	}
	
	/**
	 * 部门搜索处理
	 * @param {HTMLElement} input - 搜索输入框
	 */
	function onDeptSearchHandler(input) {
		deptSearch = input.value.trim();
		var selectedIds = [];
		document.querySelectorAll('#' + prefix + 'DeptPanel input:checked').forEach(function (cb) {
			selectedIds.push(parseInt(cb.value));
		});
		renderDeptList(selectedIds);
	}
	
	/**
	 * 团队搜索处理
	 * @param {HTMLElement} input - 搜索输入框
	 */
	function onTeamSearchHandler(input) {
		teamSearch = input.value.trim();
		var selectedDeptIds = [];
		document.querySelectorAll('#' + prefix + 'DeptPanel input:checked').forEach(function (cb) {
			selectedDeptIds.push(parseInt(cb.value));
		});
		var selectedTeamIds = [];
		document.querySelectorAll('#' + prefix + 'TeamPanel input:checked').forEach(function (cb) {
			selectedTeamIds.push(parseInt(cb.value));
		});
		renderTeamList(selectedTeamIds, selectedDeptIds);
	}
	
	/**
	 * 点击选项行的处理（点击名字可以勾选和取消勾选）
	 * @param {HTMLElement} optionEl - 选项行元素
	 * @param {Event} e - 事件对象
	 */
	function onOptionClickHandler(optionEl, e) {
		// 如果点击的是checkbox本身，不处理（让checkbox自己处理勾选/取消）
		if (e && e.target && e.target.tagName === 'INPUT') {
			return;
		}
		
		var checkbox = optionEl.querySelector('input[type="checkbox"]');
		if (checkbox) {
			checkbox.checked = !checkbox.checked;
			// 触发对应的change事件
			if (optionEl.closest('#' + prefix + 'DeptPanel')) {
				onDeptChangeHandler(checkbox);
			} else if (optionEl.closest('#' + prefix + 'TeamPanel')) {
				onTeamChangeHandler(checkbox);
			}
		}
	}
	
	// 将方法挂载到window对象，以便HTML中调用
	window[prefix + 'RenderDeptList'] = renderDeptList;
	window[prefix + 'RenderTeamList'] = renderTeamList;
	window[prefix + 'OnDeptChange'] = onDeptChangeHandler;
	window[prefix + 'OnTeamChange'] = onTeamChangeHandler;
	window[prefix + 'OnDeptSearch'] = onDeptSearchHandler;
	window[prefix + 'OnTeamSearch'] = onTeamSearchHandler;
	window[prefix + 'OnOptionClick'] = onOptionClickHandler;
	
	return {
		renderDeptList: renderDeptList,
		renderTeamList: renderTeamList,
		updateDeptCount: updateDeptCount,
		updateTeamCount: updateTeamCount,
		setDeptList: setDeptList,
		setTeamList: setTeamList
	};
}
