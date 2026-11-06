import asyncio
import os
import re
import uuid
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg
from fastapi import APIRouter, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from calculate_logic.chunk_pipeline.config import PROJECT_ROOT, logger
from calculate_logic.chunk_pipeline.chunk import (
    chunk_markdown_text,
    build_semantic_chunks,
    resolve_child_image_urls,
)
from calculate_logic.chunk_pipeline.embedding import EmbeddingModel, store_semantic_chunks
from calculate_logic.chunk_pipeline.rewrite import rewrite_chunks_with_coref
from calculate_logic.fetch_knowledge.parse_file import DocumentParser, PARSE_RESULT_DIR
from sso_did.auth import TokenData, get_current_user


# ============================================================
# 调度入口：ChunkPipeline
# ============================================================
class ChunkPipeline:
    """旧版同步流水线，已废弃。异步处理走 _background_process。"""
    pass


# ================= 上传任务状态跟踪 =================

# 任务状态枚举
UPLOAD_STATUS_SAVING    = "saving"
UPLOAD_STATUS_PARSING   = "parsing"
UPLOAD_STATUS_PARSED    = "parsed"       # 解析完成：merged.md 已生成，文件已落到知识列表
UPLOAD_STATUS_CHUNKING  = "chunking"     # 注入阶段开始
UPLOAD_STATUS_EMBEDDING = "embedding"
UPLOAD_STATUS_INGESTING = "ingesting"
UPLOAD_STATUS_COMPLETED = "completed"     # 注入完成：切片+嵌入+入库全部完成
UPLOAD_STATUS_FAILED    = "failed"
UPLOAD_STATUS_CANCELLED  = "cancelled"

# 并发上限：同时处理的解析任务数（MinerU API 无明确并发限制，embedding batch≤20）
_upload_sem = asyncio.Semaphore(3)

# 进行中状态集合（所有活跃状态）
_UPLOAD_ACTIVE_STATUSES = (
    UPLOAD_STATUS_SAVING,
    UPLOAD_STATUS_PARSING,
    UPLOAD_STATUS_PARSED,
    UPLOAD_STATUS_CHUNKING,
    UPLOAD_STATUS_EMBEDDING,
    UPLOAD_STATUS_INGESTING,
)

# 解析阶段状态（此阶段用户可以点击停止按钮）
_PARSING_PHASE_STATUSES = (
    UPLOAD_STATUS_SAVING,
    UPLOAD_STATUS_PARSING,
)

# 注入阶段状态（此阶段前台无停止按钮，但用户可编辑全文/切片后重新嵌入）
_INJECTION_PHASE_STATUSES = (
    UPLOAD_STATUS_PARSED,
    UPLOAD_STATUS_CHUNKING,
    UPLOAD_STATUS_EMBEDDING,
    UPLOAD_STATUS_INGESTING,
)

UPLOAD_TASKS_TABLE = os.getenv("PG_MYAGENT_UPLOAD_TASKS_TABLE", "myagent_upload_tasks")
# 可写入任务表的字段白名单
_TASK_COLUMNS = {
    "task_id", "uid", "original_name", "save_name", "save_path", "file_size",
    "status", "father_chunks", "semantic_chunks", "chunk_count",
    "inserted", "updated", "error", "created_at", "updated_at", "expire_time",
}


def _task_conn():
    """上传任务状态表专用 PG 连接。"""
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


def _has_active_task(uid: str, statuses: tuple[str, ...] = ()) -> bool:
    """并发模式下不再限制单用户单任务，直接返回 False。"""
    return False


def _claim_next_queued_task(uid: str) -> dict[str, Any] | None:
    """并发模式下不再使用串行排队，返回 None。"""
    return None


def _create_task(task_id: str, uid: str, original_name: str, save_name: str,
                 save_path: str, file_size: int,
                 status: str = UPLOAD_STATUS_SAVING,
                 expire_time: Optional[str] = None,
                 if_show: bool = True) -> bool:
    """插入一条新任务记录，返回是否插入成功（失败说明并发冲突）。

    status 默认 saving；排队场景传入 queued。
    expire_time: 可选过期时间（ISO 字符串），存入任务表供排队任务恢复时取用。None=永久。
    if_show: 该文件图片是否可展示（文件级开关），存入任务表供排队任务恢复。
    """
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {UPLOAD_TASKS_TABLE} "
                    f"(task_id, uid, original_name, save_name, save_path, file_size, status, expire_time, if_show) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (task_id, uid, original_name, save_name, save_path, file_size, status,
                     _parse_timestamp(expire_time), if_show),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"[UploadTask] 创建任务记录失败: {e}")
        return False


async def recover_queued_tasks() -> None:
    """进程启动时清理上次崩溃遗留的中断任务。"""
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {UPLOAD_TASKS_TABLE} "
                    f"SET status = %s, error = %s, updated_at = %s "
                    f"WHERE status NOT IN (%s, %s, %s)",
                    (UPLOAD_STATUS_FAILED, "进程重启，任务中断",
                     datetime.now(timezone.utc).isoformat(),
                     UPLOAD_STATUS_COMPLETED, UPLOAD_STATUS_FAILED, UPLOAD_STATUS_CANCELLED),
                )
                cleaned = cur.rowcount
            conn.commit()
        if cleaned:
            logger.info(f"[UploadTask] 启动清理 {cleaned} 个中断任务（标记为 failed）")
    except Exception as e:
        logger.error(f"[UploadTask] 恢复任务失败: {e}")


def _set_task_status(task_id: str, status: str, **kwargs) -> None:
    """更新任务状态及附加字段。"""
    updates = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    for k, v in kwargs.items():
        if k in _TASK_COLUMNS:
            updates[k] = v
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    params = list(updates.values()) + [task_id]
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {UPLOAD_TASKS_TABLE} SET {set_clause} WHERE task_id = %s",
                    params,
                )
            conn.commit()
    except Exception as e:
        logger.error(f"[UploadTask] 更新任务状态失败 task={task_id} status={status}: {e}")


class TaskCancelled(Exception):
    """任务被用户主动中止时抛出，用于在 _background_process 阶段间检查点退出。"""


def _get_task_status(task_id: str) -> str | None:
    """读取单条任务的当前状态，用于后台协程的中止检查。"""
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT status FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s", (task_id,))
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"[UploadTask] 读取任务状态失败 task={task_id}: {e}")
        return None


def _check_cancelled(task_id: str) -> None:
    """阶段间检查点：若任务已被标记为 cancelled，抛 TaskCancelled 退出后台处理。
    不能打断正在跑的同步步骤（如一次 MinerU/VLM 调用），只在 await 点之间生效。
    """
    status = _get_task_status(task_id)
    if status == UPLOAD_STATUS_CANCELLED:
        raise TaskCancelled("用户已中止任务")


def _cleanup_task_artifacts(save_path: str, uid: str = "") -> dict:
    """清理任务相关文件：删除上传的原文件和解析产物目录。

    返回 {"file_removed": bool, "parse_removed": bool}。
    """
    result = {"file_removed": False, "parse_removed": False}
    # 1. 删除上传的原文件
    if save_path:
        try:
            p = Path(save_path)
            if p.is_file():
                p.unlink()
                result["file_removed"] = True
                logger.info(f"[Cleanup] 已删除上传文件: {save_path}")
        except Exception as e:
            logger.warning(f"[Cleanup] 删除上传文件失败 {save_path}: {e}")
    # 2. 删除解析产物目录 parse_result/{uid}/{file_stem}/
    if save_path:
        try:
            stem = Path(save_path).stem.replace(" ", "_")
            parse_dir = Path(PARSE_RESULT_DIR) / uid / stem if uid else Path(PARSE_RESULT_DIR) / stem
            if parse_dir.exists() and parse_dir.is_dir():
                shutil.rmtree(parse_dir)
                result["parse_removed"] = True
                logger.info(f"[Cleanup] 已删除解析产物: {parse_dir}")
        except Exception as e:
            logger.warning(f"[Cleanup] 删除解析产物失败: {e}")
    return result


def _list_tasks(uid: str | None = None) -> list[dict[str, Any]]:
    """列出任务（可按 uid 过滤），已完成的任务保留 5 分钟后自动清理。"""
    tasks: list[dict[str, Any]] = []
    expired_ids: list[str] = []
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                if uid:
                    cur.execute(
                        f"SELECT * FROM {UPLOAD_TASKS_TABLE} WHERE uid = %s ORDER BY created_at DESC",
                        (uid,),
                    )
                else:
                    cur.execute(f"SELECT * FROM {UPLOAD_TASKS_TABLE} ORDER BY created_at DESC")
                colnames = [desc[0] for desc in cur.description]
                for row in cur.fetchall():
                    record = dict(zip(colnames, row))
                    if record.get("status") == UPLOAD_STATUS_COMPLETED:
                        expired_ids.append(record["task_id"])
                        continue
                    tasks.append(record)
            # 清理过期已完成任务
            if expired_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM {UPLOAD_TASKS_TABLE} WHERE task_id = ANY(%s)",
                        (expired_ids,),
                    )
                conn.commit()
    except Exception as e:
        logger.error(f"[UploadTask] 查询任务列表失败: {e}")
    return tasks


def _serialize_task(record: dict[str, Any]) -> dict[str, Any]:
    """将任务记录序列化为前端可用的 dict。"""
    task_info = {
        "task_id":       record.get("task_id"),
        "uid":           record.get("uid"),
        "original_name": record.get("original_name"),
        "save_name":     record.get("save_name"),
        "file_size":     record.get("file_size", 0),
        "status":        record.get("status"),
        "created_at":    record["created_at"].isoformat() if record.get("created_at") else None,
        "updated_at":    record["updated_at"].isoformat() if record.get("updated_at") else None,
        "error":         record.get("error"),
    }
    if record.get("status") == UPLOAD_STATUS_COMPLETED:
        task_info["chunk_count"] = record.get("chunk_count", 0)
        task_info["inserted"]    = record.get("inserted", 0)
        task_info["updated"]     = record.get("updated", 0)
        task_info["father_chunk_count"] = record.get("father_chunks", 0)
    elif record.get("status") in (UPLOAD_STATUS_PARSED, UPLOAD_STATUS_CHUNKING, UPLOAD_STATUS_EMBEDDING, UPLOAD_STATUS_INGESTING):
        task_info["father_chunks"]   = record.get("father_chunks", 0)
        task_info["semantic_chunks"] = record.get("semantic_chunks", 0)
    return task_info


def _kb_conn():
    """知识库表专用 PG 连接。"""
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


async def _trigger_next_queued(uid: str) -> None:
    """并发模式下不再使用串行排队，空操作。"""
    pass


async def _background_process(task_id: str, save_path: str, original_name: str, uid: str = "anonymous",
                              expire_time: Optional[str] = None,
                              if_show: bool = True) -> None:
    """后台协程：解析 → [解析完成] → 切块 → 向量化 → 入库 → [注入完成]。

    两阶段进度：
    - 解析完成(parsed)：merged.md 已生成，文件落到知识列表中，用户可查看全文解析结果
    - 注入完成(completed)：切片+嵌入+入库全部完成，用户可查看全文切片结果

    expire_time: 可选过期时间（ISO 字符串），None 表示永久有效。透传给切块写入 PG。
    if_show: 该文件图片是否可展示（文件级开关），冗余到每个入库切块。
    """
    embedding_model = None
    try:
        async with _upload_sem:
            # ── 阶段1：解析 ──
            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_PARSING)
            logger.info(f"[Task {task_id}] 开始解析文件: {save_path}")
            parsed_text = await DocumentParser().parse_file(save_path, display_name=original_name, uid=uid)

            # 解析完成检查点：用户可能在解析期间点击了停止
            # cancel 接口已删文件和任务记录，此处检测到文件被删 → 直接退出
            if not os.path.exists(save_path):
                logger.info(f"[Task {task_id}] 文件已被删除（用户中止），退出处理")
                _cleanup_task_artifacts(save_path, uid)
                return
            _check_cancelled(task_id)

            # 标记"解析完成"——此时 merged.md 已落到 parse_result/{uid}/ 目录
            # 文件已落到知识列表中，用户可查看全文解析结果
            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_PARSED)
            logger.info(f"[Task {task_id}] 解析完成，merged.md 已生成")
            await asyncio.sleep(1)

            # ── 阶段2：注入（切片→向量化→入库）──
            # 此阶段前台无停止按钮，但用户可编辑全文/切片后重新嵌入
            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_CHUNKING)
            logger.info(f"[Task {task_id}] 开始生成父级分块")
            stat_info = os.stat(save_path)
            father_chunks = await asyncio.to_thread(
                chunk_markdown_text,
                text=parsed_text,
                file_path=save_path,
                upload_time=datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
                update_time=datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                expire_time=expire_time,
                if_show=if_show,
            )
            await asyncio.to_thread(
                _set_task_status, task_id, UPLOAD_STATUS_CHUNKING,
                **{"father_chunks": len(father_chunks)},
            )
            logger.info(f"[Task {task_id}] 父级分块完成，数量: {len(father_chunks)}")

            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_EMBEDDING)
            logger.info(f"[Task {task_id}] 开始向量化")
            embedding_model = EmbeddingModel()
            semantic_chunks = await build_semantic_chunks(father_chunks, embedder=embedding_model.embed)
            await asyncio.to_thread(resolve_child_image_urls, semantic_chunks, save_path, uid)
            await rewrite_chunks_with_coref(semantic_chunks)
            await asyncio.to_thread(
                _set_task_status, task_id, UPLOAD_STATUS_EMBEDDING,
                **{"semantic_chunks": len(semantic_chunks)},
            )
            logger.info(f"[Task {task_id}] 语义分块完成，数量: {len(semantic_chunks)}")

            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_INGESTING)
            logger.info(f"[Task {task_id}] 开始写入 PG表: {os.getenv('PG_MYAGENT_KB_TABLE')}")
            summary = await asyncio.to_thread(
                store_semantic_chunks,
                semantic_chunks,
                schema="public",
                table=os.getenv("PG_MYAGENT_KB_TABLE"),
                upsert_key=("uid", "file_name", "text_hash"),
                uid=uid,
                original_name=original_name,
            )

            await asyncio.to_thread(
                _set_task_status, task_id, UPLOAD_STATUS_COMPLETED,
                **{
                    "chunk_count": summary.get("chunk_count", len(semantic_chunks)),
                    "inserted": summary.get("inserted", 0),
                    "updated": summary.get("updated", 0),
                    "father_chunks": len(father_chunks),
                },
            )
            logger.info(f"[Task {task_id}] 注入完成！共 {summary.get('chunk_count', 0)} 个切片")
    except TaskCancelled:
        logger.info(f"[Task {task_id}] 任务已被中止: {original_name}")
        _cleanup_task_artifacts(save_path, uid)
        try:
            with _task_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s", (task_id,))
                conn.commit()
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"[Task {task_id}] 处理失败: {original_name}")
        await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_FAILED, **{"error": str(e)})
    finally:
        if embedding_model is not None:
            await embedding_model.aclose()


async def _background_rechunk(task_id: str, file_name: str, save_path: str,
                              original_name: str, uid: str = "anonymous", if_show: bool = True) -> None:
    """后台重切片：跳过解析，直接用 merged.md 全文 切块→向量化→入库。"""
    embedding_model = None
    try:
        async with _upload_sem:
            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_PARSING)
            merged_text = _read_merged_md(save_path, uid)
            if not merged_text.strip():
                raise RuntimeError("merged.md 为空，无法重新切片。请先在编辑页输入内容并保存。")
            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_PARSED,
                                    **{"original_name": original_name})

            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_CHUNKING)
            stat_info = os.stat(save_path)
            father_chunks = await asyncio.to_thread(
                chunk_markdown_text,
                text=merged_text,
                file_path=save_path,
                upload_time=datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
                update_time=datetime.now(timezone.utc).isoformat(),
                if_show=if_show,
            )
            await asyncio.to_thread(
                _set_task_status, task_id, UPLOAD_STATUS_CHUNKING,
                **{"father_chunks": len(father_chunks)},
            )

            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_EMBEDDING)
            embedding_model = EmbeddingModel()
            semantic_chunks = await build_semantic_chunks(father_chunks, embedder=embedding_model.embed)
            await asyncio.to_thread(resolve_child_image_urls, semantic_chunks, save_path, uid)
            await rewrite_chunks_with_coref(semantic_chunks)
            await asyncio.to_thread(
                _set_task_status, task_id, UPLOAD_STATUS_EMBEDDING,
                **{"semantic_chunks": len(semantic_chunks)},
            )

            await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_INGESTING)
            KB_TABLE = os.getenv("PG_MYAGENT_KB_TABLE", "myagent_kb_chunks_mgmt")
            try:
                with _kb_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"DELETE FROM {KB_TABLE} WHERE file_name = %s AND uid = %s", (file_name, uid))
                    conn.commit()
            except Exception as e:
                logger.warning(f"[Rechunk] 删除旧切片失败 {file_name}: {e}")
            summary = await asyncio.to_thread(
                store_semantic_chunks,
                semantic_chunks,
                schema="public",
                table=os.getenv("PG_MYAGENT_KB_TABLE"),
                upsert_key=("uid", "file_name", "text_hash"),
                uid=uid,
                original_name=original_name,
            )
            await asyncio.to_thread(
                _set_task_status, task_id, UPLOAD_STATUS_COMPLETED,
                **{
                    "chunk_count": summary.get("chunk_count", len(semantic_chunks)),
                    "inserted": summary.get("inserted", 0),
                    "updated": summary.get("updated", 0),
                    "father_chunks": len(father_chunks),
                    "original_name": original_name,
                },
            )
            logger.info(f"[Rechunk] 重切片完成 {original_name}: {summary.get('chunk_count', 0)} 切片")
    except TaskCancelled:
        logger.info(f"[Rechunk] 任务 {task_id} 被中止")
    except Exception as e:
        logger.exception(f"[Rechunk] 重切片失败: {original_name}")
        await asyncio.to_thread(_set_task_status, task_id, UPLOAD_STATUS_FAILED,
                                **{"error": str(e), "original_name": original_name})
    finally:
        if embedding_model is not None:
            await embedding_model.aclose()


# ================= 内容切片API =================
router = APIRouter()

UPLOAD_DIR = Path(PROJECT_ROOT) / "upload_kb"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".txt", ".md", ".csv", ".jpeg", ".jpg", ".png"}


@router.post("/chunk/upload")
async def upload_file(file: UploadFile = File(...), display_name: str = Form(None),
                      expire_time: str = Form(None), if_show: bool = Form(False),
                      overwrite: bool = Form(False),
                      current_user: TokenData = Depends(get_current_user)):
    """上传知识库文件，保存后立即返回 task_id，后台异步执行解析入库流程。

    文件存储路径：upload_kb/{uid}/{original_name}（重名则覆盖旧文件）。
    上传时若检测到重名且 overwrite=False，返回 409 提示用户确认覆盖。

    display_name: 可选，前端指定的展示文件名（不含路径）。不传则使用原始文件名。
    expire_time: 可选，文件过期时间（ISO 8601 字符串，如 "2026-12-31T23:59"）。
                 不传或为空则视为永久有效。检索时自动过滤已过期文件。
    if_show: 该文件内含的图片是否允许在检索结果/聊天中展示。False 时不展示图片，文字仍正常检索。
    overwrite: 若 upload_kb/{uid}/ 下已有同名文件，是否直接覆盖。False 时返回 409 让前端确认。
    """
    uid = current_user.uid  # 已登录用户的真实账号
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return JSONResponse(
            status_code=400,
            content={"error": f"不支持的文件格式: {ext}，支持: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"},
        )
    original_name = file.filename or "unknown"
    # 使用 display_name（若有）作为展示名；不传则使用原始文件名
    display_name_final = display_name.strip() if display_name and display_name.strip() else original_name
    # 保证扩展名与原始文件一致
    if not display_name_final.lower().endswith(ext):
        display_name_final = display_name_final + ext

    # 文件存储到 upload_kb/{uid}/{display_name_final}（以文件上传名称作为文件名）
    uid_upload_dir = UPLOAD_DIR / uid
    uid_upload_dir.mkdir(parents=True, exist_ok=True)
    save_name = display_name_final  # 物理文件名 = 展示名（允许空格、波浪线等任意字符）
    save_path = str(uid_upload_dir / save_name)

    # 重名检测：若文件已存在且用户未确认覆盖，返回 409 提示
    if os.path.exists(save_path) and not overwrite:
        return JSONResponse(
            status_code=409,
            content={
                "error": f"文件 '{save_name}' 已存在，是否覆盖？",
                "duplicate": True,
                "file_name": save_name,
            },
        )

    # 解析并校验过期时间：空=永久有效；非法格式返回 400
    expire_iso: Optional[str] = None
    if expire_time and expire_time.strip():
        try:
            dt = datetime.fromisoformat(expire_time.strip())
            if dt.tzinfo is None:
                dt = dt.astimezone()
            expire_iso = dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": f"expire_time 格式非法: {expire_time}，需为 ISO 8601（如 2026-12-31T23:59）"},
            )

    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)
    logger.info(f"文件已保存: {save_path} ({len(content)} bytes) expire_time={expire_iso}")

    # 基于 PG 创建任务记录（跨 worker 共享）
    task_id = uuid.uuid4().hex[:12]

    # 并发模式：不再排队，直接启动（信号量 _upload_sem 控制并发上限）
    init_status = UPLOAD_STATUS_SAVING
    if not _create_task(task_id, uid, display_name_final, save_name, save_path, len(content),
                        status=init_status, expire_time=expire_iso, if_show=if_show):
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
                logger.info(f"[UploadTask] 创建任务失败，已回滚物理文件: {save_path}")
        except Exception as rollback_err:
            logger.warning(f"[UploadTask] 回滚物理文件失败: {save_path}: {rollback_err}")
        return JSONResponse(
            status_code=409,
            content={"error": "任务创建失败，请稍后重试", "task_id": task_id},
        )

    # 立即启动后台异步处理，信号量控制并发上限
    asyncio.create_task(_background_process(task_id, save_path, original_name, uid=uid,
                                            expire_time=expire_iso, if_show=if_show))

    return JSONResponse(content={
        "success": True,
        "task_id": task_id,
        "queued": False,
        "original_name": original_name,
        "message": "文件已保存，正在后台解析入库...",
    })


# ================= 知识库文件列表 API =================

KB_TABLE = os.getenv("PG_MYAGENT_KB_TABLE", "")


@router.get("/kb/files")
async def kb_files(current_user: TokenData = Depends(get_current_user)):
    """返回知识库中所有文件的聚合信息列表。

    包含两类文件：
    - ingested：已入库到 {KB_TABLE} 的文件（按 file_name 聚合）；
    - orphan：上传失败或未入库、但在 data/upload_kb/ 留有物理文件、且无任务记录的孤儿文件。
      这类文件前端可见、可删除（删除时一并清掉磁盘文件），避免“上传失败后既不可见又删不掉”。
    """
    if not KB_TABLE:
        return JSONResponse(status_code=500, content={"error": "KB 表名未配置"})
    uid = current_user.uid
    is_admin = current_user.is_admin
    try:
        with _kb_conn() as conn:
            with conn.cursor() as cur:
                if is_admin:
                    cur.execute(f"""
                        SELECT
                            k.file_name,
                            COALESCE(k.original_name, t.original_name, k.file_name) AS original_name,
                            k.file_path,
                            COUNT(*)                                          AS total_chunks,
                            COUNT(DISTINCT k.group_id)                        AS parent_chunks,
                            MIN(k.upload_time)                                AS upload_time,
                            MAX(k.update_time)                                AS last_update
                        FROM {KB_TABLE} k
                        LEFT JOIN {UPLOAD_TASKS_TABLE} t ON t.save_name = k.file_name
                        GROUP BY k.file_name, k.file_path, k.original_name, t.original_name
                        ORDER BY MIN(k.upload_time) DESC
                    """)
                else:
                    cur.execute(f"""
                        SELECT
                            k.file_name,
                            COALESCE(k.original_name, t.original_name, k.file_name) AS original_name,
                            k.file_path,
                            COUNT(*)                                          AS total_chunks,
                            COUNT(DISTINCT k.group_id)                        AS parent_chunks,
                            MIN(k.upload_time)                                AS upload_time,
                            MAX(k.update_time)                                AS last_update
                        FROM {KB_TABLE} k
                        LEFT JOIN {UPLOAD_TASKS_TABLE} t ON t.save_name = k.file_name
                        WHERE k.uid = %s
                        GROUP BY k.file_name, k.file_path, k.original_name, t.original_name
                        ORDER BY MIN(k.upload_time) DESC
                    """, (uid,))
                rows = cur.fetchall()
                # 任务记录里的 save_name，用于区分"正在处理中"与"真孤儿"
                cur.execute(f"SELECT save_name, status, created_at FROM {UPLOAD_TASKS_TABLE}")
                task_rows = cur.fetchall()

        task_map = {r[0]: {"status": r[1], "upload_time": r[2]} for r in task_rows}

        def _disk_size(save_name: str, file_uid: str = "") -> int:
            """从 upload_kb/{uid}/ 读物理文件大小；读不到返回 0。"""
            try:
                if file_uid:
                    p = UPLOAD_DIR / file_uid / save_name
                else:
                    p = UPLOAD_DIR / save_name
                return os.path.getsize(p) if p.is_file() else 0
            except Exception:
                return 0

        def _disk_size_by_path(file_path: str) -> int:
            """直接从 file_path 列读取物理文件大小。"""
            try:
                p = Path(file_path)
                return os.path.getsize(p) if p.is_file() else 0
            except Exception:
                return 0

        files = [
            {
                "file_name":      row[0],
                "original_name":  row[1],
                "file_path":      row[2],
                "file_size":      _disk_size_by_path(row[2]) or _disk_size(row[0], row[2].split("/")[-2] if "/" in row[2] else uid),
                "total_chunks":   row[3],
                "parent_chunks":  row[4],
                "upload_time":    row[5].isoformat() if row[5] else "",
                "last_update":    row[6].isoformat() if row[6] else "",
                "status":         "ingested",
            }
            for row in rows
        ]

        # 扫描 upload_kb/{uid}/ 目录，找出孤儿物理文件
        orphan_files = []
        try:
            ingested_names = {f["file_name"] for f in files}
            now_ts = datetime.now().timestamp()
            FRESH_GRACE_SECS = 120
            # 从任务表取 save_name -> original_name 映射
            task_name_map = {r[0]: r[1] for r in task_rows if r[0] and r[1]} if task_rows else {}
            # admin 扫描所有 uid 子目录，普通用户只扫自己的
            scan_dirs = []
            if is_admin:
                for d in sorted(os.listdir(UPLOAD_DIR)):
                    full_d = UPLOAD_DIR / d
                    if full_d.is_dir() and not d.startswith("."):
                        scan_dirs.append((d, full_d))
            else:
                uid_dir = UPLOAD_DIR / uid
                if uid_dir.is_dir():
                    scan_dirs.append((uid, uid_dir))
            for dir_uid, scan_dir in scan_dirs:
                for name in sorted(os.listdir(scan_dir)):
                    if name.startswith(".") or name == ".gitkeep":
                        continue
                    full = str(scan_dir / name)
                    if name in ingested_names or name in task_map:
                        continue
                    mtime = os.path.getmtime(full)
                    if now_ts - mtime < FRESH_GRACE_SECS:
                        continue
                    orig = task_name_map.get(name, name)
                    orphan_files.append({
                        "file_name":      name,
                        "original_name":  orig,
                        "file_path":      full,
                        "file_size":      os.path.getsize(full),
                        "total_chunks":   0,
                        "parent_chunks":  0,
                        "upload_time":    datetime.fromtimestamp(mtime).isoformat(),
                        "last_update":    "",
                        "status":         "orphan",
                    })
        except Exception as e:
            logger.warning(f"[KBFiles] 扫描孤儿文件失败: {e}")

        files.extend(orphan_files)
        return JSONResponse(content={"success": True, "files": files, "total": len(files)})
    except Exception as e:
        logger.error(f"[KBFiles] 查询失败: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/kb/upload-status")
async def kb_upload_status(current_user: TokenData = Depends(get_current_user)):
    """返回当前用户的上传任务状态列表（从 PG 读取，多 worker 共享）。"""
    tasks = await asyncio.to_thread(_list_tasks, current_user.uid)
    serialized = [_serialize_task(t) for t in tasks]
    return JSONResponse(content={"success": True, "tasks": serialized, "total": len(serialized)})


@router.get("/kb/file/{file_name}/parents")
async def kb_file_parents(file_name: str, current_user: TokenData = Depends(get_current_user)):
    """返回指定文件的父块列表（internal_id='0' 的切片即为父块）。"""
    if not KB_TABLE:
        return JSONResponse(status_code=500, content={"error": "KB 表名未配置"})
    uid = current_user.uid
    is_admin = current_user.is_admin
    try:
        with _kb_conn() as conn:
            with conn.cursor() as cur:
                if is_admin:
                    cur.execute(f"""
                        SELECT
                            chunk_index,
                            group_id,
                            child_content AS content,
                            parent_content AS related_content,
                            upload_time
                        FROM {KB_TABLE}
                        WHERE file_name = %s
                          AND internal_id = '0'
                        ORDER BY chunk_index ASC
                    """, (file_name,))
                else:
                    cur.execute(f"""
                        SELECT
                            chunk_index,
                            group_id,
                            child_content AS content,
                            parent_content AS related_content,
                            upload_time
                        FROM {KB_TABLE}
                        WHERE file_name = %s
                          AND internal_id = '0'
                          AND uid = %s
                        ORDER BY chunk_index ASC
                    """, (file_name, uid))
                rows = cur.fetchall()

        parents = [
            {
                "chunk_index":    row[0],
                "group_id":       row[1],
                "content":        row[2],
                "related_content": row[3] or "",
                "upload_time":    row[4].isoformat() if row[4] else "",
            }
            for row in rows
        ]
        return JSONResponse(content={"success": True, "parents": parents, "total": len(parents)})
    except Exception as e:
        logger.error(f"[KBFileParents] 查询失败: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.delete("/kb/file/{file_name}")
async def kb_file_delete(file_name: str, current_user: TokenData = Depends(get_current_user)):
    """删除指定文件：删 kb_chunks PG 表切片 + 删 parse_result 目录 + 保留 upload_kb 文件（以备追查）。

    覆盖三类情形：
    - 已入库文件：删切片 + 删 parse_result 目录；upload_kb 文件保留；
    - 上传失败/未入库的孤儿文件：删磁盘文件（upload_kb 下的也删，因为未入库无需追查）；
    - 残留任务记录：一并清掉，避免任务表堆积。
    """
    if not KB_TABLE:
        return JSONResponse(status_code=500, content={"error": "KB 表名未配置"})
    uid = current_user.uid
    is_admin = current_user.is_admin
    deleted_chunks = 0
    deleted_tasks = 0
    parse_result_removed = False
    disk_removed = False
    file_uid = uid  # 文件所属 uid，用于定位 parse_result 目录
    try:
        with _kb_conn() as conn:
            with conn.cursor() as cur:
                # 先查出文件所属 uid（用于定位 parse_result 目录）
                if is_admin:
                    cur.execute(f"SELECT uid FROM {KB_TABLE} WHERE file_name = %s LIMIT 1", (file_name,))
                    row = cur.fetchone()
                    if row:
                        file_uid = row[0] or uid

                # 1. 删知识库切片（admin 可删任意文件，普通用户只能删自己的）
                if is_admin:
                    cur.execute(f"DELETE FROM {KB_TABLE} WHERE file_name = %s", (file_name,))
                else:
                    cur.execute(f"DELETE FROM {KB_TABLE} WHERE file_name = %s AND uid = %s", (file_name, uid))
                deleted_chunks = cur.rowcount
                # 2. 删上传任务记录（save_name 或 original_name 命中）
                cur.execute(
                    f"DELETE FROM {UPLOAD_TASKS_TABLE} WHERE save_name = %s OR original_name = %s",
                    (file_name, file_name),
                )
                deleted_tasks = cur.rowcount
            conn.commit()

        # 3. 删 parse_result/{uid}/{file_stem}/ 目录（同步删除解析中间结果）
        file_stem = Path(file_name).stem.replace(" ", "_")
        # 尝试文件所属 uid 的 parse_result 目录
        parse_dir = Path(PARSE_RESULT_DIR) / file_uid / file_stem
        if not parse_dir.exists():
            # 尝试当前用户的 parse_result 目录
            parse_dir = Path(PARSE_RESULT_DIR) / uid / file_stem
        if not parse_dir.exists() and is_admin:
            # admin 删除时搜索所有 uid 子目录
            parse_root = Path(PARSE_RESULT_DIR)
            if parse_root.is_dir():
                for d in parse_root.iterdir():
                    candidate = d / file_stem
                    if candidate.exists() and candidate.is_dir():
                        parse_dir = candidate
                        break
        # 兼容旧结构：parse_result/{file_stem}/
        if not parse_dir.exists():
            old_parse_dir = Path(PARSE_RESULT_DIR) / file_stem
            if old_parse_dir.exists() and old_parse_dir.is_dir():
                parse_dir = old_parse_dir
        if parse_dir.exists() and parse_dir.is_dir():
            try:
                shutil.rmtree(parse_dir)
                parse_result_removed = True
            except Exception as e:
                logger.warning(f"[KBFileDelete] 删除 parse_result 目录失败: {parse_dir}: {e}")

        # 4. upload_kb 文件保留（以备追查），但孤儿文件（未入库）直接删磁盘
        if deleted_chunks == 0:
            # 未入库的孤儿文件：尝试从 upload_kb/{uid}/ 或 upload_kb/ 删除
            for base in [UPLOAD_DIR / uid, UPLOAD_DIR / file_uid, UPLOAD_DIR]:
                target = (base / file_name).resolve()
                if str(target).startswith(str(UPLOAD_DIR.resolve())) and target.is_file():
                    try:
                        os.remove(target)
                        disk_removed = True
                        break
                    except Exception as e:
                        logger.warning(f"[KBFileDelete] 删除磁盘文件失败: {file_name}: {e}")

        logger.info(f"[KBFileDelete] 删除: {file_name} | 切片 {deleted_chunks} | 任务 {deleted_tasks} | parse_result {'已删' if parse_result_removed else '无/失败'} | upload_kb 保留")
        return JSONResponse(content={
            "success": True,
            "deleted_chunks": deleted_chunks,
            "deleted_tasks": deleted_tasks,
            "parse_result_removed": parse_result_removed,
            "upload_kb_retained": deleted_chunks > 0,
            "disk_removed": disk_removed,
            "file_name": file_name,
        })
    except Exception as e:
        logger.error(f"[KBFileDelete] 删除失败: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.delete("/kb/task/{task_id}")
async def kb_task_delete(task_id: str, current_user: TokenData = Depends(get_current_user)):
    """删除一条上传任务记录（主要用于清理 failed/cancelled 任务，让 Upload Status 区干净）。

    - 删任务记录；
    - 若该任务对应磁盘文件仍残留且未入库，一并删（防目录穿越，不误删已入库文件）；
    - 不动已入库的知识库切片（那用 DELETE /kb/file/{file_name}）；
    - 若任务正在运行中（parsing/chunking/...），不允许直接删，应先用 cancel 中止。
    """
    deleted_tasks = 0
    disk_removed = False
    original_name = ""
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT save_name, original_name, status, save_path, uid FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    return JSONResponse(status_code=404, content={"error": "任务不存在", "task_id": task_id})
                save_name, original_name, status, save_path, task_uid = row[0], row[1], row[2], row[3] or "", row[4] or ""
                # 正在运行中的任务不让直接删（会留下正在跑的孤儿协程）
                if status in _UPLOAD_ACTIVE_STATUSES:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "任务正在运行中，请先中止（cancel）再删除", "task_id": task_id, "status": status},
                    )
                cur.execute(f"DELETE FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s", (task_id,))
                deleted_tasks = cur.rowcount
            conn.commit()

        # 残留磁盘文件清理（仅当未入库时）
        if save_path:
            target = Path(save_path).resolve()
            if str(target).startswith(str(UPLOAD_DIR.resolve())) and target.is_file():
                with _kb_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT 1 FROM {KB_TABLE} WHERE file_name = %s", (save_name,))
                        in_kb = cur.fetchone() is not None
                if not in_kb:
                    try:
                        os.remove(target)
                        disk_removed = True
                    except Exception as e:
                        logger.warning(f"[KBTaskDelete] 删除磁盘文件失败: {save_name}: {e}")

        logger.info(f"[KBTaskDelete] 删除任务: {task_id} ({original_name}) | 任务 {deleted_tasks} | 磁盘 {'已删' if disk_removed else '无/已入库'}")
        return JSONResponse(content={
            "success": True,
            "deleted_tasks": deleted_tasks,
            "disk_removed": disk_removed,
            "task_id": task_id,
            "original_name": original_name,
        })
    except Exception as e:
        logger.error(f"[KBTaskDelete] 删除失败: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/kb/task/{task_id}/cancel")
async def kb_task_cancel(task_id: str, current_user: TokenData = Depends(get_current_user)):
    """中止一个上传任务，并立即清理已上传文件和解析产物。

    仅在解析阶段（saving/parsing）允许中止，进入注入阶段后不可中止。

    - saving/parsing：标记为 cancelled，立即删除 upload_kb 文件和 parse_result 产物，后台协程在检查点退出；
    - parsed/chunking/embedding/ingesting：注入阶段不可中止（前台无停止按钮），提示用户等待完成；
    - completed/failed/cancelled：不可中止，提示用删除接口清理。
    """
    try:
        with _task_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT status, save_path, uid FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    return JSONResponse(status_code=404, content={"error": "任务不存在", "task_id": task_id})
                status, save_path, task_uid = row[0], row[1] or "", row[2] or ""

                if status == UPLOAD_STATUS_CANCELLED:
                    cur.execute(f"DELETE FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s", (task_id,))
                    conn.commit()
                    cleanup = _cleanup_task_artifacts(save_path, task_uid)
                    logger.info(f"[KBTaskCancel] 已中止任务直接删除: {task_id} cleanup={cleanup}")
                    return JSONResponse(content={"success": True, "action": "deleted", "task_id": task_id, **cleanup})

                # 仅解析阶段可中止
                if status in _PARSING_PHASE_STATUSES:
                    cur.execute(
                        f"UPDATE {UPLOAD_TASKS_TABLE} SET status = %s, updated_at = %s WHERE task_id = %s",
                        (UPLOAD_STATUS_CANCELLED, datetime.now(timezone.utc).isoformat(), task_id),
                    )
                    cur.execute(f"DELETE FROM {UPLOAD_TASKS_TABLE} WHERE task_id = %s", (task_id,))
                    conn.commit()
                    logger.info(f"[KBTaskCancel] 任务已中止并删除记录: {task_id} (原状态 {status})")
                    cleanup = _cleanup_task_artifacts(save_path, task_uid)
                    logger.info(f"[KBTaskCancel] 已清理文件和解析产物: {task_id} cleanup={cleanup}")
                    return JSONResponse(content={
                        "success": True,
                        "action": "cancelled",
                        "task_id": task_id,
                        "note": "已中止并清理文件",
                        **cleanup,
                    })

                # 注入阶段不可中止
                if status in _INJECTION_PHASE_STATUSES:
                    return JSONResponse(
                        status_code=409,
                        content={"error": "文件已进入注入阶段（切片+嵌入），无法中止。请等待注入完成后编辑全文/切片。", "task_id": task_id, "status": status},
                    )

                # completed/failed：不可中止
                return JSONResponse(
                    status_code=409,
                    content={"error": f"任务状态为 {status}，无需中止；可用删除接口清理", "task_id": task_id, "status": status},
                )
    except Exception as e:
        logger.error(f"[KBTaskCancel] 中止失败: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# ================= 解析结果 查看/编辑/重新切片 =================
# 前端"知识库管理"里查看/编辑某文件整篇解析文本（merged.md），编辑后重新切片入库。


def _kb_file_row(file_name: str) -> dict[str, Any] | None:
    """从知识库表查出某 file_name 的一行(file_path/original_name/expire_time/if_show/uid)。"""
    if not KB_TABLE:
        return None
    try:
        with _kb_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT k.file_name, k.file_path, "
                    f"COALESCE(t.original_name, k.file_name) AS original_name, "
                    f"k.expire_time, k.if_show, k.uid, t.uid AS task_uid "
                    f"FROM {KB_TABLE} k "
                    f"LEFT JOIN {UPLOAD_TASKS_TABLE} t ON t.save_name = k.file_name "
                    f"WHERE k.file_name = %s LIMIT 1",
                    (file_name,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"file_name": row[0], "file_path": row[1], "original_name": row[2],
                "expire_time": row[3], "if_show": bool(row[4]),
                "uid": row[5] or row[6] or ""}
    except Exception as e:
        logger.warning(f"[ParsedText] 查询文件记录失败 {file_name}: {e}")
        return None


def _merged_md_path(file_path: str, uid: str = "") -> Path:
    """由入库源文件的物理路径，推算其 parse_result 产物目录下的 merged.md。
    约定：parse_result/{uid}/{file_stem}/merged.md
    """
    stem = Path(file_path).stem.replace(" ", "_")
    return Path(PARSE_RESULT_DIR) / uid / stem / "merged.md"


def _read_merged_md(file_path: str, uid: str = "") -> str:
    """读取源文件对应的 merged.md 全文；不存在/读失败返回 ""。"""
    p = _merged_md_path(file_path, uid)
    try:
        if not p.exists():
            return ""
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.warning(f"[ParsedText] 读取 merged.md 失败 {p}: {e}")
        return ""


class ParsedTextUpdate(BaseModel):
    """PUT /kb/file/{file_name}/parsed-text 请求体。"""
    parsed_text: str = Field(..., description="编辑后的整篇 Markdown 文本")


@router.get("/kb/file/{file_name}/parsed-text")
async def kb_file_parsed_text(file_name: str, current_user: TokenData = Depends(get_current_user)):
    """返回指定文件整篇解析结果（merged.md）文本，供前端查看/编辑。"""
    row = _kb_file_row(file_name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "文件不存在或未入库", "file_name": file_name})
    text = _read_merged_md(row["file_path"], row.get("uid", ""))
    return JSONResponse(content={
        "success": True,
        "file_name": row["file_name"],
        "original_name": row["original_name"],
        "parsed_text": text,
        "has_text": bool(text.strip()),
    })


@router.put("/kb/file/{file_name}/parsed-text")
async def kb_file_update_parsed_text(file_name: str, body: ParsedTextUpdate,
                                     current_user: TokenData = Depends(get_current_user)):
    """把编辑后的全文写回 merged.md（供之后重新切片入库）。"""
    row = _kb_file_row(file_name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "文件不存在或未入库", "file_name": file_name})
    p = _merged_md_path(row["file_path"], row.get("uid", ""))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body.parsed_text)
        logger.info(f"[ParsedText] 已更新 merged.md: {p} ({len(body.parsed_text)} chars)")
    except Exception as e:
        logger.error(f"[ParsedText] 写回 merged.md 失败 {p}: {e}")
        return JSONResponse(status_code=500, content={"error": f"保存失败: {e}"})
    return JSONResponse(content={"success": True, "file_name": file_name, "merged_path": str(p)})


@router.post("/kb/file/{file_name}/rechunk")
async def kb_file_rechunk(file_name: str, current_user: TokenData = Depends(get_current_user)):
    """用当前 merged.md 全文重新切片入库（跳过解析）。异步执行，返回 task_id。"""
    row = _kb_file_row(file_name)
    if not row:
        return JSONResponse(status_code=404, content={"error": "文件不存在或未入库", "file_name": file_name})
    uid = current_user.uid if current_user else "anonymous"

    task_id = uuid.uuid4().hex[:12]
    if not _create_task(task_id, uid, row["original_name"], row["file_name"],
                        row["file_path"], 0, status=UPLOAD_STATUS_SAVING):
        return JSONResponse(status_code=500, content={"error": "创建任务记录失败"})

    asyncio.create_task(_background_rechunk(task_id, row["file_name"], row["file_path"],
                                            row["original_name"], uid=uid,
                                            if_show=row.get("if_show", True)))
    return JSONResponse(content={"success": True, "task_id": task_id,
                                 "original_name": row["original_name"],
                                 "message": "已开始重新切片入库"})
