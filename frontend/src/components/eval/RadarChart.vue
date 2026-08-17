<template>
  <!-- 画布 440x380：中心 (220,190)，左右留空间给长标签,上下余量充足,渲染比例接近正方形 -->
  <svg viewBox="0 0 440 380" class="radar-chart">
    <!-- 展示维度不足 3 个时雷达图无法成型（至少需要三角形），给出空态提示 -->
    <text v-if="dims.length < 3" x="220" y="190" text-anchor="middle" fill="#9ca3af" font-size="13">展示维度不足 3 个，无法绘制雷达图</text>
    <template v-else>
      <!-- 背景网格（4 圈） -->
      <polygon v-for="r in 4" :key="'g' + r" :points="gridPoints(r / 4)" fill="none" stroke="#e5e7eb" stroke-width="1" />
      <!-- 轴线 + 维度标签 -->
      <g v-for="(d, i) in dims" :key="d">
        <line :x1="cx" :y1="cy" :x2="axisX(i)" :y2="axisY(i)" stroke="#e5e7eb" stroke-width="1" />
        <text :x="labelX(i)" :y="labelY(i)" text-anchor="middle" dominant-baseline="middle" font-size="10" fill="#6b7280">{{ labels[d] || d }}</text>
      </g>
      <!-- 数据多边形 + 数据点 -->
      <polygon :points="dataPtsStr" fill="rgba(59,130,246,0.2)" stroke="#3b82f6" stroke-width="2" />
      <circle v-for="(p, i) in dataPts" :key="'d' + i" :cx="p.x" :cy="p.y" r="3" fill="#3b82f6" />
    </template>
  </svg>
</template>

<script setup>
import { computed } from 'vue'

/**
 * 12 维质量画像雷达图（原 admin-eval.js renderRadarChart 迁移）
 * 传入按展示维度白名单过滤后的维度顺序,未勾选的维度不绘制
 * @param {Object} groups 后端 overview.dimension_groups（按 4 大类分组,含各维度 avg）
 * @param {Array}  dims   需绘制的维度 key 列表（有序）
 * @param {Object} labels 维度 key → 中文名（默认取 DIM_LABEL）
 */
const props = defineProps({
  groups: { type: Object, default: () => ({}) },
  dims: { type: Array, default: () => [] },
  labels: { type: Object, default: () => ({}) },
})

const cx = 220
const cy = 190
const R = 130
const n = computed(() => props.dims.length)

// 角度：从 -90°（正上方）开始顺时针均分
function angle(i) {
  return -Math.PI / 2 + i * 2 * Math.PI / n.value
}

// 网格多边形顶点（半径比例 rr）
function gridPoints(rr) {
  return props.dims.map((_, i) => {
    const a = angle(i)
    return `${cx + R * rr * Math.cos(a)},${cy + R * rr * Math.sin(a)}`
  }).join(' ')
}

function axisX(i) { return cx + R * Math.cos(angle(i)) }
function axisY(i) { return cy + R * Math.sin(angle(i)) }
function labelX(i) { return cx + (R + 18) * Math.cos(angle(i)) }
function labelY(i) { return cy + (R + 18) * Math.sin(angle(i)) }

// 各维度得分：从对应分组中查找该维度 avg,找不到补 0
const values = computed(() => props.dims.map(d => {
  for (const g of Object.values(props.groups)) {
    const found = (g.dimensions || []).find(x => x.name === d)
    if (found) return found.avg
  }
  return 0
}))

// 数据点坐标（得分 clamp 到 0~1 防止超出外圈）
const dataPts = computed(() => values.value.map((v, i) => {
  const rr = R * Math.max(0, Math.min(1, v))
  return { x: cx + rr * Math.cos(angle(i)), y: cy + rr * Math.sin(angle(i)) }
}))
const dataPtsStr = computed(() => dataPts.value.map(p => `${p.x},${p.y}`).join(' '))
</script>

<style scoped>
.radar-chart {
  width: 100%;
  height: auto;
  display: block;
}
</style>
