import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg
from dotenv import load_dotenv, find_dotenv

# ================= 路径配置及import内部功能 ===============
from sso_did.rbac_service import (
    ROLE_TABLE,
    USER_ROLE_TABLE,
    USER_TABLE,
    UserService,
    _pg_conn,
)

load_dotenv(find_dotenv())

logger = logging.getLogger("AuthService")

IDENTITY_TABLE = os.getenv("PG_HRSSC_IDENTITY_TABLE", "hrssc_identity_mgmt")
VERIFY_CODE_TABLE = os.getenv("PG_HRSSC_VERIFY_CODE_TABLE", "hrssc_verify_code_mgmt")
PASSWORD_TABLE = os.getenv("PG_HRSSC_PASSWORD_TABLE", "hrssc_password_mgmt")

SMS_DRIVER = os.getenv("SMS_DRIVER", "mock")
VERIFY_CODE_TTL_MINUTES = int(os.getenv("VERIFY_CODE_TTL_MINUTES", "5"))
VERIFY_CODE_MAX_ATTEMPTS = int(os.getenv("VERIFY_CODE_MAX_ATTEMPTS", "5"))
VERIFY_CODE_SEND_COOLDOWN_SECONDS = int(os.getenv("VERIFY_CODE_SEND_COOLDOWN_SECONDS", "60"))
DEFAULT_ROLE = os.getenv("AUTH_DEFAULT_ROLE", "guest")

_user_svc = UserService()


# ================= 手机号/验证码 =================
def _now() -> datetime:
    """朴素 UTC 时间（与 PG timestamp 列对齐，避免 naive/aware 相减报错）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_phone(phone: str) -> str:
    """归一化手机号：去空格/横线，去掉 +86 前缀。"""
    p = (phone or "").strip().replace(" ", "").replace("-", "")
    if p.startswith("+86"):
        p = p[3:]
    return p


def generate_code() -> str:
    return f"{random.randrange(0, 1_000_000):06d}"


def send_verify_code(phone: str, scene: str = "login") -> str | None:
    """生成并下发验证码。SMS_DRIVER=mock 时验证码打印到日志并返回（供联调）。"""
    phone = normalize_phone(phone)
    if scene not in {"login", "bind"}:
        scene = "login"

    now = _now()
    with _pg_conn() as conn, conn.cursor() as cur:
        # 发送冷却：同手机号同场景 60s 内有未使用的验证码则复用发送时间限制
        cur.execute(
            f"SELECT create_time FROM {VERIFY_CODE_TABLE} "
            f"WHERE phone=%s AND scene=%s AND used=false ORDER BY id DESC LIMIT 1",
            (phone, scene),
        )
        row = cur.fetchone()
        if row and (now - row[0]).total_seconds() < VERIFY_CODE_SEND_COOLDOWN_SECONDS:
            remaining = VERIFY_CODE_SEND_COOLDOWN_SECONDS - (now - row[0]).total_seconds()
            raise ValueError(f"发送过于频繁，请 {int(remaining)} 秒后再试")

        code = generate_code()
        cur.execute(
            f"INSERT INTO {VERIFY_CODE_TABLE} (phone, code, scene, expires_at) "
            f"VALUES (%s,%s,%s,%s)",
            (phone, code, scene, now + timedelta(minutes=VERIFY_CODE_TTL_MINUTES)),
        )
        conn.commit()

    _dispatch_sms(phone, code)
    return code


def _dispatch_sms(phone: str, code: str) -> None:
    """短信下发驱动。SMS_DRIVER=mock 时只打日志（生产接入真实短信后再扩展分支）。"""
    if SMS_DRIVER == "mock":
        logger.info(f"[SMS-MOCK] 发送验证码到 {phone}: {code}")
        return
    logger.error(f"[SMS] 未配置真实短信驱动（SMS_DRIVER={SMS_DRIVER}），验证码 {code} 未真正下发")


def verify_code(phone: str, code: str, scene: str = "login") -> None:
    """校验验证码，成功后标记已使用。非法抛 ValueError。"""
    phone = normalize_phone(phone)
    scene = "login" if scene not in {"login", "bind"} else scene
    if not phone:
        raise ValueError("手机号不能为空")

    now = _now()
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, code, expires_at, attempts FROM {VERIFY_CODE_TABLE} "
            f"WHERE phone=%s AND scene=%s AND used=false "
            f"ORDER BY id DESC LIMIT 5",
            (phone, scene),
        )
        rows = cur.fetchall()
        if not rows:
            raise ValueError("验证码不存在或已使用，请重新获取")

        # 逐个尝试：优先从尝试次数最少的最新鲜记录匹配
        for row_id, row_code, expires_at, attempts in rows:
            if attempts >= VERIFY_CODE_MAX_ATTEMPTS:
                continue
            if now > expires_at:
                continue
            if row_code != code:
                cur.execute(
                    f"UPDATE {VERIFY_CODE_TABLE} SET attempts=attempts+1 WHERE id=%s",
                    (row_id,),
                )
                conn.commit()
                raise ValueError("验证码错误")
            cur.execute(
                f"UPDATE {VERIFY_CODE_TABLE} SET used=true, attempts=attempts+1 WHERE id=%s",
                (row_id,),
            )
            conn.commit()
            return
        raise ValueError("验证码错误或已过期，请重新获取")


# ================= 用户 upsert =================
def _find_user_id_by_identity(provider: str, identifier: str) -> int | None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT user_id FROM {IDENTITY_TABLE} "
            f"WHERE provider=%s AND identifier=%s AND deleted=false AND status='Enabled'",
            (provider, identifier),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _create_user(account_id: str, display_name: str | None = None) -> int:
    """建本地用户并绑定默认角色（幂等：account_id 冲突则复用）。"""
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s", (account_id,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            f"INSERT INTO {USER_TABLE} (account_id, display_name, status, deleted) "
            f"VALUES (%s,%s,'Enabled',false) RETURNING id",
            (account_id, display_name or account_id),
        )
        user_id = cur.fetchone()[0]
        cur.execute(
            f"SELECT id FROM {ROLE_TABLE} WHERE code=%s AND deleted=false AND status='Enabled'",
            (DEFAULT_ROLE,),
        )
        role_row = cur.fetchone()
        if role_row:
            cur.execute(
                f"INSERT INTO {USER_ROLE_TABLE} (user_id, role_id, deleted) VALUES (%s,%s,false)",
                (user_id, role_row[0]),
            )
        conn.commit()
        logger.info(f"[Auth] 已自动创建用户 account_id={account_id}，默认角色 {DEFAULT_ROLE}")
        return user_id


def _bind_identity(conn, user_id: int, provider: str, identifier: str, unionid: str | None = None) -> None:
    """绑定身份（幂等：已存在则更新 unionid）。"""
    cur = conn.cursor()
    cur.execute(
        f"SELECT id FROM {IDENTITY_TABLE} WHERE provider=%s AND identifier=%s",
        (provider, identifier),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            f"UPDATE {IDENTITY_TABLE} SET user_id=%s, unionid=%s, status='Enabled', deleted=false, update_time=now() "
            f"WHERE provider=%s AND identifier=%s",
            (user_id, unionid, provider, identifier),
        )
    else:
        cur.execute(
            f"INSERT INTO {IDENTITY_TABLE} (user_id, provider, identifier, unionid, status, deleted) "
            f"VALUES (%s,%s,%s,%s,'Enabled',false)",
            (user_id, provider, identifier, unionid),
        )


def ensure_phone_user(phone: str) -> dict:
    """按手机号定位或创建用户，返回 sys_user 行 + isNew 标记。"""
    phone = normalize_phone(phone)
    user_id = _find_user_id_by_identity("mobile", phone)
    if not user_id:
        # 兼容历史数据：account_id 直接是手机号的存量用户
        existing = _user_svc.get_by_uid(phone)
        if existing:
            user_id = existing["id"]
        else:
            user_id = _create_user(phone, display_name=f"用户{phone[-4:]}")
        with _pg_conn() as conn:
            with conn:
                _bind_identity(conn, user_id, "mobile", phone)
    return _user_svc.get_by_uid(str(phone)) or _load_user_by_id(user_id)


def _load_user_by_id(user_id: int) -> dict:
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT * FROM {USER_TABLE} WHERE id=%s AND deleted=false", (user_id,))
        return cur.fetchone() or {}


def get_user_id_by_uid(account_id: str) -> int | None:
    """按 account_id 取本地用户主键 id（供绑定手机等服务使用）。"""
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (account_id,))
        row = cur.fetchone()
        return row[0] if row else None


def has_mobile_identity(user_id: int) -> bool:
    """用户是否已绑定手机号身份。"""
    if not user_id:
        return False
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {IDENTITY_TABLE} WHERE user_id=%s AND provider='mobile' AND deleted=false AND status='Enabled'",
            (user_id,),
        )
        return cur.fetchone() is not None


def get_user_with_grant(uid: str) -> tuple[dict | None, list[str], list[str]]:
    """取用户 + 角色 codes + 权限 codes，供签发 JWT。"""
    user = _user_svc.get_by_uid(uid)
    roles = _user_svc.get_role_codes(uid)
    permissions = _user_svc.get_permissions(uid)
    return user, roles, permissions


def resolve_wechat_user(provider: str, openid: str, unionid: str | None, nickname: str | None) -> dict:
    """按 openid 定位或创建微信用户。provider ∈ wechat_mini / wechat_mp。"""
    user_id = _find_user_id_by_identity(provider, openid)
    if user_id:
        return _load_user_by_id(user_id)

    account_id = f"wx_{openid}"
    user_id = _create_user(account_id, display_name=nickname or account_id[:13])
    with _pg_conn() as conn:
        with conn:
            _bind_identity(conn, user_id, provider, openid, unionid)
    return _load_user_by_id(user_id)


def bind_phone(user_id: int, phone: str, unionid: str | None = None) -> str:
    """给已登录用户绑定手机号；返回绑定后的 account_id（微信首登用户会转为手机号）。"""
    phone = normalize_phone(phone)
    owner_id = _find_user_id_by_identity("mobile", phone)
    if owner_id and owner_id != user_id:
        raise ValueError("该手机号已绑定其他账号")
    with _pg_conn() as conn:
        with conn:
            _bind_identity(conn, user_id, "mobile", phone, unionid)
    # 微信首登生成的 wx_<openid> 账号，绑定手机后转为手机号作为主标识
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {USER_TABLE} SET account_id=%s, display_name=COALESCE(NULLIF(display_name,''), %s), "
            f"update_time=now() WHERE id=%s AND deleted=false",
            (phone, f"用户{phone[-4:]}", user_id),
        )
        conn.commit()
    return phone


# ================= 账号密码注册/登录 =================
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")
_PASSWORD_MIN_LEN = 6


def _hash_password(plain: str) -> str:
    """bcrypt 哈希密码，返回 utf-8 字符串。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _validate_username(username: str) -> str:
    """校验用户名格式：3-30 位字母/数字/下划线。"""
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("用户名需 3-30 位，仅支持字母、数字、下划线")
    return username


def _validate_password(password: str) -> str:
    """校验密码强度：至少 6 位。"""
    if not password or len(password) < _PASSWORD_MIN_LEN:
        raise ValueError(f"密码至少 {_PASSWORD_MIN_LEN} 位")
    return password


def register_password_user(username: str, password: str, display_name: str | None = None) -> dict:
    """账号密码注册：创建用户 + 绑定 password 身份 + 写入密码哈希。

    account_id 使用用户名（唯一），identity provider='password'。
    返回 sys_user 行。
    """
    username = _validate_username(username)
    password = _validate_password(password)

    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 检查用户名是否已存在（account_id 唯一）
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (username,))
        if cur.fetchone():
            raise ValueError("该用户名已被注册")
        # 检查 password 身份是否已绑定
        cur.execute(
            f"SELECT 1 FROM {IDENTITY_TABLE} WHERE provider='password' AND identifier=%s AND deleted=false",
            (username,),
        )
        if cur.fetchone():
            raise ValueError("该用户名已被注册")

        # 创建用户
        cur.execute(
            f"INSERT INTO {USER_TABLE} (account_id, display_name, status, deleted) "
            f"VALUES (%s,%s,'Enabled',false) RETURNING id",
            (username, display_name or username),
        )
        user_id = cur.fetchone()["id"]

        # 绑定默认角色
        cur.execute(
            f"SELECT id FROM {ROLE_TABLE} WHERE code=%s AND deleted=false AND status='Enabled'",
            (DEFAULT_ROLE,),
        )
        role_row = cur.fetchone()
        if role_row:
            cur.execute(
                f"INSERT INTO {USER_ROLE_TABLE} (user_id, role_id, deleted) VALUES (%s,%s,false)",
                (user_id, role_row["id"]),
            )

        # 绑定 password 身份
        cur.execute(
            f"INSERT INTO {IDENTITY_TABLE} (user_id, provider, identifier, status, deleted) "
            f"VALUES (%s,'password',%s,'Enabled',false)",
            (user_id, username),
        )

        # 写入密码哈希
        cur.execute(
            f"INSERT INTO {PASSWORD_TABLE} (user_id, password_hash, status, deleted) "
            f"VALUES (%s,%s,'Enabled',false)",
            (user_id, _hash_password(password)),
        )

        conn.commit()
        logger.info(f"[Auth] 账号密码注册成功 username={username}, user_id={user_id}")
        return {"id": user_id, "account_id": username, "display_name": display_name or username}


def verify_password_login(username: str, password: str) -> dict:
    """账号密码登录：校验用户名+密码，返回 sys_user 行。

    失败抛 ValueError。
    """
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")

    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 按 password 身份定位用户
        cur.execute(
            f"SELECT user_id FROM {IDENTITY_TABLE} "
            f"WHERE provider='password' AND identifier=%s AND deleted=false AND status='Enabled'",
            (username,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("用户名或密码错误")
        user_id = row["user_id"]

        # 取密码哈希
        cur.execute(
            f"SELECT password_hash FROM {PASSWORD_TABLE} "
            f"WHERE user_id=%s AND deleted=false AND status='Enabled'",
            (user_id,),
        )
        pwd_row = cur.fetchone()
        if not pwd_row:
            raise ValueError("用户名或密码错误")

        if not _verify_password(password, pwd_row["password_hash"]):
            raise ValueError("用户名或密码错误")

        # 取用户行
        cur.execute(f"SELECT * FROM {USER_TABLE} WHERE id=%s AND deleted=false", (user_id,))
        user = cur.fetchone()
        if not user:
            raise ValueError("用户不存在或已禁用")
        if user.get("status") != "Enabled":
            raise ValueError("账号已禁用，请联系管理员")

        return user


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    """修改密码：校验旧密码后更新。"""
    new_password = _validate_password(new_password)
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT password_hash FROM {PASSWORD_TABLE} "
            f"WHERE user_id=%s AND deleted=false AND status='Enabled'",
            (user_id,),
        )
        pwd_row = cur.fetchone()
        if not pwd_row:
            raise ValueError("该账号未设置密码，请先绑定密码")
        if not _verify_password(old_password, pwd_row["password_hash"]):
            raise ValueError("旧密码错误")
        cur.execute(
            f"UPDATE {PASSWORD_TABLE} SET password_hash=%s, update_time=now() WHERE user_id=%s",
            (_hash_password(new_password), user_id),
        )
        conn.commit()
        logger.info(f"[Auth] 修改密码成功 user_id={user_id}")