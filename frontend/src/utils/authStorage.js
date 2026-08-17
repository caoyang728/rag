/**
 * 登录态存储工具：根据"记住我"选择 localStorage / sessionStorage
 *
 * - 记住我（默认勾选）→ localStorage：关闭浏览器后仍保持登录（refresh token 7 天）
 * - 不记住我 → sessionStorage：关闭浏览器即登出（后端同时收紧 refresh token 到 24 小时）
 *
 * rag_remember 标记始终存 localStorage，用于页面刷新后判断应从哪个存储读取 token。
 * 切换"记住我"选项时会清理另一存储中的残留 token，避免读到过期登录态。
 */

const REMEMBER_FLAG = 'rag_remember'
const TOKEN_KEYS = ['rag_access', 'rag_refresh', 'rag_user']

// 当前是否处于"记住我"状态（默认按记住处理，兼容旧登录态）
export function getRemember() {
  return localStorage.getItem(REMEMBER_FLAG) !== '0'
}

function targetStore() {
  return getRemember() ? localStorage : sessionStorage
}

// 读取 access token（从当前生效的存储）
export function getToken() {
  return targetStore().getItem('rag_access')
}

// 读取 refresh token（从当前生效的存储）
export function getRefreshToken() {
  return targetStore().getItem('rag_refresh')
}

// 读取用户信息（从当前生效的存储，解析失败返回 null）
export function getUser() {
  const raw = targetStore().getItem('rag_user')
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

// 写入用户信息到当前生效的存储
export function saveUser(user) {
  targetStore().setItem('rag_user', JSON.stringify(user))
}

// 登录成功后保存完整登录态：按 remember 决定存储位置，并清理另一存储残留
export function saveLoginState({ access, refresh, user, remember }) {
  localStorage.setItem(REMEMBER_FLAG, remember ? '1' : '0')
  const store = remember ? localStorage : sessionStorage
  store.setItem('rag_access', access)
  store.setItem('rag_refresh', refresh)
  if (user) store.setItem('rag_user', JSON.stringify(user))
  const other = remember ? sessionStorage : localStorage
  TOKEN_KEYS.forEach(k => other.removeItem(k))
}

// token 刷新后回写（写入当前生效的存储，保持与读取一致）
export function saveTokens(access, refresh) {
  const store = targetStore()
  store.setItem('rag_access', access)
  if (refresh) store.setItem('rag_refresh', refresh)
}

// 清空全部登录态（登出/过期，两个存储都清，防残留）
export function clearLoginState() {
  localStorage.removeItem(REMEMBER_FLAG)
  TOKEN_KEYS.forEach(k => {
    localStorage.removeItem(k)
    sessionStorage.removeItem(k)
  })
}
