// 通用文件下载工具：统一"创建对象 URL → 触发 <a> 点击 → 撤销 URL"流程
// 业务背景：多个页面各自手写 createObjectURL + 创建 <a> + click + revoke，
// 且细节有漂移（是否 append/remove、是否延迟 revoke），统一收敛到这里避免差异。

/**
 * 下载 Blob 文件
 * @param {Blob} blob 文件内容
 * @param {string} filename 下载文件名
 * @param {{ revokeDelay?: number }} [opts] revokeDelay：撤销对象 URL 的延迟毫秒数。
 *        大文件下载偶发中断时传 >0（如 10000），其余场景默认立即撤销。
 */
export function downloadBlob(blob, filename, opts = {}) {
  const { revokeDelay = 0 } = opts
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  if (revokeDelay > 0) {
    setTimeout(() => { URL.revokeObjectURL(url) }, revokeDelay)
  } else {
    URL.revokeObjectURL(url)
  }
}

/**
 * 导出 CSV 文件：自动加 UTF-8 BOM（EF BB BF），解决 Excel 打开中文乱码。
 * 收敛各页面各自拼 BOM + new Blob([...], { type: 'text/csv' }) 的重复实现。
 * @param {string} filename 下载文件名（含 .csv 后缀）
 * @param {string} content  完整 CSV 文本（header 与 rows 拼接后的内容，不含 BOM）
 */
export function exportCsv(filename, content) {
  const blob = new Blob(['\uFEFF' + content], { type: 'text/csv;charset=utf-8' })
  downloadBlob(blob, filename)
}
