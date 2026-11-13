/* Progressive enhancement: the native POST form and demo details work without JS. */
'use strict';
(() => {
    const password = document.getElementById('password');
    const toggle = document.querySelector('.login-password-toggle');
    if (!password || !toggle) return;
    toggle.hidden = false;
    toggle.addEventListener('click', () => {
        const show = password.type === 'password';
        password.type = show ? 'text' : 'password';
        toggle.setAttribute('aria-pressed', String(show));
        toggle.textContent = show ? '隐藏密码' : '显示密码';
    });
    // Hide any revealed password when returning via the browser history cache.
    window.addEventListener('pageshow', () => {
        password.type = 'password';
        toggle.setAttribute('aria-pressed', 'false');
        toggle.textContent = '显示密码';
    });
})();
