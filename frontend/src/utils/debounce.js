// 通用防抖工具：多个页面手写 "clearTimeout + setTimeout(300ms)" 模式，统一收敛

/**
 * 防抖：delay 毫秒内重复调用只执行最后一次
 * @param {Function} fn 目标函数
 * @param {number} [delay=300] 延迟毫秒数
 * @returns {(Function & { cancel: () => void })} 防抖后的函数；cancel() 用于组件卸载时取消挂起的执行
 */
export function debounce(fn, delay = 300) {
  let timer = null
  const wrapped = (...args) => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      timer = null
      fn(...args)
    }, delay)
  }
  wrapped.cancel = () => {
    if (timer) clearTimeout(timer)
    timer = null
  }
  return wrapped
}
