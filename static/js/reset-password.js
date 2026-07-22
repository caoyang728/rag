/* ============ 重置密码页 ============ */
let resetStep = 1;

document.addEventListener('DOMContentLoaded', () => {
  resetStep = 1;
  renderResetStep();
});

function renderResetStep() {
  const $steps = $$('#resetSteps .step');
  $steps.forEach((s, i) => {
    s.classList.remove('active', 'done');
    if (i + 1 < resetStep) s.classList.add('done');
    else if (i + 1 === resetStep) s.classList.add('active');
  });
  const box = $('#resetForm');
  if (!box) return;
  if (resetStep === 1) {
    box.innerHTML = `
      <div class="form-item">
        <label class="form-label">企业邮箱</label>
        <input class="input input-lg" id="resetEmail" type="email" placeholder="name@company.com" value="zhangwei@company.com">
        <div class="form-hint">系统将向该邮箱发送 6 位验证码，10 分钟内有效</div>
      </div>
      <button class="btn btn-primary btn-lg btn-block" onclick="resetNext(2)">发送验证码</button>`;
  } else if (resetStep === 2) {
    box.innerHTML = `
      <div class="form-item">
        <label class="form-label">邮箱验证码</label>
        <input class="input input-lg" id="resetCode" type="text" placeholder="请输入 6 位验证码" maxlength="6" value="284915">
        <div class="form-hint">验证码已发送至 <b>zhangwei@company.com</b>，<a onclick="toast('验证码已重发','success')" style="cursor:pointer">重新发送</a></div>
      </div>
      <button class="btn btn-primary btn-lg btn-block" onclick="resetNext(3)">下一步</button>`;
  } else if (resetStep === 3) {
    box.innerHTML = `
      <div class="form-item">
        <label class="form-label">新密码</label>
        <input class="input input-lg" id="resetPwd" type="password" placeholder="至少 8 位，包含字母和数字" oninput="updatePwdStrength(this.value)">
        <div class="password-strength" id="pwdStrength"><div class="bar"></div><div class="bar"></div><div class="bar"></div></div>
        <div class="password-hint" id="pwdHint">密码强度：待输入</div>
      </div>
      <div class="form-item">
        <label class="form-label">确认新密码</label>
        <input class="input input-lg" type="password" placeholder="再次输入新密码">
      </div>
      <button class="btn btn-primary btn-lg btn-block" onclick="doResetDone()">确认修改</button>`;
  } else {
    box.innerHTML = `
      <div style="text-align:center;padding:20px 0">
        <div style="font-size:48px;margin-bottom:12px">✅</div>
        <div style="font-size:16px;font-weight:500;margin-bottom:6px">密码修改成功</div>
        <div style="color:var(--text-sub);margin-bottom:20px">正在返回登录页...</div>
      </div>`;
    setTimeout(() => { window.location.href = '/login/'; }, 1600);
  }
}

function resetNext(step) {
  if (step === 2 && !$('#resetEmail').value.trim()) { toast('请输入邮箱', 'error'); return; }
  if (step === 3 && $('#resetCode').value.length !== 6) { toast('请输入 6 位验证码', 'error'); return; }
  resetStep = step;
  renderResetStep();
  if (step === 2) toast('验证码已发送至邮箱', 'success');
}

function doResetDone() {
  const pwd = $('#resetPwd').value;
  if (pwd.length < 8) { toast('密码至少 8 位', 'error'); return; }
  resetStep = 4;
  renderResetStep();
}

