import os
import sys
import bcrypt
import psycopg
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

USER_TABLE = os.getenv("PG_MYAGENT_USER_TABLE", "myagent_user_mgmt")
ADMIN_UID = os.getenv("ADMIN_UID", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

DDL = f"""
CREATE TABLE IF NOT EXISTS {USER_TABLE} (
    id              bigserial PRIMARY KEY,
    uid             varchar(64)  NOT NULL UNIQUE,
    username        varchar(30)  NOT NULL,
    password_hash   varchar(200) NOT NULL,
    apply_reason    text         NOT NULL DEFAULT '',
    approved        boolean      NOT NULL DEFAULT false,
    remark          text         NOT NULL DEFAULT '',
    deleted         boolean      NOT NULL DEFAULT false,
    create_time     timestamp    NOT NULL DEFAULT now(),
    update_time     timestamp    NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_{USER_TABLE}_uid ON {USER_TABLE} (uid) WHERE deleted = false;
CREATE INDEX IF NOT EXISTS idx_{USER_TABLE}_approved ON {USER_TABLE} (approved) WHERE deleted = false;
"""


def main():
    conn = psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )
    conn.autocommit = True
    cur = conn.cursor()

    print("[DDL] Creating table...")
    cur.execute(DDL)
    print(f"[DDL] Table {USER_TABLE} ready")

    # Insert default admin if not exists
    cur.execute(f"SELECT 1 FROM {USER_TABLE} WHERE uid=%s AND deleted=false", (ADMIN_UID,))
    if cur.fetchone():
        print(f"[Admin] Admin user '{ADMIN_UID}' already exists, skipping")
    else:
        pwd_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
        cur.execute(
            f"INSERT INTO {USER_TABLE} (uid, username, password_hash, apply_reason, approved, remark) "
            f"VALUES (%s, %s, %s, 'system init', true, 'default admin')",
            (ADMIN_UID, ADMIN_UID, pwd_hash),
        )
        print(f"[Admin] Created admin user '{ADMIN_UID}' with password '{ADMIN_PASSWORD}'")
        print("[Admin] Please change the password immediately after first login!")

    cur.close()
    conn.close()
    print("[Done] All tables created successfully")


if __name__ == "__main__":
    main()
