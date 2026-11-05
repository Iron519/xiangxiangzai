import sys
import os
import json
import re
import asyncio
import jieba
from rank_bm25 import BM25Plus
from pathlib import Path
from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse, FileResponse
import logging
from concurrent_log_handler import ConcurrentRotatingFileHandler
import httpx
import uuid
import psycopg
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, AnyMessage, HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from typing import TypedDict, Annotated, Literal, Sequence, Optional
import operator
from dotenv import load_dotenv, find_dotenv

# ================= 环境配置参数 =================
load_dotenv(find_dotenv())

REQUIRED_ENV_VARS = ["WORK_PATH", "ACCESSCODE", "BASE_URL", "TEXT_MODEL", "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD", "PG_MYAGENT_CONVERSATIONS_TABLE", "PG_MYAGENT_KB_TABLE", "PG_MYAGENT_FAQ_TABLE", "FAQ_COVERAGE_THRESHOLD", "FAQ_RATIO_THRESHOLD"]
config = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}

# ================= 运行路径配置 ===============
PROJECT_ROOT = config['WORK_PATH']
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "generate_answer.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("GenerateAnswer")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)

# ================= 导入自定义工具和功能 =================
from calculate_logic.rag.retrieval_rerank import PgHybridRetriever
# 引进Chat所需的Tools并组装
from calculate_logic.tools.leave_quota import query_leave_quota
from calculate_logic.tools.leave_record import query_leave_record
from calculate_logic.tools.working_time import query_working_time
from calculate_logic.tools.personal_info import query_personal_info
from sso_did.auth import TokenData, get_current_user
from sso_did.auth_service import get_user_by_uid

# ================= 定义Langchain风格的LLM客户端（深入版） =================
_llm_client = None
def get_llm_client():
    global _llm_client  # 调控全局LLM客户端
    if _llm_client is None:
        _llm_client = ChatOpenAI(
            model=config["TEXT_MODEL"],
            api_key=config["ACCESSCODE"],
            base_url=config["BASE_URL"],
            http_client=httpx.Client(verify=False, timeout=30),
            temperature=0.1,
            top_p=0.1,
        )
    return _llm_client

# ================= 使用小模型的LLM客户端（快速版） =================
_fast_llm_client = None
def get_fast_llm_client():
    global _fast_llm_client
    if _fast_llm_client is None:
        _fast_llm_client = ChatOpenAI(
            model=config["TEXT_MODEL"],
            api_key=config["ACCESSCODE"],
            base_url=config["BASE_URL"],
            http_client=httpx.Client(verify=False, timeout=30),
            temperature=0.1,
            top_p=0.1,
        )
    return _fast_llm_client

# ================= Langgraph状态State =================
class ChatState(TypedDict):
    session_id: str  # 对话ID
    messages: Annotated[list[BaseMessage], add_messages]  # 历史消息列表，Annotated+add_messages使messages追加而非覆盖
    context: str  # 检索到的上下文
    sources: list  # 检索信息来源
    image: Optional[str]  # 内容关联的图片 URL，如检索结果配图、AI 回复引用图等
    mode: str  # 当前对话意图
    final_answer: str  # AI最终回答
    turn_count: int  # 对话轮次计数
    is_followup: Optional[bool]  # 当前对话是否为追问
    query: Optional[str]  # 当前查询，LLM重写后覆盖
    user: Optional[dict]  # 当前登录用户完整画像（uid/username/roles/email/department...），路由入口注入，节点按需取用
    current_time: Optional[str]  # 当前时间，路由入口注，供 LLM 填充工具日期参数（如 startDate/endDate）
    language: Optional[str]  # 用户提问语种（中文/英文...）
    scene: Optional[str]  # 场景判定结果（场景id），入口并行判定后注入，供 node_generate 套用场景模板
    scenes: Optional[list]  # 场景模板列表（入口一次性加载，供 node_generate 查找命中场景的模板内容）

# ================= 封装Langggraph工具 ===============
tools = [query_leave_quota, query_leave_record, query_working_time, query_personal_info]
tool_node = ToolNode(tools) # ToolNode的作用是将工具封装成一个节点：解析“工具调用”输入 -> 调用对应工具 -> 将结果包装成ToolMessage返回

TOOL_QUERY_HINTS = {   # 工具名 → 面向用户的「正在查询…」提示文案（仅前端展示用，不发给 LLM）。
    "query_leave_quota": "正在查询您的假期额度",
    "query_leave_record": "正在查询您的请假记录",
    "query_working_time": "正在查询您的工时数据",
    "query_personal_info": "正在查询您的个人信息",
}

# ================= 多轮对话全局变量 =================
followup_n = 1  # 判断是否为追问时，结合的历史对话轮数
rewrite_n = 2  # 查询重写需要结合的历史对话轮数
generate_n = 5  # 生成回答时，结合的历史对话轮数
max_tool_attempts = 3  # 工具连续尝试上限：达到后不再 bind_tools，强制用自然语言给用户兜底解释

# ================= 将AI对话存入PG表 =================
def _pg_dsn() -> str:  # 构建PG连接密钥串
    return (
        f"host={config['PG_HOST']} port={int(config['PG_PORT'])} "
        f"dbname={config['PG_DATABASE']} user={config['PG_USER']} "
        f"password={config['PG_PASSWORD']}"
    )
def _pg_conn():  # 构建PG客户端
    return psycopg.connect(
        host=config["PG_HOST"],
        port=int(config["PG_PORT"]),
        dbname=config["PG_DATABASE"],
        user=config["PG_USER"],
        password=config["PG_PASSWORD"],
    )
def _save_conversation_to_pg(session_id: str, turn_count: int, user_msg: str,  # 历史对话入库字段
                              ai_msg: str, mode: str, uid: str = "anonymous",
                              sources: list | None = None,
                              duration_ms: int | None = None,
                              prompt_token: int | None = None,
                              completion_token: int | None = None,
                              total_token: int | None = None) :
    try:
        with _pg_conn() as conn:
            # 依次存入user和assistant的对话内容
            conn.execute(
                f"INSERT INTO {config["PG_MYAGENT_CONVERSATIONS_TABLE"]} (uid, session_id, turn_count, role, content, mode, duration_ms, prompt_token, completion_token, total_token) VALUES (%s, %s, %s, 'user', %s, %s, %s, NULL, NULL, NULL)",
                (uid, session_id, turn_count, user_msg, mode, None),
            )
            conn.execute(
                f"INSERT INTO {config["PG_MYAGENT_CONVERSATIONS_TABLE"]} (uid, session_id, turn_count, role, content, mode, sources, duration_ms, prompt_token, completion_token, total_token) VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s)",
                (uid, session_id, turn_count, ai_msg, mode, json.dumps(sources if sources else []), duration_ms, prompt_token, completion_token, total_token),
            )
            conn.commit()
        logger.info(f"[PG] 已保存第{turn_count}轮对话: uid={uid} | session={session_id[:3]}... | mode={mode} | duration={duration_ms}ms | tokens={total_token}")
    except Exception as e:
        logger.error(f"[PG] 保存对话失败: {e}")
    
# ================= 兜底提示文案（LLM 生成失败时使用）================= 
_FALLBACK_ANSWERS = {
    "中文": "抱歉，我暂时无法回答您的问题，请稍后再试。",
    "英文": "Sorry, I'm unable to answer your question at the moment. Please try again later.",
}

# ================= 辅助函数 —— 检测用户提问语种 =================
def _detect_language(text: str) -> str:
    if not text or not text.strip():
        return "中文"
    if re.search(r"[\u3040-\u30ff]", text):  # 平假名+片假名
        return "日文"
    for pattern, lang in [
        (r"[\uac00-\ud7af]", "韩文"),      # 谚文
        (r"[\u0400-\u04ff]", "俄文"),      # 西里尔字母
        (r"[\u0600-\u06ff]", "阿拉伯文"),  # 阿拉伯字母
    ]:
        if re.search(pattern, text):
            return lang
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    return "英文" if latin > cjk else "中文"

# ================= 辅助函数 —— 取当前本地时间 =================
def _now_time() -> str:
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S（星期") \
        + ["一", "二", "三", "四", "五", "六", "日"][now.weekday()] \
        + f"，ISO日期 {now.strftime('%Y-%m-%d')}，dd/mm/yyyy 格式 {now.strftime('%d/%m/%Y')}）"

# ================= 辅助函数 ——  清洗历史消息，去除消息的元数据，仅保留Human和AI的对话正文 =================
def _clean_history_messages(history_messages: list[BaseMessage]) -> list[str]:
    dialogue_history = []
    for msg in history_messages:
        if isinstance(msg, HumanMessage): 
            dialogue_history.append(f"用户：{msg.content}")
        elif isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            dialogue_history.append(f"助手：{msg.content}")
    return dialogue_history


# ================= 判断用户问题是追问/全新问题 =================
async def if_follow(state: ChatState) -> ChatState:
    last_message = state["messages"][-1].content if state["messages"] else ""
    history_messages = _clean_history_messages(state["messages"])[-(followup_n * 2 + 1):-1]
    history_text = "\n".join(history_messages)
    if not history_messages:
        logger.info("[ClassifyTurn] 无历史消息 -> 判定为全新问题")
        return {"is_followup": False}

    classification_prompt = f"""你是一个多轮对话意图分类器。请阅读下方的【历史对话】和用户的【当前问题】。
    请判断当前问题属于以下哪种类型：

    【全新问题】
    - 独立主题，不依赖之前对话
    - 完整的问题陈述，不需要前文就能理解
    - 寒暄/客套/告别/... 如OK、谢谢、再见、了解等，不属于追问

    【追问】
    - 只有结合前文才具备完整的逻辑
    - 使用代词（它、这个、那、怎么做、上次说的）
    - 省略句（那怎么办？还有呢？然后呢？）
    - 承接前文的补充提问

    ---
    【历史对话】：
    {history_text}

    ---
    【当前问题】：
    {last_message}

    请只返回以下JSON格式（不要有任何其他内容）：
    {{"result": "followup"}}  如果是追问
    {{"result": "new"}}  如果是全新问题"""

    llm_client = get_fast_llm_client()
    try:
        response = await llm_client.ainvoke(
            [HumanMessage(content=classification_prompt)],
            extra_body={"enable_thinking": False, "max_tokens": 10},
            config={"tags": ["classify_turn_type"]}
        )
        decision = response.content.strip().lower()
    except Exception as e:
        logger.error(f"[ClassifyTurn] LLM 追问分类失败，退化为全新问题: {e}")
        decision = "new"
    is_followup = "followup" in decision

    context_mode = "followup" if is_followup else "new"
    logger.info(f"[ClassifyTurn] 该轮对话属于[{context_mode}]")
    return {"is_followup": is_followup}

# ================= 查询重写/指代消解 =================
async def query_rewrite(state: ChatState) -> ChatState:
    last_message = state["messages"][-1].content if state["messages"] else ""
    history_messages = _clean_history_messages(state["messages"])[-(rewrite_n * 2 + 1):-1]
    history_text = "\n".join(history_messages)
    previous_query = state.get("query", "")

    logger.info(f"[QueryRewrite] 用户: {last_message[:50] if last_message else '(empty)'}...")
    
    is_followup = state.get("is_followup", False)
    if not is_followup:      # 全新问题，无需改写
        logger.info(f"[QueryRewrite] 全新问题，无需改写")
        return {"query": last_message}
    
    rewrite_prompt = f"""你是一个查询改写专家，负责将用户的追问改写成完整的、无歧义的检索query。

    【任务说明】
    改写【当前问题】，需要结合【历史对话】将其还原为完整的、可独立检索的问题，并聚焦于【前一个提问】、继承关键信息，避免遗漏。

    【历史对话】
    {history_text if history_text else "（无历史对话）"}

    【当前问题】
    {last_message}

    【前一个提问】
    {previous_query if previous_query else " (无前序问题) "}

    【改写要求】
    1. **继承关键信息**：从【前一个提问】中提取所有关键术语、概念、实体、专有名词，确保不遗漏、不误读
        - 核心词（如政策类型、产品名、流程名等）必须原样保留，不得用近义词替换或泛化
        - 不要用更宽泛或更狭窄的词替换核心概念
    2. **指代消解**：将代词（这、那、它、这个、怎么办等）替换为历史对话中对应的具体内容
    3. **补充省略**：将不完整的句子补充为完整问题
    4. **独立可检索**：改写后的问题必须能独立理解，不依赖历史对话

    【示例】
    历史: 用户：年假过期后可以顺延吗？ 助手：过期后未使用的年假作废，无法顺延。
    当前: 那加班费怎么算？
    改写: 年假过期后加班费怎么计算？

    历史: 用户：我想请3天假 助手：请说明请假类型和日期
    当前: 事假需要提供证明吗？
    改写: 事假申请需要提供证明材料吗？

    历史：用户：我想购买MINI车，如何参加员工购车计划？助手：根据2026年MINI员工购车项目政策...
    当前追问：提交方式有哪些？
    改写：员工参加购车计划购买MINI车，提交方式有哪些？
    （注：继承提问中的MINI，确保不遗漏关键信息）

    历史: 无历史对话
    当前: 请问年假有多少天？
    改写: 请问年假有多少天？

    请直接返回改写后的query，不要任何解释或其他内容。"""

    llm_client = get_fast_llm_client()
    try:
        response = await llm_client.ainvoke(
            [HumanMessage(content=rewrite_prompt)],
            extra_body={"enable_thinking": False, "max_tokens": 100},
            config={"tags": ["query_rewrite"]}
        )
        rewritten_query = response.content.strip()
    except Exception as e:
        logger.error(f"[QueryRewrite] LLM 查询重写失败，回退原始 query: {e}")
        rewritten_query = last_message
    logger.info(f"[QueryRewrite] 改写结果: {rewritten_query}")    
    return {"query": rewritten_query}

# ================= 敏感话题LLM检测（与主线并行） =================
# 命中敏感话题时不走 RAG 链路，而是走独立固定话术分支（mode=sensitive）。
SENSITIVE_REPLY = os.getenv("SENSITIVE_REPLY", "抱歉，我无法回答这个问题。").strip()
SENSITIVE_TOPICS = os.getenv("SENSITIVE_TOPICS", "").strip()
async def _judge_sensitive(query: str) -> str:
    if not query or not query.strip():
        return "miss"
    judge_prompt = f"""你是一个内容安全审查器。请判断用户的最新提问是否涉及敏感或违规话题。
    注意：中英文表述都需识别，除非严格命中话题范围，否则视为安全。

    【敏感话题】
    {SENSITIVE_TOPICS}

    【违规话题】
    政治敏感、赌博、毒品药物、色情、暴力恐怖、诈骗、违法活动、侵犯他人隐私、黑客攻击等相关内容

    【用户提问】
    {query}

    只返回以下 JSON 之一（不要任何其他内容）：
    {{"result": "hit"}}  如果属于敏感/违规话题
    {{"result": "miss"}}  如果属于正常提问
    """

    llm_client = get_fast_llm_client()
    try:
        response = await asyncio.wait_for(
            llm_client.ainvoke(
                [HumanMessage(content=judge_prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 10},
            ),
            timeout=10,
        )
        text = (response.content or "").strip().lower()
        logger.info(f"[Sensitive] LLM判定 query='{query[:50]}' -> {text}")
        return "hit" if '"hit"' in text or "'hit'" in text else "miss"
    except asyncio.TimeoutError:
        logger.warning(f"[Sensitive] LLM 超时，按 miss 放行: query='{query[:50]}'")
        return "miss"
    except Exception as e:
        logger.error(f"[Sensitive] LLM 判定失败，按 miss 放行: {e}")
        return "miss"

# ================= 检测场景命中，调用场景模板（查表注入，实时生效） =================
SCENE_TABLE = os.getenv("PG_MYAGENT_SCENE_TABLE", "myagent_scene_templates")

def _load_scenes() -> list[dict]:
    # 从 PG 加载全部启用的场景模板,表极小（个位数行），同步查库耗时毫秒级。
    # 每次查库保证多 worker 数据完全一致，无需缓存。
    try:
        with _pg_conn() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(
                f"SELECT scene_id, name, trigger, answer_template, answer_mode, followup_prompt "
                f"FROM {SCENE_TABLE} ORDER BY id"
            )
            rows = cur.fetchall()
        scenes = [
            {
                "id": r["scene_id"],
                "name": r["name"],
                "trigger": r["trigger"] or "",
                "answer_template": r["answer_template"] or "",
                "answer_mode": r["answer_mode"] or "prompt",
                "followup_prompt": r["followup_prompt"] or "",
            }
            for r in rows
        ]
        logger.info(f"[SceneCache] 场景模板加载：{len(scenes)} 条")
        return scenes
    except Exception as e:
        logger.warning(f"[SceneCache] 加载失败，按空场景放行: {e}")
        return []


async def _judge_scene(query: str, scenes: list[dict] = None) -> str:
    """判断 query 命中哪个场景，返回场景 id；未命中返回空字符串。
    scenes 参数由调用方一次性加载后传入，避免重复查库。"""
    if scenes is None:
        scenes = _load_scenes()
    if not query or not query.strip() or not scenes:
        return ""
    scenes_desc = "\n".join(
        f"- id={s['id']} 场景名={s['name']} 触发场景={s['trigger']}" for s in scenes
    )
    judge_prompt = f"""判断用户提问是否命中以下【预设场景】之一，命中则返回对应场景 id，否则返回 none。
    
    【预设场景】
    {scenes_desc}

    【用户提问】
    {query}

    只返回以下 JSON 之一（不要任何其他内容）：
    {{"scene": "场景id"}}  如果命中某个场景
    {{"scene": "none"}}  如果未命中任何场景"""

    llm_client = get_fast_llm_client()
    try:
        response = await asyncio.wait_for(
            llm_client.ainvoke(
                [HumanMessage(content=judge_prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 10},
            ),
            timeout=10,
        )
        text = (response.content or "").strip().lower()
        logger.info(f"[SceneJudge] query='{query[:50]}' -> {text}")
        import re as _re
        m = _re.search(r'"scene"\s*:\s*"?([a-z0-9_-]+)"?', text)
        hit = m.group(1) if m else ""
        if hit in {s["id"] for s in scenes}:
            return hit
        return ""
    except asyncio.TimeoutError:
        logger.warning(f"[SceneJudge] LLM 超时，按未命中放行: query='{query[:50]}'")
        return ""
    except Exception as e:
        logger.error(f"[SceneJudge] 判定失败，按未命中放行: {e}")
        return ""


# ================= FAQ 快速匹配（置于入口之后，最先判断） =================
_faq_cache: dict = {"questions": [], "answers": [], "bm25": None}  # FAQ 缓存：进程内缓存一份 (questions, answers, bm25)，避免每问都查库+重建索引

# FAQ 快速匹配（BM25 + jieba 分词）
FAQ_TABLE = config['PG_MYAGENT_FAQ_TABLE']
FAQ_COVERAGE_THRESHOLD = float(config['FAQ_COVERAGE_THRESHOLD'])  # 查询分词位于Top 1文档分词的IDF加权概率
FAQ_RATIO_THRESHOLD = float(config['FAQ_RATIO_THRESHOLD'])  # Top1 Score/(Top1 Score + Top2 Score)

class _FaqBM25(BM25Plus):          # 继承 rank_bm25 库的 BM25Plus
    def __init__(self, faq_tokenized: list[list[str]], idf: dict[str, float]):
        super().__init__(faq_tokenized)      # 用父类初始化，但只喂 FAQ 问题
        self.idf = {**self.idf, **idf}       # 用外部传入的 IDF 覆盖父类算的 IDF

def _load_faq() -> tuple[list[str], list[str], BM25Plus]:  # 从 PG 加载全部 FAQ，并用 kb 全量语料的 IDF 构建 BM25 索引
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT question, answer FROM {FAQ_TABLE} ORDER BY id")
            rows = cur.fetchall()
            cur.execute(f"SELECT parent_content FROM {os.getenv('PG_MYAGENT_KB_TABLE', '')} WHERE parent_content IS NOT NULL")
            kb_rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"[FaqMatch] 加载FAQ/KB失败: {e}")
        return [], [], None

    questions = [r[0] for r in rows if r[0]]
    answers = [r[1] for r in rows if r[0]]
    if not questions:
        return [], [], None
    faq_tokenized = [list(jieba.cut(q)) for q in questions] # kb 全量语料分词，仅用于统计文档频率 → 算 IDF
    kb_tokenized = [list(jieba.cut(r[0])) for r in kb_rows if r[0]]
    idf = _calc_idf_from_corpus(kb_tokenized)
    bm25 = _FaqBM25(faq_tokenized, idf)
    return questions, answers, bm25

def _calc_idf_from_corpus(tokenized_corpus: list[list[str]]) -> dict[str, float]:
    import math
    n = len(tokenized_corpus)
    if n == 0:
        return {}
    df: dict[str, int] = {}
    for doc in tokenized_corpus:
        for word in set(doc):
            df[word] = df.get(word, 0) + 1
    return {word: math.log((n + 1) / d) for word, d in df.items()}

def _get_faq_bm25() -> tuple[list[str], list[str], BM25Plus]:
    if not _faq_cache["questions"] or _faq_cache["bm25"] is None:
        qs, ans, bm25 = _load_faq()
        _faq_cache.update(questions=qs, answers=ans, bm25=bm25)
        logger.info(f"[FaqMatch] FAQ索引已加载：{len(qs)} 条（IDF来自kb语料）")
    return _faq_cache["questions"], _faq_cache["answers"], _faq_cache["bm25"]

_faq_load_lock: "asyncio.Lock | None" = None  # 跨并发抢建索引的进程内互斥锁（异步上下文使用）

async def _aget_faq_bm25() -> tuple[list[str], list[str], BM25Plus]:
    """异步取 FAQ 索引：缓存命中直接返回，未命中则丢到线程池加载，避免阻断事件循环。"""
    global _faq_load_lock
    if _faq_load_lock is None:
        _faq_load_lock = asyncio.Lock()
    if _faq_cache["questions"] and _faq_cache["bm25"] is not None:
        return _faq_cache["questions"], _faq_cache["answers"], _faq_cache["bm25"]
    async with _faq_load_lock:
        # 双重检查：持锁期间可能已被其它协程加载完成
        if _faq_cache["questions"] and _faq_cache["bm25"] is not None:
            return _faq_cache["questions"], _faq_cache["answers"], _faq_cache["bm25"]
        qs, ans, bm25 = await asyncio.to_thread(_load_faq)
        _faq_cache.update(questions=qs, answers=ans, bm25=bm25)
        logger.info(f"[FaqMatch] FAQ索引已加载：{len(qs)} 条（IDF来自kb语料）")
    return _faq_cache["questions"], _faq_cache["answers"], _faq_cache["bm25"]

def preload_faq_index() -> None:
    """启动预热：worker 进程内预构建 FAQ BM25 索引。

    调用后首个请求不必现场同步重建索引（重建需对 kb 全量语料分词算 IDF，
    同步执行会阻塞事件循环数秒）。每个 worker 进程独立持有 _faq_cache，均需预热。
    """
    if _faq_cache["questions"] and _faq_cache["bm25"] is not None:
        return
    qs, ans, bm25 = _load_faq()
    _faq_cache.update(questions=qs, answers=ans, bm25=bm25)
    logger.info(f"[FaqMatch] 启动预热完成：{len(qs)} 条FAQ，IDF来自kb语料")

async def node_faq_match(state: ChatState) -> ChatState:
    query = state["messages"][-1].content if state["messages"] else ""
    if not query or not query.strip():
        return {"mode": state.get("mode", "rag")}

    questions, answers, bm25 = await _aget_faq_bm25()
    if not questions or bm25 is None:
        return {"mode": state.get("mode", "rag")}

    tokens = [t for t in jieba.cut(query) if t.strip()]
    scores = bm25.get_scores(tokens)
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    top1_idx, top1_score = ranked[0]
    top2_score = ranked[1][1] if len(ranked) > 1 else 0.0

    # 绝对相关度：查询的不重复词中，多大比例（按 IDF 加权）命中了 TOP1 文档
    unique_tokens = set(tokens)
    total_weight = sum(bm25.idf.get(t, 0) for t in unique_tokens)
    hit_weight = sum(bm25.idf.get(t, 0) for t in unique_tokens
                     if bm25.doc_freqs[top1_idx].get(t, 0) > 0)
    coverage = hit_weight / total_weight if total_weight > 0 else 0.0

    # 相对区分度：TOP1 在 TOP1+TOP2 中的占比，[0.5, 1]
    ratio = top1_score / (top1_score + top2_score) if (top1_score + top2_score) > 0 else 1.0
    logger.info(f"[FaqMatch] query='{query[:50]}' TOP1={top1_score:.4f} coverage={coverage:.4f} ratio={ratio:.4f}")

    if coverage >= FAQ_COVERAGE_THRESHOLD and ratio >= FAQ_RATIO_THRESHOLD:
        answer = answers[top1_idx]
        logger.info(f"[FaqMatch] 命中FAQ：Q='{questions[top1_idx][:50]}'")
        return {
            "mode": "faq",
            "final_answer": answer,
            "messages": [AIMessage(content=answer)],
        }

    logger.info(f"[FaqMatch] 未达阈值（coverage={coverage:.4f}<{FAQ_COVERAGE_THRESHOLD} 或 ratio={ratio:.4f}<{FAQ_RATIO_THRESHOLD}），走正常RAG")
    return {"mode": state.get("mode", "rag")}


# ================= RAG节点 —— 检索知识库chunk =================
retriever = PgHybridRetriever.from_env()
async def do_retrieval(query: str, uid: str = "") -> dict:
    result = await retriever.search(query, uid=uid)
    context = "\n".join([hit.parent_content for hit in result.final_results])
    sources = [
        {"file_name": hit.file_name, "content": hit.parent_content, "image_url": hit.image_url}
        for hit in result.final_results
    ]
    return context, sources

async def node_retrieve(state: ChatState) -> ChatState:
    query = state.get("query", "")
    uid = (state.get("user") or {}).get("uid", "") if isinstance(state.get("user"), dict) else ""
    logger.info(f"[Retrieve] 开始检索：'{query[:50] if query else '(empty)'}' uid={uid}...")

    try:
        context, sources = await do_retrieval(query, uid=uid)
    except Exception as e:
        logger.error(f"[Retrieve] 检索失败，返回空结果: {e}")
        context, sources = "", []
    logger.info(f"[Retrieve] 检索完成，sources数量={len(sources)}")

    return {
        "context": context,
        "sources": sources
    }

# ================= 辅助函数 ——  提取最新的ToolMessage，填充至生成回答的提示词中 =================
def _extract_tool_message(history_messages: list[BaseMessage]) -> list[str]:  # 从后往前提取 ToolMessage 和对应的工具调用记录
    tool_messages = []
    for msg in reversed(history_messages): 
        if isinstance(msg, ToolMessage) and msg.content:
            tool_messages.append(f"[工具结果]：{msg.content}")
        elif isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                tool_calls_text = " ".join(
                    f"[调用工具 {tc['name']}]" for tc in msg.tool_calls
                )
                tool_messages.append(tool_calls_text)
        else:
            break
    return list(reversed(tool_messages))

# ================= 辅助函数 ——  统计当前连续的工具调用尝试次数 =================
def _count_recent_tool_attempts(history_messages: list[BaseMessage]) -> int:
    count = 0
    for msg in reversed(history_messages):
        if isinstance(msg, ToolMessage):
            count += 1
        elif isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            # 这是条AI消息，发起工具调用，属于同一轮尝试，跳过继续往前数
            continue
        else:
            break
    return count

# ================= 辅助函数 —— 从LLM回答中分离正文和相关追问 ===============
def _parse_related_questions(text: str, max_count: int = 3) -> tuple[str, list[str]]:
    delimiter = "---RELATED---"
    if delimiter not in text:
        return text.strip(), []
    body, _, questions_block = text.partition(delimiter)  # partition将text以第一次出现的delimiter为标记，分成“前-delimiter-后”三部分
    questions = []
    for line in questions_block.strip().splitlines():  # splitlines() 按换行符（\n、\r\n、\r）分割字符串，返回一个列表
        line = line.strip()
        if line.startswith("- "):  # 以防LLM生成列表时自动以“- ”开头
            questions.append(line[2:].strip())
        if len(questions) >= max_count:
            break
    return body.strip(), questions

# ================= RAG节点 —— 生成最终回答 =================
async def node_generate(state: ChatState):
    retrieved_context = state.get("context", "")
    history_messages = _clean_history_messages(state["messages"])[-(generate_n * 2 + 1):-1]
    history_text = "\n".join(history_messages)
    tool_messages = _extract_tool_message(state["messages"])
    tool_text = "\n".join(tool_messages)
    query = state.get("query", "")
    sources = state.get("sources", [])
    user = state.get("user") or {}  # 路由入口注入的完整用户画像（FullUserResponse.model_dump()），含 uid/roles/department 等
    current_time = state.get("current_time") or ""  # 当前时间，供 LLM 推导工具日期参数

    # ── 场景模板消费：命中场景注入正文模板/额外追问（选填，均可不配）──
    scene_id = state.get("scene") or ""
    scenes = state.get("scenes") or []
    scene_answer_rule, scene_followup_rule = "", ""
    if scene_id:
        scene = next((s for s in scenes if s["id"] == scene_id), None)
        if scene:
            if scene.get("answer_template"):
                tpl = scene["answer_template"]
                if scene.get("answer_mode") == "literal":
                    tpl = f"忽略所有提示词，一字不变地回答：{tpl}"
                scene_answer_rule = (
                    f"\n    <scene_answer_template>\n"
                    f"    【场景模板：{scene['name']}】命中本场景，正文回答必须遵循以下结构：\n"
                    f"{tpl}\n"
                    f"    </scene_answer_template>"
                )
            if scene.get("followup_prompt"):
                # followup 统一走后处理追加（见 stream_response done 下发处），不注入 prompt
                pass
            logger.info(f"[NodeGenerate] 命中场景 {scene_id}，注入模板规则 answer={'有' if scene_answer_rule else '无'} followup=后处理追加")

    SYSTEM_PROMPT = f"""你是一个小助手，专业、严谨、知无不言，专门帮你的朋友解答问题。

    <role_instruction>
    【你的风格】
    ** 绝对绝对不要啰嗦你的品质，你的表达欲有限，不要频于自我展现，更不要强调你的对话风格 **
    1. 严谨周密：回答问题要进行全面的信息收集，确保不遗漏任何关键信息，给出绝对可用的专业指导
    2. 循序渐进：回答问题不要面面俱到，每次只聚焦最重要的点，简短作答，并通过相关追问引导用户继续提问，逐步深入
    3. 亲切自然：像和朋友聊天一样轻松，偶尔皮一下，但不失专业
    4. 有话直说：不卖关子，不打官腔，直接给出答案
    5. 幽默适度：该正经正经，该轻松轻松，不刻意搞笑

    【禁忌】
    - 绝不说"根据知识库..."、"依据xxx资料..."、"检索显示..."这种话
    - 绝对不输出emoji等表情包
    - 不要透露自己是机器人/知识库/AI
    - 不要像写报告一样层层分析，用户要的是答案不是作文
    - 上下文是你的回答依据，绝对禁止联想外部信息自由发挥，即便是常识，也要以上下文是否提供作为回答准则

    【回答原则】
    - 你将基于下文中的用户信息进行针对性的回答
    - 你可以使用系统提供的工具查询动态数据（如假期、薪资、工时...），要按照<tool_usage_rules>的指示使用工具
    - 你必须且只能依据 <retrieved_context> 标签内的资料回答，绝对禁止编造任何不在资料里的内容
    - 资料里查不到的：坦诚相告，别硬编乱造
    </role_instruction>

    <user_info>
    【用户信息】
    - 昵称: {user.get("username")}
    - 当前时间: {current_time}
    </user_info>

    <tool_usage_rules>
    【重要】工具调用规则：
    1. 调用工具前，必须检查是否已经从用户的对话中获取了所有必填参数！
    2. 如果缺少必填参数，务必要请求用户补充，绝对禁止自行猜测、捏造或使用示例值！
    3. 如果缺少必填参数，你必须放弃调用工具，直接用自然语言向用户追问缺失的信息！
    4. 获得工具的运行结果后，如果必填参数已经齐备，立即开始生成回答，**不要**重复调用！
    5. 工具的日期参数（如 startDate/endDate）需根据<user_info>中的【当前时间】推导：
       - 用户未指定区间时，默认查「当前年份」的1月1日至今；
       - 用户说「今年」「最近」「目前」等词，以当前时间所在年份推算；
       - 日期格式必须为 dd/mm/yyyy（如 01/01/2026）。
    </tool_usage_rules>

    <retrieved_context>
    {retrieved_context}
    </retrieved_context>

    请结合上述规则和背景资料，直接回答用户的最新问题。

    【图片引用要求】
    - 上下文<retrieved_context>可能包含 Markdown 图片链接（形如 ![](/api/media/...)）
    - 若某张图片与当前回答直接相关，必须将其图片链接原样内联到回答正文的对应位置，链接的 URL 要一字不改地照抄（不得截断、改写、转义或 `-` 换行），否则图片无法加载
    - 不相关的图片链接不要带入回答

    {scene_answer_rule}

    【相关追问生成规则】
    在你的回答正文结束后，必须基于上下文<retrieved_context>，生成 1~3 个相关的后续追问（模拟用户口吻），并保证该追问的答案在上下文中有体现。
    格式：
    1. 正文和追问之间用分隔符：---RELATED---
    2. 每个追问占一行，以 - 开头
    3. 追问是以用户视角发问，而非以助手口吻启示，比如可以是“那B如何实现？”，而不能是“你是否还想了解B如何实现？”
    4. 追问要具体、可直接提问
    5. 追问必须完全基于上下文内容，不许联想外部信息，避免用户追问却得不到回答
    6. 要避免生成已经问答过的内容
    7. 如果是闲聊/打招呼/情绪安抚类，生成 1 个追问即可
    {scene_followup_rule}
    示例格式：
    回答正文内容...
    ---RELATED---
    - 我还想了解年假的具体计算方式
    - 试用期的年假怎么算
    """

    USER_PROMPT = f"""
    <history_massage>
    {history_text}
    </history_massage>

    <current_query>
    {query}
    </current_query>

    <tool_result>
    {tool_text}
    </tool_result>
    """

    logger.info(f"[NodeGenerate] 开始生成回答")

    # 统计本次会话中工具已连续尝试的次数；达到上限则不再 bind_tools，强制用自然语言兜底
    tool_attempts = _count_recent_tool_attempts(state["messages"])  # 连续尝试调用Tools的次数
    tool_exhausted = tool_attempts >= max_tool_attempts
    if tool_exhausted:  # 工具已连续尝试 {max_tool_attempts} 次仍未拿到可用结果，本轮放弃调用工具，改为直接向用户给出合理的解释
        SYSTEM_PROMPT += (
            f"\n    <tool_fallback>\n"
            f"    【重要】工具已连续尝试 {max_tool_attempts} 次仍未查到可用结果。本轮**禁止再调用任何工具**，"
            f"请直接用自然语言向用户说明：相关查询暂时未能成功，可能是系统暂未同步该数据或当前信息不足，"
            f"并给出后续建议（如稍后再试、或请用户提供更多信息）。语气保持亲切自然，不要透露内部工具/系统的存在。\n"
            f"    </tool_fallback>"
        )
        logger.warning(f"[NodeGenerate] 工具已尝试 {tool_attempts} 次（上限 {max_tool_attempts}），本轮停用工具，走兜底解释")

    llm_client = get_llm_client()
    # 未达到工具尝试上限时才 bind_tools；达到上限后用裸 llm_client，强制模型只能输出纯文本
    llm_with_tools = llm_client if tool_exhausted else llm_client.bind_tools(tools)

    # 用 astream 逐 chunk 累加：既能在纯文本路径下让 chunk 经 astream_events 冒泡成 on_chat_model_stream（出口实现打字机），又能在流末拿到拼装好的 tool_calls。
    #  langchain-openai 的 bind_tools().astream() 逐 chunk 同时带 content 与 tool_call_chunks，AIMessageChunk 相加(acc + chunk)会自动合并 content 与 tool_call_chunks。
    acc: Optional[AIMessage] = None
    try:
        async for chunk in llm_with_tools.astream(
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=USER_PROMPT)],
            config={"tags": ["node_generate"]},
        ):
            acc = chunk if acc is None else acc + chunk
    except Exception as e:
        logger.error(f"[NodeGenerate] LLM 生成失败: {e}")
        fallback = "抱歉，我暂时无法回答您的问题，请稍后再试。"
        return {
            "mode": "rag",
            "final_answer": fallback,
            "messages": [AIMessage(content=fallback)],
            "sources": sources,
        }

    # 流末判断本次是否要调工具：
    # - 有 tool_calls：返回带 tool_calls 的 AIMessage，过渡文字丢弃（final_answer 置空），出口检测到 tool_calls 会发 type=tool_query 提示，然后走 tool_node → 回 node_generate 出最终文本。
    # - 无 tool_calls：纯文本回答，content 已在流式过程中经事件冒泡给前端（打字机）。
    tool_calls = getattr(acc, "tool_calls", None) if acc is not None else None
    if tool_calls:
        logger.info(f"[NodeGenerate] 转入工具节点：tool_calls={[tc['name'] for tc in tool_calls]}")
        return {
            "mode": "rag",
            "final_answer": "",          # 过渡文字丢弃，不持久化、不下发
            "messages": [acc],           # 带 tool_calls 的 AIMessage，供 tools_condition 路由到 tool_node
            "sources": sources,
        }

    full_answer = (acc.content if acc is not None else "") or ""

    # 打印近5轮的生成消息明细 
    def _debug_state(state: ChatState, show_n: int = 5):
        all_msgs = state["messages"]
        recent_msgs = all_msgs[-show_n:] 
        
        msgs = []
        for i, msg in enumerate(recent_msgs):
            name = type(msg).__name__
            content = getattr(msg, 'content', '')[:80] if hasattr(msg, 'content') else ""
            
            if isinstance(msg, ToolMessage):
                tool_info = f" (tool_call_id={msg.tool_call_id[:12]}...)"
            elif getattr(msg, 'tool_calls', None):
                tool_calls_desc = [
                    f"{tc['name']}({tc.get('args', {})})" for tc in msg.tool_calls
                ]
                tool_info = f" → {tool_calls_desc}"
            else:
                tool_info = ""            
            msgs.append(f"[{i+1}]{name}: {content}{tool_info}")        
        logger.info(f"[State] 最近{show_n}轮对话（工具尝试 {tool_attempts}/{max_tool_attempts}{'，已停用工具' if tool_exhausted else ''}）:\n  " + "\n  ".join(msgs))
    _debug_state(state)

    return {
        "mode": "rag",
        "final_answer": full_answer,
        "messages": [acc],           # 纯文本 AIMessage（content 已流式下发）
        "sources": sources
    }


# ================= 创建Langgraph链路 =================

# 第一步：创建 StateGraph，指定状态类型为 ChatState
builder = StateGraph(ChatState)

# 第二步：向图中添加所有节点
builder.add_node("classify_turn_type",  if_follow)           # 判断追问/全新问题
builder.add_node("query_rewrite",        query_rewrite)      # 查询重写（RAG专用）
builder.add_node("node_retrieve",        node_retrieve)      # 执行检索
builder.add_node("node_faq_match",       node_faq_match)     # FAQ快速匹配（BM25，入口后第一时间）

builder.add_node("tool_node",           tool_node)  # 工具节点，条件触发
builder.add_node("node_generate",        node_generate)     # 生成回答（终端）
 

# 第三步：设置图的入口节点
builder.set_entry_point("classify_turn_type")
 
# 第四步：定义节点之间的边（数据流向）
# FAQ 提到最前面：入口后第一时间判断是否为 FAQ，命中则直接回答并 END，否则直接走 RAG
builder.add_edge("classify_turn_type", "node_faq_match")

def route_faq_match(state: ChatState) -> Literal["query_rewrite", "__end__"]:
    """FAQ 命中（mode=faq）→ END；否则直接走 RAG（查询重写）。"""
    if state.get("mode") == "faq":
        logger.info("[RouteFaq] FAQ命中，进入结束节点")
        return END
    logger.info("[RouteFaq] FAQ未命中，直接走RAG")
    return "query_rewrite"

builder.add_conditional_edges(
    "node_faq_match",
    route_faq_match,
    {"query_rewrite": "query_rewrite", END: END},
)

# 第五步：RAG链路内部的边：查询重写 -> 检索 -> 生成 -> 结束
builder.add_edge("query_rewrite",    "node_retrieve")

builder.add_edge("node_retrieve",   "node_generate")

# 第六步： 选择工具
builder.add_conditional_edges("node_generate", 
    tools_condition,  # tools_condition检查状态里最后一条 AIMessage 有没有 tool_calls 字段
    {
        "tools": "tool_node",
        END: END,
    })
builder.add_edge("tool_node", "node_generate") # 将工具执行的结果传给generate节点，重新生成回答

# 第七步：编译图
class _AsyncPostgresSaverAdapter(PostgresSaver):  # 在同步 PostgresSaver 基础上补齐 async 接口，委托到线程池执行，不阻塞事件循环
    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)
    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)
    async def aput_writes(self, config, writes, task_id, task_path=""):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

_checkpointer_pool = ConnectionPool(
    conninfo=_pg_dsn(),
    min_size=1,
    max_size=10,
    open=False,          # 延迟到 setup()/首次使用时建立连接，避免 import 阶段就连接失败导致模块加载失败
    kwargs={"autocommit": True, "prepare_threshold": 0},
)
checkpointer = _AsyncPostgresSaverAdapter(conn=_checkpointer_pool)
try:
    _checkpointer_pool.open(wait=True)
    checkpointer.setup()        # 幂等：建表 IF NOT EXISTS
    logger.info("[Checkpoint] PG checkpointer 已就绪")
except Exception as _ckpt_err:
    # checkpoint 初始化失败不应阻断服务启动（对话仍可走PG历史持久化兜底）
    logger.error(f"[Checkpoint] PG checkpointer 初始化失败: {_ckpt_err}")

graph = builder.compile(checkpointer=checkpointer)
logger.info("[LangGraph] 图编译完成")

def close_checkpointer() -> None:  # 关闭 checkpoint 连接池。供应用关闭时调用
    try:
        _checkpointer_pool.close()
    except Exception as e:
        logger.warning(f"[Checkpoint] 关闭连接池失败: {e}")



# ================= chat session 管理函数 =================

# /test U00XXXXX 命令解析：admin 用户可模拟指定 uid 进行测试
_TEST_UID_PATTERN = re.compile(r"^/test\s+(U00\d{5})\s*(.*)", re.DOTALL)

def _parse_test_command(message: str, current_user: TokenData) -> tuple[str, str]:
    if not current_user.is_admin:
        return current_user.uid, message
    m = _TEST_UID_PATTERN.match(message.strip())
    if m:
        test_uid = m.group(1)
        rest = m.group(2).strip()
        logger.info(f"[TestCommand] admin 模拟 uid={test_uid}, 原始消息前50: {rest[:50] if rest else '(empty)'}...")
        return test_uid, rest
    return current_user.uid, message

def _make_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}

def _get_or_create_session_id(session_id: str | None) -> str:
    if session_id:
        return session_id
    return str(uuid.uuid4())

async def _get_current_state(session_id: str) -> dict:  # 获取当前会话状态，不存在则返回空状态
    config = _make_config(session_id)
    current_state = await graph.aget_state(config)
    if current_state and current_state.values:
        return current_state.values
    return {}

def _build_initial_state(session_id: str, message: str, turn_count: int, user: dict | None = None) -> ChatState:
    return {
        "session_id": session_id,
        "messages": [HumanMessage(content=message)],
        "context": "",
        "sources": [],
        "mode": "unknown",
        "final_answer": "",
        "turn_count": turn_count,
        "is_followup": None,
        "context_config": None,
        "query": None,
        "context_mode": None,
        "user": user,
        "current_time": _now_time(),
        "scene": None,
        "scenes": None,
    }


# ================= 对话API =================
router = APIRouter()

@router.post("/chat/stream")
async def chat_stream(
    session_id: str | None = Body(default=None),  # 取自请求体JSON，字段为session_id，可选，默认为None
    message: str = Body(...),  # 取自请求体JSON，字段为message，必填
    current_user: TokenData = Depends(get_current_user),  # 不从body取，依赖注入
):
    # 解析 /test U00XXXXX 命令：admin 可模拟指定 uid
    uid, message = _parse_test_command(message, current_user)

    session_id = _get_or_create_session_id(session_id)
    config = _make_config(session_id)

    current_state = await _get_current_state(session_id)
    turn_count = (current_state.get("turn_count", 0) + 1) if current_state else 1

    # 从单表取用户画像，供链路各节点按需取用。
    try:
        user_row = get_user_by_uid(uid)
        user_dict = {
            "uid": uid,
            "username": (user_row or {}).get("username") or current_user.username or uid,
        } if user_row else {"uid": uid, "username": current_user.username or uid}
    except Exception as e:
        logger.warning(f"[ChatStream] 取用户画像失败，退化为 token 层: {e}")
        user_dict = {"uid": uid, "username": current_user.username or uid}

    initial_state: ChatState = _build_initial_state(session_id, message, turn_count, user=user_dict)
    
    # 保持历史消息
    if current_state and current_state.get("messages"):
        initial_state["messages"] = current_state["messages"] + [HumanMessage(content=message)]
    
    async def stream_response():
        try:
            start_time = datetime.now(timezone.utc)
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id, 'turn_count': turn_count})}\n\n"
            # 判定阶段先发 thinking 提示，避免 start 后到主图启动间出现无反馈空窗
            yield f"data: {json.dumps({'type': 'thinking', 'hint': '收到您的问题'})}\n\n"

            # 联运行时共享的统计结果（由主图循环写入，命中敏感时由本函数覆盖）
            ctx = {
                "full_answer": "", "final_mode": "", "final_sources": [],
                "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                "scene_id": "",
            }
            QUEUE_END = object()
            persisted = False

            def _persist_turn(ctx_: dict) -> None:
                """在完成点就地落库，避免客户端断开时外层尾部持久化被中断导致漏存。"""
                nonlocal persisted
                if persisted:
                    return
                persisted = True
                try:
                    elapsed_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
                    logger.info(f"[Timing] 第{turn_count}轮 {ctx_['final_mode']} 模式耗时 {elapsed_ms}ms")
                    clean_answer_for_pg, _ = _parse_related_questions(ctx_["full_answer"])
                    _save_conversation_to_pg(
                        session_id=session_id,
                        turn_count=turn_count,
                        user_msg=message,
                        ai_msg=clean_answer_for_pg,
                        mode=ctx_["final_mode"],
                        uid=uid,
                        sources=ctx_["final_sources"],
                        duration_ms=elapsed_ms,
                        prompt_token=ctx_["prompt_tokens"],
                        completion_token=ctx_["completion_tokens"],
                        total_token=ctx_["total_tokens"],
                    )
                except Exception as pg_err:
                    logger.warning(f"[PG] 持久化写入失败（不影响对话）: {pg_err}")

            # ── 主图事件循环：独立 task，SSE 经 queue 透传给响应体 ──
            q: "asyncio.Queue" = asyncio.Queue()
            async def run_main():
                try:
                    async for event in graph.astream_events(initial_state, config=config, version="v2"):
                        event_type = event.get("event", "")
                        tags = event.get("tags", [])

                        # 查询重写/检索节点开始时，通知前端显示"正在思考中"
                        if event_type == "on_chain_start":
                            node_name = event.get("name", "")
                            if node_name in ["query_rewrite", "node_retrieve"]:
                                await q.put(f"data: {json.dumps({'type': 'thinking', 'hint': '正在思考中'})}\n\n")

                        # LLM 流式输出
                        if event_type == "on_chat_model_stream":
                            if "node_generate" in tags:
                                chunk = event.get("data", {}).get("chunk", {})
                                content = chunk.content if hasattr(chunk, 'content') else ""
                                if content:
                                    ctx["full_answer"] += content
                                    await q.put(f"data: {json.dumps({'type': 'content', 'content': content})}\n\n")

                        # 各终端节点完成
                        if event_type == "on_chain_end":
                            node_name = event.get("name", "")
                            output = event.get("data", {}).get("output", {})

                            if node_name == "node_generate":
                                # node_generate 在工具链路中会被多次执行：pass1 返回带 tool_calls 的
                                # AIMessage → 路由到 tool_node → 回到 node_generate；pass2 才返回最终文本回答。
                                # 只有当返回的消息不带 tool_calls 时才是终点，此时才发 done，避免提前/重复发 done。
                                msgs = output.get("messages", []) if isinstance(output, dict) else []
                                last_msg = msgs[-1] if msgs else None
                                if getattr(last_msg, "tool_calls", None):
                                    logger.info(f"[Stream] node_generate 产出 tool_calls，转入工具节点，暂不发 done")
                                    # 方案 A：工具路径下，模型可能已吐出少量过渡文字（经 on_chat_model_stream
                                    # 冒泡已发给前端），SSE 无法回滚；这里额外发一条工具查询提示，告诉用户
                                    # 正在查什么。文案从 TOOL_QUERY_HINTS 取，未登记的工具名回退为通用提示。
                                    tool_names = [tc.get("name", "") for tc in last_msg.tool_calls]
                                    hint = TOOL_QUERY_HINTS.get(tool_names[0], "正在查询…") if tool_names else "正在查询…"
                                    await q.put(f"data: {json.dumps({'type': 'tool_query', 'tool': tool_names[0] if tool_names else '', 'hint': hint})}\n\n")
                                else:
                                    ctx["final_sources"] = output.get("sources", [])
                                    ctx["final_mode"] = "rag"
                                    # 提取本次 LLM 生成消耗的 token 数
                                    # 非流式 ainvoke 的 usage 在 response_metadata.token_usage；
                                    # 流式 astream 逐 chunk 累加的 AIMessage 无 token_usage，usage 在 usage_metadata 字段。
                                    usage = (getattr(last_msg, "response_metadata", None) or {}).get("token_usage", {}) or {}
                                    ctx["prompt_tokens"] = usage.get("prompt_tokens")
                                    ctx["completion_tokens"] = usage.get("completion_tokens")
                                    ctx["total_tokens"] = usage.get("total_tokens")
                                    if ctx["total_tokens"] is None:
                                        um = getattr(last_msg, "usage_metadata", None) or {}
                                        ctx["prompt_tokens"] = um.get("input_tokens")
                                        ctx["completion_tokens"] = um.get("output_tokens")
                                        ctx["total_tokens"] = um.get("total_tokens")
                                    # 解析相关追问（场景命中时放宽上限，容纳场景额外追问）
                                    max_q = 6 if ctx.get("scene_id") else 3
                                    clean_answer, related_questions = _parse_related_questions(ctx["full_answer"], max_count=max_q)
                                    # 场景追问后处理：把场景 followup_prompt 原文追加到追问列表末尾（不经过 LLM，保证不挤掉原有追问）
                                    scene_hit_id = ctx.get("scene_id") or ""
                                    if scene_hit_id:
                                        scene_obj = next((s for s in _load_scenes() if s["id"] == scene_hit_id), None)
                                        if scene_obj and scene_obj.get("followup_prompt"):
                                            followup_q = scene_obj["followup_prompt"].strip()
                                            if followup_q and followup_q not in related_questions:
                                                related_questions.append(followup_q)
                                                logger.info(f"[Stream] 场景追问追加: {followup_q[:50]}")
                                    logger.info(f"[Stream] node_generate 完成，sources={len(ctx['final_sources'])}，related={len(related_questions)}")
                                    await q.put(f"data: {json.dumps({'type': 'done', 'sources': ctx['final_sources'], 'related_questions': related_questions})}\n\n")
                                    _persist_turn(ctx)

                            elif node_name == "node_faq_match":
                                # FAQ 命中（mode=faq）时为终端节点：答案非 LLM 流式产出，需手动下发全文后发 done
                                if isinstance(output, dict) and output.get("mode") == "faq":
                                    ctx["final_mode"] = "faq"
                                    faq_answer = output.get("final_answer", "")
                                    ctx["full_answer"] = faq_answer
                                    await q.put(f"data: {json.dumps({'type': 'content', 'content': faq_answer})}\n\n")
                                    await q.put(f"data: {json.dumps({'type': 'done', 'sources': []})}\n\n")
                                    _persist_turn(ctx)
                    await q.put(QUEUE_END)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[Stream] 主图事件循环异常: {str(e)}")
                    try:
                        await q.put(f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n")
                    except Exception:
                        pass
                    await q.put(QUEUE_END)

            # ── 敏感/场景判定（并行，先决出结果再启动主图）──
            # 两者都是小模型+极小输出，并行跑，等待成本≈慢者一趟，几乎不增加延迟。
            # - 敏感命中：不启动主图，直接返回固定话术。
            # - 场景命中：结果注入 initial_state 的 scene 字段，node_generate 据此套用场景模板。
            # 场景模板列表只加载一次，同时传给 _judge_scene 和 initial_state，避免重复查库。
            scenes = _load_scenes()
            initial_state["scenes"] = scenes
            sens_task = asyncio.create_task(_judge_sensitive(message))
            scene_task = asyncio.create_task(_judge_scene(message, scenes))
            scene_id = ""
            try:
                await asyncio.gather(sens_task, scene_task, return_exceptions=True)
                scene_id = scene_task.result() if not isinstance(scene_task.exception(), Exception) else ""
            except Exception as e:
                logger.warning(f"[SceneJudge] 判定读取失败，按未命中放行: {e}")
            # 确保 sens_task 已完成（gather with return_exceptions=True 已保证）
            sens_result = sens_task.result() if not isinstance(sens_task.exception(), Exception) else "miss"
            if sens_result == "hit":
                hit = True
                logger.warning("[Sensitive] 命中敏感话题，返回固定话术")
            else:
                hit = False
                if scene_id:
                    initial_state["scene"] = scene_id
                    ctx["scene_id"] = scene_id
                    logger.info(f"[SceneJudge] 命中场景: {scene_id}")

            # ── 主图事件循环 + 并行消费（敏感命中则跳过主图）──
            main_task = asyncio.create_task(run_main()) if not hit else None
            if not hit:
                # 正常流程：消费主图事件队列
                get_task = asyncio.create_task(q.get())
                pendings = {get_task} | ({main_task} if main_task else set())
                try:
                    while True:
                        done, pendings = await asyncio.wait(pendings, return_when=asyncio.FIRST_COMPLETED)
                        if get_task in done:
                            item = get_task.result()
                            if item is QUEUE_END:
                                break
                            yield item
                            get_task = asyncio.create_task(q.get())
                            pendings.add(get_task)
                finally:
                    if not get_task.done():
                        get_task.cancel()
                    if main_task is not None and not main_task.done():
                        main_task.cancel()
                        try:
                            await main_task
                        except (asyncio.CancelledError, Exception):
                            pass
            else:
                # 敏感命中：取消未完成的判定任务，不进入主图事件循环
                if not scene_task.done():
                    scene_task.cancel()
                if not sens_task.done():
                    sens_task.cancel()

            # ── 命中敏感：丢弃主图已产内容，下发固定话术 ──
            if hit:
                ctx["final_mode"] = "sensitive"
                ctx["full_answer"] = SENSITIVE_REPLY
                ctx["final_sources"] = []
                ctx["prompt_tokens"] = None
                ctx["completion_tokens"] = None
                ctx["total_tokens"] = None
                yield f"data: {json.dumps({'type': 'content', 'content': SENSITIVE_REPLY})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'sources': []})}\n\n"
                _persist_turn(ctx)

            # 兜底：正常路径下若主图完成点已落库则不重复写；异常中断时补写
            _persist_turn(ctx)

        except Exception as e:
            logger.error(f"[ChatStream] 流式输出异常: {str(e)}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


# ================= 用户反馈（点赞/点踩） =================
@router.post("/conversation/judgement")
async def conversation_judgement(session_id: str = Body(...),
                                 turn_count: int = Body(...),
                                 judgement: str = Body(...),
                                 detail: str = Body("", embed=True),
                                 _: TokenData = Depends(get_current_user)):
    """记录用户对某轮 AI 回答的反馈：judgement in {"like", "dislike", ""}。
    detail: 可选，点踩时的不满原因（如"回答不准确"等）。
    """
    if judgement not in {"like", "dislike", ""}:
        return {"success": False, "error": "judgement 仅支持 like/dislike（空串表示取消）"}
    if turn_count < 1:
        return {"success": False, "error": "turn_count 必须为正整数"}
    uid = current_user.uid
    try:
        with _pg_conn() as conn:
            if detail and judgement == "dislike":
                cur = conn.execute(
                    f"UPDATE {config["PG_MYAGENT_CONVERSATIONS_TABLE"]} "
                    "SET user_judgement = NULLIF(%s, ''), judgement_detail = %s "
                    "WHERE session_id = %s AND role = 'assistant' AND turn_count = %s AND uid = %s ",
                    (judgement, detail, session_id, turn_count, uid),
                )
            else:
                cur = conn.execute(
                    f"UPDATE {config["PG_MYAGENT_CONVERSATIONS_TABLE"]} "
                    "SET user_judgement = NULLIF(%s, '') "
                    "WHERE session_id = %s AND role = 'assistant' AND turn_count = %s AND uid = %s ",
                    (judgement, session_id, turn_count, uid),
                )
            conn.commit()
        logger.info(f"[Judgement] 已记录反馈: session={session_id[:8]}... turn={turn_count} judgement='{judgement}' detail='{detail}' 受影响{cur.rowcount}行")
        return {"success": True, "session_id": session_id, "turn_count": turn_count, "judgement": judgement}
    except Exception as e:
        logger.error(f"[Judgement] 记录反馈失败: {e}")
        return {"success": False, "error": str(e)}


# ================= 清除会话 =================
@router.post("/session/clear")
async def session_clear(session_id: str = Body(...), _: TokenData = Depends(get_current_user)):
    config = _make_config(session_id)
    
    # 获取空状态用于覆盖
    empty_state: ChatState = {
        "session_id": session_id,
        "messages": [],
        "context": "",
        "sources": [],
        "mode": "unknown",
        "final_answer": "",
        "turn_count": 0,
        "is_followup": None,
        "context_config": None,
        "query": None,
        "context_mode": None
    }
    
    await graph.aupdate_state(config, empty_state)
    logger.info(f"[Session] 清除会话: {session_id}")
    return {"success": True, "session_id": session_id}


# ================= 获取会话状态 =================
@router.get("/session/{session_id}")
async def session_get(session_id: str, _: TokenData = Depends(get_current_user)):
    values = await _get_current_state(session_id)
    
    if not values:
        return {"error": "session not found", "session_id": session_id}
    
    return {
        "session_id": values.get("session_id", session_id),
        "turn_count": values.get("turn_count", 0),
        "mode": values.get("mode", "unknown"),
        "messages_count": len(values.get("messages", [])),
        "context_preview": values.get("context", "")[:100] if values.get("context") else ""
    }