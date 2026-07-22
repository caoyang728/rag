/* ============ 登录页 ============ */
function generateCaptcha() {
  return Math.random().toString(36).slice(2,6).toUpperCase();
}

document.addEventListener('DOMContentLoaded', () => {
  const captchaImg = document.querySelector('.auth-captcha-img');
  if (captchaImg) {
    captchaImg.textContent = generateCaptcha();
  }
});

async function doLogin() {
  const account = $('#loginAccount').value.trim();
  const pwd = $('#loginPwd').value.trim();
  if (!account || !pwd) { toast('请输入账号和密码', 'warning'); return; }

  const btn = document.querySelector('.auth-form button[type="submit"]');
  btn.disabled = true;
  btn.textContent = '登录中...';

  try {
    const resp = await fetch('/api/v1/auth/login/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: account, password: pwd })
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || '登录失败');
    }
    // 存储 JWT token
    localStorage.setItem('rag_access', data.access);
    localStorage.setItem('rag_refresh', data.refresh);
    localStorage.setItem('rag_user', JSON.stringify(data.user));
    // 更新 common.js 中的 STATE
    if (data.user) {
      STATE.user.name = data.user.real_name || data.user.username;
      STATE.user.avatar = (data.user.real_name || data.user.username).charAt(0);
      STATE.user.email = data.user.email || '';
    }
    toast('登录成功，欢迎回来', 'success');
    setTimeout(() => { window.location.href = '/chat/'; }, 500);
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
    btn.textContent = '登 录';
  }
}
