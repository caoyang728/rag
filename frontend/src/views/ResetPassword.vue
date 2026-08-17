<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-logo"><el-icon :size="28"><MagicStick /></el-icon></div>
      <h1 class="auth-title">重置密码</h1>

      <!-- 步骤 1：输入邮箱 + 图形验证码，发送验证码 -->
      <el-form v-if="step === 1" size="large" @submit.prevent="doSendCode">
        <p class="auth-desc">请输入注册邮箱，我们将向您发送验证码</p>
        <el-form-item>
          <el-input v-model="email" type="email" placeholder="请输入注册邮箱" :prefix-icon="Message" clearable />
        </el-form-item>
        <el-form-item>
          <div class="captcha-row">
            <el-input v-model="captcha" placeholder="请输入 4 位验证码" maxlength="4" :prefix-icon="Key" />
            <img class="captcha-img" :src="captchaSrc" title="点击刷新" alt="验证码" @click="refreshCaptcha" />
          </div>
        </el-form-item>
        <el-button type="primary" size="large" class="block-btn" :loading="sending" native-type="submit">
          发送验证码
        </el-button>
        <div class="auth-back">
          <el-link type="primary" :underline="false" @click="router.push('/login')">← 返回登录</el-link>
        </div>
      </el-form>

      <!-- 步骤 2：验证码 + 新密码 + 确认密码 -->
      <el-form v-else size="large" @submit.prevent="doResetConfirm">
        <p class="auth-desc">验证码已发送至您的邮箱（5 分钟内有效），请输入验证码和新密码</p>
        <el-form-item>
          <el-input v-model="code" placeholder="请输入 6 位验证码" maxlength="6" :prefix-icon="Key" clearable />
        </el-form-item>
        <el-form-item>
          <el-input v-model="newPwd" type="password" placeholder="至少 8 位，含大小写字母和数字" :prefix-icon="Lock" show-password />
        </el-form-item>
        <el-form-item>
          <el-input v-model="confirmPwd" type="password" placeholder="请再次输入新密码" :prefix-icon="Lock" show-password />
        </el-form-item>
        <el-button type="primary" size="large" class="block-btn" :loading="resetting" native-type="submit">
          重置密码
        </el-button>
        <div class="auth-back">
          <el-link type="primary" :underline="false" :disabled="countdown > 0" @click="backToStep1">
            {{ countdown > 0 ? `重新发送 (${countdown}s)` : '← 重新发送验证码' }}
          </el-link>
        </div>
      </el-form>

      <div class="auth-footer">
        <el-tag size="small" type="info" effect="plain">🔒 企业内网私有化部署</el-tag>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Key, Lock, MagicStick, Message } from '@element-plus/icons-vue'

const router = useRouter()

const step = ref(1)
const email = ref('')
const captcha = ref('')
const captchaId = ref('')
const captchaSrc = ref('')
const code = ref('')
const newPwd = ref('')
const confirmPwd = ref('')
const sending = ref(false)
const resetting = ref(false)
// 重新发送 60 秒倒计时
const countdown = ref(0)
let countdownTimer = null

async function refreshCaptcha() {
  try {
    const resp = await fetch('/api/v1/security/captcha/')
    const data = await resp.json()
    if (resp.ok && data.image_b64) {
      captchaId.value = data.captcha_id
      captchaSrc.value = `data:image/png;base64,${data.image_b64}`
    }
  } catch {
    ElMessage.error('验证码加载失败')
  }
}

// 启动重新发送倒计时（期间禁用重发入口）
function startResendCountdown() {
  countdown.value = 60
  clearInterval(countdownTimer)
  countdownTimer = setInterval(() => {
    countdown.value--
    if (countdown.value <= 0) clearInterval(countdownTimer)
  }, 1000)
}

// 步骤 1：发送验证码到邮箱
async function doSendCode() {
  if (!email.value.trim()) { ElMessage.warning('请输入邮箱'); return }
  if (!captcha.value.trim()) { ElMessage.warning('请输入图形验证码'); return }
  sending.value = true
  try {
    const resp = await fetch('/api/v1/auth/password-reset/request/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.value.trim(), captcha_id: captchaId.value, captcha_code: captcha.value.trim() })
    })
    const data = await resp.json()
    if (!resp.ok) {
      ElMessage.error(data.detail || '发送失败')
      refreshCaptcha()  // 验证码错误后刷新图形验证码
      return
    }
    ElMessage.success(data.message || '验证码已发送')
    step.value = 2
    startResendCountdown()
  } catch {
    ElMessage.error('网络错误，请稍后重试')
  } finally {
    sending.value = false
  }
}

// 返回步骤 1（并清空已填内容）
function backToStep1() {
  clearInterval(countdownTimer)
  countdown.value = 0
  step.value = 1
  code.value = ''
  newPwd.value = ''
  confirmPwd.value = ''
  refreshCaptcha()
}

// 步骤 2：验证码 + 新密码 → 重置密码
async function doResetConfirm() {
  if (!code.value.trim()) { ElMessage.warning('请输入验证码'); return }
  if (!newPwd.value || !confirmPwd.value) { ElMessage.warning('请输入新密码'); return }
  if (newPwd.value !== confirmPwd.value) { ElMessage.error('两次输入的密码不一致'); return }
  resetting.value = true
  try {
    const resp = await fetch('/api/v1/auth/password-reset/confirm/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.value.trim(), code: code.value.trim(), new_password: newPwd.value })
    })
    const data = await resp.json()
    if (!resp.ok) {
      ElMessage.error(data.detail || '重置失败')
      return
    }
    ElMessage.success(data.message || '密码已重置')
    setTimeout(() => router.replace('/login'), 2000)
  } catch {
    ElMessage.error('网络错误，请稍后重试')
  } finally {
    resetting.value = false
  }
}

onMounted(refreshCaptcha)
onBeforeUnmount(() => clearInterval(countdownTimer))
</script>

<style scoped>
.auth-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1f2d3d 0%, #3a7bd5 100%);
  padding: 20px;
}

.auth-card {
  width: 420px;
  max-width: 100%;
  background: var(--app-card-bg);
  border-radius: 12px;
  padding: 40px 36px 28px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25);
}

.auth-logo {
  display: flex;
  justify-content: center;
  color: #409eff;
  margin-bottom: 12px;
}

.auth-title {
  text-align: center;
  font-size: 24px;
  margin: 0 0 16px;
  color: var(--app-text);
}

.auth-desc {
  text-align: center;
  font-size: 13px;
  color: var(--app-text-sub);
  margin: 0 0 20px;
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

.block-btn {
  width: 100%;
}

.auth-back {
  margin-top: 14px;
  text-align: center;
}

.auth-footer {
  text-align: center;
  margin-top: 24px;
}
</style>
