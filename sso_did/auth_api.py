import logging
import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sso_did.auth import (
    TokenData,
    create_access_token,
    get_current_user,
    revoke_access_token,
    security_scheme,
)
from sso_did.auth_service import (
    change_password,
    get_user_id_by_uid,
    get_user_with_grant,
    register_password_user,
    verify_password_login,
)
from sso_did.rbac_models import Response

logger = logging.getLogger("AuthAPI")
router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


# ================= 请求模型 =================
class PasswordRegisterReq(BaseModel):
    username: str
    password: str
    displayName: str | None = None


class PasswordLoginReq(BaseModel):
    username: str
    password: str


class ChangePasswordReq(BaseModel):
    oldPassword: str
    newPassword: str


# ================= 通用 =================
def _issue_token(uid: str, username: str | None = None) -> dict:
    user, roles, permissions = get_user_with_grant(uid)
    display_name = username or (user or {}).get("display_name") or uid
    access_token = create_access_token(uid=uid, username=display_name, roles=roles, permissions=permissions)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "uid": uid,
        "username": display_name,
        "roles": roles,
        "permissions": permissions,
    }


# ================= 账号密码注册/登录 =================
@router.post("/register", response_model=Response)
async def password_register(body: PasswordRegisterReq):
    """账号密码注册：创建用户并绑定默认角色，返回 token。"""
    try:
        user = register_password_user(body.username, body.password, body.displayName)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    uid = user["account_id"]
    data = _issue_token(uid, user.get("display_name"))
    logger.info(f"[Auth] Password register uid={uid}, roles={data['roles']}")
    return Response.ok(data=data)


@router.post("/login", response_model=Response)
async def password_login(body: PasswordLoginReq):
    """账号密码登录：校验通过后签发 token。"""
    try:
        user = verify_password_login(body.username, body.password)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    uid = user["account_id"]
    data = _issue_token(uid, user.get("display_name"))
    logger.info(f"[Auth] Password login uid={uid}, roles={data['roles']}")
    return Response.ok(data=data)


@router.post("/change-password", response_model=Response)
async def auth_change_password(
    body: ChangePasswordReq,
    current_user: TokenData = Depends(get_current_user),
):
    """修改密码：校验旧密码后更新。"""
    user_id = get_user_id_by_uid(current_user.uid)
    if not user_id:
        return Response.fail(code=400, message="无法确定本地用户")
    try:
        change_password(user_id, body.oldPassword, body.newPassword)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    return Response.ok(data={"changed": True})


# ================= 登出 =================
@router.post("/logout", response_model=Response)
async def auth_logout(
    credentials=Depends(security_scheme),
):
    """登出：从 Redis 删除 token（真正失效，不像 JWT 只能前端清除）。"""
    if credentials:
        revoke_access_token(credentials.credentials)
    return Response.ok(data={"logout": True})


# ================= [DEV] 调试入口 =================
@router.get("/debug/login", response_model=Response)
async def debug_login(username: str):
    """[DEV] 免密码直签 token（仅 DEBUG_SSO=true 时可用），联调用。"""
    if os.getenv("DEBUG_SSO", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return Response.fail(code=403, message="Debug login disabled (DEBUG_SSO not enabled)")
    from sso_did.auth_service import _user_svc
    user = _user_svc.get_by_uid(username)
    if not user:
        return Response.fail(code=404, message=f"User '{username}' not found")
    data = _issue_token(username, user.get("display_name"))
    return Response.ok(data=data)
