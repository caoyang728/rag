<template>
  <div ref="el" class="v-chart"></div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, BarChart, RadarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

// 按需注册：只引入本项目用到的图表/组件，避免全量打包（echarts 全量 ~1MB+）
echarts.use([LineChart, BarChart, RadarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

/**
 * 统一 ECharts 封装：收敛初始化 / setOption / resize / dispose，
 * 所有图表共用同一套生命周期与响应式
 * - option 变化时重渲染（notMerge 整体替换）
 * - 容器尺寸变化（含 Tab 切换、窗口缩放、flex 布局调整）通过 ResizeObserver 自动 resize
 * - events 透传 echarts 事件：{ 'legendselectchanged': (params, chart) => void }
 */
const props = defineProps({
  // echarts option（由各业务面板构建）
  option: { type: Object, required: true },
  // echarts 事件回调：name 为事件名，handler 接收 (params, chartInstance)
  events: { type: Object, default: () => ({}) },
})

const el = ref(null)
const chart = shallowRef(null)
let resizeObserver = null
let lastEvents = {} // 记录上一次绑定的事件，便于变更时解绑旧的再绑定新的

function bindEvents(events) {
  if (!chart.value) return
  // 先解绑旧事件
  Object.keys(lastEvents).forEach(name => chart.value.off(name))
  // 绑定新事件
  Object.entries(events).forEach(([name, handler]) => {
    chart.value.on(name, (params) => handler(params, chart.value))
  })
  lastEvents = events
}

function render() {
  if (!chart.value) {
    chart.value = echarts.init(el.value)
  }
  // 事件绑定与 option 分离：首次渲染和 events 变更时均能正确绑定
  bindEvents(props.events)
  chart.value.setOption(props.option, { notMerge: true })
}

function resize() {
  // 容器隐藏（display:none）时 clientWidth 为 0，echarts resize 会留空白；
  // 恢复可见时 ResizeObserver 触发再测量，故此处对 0 尺寸直接跳过
  if (chart.value && el.value && el.value.clientWidth > 0 && el.value.clientHeight > 0) {
    chart.value.resize()
  }
}

onMounted(() => {
  render()
  resizeObserver = new ResizeObserver(resize)
  if (el.value) resizeObserver.observe(el.value)
})

onBeforeUnmount(() => {
  if (resizeObserver) { resizeObserver.disconnect(); resizeObserver = null }
  chart.value?.dispose()
  chart.value = null
})

watch(() => props.option, render, { deep: true })
</script>

<style scoped>
.v-chart {
  width: 100%;
  height: 100%;
}
</style>
