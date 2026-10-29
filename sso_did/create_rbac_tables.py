import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

USER_TABLE = os.getenv("PG_MYAGENT_USER_TABLE", "myagent_user_mgmt")
ROLE_TABLE = os.getenv("PG_MYAGENT_ROLE_TABLE", "myagent_role_mgmt")
USER_ROLE_TABLE = os.getenv("PG_MYAGENT_USER_ROLE_TABLE", "myagent_user_role_mgmt")
PERMISSION_TABLE = os.getenv("PG_MYAGENT_PERMISSION_TABLE", "myagent_permission_mgmt")
ROLE_PERMISSION_TABLE = os.getenv("PG_MYAGENT_ROLE_PERMISSION_TABLE", "myagent_role_permission_mgmt")


def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


TABLES = [
    f"""
    CREATE TABLE IF NOT EXISTS {ROLE_TABLE} (
        id bigserial PRIMARY KEY,
        code varchar(100) NOT NULL UNIQUE,
        name varchar(200) NOT NULL,
        description text NULL,
        status varchar(20) NOT NULL DEFAULT 'Enabled',
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {USER_TABLE} (
        id bigserial PRIMARY KEY,
        account_id varchar(100) NOT NULL UNIQUE,
        display_name varchar(200) NULL,
        dept_code varchar(100) NULL,
        department varchar(200) NULL,
        employee_type varchar(50) NULL,
        work_city varchar(20) NULL,
        job_grade varchar(20) NULL,
        email varchar(200) NULL,
        mobile varchar(50) NULL,
        status varchar(20) NOT NULL DEFAULT 'Enabled',
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {USER_ROLE_TABLE} (
        id bigserial PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES {USER_TABLE}(id),
        role_id bigint NOT NULL REFERENCES {ROLE_TABLE}(id),
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PERMISSION_TABLE} (
        id bigserial PRIMARY KEY,
        code varchar(150) NOT NULL UNIQUE,
        name varchar(200) NOT NULL,
        action varchar(20) NOT NULL DEFAULT 'read',
        type varchar(20) NOT NULL DEFAULT 'menu',
        description text NULL,
        status varchar(20) NOT NULL DEFAULT 'Enabled',
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ROLE_PERMISSION_TABLE} (
        id bigserial PRIMARY KEY,
        role_id bigint NOT NULL REFERENCES {ROLE_TABLE}(id),
        permission_id bigint NOT NULL REFERENCES {PERMISSION_TABLE}(id),
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now(),
        UNIQUE(role_id, permission_id)
    )
    """,
]

INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_{USER_ROLE_TABLE}_user_id ON {USER_ROLE_TABLE}(user_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{USER_ROLE_TABLE}_role_id ON {USER_ROLE_TABLE}(role_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{ROLE_PERMISSION_TABLE}_role_id ON {ROLE_PERMISSION_TABLE}(role_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{ROLE_PERMISSION_TABLE}_permission_id ON {ROLE_PERMISSION_TABLE}(permission_id)",
]

# 权限明细初始化
SEED_PERMISSIONS = [
    # code, name, action, type(前端渲染约定), description
    # 用户管理
    ("system:user:list", "用户管理-查询", "read", "menu", "查看用户管理页"),
    ("system:user:save", "用户管理-保存", "create", "menu", "新增/修改用户"),
    ("system:user:delete", "用户管理-删除", "delete", "menu", "删除用户"),
    ("system:role:list", "角色管理-查询", "read", "menu", "查看角色管理页"),
    ("system:role:save", "角色管理-保存", "create", "menu", "新增/修改角色"),
    ("system:role:delete", "角色管理-删除", "delete", "menu", "删除角色"),
    ("system:permission:list", "权限管理-查询", "read", "menu", "查看权限管理页"),
    ("system:permission:save", "权限管理-保存", "create", "menu", "新增/修改权限"),
    ("system:permission:delete", "权限管理-删除", "delete", "menu", "删除权限"),
    # 知识库
    ("kb:read", "知识库-查看", "read", "api", "查看知识库文件/任务状态"),
    ("kb:upload", "知识库-上传", "create", "api", "上传文件/中止任务"),
    ("kb:delete", "知识库-删除", "delete", "api", "删除知识库文件/任务"),
    # 报表
    ("stats:read", "统计-查看", "read", "api", "查看数据洞察统计"),
    # FAQ
    ("faq:read", "FAQ-查看", "read", "api", "查看FAQ列表"),
    ("faq:save", "FAQ-保存", "create", "api", "新增/修改FAQ"),
    ("faq:delete", "FAQ-删除", "delete", "api", "删除FAQ"),
    # 附身
    ("possession:possess", "附身-确认", "possess", "button", "附身到其他用户"),
    ("possession:quit", "附身-撤离", "quit", "button", "取消附身"),
]

# 权限角色初始化
SEED_ROLES = [
    # code, name, description
    ("admin", "admin", "all permission"),
    ("operator", "operator", "read and operate anything except for user-related page"),
    ("guest", "guest", "just read"),
]

# 角色-权限 初始化
SEED_ROLE_PERMISSIONS = {
    "admin": [p[0] for p in SEED_PERMISSIONS],
    "operator": ["kb:read", "kb:upload", "stats:read", "faq:read", "faq:save"],
    "guest": ["kb:read", "stats:read", "faq:read"],
}

def run():
    conn = _pg_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for ddl in TABLES:
                    cur.execute(ddl)  # 建表
                print(f"[RBAC] {len(TABLES)} 张表已就绪")
                for ddl in INDEXES:
                    cur.execute(ddl)  # 创建索引
                print(f"[RBAC] {len(INDEXES)} 个索引已就绪")

                # ==== 插入 权限角色 初始数据 ====
                for code, name, desc in SEED_ROLES:
                    cur.execute( 
                        f"INSERT INTO {ROLE_TABLE} (code, name, description, status, deleted) VALUES (%s,%s,%s,'Enabled',false)",
                        (code, name, desc),
                    )
                    print(f"[RBAC] 初始权限角色 {code} 已插入")

                # ==== 插入 权限明细 初始数据 ====
                for code, name, action, ptype, pdesc in SEED_PERMISSIONS:
                    cur.execute(
                        f"INSERT INTO {PERMISSION_TABLE} (code, name, action, type, description, status, deleted) "
                        f"VALUES (%s,%s,%s,%s,%s,'Enabled',false)",
                        (code, name, action, ptype, pdesc),
                    )                    
                print(f"[RBAC] 初始权限明细 {len(SEED_PERMISSIONS)} 条已插入")

                # ==== 插入 权限角色-权限明细 初始数据 ====
                for role_code, perm_codes in SEED_ROLE_PERMISSIONS.items():

                    cur.execute(  # 取权限角色的id
                        f"SELECT r.id FROM {ROLE_TABLE} r WHERE r.code=%s AND r.deleted=false",
                        (role_code,),
                    )
                    role_row = cur.fetchone()
                    if not role_row:
                        continue
                    role_id = role_row[0]

                    for pcode in perm_codes:  # 遍历取权限明细的id
                        cur.execute(
                            f"SELECT p.id FROM {PERMISSION_TABLE} p WHERE p.code=%s AND p.deleted=false",
                            (pcode,),
                        )
                        p_row = cur.fetchone()
                        if not p_row:
                            continue
                        
                        cur.execute(
                            f"INSERT INTO {ROLE_PERMISSION_TABLE} (role_id, permission_id, deleted) VALUES (%s,%s,false)",
                            (role_id, p_row[0]),
                        )
                    print(f"[RBAC] 角色 {role_code} 默认权限已分配（{len(perm_codes)} 条）")
        print("[RBAC] 全部初始化完成！")
    except Exception as e:
        print(f"[RBAC] 初始化失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
