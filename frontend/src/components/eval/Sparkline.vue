<template>
  <!-- 带均值虚线的 sparkline（原 admin-eval.js buildSparkline 迁移）
       viewBox 宽度取容器实际宽度,preserveAspectRatio 等比缩放 -->
  <svg width="100%" height="32" :viewBox="`0 0 ${W} 32`" class="sparkline" preserveAspectRatio="xMidYMid meet">
    <!-- 空数据：仅一条水平基准线 -->
    <template v-if="!values.length">
      <line x1="0" y1="16" :x2="W" y2="16" stroke="#e5e7eb" stroke-width="1" />
    </template>
    <!-- 单点：均值虚线 + 圆点 -->
    <template v-else-if="pts.length < 2">
      <line :x1="pad" :y1="avgY" :x2="W - pad" :y2="avgY" stroke="#94a3b8" stroke-width="0.8" stroke-dasharray="3,2" opacity="0.6" />
      <circle :cx="pts[0].x" :cy="pts[0].y" r="1.8" fill="#3b82f6" />
    </template>
    <!-- 折线：淡蓝面积 + 均值虚线 + 折线 + 终点圆点 -->
    <template v-else>
      <path :d="areaPath" fill="rgba(59,130,246,0.08)" stroke="none" />
      <line :x1="pad" :y1="avgY" :x2="W - pad" :y2="avgY" stroke="#94a3b8" stroke-width="0.8" stroke-dasharray="3,2" opacity="0.6" />
      <polyline :points="polyline" fill="none" stroke="#3b82f6" stroke-width="1.2" stroke-linejoin="miter" stroke-linecap="butt" />
      <circle :cx="pts[pts.length - 1].x" :cy="pts[pts.length - 1].y" r="1.8" fill="#3b82f6" />
    </template>
  </svg>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  values: { type: Array, default: () => [] }, // 7 日趋势数值
  width: { type: Number, default: 140 },      // 容器实际宽度(像素),用于 viewBox 等比缩放
})

const H = 32
const pad = 2
// viewBox 宽度 = 容器宽度,保证等比缩放时不被拉伸变形
const W = computed(() => Math.max(props.width || 140, 80))

// 计算每个点的坐标（基于有效值 min/max 归一化）
const pts = computed(() => {
  const valid = props.values.filter(v => v > 0)
  const max = valid.length ? Math.max(...valid) : 1
  const min = valid.length ? Math.min(...valid) : 0
  const range = max - min || 1
  const stepX = props.values.length > 1 ? (W.value - 2 * pad) / (props.values.length - 1) : 0
  return props.values.map((v, i) => ({
    x: pad + i * stepX,
    y: H - pad - ((v - min) / range) * (H - 2 * pad),
  }))
})

// 7 日均值(仅基于有效值)对应的 Y 坐标
const avgY = computed(() => {
  const valid = props.values.filter(v => v > 0)
  if (!valid.length) return H / 2
  const max = Math.max(...valid)
  const min = Math.min(...valid)
  const range = max - min || 1
  const avg = valid.reduce((a, b) => a + b, 0) / valid.length
  return H - pad - ((avg - min) / range) * (H - 2 * pad)
})

const polyline = computed(() => pts.value.map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '))
// 面积填充路径：起点 → 各点折线 → 终点 → 回到底部闭合
const areaPath = computed(() => {
  if (!pts.value.length) return ''
  const first = pts.value[0]
  const last = pts.value[pts.value.length - 1]
  return `M${first.x.toFixed(1)},${H - pad} L${polyline.value.split(' ').join(' L')} L${last.x.toFixed(1)},${H - pad} Z`
})
</script>
