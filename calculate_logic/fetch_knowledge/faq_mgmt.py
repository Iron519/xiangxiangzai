import os
import logging
from pathlib import Path
from datetime import datetime, timezone

import psycopg
from dotenv import load_dotenv, find_dotenv
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from concurrent_log_handler import ConcurrentRotatingFileHandler

from sso_did.rbac_models import Response
from sso_did.auth import TokenData, require_permission

load_dotenv(find_dotenv())

# ================= 运行路径配置 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if PROJECT_ROOT and __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "faq_mgmt.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("FaqManagement")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)

# ================= 表配置 =================
FAQ_TABLE = os.getenv("PG_HRSSC_FAQ_TABLE", "hrssc_faq_mgmt")

# 鉴权依赖：按权限码守卫
_FAQ_READ_DEP = require_permission("faq:read")
_FAQ_SAVE_DEP = require_permission("faq:save")
_FAQ_DELETE_DEP = require_permission("faq:delete")


# ================= Pydantic 模型 =================
class FaqCreate(BaseModel):
    """新增/修改 FAQ 入参。"""
    question: str
    answer: str


# ================= 数据访问 =================
def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


def _invalidate_faq_cache():
    """失效 generate_answer.py 的 FAQ BM25 进程内缓存。

    CRUD 后调用，下次对话匹配时 _get_faq_bm25 检测到缓存为空会重新查库+重建索引。
    """
    try:
        from src import generate_answer
        generate_answer._faq_cache.update(questions=[], answers=[], bm25=None)
        logger.info("[FaqCache] 已失效 BM25 缓存，下次匹配自动重建")
    except Exception as e:
        logger.warning(f"[FaqCache] 失效缓存失败（不影响CRUD）: {e}")


def _renumber_ids(cur):
    """重排 hrssc_faq 的 id，使其从1开始连续（按当前 id 升序重新编号）。

    用临时映射表（旧id→新id）批量更新，避免直接 UPDATE 时主键冲突；
    随后重置序列到 MAX(id)+1，保证下次 INSERT 接续递增。
    """
    cur.execute(f"""
        WITH renumbered AS (
            SELECT id AS old_id, ROW_NUMBER() OVER (ORDER BY id) AS new_id
            FROM {FAQ_TABLE}
        )
        UPDATE {FAQ_TABLE} f SET id = r.new_id
        FROM renumbered r WHERE f.id = r.old_id AND f.id <> r.new_id
    """)
    cur.execute(
        f"SELECT setval(pg_get_serial_sequence('{FAQ_TABLE}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {FAQ_TABLE}), 0) + 1, false)"
    )


# ================= 路由 =================
router = APIRouter(prefix="/api/v1/faq", tags=["FAQ"])


@router.get("", response_model=Response)
async def list_faqs(_: TokenData = Depends(_FAQ_READ_DEP)):
    """查询全部 FAQ（按 id 正序，从1开始连续）。"""
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT id, question, answer, created_at, updated_at "
            f"FROM {FAQ_TABLE} ORDER BY id ASC"
        )
        rows = cur.fetchall()
    return Response.ok(data=[
        {
            "id": r["id"],
            "question": r["question"],
            "answer": r["answer"],
            "created_at": r["created_at"].isoformat() if isinstance(r.get("created_at"), datetime) else r.get("created_at"),
            "updated_at": r["updated_at"].isoformat() if isinstance(r.get("updated_at"), datetime) else r.get("updated_at"),
        }
        for r in rows
    ])


@router.post("", response_model=Response)
async def create_faq(body: FaqCreate, current_user: TokenData = Depends(_FAQ_SAVE_DEP)):
    """新增单条 FAQ。"""
    q = body.question.strip()
    a = body.answer.strip()
    if not q or not a:
        return Response.fail(code=400, message="question 和 answer 不能为空")
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"INSERT INTO {FAQ_TABLE} (question, answer) VALUES (%s, %s) RETURNING id",
            (q, a),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
    _invalidate_faq_cache()
    logger.info(f"[FaqCreate] uid={current_user.uid} 新增FAQ id={new_id} Q='{q[:50]}'")
    return Response.ok(data=new_id)


@router.put("/{faq_id}", response_model=Response)
async def update_faq(faq_id: int, body: FaqCreate, current_user: TokenData = Depends(_FAQ_SAVE_DEP)):
    """修改单条 FAQ。"""
    q = body.question.strip()
    a = body.answer.strip()
    if not q or not a:
        return Response.fail(code=400, message="question 和 answer 不能为空")
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {FAQ_TABLE} WHERE id=%s", (faq_id,))
        if not cur.fetchone():
            conn.commit()
            return Response.fail(code=404, message=f"FAQ {faq_id} 不存在")
        cur.execute(
            f"UPDATE {FAQ_TABLE} SET question=%s, answer=%s, updated_at=NOW() WHERE id=%s",
            (q, a, faq_id),
        )
        conn.commit()
    _invalidate_faq_cache()
    logger.info(f"[FaqUpdate] uid={current_user.uid} 修改FAQ id={faq_id} Q='{q[:50]}'")
    return Response.ok(data=faq_id)


@router.delete("/{faq_id}", response_model=Response)
async def delete_faq(faq_id: int, current_user: TokenData = Depends(_FAQ_DELETE_DEP)):
    """删除单条 FAQ，并重排剩余行 id 保持从1开始连续。"""
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {FAQ_TABLE} WHERE id=%s", (faq_id,))
        if not cur.fetchone():
            conn.commit()
            return Response.fail(code=404, message=f"FAQ {faq_id} 不存在")
        cur.execute(f"DELETE FROM {FAQ_TABLE} WHERE id=%s", (faq_id,))
        _renumber_ids(cur)  # 重排剩余行 id 为从1连续，并重置序列
        conn.commit()
    _invalidate_faq_cache()
    logger.info(f"[FaqDelete] uid={current_user.uid} 删除FAQ id={faq_id}，已重排 id 保持连续")
    return Response.ok(data=faq_id)
