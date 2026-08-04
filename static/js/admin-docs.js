/* ============================================================================
 * admin-docs.js —— 文档审核页面
 *
 * 页面访问权限（前后端双重校验）：
 * - 仅 super_admin / kb_admin / dept_manager / team_leader 可见
 * - 普通用户直接跳回首页并提示无权限
 *
 * 功能模块：
 * 1. 待审文档列表：拉取待审核 / 待复核 文档
 * 2. 详情弹窗：展示文档元信息 + 摘要预览（本期仅元信息）
 * 3. 审核动作：通过（备注选填）/ 驳回（理由必填，支持 Ctrl+Enter 提交）
 * ============================================================================ */

// 当前正在审核的文档对象
let _currentDoc = null;
// 提交防重锁（防止审核通过/驳回重复提交）
let _submitting = false;

/* ============ 页面启动 ============ */
document.addEventListener('DOMContentLoaded', () => {
	// 页面级权限校验：仅管理角色可进入
	if (!_canAccessPage()) {
		toast('您没有权限访问文档审核', 'error');
		setTimeout(() => { window.location.href = '/chat/'; }, 800);
		return;
	}

	// 顶栏 / 侧栏 / 全局搜索 已由 common.js 的 DOMContentLoaded 注入，无需重复

	// 加载待审文档列表
	loadDocList();
});

/* ---------- 页面级权限判断 ---------- */
function _canAccessPage() {
	// 对齐需求：超级管理员、知识管理员、部门经理、团队组长可见
	return hasAnyRole('super_admin', 'kb_admin', 'dept_manager', 'team_leader');
}

/* ============================================================================
 * 待审文档 —— 列表加载
 * ============================================================================ */
function loadDocList() {
	const tbody = $('#docTable');
	tbody.innerHTML = `<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px">加载中...</td></tr>`;
	api.getJson('/api/v1/knowledge/documents/pending-audits/')
		.then(res => {
			const rows = res?.rows || [];
			if (!rows.length) {
				tbody.innerHTML = `<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px">暂无待审核文档</td></tr>`;
				return;
			}
			tbody.innerHTML = rows.map(_renderDocRow).join('');
			// 绑定行点击事件：打开审核详情弹窗
			tbody.querySelectorAll('[data-doc-id]').forEach(tr => {
				tr.addEventListener('click', () => {
					const id = +tr.getAttribute('data-doc-id');
					const data = rows.find(r => r.id === id);
					if (data) openDocModal(data);
				});
			});
		})
		.catch(err => {
			toast('加载待审核文档失败', 'error');
			console.error(err);
			tbody.innerHTML = `<tr><td colspan="8" class="text-sub text-sm text-center" style="padding:30px;color:var(--danger)">加载失败，请稍后重试</td></tr>`;
		});
}

function _renderDocRow(d) {
	// 密级映射：1=公开, 2=内部, 3=秘密, 4=绝密
	const secLvMap = { 1: '公开', 2: '内部', 3: '秘密', 4: '绝密' };
	const secBadge = { 1: '', 2: 'badge-info', 3: 'badge-warn', 4: 'badge-danger' }[d.secret_level] || '';
	// 审核阶段徽章：待审核 / 待复核
	const auditBadge = d.audit_status === 'pending_team'
		? '<span class="badge badge-warn">待审核</span>'
		: '<span class="badge badge-info">待复核</span>';
	const belong = [d.dept_name, d.team_name].filter(Boolean).join(' / ');
	return `
	<tr class="table-row-hover" data-doc-id="${d.id}" style="cursor:pointer">
		<td>
			<div class="flex items-center gap-8">
				<span style="font-size:16px">${_iconForFileType(d.file_type)}</span>
				<div>
					<div class="text-strong">${escapeHtml(d.title)}</div>
					<div class="text-sub text-xs">${escapeHtml(d.file_name || '')}</div>
				</div>
			</div>
		</td>
		<td class="text-sm">${escapeHtml(d.file_type || '—')}</td>
		<td>${secLvMap[d.secret_level] ? `<span class="badge ${secBadge}">${secLvMap[d.secret_level]}</span>` : '—'}</td>
		<td>
			<div>${escapeHtml(d.owner_name)}</div>
			<div class="text-sub text-xs">${escapeHtml(d.owner_email || '')}</div>
		</td>
		<td class="text-sm">${escapeHtml(belong || '—')}</td>
		<td>
			${auditBadge}
			<div class="text-sub text-xs mt-4">${escapeHtml(d.audit_step || '')}</div>
		</td>
		<td class="text-sm text-sub">${formatDate(d.created_at)}</td>
		<td>
			<button class="btn btn-sm btn-primary">处理</button>
		</td>
	</tr>`;
}

/* ============================================================================
 * 待审文档 —— 详情弹窗
 * ============================================================================ */
function openDocModal(d) {
	_currentDoc = d;
	$('#docModalTitle').textContent = '文档审核 · ' + (d.audit_status === 'pending_team' ? '团队组长审核' : '合规复核');
	// 可见性 / 密级 文案映射
	const visMap = { 1: '全局公开', 2: '部门内可见', 3: '团队内可见', 4: '私有' };
	const secLvMap = { 1: '公开', 2: '内部', 3: '秘密', 4: '绝密' };
	const belong = [d.dept_name, d.team_name].filter(Boolean).join(' / ');
	const fileSizeTxt = d.file_size ? formatFileSize(d.file_size) : '—';
	const isPendingTeam = d.audit_status === 'pending_team';
	// 取上传人姓名首字作为头像占位
	const avatarChar = (d.owner_name || '?').charAt(0).toUpperCase();

	$('#docModalBody').innerHTML = `
		<div class="applicant-card">
			<div class="applicant-avatar">${escapeHtml(avatarChar)}</div>
			<div class="applicant-info">
				<div class="applicant-name">${escapeHtml(d.owner_name)}</div>
				<div class="applicant-meta">${escapeHtml(d.owner_email || '')}</div>
			</div>
			<div class="applicant-time">
				<div class="applicant-time-label">上传时间</div>
				${formatDate(d.created_at)}
			</div>
		</div>

		<div class="detail-section-title">文档信息</div>
		<div class="detail-grid">
			<div class="detail-cell" style="grid-column:1/-1">
				<div class="detail-cell-label">文档标题</div>
				<div class="detail-cell-value">${escapeHtml(d.title)}</div>
				<div class="detail-cell-sub">${escapeHtml(d.file_name || '')}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">当前阶段</div>
				<div class="detail-cell-value">
					${isPendingTeam
						? '<span class="badge badge-warn">待审核（团队组长）</span>'
						: '<span class="badge badge-info">待复核（合规/部门经理）</span>'}
				</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">版本</div>
				<div class="detail-cell-value">v${d.version || 1}${d.version_tag ? ' · ' + escapeHtml(d.version_tag) : ''}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">文件类型</div>
				<div class="detail-cell-value">${escapeHtml(d.file_type || '—')} · ${fileSizeTxt}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">可见性</div>
				<div class="detail-cell-value">${visMap[d.visibility_level] || '—'}</div>
			</div>
			<div class="detail-cell">
				<div class="detail-cell-label">密级</div>
				<div class="detail-cell-value">${secLvMap[d.secret_level] || '—'}</div>
			</div>
			<div class="detail-cell" style="grid-column:1/-1">
				<div class="detail-cell-label">归属路径</div>
				<div class="detail-cell-value">
					${belong ? escapeHtml(belong) : '—'}
					${d.node_name ? ` <span class="detail-cell-sub">（节点：${escapeHtml(d.node_name)}）</span>` : ''}
				</div>
			</div>
		</div>

		<div class="flex mt-20">
			<a class="btn btn-sm btn-outline" href="/admin-nodes/?doc_id=${encodeURIComponent(d.uuid)}" target="_blank" rel="noopener">
				🔗 在知识库中查看
			</a>
		</div>

		<div class="detail-section-title">文档摘要</div>
		<div class="doc-summary-box">
			<div class="doc-summary-meta">
				<span>${_iconForFileType(d.file_type)}</span>
				<span class="text-strong">${escapeHtml(d.file_name || d.title)}</span>
				<span class="text-sub text-sm">${escapeHtml(d.file_type || '—')} · ${fileSizeTxt}</span>
			</div>
			<div class="doc-summary-tip">📄 完整内容预览功能开发中</div>
		</div>
	`;
	showModal('docModal');
}

/* ============================================================================
 * 审核动作 —— 通过 / 驳回
 * ============================================================================ */

/* ---------- 审核通过（二次确认，备注选填） ---------- */
function onDocApproveClick() {
	if (!_currentDoc) return;
	const d = _currentDoc;
	showConfirmDialog({
		title: '确认通过审核',
		bannerType: 'success',
		bannerIcon: '✓',
		bannerText: `确认通过文档《${d.title}》？`,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">审批意见<span class="form-hint-inline">（选填）</span></label>' +
			'<textarea id="confirmDialogComment" class="input" rows="3" placeholder="可填写备注说明，记录审批意见..."></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: '确认通过', type: 'primary', onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				ctx.close();
				_submitDocApprove(d.id, comment);
			}}
		]
	});
}

/* ---------- 审核驳回（二次确认，理由必填） ---------- */
function onDocRejectClick() {
	if (!_currentDoc) return;
	const d = _currentDoc;
	showConfirmDialog({
		title: '驳回理由',
		bannerType: 'danger',
		bannerIcon: '⚠',
		bannerText: `确认驳回文档《${d.title}》？驳回后需上传人重新提交。`,
		bodyHtml: '<div class="form-item mt-12">' +
			'<label class="form-label">驳回理由<span class="required">*</span></label>' +
			'<textarea id="confirmDialogComment" class="input" rows="4" placeholder="必填，请说明驳回原因，便于申请人了解问题..."></textarea>' +
			'</div>',
		buttons: [
			{ text: '取消', type: 'cancel', onClick: ctx => ctx.close() },
			{ text: '确认驳回', type: 'danger', onClick: ctx => {
				const comment = (ctx.el.querySelector('#confirmDialogComment')?.value || '').trim();
				// 驳回理由必填，空则拦截并提示
				if (!comment) { ctx.setError('驳回理由不能为空'); return; }
				ctx.close();
				_submitDocReject(d.id, comment);
			}}
		],
		onShow: ctx => {
			const ta = ctx.el.querySelector('#confirmDialogComment');
			if (ta) {
				ta.focus();
				// Ctrl/Cmd + Enter 快捷提交驳回
				ta.addEventListener('keydown', (e) => {
					if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
						ctx.el.querySelector('.btn-reject')?.click();
					}
				});
			}
		}
	});
}

/* ---------- 提交：文档通过 ---------- */
function _submitDocApprove(id, comment) {
	if (_submitting) return;
	_submitting = true;
	api.postJson(`/api/v1/knowledge/documents/${id}/audit-approve/`, { comment })
		.then(res => {
			if (res?.ok) {
				// 复核通过 → 已发布；审核通过 → 流转复核
				const nextLabel = res.audit_status === 'passed'
					? '审核通过（已发布）'
					: `审核通过，流转至：${_auditStatusLabel(res.audit_status)}`;
				toast(nextLabel, 'success');
				closeModal('docModal');
				_currentDoc = null;
				loadDocList();
			} else {
				toast(res?.detail || '审核失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '审核失败'), 'error');
		console.error(err);
	}).finally(() => { _submitting = false; });
}

/* ---------- 提交：文档驳回 ---------- */
function _submitDocReject(id, comment) {
	if (_submitting) return;
	_submitting = true;
	api.postJson(`/api/v1/knowledge/documents/${id}/audit-reject/`, { comment })
		.then(res => {
			if (res?.ok) {
				toast('文档已驳回', 'success');
				closeModal('docModal');
				_currentDoc = null;
				loadDocList();
			} else {
				toast(res?.detail || '驳回失败', 'error');
			}
	}).catch(err => {
		toast(_errMsg(err, '驳回失败'), 'error');
		console.error(err);
	}).finally(() => { _submitting = false; });
}

/* ============================================================================
 * 通用辅助
 * ============================================================================ */

/* ---------- 审核状态文案映射 ---------- */
function _auditStatusLabel(s) {
	return {
		'pending_team': '待审核',
		'pending_compliance': '待复核',
		'passed': '已通过',
		'rejected': '已驳回',
		'archived': '已归档',
		'deleted': '已删除',
	}[s] || s;
}

/* ---------- 文件类型 → emoji 图标 ---------- */
function _iconForFileType(ft) {
	const f = (ft || '').toLowerCase();
	if (f === 'pdf') return '📄';
	if (['doc', 'docx'].includes(f)) return '📝';
	if (['xls', 'xlsx'].includes(f)) return '📊';
	if (['ppt', 'pptx'].includes(f)) return '📽️';
	if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(f)) return '🖼️';
	if (['zip', 'rar', '7z', 'tar', 'gz'].includes(f)) return '🗜️';
	if (['txt', 'md'].includes(f)) return '📃';
	return '📁';
}
