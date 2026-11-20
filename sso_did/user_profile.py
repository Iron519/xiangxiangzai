"""用户画像系统 —— 从历史对话提取用户画像，定时更新，生成回答时引用。

画像存储在 myagent_user.profile (JSONB) 列，结构：
{
  "basic_info": {"name": "...", "department": "...", "role": "...", "location": "..."},
  "personality": "...",
  "preferences": ["...", ...],
  "recent_topics": ["...", ...],
  "communication_style": "..."
}

定时更新周期由 .env USER_PROFILE_INTERVAL_HOURS 控制，默认 12 小时。
增量更新：只拉取自 profile_updated_at 之后的新对话，与老画像合并。
"""
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg_pool import ConnectionPool
from dotenv import load_dotenv, find_dotenv
from concurrent_log_handler import ConcurrentRotatingFileHandler
from fastapi import APIRouter, Depends
from sso_did.auth import TokenData, get_current_user

load_dotenv(find_dotenv())

PROJECT_ROOT = os.getenv("WORK_PATH", "")
if PROJECT_ROOT and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ================= 日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "user_profile.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("UserProfile")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch = logging.StreamHandler(); ch.setFormatter(formatter); logger.addHandler(ch)
    fh = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    fh.setFormatter(formatter); logger.addHandler(fh)
    logger.setLevel(logging.INFO)

# ================= 配置 =================
USER_TABLE = os.getenv("PG_MYAGENT_USER_TABLE", "myagent_user")
CONV_TABLE = os.getenv("PG_MYAGENT_CONVERSATIONS_TABLE", "myagent_conversations_history")
PROFILE_INTERVAL_HOURS = int(os.getenv("USER_PROFILE_INTERVAL_HOURS", "12"))
MIN_TURNS_FOR_PROFILE = 5
MAX_MESSAGES_PER_EXTRACTION = 100

# ================= PG 连接池 =================
_profile_pool: ConnectionPool | None = None

def _get_pool() -> ConnectionPool:
    global _profile_pool
    if _profile_pool is None:
        _profile_pool = ConnectionPool(
            conninfo=(
                f"host={os.getenv('PG_HOST')} port={int(os.getenv('PG_PORT', '5432'))} "
                f"dbname={os.getenv('PG_DATABASE')} user={os.getenv('PG_USER')} "
                f"password={os.getenv('PG_PASSWORD')}"
            ),
            min_size=1,
            max_size=3,
            open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        logger.debug("[UserProfile] PG 连接池已创建: min=1 max=3")
    return _profile_pool

def close_profile_pool() -> None:
    global _profile_pool
    if _profile_pool is not None:
        try:
            _profile_pool.close()
        except Exception as e:
            logger.warning(f"[UserProfile] 关闭连接池失败: {e}")
        _profile_pool = None

# ================= LLM 客户端 =================
_fast_llm = None

def _get_fast_llm():
    global _fast_llm
    if _fast_llm is None:
        from langchain_openai import ChatOpenAI
        _fast_llm = ChatOpenAI(
            model=os.getenv("FAST_MODEL") or os.getenv("TEXT_MODEL", ""),
            api_key=os.getenv("ACCESSCODE", ""),
            base_url=os.getenv("BASE_URL", ""),
            http_client=httpx.Client(verify=False, timeout=30, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)),
            http_async_client=httpx.AsyncClient(verify=False, timeout=30, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5)),
            temperature=0.1,
            top_p=0.1,
        )
    return _fast_llm

# ================= 核心函数 =================

def get_user_profile(uid: str) -> dict | None:
    """从 DB 读取用户画像 JSON，返回 dict 或 None。"""
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT profile FROM {USER_TABLE} WHERE uid=%s AND deleted=false",
                (uid,)
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return None
    except Exception as e:
        logger.error(f"[GetProfile] 读取画像失败 uid={uid}: {e}")
        return None


def _fetch_user_messages(uid: str, since: datetime | None = None) -> list[tuple[datetime, str]]:
    """拉取用户消息列表，返回 [(created_at, content), ...]。
    since 为 None 时拉全部（首次生成），否则只拉 since 之后的新消息。
    最多 MAX_MESSAGES_PER_EXTRACTION 条。
    """
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            if since:
                cur.execute(
                    f"SELECT created_at, content FROM {CONV_TABLE} "
                    f"WHERE uid=%s AND role='user' AND content != '' AND created_at > %s "
                    f"ORDER BY created_at ASC LIMIT %s",
                    (uid, since, MAX_MESSAGES_PER_EXTRACTION)
                )
            else:
                cur.execute(
                    f"SELECT created_at, content FROM {CONV_TABLE} "
                    f"WHERE uid=%s AND role='user' AND content != '' "
                    f"ORDER BY created_at DESC LIMIT %s",
                    (uid, MAX_MESSAGES_PER_EXTRACTION)
                )
                rows = cur.fetchall()
                rows.reverse()  # 倒序取最近 N 条，再反转为时间正序
                return [(r[0], r[1]) for r in rows]
            return [(r[0], r[1]) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"[FetchMessages] 拉取用户消息失败 uid={uid}: {e}")
        return []


def _get_profile_updated_at(uid: str) -> datetime | None:
    """获取用户画像的最后更新时间。"""
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT profile_updated_at FROM {USER_TABLE} WHERE uid=%s",
                (uid,)
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _count_user_messages(uid: str) -> int:
    """统计用户消息总数。"""
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {CONV_TABLE} WHERE uid=%s AND role='user' AND content != ''",
                (uid,)
            )
            return cur.fetchone()[0]
    except Exception:
        return 0


def _format_conversations(messages: list[tuple[datetime, str]]) -> str:
    """将消息列表格式化为 LLM 可读的对话文本。"""
    lines = []
    for ts, content in messages:
        if ts:
            time_str = ts.strftime("%m-%d %H:%M")
        else:
            time_str = "?"
        content_short = content[:300].replace("\n", " ")
        lines.append(f"[{time_str}] {content_short}")
    return "\n".join(lines)


def _build_extraction_prompt(old_profile: dict | None, conversations: str) -> str:
    """构建画像提取 LLM prompt。"""
    old_profile_text = json.dumps(old_profile, ensure_ascii=False, indent=2) if old_profile else "无（首次生成）"

    return f"""你是一个用户画像分析助手。根据用户的历史对话记录，提取或更新用户画像。

<existing_profile>
{old_profile_text}
</existing_profile>

<recent_conversations>
{conversations}
</recent_conversations>

请基于对话内容，提取或更新用户画像，返回 JSON：
{{
  "basic_info": {{
    "name": "用户提到的姓名或昵称，如无则为空字符串",
    "department": "部门，如无则为空字符串",
    "role": "职位或角色，如无则为空字符串",
    "location": "所在地，如无则为空字符串"
  }},
  "personality": "性格特征描述，如沟通风格、专业程度、关注点倾向",
  "preferences": ["偏好1", "偏好2", "偏好3"],
  "recent_topics": ["最近关注的话题1", "话题2", "话题3"],
  "communication_style": "沟通风格描述，如提问方式、表达习惯"
}}

规则：
1. 如果有 existing_profile 且不为"无"，在其基础上更新和补充，不要丢失已有有效信息
2. recent_topics 只保留最近 5-10 个话题，过时的应移除
3. 如果对话中未提及某字段，保留 existing_profile 中的值
4. 不要记录手机号、身份证号等敏感隐私信息
5. preferences 列表不超过 5 条
6. 返回纯 JSON，不要有任何其他文字、不要有 markdown 代码块标记"""


async def _extract_profile(old_profile: dict | None, conversations: str) -> dict | None:
    """调用 Fast LLM 提取画像，返回 dict 或 None。"""
    try:
        llm = _get_fast_llm()
        prompt = _build_extraction_prompt(old_profile, conversations)
        resp = await llm.ainvoke(prompt)
        text = resp.content if hasattr(resp, "content") else str(resp)
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        profile = json.loads(text)
        if not isinstance(profile, dict):
            logger.warning("[ExtractProfile] LLM 返回非 dict")
            return None
        logger.info(f"[ExtractProfile] 画像提取成功，keys={list(profile.keys())}")
        return profile
    except json.JSONDecodeError as e:
        logger.error(f"[ExtractProfile] JSON 解析失败: {e}")
        return None
    except Exception as e:
        logger.error(f"[ExtractProfile] LLM 调用失败: {e}")
        return None


def _save_profile(uid: str, profile: dict) -> bool:
    """将画像写入 DB。"""
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE {USER_TABLE} SET profile=%s, profile_updated_at=now() WHERE uid=%s AND deleted=false",
                (json.dumps(profile, ensure_ascii=False), uid)
            )
            conn.commit()
        logger.info(f"[SaveProfile] 画像已保存 uid={uid}")
        return True
    except Exception as e:
        logger.error(f"[SaveProfile] 保存画像失败 uid={uid}: {e}")
        return False


async def update_user_profile(uid: str) -> bool:
    """完整画像更新管线：读老画像 → 拉新对话 → LLM 提取 → 写 DB。
    返回 True 表示成功更新，False 表示跳过或失败。
    """
    try:
        total_msgs = await asyncio.to_thread(_count_user_messages, uid)
        if total_msgs < MIN_TURNS_FOR_PROFILE:
            logger.info(f"[UpdateProfile] uid={uid} 消息数 {total_msgs} < {MIN_TURNS_FOR_PROFILE}，跳过")
            return False

        old_profile = await asyncio.to_thread(get_user_profile, uid)
        last_updated = await asyncio.to_thread(_get_profile_updated_at, uid)

        messages = await asyncio.to_thread(_fetch_user_messages, uid, last_updated)
        if not messages:
            logger.info(f"[UpdateProfile] uid={uid} 无新对话，跳过")
            return False

        logger.info(f"[UpdateProfile] uid={uid} 老画像={'有' if old_profile else '无'}, 新消息={len(messages)}条")
        conversations = _format_conversations(messages)
        new_profile = await _extract_profile(old_profile, conversations)
        if not new_profile:
            logger.warning(f"[UpdateProfile] uid={uid} LLM 未返回有效画像")
            return False

        return await asyncio.to_thread(_save_profile, uid, new_profile)
    except Exception as e:
        logger.error(f"[UpdateProfile] uid={uid} 更新失败: {e}")
        return False


def _get_users_needing_update() -> list[str]:
    """查询需要更新画像的用户 uid 列表。
    条件：approved=true, deleted=false, 且有自 profile_updated_at 之后的新对话（或 profile_updated_at 为空）。
    """
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT u.uid
                FROM {USER_TABLE} u
                INNER JOIN {CONV_TABLE} c ON c.uid = u.uid AND c.role = 'user' AND c.content != ''
                WHERE u.deleted = false AND u.approved = true
                  AND (u.profile_updated_at IS NULL
                       OR c.created_at > u.profile_updated_at)
            """)
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"[GetUsersNeedingUpdate] 查询失败: {e}")
        return []


async def _update_all_profiles() -> int:
    """扫描并更新所有需要更新画像的用户。返回成功更新数。"""
    uids = await asyncio.to_thread(_get_users_needing_update)
    if not uids:
        logger.info("[ProfileLoop] 无用户需要更新画像")
        return 0

    logger.info(f"[ProfileLoop] 需要更新画像的用户: {len(uids)} 人")
    success = 0
    for uid in uids:
        ok = await update_user_profile(uid)
        if ok:
            success += 1
        await asyncio.sleep(1)  # 速率限制，避免 LLM API 并发过高
    logger.info(f"[ProfileLoop] 完成: {success}/{len(uids)} 成功")
    return success

# ================= 后台定时循环 =================

_profile_task: asyncio.Task | None = None

async def _profile_loop():
    """后台定时循环，每 PROFILE_INTERVAL_HOURS 小时执行一次。"""
    logger.info(f"[ProfileLoop] 启动，周期={PROFILE_INTERVAL_HOURS}h")
    while True:
        try:
            await _update_all_profiles()
        except asyncio.CancelledError:
            logger.info("[ProfileLoop] 已取消")
            break
        except Exception as e:
            logger.error(f"[ProfileLoop] 异常: {e}")
        await asyncio.sleep(PROFILE_INTERVAL_HOURS * 3600)


def start_profile_loop():
    """启动后台画像更新循环（在 app startup 时调用）。"""
    global _profile_task
    if _profile_task is None or _profile_task.done():
        _profile_task = asyncio.create_task(_profile_loop())
        logger.info("[ProfileLoop] 后台任务已创建")


def stop_profile_loop():
    """停止后台画像更新循环（在 app shutdown 时调用）。"""
    global _profile_task
    if _profile_task and not _profile_task.done():
        _profile_task.cancel()
        logger.info("[ProfileLoop] 后台任务已取消")
    _profile_task = None

# ================= API Router =================

router = APIRouter(prefix="/api/v1/profile", tags=["UserProfile"])


@router.get("/{uid}")
async def get_profile(uid: str, current_user: TokenData = Depends(get_current_user)):
    """查看用户画像（仅管理员或本人可查）。"""
    if current_user.uid != uid and not current_user.is_admin:
        return {"success": False, "error": "无权查看他人画像"}
    profile = await asyncio.to_thread(get_user_profile, uid)
    updated_at = await asyncio.to_thread(_get_profile_updated_at, uid)
    return {
        "success": True,
        "uid": uid,
        "profile": profile,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


@router.post("/{uid}/refresh")
async def refresh_profile(uid: str, current_user: TokenData = Depends(get_current_user)):
    """手动触发画像更新（仅管理员）。"""
    if not current_user.is_admin:
        return {"success": False, "error": "仅管理员可触发画像更新"}
    ok = await update_user_profile(uid)
    return {"success": ok, "uid": uid}
