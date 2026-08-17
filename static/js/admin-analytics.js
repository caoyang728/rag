/* ============ 反馈与准确率报表 ============ */

let currentTimeRange = '7d';
let customDateStart = null;
let customDateEnd = null;

let currentTab = 'overview';
let qaPage = 1;
let qaPageSize = 20;
let qaTotal = 0;

/* 日报趋势图的指标显示开关和缓存数据，勾选 checkbox 时更新开关并重渲染 */
let dailyTrendData = [];
let dailyMetricVisible = { qa: true, good: true, bad: true, accuracy: true };
/* 系统指标页延迟直方图 ECharts 实例：每次重渲染前须 dispose，避免重复 init 报错/内存泄漏 */
let sysMetricsHistChart = null;

document.addEventListener('DOMContentLoaded', () => {
	initAnalyticsPage();
});

async function initAnalyticsPage() {
	await loadRootTypes();
	// 默认日期预填（昨日 = 系统报表通常已就绪）
	const yesterday = new Date(Date.now() - 86400000);
	const yStr = yesterday.toISOString().slice(0, 10);
	const sDate = $('#systemMetricsDate');
	if (sDate) { sDate.value = yStr; sDate.max = yStr; }
	const oDate = $('#orgUsageDate');
	if (oDate) { oDate.value = yStr; oDate.max = yStr; }
	// 默认加载概览数据（今日实时 + 趋势；原 KPI 卡片指标已并入下方趋势折线图）
	loadRealtimeStrip();
	// 首屏同步趋势图标题（默认「近 7 天」），避免 HTML 写死值与实际时间范围不一致
	updateChartTitle(currentTimeRange);
	loadTrend();
	// 默认落在概览 Tab，启动今日实时轮询；页面切到后台时暂停，回前台立即刷新并恢复
	startRealtimePolling();
	document.addEventListener('visibilitychange', () => {
		if (document.hidden) {
			stopRealtimePolling();
		} else if (currentTab === 'overview') {
			loadRealtimeStrip(true);
			startRealtimePolling();
		}
	});
}

/* ====== Tab 切换 ====== */
function switchTab(name) {
	currentTab = name;
	$$('#analyticsTabs .tab-item').forEach(el => {
		el.classList.toggle('active', el.getAttribute('data-tab') === name);
	});
	$$('.tab-panel').forEach(p => {
		p.classList.toggle('active', p.getAttribute('data-panel') === name);
	});
	// 今日实时轮询仅概览 Tab 生效：切到概览启动，切走停止
	if (name === 'overview') {
		startRealtimePolling();
	} else {
		stopRealtimePolling();
	}
	// 切到对应 Tab 时加载该面板数据（每次切换均刷新，根节点筛选变更也走 reloadCurrentTab）
	// 概览 Tab 已由 loadRealtimeStrip 轮询 + 首屏 loadTrend 覆盖，切回时不重复加载
	if (name !== 'overview') loadTabData(name, false);
}

function reloadCurrentTab() {
	// 根节点切换时，按当前 Tab 懒加载对应数据（qa 分页重置回第 1 页）
	loadTabData(currentTab, true);
}

// Tab 数据懒加载分发：switchTab / reloadCurrentTab 共用，避免两份重复的 switch 分支
function loadTabData(name, resetQaPage) {
	switch (name) {
		case 'overview': loadTrend(); break;
		case 'system': loadSystemMetrics(); break;
		case 'queue': loadQueueDepth(); break;
		case 'org': loadOrgUsage(); break;
		case 'qa':
			if (resetQaPage) qaPage = 1;
			loadQaRecords();
			break;
		case 'daily': loadDailyReport(); break;
		case 'tools':
			loadKeywords();
			loadBadFeedbacks();
			loadFeedbackLoopAggs();
			break;
	}
}

/* ============ 双轴折线趋势图组件 TrendChart（ECharts 版） ============ */
/**
 * ECharts 双轴折线趋势图（含图例勾选框），仅本页面使用，故放在页面内而非 common.js。
 * 依赖：/static/vendor/echarts.min.js（已在本页引入，defer 加载，DOMContentLoaded 前就绪）。
 * 布局结构（.chart-row 横向排列）：
 *   div.chart-row
 *     div.chart-sidebar        图例区：宽度固定（可通过 legendWidth 传参调整）
 *     div.chart-container-flex 折线图区：占满剩余宽度，高度由 chartHeight 控制
 *
 * 用法：
 *   const chart = TrendChart.create({
 *     container: '#trendChart',                // 容器选择器或 DOM 元素
 *     series: [                                // 指标线配置
 *       { key: 'accuracy', label: '满意率', color: '#2563eb', axis: 'left',
 *         get: t => (t.accuracy || 0) * 100 }, // get: 从数据点取值（默认读 t[key]）
 *       { key: 'ttft', label: '首字耗时', color: '#a16207', axis: 'right',
 *         dashed: true, get: t => (t.avg_ttft_ms || 0) / 1000 },
 *     ],
 *     axes: {                                  // 可选：坐标轴配置（默认已内置）
 *       left:  { toFixed: 0, unit: '%' },
 *       right: { toFixed: 2, unit: 's', min: 0, max: 10, interval: 2 },  // 传 min/max 可锁死刻度，interval 定刻度间隔
 *     },
 *     options: { legendWidth: 130, chartHeight: 320 },  // 可选：图例宽度 / 图表高度（'100%' 占满父容器）
 *   });
 *   chart.render(trend);   // 传入数据渲染；再次调用仅更新数据重绘
 *   chart.destroy();       // 卸载（释放 ECharts 实例、清空容器）
 *
 * 说明：图例勾选在组件内部处理（至少保留一条指标线），无需外部重发请求；
 * 容器尺寸变化（含 Tab 切换 display:none→block）通过 ResizeObserver 自动 resize。
 */
const TrendChart = (function () {
	// 各坐标轴的默认取值/兜底范围参数
	const DEFAULT_AXES = {
		left: {
			toFixed: 0,        // 左轴刻度小数位
			unit: '',          // 左轴刻度单位后缀
			defaultMin: 70,    // 无可见左轴指标时的兜底最小值
			defaultMax: 100,   // 无可见左轴指标时的兜底最大值
			padMin: 3, padMax: 3,  // 数据范围上下留白
			clampMin: 0, clampMax: 100,  // 钳制范围（null 表示不限）
			minSpan: 5,        // 最小跨度：数据过窄时下探 min 保证跨度
			includeZero: false,// 最小值计算是否纳入 0
		},
		right: {
			toFixed: 2,
			unit: '',
			defaultMin: 0,
			defaultMax: 5,
			padMin: 0.2, padMax: 0.3,
			clampMin: 0, clampMax: null,
			minSpan: 0.1,
			includeZero: true,
		},
		// 耗时类指标专用轴（首字耗时/整体耗时，单位 ms）：与百分比/计数轴独立，
		// 避免毫秒级数值混入 0-100 的百分比轴导致刻度单位错乱
		time: {
			toFixed: 0,
			unit: 'ms',
			defaultMin: 0,
			defaultMax: 5000,
			padMin: 100, padMax: 200,
			clampMin: 0, clampMax: null,
			minSpan: 100,
			includeZero: true,
		},
	};

	/**
	 * 创建趋势图实例
	 * @param {Object} opts
	 * @param {string|HTMLElement} opts.container  容器
	 * @param {Array} opts.series                 指标线配置（见顶部用法注释）
	 * @param {Object} [opts.axes]                左右轴配置（可选，默认内置）
	 * @param {Object} [opts.options]             绘图参数：chartHeight/legendWidth/xLabel/emptyText/singleText
	 * @param {Function} [opts.onToggle]          图例勾选变化回调 (key, visible) => void
	 * @returns {{ render: Function, destroy: Function } | null}
	 */
	function create(opts) {
		const container = typeof opts.container === 'string'
			? document.querySelector(opts.container)
			: opts.container;
		if (!container) return null;
		// ECharts 未加载（如脚本被拦截）时输出占位文案，避免页面报错
		if (typeof echarts === 'undefined') {
			container.innerHTML = '<div class="empty">图表组件未加载</div>';
			return null;
		}

		const series = (opts.series || []).map(s => ({
			key: s.key,
			label: s.label || s.key,
			color: s.color || '#2563eb',
			axis: s.axis === 'time' ? 'time' : (s.axis === 'right' ? 'right' : 'left'),
			dashed: !!s.dashed,
			strokeWidth: s.strokeWidth || (s.dashed ? 2.5 : 3),
			get: typeof s.get === 'function' ? s.get : (t => t[s.key] || 0),
			visible: s.visible !== false,
		}));
		const options = Object.assign({
			chartHeight: 320,   // 折线图区高度（px）；传 '100%' 时占满父容器剩余高度
			xLabel: t => String(t.date || '').slice(5),  // 默认取 MM-DD
			legendWidth: 130,   // 图例区固定宽度（px），可传参调整
			emptyText: '暂无数据',
			singleText: '仅 1 天数据，暂无法绘制趋势图',
		}, opts.options || {});
		const axes = {
			left: Object.assign({}, DEFAULT_AXES.left, (opts.axes || {}).left),
			right: Object.assign({}, DEFAULT_AXES.right, (opts.axes || {}).right),
			time: Object.assign({}, DEFAULT_AXES.time, (opts.axes || {}).time),
		};
		const onToggle = typeof opts.onToggle === 'function' ? opts.onToggle : null;

		let data = [];
		let bound = false;      // 图例 change 事件是否已绑定（只绑一次）
		let chart = null;       // ECharts 实例
		let resizeHandler = null;
		let resizeObserver = null;
		let observeMode = null; // 'ro' 用 ResizeObserver / 'win' 用 window resize 兜底

		/** 某轴当前是否有可见指标线（决定是否绘制该轴刻度） */
		function hasVisibleOn(axis) {
			return series.some(s => s.axis === axis && s.visible);
		}

		/** 计算某轴范围：仅统计可见指标，未勾选指标不参与，避免拉伸坐标轴 */
		function computeRange(axis) {
			const cfg = axes[axis];
			const vals = [];
			series.forEach(s => {
				if (s.axis !== axis || !s.visible) return;
				data.forEach(t => vals.push(s.get(t)));
			});
			if (!vals.length) return { min: cfg.defaultMin, max: cfg.defaultMax };
			let min = Math.min(...vals, cfg.includeZero ? 0 : Infinity);
			let max = Math.max(...vals);
			min -= cfg.padMin;
			max += cfg.padMax;
			if (cfg.clampMin != null) min = Math.max(cfg.clampMin, min);
			if (cfg.clampMax != null) max = Math.min(cfg.clampMax, max);
			// 数据区间过窄时保持 max，下探 min 保证最小跨度，避免折线拉平
			if (max - min < cfg.minSpan) {
				min = Math.max(cfg.clampMin != null ? cfg.clampMin : -Infinity, max - cfg.minSpan);
			}
			return { min, max };
		}

		/**
		 * 计算某轴最终范围：配置中显式提供的 min/max 优先（锁死刻度，不随数据/勾选变化），
		 * 未提供的部分才用动态计算结果，便于时间类指标稳定对比波动幅度
		 */
		function axisRange(axis) {
			const cfg = axes[axis];
			const base = computeRange(axis);
			return {
				min: cfg.min != null ? cfg.min : base.min,
				max: cfg.max != null ? cfg.max : base.max,
			};
		}

		/** 生成图例侧栏 + 图表容器骨架 HTML */
		function renderHtml() {
			const { legendWidth, chartHeight } = options;
			const sidebarHtml = `
				<div class="chart-sidebar" style="width:${legendWidth}px">
					${series.map(s => `
						<label class="checkbox"><input type="checkbox" ${s.visible ? 'checked' : ''} data-metric="${escapeHtml(s.key)}"><span class="metric-dot" style="color:${s.color};font-size:1.28em"></span>${escapeHtml(s.label)}</label>
					`).join('')}
				</div>`;
			return `
				<div class="chart-row">
					${sidebarHtml}
					<div class="chart-container chart-container-flex">
						<div class="chart-echarts" style="height:${chartHeight}${typeof chartHeight === 'number' ? 'px' : ''}"></div>
					</div>
				</div>`;
		}

		/** 由可见指标 + 轴配置生成 ECharts option（坐标轴范围按锁定值或可见指标计算） */
		function buildOption() {
			const days = data.map(options.xLabel);
			const leftRange = hasVisibleOn('left') ? axisRange('left') : null;
			const rightRange = hasVisibleOn('right') ? axisRange('right') : null;
			const timeRange = hasVisibleOn('time') ? axisRange('time') : null;
			// 单轴配置：无可见指标时不渲染该轴，有可见指标时按计算范围固定 min/max；
			// interval 为显式刻度间隔（undefined 时由 ECharts 自动分档）
			// showSplitLine 控制是否显示该轴的网格线，通常只保留一个轴（如左轴）显示，避免双轴导致网格线杂乱
			// 同侧存在两个轴时（百分比 + 耗时），耗时轴用 offset 错开，并设 onZero:false 使 position/offset 生效
			const mkAxis = (cfg, range, showSplitLine, pos, offset) => ({
				type: 'value',
				show: !!range,
				min: range ? range.min : undefined,
				max: range ? range.max : undefined,
				interval: cfg.interval,
				position: pos,
				offset: offset || 0,
				axisLine: { onZero: false },
				axisLabel: { formatter: v => v.toFixed(cfg.toFixed) + cfg.unit },
				splitLine: { show: showSplitLine, lineStyle: { color: '#e5e7eb', type: 'dashed' } },
			});
			// 耗时轴与百分比轴同侧时右移 56px 错开刻度标签；仅耗时轴可见时无需偏移
			const timeOffset = (rightRange && timeRange) ? 56 : 0;
			return {
				tooltip: {
					trigger: 'axis',
					// 自定义格式化：按指标所属轴的单位/小数位展示数值（日期经转义,防 XSS）
					formatter: (params) => {
						const date = escapeHtml(days[params[0].dataIndex]);
						const lines = params.map(p => {
							const s = series.find(x => x.label === p.seriesName);
							const ax = s ? axes[s.axis] : null;
							return p.marker + p.seriesName + ': ' + (ax ? p.value.toFixed(ax.toFixed) + ax.unit : p.value);
						}).join('<br/>');
						return date + '<br/>' + lines;
					},
				},
				legend: { show: false },  // 图例由左侧自绘勾选框承担，不用内置图例
				// 耗时轴可见时右侧需额外留白（offset 位移 + 两组刻度标签宽度），否则刻度被裁切
				grid: { left: 52, right: timeRange ? (rightRange ? 116 : 78) : 56, top: 20, bottom: 32 },
				xAxis: {
					type: 'category',
					data: days,
					boundaryGap: false,
					axisLabel: { color: '#4b5563', interval: 'auto' },
					axisLine: { lineStyle: { color: '#d1d5db' } },
				},
				yAxis: [
					mkAxis(axes.left, leftRange, true, 'left'),            // 左轴：显示网格线作为视觉基准
					mkAxis(axes.right, rightRange, false, 'right'),        // 右轴（百分比）：隐藏网格线，仅保留刻度标签
					mkAxis(axes.time, timeRange, false, 'right', timeOffset), // 耗时轴（ms）
				],
				series: series.filter(s => s.visible).map(s => ({
					name: s.label,
					type: 'line',
					yAxisIndex: s.axis === 'right' ? 1 : (s.axis === 'time' ? 2 : 0),
					boundaryGap: false,
					symbol: 'circle',
					symbolSize: 5,
					lineStyle: { width: s.strokeWidth, type: s.dashed ? 'dashed' : 'solid' },
					itemStyle: { color: s.color },
					data: data.map(t => s.get(t)),
				})),
			};
		}

		/** 初始化 ECharts 实例并绑定尺寸自适应监听（容器变化或窗口缩放时 resize） */
		function ensureChart() {
			if (chart) return chart;
			const el = container.querySelector('.chart-echarts');
			if (!el) return null;
			chart = echarts.init(el, null, { renderer: 'canvas' });
			resizeHandler = () => { if (chart) chart.resize(); };
			if (typeof ResizeObserver !== 'undefined') {
				observeMode = 'ro';
				resizeObserver = new ResizeObserver(resizeHandler);
				resizeObserver.observe(container);
			} else {
				observeMode = 'win';
				window.addEventListener('resize', resizeHandler);
			}
			return chart;
		}

		/** 释放 ECharts 实例与尺寸监听（销毁/空态时调用，避免内存泄漏） */
		function teardownChart() {
			if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null; }
			if (observeMode === 'win' && resizeHandler) window.removeEventListener('resize', resizeHandler);
			observeMode = null;
			resizeHandler = null;
			if (chart) { chart.dispose(); chart = null; }
		}

		/** 图例勾选事件：委托绑定在容器上，仅绑一次 */
		function bindEvents() {
			if (bound) return;
			bound = true;
			container.addEventListener('change', (evt) => {
				const cb = evt.target.closest('input[data-metric]');
				if (!cb) return;
				const s = series.find(x => x.key === cb.getAttribute('data-metric'));
				if (!s) return;
				s.visible = cb.checked;
				// 至少保留一条指标线，避免图表空白
				if (!series.some(x => x.visible)) {
					s.visible = true;
					cb.checked = true;
				}
				if (onToggle) onToggle(s.key, s.visible);
				render();
			});
		}

		/**
		 * 渲染/更新数据（空数据或仅 1 个数据点时输出占位文案）
		 * @param {Array} [newData] 数据点数组；缺省时用上次缓存重绘（供图例切换）
		 */
		function render(newData) {
			if (newData !== undefined) data = newData;
			if (!container) return;
			// 空态 / 单点：先释放图表实例，再输出占位文案
			if (!data || data.length === 0) {
				teardownChart();
				container.innerHTML = `<div class="empty">${escapeHtml(options.emptyText)}</div>`;
				return;
			}
			if (data.length === 1) {
				teardownChart();
				container.innerHTML = `<div class="empty">${escapeHtml(options.singleText)}</div>`;
				return;
			}
			// 首次（或空态后）重建骨架：图例侧栏 + 图表容器
			if (!container.querySelector('.chart-echarts')) {
				container.innerHTML = renderHtml();
				bindEvents();
			}
			const inst = ensureChart();
			if (inst) inst.setOption(buildOption(), true);  // notMerge=true 全量替换
		}

		/** 卸载：释放实例、清空容器 */
		function destroy() {
			teardownChart();
			container.innerHTML = '';
			data = [];
			bound = false;
		}

		return { render, destroy };
	}

	return { create };
})();

/* ---- 趋势图 ---- */
/* 构造趋势报表接口 URL：自定义范围走 start_date/end_date，否则按当前时间范围换算 days，
   统一追加 root_type 过滤。loadTrend / exportReport / loadDailyReport 复用，避免三处拼 URL 逻辑漂移。
   opts.days 显式指定天数；opts.forceDays=true 时强制按 days 查询（日报 Tab 的天数选择器
   独立于概览时间范围，不受 custom 状态影响）；opts.rootType 缺省取全局根节点筛选。 */
function buildTrendUrl(opts) {
	opts = opts || {};
	const rootType = opts.rootType !== undefined ? opts.rootType : getSelectedRootType();
	let url;
	if (!opts.forceDays && currentTimeRange === 'custom' && customDateStart && customDateEnd) {
		url = `/api/v1/analytics/trend/?start_date=${customDateStart}&end_date=${customDateEnd}`;
	} else {
		const days = opts.days || (currentTimeRange === '7d' ? 7 : (currentTimeRange === '30d' ? 30 : 90));
		url = `/api/v1/analytics/trend/?days=${days}`;
	}
	if (rootType) url += '&root_type=' + encodeURIComponent(rootType);
	return url;
}

/* 概览趋势图组件实例：懒创建一次，图例勾选在组件内部处理 */
let overviewTrendChart = null;

// 请求序号守卫:时间范围/根节点筛选快速切换时,旧响应后返回不覆盖新状态
let trendSeq = 0;
async function loadTrend() {
	const mySeq = ++trendSeq;
	try {
		const data = await api.getJson(buildTrendUrl());
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== trendSeq) return;
		const trend = data.trend || [];

		if (!overviewTrendChart) {
			overviewTrendChart = TrendChart.create({
				container: $('#trendChart'),
				// 指标分轴：左轴=计数类（问答/缓存/好评/差评/活跃用户），右轴=满意率百分比，
				// 耗时类（首字耗时/整体耗时）走独立 time 轴（单位 ms），避免与 % 混轴导致刻度单位错乱；
				// 耗时类默认不勾选，需要时手动勾选
				series: [
					{ key: 'qa', label: '总问答数', color: '#2563eb', axis: 'left', get: t => t.qa_count || 0 },
					{ key: 'cache', label: '缓存命中', color: '#059669', axis: 'left', get: t => t.cache_hit_count || 0 },
					{ key: 'good', label: '好评', color: '#16a34a', axis: 'left', visible: false, get: t => t.good || 0 },
					{ key: 'bad', label: '差评', color: '#dc2626', axis: 'left', visible: false, get: t => t.bad || 0 },
					{ key: 'active', label: '活跃用户', color: '#0891b2', axis: 'left', visible: false, get: t => t.active_users || 0 },
					{ key: 'accuracy', label: '满意率', color: '#7c3aed', axis: 'right', dashed: true, get: t => (t.accuracy || 0) * 100 },
					{ key: 'ttft', label: '首字耗时', color: '#a16207', axis: 'time', visible: false, get: t => t.avg_ttft_ms || 0 },
					{ key: 'total', label: '整体耗时', color: '#ef4444', axis: 'time', visible: false, get: t => t.avg_total_ms || 0 },
				],
				axes: {
					// 左轴：计数类从 0 起算、不设上限，刻度取整数
					left: { toFixed: 0, includeZero: true, clampMin: 0, clampMax: null, minSpan: 1, padMin: 1, padMax: 1 },
					// 右轴：满意率百分比（0-100%），耗时类不再共用该轴
					right: { toFixed: 1, unit: '%', minSpan: 0.1 },
					// 耗时轴：毫秒刻度，从 0 起算、不设上限；pad/minSpan 保证小波动也有刻度跨度
					time: { toFixed: 0, unit: 'ms', includeZero: true, clampMin: 0, clampMax: null, minSpan: 100, padMin: 100, padMax: 100 },
				},
				// 图表高度占满趋势图区剩余空间（配合 .overview-card .chart-echarts 的 flex 链），
				// 图例侧栏宽度 130px，比默认更宽松以容纳中文标签
				options: { chartHeight: '100%', legendWidth: 130 },
			});
		}
		overviewTrendChart.render(trend);
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== trendSeq) return;
		const chart = $('#trendChart');
		if (chart) chart.innerHTML = '<div class="error-block-lg">加载趋势数据失败</div>';
		toast('加载趋势数据失败', 'error');
		console.error('load trend failed:', e);
	}
}

/* ---- 关键词表格 ---- */
// 页面内唯一承载表格的 tbody 为 keywordsTableBody2，历史参数已冗余，改为固定 id
let kwSeq = 0; // 请求序号守卫:根节点快速切换时,旧响应后返回不覆盖新状态
async function loadKeywords() {
	const mySeq = ++kwSeq;
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/keywords/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const keywords = data.rows || [];
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== kwSeq) return;

		const kwBody = document.getElementById('keywordsTableBody2');
		if (kwBody) {
			const kwTpl = tpl('tmpl-kw-row');
			kwBody.innerHTML = keywords.length === 0
				? '<tr><td colspan="4" class="empty">暂无关键词数据</td></tr>'
				: keywords.map(k => {
					const row = kwTpl.content.cloneNode(true).firstElementChild;
					row.querySelectorAll('td')[0].textContent = k.keyword;
					const tag = row.querySelector('.tag');
					tag.textContent = '×' + (k.weight_score || 1).toFixed(1);
					if (k.weight_score > 1) tag.classList.add('tag-success');
					else if (k.weight_score < 1) tag.classList.add('tag-warning');
					row.querySelectorAll('td')[2].textContent = (k.hit_count || 0) + ' 次命中 · ' + (k.good_feedback || 0) + ' 好评 · ' + (k.bad_feedback || 0) + ' 差评';
					const incrBtn = row.querySelector('.incr');
					const decrBtn = row.querySelector('.decr');
					incrBtn.setAttribute('data-kw-id', k.id);
					incrBtn.setAttribute('data-kw-delta', '0.1');
					decrBtn.setAttribute('data-kw-id', k.id);
					decrBtn.setAttribute('data-kw-delta', '-0.1');
					return row.outerHTML;
				}).join('');

			// 在 tbody 上绑 click 监听，处理所有 .incr/.decr 按钮
			if (!kwBody._kwListenerAttached) {
				kwBody.addEventListener('click', (evt) => {
					const btn = evt.target.closest('.incr, .decr');
					if (!btn) return;
					const id = parseInt(btn.getAttribute('data-kw-id'), 10);
					const delta = parseFloat(btn.getAttribute('data-kw-delta'));
					if (!isNaN(id) && !isNaN(delta)) adjustKeywordWeight(id, delta);
				});
				kwBody._kwListenerAttached = true;
			}
		}
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== kwSeq) return;
		const kwBody = document.getElementById('keywordsTableBody2');
		if (kwBody) kwBody.innerHTML = '<tr><td colspan="4" class="error-block">加载关键词数据失败</td></tr>';
		toast('加载关键词数据失败', 'error');
		console.error('load keywords failed:', e);
	}
}

async function adjustKeywordWeight(id, delta) {
	try {
		await api.put(`/api/v1/analytics/keywords/${id}/`, { delta: delta });
		toast(delta > 0 ? '已加权 +0.1' : '已降权 -0.1', 'success');
		// 刷新关键词表 + 自动调整记录（手动调整也写入审计）
		loadKeywords();
		loadFeedbackLoopAggs();
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ---- 反馈闭环自动调整记录 ---- */
let fbAggSeq = 0; // 请求序号守卫:与关键词表同步刷新时,旧响应后返回不覆盖新状态
async function loadFeedbackLoopAggs() {
	const body = document.getElementById('feedbackAggBody');
	if (!body) return;
	const mySeq = ++fbAggSeq;
	try {
		const data = await api.getJson('/api/v1/analytics/feedback-loop/aggregations/?limit=100');
		const rows = data.rows || [];
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== fbAggSeq) return;
		const kwTpl = tpl('tmpl-fb-agg-row');
		body.innerHTML = rows.length === 0
			? '<tr><td colspan="8" class="empty">暂无自动调整记录（点击/反馈数据不足或尚未聚合）</td></tr>'
			: rows.map(r => {
				const row = kwTpl.content.cloneNode(true).firstElementChild;
				const tds = row.querySelectorAll('td');
				tds[0].textContent = r.report_date;
				tds[1].textContent = r.keyword;
				tds[2].textContent = `${r.shown_count || 0} / ${r.click_count || 0} / ${r.adopt_count || 0} / ${r.bad_count || 0}`;
				tds[3].textContent = Math.round((r.adopt_rate || 0) * 100) + '%';
				tds[4].textContent = (r.old_score || 1).toFixed(2) + ' → ' + (r.new_score || 1).toFixed(2);
				tds[5].textContent = r.reason || '-';
				const tag = row.querySelector('.tag');
				const statusMap = { pending: ['待复核', 'tag-warning'], applied: ['已应用', 'tag-success'], ignored: ['已忽略', ''] };
				const st = statusMap[r.status] || [r.status || '', ''];
				tag.textContent = st[0];
				if (st[1]) tag.classList.add(st[1]);
				const actions = row.querySelector('.table-actions');
				if (r.status === 'pending') {
					actions.innerHTML =
						`<button class="btn-link btn-sm" data-fbagg-id="${r.id}" data-fbagg-action="apply">应用</button>` +
						`<button class="btn-link btn-sm" data-fbagg-id="${r.id}" data-fbagg-action="ignore">忽略</button>`;
				} else {
					actions.innerHTML = '<span class="text-sub text-sm">' + (r.adjust_type === 'manual' ? '手动' : '自动') + '</span>';
				}
				return row.outerHTML;
			}).join('');
		// 反馈闭环记录容器级事件委托：处理应用/忽略按钮，避免内联 onclick 的 XSS 风险
		if (!body._fbAggListenerAttached) {
			body.addEventListener('click', (evt) => {
				const btn = evt.target.closest('[data-fbagg-id]');
				if (!btn) return;
				const id = parseInt(btn.getAttribute('data-fbagg-id'), 10);
				const action = btn.getAttribute('data-fbagg-action');
				if (!isNaN(id) && (action === 'apply' || action === 'ignore')) {
					applyFeedbackAgg(id, action);
				}
			});
			body._fbAggListenerAttached = true;
		}
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== fbAggSeq) return;
		body.innerHTML = '<tr><td colspan="8" class="error-block">加载自动调整记录失败</td></tr>';
		console.error('load feedback loop aggs failed:', e);
	}
}

/* 手动触发一次反馈闭环聚合（默认聚合昨天，支持运营即时回补） */
async function runFeedbackLoop() {
	try {
		await api.postJson('/api/v1/analytics/feedback-loop/run/', {});
		toast('聚合完成，已刷新记录', 'success');
		loadFeedbackLoopAggs();
		loadKeywords();
	} catch (e) {
		toast(e.message || '聚合失败', 'error');
	}
}

/* 人工复核：应用/忽略一条待复核的自动调整 */
async function applyFeedbackAgg(id, action) {
	try {
		await api.postJson('/api/v1/analytics/feedback-loop/apply/', { id: id, action: action });
		toast(action === 'apply' ? '已应用调整' : '已忽略', 'success');
		loadFeedbackLoopAggs();
		loadKeywords();
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ---- 动态加载节点树 ---- */
let nodesCache = [];

function getSelectedRootType() {
	const sel = document.getElementById('reportRootType');
	if (!sel || !sel.value) return '';
	const selectedOption = sel.options[sel.selectedIndex];
	return selectedOption?.getAttribute('data-root-type') || '';
}

async function loadRootTypes() {
	try {
		const data = await api.getJson('/api/v1/knowledge/nodes/tree/');
		const tree = data.tree || [];
		nodesCache = [];
		for (const n of tree) {
			// 只取根节点作为领域筛选项(子节点会随选中根节点联动,无需在前端展开树)
			if (n.node_type === 'root') {
				nodesCache.push({ id: n.id, root_type: n.root_type, name: n.name });
			}
		}
		updateRootTypeSelect();
	} catch (e) {
		toast('加载节点树失败', 'error');
		console.error('load nodes failed:', e);
		nodesCache = [];
		updateRootTypeSelect();
	}
}

function updateRootTypeSelect() {
	const sel1 = document.getElementById('reportRootType');
	const sel2 = document.getElementById('newKeywordRootType');
	if (nodesCache.length === 0) {
		if (sel1) sel1.innerHTML = `<option value="">全部节点</option><option value="" disabled>暂无节点数据</option>`;
		if (sel2) sel2.innerHTML = `<option value="all">全部</option><option value="" disabled>暂无节点数据</option>`;
		return;
	}
	const options = nodesCache.map(n => {
		return `<option value="${escapeHtml(String(n.id))}" data-root-type="${escapeHtml(n.root_type)}">${escapeHtml(n.name)}</option>`;
	}).join('');
	if (sel1) sel1.innerHTML = `<option value="">全部节点</option>` + options;
	if (sel2) sel2.innerHTML = `<option value="all">全部</option>` + options;
}

/* ---- 新增关键词 ---- */
function addKeyword() {
	const textInput = document.getElementById('newKeywordText');
	const weightInput = document.getElementById('newKeywordWeight');
	const rootInput = document.getElementById('newKeywordRootType');
	if (textInput) textInput.value = '';
	if (weightInput) weightInput.value = '1.0';
	if (rootInput) rootInput.value = 'all';
	showModal('modal-add-keyword');
	setTimeout(() => textInput && textInput.focus(), 50);
}

async function submitNewKeyword() {
	const keyword = (document.getElementById('newKeywordText')?.value || '').trim();
	const weight = parseFloat(document.getElementById('newKeywordWeight')?.value || '1.0');
	// 所属根节点：下拉 option 的 value 是节点 id，实际 root_type 在 data-root-type 上，
	// 与筛选下拉 getSelectedRootType 同口径，避免把节点 id 误当 root_type 写入
	const rootSel = document.getElementById('newKeywordRootType');
	const rootType = rootSel?.options[rootSel.selectedIndex]?.getAttribute('data-root-type') || 'all';

	if (!keyword) { toast('请输入关键词', 'error'); return; }
	if (isNaN(weight) || weight < 0.1 || weight > 5.0) { toast('权重范围 0.1 ~ 5.0', 'error'); return; }

	try {
		await api.postJson('/api/v1/analytics/keywords/', { keyword: keyword, weight_score: weight, root_type: rootType });
		toast('已新增关键词', 'success');
		closeAllOverlays();
		loadKeywords();
	} catch (e) {
		toast(e.message || '添加失败', 'error');
	}
}

/* ---- 差评反馈列表 ---- */
// 页面内唯一承载差评列表的容器为 feedbackList2，历史参数已冗余，改为固定 id
let badFbSeq = 0; // 请求序号守卫:根节点快速切换时,旧响应后返回不覆盖新状态
async function loadBadFeedbacks() {
	const mySeq = ++badFbSeq;
	try {
		const rootType = getSelectedRootType();
		let url = '/api/v1/analytics/bad-feedbacks/';
		if (rootType) url += '?root_type=' + encodeURIComponent(rootType);
		const data = await api.getJson(url);
		const feedbacks = data.rows || [];
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== badFbSeq) return;

		const fbList = document.getElementById('feedbackList2');
		if (fbList) {
			fbList.innerHTML = feedbacks.length === 0
				? '<div class="empty">暂无差评反馈</div>'
				: feedbacks.map(f => {
					const isResolved = f.status === 'resolved';
					return htmlFromTpl('tmpl-feedback-card', (frag) => {
						const root = frag.firstElementChild;
						root.querySelector('.fb-question').textContent = 'Q: ' + (f.question || '');
						// fb-answer 纯文本展示用 textContent，避免 innerHTML + escapeHtml 混用,性能差且易出错
						const a = root.querySelector('.fb-answer');
						a.textContent = 'A（摘要）: ' + ((f.answer || '').slice(0, 120) + ((f.answer || '').length > 120 ? '…' : ''));
						// fb-comment 有"<b>反馈：</b>"前缀，后面纯文本部分用 createTextNode 追加
						// （textContent/文本节点天然转义，无需再包 escapeHtml，否则会把 &lt; 原样展示）
						const c = root.querySelector('.fb-comment');
						c.innerHTML = '<b>反馈：</b>';
						c.appendChild(document.createTextNode(f.comment || '无详细反馈'));
						// fb-meta 有条件的 span，使用文本节点 + createElement 组合避免 innerHTML
						const meta = root.querySelector('.fb-meta');
						meta.textContent = '';
						meta.appendChild(document.createTextNode((f.user || '-') + ' · ' + formatDate(f.created_at)));
						if (isResolved) {
							meta.appendChild(document.createTextNode(' · '));
							const sp = document.createElement('span');
							sp.className = 'tag tag-success';
							sp.textContent = '已处理';
							meta.appendChild(sp);
						}

						const adjBtn = root.querySelector('.adjust-btn');
						const procBtn = root.querySelector('.process-btn');
						adjBtn.setAttribute('data-fb-id', f.id);
						adjBtn.setAttribute('data-fb-action', 'adjust');
						if (isResolved) {
							procBtn.style.display = 'none';
						} else {
							procBtn.setAttribute('data-fb-id', f.id);
							procBtn.setAttribute('data-fb-action', 'process');
						}
					});
				}).join('');

			// 差评反馈容器级事件委托
			if (!fbList._fbListenerAttached) {
				fbList.addEventListener('click', (evt) => {
					const btn = evt.target.closest('.adjust-btn, .process-btn');
					if (!btn) return;
					const fbId = parseInt(btn.getAttribute('data-fb-id'), 10);
					const action = btn.getAttribute('data-fb-action');
					if (isNaN(fbId)) return;
					if (action === 'adjust') adjustKeywordWeightByFeedback();
					else if (action === 'process') markFeedbackProcessed(fbId);
				});
				fbList._fbListenerAttached = true;
			}
		}
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== badFbSeq) return;
		const fbList = document.getElementById('feedbackList2');
		if (fbList) fbList.innerHTML = '<div class="error-block">加载反馈数据失败</div>';
		toast('加载反馈数据失败', 'error');
		console.error('load bad feedbacks failed:', e);
	}
}

/* 差评卡「调整权重」：反馈与关键词无直接关联，暂不自动定位，引导运营到关键词列表手动调整 */
function adjustKeywordWeightByFeedback() {
	toast('请在关键词列表中手动调整相关关键词权重', '');
	switchTab('tools');
}

async function markFeedbackProcessed(fbId) {
	try {
		await api.put(`/api/v1/analytics/bad-feedbacks/${fbId}/`, { status: 'resolved' });
		toast('已标记为已处理', 'success');
		loadBadFeedbacks();
	} catch (e) {
		toast(e.message || '操作失败', 'error');
	}
}

/* ---- 时间范围切换 ---- */
function setTimeRange(range) {
	if (range !== 'custom') {
		currentTimeRange = range;
		updateTimeButtons(range);
		updateChartTitle(range);
		loadTrend();
	} else {
		showCustomDateRange();
	}
}

function updateTimeButtons(range) {
	const timeBtns = $$('#timeRangeButtons .btn');
	const rangeMap = ['7d', '30d', '90d', 'custom'];
	timeBtns.forEach((b, i) => {
		if (rangeMap[i] === range) {
			b.classList.remove('btn-ghost');
			b.classList.add('btn-primary');
		} else if (rangeMap[i]) {
			b.classList.remove('btn-primary');
			b.classList.add('btn-ghost');
		}
	});
}

function updateChartTitle(range) {
	const titleEl = $('#trendTitle');
	if (!titleEl) return;
	let label;
	if (range === 'custom' && customDateStart && customDateEnd) {
		label = `${customDateStart} ~ ${customDateEnd}`;
	} else {
		const labels = { '7d': '近 7 天', '30d': '近 30 天', '90d': '近 90 天', 'custom': '自定义' };
		label = labels[range] || '近 7 天';
	}
	titleEl.textContent = `📈 指标趋势（${label}）`;
}

/* ---- 自定义日期范围弹窗 ---- */
function showCustomDateRange() {
	const today = new Date().toISOString().slice(0, 10);
	const weekAgo = new Date(Date.now() - 6 * 86400000).toISOString().slice(0, 10);
	const startInput = document.getElementById('customDateStart');
	const endInput = document.getElementById('customDateEnd');
	if (startInput) { startInput.value = customDateStart || weekAgo; startInput.max = today; }
	if (endInput) { endInput.value = customDateEnd || today; endInput.max = today; }
	showModal('modal-date-range');
}

function applyCustomDateRange() {
	const start = document.getElementById('customDateStart')?.value;
	const end = document.getElementById('customDateEnd')?.value;
	if (!start || !end) { toast('请选择开始日期和结束日期', 'error'); return; }
	if (start > end) { toast('开始日期不能晚于结束日期', 'error'); return; }
	customDateStart = start;
	customDateEnd = end;
	currentTimeRange = 'custom';
	closeAllOverlays();
	updateTimeButtons('custom');
	updateChartTitle('custom');
	loadTrend();
	toast(`已切换至自定义范围：${start} ~ ${end}`, 'success');
}

/* ---- 导出报表 ---- */
// 防 CSV 公式注入:单元格以 = + - @ \t \r 开头时前置单引号,避免 Excel 打开时被当作公式执行
function csvCell(v) {
	const s = String(v ?? '');
	return /^[=+\-@\t\r]/.test(s) ? "'" + s : s;
}

async function exportReport() {
	try {
		const data = await api.getJson(buildTrendUrl());

		// CSV 加 UTF-8 BOM（EF BB BF），解决 Excel 打开中文乱码
		const BOM = '\uFEFF';
		let csv = BOM + '日期,问答数,好评数,差评数,准确率(%),平均耗时(ms)\n';
		(data.trend || []).forEach(t => {
			// 后端 TrendReportView 返回 avg_total_ms（非缓存命中的整体总耗时），并非 avg_latency_ms
			// accuracy 可能缺失,先归一为 0 再乘 100,避免 (undefined*100).toFixed 输出 "NaN"
			csv += [csvCell(t.date), csvCell(t.qa_count), csvCell(t.good), csvCell(t.bad),
				csvCell(((t.accuracy || 0) * 100).toFixed(2)), csvCell(t.avg_total_ms || 0)].join(',') + '\n';
		});

		const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
		const link = document.createElement('a');
		const url2 = URL.createObjectURL(blob);
		link.setAttribute('href', url2);
		link.setAttribute('download', `报表_${new Date().toISOString().slice(0, 10)}.csv`);
		link.style.visibility = 'hidden';
		document.body.appendChild(link);
		link.click();
		document.body.removeChild(link);
		URL.revokeObjectURL(url2);

		toast('报表已导出', 'success');
	} catch (e) {
		toast('导出失败', 'error');
		console.error('export failed:', e);
	}
}

/* ---- Tab 2: 系统性能指标报表（P50/P95/P99 / 缓存命中率 / 失败率 / Token / 错误分布） ---- */
// 请求序号守卫:日期切换快速触发时,旧响应后返回不覆盖新状态
let sysMetricsSeq = 0;
async function loadSystemMetrics() {
	const mySeq = ++sysMetricsSeq;
	const box = $('#systemMetricsBody');
	// 重渲染前先销毁旧柱状图实例：innerHTML 会替换掉旧容器，残留实例会报"容器已存在"并泄漏
	if (sysMetricsHistChart) { sysMetricsHistChart.dispose(); sysMetricsHistChart = null; }
	const date = $('#systemMetricsDate')?.value;
	try {
		let url = '/api/v1/analytics/system-metrics/';
		if (date) url += '?date=' + encodeURIComponent(date);
		const data = await api.getJson(url);
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== sysMetricsSeq) return;

		if (!data.available) {
			// 空态在 system-card 内直接占位，不再嵌套 .card（避免双层边框）
			box.innerHTML = `
        <div class="card-empty">
          <div class="empty-emoji">📅</div>
          <div class="text-lg fw-500 mb-8">${escapeHtml(data.message || '暂无数据')}</div>
          <div class="text-sub">报表日期：${escapeHtml(date || data.date || '-')}</div>
        </div>`;
			return;
		}

		// 1. KPI 层：QA 规模 + 比率
		const kpiCards = [
			{ label: '总 QA 数', value: data.total_qa?.toLocaleString() || 0, color: '#1f2937' },
			{ label: '正常请求数', value: data.normal_qa_count?.toLocaleString() || 0, color: '#2563eb' },
			{ label: '缓存命中数', value: data.cache_hit_count?.toLocaleString() || 0, color: '#059669' },
			{ label: '缓存命中率', value: (data.cache_hit_rate || 0) * 100 + '%', color: '#059669' },
			{ label: 'LLM 成功率', value: (data.llm_success_rate || 0) * 100 + '%', color: data.llm_success_rate < 0.9 ? '#dc2626' : '#059669' },
			{ label: 'LLM 超时率', value: (data.llm_timeout_rate || 0) * 100 + '%', color: data.llm_timeout_rate > 0.01 ? '#dc2626' : '#f59e0b' },
			{ label: 'Embedding 错误率', value: (data.embedding_error_rate || 0) * 100 + '%', color: data.embedding_error_rate > 0.01 ? '#dc2626' : '#f59e0b' },
			{ label: '平均 Token/s', value: data.avg_tokens_per_second || 0, color: '#7c3aed' },
		].map(c => `
      <div class="kpi-card">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value kpi-value-dynamic" style="--kpi-color:${c.color}">${c.value}</div>
      </div>`).join('');

		// 2. 响应及缓存耗时 / Token 成本：两个独立卡片并排（perf-grid 2fr/1fr），标题在卡片内
		//    左卡：5 个维度列（总延迟/LLM/检索/TTFB/缓存命中），每列上下展示 P50/P95/P99
		//    无数据（null/0）显示 "/"，避免 0 被误读为真实延迟
		const msNum = v => (!v) ? '/' : v.toLocaleString();
		const latencyCol = (name, p50, p95, p99) => `
      <div class="latency-col">
        <div class="latency-col-title">${name}</div>
        <div class="latency-row"><span>P50</span><b>${msNum(p50)}</b></div>
        <div class="latency-row"><span>P95</span><b>${msNum(p95)}</b></div>
        <div class="latency-row"><span>P99</span><b>${msNum(p99)}</b></div>
      </div>`;

		const latencyPanel = `
      <div class="card">
        <div class="card-title">⚡ 响应及缓存耗时（ms）</div>
        <div class="latency-grid">
          ${latencyCol('总延迟', data.p50_latency_total, data.p95_latency_total, data.p99_latency_total)}
          ${latencyCol('LLM', data.p50_latency_llm, data.p95_latency_llm, data.p99_latency_llm)}
          ${latencyCol('检索', data.p50_latency_retrieval, data.p95_latency_retrieval, data.p99_latency_retrieval)}
          ${latencyCol('TTFB', data.p50_ttfb, data.p95_ttfb, data.p99_ttfb)}
          ${latencyCol('缓存命中', data.cache_hit_p50_latency, data.cache_hit_p95_latency, data.cache_hit_p99_latency)}
        </div>
      </div>`;

		// 3. Token 成本卡（纵向三行：Prompt / Completion / 费用，费用红色强调）
		const tokenStr = `
      <div class="card">
        <div class="card-title">🪙 Token 成本</div>
        <div class="token-rows">
          <div class="token-row"><span>Prompt Token</span><b>${(data.total_tokens_prompt || 0).toLocaleString()}</b></div>
          <div class="token-row"><span>Completion Token</span><b>${(data.total_tokens_completion || 0).toLocaleString()}</b></div>
          <div class="token-row"><span>预估费用（¥）</span><b class="cost">¥ ${(data.total_cost || 0).toFixed(4)}</b></div>
        </div>
      </div>`;

		// 4. 延迟直方图：改为 ECharts 柱状图（100ms 等宽桶，由后端 build_latency_histogram 生成）
		//    桶数可能上百（延迟跨度大），柱状图固定高度不撑页面，label 靠 echarts 自动隐藏重叠项 + tooltip 看值
		const hist = data.latency_histogram || {};
		const histKeys = Object.keys(hist).sort((a, b) => parseInt(a) - parseInt(b));
		// 直方图总量只算一次，避免每条记录 O(n) reduce 造成的 O(n²)
		const histTotal = histKeys.reduce((s, kk) => s + (hist[kk] || 0), 0);
		/* histEl 为柱状图容器，数据为空时不渲染 echarts 直接显示空态；
		   echarts 初始化在下方 setTimeout 中执行（等 innerHTML 落盘） */
		const histEl = histKeys.length === 0 ? '<div class="empty">暂无分布数据</div>'
			: '<div class="hist-chart" id="sysMetricsHistChart"></div>';

		// 5. 错误分布
		const errDist = data.error_distribution || {};
		const errKeys = Object.keys(errDist).sort((a, b) => (errDist[b] || 0) - (errDist[a] || 0));
		const errTotal = errKeys.reduce((s, k) => s + (errDist[k] || 0), 0) || 1;
		/* 错误分布用红色系 */
		const errHtml = errKeys.length === 0 ? '<div class="empty">暂无错误数据 🎉</div>' : errKeys.map(k => {
			const v = errDist[k] || 0;
			const pct = (v / errTotal) * 100;
			return `<div class="hist-row">
        <span class="hist-label-err">${escapeHtml(k || 'unknown')}</span>
        <div class="hist-track-err">
          <div class="hist-bar-err" style="width:${pct.toFixed(1)}%"></div>
        </div>
        <span class="hist-value">${v} (${pct.toFixed(1)}%)</span>
      </div>`;
		}).join('');

		box.innerHTML = `
      <div class="system-kpi-section">
        <div class="section-title">📊 关键指标</div>
        <div class="kpi-grid">${kpiCards}</div>
      </div>
      <div class="system-section">
        <!-- 响应耗时 / Token 成本两个独立卡片，标题各自在卡内，无需区块级标题 -->
        <div class="perf-grid">
          ${latencyPanel}
          ${tokenStr}
        </div>
      </div>
      <div class="system-section">
        <div class="section-title">📊 延迟与错误分布</div>
        <div class="grid-2 grid-cols-1-1">
          <div class="sub-panel">
            <div class="sub-panel-title">⚡ 延迟分布（ms）</div>
            ${histEl}
          </div>
          <div class="sub-panel">
            <div class="sub-panel-title">❌ 错误分布</div>
            ${errHtml}
          </div>
        </div>
      </div>`;

		// 延迟直方图柱状图初始化（等 innerHTML 渲染完成再 init）
		// 桶少时 label 直排，桶多时旋转 45° 并依赖 echarts hideOverlap 自动隐藏重叠项，tooltip 可看任意桶
		setTimeout(() => {
			const histEl = $('#sysMetricsHistChart');
			if (!histEl) return;
			sysMetricsHistChart = echarts.init(histEl, null, { renderer: 'canvas' });
			sysMetricsHistChart.setOption({
				tooltip: {
					trigger: 'axis',
					axisPointer: { type: 'shadow' },
					formatter: (params) => {
						const p = params[0];
						const pct = histTotal ? ((p.value / histTotal) * 100).toFixed(1) : '0.0';
						return `${p.name} ms<br/><b>${Number(p.value).toLocaleString()} 条</b>（${pct}%）`;
					},
				},
				grid: { left: 8, right: 8, top: 8, bottom: 4, containLabel: true },
				xAxis: {
					type: 'category',
					data: histKeys,
					axisLabel: { fontSize: 10, color: '#6b7280', rotate: histKeys.length > 15 ? 45 : 0 },
					axisTick: { alignWithLabel: true },
				},
				yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#f3f4f6' } } },
				series: [{
					type: 'bar',
					data: histKeys.map(k => hist[k] || 0),
					barMaxWidth: 18,
					itemStyle: { color: '#2563eb', borderRadius: [3, 3, 0, 0] },
				}],
			});
		}, 30);
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== sysMetricsSeq) return;
		box.innerHTML = `<div class="card-error">加载系统指标失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载系统指标失败', 'error');
		console.error('load system metrics failed:', e);
	}
}

/* ---- 今日实时自动轮询 ----
 * 仅概览 Tab 激活时运行：每 5 分钟刷新一次，与后端 flush_realtime_metrics 周期对齐；
 * 切走/页面隐藏时暂停，回到概览/页面时立即刷新并恢复轮询。
 * 轮询失败静默（保留已渲染数据），避免定时任务打扰用户。 */
const REALTIME_POLL_INTERVAL = 5 * 60 * 1000; // 5 分钟
let _rtTimer = null;
let _rtPollSeq = 0; // 实时请求序号：手动刷新与轮询可能并发，仅采用最后一次发起的响应

function startRealtimePolling() {
	stopRealtimePolling();
	_rtTimer = setInterval(() => loadRealtimeStrip(true), REALTIME_POLL_INTERVAL);
}

function stopRealtimePolling() {
	if (_rtTimer) { clearInterval(_rtTimer); _rtTimer = null; }
}

/** 生成实时卡片同比对比行（今日 vs 昨日同时段）
 *  - 无昨日数据（yesterday 缺失/为 null）时显示"暂无对比"
 *  - 与昨日持平显示"持平"；否则按涨跌方向 + 差值 + 百分比展示，
 *    颜色按指标业务预期着色（positive=true 表示上涨符合预期 → 涨绿跌红，反之相反） */
function buildRealtimeCompare(c, yesterday) {
	const yVal = (yesterday && typeof yesterday[c.key] === 'number') ? yesterday[c.key] : null;
	if (yVal == null) return '<div class="kpi-compare text-sub">暂无对比</div>';
	const diff = c.cur - yVal;
	if (Math.abs(diff) < 1e-9) return '<div class="kpi-compare">持平</div>';
	const up = diff > 0;
	const good = up === c.positive;
	const absDiff = Math.abs(diff);
	// Token/费用为小数时保留 4 位，计数类整数直接千分位
	const diffStr = Number.isInteger(absDiff) ? absDiff.toLocaleString() : absDiff.toFixed(4);
	const pct = yVal > 0 ? ` (${((absDiff / yVal) * 100).toFixed(1)}%)` : '';
	return `<div class="kpi-compare ${good ? 'up' : 'down'}" title="对比昨日同时段">${up ? '▲' : '▼'} ${diffStr}${pct}</div>`;
}

/* ---- 概览"今日实时"区块（Redis 快照，合并自原"实时监控"Tab） ---- */
async function loadRealtimeStrip(silent) {
	const box = $('#realtimeStrip');
	if (!box) return;
	// 请求序号守卫：轮询与手动刷新可能并发，仅采用最后一次发起的响应，防止旧数据覆盖新数据
	const seq = ++_rtPollSeq;
	try {
		const data = await api.getJson('/api/v1/analytics/realtime/');
		if (seq !== _rtPollSeq) return; // 已有更新的请求，丢弃本次过时响应

		const freshness = data.last_flush_at
			? Math.floor((Date.now() / 1000) - data.last_flush_at)
			: null;
		const isFresh = freshness != null && freshness < 600; // 10 分钟内视为新鲜

		// 数据新鲜度徽标：实时指标每 5 分钟由 flush_realtime_metrics 更新时间戳
		const freshnessEl = $('#realtimeFreshness');
		if (freshnessEl) {
			freshnessEl.innerHTML = freshness == null
				? '<span class="tag tag-warning">尚未同步</span>'
				: (isFresh
					? `<span class="tag tag-success">数据新鲜（${freshness}s 前同步）</span>`
					: `<span class="tag tag-danger">数据陈旧（${freshness}s 未同步）</span>`);
		}

		// 今日实时卡片：cur 为原始数值用于同比计算，positive 表示"上涨是否符合业务预期"
		// （缓存命中/正常请求上涨为佳；Token/费用/LLM 错误上涨为劣，用于对比行着色）
		const kpiCards = [
			{ label: '今日 QA 总数', key: 'total_qa', cur: data.total_qa || 0, color: '#1f2937', positive: true, fmt: v => v.toLocaleString() },
			{ label: '缓存命中', key: 'cache_hits', cur: data.cache_hits || 0, color: '#059669', positive: true, fmt: v => v.toLocaleString() },
			{ label: '正常请求', key: 'normal_qa', cur: data.normal_qa || 0, color: '#2563eb', positive: true, fmt: v => v.toLocaleString() },
			{ label: 'LLM 错误', key: 'llm_errors', cur: data.llm_errors || 0, color: data.llm_errors > 0 ? '#dc2626' : '#059669', positive: false, fmt: v => v.toLocaleString() },
			{ label: '今日 Prompt Token', key: 'tokens_prompt', cur: data.tokens_prompt || 0, color: '#7c3aed', positive: false, fmt: v => v.toLocaleString() },
			{ label: '今日 Completion Token', key: 'tokens_completion', cur: data.tokens_completion || 0, color: '#7c3aed', positive: false, fmt: v => v.toLocaleString() },
			{ label: '今日预估费用', key: 'cost_estimate', cur: data.cost_estimate || 0, color: '#dc2626', positive: false, fmt: v => '¥ ' + v.toFixed(4) },
		].map(c => `
      <div class="kpi-card">
        <div class="kpi-label">${c.label}</div>
        <div class="kpi-value kpi-value-dynamic" style="--kpi-color:${c.color}">${c.fmt(c.cur)}</div>
        ${buildRealtimeCompare(c, data.yesterday)}
      </div>`).join('');

		box.innerHTML = kpiCards;
	} catch (e) {
		if (seq !== _rtPollSeq) return; // 过时响应不处理
		// 轮询失败静默：保留上一次已渲染的数据，避免卡片闪烁或清空
		if (!silent) {
			box.innerHTML = `<div class="card card-error">加载实时指标失败：${escapeHtml(e.message || '')}</div>`;
			toast('加载实时指标失败', 'error');
		}
		console.error('load realtime failed:', e);
	}
}

/* ---- Tab 4: 队列深度监控 ---- */
// 请求序号守卫:窗口切换快速触发时,旧响应后返回不覆盖新状态
let queueSeq = 0;
async function loadQueueDepth() {
	const mySeq = ++queueSeq;
	try {
		const hours = $('#queueHours')?.value || 24;
		const data = await api.getJson(`/api/v1/analytics/queue-depth/?hours=${hours}`);
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== queueSeq) return;

		// 1. 当前实时快照 — 渲染到上方紧凑卡片
		const snapBox = $('#queueSnapshotBody');
		const cur = data.current || {};
		const curKeys = Object.keys(cur);
		/* 队列大小超过 1000 视为危险，用 .cell-danger / .cell-success 切换颜色 */
		const curHtml = curKeys.length === 0
			? '<div class="empty">当前无队列数据（Celery Worker 未启动？）</div>'
			: curKeys.map(q => {
				const d = cur[q] || {};
				const size = d.size || d.length || 0;
				const danger = size > 1000;
				return `<tr>
          <td>${escapeHtml(q)}</td>
          <td class="${danger ? 'cell-danger' : 'cell-success'}">${size.toLocaleString()}</td>
          <td>${d.queued != null ? escapeHtml(d.queued) : '-'}</td>
          <td>${d.active != null ? escapeHtml(d.active) : '-'}</td>
          <td>${d.idle != null ? escapeHtml(d.idle) : '-'}</td>
          <td>${d.failed != null ? escapeHtml(d.failed) : '-'}</td>
        </tr>`;
			}).join('');

		snapBox.innerHTML = `
        <table class="table table-bordered">
          <thead><tr>
            <th>队列名</th><th>等待任务数</th><th>已排队</th><th>运行中</th><th>空闲 Worker</th><th>失败</th>
          </tr></thead>
          <tbody>${curHtml || '<tr><td colspan="6" class="empty">无数据</td></tr>'}</tbody>
        </table>`;

		// 2. 历史趋势 — 渲染到下方大卡片，撑满剩余空间
		const histBox = $('#queueDepthHistory');
		const history = data.history || [];
		if (history.length === 0) {
			// 无历史时先销毁旧图表实例，再输出占位文案，避免残留孤立的 ECharts 实例
			destroyQueueDepthChart();
			histBox.innerHTML = '<div class="empty">暂无历史数据（需要等待至少 1 个 5 分钟周期）</div>';
		} else {
			renderQueueDepthChart(history);
		}
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== queueSeq) return;
		const snapBox = $('#queueSnapshotBody');
		if (snapBox) snapBox.innerHTML = `<div class="error-block">加载队列深度失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载队列深度失败', 'error');
		console.error('load queue depth failed:', e);
	}
}

/* 队列深度历史趋势图实例（ECharts 版，复用概览的 TrendChart 组件）：
   懒创建，队列集合变化时销毁重建（队列名是动态的，无法像概览那样固定 series） */
let queueDepthChart = null;
let queueDepthQueueNames = null;  // 上次创建时的队列集合，用于判断是否需要重建实例

/** 销毁队列深度趋势图实例（无数据/重建前调用，释放 ECharts 与 ResizeObserver） */
function destroyQueueDepthChart() {
	if (queueDepthChart) { queueDepthChart.destroy(); queueDepthChart = null; }
	queueDepthQueueNames = null;
}

function renderQueueDepthChart(history) {
	// history: [{queue_name, minute_bucket, queued_size, active_size, ...}]
	// minute_bucket 为 YYYYMMDDHHmm（本地时间），X 轴标签 slice 成 HH:MM
	const buckets = [...new Set(history.map(h => h.minute_bucket))].sort();
	const queues = [...new Set(history.map(h => h.queue_name))].sort();

	if (buckets.length < 2) {
		destroyQueueDepthChart();
		$('#queueDepthHistory').innerHTML = `<div class="empty">历史数据不足（当前样本数 ${buckets.length}），至少需要 2 个时间槽</div>`;
		return;
	}

	// 先构造 (bucket, queue) → 总深度（queued + active）的 Map，O(H) 建索引，
	// 后续组装数据点 O(B*Q) 直接查 Map，避免双重循环内 .find() 的 O(Q*B*H)
	const depthMap = new Map();
	for (const h of history) {
		depthMap.set(`${h.minute_bucket}||${h.queue_name}`, (h.queued_size || 0) + (h.active_size || 0));
	}

	// 组装 ECharts 数据点：每个时间槽一个对象，字段按队列名取值（TrendChart 默认读 t[key]）
	const data = buckets.map(b => {
		const point = { date: b };
		queues.forEach(q => { point[q] = depthMap.get(`${b}||${q}`) || 0; });
		return point;
	});

	const palette = ['#2563eb', '#059669', '#f59e0b', '#dc2626', '#7c3aed', '#0891b2', '#db2777'];
	const series = queues.map((q, i) => ({
		key: q,
		label: q,
		color: palette[i % palette.length],
		axis: 'left',
	}));

	// 队列集合未变时复用实例（保留用户图例勾选状态）仅更新数据；变化则销毁重建
	const sameQueues = queueDepthQueueNames && queueDepthQueueNames.length === queues.length
		&& queues.every(q => queueDepthQueueNames.includes(q));
	if (queueDepthChart && sameQueues) {
		queueDepthChart.render(data);
		return;
	}

	destroyQueueDepthChart();
	queueDepthQueueNames = queues;
	queueDepthChart = TrendChart.create({
		container: $('#queueDepthHistory'),
		series: series,
		axes: {
			// 计数轴：从 0 起算、不设上限（队列深度可能远超 100），刻度取整数
			left: { toFixed: 0, includeZero: true, clampMin: 0, clampMax: null, minSpan: 1, padMin: 1, padMax: 1 },
		},
		options: {
			chartHeight: '100%', legendWidth: 130,
			xLabel: t => String(t.date).slice(8, 10) + ':' + String(t.date).slice(10, 12),
		},
	});
	if (queueDepthChart) queueDepthChart.render(data);
}

/* ---- Tab 5: 部门/团队使用统计 ---- */
// 请求序号守卫:日期/层级切换快速触发时,旧响应后返回不覆盖新状态
let orgSeq = 0;
async function loadOrgUsage() {
	const mySeq = ++orgSeq;
	const box = $('#orgUsageBody');
	try {
		const date = $('#orgUsageDate')?.value;
		const level = $('#orgLevel')?.value || 'team';
		let url = '/api/v1/analytics/org-usage/';
		const params = [];
		if (date) params.push('date=' + encodeURIComponent(date));
		if (level === 'dept') params.push('team_id=-1'); // 部门汇总哨兵值
		const finalUrl = params.length ? url + '?' + params.join('&') : url;

		const data = await api.getJson(finalUrl);
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== orgSeq) return;
		const rows = data.rows || [];

		if (rows.length === 0) {
			box.innerHTML = `
        <div class="card card-empty">
          <div class="empty-emoji">🧾</div>
          <div class="text-lg fw-500 mb-8">暂无该日期的组织使用报表</div>
          <div class="text-sub">报表日期：${escapeHtml(date || data.date || '-')}（请等待凌晨聚合任务完成或切换到其他日期）</div>
        </div>`;
			return;
		}

		const headers = level === 'dept'
			? ['部门', 'QA 次数', '活跃用户', '总 Token', '预估费用（¥）', '平均延迟 (ms)', 'P95 延迟 (ms)', '好评率 (%)', '缓存命中数', '缓存命中率 (%)']
			: ['部门', '团队', 'QA 次数', '活跃用户', '总 Token', '预估费用（¥）', '平均延迟 (ms)', 'P95 延迟 (ms)', '好评率 (%)', '缓存命中数', '缓存命中率 (%)'];

		const fmtCost = v => '¥ ' + (v || 0).toFixed(4);
		const fmtPct = v => v == null ? '-' : (v * 100).toFixed(2) + '%';

		const tableRows = rows.map(r => {
			const deptCell = `<td>${escapeHtml(r.department_name || '-')}</td>`;
			const teamCell = level === 'team' ? `<td>${escapeHtml(r.team_name || '-')}</td>` : '';
			const cells = [
				(r.qa_count || 0).toLocaleString(),
				(r.user_count || 0).toLocaleString(),
				(r.total_tokens || 0).toLocaleString(),
				fmtCost(r.total_cost),
				(r.avg_latency_ms || 0).toLocaleString(),
				(r.p95_latency_ms || 0).toLocaleString(),
				fmtPct(r.good_feedback_rate),
				(r.cache_hit_count || 0).toLocaleString(),
				fmtPct(r.cache_hit_rate),
			].map(c => `<td>${c}</td>`).join('');
			return `<tr>${deptCell}${teamCell}${cells}</tr>`;
		}).join('');

		box.innerHTML = `
      <div class="card">
        <div class="card-title">🏢 ${level === 'dept' ? '部门' : '团队'}级使用统计 · ${escapeHtml(data.date || date || '-')}</div>
        <div class="table-container">
          <table class="table table-bordered">
            <thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead>
            <tbody>${tableRows}</tbody>
          </table>
        </div>
      </div>`;
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== orgSeq) return;
		box.innerHTML = `<div class="card card-error">加载组织统计失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载组织统计失败', 'error');
		console.error('load org usage failed:', e);
	}
}

/* ---- Tab 6: QA 记录列表（筛选 + 分页 + 详情弹窗） ---- */
let _qaLoadSeq = 0;        // QA 记录请求序列号，防止筛选/翻页竞态
let _qaSearchTimer = null; // 问题搜索防抖定时器

/** 问题搜索输入：300ms 防抖后触发筛选 */
function onQaSearchInput() {
	clearTimeout(_qaSearchTimer);
	_qaSearchTimer = setTimeout(() => onQaFilterChange(), 300);
}

/** 筛选条件变化：重置回第 1 页并重新加载 */
function onQaFilterChange() {
	qaPage = 1;
	loadQaRecords();
}

async function loadQaRecords() {
	const box = $('#qaRecordsBody');
	const seq = ++_qaLoadSeq;
	try {
		const params = [];
		const q = $('#qaSearchInput')?.value.trim();
		if (q) params.push('q=' + encodeURIComponent(q));
		const start = $('#qaStartDate')?.value;
		if (start) params.push('start_date=' + encodeURIComponent(start));
		const end = $('#qaEndDate')?.value;
		if (end) params.push('end_date=' + encodeURIComponent(end));
		const answerType = $('#qaAnswerType')?.value;
		if (answerType) params.push('answer_type=' + encodeURIComponent(answerType));
		const cache = $('#qaCache')?.value;
		if (cache) params.push('cache=' + encodeURIComponent(cache));
		const rating = $('#qaRating')?.value;
		if (rating) params.push('rating=' + encodeURIComponent(rating));
		const latency = $('#qaLatency')?.value;
		if (latency) {
			// 延迟区间格式为 min-max，max 为空表示无上限
			const [latMin, latMax] = latency.split('-');
			if (latMin !== '') params.push('latency_min=' + encodeURIComponent(latMin));
			if (latMax !== '') params.push('latency_max=' + encodeURIComponent(latMax));
		}
		params.push('page=' + qaPage);
		params.push('page_size=' + qaPageSize);
		const data = await api.getJson('/api/v1/analytics/qa-records/?' + params.join('&'));
		if (seq !== _qaLoadSeq) return;  // 过时请求，忽略

		qaTotal = data.total || 0;
		const rows = data.rows || [];

		const typeBadge = t => {
			// 与 QaRecord 实际写入值对齐（rag/reasoning/mixed/refused/agent/general），
			// 与顶部筛选下拉选项保持一致；未知类型回退为无配色裸标签
			const map = {
				rag: ['tag-info', 'RAG'],
				reasoning: ['tag-primary', '推理'],
				mixed: ['tag-warning', '混合'],
				refused: ['tag-danger', '拒答'],
				agent: ['tag-success', 'Agent'],
				general: ['tag-info', '通用'],
			};
			const [cls, text] = map[t] || ['', t || '-'];
			return `<span class="tag ${cls}">${escapeHtml(text)}</span>`;
		};
		const ratingBadge = r => {
			if (r === 1) return '<span class="tag tag-success">👍 好评</span>';
			if (r === -1) return '<span class="tag tag-danger">👎 差评</span>';
			return '<span class="tag">-</span>';
		};

		const tableHtml = rows.length === 0
			? '<tr><td colspan="9" class="empty">暂无 QA 记录</td></tr>'
			: rows.map(r => `
          <tr class="tr-clickable" data-qa-id="${r.id}">
            <td>${r.id}</td>
            <td class="td-question" title="${escapeHtml(r.question)}">${escapeHtml(r.question)}</td>
            <td>${typeBadge(r.answer_type)}</td>
            <td>${r.is_hit_cache ? '<span class="tag tag-warning">是</span>' : '<span class="tag">-</span>'}</td>
            <td>${ratingBadge(r.rating)}</td>
            <td>${(r.latency_total_ms || 0).toLocaleString()} ms</td>
            <td>${(r.tokens_prompt || 0) + (r.tokens_completion || 0)}</td>
            <td>¥ ${(r.cost_estimate || 0).toFixed(4)}</td>
            <td class="text-sub text-sm">${formatDate(r.created_at)}</td>
          </tr>`).join('');

		// 仅渲染 tbody 行，表格结构与表头在 HTML 中静态声明
		box.innerHTML = tableHtml;

		// QA 行点击：容器级事件委托，避免每行 setAttribute('onclick', ...) 的 eval 模式
		if (!box._qaRowListener) {
			box.addEventListener('click', (evt) => {
				const tr = evt.target.closest('tr[data-qa-id]');
				if (!tr) return;
				const id = parseInt(tr.getAttribute('data-qa-id'), 10);
				if (!isNaN(id)) showQaDetail(id);
			});
			box._qaRowListener = true;
		}

		renderQaPagination();
	} catch (e) {
		if (seq !== _qaLoadSeq) return;  // 过时请求，忽略
		box.innerHTML = '<tr><td colspan="9" class="empty">加载 QA 记录失败：' + escapeHtml(e.message || '') + '</td></tr>';
		toast('加载 QA 记录失败', 'error');
		console.error('load qa records failed:', e);
	}
}

/* ============================================================================
 * 分页：复用公共 Pagination 组件（common.js）。
 * 首次 render 绑定回调，后续 update 仅刷新页码状态；切换每页条数后重置回第 1 页
 * ============================================================================ */
let _qaPaginationInited = false; // 分页组件是否已初始化

function renderQaPagination() {
	const totalPages = Math.max(1, Math.ceil(qaTotal / qaPageSize));
	if (!_qaPaginationInited) {
		Pagination.render({
			container: '#qaPagination',
			page: qaPage,
			totalPages: totalPages,
			total: qaTotal,
			pageSize: qaPageSize,
			align: 'right',
			pageSizeOptions: [20, 50, 100],
			onPageChange(p) { qaPage = p; loadQaRecords(); },
			onPageSizeChange(size) { qaPageSize = size; qaPage = 1; loadQaRecords(); },
		});
		_qaPaginationInited = true;
	} else {
		Pagination.update({
			page: qaPage,
			totalPages: totalPages,
			total: qaTotal,
			pageSize: qaPageSize,
		});
	}
}

/* QA 详情弹窗 */
async function showQaDetail(id) {
	const box = $('#qaDetailBody');
	box.innerHTML = '<div class="text-sub text-loading">加载中...</div>';
	showModal('modal-qa-detail');
	try {
		// 调用后端新增的 qa_id 参数接口，直接查询单条（避免 page_size=100 的前 100 条限制）
		const d = await api.getJson(`/api/v1/analytics/qa-records/?qa_id=${encodeURIComponent(id)}`);
		const r = d.row;
		if (!r) { box.innerHTML = '<div class="error-block">未找到该 QA 记录</div>'; return; }
		box.innerHTML = `
      <div class="mb-16"><div class="text-sub text-sm mb-4">问题</div>
        <div class="qa-detail-question">${escapeHtml(r.question)}</div>
      </div>
      <div class="mb-16"><div class="text-sub text-sm mb-4">回答</div>
        <div class="qa-detail-answer">${escapeHtml(r.answer)}</div>
      </div>
      <div class="grid-2">
        <div><div class="text-sub text-sm">回答类型</div><div>${escapeHtml(r.answer_type || '-')}</div></div>
        <div><div class="text-sub text-sm">领域</div><div>${escapeHtml(r.root_type || '-')}</div></div>
        <div><div class="text-sub text-sm">总延迟</div><div>${(r.latency_total_ms || 0).toLocaleString()} ms</div></div>
        <div><div class="text-sub text-sm">缓存命中</div><div>${r.is_hit_cache ? '是' : '否'}</div></div>
        <div><div class="text-sub text-sm">Prompt Token</div><div>${(r.tokens_prompt || 0).toLocaleString()}</div></div>
        <div><div class="text-sub text-sm">Completion Token</div><div>${(r.tokens_completion || 0).toLocaleString()}</div></div>
        <div><div class="text-sub text-sm">预估费用</div><div>¥ ${(r.cost_estimate || 0).toFixed(4)}</div></div>
        <div><div class="text-sub text-sm">时间</div><div>${formatDate(r.created_at)}</div></div>
      </div>`;
	} catch (err) {
		box.innerHTML = `<div class="error-block">加载失败：${escapeHtml(err.message || '')}</div>`;
	}
}

/* ---- Tab 8: 日报详情（今日 vs 昨日对比 + 多日趋势折线图） ---- */
// 请求序号守卫:天数/根节点筛选快速切换时,旧响应后返回不覆盖新状态
let dailySeq = 0;
async function loadDailyReport() {
	const box = $('#dailyBody');
	const mySeq = ++dailySeq;
	try {
		// 并行拉取日报对比数据和趋势数据，减少等待时间
		const trendDays = $('#dailyTrendDays')?.value || 30;
		const rootType = getSelectedRootType();
		const rtQ = rootType ? `?root_type=${encodeURIComponent(rootType)}` : '';
		const [dailyData, trendData] = await Promise.all([
			api.getJson('/api/v1/analytics/daily/' + rtQ),
			// 日报天数选择器独立于概览时间范围，forceDays 强制按 days 查询
			api.getJson(buildTrendUrl({ days: trendDays, forceDays: true, rootType })),
		]);
		// 旧响应后返回时丢弃,避免覆盖新筛选条件下的数据
		if (mySeq !== dailySeq) return;

		const t = dailyData.today || {};
		const y = dailyData.yesterday || {};

		const fields = [
			// 日期由后端返回,经 escapeHtml 转义后拼入 innerHTML,与表头转义口径一致
			{ key: 'date', label: '日期', tf: v => escapeHtml(v || '-'), yf: v => escapeHtml(v || '-') },
			{ key: 'qa_count', label: 'QA 次数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true },
			{ key: 'good', label: '好评数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true },
			{ key: 'bad', label: '差评数', tf: v => (v || 0).toLocaleString(), yf: v => (v || 0).toLocaleString(), cmp: true, warn: true },
			{ key: 'accuracy', label: '准确率', tf: v => (v * 100 || 0).toFixed(2) + '%', yf: v => (v * 100 || 0).toFixed(2) + '%', cmp: true },
		];

		const diff = (tVal, yVal, warn) => {
			if (tVal == null || yVal == null || (yVal === 0 && tVal === 0)) return '';
			let delta, pct;
			if (typeof tVal === 'number' && typeof yVal === 'number') {
				delta = tVal - yVal;
				pct = yVal === 0 ? null : (delta / Math.abs(yVal)) * 100;
			} else { return ''; }
			const arrow = delta > 0 ? '▲' : delta < 0 ? '▼' : '·';
			/* warn=true 表示"差评数"等反向指标：上升为红，下降为绿；其他正向指标则上升绿下降红 */
			let cls = '';
			if (delta !== 0) {
				if (warn) cls = delta > 0 ? 'diff-down' : 'diff-up';
				else cls = delta > 0 ? 'diff-up' : 'diff-down';
			}
			const pctStr = pct == null ? '—' : (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
			return `<span class="text-sm ml-8 ${cls}">${arrow} ${Math.abs(delta).toLocaleString()} (${pctStr})</span>`;
		};

		// 缓存趋势数据，勾选指标时直接重渲染无需重新请求 API
		dailyTrendData = trendData.trend || [];

		// 渲染多日趋势折线图：QA次数 / 好评 / 差评 / 准确率（双 Y 轴，ECharts）
		const trendChartHtml = renderDailyTrendChart(dailyTrendData);

		// 先销毁旧图表实例，避免 innerHTML 整体替换后残留孤立的 ECharts 实例
		destroyDailyTrendChart();
		// 趋势区 + 摘要表合并为一张卡片，内部用分隔线间隔（同 overview-card / 队列卡片方案）
		box.innerHTML = `
      <div class="card daily-card">
        <div class="daily-trend-section">
          ${trendChartHtml}
        </div>
        <div class="daily-summary-section">
          <div class="card-title">📅 每日摘要对比</div>
          <table class="table table-bordered">
            <thead><tr>
              <th>指标</th><th>今日 (${escapeHtml(t.date || '-')})</th><th>昨日 (${escapeHtml(y.date || '-')})</th><th>环比</th>
            </tr></thead>
            <tbody>
				${fields.map(f => {
					const tv = t[f.key];
					const yv = y[f.key];
					const cmpEl = f.cmp ? diff(tv, yv, f.warn) : '';
					return `<tr><td>${f.label}</td><td>${f.tf(tv)}</td><td>${f.yf(yv)}</td><td>${cmpEl}</td></tr>`;
				}).join('')}
			</tbody>
          </table>
        </div>
      </div>`;

		// DOM 就绪后创建/刷新趋势图实例（空数据时容器不存在则跳过）
		initDailyTrendChart();

		// 天数选择器事件委托：绑在 #dailyBody 上（checkbox 勾选由 TrendChart 组件内部处理）
		if (!box._dailyListener) {
			box.addEventListener('change', (evt) => {
				const sel = evt.target.closest('select[data-action="reload-daily"]');
				if (sel) { loadDailyReport(); return; }
			});
			box._dailyListener = true;
		}
	} catch (e) {
		// 旧请求失败同样忽略,避免过期错误提示干扰当前筛选条件
		if (mySeq !== dailySeq) return;
		box.innerHTML = `<div class="card card-error">加载日报失败：${escapeHtml(e.message || '')}</div>`;
		toast('加载日报失败', 'error');
		console.error('load daily report failed:', e);
	}
}

/**
 * 渲染日报趋势区内容 HTML（不含外层卡片，由 loadDailyReport 组装成一张合并卡片）
 * - 正常数据：输出头部（标题 + 天数选择器）与图表容器，实际图表由 initDailyTrendChart 在 DOM 就绪后创建
 * - 空数据 / 仅 1 天时不建图表，直接返回占位文案
 * - 输入 trend: [{date, qa_count, good, bad, accuracy, avg_total_ms, avg_ttft_ms}, ...]
 */
function renderDailyTrendChart(trend) {
	if (!trend || trend.length === 0) {
		return '<div class="card-title">📈 多日趋势</div><div class="empty">暂无趋势数据</div>';
	}
	if (trend.length === 1) {
		return '<div class="card-title">📈 多日趋势</div><div class="empty">仅 1 天数据，暂无法绘制趋势图</div>';
	}

	/* 读取当前选中的天数，重渲染时保持选中项不变 */
	const curDays = $('#dailyTrendDays')?.value || '30';

	return `
      <div class="daily-chart-header">
        <div class="text-lg fw-600">📈 最近 ${trend.length} 天趋势</div>
        <select class="select select-xs" data-action="reload-daily" id="dailyTrendDays">
          <option value="7" ${curDays === '7' ? 'selected' : ''}>7 天</option>
          <option value="14" ${curDays === '14' ? 'selected' : ''}>14 天</option>
          <option value="30" ${curDays === '30' ? 'selected' : ''}>30 天</option>
        </select>
      </div>
      <div id="dailyTrendChart"></div>`;
}

/* 日报趋势图实例（ECharts）：每次加载重建（DOM 整体替换），勾选状态由 dailyMetricVisible 恢复 */
let dailyTrendChart = null;

/** 销毁日报趋势图实例（重载/空态前调用，释放 ECharts 与 ResizeObserver） */
function destroyDailyTrendChart() {
	if (dailyTrendChart) { dailyTrendChart.destroy(); dailyTrendChart = null; }
}

/** 创建/刷新日报趋势图（ECharts）：需在 renderDailyTrendChart 输出的 DOM 就绪后调用 */
function initDailyTrendChart() {
	const container = $('#dailyTrendChart');
	if (!container) return;
	dailyTrendChart = TrendChart.create({
		container: container,
		series: [
			{ key: 'qa', label: 'QA次数', color: '#2563eb', axis: 'left', visible: dailyMetricVisible.qa, get: t => t.qa_count || 0 },
			{ key: 'good', label: '好评', color: '#059669', axis: 'left', visible: dailyMetricVisible.good },
			{ key: 'bad', label: '差评', color: '#dc2626', axis: 'left', visible: dailyMetricVisible.bad },
			{ key: 'accuracy', label: '准确率', color: '#7c3aed', axis: 'right', dashed: true, visible: dailyMetricVisible.accuracy, get: t => (t.accuracy || 0) * 100 },
		],
		axes: {
			// 左轴：计数值（QA/好评/差评），从 0 起算、不设上限；右轴：准确率百分比，0-100 封顶
			left: { toFixed: 0, includeZero: true, clampMin: 0, clampMax: null },
			right: { unit: '%', toFixed: 0, includeZero: false, clampMin: 0, clampMax: 100, defaultMin: 0, defaultMax: 100, minSpan: 5, padMin: 5, padMax: 5 },
		},
		options: { chartHeight: '100%', legendWidth: 130 },
		// 勾选状态回写开关对象，重载（如切换天数）时用 dailyMetricVisible 恢复勾选
		onToggle: (key, visible) => { dailyMetricVisible[key] = visible; },
	});
	if (dailyTrendChart) dailyTrendChart.render(dailyTrendData);
}
