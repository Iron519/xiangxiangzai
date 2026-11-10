import os
import sys
import bcrypt
import psycopg
from dotenv import load_dotenv, find_dotenv

# 确保能从项目根目录加载 .env
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(find_dotenv(usecwd=True) or os.path.join(_PROJECT_ROOT, ".env"))

USER_TABLE = os.getenv("PG_MYAGENT_USER_TABLE", "myagent_user")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
KB_TABLE = os.getenv("PG_MYAGENT_KB_TABLE", "myagent_kb_chunks")
CONV_TABLE = os.getenv("PG_MYAGENT_CONVERSATIONS_TABLE", "myagent_conversations_history")
SESSION_TABLE = os.getenv("PG_MYAGENT_SESSIONS_TABLE", "myagent_sessions")
TASK_TABLE = os.getenv("PG_MYAGENT_UPLOAD_TASKS_TABLE", "myagent_upload_tasks")
FAQ_TABLE = os.getenv("PG_MYAGENT_FAQ_TABLE", "myagent_faq")
SCENE_TABLE = os.getenv("PG_MYAGENT_SCENE_TABLE", "myagent_scene_templates")

# 旧表（已废弃的 _mgmt 后缀表）
OLD_TABLES = [
    "myagent_user_mgmt",
    "myagent_kb_chunks_mgmt",
    "myagent_conversations_history_mgmt",
    "myagent_upload_tasks_mgmt",
    "myagent_faq_mgmt",
]
# 当前所有表，重建时先 DROP
ALL_TABLES = [USER_TABLE, KB_TABLE, CONV_TABLE, SESSION_TABLE, TASK_TABLE, FAQ_TABLE, SCENE_TABLE]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))


def _conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


DDL_STATEMENTS = [
    # ─── 删除旧的 _mgmt 后缀表 ───
    *[f"DROP TABLE IF EXISTS {t} CASCADE" for t in OLD_TABLES],

    # ─── 删除当前所有表（重建）───
    *[f"DROP TABLE IF EXISTS {t} CASCADE" for t in ALL_TABLES],

    # ─── 用户表：uid 为5位数字编号，自增序列 ───
    "DROP SEQUENCE IF EXISTS user_uid_seq",
    "CREATE SEQUENCE user_uid_seq START 1",
    f"""
    CREATE TABLE {USER_TABLE} (
        id              bigserial PRIMARY KEY,
        uid             varchar(5)  NOT NULL UNIQUE DEFAULT lpad(nextval('user_uid_seq')::text, 5, '0'),
        username        varchar(30) NOT NULL,
        password_hash   varchar(200) NOT NULL,
        apply_reason    text         NOT NULL DEFAULT '',
        approved        boolean      NOT NULL DEFAULT false,
        remark          text         NOT NULL DEFAULT '',
        deleted         boolean      NOT NULL DEFAULT false,
        create_time     timestamp    NOT NULL DEFAULT now(),
        update_time     timestamp    NOT NULL DEFAULT now()
    )
    """,
    f"CREATE INDEX idx_{USER_TABLE}_uid ON {USER_TABLE} (uid) WHERE deleted = false",
    f"CREATE INDEX idx_{USER_TABLE}_approved ON {USER_TABLE} (approved) WHERE deleted = false",

    # ─── 知识库切片表：uid + original_name 贯穿 ───
    f"""
    CREATE TABLE {KB_TABLE} (
        chunk_index           BIGINT PRIMARY KEY,
        uid                   VARCHAR(5) NOT NULL DEFAULT '',
        original_name         TEXT NOT NULL DEFAULT '',
        child_content         TEXT NOT NULL,
        file_path             TEXT NOT NULL,
        file_name             TEXT NOT NULL,
        text_hash             TEXT NOT NULL,
        file_size             BIGINT DEFAULT 0,
        metadata              JSONB DEFAULT '{{}}',
        group_id              BIGINT DEFAULT 0,
        internal_id           TEXT DEFAULT '',
        parent_content        TEXT DEFAULT '',
        related_image_url     TEXT DEFAULT '',
        if_show               BOOLEAN DEFAULT TRUE,
        embedding             vector({EMBEDDING_DIM}),
        embedding_dim         INT DEFAULT {EMBEDDING_DIM},
        content_tsv           TSVECTOR DEFAULT NULL,
        upload_time           TIMESTAMPTZ DEFAULT NULL,
        update_time           TIMESTAMPTZ DEFAULT NULL,
        expire_time           TIMESTAMPTZ DEFAULT NULL,
        extra_info            TEXT DEFAULT '',
        UNIQUE (uid, file_name, text_hash)
    )
    """,
    f"CREATE INDEX idx_{KB_TABLE}_content_tsv ON {KB_TABLE} USING gin (content_tsv)",
    f"CREATE INDEX idx_{KB_TABLE}_uid ON {KB_TABLE} (uid)",
    f"CREATE INDEX idx_{KB_TABLE}_file_hash ON {KB_TABLE} (uid, file_name, text_hash)",
    f"CREATE INDEX idx_{KB_TABLE}_expire_time ON {KB_TABLE} (expire_time)",

    # ─── 会话管理表：一个用户最多10个session ───
    f"""
    CREATE TABLE {SESSION_TABLE} (
        id              BIGSERIAL PRIMARY KEY,
        uid             VARCHAR(5) NOT NULL,
        session_id      TEXT NOT NULL,
        title           TEXT NOT NULL DEFAULT '',
        last_message_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        turn_count      INT NOT NULL DEFAULT 0,
        deleted         BOOLEAN NOT NULL DEFAULT false,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (uid, session_id)
    )
    """,
    f"CREATE INDEX idx_{SESSION_TABLE}_uid_time ON {SESSION_TABLE} (uid, last_message_at DESC) WHERE deleted = false",

    # ─── 对话历史表 ───
    f"""
    CREATE TABLE {CONV_TABLE} (
        id            BIGSERIAL PRIMARY KEY,
        uid           VARCHAR(5) NOT NULL DEFAULT '',
        session_id    TEXT NOT NULL,
        turn_count    INT NOT NULL DEFAULT 0,
        role          TEXT NOT NULL,
        content       TEXT NOT NULL DEFAULT '',
        tags          TEXT DEFAULT NULL,
        emotion       TEXT DEFAULT NULL,
        sources       JSONB DEFAULT NULL,
        mode          TEXT DEFAULT NULL,
        duration_ms   INT DEFAULT NULL,
        completion_token   BIGINT DEFAULT 0,
        prompt_token   BIGINT DEFAULT 0,
        total_token   BIGINT DEFAULT 0,
        user_judgement TEXT DEFAULT NULL,
        judgement_detail TEXT DEFAULT NULL,
        related_questions JSONB DEFAULT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"CREATE INDEX idx_{CONV_TABLE}_uid_session ON {CONV_TABLE} (uid, session_id, turn_count)",
    f"CREATE INDEX idx_{CONV_TABLE}_untagged ON {CONV_TABLE} (tags) WHERE role = 'user' AND tags IS NULL",

    # ─── 上传任务表 ───
    f"""
    CREATE TABLE {TASK_TABLE} (
        task_id         TEXT PRIMARY KEY,
        uid             VARCHAR(5) NOT NULL DEFAULT '',
        original_name   TEXT NOT NULL,
        save_name       TEXT NOT NULL,
        save_path       TEXT NOT NULL,
        file_size       BIGINT DEFAULT 0,
        status          TEXT NOT NULL DEFAULT 'saving',
        father_chunks   INT DEFAULT 0,
        semantic_chunks INT DEFAULT 0,
        chunk_count     INT DEFAULT 0,
        inserted        INT DEFAULT 0,
        updated         INT DEFAULT 0,
        error           TEXT DEFAULT NULL,
        expire_time     TIMESTAMPTZ DEFAULT NULL,
        if_show         BOOLEAN DEFAULT TRUE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"CREATE INDEX idx_{TASK_TABLE}_uid_status ON {TASK_TABLE} (uid, status)",

    # ─── FAQ 表（全局共享）───
    f"""
    CREATE TABLE {FAQ_TABLE} (
        id            BIGSERIAL PRIMARY KEY,
        question      TEXT NOT NULL,
        answer        TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    f"CREATE INDEX idx_{FAQ_TABLE}_question ON {FAQ_TABLE} (question)",

    # ─── 场景模板表（全局共享）───
    f"""
    CREATE TABLE {SCENE_TABLE} (
        id               BIGSERIAL PRIMARY KEY,
        scene_id         TEXT NOT NULL UNIQUE,
        name             TEXT NOT NULL,
        trigger          TEXT NOT NULL DEFAULT '',
        answer_template  TEXT NOT NULL DEFAULT '',
        answer_mode      TEXT NOT NULL DEFAULT 'prompt',
        followup_prompt  TEXT NOT NULL DEFAULT '',
        followup_mode    TEXT NOT NULL DEFAULT 'prompt',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]

SCENE_SEED = [
    {"scene_id": "maternity", "name": "产假/陪产假", "trigger": "用户询问产假、陪产假、陪产假政策、生育津贴、产假申请等", "answer_template": "", "followup_prompt": "在原有追问基础上，额外生成 1 个关于【生育津贴】申领条件、材料或流程的追问，引导用户继续了解生育津贴。"},
]


def main():
    conn = _conn()
    conn.autocommit = True
    cur = conn.cursor()

    print("[DDL] 删除旧表 + 重建所有表...")
    for stmt in DDL_STATEMENTS:
        first_line = stmt.strip().splitlines()[0].strip()
        try:
            cur.execute(stmt)
            print(f"  OK: {first_line}")
        except Exception as e:
            print(f"  SKIP: {first_line} ({e})")

    # 管理员：uid=00000，固定不参与自增序列
    cur.execute(f"SELECT 1 FROM {USER_TABLE} WHERE uid='00000' AND deleted=false")
    if not cur.fetchone():
        pwd_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
        cur.execute(
            f"INSERT INTO {USER_TABLE} (uid, username, password_hash, apply_reason, approved, remark) "
            f"VALUES ('00000', 'admin', %s, 'system init', true, 'default admin')",
            (pwd_hash,),
        )
        print(f"[Admin] 创建管理员 uid=00000, 密码={ADMIN_PASSWORD}")
    else:
        print("[Admin] 管理员已存在，跳过")

    # 场景模板 seed
    for s in SCENE_SEED:
        cur.execute(
            f"INSERT INTO {SCENE_TABLE} (scene_id, name, trigger, answer_template, followup_prompt) "
            f"VALUES (%s, %s, %s, %s, %s) ON CONFLICT (scene_id) DO NOTHING",
            (s["scene_id"], s["name"], s["trigger"], s["answer_template"], s["followup_prompt"]),
        )
    print(f"[Seed] 场景模板 {len(SCENE_SEED)} 条")

    cur.close()
    conn.close()
    print("\n[Done] 全部表重建完成")


if __name__ == "__main__":
    main()
