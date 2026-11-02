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
    uid: str
    username: str | None = None
    approved: bool = True

    @property
    def is_admin(self) -> bool:
        return self.uid == os.getenv("ADMIN_UID", "admin")


# ================= Token 签发 / 校验 / 销毁 =================
def _serialize(user: TokenData) -> str:
    return json.dumps({
        "uid": user.uid,
        "username": user.username,
        "approved": user.approved,
    }, ensure_ascii=False)


def _deserialize(raw: str) -> TokenData:
    d = json.loads(raw)
    return TokenData(
        uid=d["uid"],
        username=d.get("username"),
        approved=d.get("approved", True),
    )


def create_access_token(uid: str, username: str | None = None, approved: bool = True) -> str:
    token = secrets.token_urlsafe(32)
    data = TokenData(uid=uid, username=username or uid, approved=approved)
    r = _get_redis()
    r.setex(f"{SESSION_KEY_PREFIX}{token}", SESSION_TTL_SECONDS, _serialize(data))
    logger.info(f"[Auth] Token issued uid={uid}, approved={approved}")
    return token


def decode_access_token(token: str) -> TokenData:
    r = _get_redis()
    raw = r.get(f"{SESSION_KEY_PREFIX}{token}")
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired or invalid")
    return _deserialize(raw)


def revoke_access_token(token: str) -> bool:
    r = _get_redis()
    deleted = r.delete(f"{SESSION_KEY_PREFIX}{token}")
    return deleted > 0


# ================= 鉴权依赖 =================
security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> TokenData:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return decode_access_token(credentials.credentials)


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> TokenData | None:
    if not credentials:
        return None
    try:
        return decode_access_token(credentials.credentials)
    except HTTPException:
        return None
