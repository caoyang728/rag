/* ============ 登录页 ============ */
let currentCaptchaId = '';

async function refreshCaptcha() {
	const img = document.getElementById('captchaImg');
	try {
		const resp = await fetch('/api/v1/security/captcha/');
		const data = await resp.json();
		if (resp.ok && data.image_b64) {
			currentCaptchaId = data.captcha_id;
			img.src = `data:image/png;base64,${data.image_b64}`;
			img.style.display = 'block';
		} else {
			throw new Error(data.detail || '验证码获取失败');
		}
	} catch (e) {
		toast('验证码加载失败，请刷新页面重试', 'error');
		currentCaptchaId = '';
	}
}

document.addEventListener('DOMContentLoaded', () => {
	refreshCaptcha();
});

async function doLogin() {
	const account = $('#loginAccount').value.trim();
	const pwd = $('#loginPwd').value.trim();
	const captchaCode = $('#loginCaptcha').value.trim();

	if (!account || !pwd) { toast('请输入账号和密码', 'warning'); return; }
	if (!captchaCode) { toast('请输入验证码', 'warning'); return; }
	if (!currentCaptchaId) {
		toast('验证码加载失败，请刷新页面', 'error');
		return;
	}

	const btn = document.querySelector('.auth-form button[type="submit"]');
	btn.disabled = true;
	btn.textContent = '登录中...';

	try {
		const resp = await fetch('/api/v1/auth/login/', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				username: account,
				password: pwd,
				captcha_id: currentCaptchaId,
				captcha_code: captchaCode
			})
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
		// 登录失败后验证码已失效，统一刷新并清空输入
		refreshCaptcha();
		$('#loginCaptcha').value = '';
	}
}
