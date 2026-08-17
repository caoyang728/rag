import { ref } from 'vue'

/**
 * 列表分页状态与事件处理：统一"页码/每页条数 + 翻页/改每页条数后重新加载"逻辑，
 * 避免各列表页重复写 `page.value = p; loadXxx()` 与 `pageSize.value = s; page.value = 1; loadXxx()`。
 * @param {(page?: number) => void} loadFn 加载函数；函数声明可后置（hoisting），
 *        加载函数若内部读取返回的 page/pageSize ref 则无需接收页码参数
 * @param {{ initialPage?: number, initialSize?: number }} [opts] 初始页码/每页条数
 * @returns {{ page: import('vue').Ref<number>, pageSize: import('vue').Ref<number>,
 *             onPageChange: (p:number)=>void, onPageSizeChange: (s:number)=>void,
 *             reset: ()=>void, guardOverflow: (total:number)=>boolean }}
 */
export function usePagination(loadFn, opts = {}) {
  const { initialPage = 1, initialSize = 20 } = opts
  const page = ref(initialPage)
  const pageSize = ref(initialSize)

  // 翻页：更新页码后重新加载；loadFn 需要显式页码时可接收传入的 p
  function onPageChange(p) {
    page.value = p
    loadFn(p)
  }

  // 改每页条数：每页条数变化后回到第 1 页再加载，避免停留在越界页
  function onPageSizeChange(s) {
    pageSize.value = s
    page.value = 1
    loadFn(1)
  }

  // 筛选/切换条件变化：重置回第 1 页并重新加载
  function reset() {
    page.value = 1
    loadFn(1)
  }

  // 列表加载完成后调用：数据总量减少（删除/过滤/任务清理等）导致当前页越界时，
  // 回退到最后一页并重新加载，避免停留在空白页；返回是否发生了回退（true 时调用方应直接 return）
  function guardOverflow(total) {
    const totalPages = Math.max(1, Math.ceil((total || 0) / pageSize.value))
    if (page.value <= totalPages) return false
    page.value = totalPages
    loadFn(totalPages)
    return true
  }

  return { page, pageSize, onPageChange, onPageSizeChange, reset, guardOverflow }
}
