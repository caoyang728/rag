import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { errMsg } from '../utils/format'

/**
 * 列表加载统一封装：收敛各页面重复的
 * `loading + 请求序号守卫（防快速筛选/翻页时旧响应覆盖新状态）+ 失败错误提示` 三件套。
 * 业务拉取逻辑通过 fetchFn 注入，内部只负责状态与竞态，无需再手写 seq/loading/ElMessage。
 * @param {(...args: any[]) => Promise<void>} fetchFn 实际拉取逻辑（内部写入业务 ref），
 *        失败时向上抛出由本组件统一提示；支持接收 load() 透传的参数（如页码/静默标记）
 * @param {{
 *   errorPrefix?: string,
 *   directError?: boolean | ((e: any) => boolean),
 *   onError?: (e: any, ctx: { silent: boolean }) => void,
 * }} [opts]
 * - errorPrefix: 错误提示前缀，默认"加载失败"
 * - directError: 业务错误（如 403 无权限）直接展示后端消息、不加前缀；可为函数按错误定制判定
 * - onError: 自定义错误处理（覆盖默认提示），适合需要"静默失败保留旧数据"或固定文案的场景；
 *            ctx.silent 表示本次是否为静默刷新
 * @returns {{ loading: import('vue').Ref<boolean>, load: (...args: any[]) => Promise<void>, refresh: (...args: any[]) => Promise<void> }}
 */
export function useListLoader(fetchFn, opts = {}) {
  const { errorPrefix = '加载失败', directError, onError } = opts
  const loading = ref(false)
  let seq = 0 // 请求版本号：只有最新一次请求的结果允许写入状态

  async function load(...args) {
    const s = ++seq
    // 静默刷新（轮询等场景）：不显示 loading、失败不打扰用户，由调用方以 { silent: true } 触发；
    // 兼容 load({silent}) 与 load(page, {silent}) 两种传参位置
    const silent = args.some(a => a && typeof a === 'object' && a.silent === true)
    if (!silent) loading.value = true
    try {
      await fetchFn(...args)
    } catch (e) {
      // 旧请求失败同样丢弃，避免错误提示被过期请求触发
      if (s !== seq) return
      if (onError) {
        onError(e, { silent })
        return
      }
      if (silent) return
      // 业务错误（如 403 无权限）直接展示后端消息；其余加统一前缀
      if (typeof directError === 'function' ? directError(e) : directError) {
        ElMessage.error(errMsg(e, '未知错误'))
      } else {
        ElMessage.error(`${errorPrefix}：${errMsg(e, '未知错误')}`)
      }
    } finally {
      if (s === seq && !silent) loading.value = false
    }
  }

  // 手动刷新：语义与 load 一致，供"重试/刷新按钮"调用
  function refresh(...args) {
    return load(...args)
  }

  return { loading, load, refresh }
}
