import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
import httpx
import psycopg
from psycopg_pool import ConnectionPool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from concurrent_log_handler import ConcurrentRotatingFileHandler
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

PROJECT_ROOT = os.getenv("WORK_PATH", "")
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "file_tagging.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("FileTagging")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)

REQUIRED_ENV_VARS = ["ACCESSCODE", "BASE_URL", "FAST_MODEL", "TEXT_MODEL", "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD", "PG_MYAGENT_KB_TABLE"]
_config = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}

ADMIN_UID = os.getenv("ADMIN_UID", "admin").strip()

_fast_llm_client = None
def get_fast_llm_client():
    global _fast_llm_client
    if _fast_llm_client is None:
        _fast_llm_client = ChatOpenAI(
            model=_config.get("FAST_MODEL") or _config["TEXT_MODEL"],
            api_key=_config["ACCESSCODE"],
            base_url=_config["BASE_URL"],
            http_client=httpx.Client(
                verify=False,
                timeout=30,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            ),
            temperature=0.1,
            top_p=0.1,
        )
    return _fast_llm_client


_pg_pool = None
def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = ConnectionPool(
            conninfo=f"host={_config['PG_HOST']} port={int(_config['PG_PORT'])} "
                     f"dbname={_config['PG_DATABASE']} user={_config['PG_USER']} "
                     f"password={_config['PG_PASSWORD']}",
            min_size=1, max_size=5, open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
    return _pg_pool


def _load_file_chunks_text(uid: str, file_name: str, max_chars: int = 10000) -> str:
    table = _config["PG_MYAGENT_KB_TABLE"]
    try:
        with _get_pg_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT parent_content FROM {table} "
                    f"WHERE uid = %s AND file_name = %s AND parent_content IS NOT NULL "
                    f"ORDER BY chunk_index ASC LIMIT 5",
                    (uid, file_name),
                )
                rows = cur.fetchall()
                if not rows:
                    cur.execute(
                        f"SELECT child_content FROM {table} "
                        f"WHERE uid = %s AND file_name = %s AND child_content IS NOT NULL "
                        f"ORDER BY chunk_index ASC LIMIT 10",
                        (uid, file_name),
                    )
                    rows = cur.fetchall()
                    text = "\n".join(r[0] for r in rows if r[0])
                else:
                    text = "\n".join(r[0] for r in rows if r[0])
                return text[:max_chars] if text else ""
    except Exception as e:
        logger.error(f"[FileTagging] 加载文件内容失败 uid={uid} file={file_name}: {e}")
        return ""


async def generate_file_description(uid: str, file_name: str, original_name: str = "") -> dict | None:
    table = _config["PG_MYAGENT_KB_TABLE"]
    display_name = original_name or file_name

    chunks_text = _load_file_chunks_text(uid, file_name)
    if not chunks_text.strip():
        logger.warning(f"[FileTagging] 文件无内容可读，跳过打标: uid={uid} file={file_name}")
        return None

    prompt = f"""请分析以下文档内容，生成一段简短的文件描述和主题标签。

【文件名】
{display_name}

【文档内容（节选）】
{chunks_text}

请输出 JSON 格式，包含以下字段：
- reasoning: 说明你为什么打这些标签（1-2句话，解释从文档内容中提取了哪些关键主题）
- description: 用1-2句话总结文档的核心内容（不要超过80字）
- tags: 至少3个，最多20个主题标签，该关键词将用来进行与问题的匹配，直接影响检索结果，所以一定要覆盖完全，丰富且充分，宁滥勿缺

要求：
1. tags 数量必须 >= 3，不允许为空列表
2. 标签应覆盖文档的主要主题，不要只打一个方面
3. 标签用简短中文关键词，不要长句

只输出 JSON，不要任何其他内容。格式如下：
{{"reasoning": "...", "description": "...", "tags": ["标签1", "标签2", "标签3"]}}"""

    llm = get_fast_llm_client()
    raw = ""
    try:
        response = await asyncio.wait_for(
            llm.ainvoke(
                [SystemMessage(content="你是一个文档分析助手，只输出JSON。"),
                 HumanMessage(content=prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 800},
            ),
            timeout=30,
        )
        raw = response.content.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        result = json.loads(raw)

        reasoning = result.get("reasoning", "").strip()
        description = result.get("description", "").strip()
        tags = result.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)] if tags else []
        tags = [str(t).strip() for t in tags if str(t).strip()][:20]

        if len(tags) < 3:
            logger.warning(f"[FileTagging] LLM 返回标签不足3个({len(tags)}个): uid={uid} file={file_name} tags={tags}")

        if not description:
            logger.warning(f"[FileTagging] LLM 返回的 description 为空: uid={uid} file={file_name}")
            return None

        if not tags:
            logger.warning(f"[FileTagging] LLM 返回的 tags 为空，跳过写入: uid={uid} file={file_name}")
            return None

        tagged_at = datetime.now(timezone.utc).isoformat()

        with _get_pg_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT metadata FROM {table} WHERE uid = %s AND file_name = %s",
                    (uid, file_name),
                )
                row = cur.fetchone()
                existing = {}
                if row and row[0]:
                    existing = json.loads(row[0]) if isinstance(row[0], str) else row[0]

                metadata = {
                    "description": existing.get("description") or description,
                    "tags": tags,
                    "tag_source": "llm_auto",
                    "tagged_at": tagged_at,
                    "tag_reasoning": reasoning,
                }

                cur.execute(
                    f"UPDATE {table} SET metadata = %s::jsonb "
                    f"WHERE uid = %s AND file_name = %s",
                    (json.dumps(metadata), uid, file_name),
                )
            conn.commit()

        logger.info(f"[FileTagging] 打标完成: uid={uid} file={file_name} desc='{description[:50]}' tags={tags}")
        return metadata

    except asyncio.TimeoutError:
        logger.warning(f"[FileTagging] LLM 超时(30s): uid={uid} file={file_name}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"[FileTagging] JSON 解析失败: uid={uid} file={file_name} raw={raw[:200]}: {e}")
        return None
    except Exception as e:
        logger.error(f"[FileTagging] 打标失败: uid={uid} file={file_name}: {e}")
        return None


def has_manual_description(uid: str, file_name: str) -> bool:
    """检查该文件是否已有用户手填的描述（tag_source=manual），有则跳过 LLM 打标。"""
    table = _config["PG_MYAGENT_KB_TABLE"]
    try:
        with _get_pg_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT metadata->'tag_source' FROM {table} "
                    f"WHERE uid = %s AND file_name = %s LIMIT 1",
                    (uid, file_name),
                )
                row = cur.fetchone()
                if row and row[0]:
                    source = row[0].strip('"') if isinstance(row[0], str) else str(row[0]).strip('"')
                    return source == "manual"
        return False
    except Exception:
        return False


def write_manual_description(uid: str, file_name: str, description: str) -> None:
    table = _config["PG_MYAGENT_KB_TABLE"]
    try:
        with _get_pg_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET metadata = jsonb_set("
                    f"COALESCE(metadata, '{{}}'::jsonb), "
                    f"'{{\"description\"}}', %s::jsonb) "
                    f"WHERE uid = %s AND file_name = %s",
                    (json.dumps(description.strip()), uid, file_name),
                )
            conn.commit()
        logger.info(f"[FileTagging] 用户手填描述已写入(仅描述，tags 交给 LLM 打标): uid={uid} file={file_name}")
    except Exception as e:
        logger.error(f"[FileTagging] 写入手填描述失败: uid={uid} file={file_name}: {e}")


def load_file_descriptions(uid: str, admin_uid: str = "") -> list[dict]:
    table = _config["PG_MYAGENT_KB_TABLE"]
    try:
        with _get_pg_pool().connection() as conn:
            with conn.cursor() as cur:
                uids = [uid] if not admin_uid or admin_uid == uid else [uid, admin_uid]
                placeholders = ", ".join(["%s"] * len(uids))
                cur.execute(
                    f"SELECT DISTINCT file_name, metadata "
                    f"FROM {table} "
                    f"WHERE uid IN ({placeholders}) "
                    f"AND metadata IS NOT NULL "
                    f"AND metadata != '{{}}'::jsonb "
                    f"AND metadata->'description' IS NOT NULL "
                    f"AND (expire_time IS NULL OR expire_time > now())",
                    uids,
                )
                rows = cur.fetchall()
                result = []
                seen = set()
                for file_name, metadata in rows:
                    if file_name in seen:
                        continue
                    seen.add(file_name)
                    if isinstance(metadata, str):
                        try:
                            metadata = json.loads(metadata)
                        except Exception:
                            metadata = {}
                    desc = metadata.get("description", "") if isinstance(metadata, dict) else ""
                    tags = metadata.get("tags", []) if isinstance(metadata, dict) else []
                    if desc:
                        result.append({
                            "file_name": file_name,
                            "description": desc,
                            "tags": tags if isinstance(tags, list) else [],
                        })
                return result
    except Exception as e:
        logger.error(f"[FileTagging] 加载文件描述失败 uid={uid}: {e}")
        return []
