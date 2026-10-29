import os
import sys
from pathlib import Path
import psycopg
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECT_ROOT = os.getenv("WORK_PATH")
if PROJECT_ROOT and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ==================== PG配置 ====================
def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


# ==================== 表配置 ====================
KB_TABLE = os.getenv("PG_MYAGENT_KB_TABLE", "myagent_kb_chunks_mgmt")
CONV_TABLE = os.getenv("PG_MYAGENT_CONVERSATIONS_TABLE", "myagent_conversations_history_mgmt")
TASK_TABLE = os.getenv("PG_MYAGENT_UPLOAD_TASKS_TABLE", "myagent_upload_tasks_mgmt")
FAQ_TABLE = os.getenv("PG_MYAGENT_FAQ_TABLE", "myagent_faq_mgmt")
SCENE_TABLE = os.getenv("PG_MYAGENT_SCENE_TABLE", "myagent_scene_templates")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "4096"))

# ==================== 表定义 ====================
TABLES = [
    # ─── 1. 知识库切片表 ───
    f"""
    CREATE TABLE IF NOT EXISTS {KB_TABLE} (
        chunk_index           BIGINT PRIMARY KEY,
        child_content         TEXT NOT NULL,
        file_path             TEXT NOT NULL,
        file_name             TEXT NOT NULL,
        text_hash             TEXT NOT NULL,
        metadata              JSONB DEFAULT '{{}}',
        group_id              BIGINT DEFAULT 0,
        internal_id           TEXT DEFAULT '',
        parent_content        TEXT DEFAULT '',
        related_image_url     TEXT DEFAULT '',
        if_show               BOOLEAN DEFAULT False,
        embedding             vector({EMBEDDING_DIM}),
        embedding_dim         INT DEFAULT {EMBEDDING_DIM},
        content_tsv           TSVECTOR DEFAULT NULL,
        upload_time           TIMESTAMPTZ DEFAULT NULL,
        update_time           TIMESTAMPTZ DEFAULT NULL,
        expire_time           TIMESTAMPTZ DEFAULT NULL,
        extra_info            TEXT DEFAULT '',
        UNIQUE (file_name, text_hash)
    )
    """,
    # ─── 2. 对话历史表 ───
    f"""
    CREATE TABLE IF NOT EXISTS {CONV_TABLE} (
        id            BIGSERIAL PRIMARY KEY,
        uid           TEXT NOT NULL,
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
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ─── 3. 知识库上传任务状态表 ───
    f"""
    CREATE TABLE IF NOT EXISTS {TASK_TABLE} (
        task_id         TEXT PRIMARY KEY,
        uid             TEXT NOT NULL DEFAULT 'anonymous',
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
    # ─── 4. FAQ 快速查询表 ───
    f"""
    CREATE TABLE IF NOT EXISTS {FAQ_TABLE} (
        id            BIGSERIAL PRIMARY KEY,
        question      TEXT NOT NULL,
        answer        TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    # ─── 5. 场景模板表（正文+追问模板，供 LLM 场景判定与模板注入用）───
    f"""
    CREATE TABLE IF NOT EXISTS {SCENE_TABLE} (
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


# ==================== 索引定义 ====================
# 索引名按目标表生成（idx_{表名}_{用途}），避免与历史同名索引冲突导致 IF NOT EXISTS 静默跳过。
def _idx_names(t):
    t = t.replace("-", "_")
    return {
        "embedding": f"idx_{t}_embedding",
        "content_tsv": f"idx_{t}_content_tsv",
        "file_hash": f"idx_{t}_file_hash",
        "expire_time": f"idx_{t}_expire_time",
    }


def build_indexes():
    n = _idx_names(KB_TABLE)
    return [
        # 已弃用 ANN/IVFFlat：embedding 现为 4096 维，IVFFlat 需 ≤ 2000 维且检索改精确扫描，
        # 不再建向量索引（embedding 精确匹配走全表 <=> 扫描）。
        f"""
        CREATE INDEX IF NOT EXISTS {n['content_tsv']}
        ON {KB_TABLE} USING gin (content_tsv)
        """,
        f"""
        CREATE INDEX IF NOT EXISTS {n['file_hash']}
        ON {KB_TABLE} (file_name, text_hash)
        """,
        f"""
        CREATE INDEX IF NOT EXISTS {n['expire_time']}
        ON {KB_TABLE} (expire_time)
        """,
    ]


INDEXES = build_indexes() + [
    f"""
    CREATE INDEX IF NOT EXISTS idx_{CONV_TABLE}_session
    ON {CONV_TABLE} (session_id, turn_count)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{CONV_TABLE}_untagged
    ON {CONV_TABLE} (tags) WHERE role = 'user' AND tags IS NULL
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{TASK_TABLE}_uid_status
    ON {TASK_TABLE} (uid, status)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{FAQ_TABLE}_question
    ON {FAQ_TABLE} (question)
    """,
    f"""
    CREATE INDEX IF NOT EXISTS idx_{SCENE_TABLE}_scene_id
    ON {SCENE_TABLE} (scene_id)
    """,
]

# ==================== 场景模板 seed ====================
# ON CONFLICT (scene_id) DO NOTHING 保证幂等。
SCENE_SEED = [
    {
        "scene_id": "maternity",
        "name": "产假/陪产假",
        "trigger": "用户询问产假、陪产假、陪产假政策、生育津贴、产假申请等",
        "answer_template": "",
        "followup_prompt": "在原有追问基础上，额外生成 1 个关于【生育津贴】申领条件、材料或流程的追问，引导用户继续了解生育津贴。",
    },
]


# ==================== Idempotent column additions ====================
# CREATE TABLE IF NOT EXISTS 不会给已存在旧表补列；用 ADD COLUMN IF NOT EXISTS 幂等补齐。
# 每项: (表名, [(列名, 类型与默认, 注释), ...])
MIGRATIONS = [
    (KB_TABLE, [
        ("if_show", "BOOLEAN DEFAULT TRUE", "该文件图片是否可展示"),
    ]),
    (TASK_TABLE, [
        ("if_show", "BOOLEAN DEFAULT TRUE", "该文件图片是否可展示(排队任务持久化)"),
    ]),
]


def _add_missing_columns(conn):
    """对已存在的表补充缺失列（幂等）。"""
    for tbl, cols in MIGRATIONS:
        for col, ddl, comment in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {ddl}")
            print(f"  OK [{tbl}] 补列 {col} ({comment})")
    conn.commit()


def _seed_scenes(conn):
    """幂等写入场景模板 seed（按 scene_id 冲突跳过）。"""
    if not SCENE_SEED:
        return
    for s in SCENE_SEED:
        conn.execute(
            f"""INSERT INTO {SCENE_TABLE} (scene_id, name, trigger, answer_template, followup_prompt)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (scene_id) DO NOTHING""",
            (s["scene_id"], s["name"], s["trigger"], s["answer_template"], s["followup_prompt"]),
        )
    conn.commit()
    print(f"  OK 场景模板 seed：{len(SCENE_SEED)} 条（幂等，已存在则跳过）")


# ==================== 执行 ====================
def _execute(conn, statements, label):
    """逐条执行 DDL；单条失败不中断整体，便于定位出错语句。"""
    total = len(statements)
    for i, stmt in enumerate(statements, 1):
        first_line = stmt.strip().splitlines()[0].strip() if stmt.strip() else ""
        try:
            conn.execute(stmt)
            conn.commit()
            print(f"  OK {label} {i}/{total}: {first_line}")
        except Exception as e:
            conn.rollback()
            print(f"  ERROR {label} {i}/{total}: {first_line}\n      {e}")


def run():
    conn = _pg_conn()
    try:
        print(f"建表: {len(TABLES)} 张……")
        _execute(conn, TABLES, "表")
        print("补列迁移……")
        _add_missing_columns(conn)
        print(f"建索引: {len(INDEXES)} 个……")
        _execute(conn, INDEXES, "索引")
        print("场景模板 seed……")
        _seed_scenes(conn)
        print("\n全部初始化完成！")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
