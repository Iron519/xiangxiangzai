import logging
import os
import sys
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

logger = logging.getLogger("WeChatClient")

WX_API_BASE = "https://api.weixin.qq.com"
WX_OAUTH_AUTH_URI = "https://open.weixin.qq.com/connect/oauth2/authorize"

WECHAT_MINI_APPID = os.getenv("WECHAT_MINI_APPID", "")
WECHAT_MINI_SECRET = os.getenv("WECHAT_MINI_SECRET", "")
WECHAT_MP_APPID = os.getenv("WECHAT_MP_APPID", "")
WECHAT_MP_SECRET = os.getenv("WECHAT_MP_SECRET", "")


class WeChatClient:
    """微信客户端：小程序（code2session / 手机号组件）+ 公众号 H5 网页授权。"""

    def __init__(self, name: str, appid: str, secret: str) -> None:
        self.name = name
        self.appid = appid
        self.secret = secret

    @property
    def configured(self) -> bool:
        return bool(self.appid and self.secret)

    # ---------- 小程序 ----------
    async def code2session(self, js_code: str) -> dict | None:
        """wx.login 的 code 换 openid/session_key/unionid。"""
        params = {
            "appid": self.appid,
            "secret": self.secret,
            "js_code": js_code,
            "grant_type": "authorization_code",
        }
        return await self._get(f"{WX_API_BASE}/sns/jscode2session", params)

    async def get_user_phone_number(self, phone_code: str) -> dict | None:
        """小程序手机号快捷登录组件：拿其返回的 code 换手机号（收费接口）。

        wx.getPhoneNumber 得到 e.detail.code，业务后端调用本接口换取手机号，
        无需再解密 encryptedData。
        """
        token = await self._get_stable_token()
        if not token:
            return None
        return await self._post_json(
            f"{WX_API_BASE}/wxa/business/getuserphonenumber",
            {"access_token": token},
            {"code": phone_code},
        )

    async def _get_stable_token(self) -> str | None:
        """获取小程序 access_token（用于业务 API）。"""
        data = {"grant_type": "client_credential", "appid": self.appid, "secret": self.secret}
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            try:
                resp = await client.post(f"{WX_API_BASE}/cgi-bin/stable_token", json=data)
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("access_token"):
                    return payload["access_token"]
                logger.error(f"[WeChat] stable_token error: {payload}")
                return None
            except httpx.HTTPError as e:
                logger.error(f"[WeChat] stable_token request error: {e}")
                return None

    # ---------- 公众号 H5 网页授权 ----------
    def get_mp_authorization_url(self, redirect_uri: str, state: str = "", scope: str = "snsapi_userinfo") -> str:
        """构造网页授权 URL（前端跳过去让用户点“同意授权”）。"""
        params = {
            "appid": self.appid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
        }
        return f"{WX_OAUTH_AUTH_URI}?{urlencode(params)}#wechat_redirect"

    async def exchange_oauth_token(self, code: str) -> dict | None:
        """网页授权 code → access_token/openid/unionid。"""
        params = {
            "appid": self.appid,
            "secret": self.secret,
            "code": code,
            "grant_type": "authorization_code",
        }
        return await self._get(f"{WX_API_BASE}/sns/oauth2/access_token", params)

    async def get_userinfo(self, oauth_access_token: str, openid: str) -> dict | None:
        """网页授权 userinfo（昵称/头像/unionid）。"""
        params = {"access_token": oauth_access_token, "openid": openid, "lang": "zh_CN"}
        return await self._get(f"{WX_API_BASE}/sns/userinfo", params)

    # ---------- 通用 ----------
    async def _get(self, url: str, params: dict) -> dict | None:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            try:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("errcode") not in {None, 0}:
                    logger.error(f"[WeChat] {self.name} GET {url} error: {payload}")
                    return None
                return payload
            except httpx.HTTPError as e:
                logger.error(f"[WeChat] {self.name} GET request error: {e}")
                return None

    async def _post_json(self, url: str, params: dict, json_body: dict) -> dict | None:
        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            try:
                resp = await client.post(url, params=params, json=json_body)
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("errcode") not in {None, 0}:
                    logger.error(f"[WeChat] {self.name} POST {url} error: {payload}")
                    return None
                return payload
            except httpx.HTTPError as e:
                logger.error(f"[WeChat] {self.name} POST request error: {e}")
                return None


mini_client = WeChatClient("mini", WECHAT_MINI_APPID, WECHAT_MINI_SECRET)
mp_client = WeChatClient("mp", WECHAT_MP_APPID, WECHAT_MP_SECRET)