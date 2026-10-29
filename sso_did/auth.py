import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

import redis
from dotenv import load_dotenv, find_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv(find_dotenv())

PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logger = logging.getLogger("Auth")

# ================= Redis 配置 =================
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_HOURS", "12")) * 3600
SESSION_KEY_PREFIX = "session:"

_redis_pool: redis.ConnectionPool | None = None


def _get_redis() -> redis.Redis:
    """获取 Redis 连接（惰性创建连接池）。"""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            password=REDIS_PASSWORD or None,
            decode_responses=True,
        )
    return redis.Redis(connection_pool=_redis_pool)


# ================= TokenData =================
@dataclass
class TokenData:
    """从 Redis 还原的会话数据，接口与旧 JWT 版本完全兼容。"""
    uid: str
    username: str | None = None
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)

    def has_role(self, role_code: str) -> bool:
        return role_code in self.roles

    def has_any_role(self, role_codes: list[str]) -> bool:
        return any(r in self.roles for r in role_codes)

    def has_permission(self, code: str) -> bool:
        return code in self.permissions

    def has_any_permission(self, codes: list[str]) -> bool:
        return any(c in self.permissions for c in codes)


# ================= Token 签发 / 校验 / 销毁 =================
def _serialize(user: TokenData) -> str:
    return json.dumps({
        "uid": user.uid,
        "username": user.username,
        "roles": user.roles,
        "permissions": user.permissions,
    }, ensure_ascii=False)


def _deserialize(raw: str) -> TokenData:
    d = json.loads(raw)
    return TokenData(
        uid=d["uid"],
        username=d.get("username"),
        roles=d.get("roles", []),
        permissions=d.get("permissions", []),
    )


def create_access_token(
    uid: str,
    username: str | None = None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    """生成随机 token 并存入 Redis，返回 token 字符串。"""
    token = secrets.token_urlsafe(32)
    data = TokenData(uid=uid, username=username or uid,
                     roles=roles or [], permissions=permissions or [])
    r = _get_redis()
    r.setex(f"{SESSION_KEY_PREFIX}{token}", SESSION_TTL_SECONDS, _serialize(data))
    logger.info(f"[Auth] Token issued uid={uid}, roles={data.roles}")
    return token


def decode_access_token(token: str) -> TokenData:
    """从 Redis 取会话，不存在或已过期抛 401。"""
    r = _get_redis()
    raw = r.get(f"{SESSION_KEY_PREFIX}{token}")
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired or invalid")
    return _deserialize(raw)


def revoke_access_token(token: str) -> bool:
    """登出：从 Redis 删除 token，返回是否删除成功。"""
    r = _get_redis()
    deleted = r.delete(f"{SESSION_KEY_PREFIX}{token}")
    return deleted > 0


# ================= 鉴权依赖（接口与旧版完全兼容） =================
security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> TokenData:
    """从 Bearer token 取当前用户，未登录抛 401。"""
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return decode_access_token(credentials.credentials)


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> TokenData | None:
    """已登录返回用户，否则返回 None（不抛错）。"""
    if not credentials:
        return None
    try:
        return decode_access_token(credentials.credentials)
    except HTTPException:
        return None


def require_role(role_code: str):
    """依赖：要求具备指定角色，否则 403。"""
    async def checker(
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    ) -> TokenData:
        user = await get_current_user(credentials)
        if not user.has_role(role_code):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Role required: {role_code}")
        return user
    return checker


def require_any_role(role_codes: list[str]):
    """依赖：要求具备任一角色，否则 403。"""
    async def checker(
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    ) -> TokenData:
        user = await get_current_user(credentials)
        if not user.has_any_role(role_codes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Role required: any of {role_codes}")
        return user
    return checker


def require_permission(code: str):
    """依赖：要求具备指定权限码，否则 403。"""
    async def checker(
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    ) -> TokenData:
        user = await get_current_user(credentials)
        if not user.has_permission(code):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {code}")
        return user
    return checker


def require_any_permission(codes: list[str]):
    """依赖：要求具备任一权限码，否则 403。"""
    async def checker(
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    ) -> TokenData:
        user = await get_current_user(credentials)
        if not user.has_any_permission(codes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: any of {codes}")
        return user
    return checker
