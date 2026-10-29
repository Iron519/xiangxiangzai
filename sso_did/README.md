# sso_did 单元介绍

这个目录负责 MyAgent 的**登录认证**和**权限控制**

## 整体架构

```
用户请求
  │
  ├──► auth_api.py        路由层：接收 HTTP 请求（注册/登录/登出/改密码）
  │       │
  │       ▼
  │     auth_service.py   业务层：校验账号密码、操作数据库
  │       │
  │       ▼
  │     auth.py           核心：签发/校验 Redis token，提供鉴权依赖
  │       │
  │       ▼
  │     Redis             存储：token → 用户信息的映射，TTL 自动过期
  │
  ├──► users_api.py       路由层：用户管理（增删改查）
  ├──► roles_api.py       路由层：角色管理（增删改查）
  ├──► permissions_api.py 路由层：权限管理（增删改查）
  │       │
  │       ▼
  │     rbac_service.py   业务层：用户/角色/权限的数据库操作
  │     rbac_models.py    数据模型：请求/响应的字段定义
  │
  ├──► create_auth_tables.py   建表脚本：密码表
  └──► create_rbac_tables.py   建表脚本：用户/角色/权限表 + 初始数据
```

## 文件逐一说明

### 认证模块（你是谁）

| 文件 | 作用 |
|------|------|
| `auth.py` | **鉴权核心**。签发随机 token 存入 Redis，校验时从 Redis 取回。对外暴露 `TokenData`（当前用户信息）、`get_current_user`（FastAPI 鉴权依赖）、`require_permission`（权限检查依赖）。所有需要登录的接口都依赖这里。 |
| `auth_service.py` | **登录业务**。账号密码的注册、登录验证、修改密码。密码用 bcrypt 哈希存储，不存明文。 |
| `auth_api.py` | **认证路由**。4 个接口：`POST /register` 注册、`POST /login` 登录、`POST /logout` 登出（从 Redis 删 token）、`POST /change-password` 改密码。 |

### 权限模块（你能干什么）

| 文件 | 作用 |
|------|------|
| `rbac_models.py` | **数据模型**。用 Pydantic 定义请求体和响应体的字段格式，比如 `UserCreate`、`RoleCreate`、`Response`（统一响应包装）。 |
| `rbac_service.py` | **权限业务**。操作数据库的 3 个类：`UserService`（用户 CRUD + 查角色/权限）、`RoleService`（角色 CRUD + 分配权限）、`PermissionService`（权限 CRUD）。 |
| `users_api.py` | **用户管理路由**。`GET /current-user` 取当前登录用户信息，以及用户的增删改查分页接口。 |
| `roles_api.py` | **角色管理路由**。角色的增删改查，以及给角色分配权限的接口。 |
| `permissions_api.py` | **权限管理路由**。权限的增删改查接口。 |

### 建表脚本（初始化数据库）

| 文件 | 作用 |
|------|------|
| `create_auth_tables.py` | 创建密码表（`myagent_password_mgmt`）。首次部署时运行一次。 |
| `create_rbac_tables.py` | 创建 5 张 RBAC 表（用户/角色/用户-角色/权限/角色-权限），并插入初始数据：3 个角色（admin/operator/guest）和 18 条权限码。首次部署时运行一次。 |

## 登录流程（一次请求的完整链路）

```
1. 前端发送 POST /api/v1/auth/login  body: {"username":"admin", "password":"xxx"}
2. auth_api.py 接收请求 → 调用 auth_service.verify_password_login()
3. auth_service 查数据库：找到用户 → bcrypt 校验密码 → 返回用户信息
4. auth_api 调用 auth.create_access_token() → 生成随机 token → 存入 Redis（TTL 12小时）
5. 返回给前端：{"access_token": "abc123...", "roles": ["admin"], "permissions": [...]}
6. 前端把 token 存 localStorage，后续请求都带 Authorization: Bearer abc123...
```

## 鉴权流程（每个需要登录的接口）

```
1. 前端请求 GET /api/v1/users/current-user  header: Authorization: Bearer abc123...
2. FastAPI 自动调用 auth.get_current_user()
3. get_current_user 从 Redis 取 session:abc123... → 还原 TokenData
4. 如果 Redis 里没有 → 401 未登录
5. 如果有 → 把 TokenData 注入路由函数，业务代码直接用 current_user.uid
```

## RBAC 权限体系

三个角色，权限从高到低：

| 角色 | 能做什么 |
|------|---------|
| `admin` | 全部 18 条权限（用户管理 + 角色管理 + 权限管理 + 知识库 + 报表 + FAQ + 附身） |
| `operator` | 知识库读写 + 上传 + 报表查看 + FAQ 读写（不能管用户/角色/权限） |
| `guest` | 知识库只读 + 报表查看 + FAQ 只读 |

注册新用户默认绑定 `guest` 角色，管理员可通过后台调整。

## 技术选型

| 选择 | 原因 |
|------|------|
| Redis session | 登出能真正杀 token，服务端完全掌控会话生命周期 |
| bcrypt（非 MD5/SHA） | 抗彩虹表、抗暴力破解，业界标准密码哈希方案 |
| RBAC（非 ACL） | 角色→权限的间接模型，用户量大了以后维护成本低 |
