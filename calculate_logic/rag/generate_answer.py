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
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage, ToolMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
from typing import TypedDict, Annotated, Literal, Sequence, Optional
from dotenv import load_dotenv, find_dotenv
from typesafe_sdk import TypeSafeClient, Noul, Choice
from calculate_logic.rag.token_usage import log_token_usage

# ================= 环境配置参数 =================
load_dotenv(find_dotenv())

REQUIRED_ENV_VARS = ["WORK_PATH", "ACCESSCODE", "BASE_URL", "TEXT_MODEL", "FAST_MODEL", "PG_HOST", "PG_PORT", "PG_DATABASE", "PG_USER", "PG_PASSWORD", "PG_MYAGENT_CONVERSATIONS_TABLE", "PG_MYAGENT_SESSIONS_TABLE", "PG_MYAGENT_KB_TABLE", "PG_MYAGENT_FAQ_TABLE", "FAQ_COVERAGE_THRESHOLD", "FAQ_RATIO_THRESHOLD"]
config = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}

# 安全 fallback：spawn 模式下 load_dotenv 可能未生效
SESSIONS_TABLE = config.get("PG_MYAGENT_SESSIONS_TABLE") or os.getenv("PG_MYAGENT_SESSIONS_TABLE", "") or "myagent_sessions"
CONV_TABLE = config.get("PG_MYAGENT_CONVERSATIONS_TABLE") or os.getenv("PG_MYAGENT_CONVERSATIONS_TABLE", "") or "myagent_conversations_history"

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
            http_client=httpx.Client(
                verify=False,
                timeout=30,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            ),
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
            model=config.get("FAST_MODEL") or config["TEXT_MODEL"],
            api_key=config["ACCESSCODE"],
            base_url=config["BASE_URL"],
            http_client=httpx.Client(
                verify=False,
                timeout=30,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            ),
            temperature=0.1,
            top_p=0.1,
        )
    return _fast_llm_client

# ================= Jev (TypeSafe) 客户端 —— 用于判断类节点 =================
_jev_client = None
def get_jev_client():
    global _jev_client
    if _jev_client is None:
        _jev_client = TypeSafeClient()
        logger.debug("[Jev] TypeSafeClient 初始化完成")
    return _jev_client

# ================= Langgraph状态State =================
class ChatState(TypedDict):
    session_id: str  # 对话ID
    messages: Annotated[list[BaseMessage], add_messages]  # 历史消息列表，Annotated+add_messages使messages追加而非覆盖
    context: str  # 检索到的上下文
    sources: list  # 检索信息来源
    mode: str  # 当前对话意图
    final_answer: str  # AI最终回答
    turn_count: int  # 对话轮次计数
    query: Optional[str]  # 当前查询，LLM重写后覆盖
    user: Optional[dict]  # 当前登录用户完整画像（uid/username/roles/email/department...），路由入口注入，节点按需取用
    current_time: Optional[str]  # 当前时间，路由入口注，供 LLM 填充工具日期参数（如 startDate/endDate）
    language: Optional[str]  # 用户提问语种（中文/英文...）
    scene: Optional[str]  # 场景判定结果（场景id），入口并行判定后注入，供 node_generate 套用场景模板
    scenes: Optional[list]  # 场景模板列表（入口一次性加载，供 node_generate 查找命中场景的模板内容）
    route: Optional[str]  # 意图路由结果 (rag/tool/chitchat/hybrid)
    file_descriptions: Optional[list]  # 该用户的文件描述列表，供意图路由和检索规划使用
    planned_top_k: Optional[int]  # LLM 规划的检索切片数量
    search_strategy: Optional[str]  # 检索策略 (direct/broad/precise)
    target_files: Optional[list]  # 预估命中文件（未来用于标签过滤）

# ================= 封装Langggraph工具 ===============
tools = [query_leave_quota, query_leave_record, query_working_time, query_personal_info]
tool_node = ToolNode(tools) # ToolNode的作用是将工具封装成一个节点：解析“工具调用”输入 -> 调用对应工具 -> 将结果包装成ToolMessage返回

# ---- 前端等待提示：从 .env 读取，方便统一维护 ----
_DEFAULT_TOOL_QUERY_HINTS = {
    "query_leave_quota": "正在查询您的假期额度",
    "query_leave_record": "正在查询您的请假记录",
    "query_working_time": "正在查询您的工时数据",
    "query_personal_info": "正在查询您的个人信息",
}
_DEFAULT_NODE_HINTS = {
    "node_route_intent": "正在分析问题方向",
    "node_plan_retrieval": "正在规划检索策略",
    "node_retrieve": "正在检索知识库",
    "node_faq_match": "正在匹配常见问题",
    "node_generate": "正在生成回答",
    "tool_node": "正在调用工具",
}
_DEFAULT_WAITING_TIPS = [
    "您可以尝试用更具体的描述来提问",
    "支持上传图片提问哦",
    "常见问题可以直接点击追问按钮",
    "如果您的问题涉及多个方面，可以分步提问",
    "我会记住我们的对话上下文，可以直接追问",
    "假期、工时、个人信息等问题我可以直接查询",
]

def _load_json_env(key: str, default):
    """从环境变量读取 JSON，解析失败时回退到默认值。"""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        logger.warning(f"[Config] {key} 解析失败，使用默认值")
        return default

TOOL_QUERY_HINTS = _load_json_env("TOOL_QUERY_HINTS", _DEFAULT_TOOL_QUERY_HINTS)
NODE_HINTS = _load_json_env("NODE_HINTS", _DEFAULT_NODE_HINTS)
WAITING_TIPS = _load_json_env("WAITING_TIPS", _DEFAULT_WAITING_TIPS)
WAITING_TIPS_INTERVAL = int(os.getenv("WAITING_TIPS_INTERVAL", "4").strip() or "4")

# ================= 多轮对话全局变量 =================
rewrite_n = 4  # 查询重写需要结合的历史对话轮数
generate_n = 3  # 生成回答时，结合的历史对话轮数
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
                              related_questions: list | None = None,
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
                f"INSERT INTO {config["PG_MYAGENT_CONVERSATIONS_TABLE"]} (uid, session_id, turn_count, role, content, mode, sources, related_questions, duration_ms, prompt_token, completion_token, total_token) VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s)",
                (uid, session_id, turn_count, ai_msg, mode, json.dumps(sources if sources else []), json.dumps(related_questions if related_questions else []), duration_ms, prompt_token, completion_token, total_token),
            )
            conn.commit()
        logger.info(f"[PG] 已保存第{turn_count}轮对话: uid={uid} | session={session_id[:3]}... | mode={mode} | duration={duration_ms}ms | model={config.get('TEXT_MODEL','')} | tokens={total_token}")
    except Exception as e:
        logger.error(f"[PG] 保存对话失败: {e}")
    
# ================= 兜底提示文案（LLM 生成失败时使用，具体话术在文件后方从 .env 读取）=================

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


# ================= 意图路由（Jev Noul + LLM 兜底，基于文件描述判断是否需要检索） =================
async def node_route_intent(state: ChatState) -> ChatState:
    user_query = state["messages"][-1].content if state.get("messages") else ""
    uid = (state.get("user") or {}).get("uid", "") if isinstance(state.get("user"), dict) else ""

    from calculate_logic.rag.file_tagging import load_file_descriptions, ADMIN_UID
    file_descriptions = load_file_descriptions(uid, ADMIN_UID)

    if not file_descriptions:
        logger.info("[RouteIntent] 无文件描述，默认走 rag")
        return {"route": "rag", "file_descriptions": []}

    file_list_text = "\n".join(
        f"- {f['file_name']}: {f['description']}"
        for f in file_descriptions
    )

    # 优先用 Jev Noul 判断
    try:
        client = get_jev_client()
        result = await asyncio.wait_for(
            asyncio.to_thread(
                client.system_one,
                state={
                    "user_query": user_query,
                    "file_descriptions": file_list_text,
                },
                questions={
                    "needs_rag": Noul(
                        instructions="`user_query` 的问题能否从 `file_descriptions` 描述的文件中找到答案？文件描述涵盖的主题与用户问题相关则为是。",
                    ),
                    "needs_tool": Noul(
                        instructions="`user_query` 是否需要查询用户的个人动态数据（假期额度、请假记录、工时、个人信息）？",
                    ),
                },
            ),
            timeout=10,
        )
        rag_noul = result.nouls["needs_rag"].noul
        tool_noul = result.nouls["needs_tool"].noul

        if rag_noul > 0.5 and tool_noul > 0.5:
            route = "hybrid"
        elif rag_noul > 0.5:
            route = "rag"
        elif tool_noul > 0.5:
            route = "tool"
        else:
            route = "chitchat"

        logger.info(f"[RouteIntent] Jev rag={rag_noul:.4f} tool={tool_noul:.4f} -> {route}")
        return {"route": route, "file_descriptions": file_descriptions}
    except asyncio.TimeoutError:
        logger.warning("[RouteIntent] Jev 超时(10s)，降级为 LLM 判断")
    except Exception as e:
        logger.warning(f"[RouteIntent] Jev 不可用，降级为 LLM 判断: {e}")

    # LLM 兜底
    prompt = f"""判断用户问题的意图路由。

【知识库文件】
{file_list_text}

【用户问题】
{user_query}

【判断标准】
- rag: 问题属于上述知识库文件涵盖的主题
- tool: 需要查询个人动态数据（假期、工时、个人信息）
- chitchat: 闲聊/寒暄/感谢/情绪安抚
- hybrid: 既需要知识库又需要工具

只输出一个词：rag / tool / chitchat / hybrid"""

    try:
        llm = get_fast_llm_client()
        response = await asyncio.wait_for(
            llm.ainvoke(
                [HumanMessage(content=prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 10},
                config={"tags": ["route_intent"]},
            ),
            timeout=10,
        )
        route = response.content.strip().lower()
        if route not in ("rag", "tool", "chitchat", "hybrid"):
            route = "rag"
        log_token_usage("route_intent", response, config.get("FAST_MODEL", ""))
        logger.info(f"[RouteIntent] LLM 兜底 -> {route}")
        return {"route": route, "file_descriptions": file_descriptions}
    except Exception as e:
        logger.warning(f"[RouteIntent] LLM 兜底也失败，默认走 rag: {e}")
        return {"route": "rag", "file_descriptions": file_descriptions}


def route_intent(state: ChatState) -> Literal["node_generate", "node_faq_match"]:
    """意图路由条件边：chitchat/tool 直走 generate，rag/hybrid 先走 FAQ 匹配。"""
    intent = state.get("route", "rag")
    if intent in ("chitchat", "tool"):
        return "node_generate"
    return "node_faq_match"

# ================= 检索策略规划（替代 query_rewrite，增加难度评估和检索参数决策） =================
async def node_plan_retrieval(state: ChatState) -> ChatState:
    last_message = state["messages"][-1].content if state.get("messages") else ""
    file_descriptions = state.get("file_descriptions", [])

    history_messages = _clean_history_messages(state.get("messages", []))[-(rewrite_n * 2 + 1):-1]
    history_text = "\n".join(history_messages) if history_messages else "（无）"

    file_list_text = "\n".join(
        f"- {f['file_name']}: {f['description']} [标签: {', '.join(f.get('tags', []))}]"
        for f in file_descriptions
    ) if file_descriptions else "（无文件描述）"

    prompt = f"""你是一个检索策略规划器。请分析用户问题并制定检索策略。

【知识库文件】
{file_list_text}

【历史对话】
{history_text}

【用户当前问题】
{last_message}

请完成以下任务：
1. 如果是追问（有代词/省略），先做指代消解，把问题补全为独立完整的问题。注意：历史对话中用户的提问是改写的主要依据，AI的回答仅作为辅助参考，不要让AI的回答内容主导改写方向
2. 评估问题难度：simple(单一事实) / moderate(需上下文关联) / complex(多条件/多步骤)
3. 决定检索切片数量：
   - simple: 3-5
   - moderate: 5-8
   - complex: 8-12
4. 选择检索策略：
   - direct: 问题明确，直接检索
   - broad: 问题模糊或涉及多个主题，扩大范围
   - precise: 问题涉及具体数值/条款，精确检索
5. 预估可能命中的文件（基于文件描述，可为空）

输出JSON，格式：
{{"rewritten_query": "...", "difficulty": "simple", "reasoning": "...", "planned_top_k": 5, "search_strategy": "direct", "target_files": ["文件名"]}}

只输出JSON，不要其他内容。"""

    llm = get_fast_llm_client()
    raw = ""
    try:
        response = await asyncio.wait_for(
            llm.ainvoke(
                [SystemMessage(content="你是一个检索策略规划器，只输出JSON。"),
                 HumanMessage(content=prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 300},
                config={"tags": ["node_plan_retrieval"]},
            ),
            timeout=15,
        )
        log_token_usage("node_plan_retrieval", response, config.get("FAST_MODEL", ""))
        raw = response.content.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        plan = json.loads(raw)

        rewritten_query = plan.get("rewritten_query", "").strip() or last_message
        planned_top_k = int(plan.get("planned_top_k", 5))
        planned_top_k = max(2, min(15, planned_top_k))
        search_strategy = plan.get("search_strategy", "direct")
        if search_strategy not in ("direct", "broad", "precise"):
            search_strategy = "direct"
        target_files = plan.get("target_files", [])

        logger.info(f"[PlanRetrieval] query='{rewritten_query[:50]}' difficulty={plan.get('difficulty')} top_k={planned_top_k} strategy={search_strategy}")

        return {
            "query": rewritten_query,
            "planned_top_k": planned_top_k,
            "search_strategy": search_strategy,
            "target_files": target_files,
        }
    except Exception as e:
        logger.error(f"[PlanRetrieval] 规划失败，回退基础改写: {e}")
        return {"query": last_message, "planned_top_k": 5, "search_strategy": "direct", "target_files": []}

# ================= 敏感话题检测（Jev Noul，LLM 兜底，与主线并行） =================
# 命中敏感话题时不走 RAG 链路，而是走独立固定话术分支（mode=sensitive）。
SENSITIVE_REPLY = os.getenv("SENSITIVE_REPLY", "抱歉，我无法回答这个问题。").strip()
SENSITIVE_TOPICS = os.getenv("SENSITIVE_TOPICS", "").strip()

# ================= 兜底话术（从 .env 读取，方便统一修改） =================
FALLBACK_LLM_ERROR = os.getenv("FALLBACK_LLM_ERROR", "抱歉，我暂时无法回答您的问题，请稍后再试。").strip()
FALLBACK_SYSTEM_ERROR = os.getenv("FALLBACK_SYSTEM_ERROR", "抱歉，系统暂时遇到问题，请稍后再试。").strip()
FALLBACK_TIMEOUT = os.getenv("FALLBACK_TIMEOUT", "抱歉，系统响应超时，请稍后再试。").strip()
FALLBACK_EMPTY_RESPONSE = os.getenv("FALLBACK_EMPTY_RESPONSE", "抱歉，我暂时没有生成有效回答，请尝试换个问法或稍后再试。").strip()
async def _judge_sensitive(query: str) -> str:
    if not query or not query.strip():
        return "miss"

    # 优先用 Jev Noul 判断
    try:
        client = get_jev_client()
        result = await asyncio.wait_for(
            asyncio.to_thread(
                client.system_one,
                state={
                    "user_query": query,
                    "sensitive_topics": SENSITIVE_TOPICS,
                },
                questions={
                    "is_sensitive": Noul(
                        instructions="`user_query` 涉及 `sensitive_topics` 中的话题，或政治敏感、赌博、毒品药物、色情、暴力恐怖、诈骗、违法活动、侵犯他人隐私、黑客攻击等违规内容？除非严格命中，否则视为安全。",
                    ),
                },
            ),
            timeout=15,
        )
        noul_val = result.nouls["is_sensitive"].noul
        hit = noul_val > 0.7
        logger.info(f"[Sensitive] Jev noul={noul_val:.4f} query='{query[:50]}' -> {'hit' if hit else 'miss'}")
        return "hit" if hit else "miss"
    except asyncio.TimeoutError:
        logger.warning(f"[Sensitive] Jev 超时(15s)，降级为 LLM 判断")
    except Exception as e:
        logger.warning(f"[Sensitive] Jev 不可用，降级为 LLM 判断: {e}")

    # LLM 兜底
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
        log_token_usage("sensitive_judge", response, config.get("FAST_MODEL", ""))
        text = (response.content or "").strip().lower()
        logger.info(f"[Sensitive] LLM兜底 query='{query[:50]}' -> {text}")
        return "hit" if '"hit"' in text or "'hit'" in text else "miss"
    except asyncio.TimeoutError:
        logger.warning(f"[Sensitive] LLM 超时，按 miss 放行: query='{query[:50]}'")
        return "miss"
    except Exception as e:
        logger.error(f"[Sensitive] LLM 兜底也失败，按 miss 放行: {e}")
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
    scenes 参数由调用方一次性加载后传入，避免重复查库。
    优先用 Jev Choice 判断，不可用时降级为 LLM。"""
    if scenes is None:
        scenes = _load_scenes()
    if not query or not query.strip() or not scenes:
        return ""

    valid_scene_ids = {s["id"] for s in scenes}

    # 优先用 Jev Choice 判断
    try:
        client = get_jev_client()
        criteria = {s["id"]: f"{s['name']}：{s['trigger']}" for s in scenes}
        result = await asyncio.wait_for(
            asyncio.to_thread(
                client.system_one,
                state={"user_query": query},
                questions={
                    "scene_hit": Choice(
                        instructions="`user_query` 命中以下哪个预设场景？",
                        criteria=criteria,
                    ),
                    "has_match": Noul(
                        instructions="`user_query` 明确命中某个预设场景的触发条件？如果只是泛泛提问或闲聊，未命中任何场景。",
                    ),
                },
            ),
            timeout=15,
        )
        has_match = result.nouls["has_match"].noul > 0.7
        if has_match:
            hit_id = result.choices["scene_hit"].choice
            if hit_id in valid_scene_ids:
                logger.info(f"[SceneJudge] Jev 命中场景: {hit_id} query='{query[:50]}'")
                return hit_id
        logger.info(f"[SceneJudge] Jev 未命中 has_match={result.nouls['has_match'].noul:.4f} query='{query[:50]}'")
        return ""
    except asyncio.TimeoutError:
        logger.warning(f"[SceneJudge] Jev 超时(15s)，降级为 LLM 判断")
    except Exception as e:
        logger.warning(f"[SceneJudge] Jev 不可用，降级为 LLM 判断: {e}")

    # LLM 兜底
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
        log_token_usage("scene_judge", response, config.get("FAST_MODEL", ""))
        text = (response.content or "").strip().lower()
        logger.info(f"[SceneJudge] LLM兜底 query='{query[:50]}' -> {text}")
        import re as _re
        m = _re.search(r'"scene"\s*:\s*"?([a-z0-9_-]+)"?', text)
        hit = m.group(1) if m else ""
        if hit in valid_scene_ids:
            return hit
        return ""
    except asyncio.TimeoutError:
        logger.warning(f"[SceneJudge] LLM 超时，按未命中放行: query='{query[:50]}'")
        return ""
    except Exception as e:
        logger.error(f"[SceneJudge] LLM 兜底也失败，按未命中放行: {e}")
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
        logger.debug(f"[FaqMatch] FAQ索引已加载：{len(qs)} 条（IDF来自kb语料）")
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
        logger.debug(f"[FaqMatch] FAQ索引已加载：{len(qs)} 条（IDF来自kb语料）")
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
    logger.debug(f"[FaqMatch] 启动预热完成：{len(qs)} 条FAQ，IDF来自kb语料")

async def node_faq_match(state: ChatState) -> ChatState:
    query = state["messages"][-1].content if state["messages"] else ""
    if not query or not query.strip():
        return {"mode": state.get("mode", "rag")}

    try:
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
    except Exception as e:
        logger.error(f"[FaqMatch] 异常，降级走RAG: {e}")
        return {"mode": state.get("mode", "rag")}


# ================= RAG节点 —— 检索知识库chunk =================
retriever = PgHybridRetriever.from_env()
async def do_retrieval(query: str, uid: str = "", top_k: int = 5, strategy: str = "direct") -> dict:
    if strategy == "broad":
        result = await retriever.search(query, uid=uid, rerank_top_k=top_k,
                                         candidate_top_k=max(15, top_k * 2),
                                         coarse_top_k=max(12, top_k * 2))
    elif strategy == "precise":
        result = await retriever.search(query, uid=uid, rerank_top_k=top_k,
                                         candidate_top_k=max(8, top_k),
                                         coarse_top_k=max(8, top_k))
    else:
        result = await retriever.search(query, uid=uid, rerank_top_k=top_k)
    # context 使用命中子块 + 同父块中前后各1个子块（共3个子块），节省 LLM 输入长度
    # sources 保留完整父块，供前端展示来源和数据库持久化
    context_parts = []
    sources = []
    for hit in result.final_results:
        sibling_text = retriever._fetch_sibling_child_contents(
            group_id=hit.group_id, internal_id=hit.internal_id, uid=uid
        )
        if sibling_text:
            context_parts.append(sibling_text)
        else:
            context_parts.append(hit.child_content or "")
        sources.append({"file_name": hit.file_name, "content": hit.parent_content, "image_url": hit.image_url})
    context = "\n".join(context_parts)
    return context, sources

async def node_retrieve(state: ChatState) -> ChatState:
    query = state.get("query", "")
    uid = (state.get("user") or {}).get("uid", "") if isinstance(state.get("user"), dict) else ""
    planned_top_k = state.get("planned_top_k") or 5
    search_strategy = state.get("search_strategy", "direct")
    logger.info(f"[Retrieve] 开始检索：'{query[:50] if query else '(empty)'}' uid={uid} top_k={planned_top_k} strategy={search_strategy}")

    try:
        context, sources = await do_retrieval(query, uid=uid, top_k=planned_top_k, strategy=search_strategy)
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
    route = state.get("route", "rag")
    is_chitchat = (route == "chitchat")
    history_messages = _clean_history_messages(state["messages"])[-(generate_n * 2 + 1):-1]
    history_text = "\n".join(history_messages)
    tool_messages = _extract_tool_message(state["messages"])
    tool_text = "\n".join(tool_messages)
    query = state.get("query") or (state["messages"][-1].content if state.get("messages") else "")
    sources = state.get("sources", [])
    language = state.get("language") or "中文"
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
    - {"闲聊时轻松自然，像朋友一样对话即可" if is_chitchat else "上下文是你的回答依据，绝对禁止联想外部信息自由发挥，即便是常识，也要以上下文是否提供作为回答准则"}

    【回答原则】
    - 你将基于下文中的用户信息进行针对性的回答
    - 你可以使用系统提供的工具查询动态数据（如假期、薪资、工时...），要按照<tool_usage_rules>的指示使用工具
    {"- 你必须且只能依据 <retrieved_context> 标签内的资料回答，绝对禁止编造任何不在资料里的内容" if retrieved_context.strip() else "- 如果没有提供 <retrieved_context> 资料，请根据你的能力直接回答用户问题，或使用工具查询所需数据"}
    - {"资料里查不到的：坦诚相告，别硬编乱造" if retrieved_context.strip() else "无法确定的：坦诚相告，别硬编乱造"}
    - 你必须使用与用户提问相同的语言回答（用户语种：{language}）
    </role_instruction>

    <user_info>
    【用户信息】
    - 昵称: {user.get("username")}
    - 当前时间: {current_time}
    - 用户语种: {language}
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
    在你的回答正文结束后，{"如果是在闲聊/打招呼，生成 1 个自然的追问引导用户继续对话即可" if is_chitchat else "必须基于上下文<retrieved_context>，生成 1~3 个相关的后续追问（模拟用户口吻），并保证该追问的答案在上下文中有体现"}。
    格式：
    1. 正文和追问之间用分隔符：---RELATED---
    2. 每个追问占一行，以 - 开头
    3. 追问是以用户视角发问，而非以助手口吻启示，比如可以是“那B如何实现？”，而不能是“你是否还想了解B如何实现？”
    4. 追问要具体、可直接提问
    {"5. 追问可以基于当前话题自然延伸" if is_chitchat else "5. 追问必须完全基于上下文内容，不许联想外部信息，避免用户追问却得不到回答"}
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
        return {
            "mode": "rag",
            "final_answer": FALLBACK_LLM_ERROR,
            "messages": [AIMessage(content=FALLBACK_LLM_ERROR)],
            "sources": sources,
        }

    # 兜底：astream 无任何输出（acc=None）或输出为空
    if acc is None or not (acc.content or "").strip():
        logger.warning("[NodeGenerate] LLM 返回空内容，使用兜底文案")
        return {
            "mode": "rag",
            "final_answer": FALLBACK_LLM_ERROR,
            "messages": [AIMessage(content=FALLBACK_LLM_ERROR)],
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
builder.add_node("node_route_intent",   node_route_intent)      # 意图路由（基于文件描述判断是否需要检索）
builder.add_node("node_faq_match",      node_faq_match)         # FAQ快速匹配（BM25）
builder.add_node("node_plan_retrieval", node_plan_retrieval)    # 检索策略规划（指代消解+难度评估+检索参数决策）
builder.add_node("node_retrieve",       node_retrieve)          # 执行检索
builder.add_node("tool_node",           tool_node)              # 工具节点，条件触发
builder.add_node("node_generate",       node_generate)          # 生成回答（终端）

# 第三步：设置图的入口节点
builder.set_entry_point("node_route_intent")

# 第四步：定义节点之间的边（数据流向）
# 意图路由条件边：chitchat/tool 直走 generate，rag/hybrid 先走 FAQ 匹配
builder.add_conditional_edges(
    "node_route_intent",
    route_intent,
    {"node_generate": "node_generate", "node_faq_match": "node_faq_match"},
)

# FAQ 匹配：命中 → END，未命中 → 检索策略规划
def route_faq_match(state: ChatState) -> Literal["node_plan_retrieval", "__end__"]:
    """FAQ 命中（mode=faq）→ END；否则走检索策略规划。"""
    if state.get("mode") == "faq":
        logger.info("[RouteFaq] FAQ命中，进入结束节点")
        return END
    logger.info("[RouteFaq] FAQ未命中，走检索策略规划")
    return "node_plan_retrieval"

builder.add_conditional_edges(
    "node_faq_match",
    route_faq_match,
    {"node_plan_retrieval": "node_plan_retrieval", END: END},
)

# RAG链路：检索策略规划 → 检索 → 生成
builder.add_edge("node_plan_retrieval", "node_retrieve")
builder.add_edge("node_retrieve", "node_generate")

# 工具循环：生成节点判断是否需要调用工具
builder.add_conditional_edges("node_generate",
    tools_condition,
    {
        "tools": "tool_node",
        END: END,
    })
builder.add_edge("tool_node", "node_generate")

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
    logger.debug("[Checkpoint] PG checkpointer 已就绪")
except Exception as _ckpt_err:
    # checkpoint 初始化失败不应阻断服务启动（对话仍可走PG历史持久化兜底）
    logger.error(f"[Checkpoint] PG checkpointer 初始化失败: {_ckpt_err}")
graph = builder.compile(checkpointer=checkpointer)
logger.debug("[LangGraph] 图编译完成")

def close_checkpointer() -> None:  # 关闭 checkpoint 连接池。供应用关闭时调用
    try:
        _checkpointer_pool.close()
    except Exception as e:
        logger.warning(f"[Checkpoint] 关闭连接池失败: {e}")



# ================= chat session 管理函数 =================

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
        "query": None,
        "user": user,
        "current_time": _now_time(),
        "language": _detect_language(message),
        "scene": None,
        "scenes": None,
        "route": None,
        "file_descriptions": None,
        "planned_top_k": None,
        "search_strategy": None,
        "target_files": None,
    }


# ================= 对话API =================
router = APIRouter()

@router.post("/chat/stream")
async def chat_stream(
    session_id: str | None = Body(default=None),  # 取自请求体JSON，字段为session_id，可选，默认为None
    message: str = Body(...),  # 取自请求体JSON，字段为message，必填
    current_user: TokenData = Depends(get_current_user),  # 不从body取，依赖注入
):
    uid = current_user.uid
    # ── Session 管理：归属校验 / 新建 / 超限软删 ──
    is_new_session = False
    if session_id:
        # 已有 session：校验是否属于当前 uid
        if not _verify_session_owner(uid, session_id):
            # 归属不匹配或不存在：当作新 session 处理
            session_id = str(uuid.uuid4())
            is_new_session = True
    else:
        # 新 session
        session_id = str(uuid.uuid4())
        is_new_session = True

    if is_new_session:
        _enforce_session_limit(uid)
        _create_session_row(uid, session_id)

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
            yield f"data: {json.dumps({'type': 'start', 'session_id': session_id, 'turn_count': turn_count, 'tips': WAITING_TIPS, 'tips_interval': WAITING_TIPS_INTERVAL})}\n\n"
            # 判定阶段先发 thinking 提示，避免 start 后到主图启动间出现无反馈空窗
            yield f"data: {json.dumps({'type': 'thinking', 'hint': NODE_HINTS.get('node_route_intent', '收到您的问题')})}\n\n"

            # ── 首轮并行生成 session 标题（与主图/敏感判定同时进行，不阻塞回答流）──
            title_task = None
            if is_new_session and turn_count == 1 and message:
                title_task = asyncio.create_task(_generate_session_title(message))

            # 联运行时共享的统计结果（由主图循环写入，命中敏感时由本函数覆盖）
            ctx = {
                "full_answer": "", "final_mode": "", "final_sources": [],
                "final_related_questions": [],
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
                        related_questions=ctx_.get("final_related_questions", []),
                        duration_ms=elapsed_ms,
                        prompt_token=ctx_["prompt_tokens"],
                        completion_token=ctx_["completion_tokens"],
                        total_token=ctx_["total_tokens"],
                    )
                    _update_session_timestamp(uid, session_id, turn_count)
                except Exception as pg_err:
                    logger.warning(f"[PG] 持久化写入失败（不影响对话）: {pg_err}")

            # ── 主图事件循环：独立 task，SSE 经 queue 透传给响应体 ──
            q: "asyncio.Queue" = asyncio.Queue()
            async def run_main():
                try:
                    async for event in graph.astream_events(initial_state, config=config, version="v2"):
                        event_type = event.get("event", "")
                        tags = event.get("tags", [])

                        # 各节点开始时，通知前端显示对应提示文案
                        if event_type == "on_chain_start":
                            node_name = event.get("name", "")
                            if node_name in NODE_HINTS:
                                await q.put(f"data: {json.dumps({'type': 'thinking', 'hint': NODE_HINTS[node_name]})}\n\n")

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
                                    _model = (getattr(last_msg, "response_metadata", None) or {}).get("model_name") or \
                                             (getattr(last_msg, "response_metadata", None) or {}).get("model") or \
                                             config.get("TEXT_MODEL", "")
                                    logger.info(
                                        f"[node_generate] model={_model} "
                                        f"prompt={ctx['prompt_tokens']} completion={ctx['completion_tokens']} total={ctx['total_tokens']}"
                                    )
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
                                    ctx["final_related_questions"] = related_questions
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
                        if not ctx["full_answer"]:
                            ctx["full_answer"] = FALLBACK_SYSTEM_ERROR
                            await q.put(f"data: {json.dumps({'type': 'content', 'content': ctx['full_answer']})}\n\n")
                        await q.put(f"data: {json.dumps({'type': 'done', 'sources': []})}\n\n")
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
            GRAPH_TIMEOUT = 60  # 整个主图执行超时上限（秒），防止 LLM/API 挂死导致前端永远空转
            main_task = asyncio.create_task(run_main()) if not hit else None
            if not hit:
                # 正常流程：消费主图事件队列
                get_task = asyncio.create_task(q.get())
                pendings = {get_task} | ({main_task} if main_task else set())
                timed_out = False
                try:
                    while True:
                        done, pendings = await asyncio.wait(pendings, return_when=asyncio.FIRST_COMPLETED, timeout=GRAPH_TIMEOUT)
                        if not done:
                            # 超时：没有任何事件到达，强制中断
                            logger.error(f"[Stream] 主图执行超时（{GRAPH_TIMEOUT}s），强制中断")
                            timed_out = True
                            break
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
                # 超时兜底：给前端发 fallback + done，避免前端永远空转
                if timed_out:
                    if not ctx["full_answer"]:
                        ctx["full_answer"] = FALLBACK_TIMEOUT
                        yield f"data: {json.dumps({'type': 'content', 'content': ctx['full_answer']})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'sources': []})}\n\n"
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

            # ── 兜底：主图/敏感/超时都未产出内容时，补发空响应提示 ──
            if not ctx["full_answer"]:
                ctx["full_answer"] = FALLBACK_EMPTY_RESPONSE
                yield f"data: {json.dumps({'type': 'content', 'content': FALLBACK_EMPTY_RESPONSE})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'sources': []})}\n\n"
                logger.warning(f"[Stream] 流结束时 full_answer 仍为空，补发 FALLBACK_EMPTY_RESPONSE")

            # 兜底：正常路径下若主图完成点已落库则不重复写；异常中断时补写
            _persist_turn(ctx)

            # ── 首轮自动生成标题：等待并行启动的标题任务完成 ──
            if title_task is not None:
                try:
                    title = await title_task
                    with _pg_conn() as conn:
                        conn.execute(
                            f"UPDATE {SESSIONS_TABLE} SET title = %s "
                            f"WHERE uid = %s AND session_id = %s AND deleted = false AND title = ''",
                            (title, uid, session_id),
                        )
                        conn.commit()
                    logger.info(f"[SessionTitle] 首轮标题已生成: '{title}' sid={session_id[:8]}")
                except Exception as title_err:
                    logger.warning(f"[SessionTitle] 首轮标题生成失败: {title_err}")

        except Exception as e:
            logger.error(f"[ChatStream] 流式输出异常: {str(e)}")
            yield f"data: {json.dumps({'type': 'content', 'content': FALLBACK_SYSTEM_ERROR})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'sources': []})}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")


# ================= 用户反馈（点赞/点踩） =================
@router.post("/conversation/judgement")
async def conversation_judgement(session_id: str = Body(...),
                                 turn_count: int = Body(...),
                                 judgement: str = Body(...),
                                 detail: str = Body("", embed=True),
                                 current_user: TokenData = Depends(get_current_user)):
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
async def session_clear(session_id: str = Body(...), current_user: TokenData = Depends(get_current_user)):
    uid = current_user.uid
    if not _verify_session_owner(uid, session_id):
        return {"success": False, "error": "无权操作该会话"}
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
        "query": None,
        "user": None,
        "current_time": _now_time(),
        "language": "中文",
        "scene": None,
        "scenes": None,
        "route": None,
        "file_descriptions": None,
        "planned_top_k": None,
        "search_strategy": None,
        "target_files": None,
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


# ================= Session 管理辅助函数 =================

MAX_SESSIONS_PER_USER = 10

def _create_session_row(uid: str, session_id: str) -> None:
    """在 myagent_sessions 表中插入新 session 行。"""
    with _pg_conn() as conn:
        conn.execute(
            f"INSERT INTO {SESSIONS_TABLE} (uid, session_id) VALUES (%s, %s) "
            f"ON CONFLICT (uid, session_id) DO NOTHING",
            (uid, session_id),
        )
        conn.commit()

def _verify_session_owner(uid: str, session_id: str) -> bool:
    """校验 session 是否属于该 uid 且未删除。"""
    with _pg_conn() as conn:
        cur = conn.execute(
            f"SELECT 1 FROM {SESSIONS_TABLE} "
            f"WHERE uid = %s AND session_id = %s AND deleted = false",
            (uid, session_id),
        )
        return cur.fetchone() is not None

def _enforce_session_limit(uid: str) -> None:
    """超10个时软删最旧的 session（含 checkpoint 清除）。"""
    with _pg_conn() as conn:
        cur = conn.execute(
            f"SELECT session_id FROM {SESSIONS_TABLE} "
            f"WHERE uid = %s AND deleted = false ORDER BY last_message_at ASC",
            (uid,),
        )
        rows = cur.fetchall()
        if len(rows) >= MAX_SESSIONS_PER_USER:
            excess = rows[:len(rows) - MAX_SESSIONS_PER_USER + 1]
            for (sid,) in excess:
                conn.execute(
                    f"UPDATE {SESSIONS_TABLE} SET deleted = true WHERE uid = %s AND session_id = %s",
                    (uid, sid),
                )
            conn.commit()
            for (sid,) in excess:
                try:
                    cfg = _make_config(sid)
                    empty: ChatState = {
                        "session_id": sid, "messages": [], "context": "", "sources": [],
                        "mode": "unknown", "final_answer": "", "turn_count": 0,
                        "query": None, "user": None,
                        "current_time": "", "scene": None, "scenes": None,
                    }
                    graph.update_state(cfg, empty)
                except Exception as e:
                    logger.warning(f"[SessionLimit] 清除旧 checkpoint 失败 sid={sid[:8]}: {e}")

async def _generate_session_title(user_message: str) -> str:
    """用 fast LLM 生成10字以内标题。"""
    try:
        llm = get_fast_llm_client()
        resp = await llm.ainvoke([
            SystemMessage(content="你是标题生成器。请为用户的提问生成一个10字以内的中文标题，只输出标题文字，不要加引号、标点或换行。"),
            HumanMessage(content=user_message),
        ])
        log_token_usage("session_title", resp, config.get("FAST_MODEL", ""))
        title = resp.content.strip().replace("\n", "").replace("\"", "").replace("'", "")[:10]
        return title if title else user_message[:10]
    except Exception as e:
        logger.warning(f"[SessionTitle] 标题生成失败，降级为截取: {e}")
        return user_message[:10]

def _update_session_timestamp(uid: str, session_id: str, turn_count: int) -> None:
    """每轮结束后更新 session 的时间戳和轮数。"""
    with _pg_conn() as conn:
        conn.execute(
            f"UPDATE {SESSIONS_TABLE} "
            f"SET last_message_at = NOW(), turn_count = %s "
            f"WHERE uid = %s AND session_id = %s AND deleted = false",
            (turn_count, uid, session_id),
        )
        conn.commit()


# ================= Session 管理 API =================

@router.get("/sessions")
async def list_sessions(current_user: TokenData = Depends(get_current_user)):
    """返回当前用户的 session 列表（最多10个，按最后消息时间降序）。"""
    uid = current_user.uid
    try:
        with _pg_conn() as conn:
            cur = conn.execute(
                f"SELECT session_id, title, last_message_at, turn_count "
                f"FROM {SESSIONS_TABLE} "
                f"WHERE uid = %s AND deleted = false ORDER BY last_message_at DESC",
                (uid,),
            )
            rows = cur.fetchall()
        sessions = [
            {
                "session_id": r[0],
                "title": r[1] or "新对话",
                "last_message_at": r[2].isoformat() if r[2] else None,
                "turn_count": r[3],
            }
            for r in rows
        ]
        return {"code": 200, "data": sessions}
    except Exception as e:
        logger.error(f"[Sessions] 获取列表失败 uid={uid}: {e}")
        return {"code": 500, "message": str(e), "data": []}


@router.get("/session/{session_id}/messages")
async def get_session_messages(session_id: str, current_user: TokenData = Depends(get_current_user)):
    """从对话表读取该 session 的所有消息（严格校验 uid 归属）。"""
    uid = current_user.uid
    if not _verify_session_owner(uid, session_id):
        return {"code": 403, "message": "无权访问该会话", "data": []}
    try:
        with _pg_conn() as conn:
            cur = conn.execute(
                f"SELECT role, content, turn_count, sources, related_questions, user_judgement, judgement_detail "
                f"FROM {CONV_TABLE} "
                f"WHERE uid = %s AND session_id = %s ORDER BY id ASC",
                (uid, session_id),
            )
            rows = cur.fetchall()
        messages = [
            {
                "role": r[0],
                "content": r[1],
                "turn_count": r[2],
                "sources": r[3] if r[3] else [],
                "related_questions": r[4] if r[4] else [],
                "judgement": r[5] or "",
                "judgement_detail": r[6] or "",
            }
            for r in rows
        ]
        return {"code": 200, "data": messages}
    except Exception as e:
        logger.error(f"[SessionMessages] 获取失败 uid={uid} sid={session_id[:8]}: {e}")
        return {"code": 500, "message": str(e), "data": []}


@router.patch("/session/{session_id}/title")
async def update_session_title(
    session_id: str,
    title: str = Body(..., embed=True),
    current_user: TokenData = Depends(get_current_user),
):
    """用户修改 session 标题。"""
    uid = current_user.uid
    if not _verify_session_owner(uid, session_id):
        return {"code": 403, "message": "无权修改该会话"}
    title = title.strip()[:50]
    if not title:
        return {"code": 400, "message": "标题不能为空"}
    try:
        with _pg_conn() as conn:
            conn.execute(
                f"UPDATE {SESSIONS_TABLE} SET title = %s "
                f"WHERE uid = %s AND session_id = %s AND deleted = false",
                (title, uid, session_id),
            )
            conn.commit()
        return {"code": 200, "data": {"session_id": session_id, "title": title}}
    except Exception as e:
        logger.error(f"[SessionTitle] 修改失败 uid={uid} sid={session_id[:8]}: {e}")
        return {"code": 500, "message": str(e)}


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, current_user: TokenData = Depends(get_current_user)):
    """软删 session + 清 checkpoint。"""
    uid = current_user.uid
    if not _verify_session_owner(uid, session_id):
        return {"code": 403, "message": "无权删除该会话"}
    try:
        with _pg_conn() as conn:
            conn.execute(
                f"UPDATE {SESSIONS_TABLE} SET deleted = true "
                f"WHERE uid = %s AND session_id = %s",
                (uid, session_id),
            )
            conn.commit()
        try:
            cfg = _make_config(session_id)
            empty: ChatState = {
                "session_id": session_id, "messages": [], "context": "", "sources": [],
                "mode": "unknown", "final_answer": "", "turn_count": 0,
                "query": None, "user": None,
                "current_time": "", "scene": None, "scenes": None,
            }
            graph.update_state(cfg, empty)
        except Exception as e:
            logger.warning(f"[SessionDelete] 清除 checkpoint 失败 sid={session_id[:8]}: {e}")
        return {"code": 200, "data": {"session_id": session_id}}
    except Exception as e:
        logger.error(f"[SessionDelete] 删除失败 uid={uid} sid={session_id[:8]}: {e}")
        return {"code": 500, "message": str(e)}