import logging
import os
import re
import sys

import bcrypt
import psycopg
from dotenv import load_dotenv, find_dotenv

from sso_did.rbac_service import (
    ROLE_TABLE,
    USER_ROLE_TABLE,
    USER_TABLE,
    UserService,
    _pg_conn,
)

load_dotenv(find_dotenv())

logger = logging.getLogger("AuthService")

PASSWORD_TABLE = os.getenv("PG_MYAGENT_PASSWORD_TABLE", "myagent_password_mgmt")
DEFAULT_ROLE = os.getenv("AUTH_DEFAULT_ROLE", "guest")

_user_svc = UserService()


# ================= 账号密码注册/登录/改密码 =================
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,30}$")
_PASSWORD_MIN_LEN = 6


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("用户名需 3-30 位，仅支持字母、数字、下划线")
    return username


def _validate_password(password: str) -> str:
    if not password or len(password) < _PASSWORD_MIN_LEN:
        raise ValueError(f"密码至少 {_PASSWORD_MIN_LEN} 位")
    return password


def register_password_user(username: str, password: str, display_name: str | None = None) -> dict:
    """账号密码注册：创建用户 + 绑定默认角色 + 写入密码哈希。

    account_id 使用用户名（唯一）。
    返回 sys_user 行。
    """
    username = _validate_username(username)
    password = _validate_password(password)

    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (username,))
        if cur.fetchone():
            raise ValueError("该用户名已被注册")

        cur.execute(
            f"INSERT INTO {USER_TABLE} (account_id, display_name, status, deleted) "
            f"VALUES (%s,%s,'Enabled',false) RETURNING id",
            (username, display_name or username),
        )
        user_id = cur.fetchone()["id"]

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
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (username,))
        user_row = cur.fetchone()
        if not user_row:
            raise ValueError("用户名或密码错误")
        user_id = user_row["id"]

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


def get_user_id_by_uid(account_id: str) -> int | None:
    """按 account_id 取本地用户主键 id。"""
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (account_id,))
        row = cur.fetchone()
        return row[0] if row else None


def get_user_with_grant(uid: str) -> tuple[dict | None, list[str], list[str]]:
    """取用户 + 角色 codes + 权限 codes，供签发 token。"""
    user = _user_svc.get_by_uid(uid)
    roles = _user_svc.get_role_codes(uid)
    permissions = _user_svc.get_permissions(uid)
    return user, roles, permissions
