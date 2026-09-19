import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv, find_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv(find_dotenv())

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logger = logging.getLogger("Auth")

# ================= 配置 =================
JWT_ALGORITHM = "HS256"
JWT_ACCESS_SECRET = os.getenv("JWT_ACCESS_SECRET", "")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", ""))  # 控制需要重新登录的间隔时长


# ================= JWT =================
@dataclass
class TokenData:
    """解码后的 JWT token 数据。"""
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


def create_access_token(
    uid: str,
    username: str | None = None,
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
) -> str:
    """签发 JWT access-token，将获取的user_info信息存入token中，供后续鉴权使用。

    payload 含 roles 与 permissions 两套：roles 标识身份等级（admin/operator/guest），
    permissions 为细粒度权限码（system:user:save 等），由角色-权限关联表查出。
    鉴权优先用 permissions（权限驱动），roles 保留用于前端粗粒度判断。
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": uid,
        "username": username or uid,
        "roles": roles or [],
        "permissions": permissions or [],
        "exp": expire,
        "iat": now,
    }
    return jwt.encode(payload, JWT_ACCESS_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenData:
    """校验并解码 JWT，失败抛 401。"""
    try:
        payload = jwt.decode(token, JWT_ACCESS_SECRET, algorithms=[JWT_ALGORITHM])
        return TokenData(
            uid=payload["sub"],
            username=payload.get("username"),
            roles=payload.get("roles", []),
            permissions=payload.get("permissions", []),
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ================= 鉴权依赖 =================
security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> TokenData:
    """从 JWT 取当前用户，未登录抛 401。"""
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
    """依赖：要求具备指定权限码，否则 403（权限驱动鉴权，推荐使用）。"""
    async def checker(
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    ) -> TokenData:
        user = await get_current_user(credentials)
        if not user.has_permission(code):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: {code}")
        return user
    return checker


def require_any_permission(codes: list[str]):
    """依赖：要求具备任一权限码，否则 403（权限驱动鉴权，推荐使用）。"""
    async def checker(
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    ) -> TokenData:
        user = await get_current_user(credentials)
        if not user.has_any_permission(codes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Permission required: any of {codes}")
        return user
    return checker