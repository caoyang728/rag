/* ============ 管理后台共享方法（admin-rbac / admin-org） ============ */

// 转义字符串用于 onclick 属性中的单引号参数
// 注意：先转义反斜杠，再转义单引号和双引号
// 双引号用 &quot; 转义，防止破坏 HTML 属性的双引号边界（XSS 防护）
function escapeQuote(s) {
	return String(s || '')
		.replace(/\\/g, '\\\\')
		.replace(/'/g, "\\'")
		.replace(/"/g, '&quot;');
}
