import logging
import os

import bcrypt
import psycopg
from dotenv import load_dotenv, find_dotenv
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from sso_did.auth import (
    TokenData,
    create_access_token,
    get_current_user,
    revoke_access_token,
    security_scheme,
)

load_dotenv(find_dotenv())
logger = logging.getLogger("Auth")

USER_TABLE = os.getenv("PG_MYAGENT_USER_TABLE", "myagent_user_mgmt")


def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


# ================= 工具函数 =================
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
    if len(username) < 3 or len(username) > 30:
        raise ValueError("用户名长度需在 3-30 之间")
    return username


def _validate_password(password: str) -> str:
    if not password or len(password) < _PASSWORD_MIN_LEN:
        raise ValueError(f"密码至少 {_PASSWORD_MIN_LEN} 位")
    return password


# ================= 业务逻辑 =================

def register_user(username: str, password: str, apply_reason: str) -> dict:
    username = _validate_username(username)
    password = _validate_password(password)
    apply_reason = (apply_reason or "").strip()
    if not apply_reason:
        raise ValueError("请填写申请理由")

    uid = username
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {USER_TABLE} WHERE uid=%s AND deleted=false", (uid,))
        if cur.fetchone():
            raise ValueError("该用户名已被注册")
        cur.execute(
            f"INSERT INTO {USER_TABLE} (uid, username, password_hash, apply_reason, approved, remark, deleted) "
            f"VALUES (%s,%s,%s,%s,false,'',false) RETURNING id, uid, username, approved",
            (uid, username, _hash_password(password), apply_reason),
        )
        row = cur.fetchone()
        conn.commit()
        logger.info(f"[Auth] 注册申请提交成功 username={username}, uid={uid}")
        return dict(row)


def verify_login(username: str, password: str) -> dict:
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("用户名和密码不能为空")

    uid = username
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT * FROM {USER_TABLE} WHERE uid=%s AND deleted=false", (uid,))
        user = cur.fetchone()
        if not user or not _verify_password(password, user["password_hash"]):
            raise ValueError("用户名或密码错误")
        if not user["approved"]:
            raise ValueError("账号尚未批准，请等待管理员审核")
        logger.info(f"[Auth] 登录成功 uid={uid}")
        return dict(user)


def change_password(uid: str, old_password: str, new_password: str) -> None:
    new_password = _validate_password(new_password)
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT password_hash FROM {USER_TABLE} WHERE uid=%s AND deleted=false", (uid,))
        row = cur.fetchone()
        if not row:
            raise ValueError("用户不存在")
        if not _verify_password(old_password, row["password_hash"]):
            raise ValueError("旧密码错误")
        cur.execute(f"UPDATE {USER_TABLE} SET password_hash=%s, update_time=now() WHERE uid=%s",
                     (_hash_password(new_password), uid))
        conn.commit()
        logger.info(f"[Auth] 修改密码成功 uid={uid}")


def get_user_by_uid(uid: str) -> dict | None:
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT id, uid, username, apply_reason, approved, remark, deleted, create_time, update_time "
            f"FROM {USER_TABLE} WHERE uid=%s AND deleted=false", (uid,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_username(uid: str, new_username: str) -> dict:
    new_username = (new_username or "").strip()
    if len(new_username) < 1 or len(new_username) > 30:
        raise ValueError("昵称长度需在 1-30 之间")
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"UPDATE {USER_TABLE} SET username=%s, update_time=now() "
            f"WHERE uid=%s AND deleted=false RETURNING id, uid, username",
            (new_username, uid))
        row = cur.fetchone()
        if not row:
            raise ValueError("用户不存在")
        conn.commit()
        logger.info(f"[Auth] 更新昵称 uid={uid}, username={new_username}")
        return dict(row)


def list_users(page: int = 1, size: int = 20, approved_only: bool | None = None) -> dict:
    offset = (page - 1) * size
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        where = "deleted=false"
        params: list = []
        if approved_only is True:
            where += " AND approved=true"
        elif approved_only is False:
            where += " AND approved=false"
        cur.execute(f"SELECT COUNT(*) as total FROM {USER_TABLE} WHERE {where}", params)
        total = cur.fetchone()["total"]
        cur.execute(
            f"SELECT id, uid, username, apply_reason, approved, remark, create_time, update_time "
            f"FROM {USER_TABLE} WHERE {where} ORDER BY create_time DESC LIMIT %s OFFSET %s",
            params + [size, offset])
        rows = [dict(r) for r in cur.fetchall()]
    pages = (total + size - 1) // size
    return {"data": rows, "pageNo": page, "pageSize": size, "total": total, "pages": pages}


def approve_user(uid: str, remark: str = "") -> dict:
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"UPDATE {USER_TABLE} SET approved=true, remark=%s, update_time=now() "
            f"WHERE uid=%s AND deleted=false RETURNING id, uid, username, approved", (remark, uid))
        row = cur.fetchone()
        if not row:
            raise ValueError("用户不存在")
        conn.commit()
        logger.info(f"[Auth] 批准用户 uid={uid}")
        return dict(row)


def reject_user(uid: str, remark: str = "") -> dict:
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"UPDATE {USER_TABLE} SET approved=false, remark=%s, update_time=now() "
            f"WHERE uid=%s AND deleted=false RETURNING id, uid, username, approved", (remark, uid))
        row = cur.fetchone()
        if not row:
            raise ValueError("用户不存在")
        conn.commit()
        logger.info(f"[Auth] 拒绝用户 uid={uid}")
        return dict(row)


def delete_user(uid: str) -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE {USER_TABLE} SET deleted=true, update_time=now() WHERE uid=%s AND deleted=false", (uid,))
        if cur.rowcount == 0:
            raise ValueError("用户不存在")
        conn.commit()
        logger.info(f"[Auth] 删除用户 uid={uid}")


def update_user_remark(uid: str, remark: str) -> dict:
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"UPDATE {USER_TABLE} SET remark=%s, update_time=now() "
            f"WHERE uid=%s AND deleted=false RETURNING id, uid, username, approved, remark", (remark, uid))
        row = cur.fetchone()
        if not row:
            raise ValueError("用户不存在")
        conn.commit()
        return dict(row)


# ================= API 模型 =================
class Response(BaseModel):
    code: int = 200
    message: str = "success"
    data: object = None

    @classmethod
    def ok(cls, data=None):
        return cls(data=data)

    @classmethod
    def fail(cls, code=500, message="error"):
        return cls(code=code, message=message)


class RegisterReq(BaseModel):
    username: str
    password: str
    applyReason: str = Field(..., description="申请理由")


class LoginReq(BaseModel):
    username: str
    password: str


class ChangePasswordReq(BaseModel):
    oldPassword: str
    newPassword: str


class RemarkReq(BaseModel):
    remark: str = ""


# ================= 认证路由 =================
auth_router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


@auth_router.post("/register", response_model=Response)
async def register(body: RegisterReq):
    """注册 = 提交申请。不返回 token，需管理员批准后才能登录。"""
    try:
        user = register_user(body.username, body.password, body.applyReason)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    return Response.ok(data={
        "uid": user["uid"], "username": user["username"], "approved": user["approved"],
        "message": "注册申请已提交，请等待管理员批准后登录",
    })


@auth_router.post("/login", response_model=Response)
async def login(body: LoginReq):
    try:
        user = verify_login(body.username, body.password)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    token = create_access_token(uid=user["uid"], username=user["username"], approved=True)
    return Response.ok(data={"access_token": token, "token_type": "Bearer",
                             "uid": user["uid"], "username": user["username"]})


@auth_router.post("/change-password", response_model=Response)
async def change_pwd(body: ChangePasswordReq, current_user: TokenData = Depends(get_current_user)):
    try:
        change_password(current_user.uid, body.oldPassword, body.newPassword)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    return Response.ok(data={"changed": True})


@auth_router.post("/logout", response_model=Response)
async def logout(credentials=Depends(security_scheme)):
    if credentials:
        revoke_access_token(credentials.credentials)
    return Response.ok(data={"logout": True})


@auth_router.get("/me", response_model=Response)
async def get_me(current_user: TokenData = Depends(get_current_user)):
    user = get_user_by_uid(current_user.uid)
    if not user:
        return Response.fail(code=404, message="用户不存在")
    return Response.ok(data={
        "uid": user["uid"], "username": user["username"], "approved": user["approved"],
        "applyReason": user.get("apply_reason", ""), "remark": user.get("remark", ""),
        "isAdmin": current_user.is_admin,
    })


@auth_router.post("/profile", response_model=Response)
async def update_profile(body: dict, current_user: TokenData = Depends(get_current_user)):
    """更新个人昵称。"""
    try:
        user = update_username(current_user.uid, body.get("displayName", ""))
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    return Response.ok(data={"uid": user["uid"], "username": user["username"]})


# ================= 用户管理路由 =================
users_router = APIRouter(prefix="/api/v1/users", tags=["Users"])


@users_router.get("/page", response_model=Response)
async def users_page(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    approved: bool | None = Query(None),
    current_user: TokenData = Depends(get_current_user),
):
    if not current_user.is_admin:
        return Response.fail(code=403, message="仅管理员可查看用户列表")
    return Response.ok(data=list_users(page=page, size=size, approved_only=approved))


@users_router.post("/{uid}/approve", response_model=Response)
async def approve(uid: str, body: RemarkReq = RemarkReq(),
                  current_user: TokenData = Depends(get_current_user)):
    if not current_user.is_admin:
        return Response.fail(code=403, message="仅管理员可批准用户")
    try:
        return Response.ok(data=approve_user(uid, body.remark))
    except ValueError as e:
        return Response.fail(code=400, message=str(e))


@users_router.post("/{uid}/reject", response_model=Response)
async def reject(uid: str, body: RemarkReq = RemarkReq(),
                 current_user: TokenData = Depends(get_current_user)):
    if not current_user.is_admin:
        return Response.fail(code=403, message="仅管理员可拒绝用户")
    try:
        return Response.ok(data=reject_user(uid, body.remark))
    except ValueError as e:
        return Response.fail(code=400, message=str(e))


@users_router.put("/{uid}/remark", response_model=Response)
async def update_remark(uid: str, body: RemarkReq,
                        current_user: TokenData = Depends(get_current_user)):
    if not current_user.is_admin:
        return Response.fail(code=403, message="仅管理员可修改备注")
    try:
        return Response.ok(data=update_user_remark(uid, body.remark))
    except ValueError as e:
        return Response.fail(code=400, message=str(e))


@users_router.delete("/{uid}", response_model=Response)
async def delete(uid: str, current_user: TokenData = Depends(get_current_user)):
    if not current_user.is_admin:
        return Response.fail(code=403, message="仅管理员可删除用户")
    try:
        delete_user(uid)
    except ValueError as e:
        return Response.fail(code=400, message=str(e))
    return Response.ok(data={"deleted": True})
