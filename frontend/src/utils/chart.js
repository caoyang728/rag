/**
 * 图表 option 构建工具（手写 SVG 图表 → ECharts 迁移用）
 * 所有图表统一由 components/base/VChart.vue 渲染，本模块只负责把业务数据翻译成 echarts option。
 * canvas 图表无法直接解析 CSS 变量，axis/网格/文字等颜色需取 getComputedStyle 计算后的值，
 * 调用方（各面板）依赖 useTheme().isDark 在主题切换时重建 option。
 */

// 主题色：从全局 CSS 变量读取计算后的颜色值（浅色/暗色自动适配）
// dark 参数由调用方传入（依赖 isDark 触发重建），缺省时按 html.dark 类判断
export function chartThemeColors(dark) {
  const isDark = dark ?? document.documentElement.classList.contains('dark')
  const s = getComputedStyle(document.documentElement)
  const text = s.getPropertyValue('--app-text').trim() || '#303133'
  const textSub = s.getPropertyValue('--app-text-sub').trim() || '#909399'
  const border = s.getPropertyValue('--app-border').trim() || '#e4e7ed'
  return {
    text,
    textSub,
    border,
    tooltipBg: isDark ? '#1f242a' : '#ffffff',
    tooltipText: isDark ? '#e6edf3' : '#303133',
    tooltipBorder: isDark ? '#3d444d' : '#e4e7ed',
  }
}

// 各坐标轴默认配置（与原 SVG TrendChart 的 DEFAULT_AXES 对齐）
// includeZero 从 0 起算 / clampMin、clampMax 上下限 / min、max 显式锁死刻度
const DEFAULT_AXES = {
  left: { toFixed: 0, unit: '', includeZero: false, clampMin: 0, clampMax: null, min: undefined, max: undefined },
  right: { toFixed: 2, unit: '', includeZero: true, clampMin: 0, clampMax: null, min: undefined, max: undefined },
  time: { toFixed: 0, unit: 'ms', includeZero: true, clampMin: 0, clampMax: null, min: undefined, max: undefined },
}

// HTML 转义：tooltip formatter 中插入动态数据时必须转义，防止 XSS
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))

// 折线趋势图图例保护：至少保留一条指标线可见（与原版勾选逻辑一致），避免图表空白
export function trendLegendSelectChanged(params, chart) {
  if (!Object.values(params.selected || {}).some(Boolean)) {
    chart.dispatchAction({ type: 'legendSelect', name: params.name })
  }
}

/**
 * 折线趋势图 option 构建
 * @param {Array}  series 指标线配置：{ key, label, color, axis: 'left'|'right'|'time', dashed, strokeWidth, visible, get }
 * @param {Array}  data   数据点数组，每个点含 date 及各指标字段
 * @param {Object} axes   各轴配置 { left, right, time }（缺省用内置默认）
 * @param {Function} xLabel X 轴标签取值函数（默认取 date 的 MM-DD）
 * @param {Boolean} smooth 折线是否平滑
 * @param {Object} colors 主题色（chartThemeColors 输出），缺省自动读取
 */
export function buildTrendOption({ series, data, axes = {}, xLabel, smooth = false, colors }) {
  const theme = colors || chartThemeColors()
  const xs = data.map(xLabel || (t => String(t.date || '').slice(5)))
  const visible = series.filter(s => s.visible !== false)
  const axisOrder = ['left', 'right', 'time']
  // 只保留有可见指标的坐标轴，避免出现空轴
  const activeAxes = axisOrder.filter(k => visible.some(s => (s.axis || 'left') === k))

  // 图例初始勾选态：与各面板 series.visible 一致（未勾选指标默认隐藏）
  const selected = {}
  series.forEach(s => { selected[s.label || s.key] = s.visible !== false })

  const yAxis = activeAxes.map((k, i) => {
    const cfg = { ...DEFAULT_AXES[k], ...(axes[k] || {}) }
    const axis = {
      type: 'value',
      position: i === 0 ? 'left' : 'right',
      // 右侧双轴（百分比 + 耗时）时 time 轴右移 56px 错开刻度（与原版一致）
      offset: k === 'time' && activeAxes.includes('right') ? 56 : 0,
      axisLabel: {
        color: theme.textSub,
        fontSize: 11,
        formatter: v => v.toFixed(cfg.toFixed) + cfg.unit,
      },
      axisLine: { lineStyle: { color: theme.border } },
      axisTick: { show: false },
      // 只保留左轴网格线作视觉基准（与原版逻辑一致），右/耗时轴仅刻度
      splitLine: i === 0
        ? { lineStyle: { color: theme.border, type: 'dashed' } }
        : { show: false },
      nameTextStyle: { color: theme.textSub },
    }
    // 显式 min/max 优先，其次 clamp 上下限，再按 includeZero 决定是否从 0 起算
    if (cfg.min != null) axis.min = cfg.min
    else if (cfg.clampMin != null) axis.min = cfg.clampMin
    else if (cfg.includeZero) axis.min = 0
    if (cfg.max != null) axis.max = cfg.max
    else if (cfg.clampMax != null) axis.max = cfg.clampMax
    return axis
  })

  // tooltip 数值格式化：按指标所属轴的 toFixed/unit 展示（与原版 display 一致）
  const fmtMap = {}
  visible.forEach(s => {
    const k = s.axis || 'left'
    const cfg = { ...DEFAULT_AXES[k], ...(axes[k] || {}) }
    fmtMap[s.label || s.key] = v => v.toFixed(cfg.toFixed) + cfg.unit
  })
  // tooltip 为 HTML 字符串，esc 已在模块顶层定义，此处直接复用
  const hasTime = activeAxes.includes('time')
  const hasRight = activeAxes.includes('right')

  return {
    legend: {
      type: 'scroll',
      top: 0,
      left: 0,
      textStyle: { color: theme.text, fontSize: 12 },
      itemWidth: 12,
      itemHeight: 12,
      selected,
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: theme.tooltipBg,
      borderColor: theme.tooltipBorder,
      textStyle: { color: theme.tooltipText, fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: theme.border } },
      formatter(params) {
        const list = Array.isArray(params) ? params : [params]
        const head = `<div style="font-weight:600;margin-bottom:2px">${esc(xs[list[0].dataIndex] ?? '')}</div>`
        const rows = list.map(p => {
          const raw = p.value
          const v = typeof raw === 'number' && isFinite(raw) ? raw : 0
          const fmt = fmtMap[p.seriesName] || (x => x)
          return '<div style="display:flex;align-items:center;gap:6px">'
            + `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color}"></span>`
            + `<span>${esc(p.seriesName)}</span>`
            + `<b style="margin-left:auto;padding-left:12px">${esc(fmt(v))}</b></div>`
        }).join('')
        return head + rows
      },
    },
    grid: {
      left: 54,
      right: hasTime ? (hasRight ? 160 : 110) : 64,
      top: 40, // 顶部留出图例行
      bottom: 30,
    },
    xAxis: {
      type: 'category',
      data: xs,
      boundaryGap: false,
      axisLabel: { color: theme.textSub, fontSize: 11 },
      axisLine: { lineStyle: { color: theme.border } },
      axisTick: { show: false },
    },
    yAxis,
    series: visible.map(s => ({
      name: s.label || s.key,
      type: 'line',
      smooth: !!smooth,
      yAxisIndex: activeAxes.indexOf(s.axis || 'left'),
      // 数据点密集时省略圆点，避免画面杂乱（与原版一致）
      showSymbol: data.length <= 60,
      data: data.map(t => (typeof s.get === 'function' ? s.get(t) : (t[s.key] ?? 0))),
      lineStyle: { width: s.strokeWidth || (s.dashed ? 2.5 : 3), type: s.dashed ? 'dashed' : 'solid', color: s.color },
      itemStyle: { color: s.color },
      emphasis: { focus: 'series' },
    })),
  }
}

/**
 * 柱状图 option 构建（对应原 SVG BarChart）
 * @param {Array} data [{ label, value, color }]
 * @param {Function} valueText 柱顶文字格式化回调(value)
 * @param {String} maxMode 'value'=数值刻度(带 Y 轴百分比网格) / 'sum'=比例堆叠(无网格)
 * @param {Object} colors 主题色，缺省自动读取
 */
export function buildBarOption({ data, valueText, maxMode = 'value', colors }) {
  const theme = colors || chartThemeColors()
  const values = data.map(d => Number(d.value) || 0)
  // 数值刻度模式：max 取最大值上浮 20% 留出柱顶文字空间（与原版一致）；
  // 比例模式按总和归一（柱高=占比），无网格
  const maxVal = maxMode === 'sum'
    ? values.reduce((s, v) => s + v, 0) || 1
    : Math.max(...values, 0.1) * 1.2
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: theme.tooltipBg,
      borderColor: theme.tooltipBorder,
      textStyle: { color: theme.tooltipText, fontSize: 12 },
    },
    grid: { left: maxMode === 'sum' ? 16 : 44, right: 20, top: 28, bottom: 28 },
    xAxis: {
      type: 'category',
      data: data.map(d => d.label),
      axisLabel: { color: theme.textSub, fontSize: 12 },
      axisLine: { lineStyle: { color: theme.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      max: maxVal,
      // 比例模式隐藏网格与刻度（原版 maxMode='sum' 无网格）；数值刻度模式按百分比展示
      show: maxMode !== 'sum',
      axisLabel: maxMode === 'sum'
        ? { show: false }
        : { color: theme.textSub, fontSize: 11, formatter: v => (v * 100).toFixed(0) + '%' },
      splitLine: maxMode === 'sum'
        ? { show: false }
        : { lineStyle: { color: theme.border, type: 'dashed' } },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 48,
      itemStyle: { borderRadius: [3, 3, 0, 0] },
      label: {
        show: true,
        position: 'top',
        color: theme.text,
        fontSize: 11,
        formatter: p => (typeof valueText === 'function' ? valueText(p.value) : p.value),
      },
      data: data.map(d => ({ value: Number(d.value) || 0, itemStyle: { color: d.color } })),
    }],
  }
}

/**
 * 雷达图 option 构建（对应原 SVG RadarChart）
 * @param {Object} groups 后端 overview.dimension_groups（按 4 大类分组,含各维度 avg）
 * @param {Array}  dims   需绘制的维度 key 列表（有序,受展示维度白名单过滤）
 * @param {Object} labels 维度 key → 中文名
 * @param {Object} colors 主题色，缺省自动读取
 */
export function buildRadarOption({ groups, dims, labels = {}, colors }) {
  const theme = colors || chartThemeColors()
  // 各维度得分：从对应分组中查找该维度 avg，找不到补 0（与原版一致）；clamp 到 0~1 防止超出外圈
  const values = dims.map(d => {
    let avg = 0
    for (const g of Object.values(groups || {})) {
      const found = (g.dimensions || []).find(x => x.name === d)
      if (found) { avg = found.avg; break }
    }
    return Math.max(0, Math.min(1, Number(avg) || 0))
  })
  return {
    tooltip: {
      backgroundColor: theme.tooltipBg,
      borderColor: theme.tooltipBorder,
      textStyle: { color: theme.tooltipText, fontSize: 12 },
    },
    radar: {
      indicator: dims.map(d => ({ name: labels[d] || d, max: 1 })),
      radius: '68%',
      splitNumber: 4,
      axisName: { color: theme.textSub, fontSize: 10 },
      splitLine: { lineStyle: { color: theme.border } },
      splitArea: { show: false },
      axisLine: { lineStyle: { color: theme.border } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: values,
        name: '质量分',
        areaStyle: { color: 'rgba(59,130,246,0.2)' },
        lineStyle: { color: '#3b82f6', width: 2 },
        itemStyle: { color: '#3b82f6' },
      }],
    }],
  }
}

/**
 * 延迟直方图：智能合并后端 100ms 细粒度桶为可读区间（目标 6~12 个柱）
 * 后端 build_latency_histogram 按100ms 分桶（"0-100", "100-200", ...），数据量大时桶数爆炸、柱极窄。
 * 本函数根据数据范围动态选择合适的合并宽度，保证柱数在合理区间。
 * @param {Object} rawHist 后端返回的 { "0-100": 123, "100-200": 456, ... }
 * @returns {{ labels: string[], values: number[], total: number }} 合并后的标签、值、总数
 */
export function mergeHistogramBuckets(rawHist) {
  if (!rawHist || Object.keys(rawHist).length === 0) {
    return { labels: [], values: [], total: 0 }
  }
  // 解析桶范围：提取每个桶的 [start, end]
  const buckets = Object.entries(rawHist).map(([k, v]) => {
    const [s, e] = k.split('-').map(Number)
    return { start: s, end: e, count: v || 0 }
  }).sort((a, b) => a.start - b.start)

  const minVal = buckets[0].start
  const maxVal = buckets[buckets.length - 1].end
  const range = maxVal - minVal || 1

  // 目标 6~12 个柱，计算合并宽度
  const rawWidth = buckets[0].end - buckets[0].start // 原始桶宽（通常 100ms）
  let mergedWidth = rawWidth
  // 尝试逐步倍增合并宽度，直到柱数降到 12 以内
  for (let w = rawWidth; w < range / 5; w *= 2) {
    mergedWidth = w
    const numBuckets = Math.ceil(range / mergedWidth)
    if (numBuckets <= 12) break
  }
  // 确保至少合并到合理宽度（如数据跨度 > 5s 时至少 500ms 一桶）
  if (range > 5000) mergedWidth = Math.max(mergedWidth, 500)
  if (range > 30000) mergedWidth = Math.max(mergedWidth, 2000)
  if (range > 120000) mergedWidth = Math.max(mergedWidth, 10000)

  // 按合并宽度重新分桶
  const merged = {}
  buckets.forEach(b => {
    const start = Math.floor(b.start / mergedWidth) * mergedWidth
    const end = start + mergedWidth
    const label = end >= 1000 ? `${(start / 1000).toFixed(start % 1000 === 0 ? 0 : 1)}s` : `${start}ms`
    if (!merged[label]) merged[label] = { start, count: 0 }
    merged[label].count += b.count
  })

  const sorted = Object.values(merged).sort((a, b) => a.start - b.start)
  const total = sorted.reduce((s, b) => s + b.count, 0)
  return {
    labels: sorted.map(b => {
      const end = b.start + mergedWidth
      const fmt = v => v >= 1000 ? (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + 's' : v + 'ms'
      return `${fmt(b.start)}~${fmt(end)}`
    }),
    values: sorted.map(b => b.count),
    total,
  }
}

/**
 * 延迟直方图 ECharts option
 * @param {Object} rawHist 后端 latency_histogram
 * @param {Object} colors 主题色
 */
export function buildHistogramOption({ rawHist, colors }) {
  const theme = colors || chartThemeColors()
  const { labels, values, total } = mergeHistogramBuckets(rawHist)
  if (labels.length === 0) return null
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: theme.tooltipBg,
      borderColor: theme.tooltipBorder,
      textStyle: { color: theme.tooltipText, fontSize: 12 },
      formatter(params) {
        const p = Array.isArray(params) ? params[0] : params
        const pct = total > 0 ? (p.value / total * 100).toFixed(1) : '0.0'
        return `<b>${esc(p.name)}</b><br/>数量: ${p.value.toLocaleString()} (${pct}%)`
      },
    },
    grid: { left: 48, right: 16, top: 16, bottom: 30 },
    xAxis: {
      type: 'category',
      data: labels,
      axisLabel: { color: theme.textSub, fontSize: 10, rotate: labels.length > 8 ? 30 : 0 },
      axisLine: { lineStyle: { color: theme.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: theme.textSub, fontSize: 11 },
      splitLine: { lineStyle: { color: theme.border, type: 'dashed' } },
    },
    series: [{
      type: 'bar',
      data: values,
      barMaxWidth: 40,
      itemStyle: { color: '#3b82f6', borderRadius: [3, 3, 0, 0] },
    }],
  }
}

/**
 * 错误分布 ECharts option（水平条形图，按次数降序）
 * @param {Object} errDist 后端 error_distribution { "timeout": 5, "network": 2 }
 * @param {Object} colors 主题色
 */
export function buildErrorDistOption({ errDist, colors }) {
  const theme = colors || chartThemeColors()
  const entries = Object.entries(errDist || {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return null
  const total = entries.reduce((s, [, v]) => s + v, 0)
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: theme.tooltipBg,
      borderColor: theme.tooltipBorder,
      textStyle: { color: theme.tooltipText, fontSize: 12 },
      formatter(params) {
        const p = Array.isArray(params) ? params[0] : params
        const pct = total > 0 ? (p.value / total * 100).toFixed(1) : '0.0'
        return `<b>${esc(p.name)}</b><br/>数量: ${p.value} (${pct}%)`
      },
    },
    grid: { left: 120, right: 60, top: 8, bottom: 8 },
    xAxis: {
      type: 'value',
      axisLabel: { color: theme.textSub, fontSize: 11 },
      splitLine: { lineStyle: { color: theme.border, type: 'dashed' } },
    },
    yAxis: {
      type: 'category',
      data: entries.map(([k]) => k || 'unknown'),
      axisLabel: { color: theme.textSub, fontSize: 11 },
      axisLine: { lineStyle: { color: theme.border } },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      data: entries.map(([, v]) => ({
        value: v,
        label: {
          show: true,
          position: 'right',
          color: theme.text,
          fontSize: 11,
          formatter: ({ value }) => `${value} (${total > 0 ? (value / total * 100).toFixed(1) : 0}%)`,
        },
      })),
      barMaxWidth: 24,
      itemStyle: { color: '#dc2626', borderRadius: [0, 3, 3, 0] },
    }],
  }
}
