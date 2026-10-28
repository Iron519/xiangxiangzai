import os
import sys

import psycopg
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

USER_TABLE = os.getenv("PG_HRSSC_USER_TABLE", "hrssc_user_mgmt")
IDENTITY_TABLE = os.getenv("PG_HRSSC_IDENTITY_TABLE", "hrssc_identity_mgmt")
VERIFY_CODE_TABLE = os.getenv("PG_HRSSC_VERIFY_CODE_TABLE", "hrssc_verify_code_mgmt")
PASSWORD_TABLE = os.getenv("PG_HRSSC_PASSWORD_TABLE", "hrssc_password_mgmt")


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
    CREATE TABLE IF NOT EXISTS {IDENTITY_TABLE} (
        id bigserial PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES {USER_TABLE}(id) ON DELETE CASCADE,
        provider varchar(20) NOT NULL,
        identifier varchar(200) NOT NULL,
        unionid varchar(200) NULL,
        status varchar(20) NOT NULL DEFAULT 'Enabled',
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now(),
        UNIQUE(provider, identifier)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {VERIFY_CODE_TABLE} (
        id bigserial PRIMARY KEY,
        phone varchar(50) NOT NULL,
        code varchar(10) NOT NULL,
        scene varchar(20) NOT NULL DEFAULT 'login',
        expires_at timestamp NOT NULL,
        attempts int NOT NULL DEFAULT 0,
        used bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PASSWORD_TABLE} (
        id bigserial PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES {USER_TABLE}(id) ON DELETE CASCADE,
        password_hash varchar(200) NOT NULL,
        status varchar(20) NOT NULL DEFAULT 'Enabled',
        deleted bool NOT NULL DEFAULT false,
        create_time timestamp NOT NULL DEFAULT now(),
        update_time timestamp NOT NULL DEFAULT now(),
        UNIQUE(user_id)
    )
    """,
]

INDEXES = [
    f"CREATE INDEX IF NOT EXISTS ix_{IDENTITY_TABLE}_user_id ON {IDENTITY_TABLE}(user_id)",
    f"CREATE INDEX IF NOT EXISTS ix_{IDENTITY_TABLE}_unionid ON {IDENTITY_TABLE}(unionid) WHERE deleted=false",
    f"CREATE INDEX IF NOT EXISTS ix_{VERIFY_CODE_TABLE}_phone_scene ON {VERIFY_CODE_TABLE}(phone, scene)",
    f"CREATE INDEX IF NOT EXISTS ix_{PASSWORD_TABLE}_user_id ON {PASSWORD_TABLE}(user_id)",
]


def run():
    conn = _pg_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                for ddl in TABLES:
                    cur.execute(ddl)
                print(f"[AUTH] {len(TABLES)} 张表已就绪")
                for ddl in INDEXES:
                    cur.execute(ddl)
                print(f"[AUTH] {len(INDEXES)} 个索引已就绪")
        print("[AUTH] 全部初始化完成！")
    except Exception as e:
        print(f"[AUTH] 初始化失败: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()