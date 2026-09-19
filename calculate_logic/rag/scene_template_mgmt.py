import os
import sys
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
SCENE_TABLE = os.getenv("PG_HRSSC_SCENE_TABLE", "hrssc_scene_templates")

# 鉴权依赖：复用 FAQ 权限码（读/写/删同源，直接对齐 faq 板块）
_SCENE_READ_DEP = require_permission("faq:read")
_SCENE_SAVE_DEP = require_permission("faq:save")
_SCENE_DELETE_DEP = require_permission("faq:delete")


# ================= Pydantic 模型 =================
class SceneTemplateCreate(BaseModel):
    """新增/修改场景模板入参。answer_template / followup_prompt 均为选填，空串表示不注入。
    answer_mode: 'prompt'(默认，作为生成指令) 或 'literal'(一字不变输出原文)。"""
    scene_id: str
    name: str
    trigger: str = ""
    answer_template: str = ""
    answer_mode: str = "prompt"
    followup_prompt: str = ""
    followup_prompt: str = ""


# ================= 数据访问 =================
def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


# ================= 路由 =================
router = APIRouter(prefix="/api/v1/scene-templates", tags=["SceneTemplate"])


@router.get("", response_model=Response)
async def list_scene_templates(_: TokenData = Depends(_SCENE_READ_DEP)):
    """查询全部场景模板（按 id 正序）。"""
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT id, scene_id, name, trigger, answer_template, answer_mode, followup_prompt, created_at, updated_at "
            f"FROM {SCENE_TABLE} ORDER BY id ASC"
        )
        rows = cur.fetchall()
    return Response.ok(data=[
        {
            "id": r["id"],
            "scene_id": r["scene_id"],
            "name": r["name"],
            "trigger": r["trigger"],
            "answer_template": r["answer_template"],
            "answer_mode": r["answer_mode"],
            "followup_prompt": r["followup_prompt"],
            "created_at": r["created_at"].isoformat() if isinstance(r.get("created_at"), datetime) else r.get("created_at"),
            "updated_at": r["updated_at"].isoformat() if isinstance(r.get("updated_at"), datetime) else r.get("updated_at"),
        }
        for r in rows
    ])


@router.post("", response_model=Response)
async def create_scene_template(body: SceneTemplateCreate, current_user: TokenData = Depends(_SCENE_SAVE_DEP)):
    """新增单条场景模板。"""
    sid = body.scene_id.strip()
    name = body.name.strip()
    if not sid or not name:
        return Response.fail(code=400, message="scene_id 和 name 不能为空")
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            f"SELECT id FROM {SCENE_TABLE} WHERE scene_id=%s", (sid,),
        )
        if cur.fetchone():
            conn.commit()
            return Response.fail(code=400, message=f"场景 {sid} 已存在")
        cur.execute(
            f"INSERT INTO {SCENE_TABLE} (scene_id, name, trigger, answer_template, answer_mode, followup_prompt) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (sid, name, body.trigger.strip(), body.answer_template.strip(), body.answer_mode.strip() or 'prompt',
             body.followup_prompt.strip()),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()

    logger.info(f"[SceneCreate] uid={current_user.uid} 新增场景 id={new_id} scene_id='{sid}'")
    return Response.ok(data=new_id)


@router.put("/{scene_id}", response_model=Response)
async def update_scene_template(scene_id: str, body: SceneTemplateCreate, current_user: TokenData = Depends(_SCENE_SAVE_DEP)):
    """修改单条场景模板（按 scene_id 定位）。"""
    sid = body.scene_id.strip()
    name = body.name.strip()
    if not sid or not name:
        return Response.fail(code=400, message="scene_id 和 name 不能为空")
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {SCENE_TABLE} WHERE scene_id=%s", (scene_id,))
        if not cur.fetchone():
            conn.commit()
            return Response.fail(code=404, message=f"场景 {scene_id} 不存在")
        cur.execute(
            f"UPDATE {SCENE_TABLE} SET scene_id=%s, name=%s, trigger=%s, answer_template=%s, answer_mode=%s, followup_prompt=%s, updated_at=NOW() "
            f"WHERE scene_id=%s",
            (sid, name, body.trigger.strip(), body.answer_template.strip(), body.answer_mode.strip() or 'prompt',
             body.followup_prompt.strip(), scene_id),
        )
        conn.commit()

    logger.info(f"[SceneUpdate] uid={current_user.uid} 修改场景 scene_id='{scene_id}'")
    return Response.ok(data=scene_id)


@router.delete("/{scene_id}", response_model=Response)
async def delete_scene_template(scene_id: str, current_user: TokenData = Depends(_SCENE_DELETE_DEP)):
    """删除单条场景模板（删除即停用：判定表将不再包含该场景）。"""
    with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(f"SELECT id FROM {SCENE_TABLE} WHERE scene_id=%s", (scene_id,))
        if not cur.fetchone():
            conn.commit()
            return Response.fail(code=404, message=f"场景 {scene_id} 不存在")
        cur.execute(f"DELETE FROM {SCENE_TABLE} WHERE scene_id=%s", (scene_id,))
        conn.commit()

    logger.info(f"[SceneDelete] uid={current_user.uid} 删除场景 scene_id='{scene_id}'")
    return Response.ok(data=scene_id)