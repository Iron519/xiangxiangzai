// ==================== 通用鉴权（手机号验证码 / 微信 + JWT + RBAC 前端） ====================
//
// 替代原 BMW SSO/OIDC 登录，改为通用登录：手机号+验证码、微信小程序、微信公众号 H5。
// 即插即用模块：登录、令牌、角色权限的整套前端逻辑。
// 依赖宿主页面提供的全局：STORE_PREFIX / Store（localStorage 封装）/ showAlert（弹窗）/ bootstrapApp（应用渲染入口）。
// 必须在主业务 <script> 之前加载——本文件声明的 apiFetch / currentUser / hasRole / canAccess / wechatMpLogin
// 等会被主脚本引用；本文件里的 smsLogin / webLogin 在运行时调用主脚本的 bootstrapApp()（前向引用，调用时已就绪）。
//
// token 存 localStorage；access_token 注入到每个业务请求的 Authorization 头。
// 流程：未登录→弹登录框（手机验证码 / 微信）→换 JWT→拉 current-user→渲染应用。
  const AUTH = {
    KEY_AT: STORE_PREFIX + 'access_token',
    KEY_STATE: STORE_PREFIX + 'auth_state',
  };
  function getAccessToken() { return localStorage.getItem(AUTH.KEY_AT); }
  function setAccessToken(t) { localStorage.setItem(AUTH.KEY_AT, t); }
  function clearAuth() {
    localStorage.removeItem(AUTH.KEY_AT);
    localStorage.removeItem(AUTH.KEY_STATE);
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
    const phoneTab = getEl('authTabPhone');
    const wxTab = getEl('authTabWechat');
    const pwdPanel = getEl('authPanelPassword');
    const phonePanel = getEl('authPanelPhone');
    const wxPanel = getEl('authPanelWechat');
    const wxBtn = getEl('authWechatMp');
    const smsBtn = getEl('authSmsLogin');
    const pwdLoginBtn = getEl('authPwdLogin');
    const switchRegisterBtn = getEl('authSwitchToRegister');
    const doRegisterBtn = getEl('authDoRegister');
    const confirmPwdWrap = getEl('authConfirmPwdWrap');

    // 重置为登录模式
    _authMode = 'login';
    if (confirmPwdWrap) confirmPwdWrap.style.display = 'none';
    if (switchRegisterBtn) switchRegisterBtn.style.display = 'none';
    if (doRegisterBtn) doRegisterBtn.style.display = 'none';

    // 隐藏所有面板
    if (pwdPanel) pwdPanel.style.display = 'none';
    if (phonePanel) phonePanel.style.display = 'none';
    if (wxPanel) wxPanel.style.display = 'none';

    // 隐藏所有操作按钮
    if (wxBtn) wxBtn.style.display = 'none';
    if (smsBtn) smsBtn.style.display = 'none';
    if (pwdLoginBtn) pwdLoginBtn.style.display = 'none';

    // 重置 tab 样式
    const resetTab = (el) => { if (el) { el.style.borderBottom = 'none'; el.style.color = 'var(--c-text-3)'; el.style.fontWeight = '400'; } };
    resetTab(pwdTab); resetTab(phoneTab); resetTab(wxTab);

    const activateTab = (el) => { if (el) { el.style.borderBottom = '2px solid var(--c-brand)'; el.style.color = 'var(--c-text)'; el.style.fontWeight = '600'; } };

    if (tab === 'password') {
      activateTab(pwdTab);
      if (pwdPanel) pwdPanel.style.display = '';
      if (pwdLoginBtn) pwdLoginBtn.style.display = '';
      if (switchRegisterBtn) switchRegisterBtn.style.display = '';
    } else if (tab === 'phone') {
      activateTab(phoneTab);
      if (phonePanel) phonePanel.style.display = '';
      if (smsBtn) smsBtn.style.display = '';
    } else if (tab === 'wechat') {
      activateTab(wxTab);
      if (wxPanel) wxPanel.style.display = '';
      if (wxBtn) wxBtn.style.display = '';
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
      const res = await fetch('/api/v1/auth/password/login', {
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

  // 发送验证码 + 60s 倒计时
  let _codeTimer = null;
  function stopCodeTimer() { if (_codeTimer) { clearInterval(_codeTimer); _codeTimer = null; } }
  function startCodeCountdown(btn) {
    stopCodeTimer();
    let s = 60;
    btn.disabled = true;
    _codeTimer = setInterval(() => {
      s -= 1;
      if (s <= 0) { stopCodeTimer(); btn.disabled = false; btn.textContent = '发送验证码'; }
      else btn.textContent = `${s}s 后重发`;
    }, 1000);
  }
  async function sendSmsCode() {
    const phoneEl = getEl('authPhone');
    const hint = getEl('authHint');
    const phone = (phoneEl && phoneEl.value || '').trim();
    if (!phone) { if (hint) hint.textContent = '请输入手机号'; return; }
    try {
      const res = await fetch('/api/v1/auth/sms/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone }),
      });
      const data = await res.json();
      if (data.code !== 200) {
        if (hint) hint.textContent = data.message || '发送失败';
        return;
      }
      if (hint) hint.textContent = '验证码已发送';
      if (data.data && data.data.debugCode) {
        if (hint) hint.textContent += `（联调验证码：${data.data.debugCode}）`;
      }
      startCodeCountdown(getEl('authSendCode'));
    } catch (e) {
      if (hint) hint.textContent = '发送失败：' + e.message;
    }
  }

  // 手机号 + 验证码登录
  async function smsLogin() {
    const phoneEl = getEl('authPhone');
    const codeEl = getEl('authCode');
    const hint = getEl('authHint');
    const phone = (phoneEl && phoneEl.value || '').trim();
    const code = (codeEl && codeEl.value || '').trim();
    if (!phone || !code) {
      if (hint) hint.textContent = '请输入手机号和验证码';
      return;
    }
    try {
      const res = await fetch('/api/v1/auth/sms/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone, code }),
      });
      const data = await res.json();
      if (data.code !== 200 || !data.data || !data.data.access_token) {
        if (hint) hint.textContent = data.message || '登录失败';
        return;
      }
      stopCodeTimer();
      setAccessToken(data.data.access_token);
      closeLogin();
      await fetchCurrentUser(true);
      bootstrapApp();
    } catch (e) {
      if (hint) hint.textContent = '登录失败：' + e.message;
    }
  }

  // 微信公众号 H5 网页授权：拿授权 URL 后整页跳转
  async function wechatMpLogin() {
    const params = new URLSearchParams({
      origin: location.origin,
      redirectUrl: '/',
      currentPage: location.href,
    });
    try {
      const res = await fetch(`/api/v1/auth/wechat/mp/url?${params}`);
      const data = await res.json();
      const wxUrl = (data && data.code === 200 && data.data && data.data.url)
                  || (data && data.url)
                  || null;
      if (!wxUrl) {
        await showAlert('Login Error', (data && data.message) || '无法获取微信授权地址，请检查后端微信配置');
        return;
      }
      const url = new URL(wxUrl);
      const state = url.searchParams.get('state') || '';
      if (state) localStorage.setItem(AUTH.KEY_STATE, state.split('|')[1] || '/');
      window.location.href = wxUrl;
    } catch (e) {
      await showAlert('Login Error', '获取微信授权地址失败：' + e.message);
    }
  }

  // 微信公众号网页授权回调：用 code 换 JWT，再拉用户身份，最后回跳原页
  async function webLogin(code, state) {
    const redirectUri = location.origin;
    try {
      const res = await fetch('/api/v1/auth/wechat/mp/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, redirectUri }),
      });
      const data = await res.json();
      if (data.code !== 200 || !data.data || !data.data.access_token) {
        await showAlert('Login Failed', (data && data.message) || '微信授权换 token 失败');
        return false;
      }
      setAccessToken(data.data.access_token);
      await fetchCurrentUser(true);
      const cleanUrl = location.origin + location.pathname;
      history.replaceState(null, '', cleanUrl);
      bootstrapApp();
      return true;
    } catch (e) {
      await showAlert('Login Failed', e.message);
      return false;
    }
  }

  // 登出：清本地 token 与身份，弹登录框（JWT 无状态，服务端不需会话）
  async function webLogout() {
    try { await fetch('/api/v1/auth/logout', { method: 'POST' }); } catch (e) { /* ignore */ }
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