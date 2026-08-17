/**
 * 登录密码加密：一次性会话密钥 + JSEncrypt（RSA PKCS#1 v1.5）
 *
 * 后端 /api/v1/security/encrypt-key/ 每次调用生成新密钥对：
 * 返回 { key_id, public_key, expires_in }。前端用本次公钥加密密码，
 * 登录时携带 key_id；后端解密后立即销毁私钥（一次性，防重放）。
 *
 * 注意：公钥只能用于本次登录会话，登录失败后需重新拉取（旧密钥已作废/超时）。
 * 获取公钥失败时降级明文提交（后端兼容）。
 */
import JSEncrypt from 'jsencrypt'

let currentKeyId = ''
let currentPublicKey = ''

// 拉取一次性登录加密密钥（key_id + 公钥），失败返回 false（走明文降级）
export async function fetchLoginPublicKey() {
  try {
    const resp = await fetch('/api/v1/security/encrypt-key/')
    const data = await resp.json()
    if (resp.ok && data.public_key && data.key_id) {
      currentKeyId = data.key_id
      currentPublicKey = data.public_key
      return true
    }
  } catch {
    /* 网络异常等，走明文降级 */
  }
  currentKeyId = ''
  currentPublicKey = ''
  return false
}

// 当前一次性密钥的 key_id（与密文一起提交登录）
export function getEncryptKeyId() {
  return currentKeyId
}

// 用当前一次性公钥加密密码；无可用公钥时返回空串（调用方按明文处理）
export function encryptPassword(plain) {
  if (!currentPublicKey) return ''
  const encryptor = new JSEncrypt()
  encryptor.setPublicKey(currentPublicKey)
  return encryptor.encrypt(plain) || ''
}
