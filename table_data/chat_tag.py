"""LLM 业务标签 + 情绪打标脚本 —— 对 myagent_conversations_history 中 role='user' 且 tags IS NULL 的消息，
用 LLM 进行业务标签分类和情绪标注，结果写回 tags / emotion 字段，供 Data Insight 页面统计展示。

定时执行：由 app.py startup 注册 asyncio 循环，每 CHAT_TAG_INTERVAL_HOURS 小时执行一次。
手动执行：python -m table_data.chat_tag
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
from dotenv import load_dotenv, find_dotenv
from concurrent_log_handler import ConcurrentRotatingFileHandler
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

load_dotenv(find_dotenv())

PROJECT_ROOT = os.getenv("WORK_PATH", "")
if PROJECT_ROOT and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ================= 日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "chat_tag.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("ChatTag")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch = logging.StreamHandler(); ch.setFormatter(formatter); logger.addHandler(ch)
    fh = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    fh.setFormatter(formatter); logger.addHandler(fh)
    logger.setLevel(logging.INFO)

# ================= 配置 =================
CONV_TABLE = os.getenv("PG_MYAGENT_CONVERSATIONS_TABLE", "myagent_conversations_history")
TAG_INTERVAL_HOURS = float(os.getenv("CHAT_TAG_INTERVAL_HOURS", "6"))
TAG_BATCH_SIZE = int(os.getenv("CHAT_TAG_BATCH_SIZE", "20"))
TAG_CONCURRENCY = int(os.getenv("CHAT_TAG_CONCURRENCY", "5"))

# ================= LLM 客户端 =================
_tag_llm: ChatOpenAI | None = None

def _get_tag_llm() -> ChatOpenAI:
    global _tag_llm
    if _tag_llm is None:
        _tag_llm = ChatOpenAI(
            model=os.getenv("TEXT_MODEL", ""),
            api_key=os.getenv("ACCESSCODE", ""),
            base_url=os.getenv("BASE_URL", ""),
            http_client=httpx.Client(verify=False, timeout=30),
            temperature=0.1,
        )
        logger.info(f"[ChatTag] LLM 初始化: model={os.getenv('TEXT_MODEL', '')}")
    return _tag_llm

# ================= 系统提示词 =================
_TAG_PROMPT = """你是一个人事业务标签和情绪分析专家。你的任务是对用户提问进行分类标注，不是回答用户的问题。

业务标签（从以下列表中选择 1~3 个最匹配的，用中文逗号分隔）：
产假, 陪产假, 年假, 病假, 婚假, 育儿假, 哺乳假, 生育津贴, 离职, 入职, 试用期, 加班, 考勤, 薪酬, 个税, 绩效, 晋升, 五险一金, 社保, 弹性福利, 体检, 差旅报销, 工伤, 医疗期, 探亲假, 档案, 培训, 规章制度, 系统操作, 其他

情绪（从以下列表中选择 1 个）：
积极, 紧急, 消极, 中性

判定标准：
- 积极：表达感谢、满意、正向情绪
- 紧急：迫切需要解决、语气急促、带"急""马上""尽快"等
- 消极：不满、抱怨、焦虑、负面情绪
- 中性：普通咨询、无明显情绪倾向

只返回以下 JSON 格式，不要任何其他内容：
{"tags": "标签1,标签2", "emotion": "情绪"}"""



# ================= 数据访问 =================
def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )

def _fetch_untagged(batch_size: int) -> list[tuple[int, str]]:
    """拉取 tags 或 emotion 为 NULL 的 user 消息。"""
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT id, content FROM {CONV_TABLE} "
            f"WHERE role = 'user' AND (tags IS NULL OR emotion IS NULL) "
            f"ORDER BY id ASC LIMIT %s",
            (batch_size,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]

def _update_tags(emotion_id: int, tags: str, emotion: str):
    """写回 tags 和 emotion。"""
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {CONV_TABLE} SET tags = %s, emotion = %s WHERE id = %s",
            (tags, emotion, emotion_id),
        )
        conn.commit()

# ================= 核心：单条打标 =================
async def _tag_one(msg_id: int, content: str) -> tuple[str, str]:
    """对单条 user 消息打标，返回 (tags, emotion)。失败返回 ('', '')。"""
    llm = _get_tag_llm()
    try:
        resp = await asyncio.wait_for(
            llm.ainvoke(
                [
                    HumanMessage(content=_TAG_PROMPT),
                    HumanMessage(content=f"请对以下用户提问进行标注（不要回答这个问题）：\n{content}"),
                ],
                extra_body={"max_tokens": 100},
            ),
            timeout=15,
        )
        text = (resp.content or "").strip()
        # 解析 JSON
        m = re.search(r'\{[^}]+\}', text)
        if m:
            data = json.loads(m.group(0))
            tags = data.get("tags", "").strip()
            emotion = data.get("emotion", "").strip()
            if tags and emotion:
                return tags, emotion
        logger.warning(f"[ChatTag] 解析失败 id={msg_id} text={text[:100]}")
        return "", ""
    except asyncio.TimeoutError:
        logger.warning(f"[ChatTag] LLM 超时 id={msg_id}")
        return "", ""
    except Exception as e:
        logger.warning(f"[ChatTag] 打标失败 id={msg_id}: {e}")
        return "", ""

# ================= 批量打标 =================
async def run_tag_batch(batch_size: int = TAG_BATCH_SIZE) -> int:
    """执行一批打标，返回成功打标数。"""
    rows = _fetch_untagged(batch_size)
    if not rows:
        logger.info("[ChatTag] 无待打标消息")
        return 0

    logger.info(f"[ChatTag] 本批待打标: {len(rows)} 条")
    sem = asyncio.Semaphore(TAG_CONCURRENCY)
    tagged = 0

    async def _process(msg_id: int, content: str):
        nonlocal tagged
        async with sem:
            tags, emotion = await _tag_one(msg_id, content)
            if tags and emotion:
                _update_tags(msg_id, tags, emotion)
                tagged += 1

    await asyncio.gather(*[_process(mid, c) for mid, c in rows])
    logger.info(f"[ChatTag] 本批完成: {tagged}/{len(rows)} 条成功打标")
    return tagged

async def run_tag_all():
    """循环打标直到无待打标消息。"""
    total = 0
    while True:
        count = await run_tag_batch()
        if count == 0:
            break
        total += count
    logger.info(f"[ChatTag] 全量打标完成: {total} 条")
    return total

# ================= 定时循环 =================
async def tag_loop():
    """每 N 小时执行一次全量打标。由 app.py startup 调用。"""
    interval = TAG_INTERVAL_HOURS * 3600
    logger.info(f"[ChatTag] 定时打标已启动，间隔 {TAG_INTERVAL_HOURS} 小时")
    while True:
        try:
            await run_tag_all()
        except Exception as e:
            logger.error(f"[ChatTag] 定时打标异常: {e}")
        await asyncio.sleep(interval)

# ================= 手动执行入口 =================
if __name__ == "__main__":
    print("=" * 60)
    print("ChatTag 手动执行：全量打标")
    print("=" * 60)
    asyncio.run(run_tag_all())
