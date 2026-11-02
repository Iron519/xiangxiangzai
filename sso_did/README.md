# sso_did — 登录鉴权

单表用户、注册申请制、Redis session token。无 RBAC。

## 文件

| 文件 | 作用 |
|------|------|
| `auth.py` | TokenData 数据类、Redis session 签发/校验/销毁、`get_current_user` 鉴权依赖 |
| `auth_service.py` | 全部业务逻辑 + FastAPI 路由（注册/登录/登出/改密码/用户管理） |
| `create_user_table.py` | 建表脚本，首次部署运行一次 |

## 数据库表

```sql
myagent_user_mgmt (
    id            bigserial PRIMARY KEY,
    uid           varchar(64)  UNIQUE,      -- = username，用户唯一标识
    username      varchar(30),
    password_hash varchar(200),             -- bcrypt
    apply_reason  text,                     -- 注册申请理由
    approved      boolean DEFAULT false,    -- 管理员批准后才能登录
    remark        text DEFAULT '',          -- 管理员备注
    deleted       boolean DEFAULT false,    -- 软删
    create_time   timestamp DEFAULT now(),
    update_time   timestamp DEFAULT now()
)
```

## API

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| POST | `/api/v1/auth/register` | 无 | 注册=提交申请，不返回 token |
| POST | `/api/v1/auth/login` | 无 | 校验密码+approved，签发 token |
| POST | `/api/v1/auth/logout` | 可选 | 从 Redis 删 token |
| POST | `/api/v1/auth/change-password` | 登录 | 改密码 |
| GET | `/api/v1/auth/me` | 登录 | 当前用户信息 |
| GET | `/api/v1/users/page` | admin | 用户列表（可按 approved 筛选） |
| POST | `/api/v1/users/{uid}/approve` | admin | 批准注册申请 |
| POST | `/api/v1/users/{uid}/reject` | admin | 撤销批准 |
| PUT | `/api/v1/users/{uid}/remark` | admin | 修改备注 |
| DELETE | `/api/v1/users/{uid}` | admin | 软删除用户 |

## 流程

```
注册:  填用户名+密码+申请理由 → approved=false → 提示等待批准
批准:  admin 登录 → 用户管理页 → Approve → approved=true
登录:  用户名+密码 → 校验密码 → 检查 approved → 签发 Redis token
请求:  Authorization: Bearer <token> → Redis 查 session:<token> → TokenData
登出:  Redis DEL session:<token>
```
