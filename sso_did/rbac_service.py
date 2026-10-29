import os
import sys
import logging
from datetime import datetime

import psycopg
from dotenv import load_dotenv, find_dotenv

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sso_did.rbac_models import (
    FullUserResponse, PageData, PermissionCreate, PermissionResponse,
    RoleCreate, UserCreate, UserResponse,
)

load_dotenv(find_dotenv())
logger = logging.getLogger("DIDRbac")

USER_TABLE = os.getenv("PG_HRSSC_USER_TABLE", "sys_user")
ROLE_TABLE = os.getenv("PG_HRSSC_ROLE_TABLE", "sys_role")
USER_ROLE_TABLE = os.getenv("PG_HRSSC_USER_ROLE_TABLE", "sys_user_role")
PERMISSION_TABLE = os.getenv("PG_HRSSC_PERMISSION_TABLE", "sys_permission")
ROLE_PERMISSION_TABLE = os.getenv("PG_HRSSC_ROLE_PERMISSION_TABLE", "sys_role_permission")


def _pg_conn():
    """复用项目其余模块的连接方式。"""
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


def _to_response(row: dict, role_ids: list[int] | None = None) -> UserResponse:
    return UserResponse(
        id=row["id"],
        uid=row["account_id"],
        displayName=row.get("display_name"),
        email=row.get("email"),
        mobile=row.get("mobile"),
        deptCode=row.get("dept_code"),
        department=row.get("department"),
        employeeType=row.get("employee_type"),
        status=row.get("status", "Enabled"),
        roleIds=role_ids or [],
    )


def _to_permission_response(row: dict) -> PermissionResponse:
    return PermissionResponse(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        action=row.get("action", "read"),
        type=row.get("type", "menu"),
        description=row.get("description"),
        status=row.get("status", "Enabled"),
    )


# ================= UserService =================
class UserService:
    """用户业务逻辑。"""

    def get_by_id(self, user_id: int) -> UserResponse | None:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT * FROM {USER_TABLE} WHERE id=%s AND deleted=false", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            role_ids = self._get_role_ids(cur, user_id)
            return _to_response(row, role_ids)

    def get_by_uid(self, uid: str) -> dict | None:
        """SSO 登录用：按 account_id 查原始用户（返回 dict，含 id/display_name）。"""
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT * FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (uid,))
            return cur.fetchone()

    def list_page(self, page: int, size: int, sort_field: str, sort_asc: bool, **filters) -> PageData:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            where, args = self._build_where(filters)
            cur.execute(f"SELECT COUNT(*) AS c FROM {USER_TABLE} WHERE deleted=false {where}", args)
            total = cur.fetchone()["c"]

            order_col = self._safe_order_col(sort_field, "create_time")
            direction = "ASC" if sort_asc else "DESC"
            offset = (page - 1) * size
            cur.execute(
                f"SELECT * FROM {USER_TABLE} WHERE deleted=false {where} "
                f"ORDER BY {order_col} {direction} LIMIT %s OFFSET %s",
                args + [size, offset],
            )
            rows = cur.fetchall()
            # 一次性查出本页所有用户的角色，避免逐行查询（N+1）
            user_ids = [r["id"] for r in rows]
            role_map: dict[int, list[int]] = {}
            if user_ids:
                cur.execute(
                    f"SELECT user_id, role_id FROM {USER_ROLE_TABLE} WHERE deleted=false AND user_id = ANY(%s)",
                    (user_ids,),
                )
                for rr in cur.fetchall():
                    role_map.setdefault(rr["user_id"], []).append(rr["role_id"])
            return PageData(
                data=[_to_response(r, role_map.get(r["id"], [])) for r in rows],
                pageNo=page, pageSize=size, total=total,
                pages=(total + size - 1) // size if total else 0,
            )

    def create(self, data: UserCreate) -> int:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s", (data.uid,))
            if cur.fetchone():
                raise ValueError(f"User '{data.uid}' already exists")
            cur.execute(
                f"INSERT INTO {USER_TABLE} (account_id, display_name, email, mobile, dept_code, department, employee_type, status) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (data.uid, data.displayName, data.email, data.mobile, data.deptCode,
                 data.department, data.employeeType, data.status),
            )
            user_id = cur.fetchone()["id"]
            self._assign_roles(cur, user_id, data.roleIds)
            conn.commit()
            return user_id

    def update(self, user_id: int, data: UserCreate) -> None:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {USER_TABLE} WHERE id=%s AND deleted=false", (user_id,))
            if not cur.fetchone():
                raise ValueError(f"User {user_id} not found")
            cur.execute(
                f"UPDATE {USER_TABLE} SET account_id=%s, display_name=%s, email=%s, mobile=%s, "
                f"dept_code=%s, department=%s, employee_type=%s, status=%s WHERE id=%s",
                (data.uid, data.displayName, data.email, data.mobile, data.deptCode,
                 data.department, data.employeeType, data.status, user_id),
            )
            self._assign_roles(cur, user_id, data.roleIds)
            conn.commit()

    def delete(self, user_id: int) -> None:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {USER_TABLE} WHERE id=%s AND deleted=false", (user_id,))
            if not cur.fetchone():
                raise ValueError(f"User {user_id} not found")
            cur.execute(f"UPDATE {USER_TABLE} SET deleted=true WHERE id=%s", (user_id,))
            self._assign_roles(cur, user_id, [])
            conn.commit()

    def get_roles(self, uid: str) -> list[dict]:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (uid,))
            row = cur.fetchone()
            if not row:
                return []
            cur.execute(
                f"SELECT r.id, r.code, r.name FROM {ROLE_TABLE} r "
                f"JOIN {USER_ROLE_TABLE} ur ON r.id=ur.role_id "
                f"WHERE ur.user_id=%s AND r.status='Enabled' AND r.deleted=false",
                (row["id"],),
            )
            return cur.fetchall()

    def get_role_codes(self, uid: str) -> list[str]:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (uid,))
            row = cur.fetchone()
            if not row:
                return []
            return self._get_role_codes(cur, row["id"])

    def get_permissions(self, uid: str) -> list[str]:
        """取用户的全部权限码（经角色-权限关联 + sys_permission 去重）。

        供 SSO 登录签发 JWT 时注入 permissions，以及 current-user 回显。
        """
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {USER_TABLE} WHERE account_id=%s AND deleted=false", (uid,))
            row = cur.fetchone()
            if not row:
                return []
            return self._get_permission_codes(cur, row["id"])

    def get_login_user(self, uid: str, roles: list[str], permissions: list[str] | None = None) -> FullUserResponse:
        """当前登录用户的完整画像（/users/current-user 返回）。

        返回完整字段（邮箱/部门/工号等），而非仅 uid/username/roles。
        roles/permissions 优先用传入值（来自 JWT，签发时已查库），未传则现查。
        """
        row = self.get_by_uid(uid)
        if not row:
            return FullUserResponse(uid=uid, username=uid, roles=roles or [],
                                    permissions=permissions or [])
        role_codes = roles if roles is not None else self.get_role_codes(uid)
        perm_codes = permissions if permissions is not None else self.get_permissions(uid)
        return FullUserResponse(
            uid=uid,
            username=row.get("display_name") or uid,
            roles=role_codes,
            permissions=perm_codes,
            email=row.get("email"),
            mobile=row.get("mobile"),
            deptCode=row.get("dept_code"),
            department=row.get("department"),
            employeeType=row.get("employee_type"),
            status=row.get("status", "Enabled"),
            userId=row.get("id"),
        )

    def get_full_user(self, uid: str, roles: list[str] | None = None,
                      permissions: list[str] | None = None) -> FullUserResponse:
        """合并 JWT 层 + sys_user 库表，返回完整用户画像。

        供生成链路等业务随时调用。roles/permissions 优先用传入值（来自 JWT，签发时
        已查库），未传则现查 sys_user_role / sys_role_permission。sys_user 行缺失时
        （理论上不该发生：auth_service 首登已建档）仍返回 uid + roles + permissions，其余字段为 None。

        注意：deptCode/department/mobile/employeeType 在首登时未写入（auth_service 仅落
        uid/display_name/email），需后续由管理后台补录或扩展 userinfo 映射才有值。
        """
        row = self.get_by_uid(uid)
        if not row:
            return FullUserResponse(uid=uid, username=uid, roles=roles or [],
                                    permissions=permissions or [])
        role_codes = roles if roles is not None else self.get_role_codes(uid)
        perm_codes = permissions if permissions is not None else self.get_permissions(uid)
        return FullUserResponse(
            uid=uid,
            username=row.get("display_name") or uid,
            roles=role_codes,
            permissions=perm_codes,
            email=row.get("email"),
            mobile=row.get("mobile"),
            deptCode=row.get("dept_code"),
            department=row.get("department"),
            employeeType=row.get("employee_type"),
            status=row.get("status", "Enabled"),
            userId=row.get("id"),
        )

    # ---------- 内部工具 ----------
    @staticmethod
    def _build_where(filters: dict) -> tuple[str, list]:
        clauses, args = [], []
        if filters.get("uid"):
            clauses.append("AND account_id ILIKE %s"); args.append(f"%{filters['uid']}%")
        if filters.get("display_name"):
            clauses.append("AND display_name ILIKE %s"); args.append(f"%{filters['display_name']}%")
        if filters.get("email"):
            clauses.append("AND email ILIKE %s"); args.append(f"%{filters['email']}%")
        if filters.get("department"):
            clauses.append("AND department=%s"); args.append(filters["department"])
        if filters.get("status"):
            clauses.append("AND status=%s"); args.append(filters["status"])
        return (" " + " ".join(clauses)) if clauses else "", args

    @staticmethod
    def _safe_order_col(sort_field: str | None, default: str) -> str:
        """排序字段白名单防注入。"""
        allowed = {"id", "account_id", "display_name", "email", "create_time", "update_time",
                   "code", "name", "status", "action", "type"}
        return sort_field if sort_field in allowed else default

    @staticmethod
    def _get_role_ids(cur, user_id: int) -> list[int]:
        cur.execute(f"SELECT role_id FROM {USER_ROLE_TABLE} WHERE user_id=%s", (user_id,))
        return [r["role_id"] for r in cur.fetchall()]

    @staticmethod
    def _get_role_codes(cur, user_id: int) -> list[str]:
        cur.execute(
            f"SELECT r.code FROM {ROLE_TABLE} r "
            f"JOIN {USER_ROLE_TABLE} ur ON r.id=ur.role_id "
            f"WHERE ur.user_id=%s AND r.status='Enabled' AND r.deleted=false",
            (user_id,),
        )
        return [r["code"] for r in cur.fetchall()]

    @staticmethod
    def _get_permission_codes(cur, user_id: int) -> list[str]:
        """取用户经角色关联的全部权限码（去重，仅 Enabled 权限/角色）。"""
        cur.execute(
            f"SELECT DISTINCT p.code FROM {PERMISSION_TABLE} p "
            f"JOIN {ROLE_PERMISSION_TABLE} rp ON p.id=rp.permission_id "
            f"JOIN {USER_ROLE_TABLE} ur ON rp.role_id=ur.role_id "
            f"WHERE ur.user_id=%s AND p.status='Enabled' AND p.deleted=false "
            f"AND rp.deleted=false AND ur.deleted=false",
            (user_id,),
        )
        return [r["code"] for r in cur.fetchall()]

    @staticmethod
    def _assign_roles(cur, user_id: int, role_ids: list[int]) -> None:
        cur.execute(f"DELETE FROM {USER_ROLE_TABLE} WHERE user_id=%s", (user_id,))
        for rid in role_ids:
            cur.execute(
                f"INSERT INTO {USER_ROLE_TABLE} (user_id, role_id, deleted) VALUES (%s,%s,false)",
                (user_id, rid),
            )


# ================= RoleService =================
class RoleService:
    """角色业务逻辑。"""

    def get_by_id(self, role_id: int) -> dict | None:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT * FROM {ROLE_TABLE} WHERE id=%s AND deleted=false", (role_id,))
            row = cur.fetchone()
            if not row:
                return None
            # 附带已分配的权限 id 列表，供角色编辑回显
            row["permissionIds"] = self._get_permission_ids(cur, role_id)
            return row

    def list_all(self, status: str | None = None) -> list[dict]:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if status:
                cur.execute(f"SELECT * FROM {ROLE_TABLE} WHERE deleted=false AND status=%s ORDER BY create_time DESC", (status,))
            else:
                cur.execute(f"SELECT * FROM {ROLE_TABLE} WHERE deleted=false ORDER BY create_time DESC")
            return cur.fetchall()

    def list_page(self, page: int, size: int, sort_field: str, sort_asc: bool, **filters) -> PageData:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            where, args = self._build_where(filters)
            cur.execute(f"SELECT COUNT(*) AS c FROM {ROLE_TABLE} WHERE deleted=false {where}", args)
            total = cur.fetchone()["c"]
            order_col = UserService._safe_order_col(sort_field, "create_time")
            direction = "ASC" if sort_asc else "DESC"
            offset = (page - 1) * size
            cur.execute(
                f"SELECT * FROM {ROLE_TABLE} WHERE deleted=false {where} "
                f"ORDER BY {order_col} {direction} LIMIT %s OFFSET %s",
                args + [size, offset],
            )
            rows = cur.fetchall()
            return PageData(
                data=rows, pageNo=page, pageSize=size, total=total,
                pages=(total + size - 1) // size if total else 0,
            )

    def create(self, data: RoleCreate) -> int:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {ROLE_TABLE} WHERE code=%s", (data.code,))
            if cur.fetchone():
                raise ValueError(f"Role '{data.code}' already exists")
            cur.execute(
                f"INSERT INTO {ROLE_TABLE} (code, name, description, status) VALUES (%s,%s,%s,%s) RETURNING id",
                (data.code, data.name, data.description, data.status),
            )
            role_id = cur.fetchone()["id"]
            conn.commit()
            return role_id

    def update(self, role_id: int, data: RoleCreate) -> None:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {ROLE_TABLE} WHERE id=%s AND deleted=false", (role_id,))
            if not cur.fetchone():
                raise ValueError(f"Role {role_id} not found")
            cur.execute(
                f"UPDATE {ROLE_TABLE} SET code=%s, name=%s, description=%s, status=%s WHERE id=%s",
                (data.code, data.name, data.description, data.status, role_id),
            )
            conn.commit()

    def delete(self, role_id: int) -> None:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {USER_ROLE_TABLE} WHERE role_id=%s AND deleted=false LIMIT 1", (role_id,))
            if cur.fetchone():
                raise ValueError("Role is in use, cannot delete")
            cur.execute(f"SELECT id FROM {ROLE_TABLE} WHERE id=%s AND deleted=false", (role_id,))
            if not cur.fetchone():
                raise ValueError(f"Role {role_id} not found")
            cur.execute(f"UPDATE {ROLE_TABLE} SET deleted=true WHERE id=%s", (role_id,))
            conn.commit()

    @staticmethod
    def _build_where(filters: dict) -> tuple[str, list]:
        clauses, args = [], []
        if filters.get("status"):
            clauses.append("AND status=%s"); args.append(filters["status"])
        if filters.get("name"):
            clauses.append("AND name ILIKE %s"); args.append(f"%{filters['name']}%")
        return (" " + " ".join(clauses)) if clauses else "", args

    @staticmethod
    def _get_permission_ids(cur, role_id: int) -> list[int]:
        """取角色已分配的权限 id（未软删）。"""
        cur.execute(
            f"SELECT permission_id FROM {ROLE_PERMISSION_TABLE} "
            f"WHERE role_id=%s AND deleted=false",
            (role_id,),
        )
        return [r["permission_id"] for r in cur.fetchall()]

    @staticmethod
    def _assign_permissions(cur, role_id: int, permission_ids: list[int]) -> None:
        """全量覆盖角色-权限关联（先清后插），对齐 _assign_roles 风格。"""
        cur.execute(f"DELETE FROM {ROLE_PERMISSION_TABLE} WHERE role_id=%s", (role_id,))
        for pid in permission_ids:
            cur.execute(
                f"INSERT INTO {ROLE_PERMISSION_TABLE} (role_id, permission_id, deleted) VALUES (%s,%s,false)",
                (role_id, pid),
            )

    def get_permission_ids(self, role_id: int) -> list[int]:
        """供角色-权限查询接口用。"""
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            return self._get_permission_ids(cur, role_id)

    def assign_permissions(self, role_id: int, permission_ids: list[int]) -> None:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {ROLE_TABLE} WHERE id=%s AND deleted=false", (role_id,))
            if not cur.fetchone():
                raise ValueError(f"Role {role_id} not found")
            self._assign_permissions(cur, role_id, permission_ids)
            conn.commit()


# ================= PermissionService =================
class PermissionService:
    """权限业务逻辑。"""

    def get_by_id(self, permission_id: int) -> dict | None:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT * FROM {PERMISSION_TABLE} WHERE id=%s AND deleted=false", (permission_id,))
            return cur.fetchone()

    def list_all(self, status: str | None = None) -> list[dict]:
        """全量查询，供角色编辑勾选用。"""
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            if status:
                cur.execute(
                    f"SELECT * FROM {PERMISSION_TABLE} WHERE deleted=false AND status=%s ORDER BY type, code",
                    (status,),
                )
            else:
                cur.execute(f"SELECT * FROM {PERMISSION_TABLE} WHERE deleted=false ORDER BY type, code")
            return cur.fetchall()

    def list_page(self, page: int, size: int, sort_field: str, sort_asc: bool, **filters) -> PageData:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            where, args = self._build_where(filters)
            cur.execute(f"SELECT COUNT(*) AS c FROM {PERMISSION_TABLE} WHERE deleted=false {where}", args)
            total = cur.fetchone()["c"]
            order_col = UserService._safe_order_col(sort_field, "create_time")
            direction = "ASC" if sort_asc else "DESC"
            offset = (page - 1) * size
            cur.execute(
                f"SELECT * FROM {PERMISSION_TABLE} WHERE deleted=false {where} "
                f"ORDER BY {order_col} {direction} LIMIT %s OFFSET %s",
                args + [size, offset],
            )
            rows = cur.fetchall()
            return PageData(
                data=[_to_permission_response(r) for r in rows],
                pageNo=page, pageSize=size, total=total,
                pages=(total + size - 1) // size if total else 0,
            )

    def create(self, data: PermissionCreate) -> int:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(f"SELECT id FROM {PERMISSION_TABLE} WHERE code=%s", (data.code,))
            if cur.fetchone():
                raise ValueError(f"Permission '{data.code}' already exists")
            cur.execute(
                f"INSERT INTO {PERMISSION_TABLE} (code, name, action, type, description, status) "
                f"VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (data.code, data.name, data.action, data.type, data.description, data.status),
            )
            pid = cur.fetchone()["id"]
            conn.commit()
            return pid

    def update(self, permission_id: int, data: PermissionCreate) -> None:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {PERMISSION_TABLE} WHERE id=%s AND deleted=false", (permission_id,))
            if not cur.fetchone():
                raise ValueError(f"Permission {permission_id} not found")
            cur.execute(
                f"UPDATE {PERMISSION_TABLE} SET code=%s, name=%s, action=%s, type=%s, description=%s, status=%s "
                f"WHERE id=%s",
                (data.code, data.name, data.action, data.type, data.description, data.status, permission_id),
            )
            conn.commit()

    def delete(self, permission_id: int) -> None:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT id FROM {PERMISSION_TABLE} WHERE id=%s AND deleted=false", (permission_id,))
            if not cur.fetchone():
                raise ValueError(f"Permission {permission_id} not found")
            # 软删权限本身 + 清理角色-权限关联
            cur.execute(f"UPDATE {PERMISSION_TABLE} SET deleted=true WHERE id=%s", (permission_id,))
            cur.execute(f"DELETE FROM {ROLE_PERMISSION_TABLE} WHERE permission_id=%s", (permission_id,))
            conn.commit()

    @staticmethod
    def _build_where(filters: dict) -> tuple[str, list]:
        clauses, args = [], []
        if filters.get("status"):
            clauses.append("AND status=%s"); args.append(filters["status"])
        if filters.get("name"):
            clauses.append("AND name ILIKE %s"); args.append(f"%{filters['name']}%")
        if filters.get("code"):
            clauses.append("AND code ILIKE %s"); args.append(f"%{filters['code']}%")
        if filters.get("type"):
            clauses.append("AND type=%s"); args.append(filters["type"])
        return (" " + " ".join(clauses)) if clauses else "", args
