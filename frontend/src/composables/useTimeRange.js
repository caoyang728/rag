import { ref } from 'vue'

// 常用时间窗口选项：评估/分析类面板统一的三档范围（label 文案与 value 保持一致，避免各页手写重复）
export const TIME_RANGE_OPTIONS = [
  { label: '最近 7 天', value: '7' },
  { label: '最近 30 天', value: '30' },
  { label: '最近 90 天', value: '90' },
]

/**
 * 时间窗口状态与选项：收敛各评估/分析面板重复书写的
 * `const days = ref('7')` + 三档 `<el-option label="最近 X 天">`。
 * @param {Array<{label:string,value:string}>} [options] 自定义选项（默认 TIME_RANGE_OPTIONS；个别面板只展示两档时传入裁剪后的数组）
 * @param {string} [defaultDays] 默认选中值
 * @returns {{ days: import('vue').Ref<string>, options: Array<{label:string,value:string}> }}
 */
export function useTimeRange(options = TIME_RANGE_OPTIONS, defaultDays = '7') {
  const days = ref(defaultDays)
  return { days, options }
}
