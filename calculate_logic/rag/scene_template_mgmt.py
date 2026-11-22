import os
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone

import psycopg
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv, find_dotenv
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from concurrent_log_handler import ConcurrentRotatingFileHandler

from sso_did.auth import TokenData, get_current_user

load_dotenv(find_dotenv())


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

# ================= 运行路径配置 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if PROJECT_ROOT and __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "scene_template_mgmt.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("SceneTemplateMgmt")
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
SCENE_TABLE = os.getenv("PG_MYAGENT_SCENE_TABLE", "myagent_scene_templates")

# 鉴权依赖：登录即可访问

# ================= Pydantic 模型 =================
class SceneTemplateCreate(BaseModel):
    """新增/修改场景模板入参。
    answer_type: 'llm'(默认，注入answer_template作为生成指令走RAG) 或 'faq'(直接返回answer_template，跳过LLM)。
    answer_template: answer_type='faq' 时为固定答案；answer_type='llm' 时为注入规则。
    followup_prompt: 可选，命中场景后追加的追问提示。"""
    scene_id: str
    name: str
    trigger: str = ""
    answer_type: str = "llm"
    answer_template: str = ""
    followup_prompt: str = ""


# ================= 数据访问 =================
_pg_pool = None
def _get_pool():
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = ConnectionPool(
            conninfo=f"host={os.getenv('PG_HOST')} port={int(os.getenv('PG_PORT', '5432'))} "
                     f"dbname={os.getenv('PG_DATABASE')} user={os.getenv('PG_USER')} "
                     f"password={os.getenv('PG_PASSWORD')}",
            min_size=1, max_size=3, open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
    return _pg_pool

def close_pool():
    global _pg_pool
    if _pg_pool is not None:
        try:
            _pg_pool.close()
        except Exception:
            pass
        _pg_pool = None


# ================= 路由 =================
router = APIRouter(prefix="/api/v1/scene-templates", tags=["SceneTemplate"])


@router.get("", response_model=Response)
async def list_scene_templates(current_user: TokenData = Depends(get_current_user)):
    """查询当前用户的全部场景模板（按 id 正序）。"""
    uid = current_user.uid
    with _get_pool().connection() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT id, scene_id, name, trigger, answer_type, answer_template, followup_prompt, created_at, updated_at "
            f"FROM {SCENE_TABLE} WHERE uid=%s ORDER BY id ASC",
            (uid,)
        )
        rows = cur.fetchall()
    return Response.ok(data=[
        {
            "id": r["id"],
            "scene_id": r["scene_id"],
            "name": r["name"],
            "trigger": r["trigger"],
            "answer_type": r["answer_type"],
            "answer_template": r["answer_template"],
            "followup_prompt": r["followup_prompt"],
            "created_at": r["created_at"].isoformat() if isinstance(r.get("created_at"), datetime) else r.get("created_at"),
            "updated_at": r["updated_at"].isoformat() if isinstance(r.get("updated_at"), datetime) else r.get("updated_at"),
        }
        for r in rows
    ])


@router.post("", response_model=Response)
async def create_scene_template(body: SceneTemplateCreate, current_user: TokenData = Depends(get_current_user)):
    """新增单条场景模板。上限 50 条。"""
    sid = body.scene_id.strip()
    name = body.name.strip()
    if not sid or not name:
        return Response.fail(code=400, message="scene_id 和 name 不能为空")
    answer_type = body.answer_type.strip() or "llm"
    if answer_type not in ("llm", "faq"):
        return Response.fail(code=400, message="answer_type 只能是 'llm' 或 'faq'")
    with _get_pool().connection() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        # 50 条上限校验（按用户计数）
        cur.execute(f"SELECT COUNT(*) AS cnt FROM {SCENE_TABLE} WHERE uid=%s", (current_user.uid,))
        cnt = cur.fetchone()["cnt"]
        if cnt >= 50:
            conn.commit()
            return Response.fail(code=400, message="场景数量已达上限（50条），请先删除不需要的场景")
        # scene_id 去重校验（同用户内）
        cur.execute(
            f"SELECT id FROM {SCENE_TABLE} WHERE uid=%s AND scene_id=%s", (current_user.uid, sid,),
        )
        if cur.fetchone():
            conn.commit()
            return Response.fail(code=400, message=f"场景 {sid} 已存在")
        cur.execute(
            f"INSERT INTO {SCENE_TABLE} (uid, scene_id, name, trigger, answer_type, answer_template, followup_prompt) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (current_user.uid, sid, name, body.trigger.strip(), answer_type, body.answer_template.strip(),
             body.followup_prompt.strip()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()

    logger.info(f"[SceneCreate] uid={current_user.uid} 新增场景 id={new_id} scene_id='{sid}' type={answer_type}")
    return Response.ok(data=new_id)


@router.put("/{scene_id}", response_model=Response)
async def update_scene_template(scene_id: str, body: SceneTemplateCreate, current_user: TokenData = Depends(get_current_user)):
    """修改单条场景模板（按 scene_id 定位）。"""
    sid = body.scene_id.strip()
    name = body.name.strip()
    if not sid or not name:
        return Response.fail(code=400, message="scene_id 和 name 不能为空")
    answer_type = body.answer_type.strip() or "llm"
    if answer_type not in ("llm", "faq"):
        return Response.fail(code=400, message="answer_type 只能是 'llm' 或 'faq'")
    with _get_pool().connection() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {SCENE_TABLE} WHERE uid=%s AND scene_id=%s", (current_user.uid, scene_id))
        if not cur.fetchone():
            conn.commit()
            return Response.fail(code=404, message=f"场景 {scene_id} 不存在")
        # scene_id 改了的话检查新 scene_id 是否冲突（同用户内）
        if sid != scene_id:
            cur.execute(f"SELECT id FROM {SCENE_TABLE} WHERE uid=%s AND scene_id=%s AND scene_id != %s", (current_user.uid, sid, scene_id))
            if cur.fetchone():
                conn.commit()
                return Response.fail(code=400, message=f"场景 {sid} 已存在")
        cur.execute(
            f"UPDATE {SCENE_TABLE} SET scene_id=%s, name=%s, trigger=%s, answer_type=%s, answer_template=%s, followup_prompt=%s, updated_at=NOW() "
            f"WHERE uid=%s AND scene_id=%s",
            (sid, name, body.trigger.strip(), answer_type, body.answer_template.strip(),
             body.followup_prompt.strip(), current_user.uid, scene_id),
        )
        conn.commit()

    logger.info(f"[SceneUpdate] uid={current_user.uid} 修改场景 scene_id='{scene_id}' type={answer_type}")
    return Response.ok(data=scene_id)


@router.delete("/{scene_id}", response_model=Response)
async def delete_scene_template(scene_id: str, current_user: TokenData = Depends(get_current_user)):
    """删除单条场景模板（删除即停用：判定表将不再包含该场景）。"""
    with _get_pool().connection() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {SCENE_TABLE} WHERE uid=%s AND scene_id=%s", (current_user.uid, scene_id))
        if not cur.fetchone():
            conn.commit()
            return Response.fail(code=404, message=f"场景 {scene_id} 不存在")
        cur.execute(f"DELETE FROM {SCENE_TABLE} WHERE uid=%s AND scene_id=%s", (current_user.uid, scene_id))
        conn.commit()

    logger.info(f"[SceneDelete] uid={current_user.uid} 删除场景 scene_id='{scene_id}'")
    return Response.ok(data=scene_id)