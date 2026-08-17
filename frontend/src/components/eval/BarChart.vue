<template>
  <svg :width="width" :height="height" :viewBox="`0 0 ${width} ${height}`">
    <!-- Y 轴网格:仅数值刻度模式渲染(比例模式柱子按占比铺满,无需参考线) -->
    <template v-if="maxMode !== 'sum'">
      <g v-for="(g, i) in gridLines" :key="'g' + i">
        <line class="bar-grid" :x1="padLeft" :y1="g.y" :x2="width - 10" :y2="g.y" stroke="#e5e7eb" stroke-width="1" />
        <text class="bar-grid-label" :x="padLeft - 5" :y="g.y + 4" text-anchor="end" fill="#6b7280" font-size="11">{{ g.label }}</text>
      </g>
    </template>

    <!-- 柱子 + 柱顶数值 + 柱底标签（柱体带入场动画） -->
    <g v-for="(b, i) in bars" :key="'b' + i">
      <rect :x="b.x" :y="b.y" :width="barW" :height="b.barH" :fill="b.color" rx="3" opacity="0.85">
        <animate attributeName="height" from="0" :to="b.barH" dur="0.6s" fill="freeze" />
        <animate attributeName="y" :from="height - padBottom" :to="b.y" dur="0.6s" fill="freeze" />
      </rect>
      <text class="bar-value" :x="b.x + barW / 2" :y="b.y - 4" text-anchor="middle" fill="#374151" font-size="11" font-weight="600">{{ b.text }}</text>
      <text class="bar-label" :x="b.x + barW / 2" :y="height - padBottom + 16" text-anchor="middle" fill="#6b7280" font-size="12">{{ b.label }}</text>
    </g>
  </svg>
</template>

<script setup>
import { computed } from 'vue'

/**
 * SVG 分组条形图（原 admin-eval.js renderSvgBarChart 迁移）
 * 供检索增益分析与文档质量分布复用:两图仅刻度模式与布局参数不同
 * - maxMode 'value'=数值刻度(带 Y 轴百分比网格) / 'sum'=比例堆叠(无网格)
 * - valueText: 柱顶文字格式化回调(value)
 */
const props = defineProps({
  data: { type: Array, default: () => [] }, // [{label, value, color}]
  width: { type: Number, default: 500 },
  height: { type: Number, default: 200 },
  padLeft: { type: Number, default: 0 },
  padBottom: { type: Number, default: 30 },
  padTop: { type: Number, default: 10 },
  startX: { type: Number, default: null },
  barGap: { type: Number, default: 10 },
  maxMode: { type: String, default: 'value' },
  valueText: { type: Function, default: v => String(v) },
})

// Number() 强制数值化,防止字段被污染为字符串导致 SVG 注入
const num = v => Number(v) || 0

const startX = computed(() => (props.startX !== null && props.startX !== undefined ? props.startX : (props.padLeft || 20)))
const barW = computed(() => {
  if (!props.data.length) return 0
  return (props.width - props.padLeft - 20) / props.data.length - props.barGap
})
const maxVal = computed(() => {
  if (props.maxMode === 'sum') return props.data.reduce((s, d) => s + num(d.value), 0) || 1
  return Math.max(...props.data.map(d => num(d.value)), 0.1) * 1.2
})

// Y 轴网格线:0%~100% 五等分（仅数值刻度模式）
const gridLines = computed(() => {
  if (props.maxMode === 'sum') return []
  const lines = []
  for (let i = 0; i <= 4; i++) {
    const y = props.padTop + (props.height - props.padTop - props.padBottom) * i / 4
    const val = (maxVal.value * (1 - i / 4)).toFixed(2)
    lines.push({ y, label: (val * 100).toFixed(0) + '%' })
  }
  return lines
})

// 预计算每根柱子的几何坐标与柱顶文字
const bars = computed(() => props.data.map((d, i) => {
  const value = num(d.value)
  const x = startX.value + i * (barW.value + props.barGap)
  const barH = (props.height - props.padTop - props.padBottom) * (value / maxVal.value)
  const y = props.height - props.padBottom - barH
  return { label: d.label, color: d.color, x, y, barH, text: props.valueText(value) }
}))
</script>

<style scoped>
/* 深色模式下 SVG presentation attribute 不响应 CSS 变量,用类覆盖颜色,
   使柱顶数值/柱底标签/网格线跟随主题自动适配 */
.bar-grid {
  stroke: var(--el-border-color-lighter, #e5e7eb);
}

.bar-grid-label,
.bar-label {
  fill: var(--el-text-color-secondary, #6b7280);
}

.bar-value {
  fill: var(--el-text-color-primary, #374151);
}
</style>
