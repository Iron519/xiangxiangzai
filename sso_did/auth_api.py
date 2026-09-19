import logging
import os
import sys

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

# ================= 路径配置及import内部功能 ===============
from sso_did.auth import TokenData, create_access_token, get_current_user
from sso_did.auth_service import (
    SMS_DRIVER,
    bind_phone,
    ensure_phone_user,
    get_user_id_by_uid,
    get_user_with_grant,
    has_mobile_identity,
    normalize_phone,
    resolve_wechat_user,
    send_verify_code,
    verify_code,
)
from sso_did.rbac_models import Response
from sso_did.wx_client import mini_client, mp_client

logger = logging.getLogger("AuthAPI")
router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


# ================= 请求模型 =================
class SmsSendReq(BaseModel):
    phone: str
    scene: str = "login"


class SmsLoginReq(BaseModel):
    phone: str
    code: str


class WeChatMiniLoginReq(BaseModel):
    code: str


class WeChatMiniBindPhoneReq(BaseModel):
    phoneCode: str


class WeChatMpLoginReq(BaseModel):
    code: str
    redirectUri: str = ""


class BindPhoneReq(BaseModel):
    phone: str
    code: str


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


# ================= 手机验证码 =================
@router.post("/sms/send", response_model=Response)
async def sms_send(body: SmsSendReq):
    """发送短信验证码。SMS_DRIVER=mock 时返回 debugCode 便于联调。"""
    phone = normalize_phone(body.phone)
    if not phone:
        return Response.fail(code=400, message="手机号不能为空")
    try:
        code = send_verify_code(phone, body.scene)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    data = {"sent": True}
    if SMS_DRIVER == "mock":
        data["debugCode"] = code
    return Response.ok(data=data)


@router.post("/sms/login", response_model=Response)
async def sms_login(body: SmsLoginReq):
    """手机号 + 验证码登录：校验通过自动建号/登录并签发 JWT。"""
    phone = normalize_phone(body.phone)
    if not phone:
        return Response.fail(code=400, message="手机号不能为空")
    try:
        verify_code(phone, body.code, "login")
    except ValueError as e:
        return Response.fail(code=400, message=str(e))

    user = ensure_phone_user(phone)
    uid = user.get("account_id") or phone
    data = _issue_token(uid)
    data["needBindPhone"] = False
    logger.info(f"[Auth] SMS login uid={uid}, roles={data['roles']}")
    return Response.ok(data=data)


# ================= 微信小程序 =================
@router.post("/wechat/mini/login", response_model=Response)
async def wechat_mini_login(body: WeChatMiniLoginReq):
    """小程序 wx.login 的 code 换 openid，定位/创建用户后签发 JWT。

    若该微信从未绑定手机号，则返回 needBindPhone=true，前端引导走手机号
    快捷登录组件（wx.getPhoneNumber → /wechat/mini/bind-phone）。
    """
    if not mini_client.configured:
        return Response.fail(code=500, message="微信小程序未配置（WECHAT_MINI_APPID/SECRET）")
    session = await mini_client.code2session(body.code)
    if not session or not session.get("openid"):
        return Response.fail(code=401, message="微信登录凭证无效或已过期")
    openid = session["openid"]
    unionid = session.get("unionid")

    user = resolve_wechat_user("wechat_mini", openid, unionid, None)
    uid = user.get("account_id") or f"wx_{openid}"
    data = _issue_token(uid)
    data["needBindPhone"] = not has_mobile_identity(user.get("id"))
    logger.info(f"[Auth] WeChat-mini login uid={uid}, needBindPhone={data['needBindPhone']}")
    return Response.ok(data=data)


@router.post("/wechat/mini/bind-phone", response_model=Response)
async def wechat_mini_bind_phone(
    body: WeChatMiniBindPhoneReq,
    current_user: TokenData = Depends(get_current_user),
):
    """小程序手机号快捷登录组件：code → 手机号 → 绑定，返回刷新后的 JWT。"""
    if not mini_client.configured:
        return Response.fail(code=500, message="微信小程序未配置（WECHAT_MINI_APPID/SECRET）")
    phone_info = await mini_client.get_user_phone_number(body.phoneCode)
    phone = None
    if phone_info and phone_info.get("phone_info"):
        phone = phone_info["phone_info"].get("purePhoneNumber") or phone_info["phone_info"].get("phoneNumber")
    if not phone:
        return Response.fail(code=401, message="手机号授权失败，请重试")

    user_id = get_user_id_by_uid(current_user.uid)
    if not user_id:
        return Response.fail(code=401, message="用户在库中不存在，请重新登录")
    try:
        phone = bind_phone(user_id, phone)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))

    data = _issue_token(phone)
    data["needBindPhone"] = False
    logger.info(f"[Auth] WeChat-mini bind phone success uid={phone}")
    return Response.ok(data=data)


# ================= 微信公众号 H5 网页授权 =================
@router.get("/wechat/mp/url", response_model=Response)
async def wechat_mp_url(
    origin: str = Query(default="", description="前端 origin，如 https://example.com:8080"),
    redirectUrl: str = Query(default="/", description="登录成功后前端跳转路径"),
    currentPage: str = Query(default="", description="当前页面完整 URL"),
):
    """获取公众号网页授权 URL，前端整页跳转（等价的旧 /sso/url/iam）。"""
    if not mp_client.configured:
        return Response.fail(code=500, message="微信公众号未配置（WECHAT_MP_APPID/SECRET）")
    redirect_uri = origin or "http://localhost:8099"
    state = f"{currentPage}|{redirectUrl}"
    url = mp_client.get_mp_authorization_url(redirect_uri, state)
    return Response.ok(data={"url": url})


@router.post("/wechat/mp/login", response_model=Response)
async def wechat_mp_login(body: WeChatMpLoginReq):
    """公众号网页授权 code → openid → 定位/创建用户 → 签发 JWT。"""
    if not mp_client.configured:
        return Response.fail(code=500, message="微信公众号未配置（WECHAT_MP_APPID/SECRET）")
    oauth = await mp_client.exchange_oauth_token(body.code)
    if not oauth or not oauth.get("openid"):
        return Response.fail(code=401, message="微信网页授权失败或已过期")
    openid = oauth["openid"]
    unionid = oauth.get("unionid")

    nickname = None
    userinfo = await mp_client.get_userinfo(oauth.get("access_token"), openid)
    if userinfo:
        nickname = userinfo.get("nickname")

    user = resolve_wechat_user("wechat_mp", openid, unionid, nickname)
    uid = user.get("account_id") or f"wx_{openid}"
    data = _issue_token(uid)
    data["needBindPhone"] = not has_mobile_identity(user.get("id"))
    logger.info(f"[Auth] WeChat-mp login uid={uid}, needBindPhone={data['needBindPhone']}")
    return Response.ok(data=data)


# ================= 登录后绑定手机号（H5/通用） =================
@router.post("/bind-phone", response_model=Response)
async def auth_bind_phone(
    body: BindPhoneReq,
    current_user: TokenData = Depends(get_current_user),
):
    """已登录但未绑手机的账号，用短信验证码补绑手机号并刷新 JWT。"""
    phone = normalize_phone(body.phone)
    if not phone:
        return Response.fail(code=400, message="手机号不能为空")
    try:
        verify_code(phone, body.code, "bind")
    except ValueError as e:
        return Response.fail(code=400, message=str(e))

    user_id = get_user_id_by_uid(current_user.uid)
    if not user_id:
        return Response.fail(code=400, message="绑定失败：无法确定本地用户")
    try:
        phone = bind_phone(user_id, phone)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))

    data = _issue_token(phone)
    data["needBindPhone"] = False
    logger.info(f"[Auth] bind-phone success uid={phone}")
    return Response.ok(data=data)


# ================= 登出 =================
@router.post("/logout", response_model=Response)
async def auth_logout():
    """登出：JWT 无状态，前端清 localStorage 即可。留空接口兼容前端。"""
    return Response.ok(data={"logout": True})


# ================= [DEV] 调试入口 =================
@router.get("/debug/uid")
async def debug_uid(uid: str = Query(..., description="测试登录账号 account_id/手机号")):
    """[DEV] 免验证码直签 JWT（仅 DEBUG_SSO=true 时可用），联调用。

    等价于旧的 /sso/debug-login。生产环境保持 DEBUG_SSO 关闭。
    """
    if os.getenv("DEBUG_SSO", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return Response.fail(code=403, message="Debug login disabled (DEBUG_SSO not enabled)")
    uid = normalize_phone(uid)
    user = ensure_phone_user(uid)
    uid = user.get("account_id") or uid
    data = _issue_token(uid)
    return Response.ok(data=data)