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
  const tplMap = {1:'tmpl-rp-step1', 2:'tmpl-rp-step2', 3:'tmpl-rp-step3', 4:'tmpl-rp-step4'};
  box.innerHTML = tpl(tplMap[resetStep]).innerHTML;
  if (resetStep === 4) {
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

