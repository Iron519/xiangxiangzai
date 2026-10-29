import os
import sys

import psycopg
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

USER_TABLE = os.getenv("PG_MYAGENT_USER_TABLE", "myagent_user_mgmt")
PASSWORD_TABLE = os.getenv("PG_MYAGENT_PASSWORD_TABLE", "myagent_password_mgmt")


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
