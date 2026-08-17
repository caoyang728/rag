<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-logo"><el-icon :size="28"><MagicStick /></el-icon></div>
      <h1 class="auth-title">知库 Agent</h1>
      <p class="auth-desc">企业私有化多场景智能 RAG 知识库平台</p>

      <el-form :model="form" size="large" @submit.prevent="doLogin">
        <el-form-item>
          <el-input v-model="form.account" placeholder="请输入邮箱或工号" :prefix-icon="User" clearable />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="请输入密码" :prefix-icon="Lock" show-password />
        </el-form-item>
        <el-form-item>
          <div class="captcha-row">
            <el-input v-model="form.captchaCode" placeholder="请输入 4 位验证码" maxlength="4" :prefix-icon="Key" />
            <img
              class="captcha-img"
              :src="captchaSrc"
              title="点击刷新"
              alt="验证码"
              @click="refreshCaptcha"
            />
          </div>
        </el-form-item>
        <div class="auth-extra">
          <el-checkbox v-model="form.remember">记住我（7 天）</el-checkbox>
          <el-link type="primary" :underline="'never'" @click="router.push('/reset-password')">忘记密码？</el-link>
        </div>
        <el-button
          type="primary"
          size="large"
          class="login-btn"
          :loading="loading"
          native-type="submit"
        >登 录</el-button>
      </el-form>

      <el-divider content-position="center">或使用企业身份</el-divider>
      <div class="auth-sso">
        <el-button disabled>🏢 企业 LDAP</el-button>
        <el-button disabled>🔐 SSO 单点</el-button>
      </div>

      <div class="auth-footer">
        <el-tag size="small" type="info" effect="plain">🔒 企业内网私有化部署</el-tag>
        <div class="copy">© 2026 知库 Agent · 内部使用 · v1.0.0</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Key, Lock, MagicStick, User } from '@element-plus/icons-vue'
import { useUserStore } from '../stores/user'
import { saveLoginState } from '../utils/authStorage'
import { fetchLoginPublicKey, getEncryptKeyId, encryptPassword } from '../utils/loginCrypto'
import { errMsg } from '../utils/format'

const router = useRouter()
const userStore = useUserStore()

// remember 默认勾选：与旧版"始终记住 7 天"行为一致，取消勾选则为会话级登录
const form = reactive({ account: '', password: '', captchaCode: '', remember: true })
const captchaSrc = ref('')
const captchaId = ref('')
const loading = ref(false)

// 拉取图形验证码（Base64 图片 + captcha_id，提交登录时一并带上）
async function refreshCaptcha() {
  try {
    const resp = await fetch('/api/v1/security/captcha/')
    const data = await resp.json()
    if (resp.ok && data.image_b64) {
      captchaId.value = data.captcha_id
      captchaSrc.value = `data:image/png;base64,${data.image_b64}`
    } else {
      throw new Error(data.detail || '验证码获取失败')
    }
  } catch (e) {
    ElMessage.error('验证码加载失败，请刷新页面重试')
    captchaId.value = ''
  }
}

async function doLogin() {
  if (!form.account || !form.password) {
    ElMessage.warning('请输入账号和密码')
    return
  }
  if (!form.captchaCode) {
    ElMessage.warning('请输入验证码')
    return
  }
  if (!captchaId.value) {
    ElMessage.error('验证码加载失败，请刷新页面')
    return
  }
  loading.value = true
  try {
    // 密码加密传输：用一次性公钥加密（密钥失效后后端无法解密，失败即重拉新密钥）
    const encPwd = encryptPassword(form.password)
    const resp = await fetch('/api/v1/auth/login/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: form.account,
        password: encPwd || form.password,
        encrypted_password: !!encPwd,
        key_id: getEncryptKeyId(),
        captcha_id: captchaId.value,
        captcha_code: form.captchaCode,
        remember_me: form.remember
      })
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || '登录失败')
    // 按"记住我"选择存储：记住→localStorage（7 天），不记住→sessionStorage（关闭浏览器即登出）
    saveLoginState({ access: data.access, refresh: data.refresh, user: data.user, remember: form.remember })
    if (data.user) userStore.setUser(data.user)
    ElMessage.success('登录成功，欢迎回来')
    router.replace('/chat')
  } catch (e) {
    ElMessage.error(errMsg(e, '登录失败'))
    // 登录失败后验证码与一次性加密密钥均已失效，统一刷新（验证码重拉、密钥重签）
    refreshCaptcha()
    fetchLoginPublicKey()
    form.captchaCode = ''
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refreshCaptcha()
  // 预取登录加密公钥：与验证码并行加载，失败则登录时走明文降级
  fetchLoginPublicKey()
})
</script>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #0f2027 0%, #203a43 50%, #2c5364 100%);
  padding: 20px;
  position: relative;
  overflow: hidden;
}

/* 背景装饰光斑：增加层次感（不阻挡交互） */
.auth-page::before {
  content: '';
  position: absolute;
  top: -20%;
  left: -10%;
  width: 60%;
  height: 60%;
  background: radial-gradient(circle, rgba(58, 123, 213, 0.35) 0%, transparent 70%);
  pointer-events: none;
}

.auth-page::after {
  content: '';
  position: absolute;
  bottom: -25%;
  right: -10%;
  width: 55%;
  height: 55%;
  background: radial-gradient(circle, rgba(99, 102, 241, 0.3) 0%, transparent 70%);
  pointer-events: none;
}

.auth-card {
  width: 420px;
  max-width: 100%;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 16px;
  padding: 40px 36px 28px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35);
  position: relative;
  z-index: 1;
}

.auth-logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 14px;
  border-radius: 14px;
  background: linear-gradient(135deg, #3b82f6 0%, #6366f1 100%);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 8px 20px rgba(59, 130, 246, 0.35);
}

.auth-title {
  text-align: center;
  font-size: 24px;
  margin: 0 0 4px;
  color: var(--app-text);
  font-weight: 700;
  letter-spacing: 0.5px;
}

.auth-desc {
  text-align: center;
  font-size: 13px;
  color: var(--app-text-sub);
  margin: 0 0 26px;
}

.captcha-row {
  display: flex;
  gap: 12px;
  width: 100%;
}

.captcha-row .el-input {
  flex: 1;
}

.captcha-img {
  width: 120px;
  height: 40px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid var(--app-border);
  flex-shrink: 0;
}

.auth-extra {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

.login-btn {
  width: 100%;
  font-weight: 600;
  letter-spacing: 2px;
}

.auth-sso {
  display: flex;
  gap: 12px;
  margin-bottom: 8px;
}

.auth-sso .el-button {
  flex: 1;
}

.auth-footer {
  text-align: center;
  margin-top: 20px;
}

.copy {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
</style>
