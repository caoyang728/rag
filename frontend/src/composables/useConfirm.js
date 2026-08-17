import { ElMessage, ElMessageBox } from 'element-plus'
import { errMsg } from '../utils/format'

/**
 * 确认弹窗统一封装：收敛各页面反复书写的
 * `ElMessageBox.confirm(...).then(async () => { api.xxx }).catch(() => {})` 同构代码。
 * 用户取消（close/cancel）时静默不提示；action 抛错时统一错误提示。
 * @returns {{ confirm: (opts: object, action?: () => Promise<void>) => Promise<boolean> }}
 */
export function useConfirm() {
  /**
   * 弹出确认框，确认后（可选）执行 action
   * @param {object} opts
   * @param {string} opts.message 确认文案
   * @param {string} [opts.title] 标题，默认"提示"
   * @param {string} [opts.confirmText] 确认按钮文案，默认"确定"
   * @param {string} [opts.cancelText] 取消按钮文案，默认"取消"
   * @param {string} [opts.type] 弹窗类型 warning/info/success，默认 warning
   * @param {boolean} [opts.dangerouslyUseHTMLString] 是否将 message 视为 HTML（需自行转义变量），默认 false
   * @param {string} [opts.errorText] action 失败时的提示前缀，默认"操作失败"
   * @param {(e: any) => boolean} [opts.onError] 返回 false 时静默吞掉错误（部分接口允许部分失败）
   * @param {() => Promise<void>} [action] 确认后执行的业务逻辑；省略时仅返回确认结果（await 场景）
   * @returns {Promise<boolean>} 确认成功返回 true；取消返回 false；action 失败返回 false
   */
  async function confirm(opts, action) {
    const {
      message,
      title = '提示',
      confirmText = '确定',
      cancelText = '取消',
      type = 'warning',
      dangerouslyUseHTMLString = false,
      errorText = '操作失败',
      onError,
    } = opts
    try {
      await ElMessageBox.confirm(message, title, {
        confirmButtonText: confirmText,
        cancelButtonText: cancelText,
        type,
        dangerouslyUseHTMLString,
      })
    } catch (e) {
      // 用户取消或点击遮罩关闭：静默，不提示错误
      return false
    }
    if (!action) return true
    try {
      await action()
      return true
    } catch (e) {
      if (onError && onError(e) === false) return false
      ElMessage.error(`${errorText}：${errMsg(e, '未知错误')}`)
      return false
    }
  }

  return { confirm }
}
