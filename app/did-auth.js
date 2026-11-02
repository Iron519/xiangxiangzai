// ==================== MyAgent 鉴权前端（账号密码 + Redis Session，无 RBAC） ====================
//
// 设计：单表用户，注册=提交申请（需申请理由），管理员批准后才能登录。
// 登录后所有页面可见，无角色/权限控制。
// 依赖宿主页面提供的全局：STORE_PREFIX / Store / bootstrapApp。
  const AUTH = {
    KEY_AT: STORE_PREFIX + 'access_token',
  };
  function getAccessToken() { return localStorage.getItem(AUTH.KEY_AT); }
  function setAccessToken(t) { localStorage.setItem(AUTH.KEY_AT, t); }
  function clearAuth() {
    localStorage.removeItem(AUTH.KEY_AT);
    if (typeof Store !== 'undefined') Store.del('current_user');
    currentUser = null;
    renderUserBox();
  }

  // 统一鉴权请求：注入 Bearer token，401 自动弹登录
  let _redirecting = false;
  function handleUnauthorized() {
    if (_redirecting) return;
    _redirecting = true;
    clearAuth();
    openLogin().finally(() => { _redirecting = false; });
  }
  async function apiFetch(url, opts = {}) {
    const token = getAccessToken();
    const headers = { ...(opts.headers || {}) };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(url, { ...opts, headers });
    if (res.status === 401) { handleUnauthorized(); throw new Error('401 Unauthorized'); }
    return res;
  }

  // ==================== 登录弹窗 ====================
  function getEl(id) { return document.getElementById(id); }

  async function openLogin() {
    const m = getEl('authModal');
    if (!m) return;
    m.style.display = 'flex';
    _authMode = 'login';
    switchAuthTab('password');
  }

  function closeLogin() {
    const m = getEl('authModal');
    if (m) m.style.display = 'none';
  }

  let _authMode = 'login';

  function switchAuthTab(tab) {
    const pwdTab = getEl('authTabPassword');
    const pwdPanel = getEl('authPanelPassword');
    const pwdLoginBtn = getEl('authPwdLogin');
    const switchRegisterBtn = getEl('authSwitchToRegister');
    const doRegisterBtn = getEl('authDoRegister');
    const confirmPwdWrap = getEl('authConfirmPwdWrap');
    const reasonWrap = getEl('authReasonWrap');

    _authMode = 'login';
    if (confirmPwdWrap) confirmPwdWrap.style.display = 'none';
    if (reasonWrap) reasonWrap.style.display = 'none';
    if (switchRegisterBtn) switchRegisterBtn.style.display = 'none';
    if (doRegisterBtn) doRegisterBtn.style.display = 'none';

    if (pwdPanel) pwdPanel.style.display = 'none';
    if (pwdLoginBtn) pwdLoginBtn.style.display = 'none';

    const resetTab = (el) => { if (el) { el.style.borderBottom = 'none'; el.style.color = 'var(--c-text-3)'; el.style.fontWeight = '400'; } };
    resetTab(pwdTab);

    const activateTab = (el) => { if (el) { el.style.borderBottom = '2px solid var(--c-brand)'; el.style.color = 'var(--c-text)'; el.style.fontWeight = '600'; } };

    if (tab === 'password') {
      activateTab(pwdTab);
      if (pwdPanel) pwdPanel.style.display = '';
      if (pwdLoginBtn) pwdLoginBtn.style.display = '';
      if (switchRegisterBtn) switchRegisterBtn.style.display = '';
    }
  }

  function switchToRegister() {
    _authMode = 'register';
    const confirmPwdWrap = getEl('authConfirmPwdWrap');
    const reasonWrap = getEl('authReasonWrap');
    const pwdLoginBtn = getEl('authPwdLogin');
    const switchRegisterBtn = getEl('authSwitchToRegister');
    const doRegisterBtn = getEl('authDoRegister');
    if (confirmPwdWrap) confirmPwdWrap.style.display = '';
    if (reasonWrap) reasonWrap.style.display = '';
    if (pwdLoginBtn) pwdLoginBtn.style.display = 'none';
    if (switchRegisterBtn) switchRegisterBtn.style.display = 'none';
    if (doRegisterBtn) doRegisterBtn.style.display = '';
  }

  // 账号密码登录
  async function passwordLogin() {
    const usernameEl = getEl('authUsername');
    const pwdEl = getEl('authPwd');
    const hint = getEl('authPwdHint');
    const username = (usernameEl && usernameEl.value || '').trim();
    const password = (pwdEl && pwdEl.value || '');
    if (!username || !password) {
      if (hint) hint.textContent = '请输入用户名和密码';
      return;
    }
    try {
      const res = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json();
      if (data.code !== 200 || !data.data || !data.data.access_token) {
        if (hint) hint.textContent = data.message || '登录失败';
        return;
      }
      setAccessToken(data.data.access_token);
      closeLogin();
      await fetchCurrentUser(true);
      bootstrapApp();
    } catch (e) {
      if (hint) hint.textContent = '登录失败：' + e.message;
    }
  }

  // 注册（提交申请）
  async function passwordRegister() {
    const usernameEl = getEl('authUsername');
    const pwdEl = getEl('authPwd');
    const confirmEl = getEl('authConfirmPwd');
    const reasonEl = getEl('authApplyReason');
    const hint = getEl('authPwdHint');
    const username = (usernameEl && usernameEl.value || '').trim();
    const password = (pwdEl && pwdEl.value || '');
    const confirmPwd = (confirmEl && confirmEl.value || '');
    const applyReason = (reasonEl && reasonEl.value || '').trim();
    if (!username || !password) {
      if (hint) hint.textContent = '请输入用户名和密码';
      return;
    }
    if (password !== confirmPwd) {
      if (hint) hint.textContent = '两次密码不一致';
      return;
    }
    if (!applyReason) {
      if (hint) hint.textContent = '请填写申请理由';
      return;
    }
    try {
      const res = await fetch('/api/v1/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, applyReason }),
      });
      const data = await res.json();
      if (data.code !== 200) {
        if (hint) hint.textContent = data.message || '注册失败';
        return;
      }
      if (hint) hint.textContent = '申请已提交，请等待管理员批准后登录';
      // 注册成功后切回登录模式
      setTimeout(() => switchAuthTab('password'), 2000);
    } catch (e) {
      if (hint) hint.textContent = '注册失败：' + e.message;
    }
  }

  // 登出
  async function webLogout() {
    try { await apiFetch('/api/v1/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    clearAuth();
    openLogin();
  }

  // ==================== 当前用户 ====================
  let currentUser = null;

  async function fetchCurrentUser(force = false) {
    if (!force) {
      const cached = Store.get('current_user', null);
      if (cached && cached.uid) { currentUser = cached; return cached; }
    }
    const res = await apiFetch('/api/v1/auth/me');
    const data = await res.json();
    if (data.code === 200 && data.data && data.data.uid) {
      currentUser = data.data;
      Store.set('current_user', data.data);
      return data.data;
    }
    throw new Error('Failed to fetch user info');
  }

  function hasRole(r) { return false; }
  function hasPermission(c) { return true; }

  // 所有页面登录后均可见
  function canAccess(pageId) { return true; }

  function renderUserBox() {
    const box = document.getElementById('appUserBox');
    const nameEl = document.getElementById('appUserName');
    const avatarEl = document.getElementById('sidebarUserAvatar');
    if (currentUser && currentUser.uid) {
      nameEl.textContent = currentUser.username || currentUser.uid;
      box.style.display = 'flex';
      if (avatarEl) {
        const initial = (currentUser.username || currentUser.uid || 'U').charAt(0).toUpperCase();
        avatarEl.textContent = initial;
      }
    } else {
      box.style.display = 'none';
    }
  }

  function applyRolePermissions() {
    // 无 RBAC，所有页面可见，无需处理
  }
