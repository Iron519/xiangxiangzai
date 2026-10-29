// ==================== MyAgent 鉴权前端（账号密码 + Redis Session + RBAC） ====================
//
// 即插即用模块：登录、注册、令牌、角色权限的整套前端逻辑。
// 依赖宿主页面提供的全局：STORE_PREFIX / Store（localStorage 封装）/ showAlert（弹窗）/ bootstrapApp（应用渲染入口）。
// 必须在主业务 <script> 之前加载——本文件声明的 apiFetch / currentUser / hasRole / canAccess
// 等会被主脚本引用；本文件里的 passwordLogin 在运行时调用主脚本的 bootstrapApp()（前向引用，调用时已就绪）。
//
// token 存 localStorage；access_token 注入到每个业务请求的 Authorization 头。
// 流程：未登录→弹登录框（账号密码 / 注册）→换 session token→拉 current-user→渲染应用。
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
    applyRolePermissions();
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

  let _authMode = 'login'; // 'login' | 'register'

  function switchAuthTab(tab) {
    const pwdTab = getEl('authTabPassword');
    const pwdPanel = getEl('authPanelPassword');
    const pwdLoginBtn = getEl('authPwdLogin');
    const switchRegisterBtn = getEl('authSwitchToRegister');
    const doRegisterBtn = getEl('authDoRegister');
    const confirmPwdWrap = getEl('authConfirmPwdWrap');

    // 重置为登录模式
    _authMode = 'login';
    if (confirmPwdWrap) confirmPwdWrap.style.display = 'none';
    if (switchRegisterBtn) switchRegisterBtn.style.display = 'none';
    if (doRegisterBtn) doRegisterBtn.style.display = 'none';

    // 隐藏面板
    if (pwdPanel) pwdPanel.style.display = 'none';
    if (pwdLoginBtn) pwdLoginBtn.style.display = 'none';

    // 重置 tab 样式
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
    const pwdLoginBtn = getEl('authPwdLogin');
    const switchRegisterBtn = getEl('authSwitchToRegister');
    const doRegisterBtn = getEl('authDoRegister');
    if (confirmPwdWrap) confirmPwdWrap.style.display = '';
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

  // 账号密码注册
  async function passwordRegister() {
    const usernameEl = getEl('authUsername');
    const pwdEl = getEl('authPwd');
    const confirmEl = getEl('authConfirmPwd');
    const hint = getEl('authPwdHint');
    const username = (usernameEl && usernameEl.value || '').trim();
    const password = (pwdEl && pwdEl.value || '');
    const confirmPwd = (confirmEl && confirmEl.value || '');
    if (!username || !password) {
      if (hint) hint.textContent = '请输入用户名和密码';
      return;
    }
    if (password !== confirmPwd) {
      if (hint) hint.textContent = '两次密码不一致';
      return;
    }
    try {
      const res = await fetch('/api/v1/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json();
      if (data.code !== 200 || !data.data || !data.data.access_token) {
        if (hint) hint.textContent = data.message || '注册失败';
        return;
      }
      setAccessToken(data.data.access_token);
      closeLogin();
      await fetchCurrentUser(true);
      bootstrapApp();
    } catch (e) {
      if (hint) hint.textContent = '注册失败：' + e.message;
    }
  }

  // 登出：通知服务端删 Redis session，清本地 token 与身份，弹登录框
  async function webLogout() {
    try { await apiFetch('/api/v1/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
    clearAuth();
    openLogin();
  }

  // ==================== 当前用户身份 + 角色权限 ====================
  let currentUser = null; // {uid, username, roles:[]}

  async function fetchCurrentUser(force = false) {
    if (!force) {
      const cached = Store.get('current_user', null);
      if (cached && cached.uid) { currentUser = cached; return cached; }
    }
    const res = await apiFetch('/api/v1/users/current-user');
    const data = await res.json();
    if (data.code === 200 && data.data && data.data.uid) {
      currentUser = data.data;
      Store.set('current_user', data.data);
      return data.data;
    }
    throw new Error('Failed to fetch user info');
  }

  function hasRole(r) { return !!(currentUser && currentUser.roles && currentUser.roles.includes(r)); }
  function hasPermission(c) { return !!(currentUser && currentUser.permissions && currentUser.permissions.includes(c)); }

  // 页面级权限：权限码驱动，admin 在权限管理页给角色勾选权限码即可自定义可见范围。
  // home/chat 任何人可访问；stats 需 stats:read；knowledge/voice 需 kb:read；users 需 system:user:list；faq/scene 需 faq:read
  function canAccess(pageId) {
    if (pageId === 'home' || pageId === 'chat') return true;
    if (pageId === 'stats') return hasPermission('stats:read');
    if (pageId === 'knowledge' || pageId === 'voice') return hasPermission('kb:read');
    if (pageId === 'users') return hasPermission('system:user:list');
    if (pageId === 'faq' || pageId === 'scene') return hasPermission('faq:read');
    return false;
  }

  function renderUserBox() {
    const box = document.getElementById('appUserBox');
    const nameEl = document.getElementById('appUserName');
    if (currentUser && currentUser.uid) {
      nameEl.textContent = currentUser.username || currentUser.uid;
      box.style.display = 'flex';
    } else {
      box.style.display = 'none';
    }
  }

  function applyRolePermissions() {
    // 顶级 Management 导航：无任何子页权限时隐藏
    const navShared = document.getElementById('navSharedService');
    if (navShared) {
      const canMgmt = canAccess('stats') || canAccess('knowledge') || canAccess('voice') || canAccess('users') || canAccess('faq') || canAccess('scene');
      navShared.style.display = canMgmt ? '' : 'none';
    }
  }
