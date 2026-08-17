<template>
  <div class="trend-chart" ref="rootEl"
    :class="{ 'trend-chart--fill': fill }"
    :style="fill ? undefined : { height: height + 'px' }">
    <!-- 空态 / 单点占位：不绘制图表，直接展示文案 -->
    <div v-if="data.length === 0" class="chart-placeholder">{{ emptyText }}</div>
    <div v-else-if="data.length === 1" class="chart-placeholder">{{ singleText }}</div>
    <template v-else>
      <div class="chart-legend" :style="{ width: legendWidth + 'px' }">
        <!-- 图例勾选：与 ECharts 版一致，至少保留一条指标线，避免图表空白 -->
        <div v-for="s in seriesState" :key="s.key" class="legend-item">
          <el-checkbox :model-value="s.visible" size="small" @change="(val) => onToggle(s, val)" />
          <span class="metric-dot" :style="{ background: s.color }"></span>
          <span class="legend-label">{{ s.label }}</span>
        </div>
      </div>
      <div class="chart-plot" ref="plotEl" @mousemove="onMouseMove" @mouseleave="onMouseLeave">
        <svg :width="svgW" :height="svgH">
          <!-- 左轴网格线 + 刻度（仅左轴有可见指标时绘制，作为视觉基准） -->
          <g v-if="leftRange">
            <template v-for="(tk, i) in leftTicks" :key="'lg' + i">
              <line :x1="padLeft" :y1="yPos(tk.v, leftRange)" :x2="svgW - padRight" :y2="yPos(tk.v, leftRange)" class="grid-line" />
              <text :x="padLeft - 6" :y="yPos(tk.v, leftRange) + 3" class="axis-label" text-anchor="end">{{ tickLabel(tk, leftCfg) }}</text>
            </template>
          </g>
          <!-- 右轴刻度（百分比轴）：不画网格线，仅保留刻度标签 -->
          <g v-if="rightRange">
            <template v-for="(tk, i) in rightTicks" :key="'rg' + i">
              <text :x="svgW - padRight + 6" :y="yPos(tk.v, rightRange) + 3" class="axis-label">{{ tickLabel(tk, rightCfg) }}</text>
            </template>
          </g>
          <!-- 耗时轴刻度（ms）：与百分比轴同侧时右移错开刻度标签 -->
          <g v-if="timeRange">
            <template v-for="(tk, i) in timeTicks" :key="'tg' + i">
              <text :x="svgW - padRight + 6 + timeOffset" :y="yPos(tk.v, timeRange) + 3" class="axis-label">{{ tickLabel(tk, timeCfg) }}</text>
            </template>
          </g>
          <!-- X 轴刻度标签：稀疏采样展示，避免数据点多时标签重叠 -->
          <text v-for="i in xTickIndexes" :key="'x' + i" :x="xPos(i)" :y="svgH - 6" class="axis-label" text-anchor="middle">{{ xLabels[i] }}</text>
          <!-- 各指标折线 + 数据点：折线可为平滑曲线（Catmull-Rom 转贝塞尔），点位密集时省略圆点 -->
          <g v-for="s in visibleSeries" :key="s.key">
            <path :d="linePathStr(s)" fill="none" :stroke="s.color" :stroke-width="s.strokeWidth" :stroke-dasharray="s.dashed ? '6 3' : ''" />
            <template v-if="showDots">
              <circle v-for="(p, i) in linePoints(s)" :key="s.key + '-' + i" :cx="p.x" :cy="p.y" r="3" :fill="s.color" />
            </template>
          </g>
          <!-- hover 指示竖线 -->
          <line v-if="hoverIndex >= 0" :x1="xPos(hoverIndex)" :y1="padTop" :x2="xPos(hoverIndex)" :y2="svgH - padBottom" class="hover-line" />
        </svg>
        <!-- hover tooltip：按指标所属轴的单位/小数位展示数值 -->
        <div v-if="hoverIndex >= 0" class="chart-tooltip" :style="{ left: tooltipLeft + 'px', top: '6px' }">
          <div class="tip-date">{{ xLabels[hoverIndex] }}</div>
          <div v-for="s in visibleSeries" :key="s.key" class="tip-row">
            <span class="tip-dot" :style="{ background: s.color }"></span>
            <span class="tip-label">{{ s.label }}</span>
            <b>{{ s.display(hoverData) }}</b>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

/**
 * 轻量 SVG 折线趋势图组件（替代原 ECharts TrendChart，不引入 echarts）
 * 保留原组件语义：
 *  - 图例勾选（至少保留一条指标线），勾选状态存于组件内
 *  - 多坐标轴：left 计数轴 / right 百分比轴 / time 耗时轴（ms），轴范围只统计可见指标
 *  - 容器尺寸变化（含 Tab 切换）通过 ResizeObserver 自动重算
 *  - 空数据 / 仅 1 个数据点时输出占位文案
 *  - fill=true 时图表撑满父容器剩余空间（由父级 flex 分配高度，组件内自动测量），
 *    此时 height 仅作为测量前的初始兜底值
 */
const props = defineProps({
  // 指标线配置：{ key, label, color, axis: 'left'|'right'|'time', dashed, get, visible }
  series: { type: Array, required: true },
  // 数据点数组，每个点含 date 及各指标字段
  data: { type: Array, default: () => [] },
  // 各轴配置：{ left: {...}, right: {...}, time: {...} }（缺省用内置默认）
  axes: { type: Object, default: () => ({}) },
  // X 轴标签取值函数（默认取 date 的 MM-DD）
  xLabel: { type: Function, default: t => String(t.date || '').slice(5) },
  legendWidth: { type: Number, default: 130 },
  // 固定高度（fill=false 时的图表高度，也是 fill 模式的初始兜底值）
  height: { type: Number, default: 320 },
  // fill=true 时去掉固定高度，撑满父容器剩余空间（父容器需为 flex 且限制高度）
  fill: { type: Boolean, default: false },
  // smooth=true 时折线用平滑曲线（Catmull-Rom 转贝塞尔）绘制，适合点位密集的趋势图
  smooth: { type: Boolean, default: false },
  emptyText: { type: String, default: '暂无数据' },
  singleText: { type: String, default: '仅 1 天数据，暂无法绘制趋势图' },
})

/* 各坐标轴默认取值/兜底范围参数（与原 ECharts 版一致） */
const DEFAULT_AXES = {
  left: { toFixed: 0, unit: '', includeZero: false, clampMin: 0, clampMax: 100, minSpan: 5, padMin: 3, padMax: 3, min: undefined, max: undefined },
  right: { toFixed: 2, unit: '', includeZero: true, clampMin: 0, clampMax: null, minSpan: 0.1, padMin: 0.2, padMax: 0.3, min: undefined, max: undefined },
  time: { toFixed: 0, unit: 'ms', includeZero: true, clampMin: 0, clampMax: null, minSpan: 100, padMin: 100, padMax: 200, min: undefined, max: undefined },
}

// 各轴配置：显式配置与默认合并（耗时类指标独立 time 轴，避免毫秒值混入百分比轴）
const leftCfg = { ...DEFAULT_AXES.left, ...(props.axes.left || {}) }
const rightCfg = { ...DEFAULT_AXES.right, ...(props.axes.right || {}) }
const timeCfg = { ...DEFAULT_AXES.time, ...(props.axes.time || {}) }

// 规范化指标线：复制一份响应式状态，勾选在此修改（不改动父组件传入的 series）
function normalizeSeries(s) {
  const item = {
    key: s.key,
    label: s.label || s.key,
    color: s.color || '#2563eb',
    axis: s.axis === 'time' ? 'time' : (s.axis === 'right' ? 'right' : 'left'),
    dashed: !!s.dashed,
    strokeWidth: s.strokeWidth || (s.dashed ? 2.5 : 3),
    get: typeof s.get === 'function' ? s.get : (t => t[s.key] || 0),
    visible: s.visible !== false,
  }
  // 展示值格式化：按指标所属轴的小数位/单位拼接（seriesState 重建后仍需重新绑定）
  const cfg = item.axis === 'right' ? rightCfg : (item.axis === 'time' ? timeCfg : leftCfg)
  item.display = t => item.get(t).toFixed(cfg.toFixed) + cfg.unit
  return item
}
const seriesState = reactive(props.series.map(normalizeSeries))
// series 变化时重建（如队列名动态变化场景）；visible 取新配置值，等同原"销毁重建"语义
watch(() => props.series, (val) => {
  seriesState.splice(0, seriesState.length, ...val.map(normalizeSeries))
}, { deep: true })

// 图例勾选：勾选直接生效；取消时若当前只剩这一条指标线则阻止，避免图表空白
function onToggle(s, val) {
  if (val) { s.visible = true; return }
  // 至少保留一条指标线（检查排除自身后是否还有可见线）
  if (!seriesState.some(x => x.visible && x.key !== s.key)) return
  s.visible = false
}

const visibleSeries = computed(() => seriesState.filter(s => s.visible))

/* ===== 尺寸测量 ===== */
const rootEl = ref(null)
const plotEl = ref(null)
const svgW = ref(800)   // 初始宽度，挂载后按容器实际宽度重算
// 高度：固定模式取 height；fill 模式由 ResizeObserver 按容器实际剩余高度测量
const svgH = ref(props.height)
const padLeft = 54
const padTop = 16
const padBottom = 30
// 耗时轴可见时右侧需额外留白（offset 位移 + 两组刻度标签宽度），否则刻度被裁切
const padRight = computed(() => timeRange.value ? 120 : 60)
// 耗时轴与百分比轴同侧时右移 56px 错开刻度标签（与原版一致）；仅耗时轴可见时无需偏移
const timeOffset = computed(() => (rightRange.value && timeRange.value) ? 56 : 0)
const plotWidth = computed(() => svgW.value - padLeft - padRight.value)
const plotHeight = computed(() => svgH.value - padTop - padBottom)

let resizeObserver = null
// 宽度从绘图区（chart-plot）实测，而不是整个根容器：
// 根容器含图例侧栏（legendWidth + gap），若按根宽绘制，SVG 会比实际绘图区宽、右侧刻度溢出被裁切
function measure() {
  // 容器隐藏（display:none，如 Tab 切走/未布局完成）时 clientWidth 为 0，
  // 此时保留上一次有效宽度，避免 svgW 归零导致绘图区宽度为负、刻度/坐标计算出 NaN
  if (plotEl.value) {
    const w = plotEl.value.clientWidth
    if (w > 0) svgW.value = w
  } else if (rootEl.value) {
    // 空态/单点占位阶段绘图区未渲染，先按根容器宽度兜底
    const w = rootEl.value.clientWidth
    if (w > 0) svgW.value = w
  }
  // fill 模式跟随容器高度变化，避免图表超出父级剩余空间
  if (props.fill && rootEl.value) svgH.value = Math.max(0, rootEl.value.clientHeight)
}
onMounted(() => {
  resizeObserver = new ResizeObserver(measure)
  resizeObserver.observe(rootEl.value)
  if (plotEl.value) resizeObserver.observe(plotEl.value)
  measure()
})
// 空态/单点占位与绘图区切换时 plotEl 创建/销毁，需等 DOM 更新后重新观测并重测
watch(() => props.data, () => {
  nextTick(() => {
    if (plotEl.value && resizeObserver) resizeObserver.observe(plotEl.value)
    measure()
  })
})
onBeforeUnmount(() => {
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
})

/* ===== 坐标轴范围计算（只统计可见指标，未勾选指标不参与，避免拉伸坐标轴） ===== */
function hasVisibleOn(axis) {
  return visibleSeries.value.some(s => s.axis === axis)
}
function computeRange(cfg) {
  const vals = []
  visibleSeries.value.forEach(s => {
    if (s.axis !== cfg._axis) return
    props.data.forEach(t => vals.push(s.get(t)))
  })
  if (!vals.length) return { min: cfg._defaultMin, max: cfg._defaultMax }
  let min = Math.min(...vals, cfg.includeZero ? 0 : Infinity)
  let max = Math.max(...vals)
  min -= cfg.padMin
  max += cfg.padMax
  if (cfg.clampMin != null) min = Math.max(cfg.clampMin, min)
  if (cfg.clampMax != null) max = Math.min(cfg.clampMax, max)
  // 数据区间过窄时保持 max，下探 min 保证最小跨度，避免折线拉平
  if (max - min < cfg.minSpan) {
    min = Math.max(cfg.clampMin != null ? cfg.clampMin : -Infinity, max - cfg.minSpan)
  }
  return { min, max }
}
// 显式配置的 min/max 优先（锁死刻度，不随数据/勾选变化），未提供的部分用动态计算
function axisRange(cfg) {
  const base = computeRange(cfg)
  return { min: cfg.min != null ? cfg.min : base.min, max: cfg.max != null ? cfg.max : base.max }
}

const leftRange = computed(() => hasVisibleOn('left') ? axisRange({ ...leftCfg, _axis: 'left', _defaultMin: 70, _defaultMax: 100 }) : null)
const rightRange = computed(() => hasVisibleOn('right') ? axisRange({ ...rightCfg, _axis: 'right', _defaultMin: 0, _defaultMax: 5 }) : null)
const timeRange = computed(() => hasVisibleOn('time') ? axisRange({ ...timeCfg, _axis: 'time', _defaultMin: 0, _defaultMax: 5000 }) : null)

/* 刻度生成：nice step 让网格线刻度整齐（如 70/80/90/100 而非 70.5/80.25） */
function niceStep(raw) {
  if (raw <= 0) return 1
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const norm = raw / mag
  if (norm < 1.5) return 1 * mag
  if (norm < 3) return 2 * mag
  if (norm < 7) return 5 * mag
  return 10 * mag
}
function makeTicks(range) {
  const span = range.max - range.min
  if (span <= 0) return [{ v: range.min }]
  const step = niceStep(span / 4)
  const lo = Math.floor(range.min / step) * step
  const hi = Math.ceil(range.max / step) * step
  const ticks = []
  for (let i = 0; lo + i * step <= hi + step * 1e-6; i++) ticks.push({ v: lo + i * step })
  return ticks
}
const leftTicks = computed(() => leftRange.value ? makeTicks(leftRange.value) : [])
const rightTicks = computed(() => rightRange.value ? makeTicks(rightRange.value) : [])
const timeTicks = computed(() => timeRange.value ? makeTicks(timeRange.value) : [])

// 坐标映射：数据值 → 画布 Y（基于刻度上下界，保证 max 数据点不贴顶）
function yPos(v, range) {
  const span = range.max - range.min
  if (span <= 0) return padTop
  return padTop + (range.max - v) / span * plotHeight.value
}
function xPos(i) {
  const n = props.data.length
  if (n <= 1) return padLeft
  return padLeft + i / (n - 1) * plotWidth.value
}
function linePoints(s) {
  const range = s.axis === 'right' ? rightRange.value : (s.axis === 'time' ? timeRange.value : leftRange.value)
  if (!range) return []
  return props.data.map((t, i) => ({ x: xPos(i), y: yPos(s.get(t), range) }))
}
// 平滑曲线：把折线点转成 Catmull-Rom 三次贝塞尔路径，点位密集时曲线更平滑不显得生硬；
// 控制点 Y 值裁剪到绘图区上下界内，避免曲线越过绘图区覆盖刻度标签
function smoothPath(points) {
  if (points.length < 2) return ''
  const minY = padTop
  const maxY = svgH.value - padBottom
  let d = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i]
    const p1 = points[i]
    const p2 = points[i + 1]
    const p3 = points[i + 2] || p2
    const c1y = Math.max(minY, Math.min(maxY, p1.y + (p2.y - p0.y) / 6))
    const c2y = Math.max(minY, Math.min(maxY, p2.y - (p3.y - p1.y) / 6))
    d += ` C ${(p1.x + (p2.x - p0.x) / 6).toFixed(2)} ${c1y.toFixed(2)}, ${(p2.x - (p3.x - p1.x) / 6).toFixed(2)} ${c2y.toFixed(2)}, ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`
  }
  return d
}
// 折线 path：smooth 模式输出贝塞尔平滑路径；否则退化为直线连接（等价原 polyline）
function linePathStr(s) {
  const pts = linePoints(s)
  if (props.smooth) return smoothPath(pts)
  return pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join(' ')
}
// 数据点圆点：点数超过阈值时不再逐点绘制，避免点位密集导致画面杂乱
const showDots = computed(() => props.data.length <= 60)
function tickLabel(tk, cfg) {
  return tk.v.toFixed(cfg.toFixed) + cfg.unit
}

const xLabels = computed(() => props.data.map(props.xLabel))

// X 轴刻度稀疏采样：数据点多时只显示部分刻度，避免标签重叠；
// 按绘图区宽度估算可容纳的刻度数（相邻间距约 80px），等间隔采样且含首尾刻度
const xTickIndexes = computed(() => {
  const n = props.data.length
  if (n <= 1) return [0]
  // 至少保留首尾两个刻度：绘图区过窄（< 80px）时 maxCount 为 1，
  // 分母 (maxCount-1) 为 0 会使 step 为 Infinity、刻度索引变成 NaN（SVG 报 attribute x: NaN）
  const maxCount = Math.max(2, Math.floor(plotWidth.value / 80))
  if (n <= maxCount) return Array.from({ length: n }, (_, i) => i)
  const step = (n - 1) / (maxCount - 1)
  const idxs = []
  for (let i = 0; i < maxCount; i++) idxs.push(Math.round(i * step))
  return [...new Set(idxs)]
})

/* ===== hover tooltip ===== */
const hoverIndex = ref(-1)
const hoverData = computed(() => (hoverIndex.value >= 0 && props.data[hoverIndex.value]) || {})
// hover tooltip 定位：接近右边界时向左翻折，避免溢出容器
const tooltipLeft = computed(() => {
  const x = xPos(hoverIndex.value)
  return Math.max(0, Math.min(x + 12, svgW.value - 190))
})
function onMouseMove(e) {
  const rect = plotEl.value.getBoundingClientRect()
  const x = e.clientX - rect.left - padLeft
  const n = props.data.length
  if (n < 2) { hoverIndex.value = -1; return }
  const idx = Math.round(x / plotWidth.value * (n - 1))
  hoverIndex.value = Math.max(0, Math.min(n - 1, idx))
}
function onMouseLeave() {
  hoverIndex.value = -1
}

defineExpose({})
</script>

<style scoped>
.trend-chart {
  display: flex;
  gap: 4px;
  width: 100%;
  min-height: 0;
}

/* fill 模式：去掉固定高度，由父级 flex 分配剩余空间（父容器需为限制高度的 flex 列布局）；
   宽度撑满父级绘图区，min-width/min-height 保证图表在窄/矮容器中仍保留可读尺寸 */
.trend-chart--fill {
  flex: 1;
  height: auto;
  min-width: 320px;
  min-height: 220px;
}

.chart-placeholder {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--app-text-sub);
  font-size: 13px;
}

/* 图例侧栏：垂直排列 + 不被压缩 */
.chart-legend {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  justify-content: center;
  padding: 10px 8px;
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  padding: 4px 6px;
  border-radius: 4px;
  transition: background .15s;
}

.legend-item:hover {
  background: var(--app-menu-hover);
}

.legend-label {
  font-size: 13px;
  color: var(--app-text);
}

/* 指标圆点：用 background 直接着色 */
.metric-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.chart-plot {
  flex: 1;
  min-width: 0;
  position: relative;
}

.chart-plot svg {
  display: block;
}

.grid-line {
  stroke: var(--app-border);
  stroke-dasharray: 4 4;
}

.axis-label {
  font-size: 11px;
  fill: var(--app-text-sub);
}

.hover-line {
  stroke: var(--app-text-sub);
  stroke-width: 1;
}

/* hover tooltip：深色半透明卡片 */
.chart-tooltip {
  position: absolute;
  background: rgba(17, 24, 39, 0.9);
  color: #e5e7eb;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 12px;
  line-height: 1.7;
  pointer-events: none;
  z-index: 5;
  white-space: nowrap;
}

.tip-date {
  font-weight: 600;
  margin-bottom: 2px;
}

.tip-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.tip-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.tip-label {
  color: #d1d5db;
}

.tip-row b {
  color: #fff;
  margin-left: auto;
  padding-left: 12px;
}
</style>
