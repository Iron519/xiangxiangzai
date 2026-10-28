# 统一身份鉴权 & RBAC 权限管理

## 文件说明

| 文件 | 职责 |
|------|------|
| `auth.py` | JWT 签发/校验、鉴权依赖（`get_current_user` / `require_permission` 等） |
| `auth_service.py` | 验证码发送/校验、用户 upsert、多身份绑定、角色权限聚合 |
| `auth_api.py` | 统一登录接口（账号密码/短信/微信小程序/公众号 H5，prefix: `/api/v1/auth`） |
| `wx_client.py` | 微信客户端：小程序 code2session / 手机号快捷组件、公众号网页授权 |
| `users_api.py` | 用户 CRUD + 当前用户画像（prefix: `/api/v1/users`） |
| `roles_api.py` | 角色 CRUD + 角色-权限分配（prefix: `/api/v1/roles`） |
| `permissions_api.py` | 权限 CRUD（prefix: `/api/v1/permissions`） |
| `rbac_models.py` | Pydantic 请求/响应模型定义 |
| `rbac_service.py` | 数据访问层（psycopg 同步直连 PG），含 UserService / RoleService / PermissionService |
| `create_rbac_tables.py` | 幂等建表脚本，创建 RBAC 相关表 + 种子数据 |
| `create_auth_tables.py` | 幂等建表脚本，创建身份绑定表 + 验证码表 |

## 数据库表

| 表名 | 说明 |
|------|------|
| `sys_user` | 用户表（account_id / display_name / email / dept_code / department / employee_type / mobile） |
| `sys_role` | 角色表（code / name / status） |
| `sys_user_role` | 用户-角色关联表 |
| `sys_permission` | 权限表（code / name / action / type） |
| `sys_role_permission` | 角色-权限关联表 |
| `hrssc_identity_mgmt` | 身份绑定表（provider ∈ mobile / wechat_mini / wechat_mp / password，UNIQUE(provider, identifier)） |
| `hrssc_verify_code_mgmt` | 短信验证码表（60s 发送冷却、5 分钟内有效、最多 5 次校验失败） |
| `hrssc_password_mgmt` | 账号密码表（user_id + bcrypt password_hash，UNIQUE(user_id)） |

## 登录体系

**统一账号模型**：第三方身份（微信 openid/unionid）、手机号与用户名密码都绑定到本地用户。
account_id 是 JWT.sub 主键：手机号登录 = 手机号；微信首登 = `wx_<openid>`；
微信用户绑定手机号后自动转为手机号。账号密码登录 = 用户名。
unis 用小程序的 openid 与公众号 openid 靠同一开放平台 unionid 打通
（`create_auth_tables.py` 的 `wx_union` 身份类型）。password 身份类型用于账号密码登录。

**首次登录自动建号**，默认绑定 `guest` 角色（可在 `.env` `AUTH_DEFAULT_ROLE` 调整）。

## 接口清单

### 统一登录 (`/api/v1/auth`)

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/v1/auth/register` | 无 | 账号密码注册：用户名（3-30 位字母/数字/下划线）+ 密码（至少 6 位），自动建号并签发 JWT |
| POST | `/api/v1/auth/password/login` | 无 | 账号密码登录，校验通过签发 JWT |
| POST | `/api/v1/auth/change-password` | Bearer | 修改密码（需旧密码校验） |
| POST | `/api/v1/auth/sms/send` | 无 | 发送短信验证码。参数 `phone`、`scene`(login/bind)。`SMS_DRIVER=mock` 时响应返回 `debugCode` 便于联调 |
| POST | `/api/v1/auth/sms/login` | 无 | 手机号 + 验证码登录，自动建号并签发 JWT |
| POST | `/api/v1/auth/wechat/mini/login` | 无 | 小程序 `wx.login` code 换 openid 登录；未绑手机时返回 `needBindPhone=true` |
| POST | `/api/v1/auth/wechat/mini/bind-phone` | Bearer | 小程序手机号快捷组件 code 绑定并刷新 JWT（收费接口） |
| GET | `/api/v1/auth/wechat/mp/url` | 无 | 公众号网页授权 URL，参数 `origin`/`redirectUrl`/`currentPage` |
| POST | `/api/v1/auth/wechat/mp/login` | 无 | 公众号授权 code 登录并签发 JWT |
| POST | `/api/v1/auth/bind-phone` | Bearer | 已登录未绑手机，用短信验证码补绑手机号并刷新 JWT |
| POST | `/api/v1/auth/logout` | 无 | 无状态登出（前端清 localStorage） |
| GET | `/api/v1/auth/debug/uid` | 无 | [DEV] 免验证码直签 JWT，仅 `DEBUG_SSO=true` 可用 |

### 用户管理 (`/api/v1/users`)

| 方法 | 路径 | 权限码 | 说明 |
|------|------|--------|------|
| GET | `/api/v1/users/current-user` | 登录即可 | 获取当前登录用户完整画像（uid / username / roles / permissions / email / mobile / deptCode / department / employeeType / status） |
| GET | `/api/v1/users/page` | `system:user:list` | 分页查询用户 |
| GET | `/api/v1/users/{user_id}` | `system:user:list` | 获取用户详情 |
| POST | `/api/v1/users` | `system:user:save` | 创建用户（含角色分配） |
| PUT | `/api/v1/users/{user_id}` | `system:user:save` | 更新用户（含角色重新分配，全量覆盖） |
| DELETE | `/api/v1/users/{user_id}` | `system:user:delete` | 删除用户（软删） |
| GET | `/api/v1/users/{user_id}/roles` | `system:user:list` | 获取用户已分配的角色列表 |

### 角色管理 (`/api/v1/roles`)

| 方法 | 路径 | 权限码 | 说明 |
|------|------|--------|------|
| GET | `/api/v1/roles` | `system:role:list` | 查询所有角色 |
| GET | `/api/v1/roles/page` | `system:role:list` | 分页查询角色 |
| GET | `/api/v1/roles/{role_id}` | `system:role:list` | 获取角色详情（含已分配 permissionIds 回显） |
| POST | `/api/v1/roles` | `system:role:save` | 创建角色 |
| PUT | `/api/v1/roles/{role_id}` | `system:role:save` | 更新角色 |
| DELETE | `/api/v1/roles/{role_id}` | `system:role:delete` | 删除角色（软删；有用户绑定则拒绝） |
| GET | `/api/v1/roles/{role_id}/permissions` | `system:role:list` | 查询角色已分配的权限 id 列表 |
| PUT | `/api/v1/roles/{role_id}/permissions` | `system:role:save` | 全量覆盖角色-权限关联 |

### 权限管理 (`/api/v1/permissions`)

| 方法 | 路径 | 权限码 | 说明 |
|------|------|--------|------|
| GET | `/api/v1/permissions` | `system:permission:list` | 查询所有权限（供角色编辑勾选） |
| GET | `/api/v1/permissions/page` | `system:permission:list` | 分页查询权限 |
| GET | `/api/v1/permissions/{permission_id}` | `system:permission:list` | 获取权限详情 |
| POST | `/api/v1/permissions` | `system:permission:save` | 创建权限 |
| PUT | `/api/v1/permissions/{permission_id}` | `system:permission:save` | 更新权限 |
| DELETE | `/api/v1/permissions/{permission_id}` | `system:permission:delete` | 删除权限（软删 + 清理角色-权限关联） |

## 鉴权机制

### JWT 结构

```json
{
  "sub": "uid",           // account_id（手机号或 wx_<openid>）
  "username": "张三",      // 显示名
  "roles": ["guest"],     // 角色 code 列表
  "permissions": ["kb:read", ...],  // 权限码列表
  "exp": 1234567890,      // 过期时间
  "iat": 1234567890       // 签发时间
}
```

### 鉴权依赖（`sso_did.auth` 导出）

| 依赖函数 | 用途 |
|----------|------|
| `get_current_user` | 已登录即可，返回 `TokenData`，未登录 401 |
| `get_current_user_optional` | 已登录返回用户，未登录返回 None（不抛错） |
| `require_permission(code)` | 要求具备指定权限码，否则 403（推荐使用） |
| `require_any_permission(codes)` | 要求具备任一权限码，否则 403 |
| `require_role(role_code)` | 要求具备指定角色，否则 403（粗粒度） |
| `require_any_role(role_codes)` | 要求具备任一角色，否则 403 |

### 初始种子数据

**角色**：admin（全量权限）、operator（读 + 上传）、guest（无权限，默认新用户）

**权限码**：

| 权限码 | 名称 | 类型 |
|--------|------|------|
| `system:user:list` | 用户管理-查询 | menu |
| `system:user:save` | 用户管理-保存 | menu |
| `system:user:delete` | 用户管理-删除 | menu |
| `system:role:list` | 角色管理-查询 | menu |
| `system:role:save` | 角色管理-保存 | menu |
| `system:role:delete` | 角色管理-删除 | menu |
| `system:permission:list` | 权限管理-查询 | menu |
| `system:permission:save` | 权限管理-保存 | menu |
| `system:permission:delete` | 权限管理-删除 | menu |
| `kb:read` | 知识库-查看 | api |
| `kb:upload` | 知识库-上传 | api |
| `kb:delete` | 知识库-删除 | api |
| `stats:read` | 统计-查看 | api |
| `faq:read` | FAQ-查看 | api |
| `faq:save` | FAQ-保存 | api |
| `faq:delete` | FAQ-删除 | api |

## 环境变量

| 变量 | 说明 |
|------|------|
| `JWT_ACCESS_SECRET` | JWT 签名密钥 |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | JWT 过期时间（分钟） |
| `SMS_DRIVER` | `mock`（验证码随响应 `debugCode` 返回）/ `syslog` / 真实渠道 |
| `WECHAT_MINI_APPID` / `WECHAT_MINI_SECRET` | 小程序凭据（缺省时小程序接口返回 500） |
| `WECHAT_MP_APPID` / `WECHAT_MP_SECRET` | 公众号 H5 凭据（缺省时 H5 接口返回 500） |
| `AUTH_DEFAULT_ROLE` | 首次登录自动绑定的角色 code（默认 `guest`） |
| `DEBUG_SSO` | `true` 时启用 `/debug/uid` 免验证码登录（仅联调） |
| `PG_HOST` / `PG_PORT` / `PG_DATABASE` / `PG_USER` / `PG_PASSWORD` | PostgreSQL 连接 |
| `PG_HRSSC_USER_TABLE` | 用户表名（默认 `sys_user`） |
| `PG_HRSSC_ROLE_TABLE` | 角色表名（默认 `sys_role`） |
| `PG_HRSSC_USER_ROLE_TABLE` | 用户-角色关联表名（默认 `sys_user_role`） |
| `PG_HRSSC_PERMISSION_TABLE` | 权限表名（默认 `sys_permission`） |
| `PG_HRSSC_ROLE_PERMISSION_TABLE` | 角色-权限关联表名（默认 `sys_role_permission`） |
| `PG_HRSSC_IDENTITY_TABLE` | 身份绑定表名（默认 `hrssc_identity_mgmt`） |
| `PG_HRSSC_VERIFY_CODE_TABLE` | 验证码表名（默认 `hrssc_verify_code_mgmt`） |
| `PG_HRSSC_PASSWORD_TABLE` | 账号密码表名（默认 `hrssc_password_mgmt`） |

## 依赖

```
pip install bcrypt pyjwt psycopg python-dotenv fastapi httpx
```

## 初始化

```bash
# RBAC 表 + 种子数据（表已存在则跳过，不破坏已有数据）
python create_rbac_tables.py
# 身份绑定表 + 验证码表
python create_auth_tables.py
```

## 登录流程

### 账号密码

```
注册：
1. 前端 POST /api/v1/auth/register {username, password}
2. 后端校验用户名格式（3-30 位字母/数字/下划线）+ 密码强度（≥6 位）
3. 创建用户 → 绑定 password 身份 → 写入 bcrypt 密码哈希 → 绑定默认角色 → 签发 JWT
4. 前端存 JWT，后续请求带 Authorization: Bearer <token>

登录：
1. 前端 POST /api/v1/auth/password/login {username, password}
2. 后端按 password 身份定位用户 → bcrypt 校验密码 → 签发 JWT

修改密码：
1. 前端 POST /api/v1/auth/change-password {oldPassword, newPassword}（需 Bearer token）
2. 后端校验旧密码 → 更新 bcrypt 哈希
```

### 手机验证码

```
1. 前端 POST /api/v1/auth/sms/send {phone, scene:"login"}
2. 后端生成验证码落库并下发（mock 模式：响应返回 debugCode）
3. 前端 POST /api/v1/auth/sms/login {phone, code}
4. 后端校验 → 建号/取号 → 查角色+权限 → 签发 JWT
5. 前端存 JWT 到 localStorage，请求 Header 带 Authorization: Bearer <token>
```

### 微信小程序

```
1. wx.login 取 code → POST /api/v1/auth/wechat/mini/login {code}
2. 后端 code2session 拿 openid/unionid → 建号/取号 → 签发 JWT
3. 若 needBindPhone=true：小程序调 wx.getPhoneNumber 组件拿 phoneCode
   → POST /api/v1/auth/wechat/mini/bind-phone {phoneCode}
   （小程序需 open-type="getPhoneNumber"，该接口为收费接口）
```

### 公众号 H5

```
1. 前端 GET /api/v1/auth/wechat/mp/url?origin=...&redirectUrl=/&currentPage=...
2. 后端返回网页授权 URL（state = "currentPage|redirectUrl"）
3. 前端整页跳转，用户在微信内授权后回调前端页面携 code
4. 前端 POST /api/v1/auth/wechat/mp/login {code, redirectUri}
5. 后端换 openid/unionid → 建号/取号 → 签发 JWT
6. 未绑手机可走 /api/v1/auth/bind-phone {phone, code}（短信验证码补绑）
```