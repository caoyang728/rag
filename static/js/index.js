/* ==========================================================
   首页工作台 (index.js)
   依赖：common.js（PAGE_MAP/escapeHtml）、layout.js（getSidebarGroups）
   功能入口卡片按角色分组动态渲染，与侧边栏菜单保持一致的可见性
   需在 common.js、api.js、layout.js 之后加载
   ========================================================== */
document.addEventListener('DOMContentLoaded', () => {
	const container = document.getElementById('homeSections');
	if (!container) return;

	container.innerHTML = getSidebarGroups().map(g => `
    <div class="home-section">
      <div class="home-section-title">${g.icon} ${g.group}</div>
      <div class="home-grid">
        ${g.items.filter(it => it.page).map(it => `
          <a class="home-card" href="${PAGE_MAP[it.page]}">
            <div class="home-card-icon">${it.icon}</div>
            <div class="home-card-info">
              <div class="home-card-title">${it.name}</div>
              <div class="home-card-desc">${escapeHtml(it.desc || '')}</div>
            </div>
          </a>
        `).join('')}
      </div>
    </div>
  `).join('');
});
