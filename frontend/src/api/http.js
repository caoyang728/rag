import { ElMessage } from 'element-plus'
import router from '../router'
import {
  getToken, getRefreshToken, saveTokens, clearLoginState
} from '../utils/authStorage'

/**
 * 统一 API 请求服务（原 api.js 的 Vue 版）
 * 包含：token 管理、自动刷新、请求封装、SSE 流式
 * 错误提示统一由调用方 catch 处理，本模块只在登录过期登出时提示
 */

const BASE_URL = '/api/v1'

// 公开接口白名单：这些接口不需要（也不应携带）Authorization，
// token 失效时也不应触发"刷新/登出"流程（登录、密码重置、验证码、加密公钥、token 刷新）
const PUBLIC_URL_PATTERNS = [
  '/auth/login/',
  '/auth/register/',
  '/auth/token/refresh/',
  '/auth/password-reset/request/',
  '/auth/password-reset/confirm/',
  '/security/captcha/',
  '/security/encrypt-key/'
]

function isPublicUrl(url) {
  return PUBLIC_URL_PATTERNS.some(p => url.includes(p))
}

let isRefreshing = false
let refreshSubscribers = []

async function refreshToken() {
  const refresh = getRefreshToken()
  if (!refresh) throw new Error('No refresh token')
  const response = await fetch(`${BASE_URL}/auth/token/refresh/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh })
  })
  if (!response.ok) throw new Error('Refresh failed')
  const data = await response.json()
  saveTokens(data.access, data.refresh)
  return data.access
}

function enqueueRefresh(callback) {
  return new Promise((resolve, reject) => {
    refreshSubscribers.push({ resolve, reject, callback })
  })
}

// 刷新 token 后统一通知所有等待中的请求；失败则清空登录态跳登录页
async function handleRefresh() {
  try {
    const newToken = await refreshToken()
    refreshSubscribers.forEach(sub => {
      try { sub.resolve(newToken) } catch (e) { sub.reject(e) }
    })
  } catch (e) {
    refreshSubscribers.forEach(sub => sub.reject(e))
    doLogout()
  } finally {
    isRefreshing = false
    refreshSubscribers = []
  }
}

function doLogout() {
  ElMessage.error('登录已过期，请重新登录')
  clearLoginState()
  setTimeout(() => { router.replace('/login') }, 1500)
}

// 解析 DRF 错误响应为可读文案：
// - details 仅含单个 detail 键 → 业务错误，直接返回该信息
// - details 含多字段 → 字段校验错误，拼接 "字段: 错误" 列表
// - 否则取 detail / message
function formatError(data) {
  if (!data) return '请求失败'
  if (data.details && typeof data.details === 'object'
    && 'detail' in data.details && Object.keys(data.details).length === 1) {
    return data.details.detail || data.message || '请求失败'
  }
  if (data.details && typeof data.details === 'object') {
    const msgs = []
    for (const [field, errors] of Object.entries(data.details)) {
      const errList = Array.isArray(errors) ? errors : [errors]
      for (const e of errList) {
        const key = `${field}:${e}`
        const map = {
          'email:具有 email 的 user 已存在。': '该邮箱已被使用',
          'username:具有 username 的 user 已存在。': '该用户名已被使用'
        }
        msgs.push(map[key] || `${field}: ${e}`)
      }
    }
    return msgs.join('；')
  }
  return data.detail || data.message || '请求失败'
}

async function handleError(res) {
  if (!res.ok) {
    let detail = '请求失败'
    let data = null
    try {
      data = await res.json()
      detail = formatError(data)
    } catch {
      if (res.status === 403) detail = '无权限访问此资源'
    }
    // 挂载 status/data 供调用方做条件分支（如 409 恢复用户场景）
    const err = new Error(detail)
    err.status = res.status
    err.data = data
    throw err
  }
  return res
}

async function fetchWithAuth(method, url, options = {}) {
  const token = getToken()
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers
  }
  // 非公开接口统一携带 token；公开接口（登录/验证码等）即使有 token 也不带
  if (token && !isPublicUrl(url)) headers['Authorization'] = `Bearer ${token}`
  return fetch(url, {
    method: method.toUpperCase(),
    headers,
    body: options.body,
    ...options
  })
}

async function request(method, url, options = {}) {
  let response = await fetchWithAuth(method, url, options)
  if (response.status === 401) {
    if (!isRefreshing) {
      isRefreshing = true
      handleRefresh()
    }
    await new Promise((resolve, reject) => {
      refreshSubscribers.push({ resolve: () => resolve(), reject: (err) => reject(err) })
    })
    response = await fetchWithAuth(method, url, options)
    // 刷新 token 后重试仍 401：说明 refresh token 也已失效，直接登出跳登录页
    if (response.status === 401) {
      doLogout()
      throw new Error('登录已过期，请重新登录')
    }
  }
  return handleError(response)
}

function bodyOf(data) {
  return typeof data === 'string' ? data : JSON.stringify(data)
}

const api = {
  get(url, options = {}) { return request('GET', url, options) },

  post(url, data, options = {}) {
    return request('POST', url, { ...options, body: bodyOf(data) })
  },

  put(url, data, options = {}) {
    return request('PUT', url, { ...options, body: bodyOf(data) })
  },

  patch(url, data, options = {}) {
    return request('PATCH', url, { ...options, body: bodyOf(data) })
  },

  delete(url, options = {}) { return request('DELETE', url, options) },

  // 统一 GET 并解析 JSON（兼容 204 空响应与 CSV blob 下载）
  async getJson(url, options = {}) {
    const res = await this.get(url, options)
    const ct = res.headers.get('content-type') || ''
    if (ct.includes('text/csv')) return res.blob()
    if (res.status === 204) return null
    return res.json()
  },

  async postJson(url, data, options = {}) {
    const res = await this.post(url, data, options)
    if (res.status === 204) return null
    return res.json()
  },

  async putJson(url, data, options = {}) {
    const res = await this.put(url, data, options)
    if (res.status === 204) return null
    return res.json()
  },

  async patchJson(url, data, options = {}) {
    const res = await this.patch(url, data, options)
    if (res.status === 204) return null
    return res.json()
  },

  async deleteJson(url, options = {}) {
    const res = await this.delete(url, options)
    if (res.status === 204) return null
    return res.json()
  },

  /**
   * SSE 流式请求（聊天/图谱流式输出）
   * 收到 [DONE] 标记后必须主动结束读取并 cancel reader，避免连接悬挂
   */
  async stream(url, data, onChunk, options = {}) {
    let token = getToken()
    const headers = {
      'Content-Type': 'application/json',
      ...options.headers
    }
    if (token) headers['Authorization'] = `Bearer ${token}`

    let response = await fetch(url, {
      method: 'POST',
      headers,
      body: bodyOf(data),
      ...options
    })

    if (response.status === 401) {
      if (!isRefreshing) {
        isRefreshing = true
        handleRefresh()
      }
      token = await new Promise((resolve, reject) => {
        refreshSubscribers.push({ resolve: (newToken) => resolve(newToken), reject: (err) => reject(err) })
      })
      headers['Authorization'] = `Bearer ${token}`
      response = await fetch(url, {
        method: 'POST',
        headers,
        body: bodyOf(data),
        ...options
      })
      // 刷新后重试仍 401：refresh token 已失效，登出跳登录页
      if (response.status === 401) {
        doLogout()
        throw new Error('登录已过期，请重新登录')
      }
    }

    if (!response.ok) {
      await handleError(response)
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let streamDone = false

    try {
      while (!streamDone) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          if (line.trim().startsWith('data: ')) {
            const jsonStr = line.slice(6)
            if (jsonStr.trim() === '[DONE]') { streamDone = true; break }
            try {
              const chunk = JSON.parse(jsonStr)
              onChunk(chunk)
            } catch (e) {
              console.warn('Failed to parse SSE chunk:', e)
            }
          }
        }
      }
    } finally {
      try { reader.cancel() } catch { /* 忽略 */ }
    }
  }
}

export default api
