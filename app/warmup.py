"""
统一预热模块 —— 在 FastAPI startup 时将所有懒加载的客户端/连接池提前初始化，
确保首个用户请求零冷启动延迟。

预热策略：
  1. 同步对象（LLM 单例、Jev 客户端等）直接调用 get_xxx() 触发实例化
  2. 异步对象（httpx.AsyncClient、AsyncOpenAI）用 asyncio.ensure_future 并发预热
  3. 外部 API 连接（Embedding、LLM、Rerank）发送一个最小 ping 请求建立 TCP/TLS 连接
  4. DB/Redis 连接池执行一条简单查询验证连通性

与高并发的兼容性：
  - 预热在 startup 事件中执行，uvicorn 尚未开始接受请求，无并发冲突
  - 预热只做"初始化"和"建连"，不执行业务逻辑，不占用后续请求处理能力
  - 连接池预热后是复用的（httpx max_connections 是上限，预热用 1 条，后续请求共享池）
  - 多 worker 各自独立预热
"""

import os
import time
import asyncio
import logging
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

logger = logging.getLogger("Warmup")
if not logger.handlers:
    import os as _os
    from pathlib import Path as _Path
    from concurrent_log_handler import ConcurrentRotatingFileHandler
    _formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    _console = logging.StreamHandler()
    _console.setFormatter(_formatter)
    logger.addHandler(_console)
    _log_dir = _Path(_os.getenv("WORK_PATH", "/app")) / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    _file = ConcurrentRotatingFileHandler(
        _log_dir / "warmup.log", "a", 10 * 1024 * 1024, 5,
        encoding='utf-8', lock_file_directory="/tmp",
    )
    _file.setFormatter(_formatter)
    logger.addHandler(_file)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _log_elapsed(name: str, t0: float, ok: bool, err: str = ""):
    elapsed = time.time() - t0
    status = "OK" if ok else "FAIL"
    msg = f"[Warmup] {name} {status} ({elapsed:.3f}s)"
    if err:
        msg += f" — {err}"
    if ok:
        logger.info(msg)
    else:
        logger.error(msg)


# ================================================================
# 第一层：同步对象预热（LLM 客户端单例、Jev 客户端）
# ================================================================

def _warmup_llm_clients():
    """预热主 LLM + Fast LLM 客户端单例（ChatOpenAI + httpx.Client 连接池）。"""
    from calculate_logic.rag.generate_answer import get_llm_client, get_fast_llm_client
    t0 = time.time()
    try:
        get_llm_client()
        get_fast_llm_client()
        _log_elapsed("LLM客户端(main+fast)", t0, True)
    except Exception as e:
        _log_elapsed("LLM客户端(main+fast)", t0, False, str(e))


def _warmup_jev_client():
    """预热 Jev (TypeSafe) 客户端单例。"""
    from calculate_logic.rag.generate_answer import get_jev_client
    t0 = time.time()
    try:
        get_jev_client()
        _log_elapsed("Jev(TypeSafe)客户端", t0, True)
    except Exception as e:
        _log_elapsed("Jev(TypeSafe)客户端", t0, False, str(e))


def _warmup_retriever_pool():
    """预热检索器 PG 连接池（懒加载 _get_pool → ConnectionPool open=True）。"""
    from calculate_logic.rag.generate_answer import retriever
    t0 = time.time()
    try:
        retriever._get_pool()
        _log_elapsed("检索器PG连接池", t0, True)
    except Exception as e:
        _log_elapsed("检索器PG连接池", t0, False, str(e))


def _warmup_embedding_pg_pool():
    """预热入库 PG 连接池（懒加载 _get_pg_pool → ConnectionPool open=True）。"""
    from calculate_logic.chunk_pipeline.embedding import _get_pg_pool
    t0 = time.time()
    try:
        _get_pg_pool()
        _log_elapsed("入库PG连接池", t0, True)
    except Exception as e:
        _log_elapsed("入库PG连接池", t0, False, str(e))


def _warmup_conversation_pg_pool():
    """预热对话持久化 PG 连接池（懒加载 _get_pg_pool → ConnectionPool open=True）。
    file_tagging 复用此池，预热后打标请求也零冷启动。"""
    from calculate_logic.rag.generate_answer import _get_pg_pool
    t0 = time.time()
    try:
        _get_pg_pool()
        _log_elapsed("对话持久化PG连接池", t0, True)
    except Exception as e:
        _log_elapsed("对话持久化PG连接池", t0, False, str(e))


def _warmup_redis_pool():
    """预热 Redis 连接池（懒加载 _get_redis → ConnectionPool）。"""
    from sso_did.auth import _get_redis
    t0 = time.time()
    try:
        r = _get_redis()
        r.ping()
        _log_elapsed("Redis连接池", t0, True)
    except Exception as e:
        _log_elapsed("Redis连接池", t0, False, str(e))


# ================================================================
# 第二层：异步对象预热（AsyncClient 实例化 + 外部 API TCP/TLS 建连）
# ================================================================

async def _warmup_embedding_api():
    """预热 Embedding 后端。

    - local 后端：触发模型首次加载（懒加载→预热加载），避免首次请求慢
    - api   后端：实例化 AsyncOpenAI + 发送 1 条最小 embedding 请求建连
    """
    from calculate_logic.rag.generate_answer import retriever
    t0 = time.time()
    try:
        embeddings = await retriever.embedding_client.text_embedding(["预热"])
        _log_elapsed(f"Embedding 后端(返回{len(embeddings)}条)", t0, True)
    except Exception as e:
        _log_elapsed("Embedding 后端", t0, False, str(e))


async def _warmup_rerank_api():
    """预热 Rerank 后端。

    - local 后端：触发模型首次加载
    - api   后端：实例化 httpx.AsyncClient + 发送 1 条最小 rerank 请求建连
    启动瞬间网络栈可能未完全就绪，连接失败时自动重试 1 次（间隔 2s）。
    即使预热失败也不阻断启动，首次检索时会再次建连。
    """
    from calculate_logic.rag.generate_answer import retriever
    t0 = time.time()
    try:
        if retriever.rerank_client is None:
            _log_elapsed("Rerank API(未配置，跳过)", t0, True)
            return
        for attempt in range(2):
            try:
                scores = await retriever.rerank_client.arerank("预热", ["预热文本"])
                _log_elapsed(f"Rerank API(返回{len(scores)}条)" + (f" 第{attempt+1}次尝试" if attempt else ""), t0, True)
                return
            except Exception:
                if attempt == 0:
                    logger.warning("[Warmup] Rerank API 首次连接失败，2s 后重试...")
                    await asyncio.sleep(2)
                else:
                    raise
    except Exception as e:
        _log_elapsed("Rerank API", t0, False, str(e))


async def _warmup_mineru_client():
    """预热 MinerU httpx.AsyncClient + VLM AsyncOpenAI 客户端。

    只创建客户端实例（建连由首次实际请求触发，因 MinerU API 可能有鉴权验证），
    避免 startup 时对第三方 API 发无意义请求。
    """
    from calculate_logic.fetch_knowledge.miner_u_parse import _get_mineru_http, _get_vlm_async_client
    t0 = time.time()
    try:
        await _get_mineru_http()
        await _get_vlm_async_client()
        _log_elapsed("MinerU+VLM客户端", t0, True)
    except Exception as e:
        _log_elapsed("MinerU+VLM客户端", t0, False, str(e))


async def _warmup_llm_api_ping():
    """预热主 LLM API 的 TCP/TLS 连接：发送 1 个最小 completion 请求。

    ChatOpenAI 的 httpx.Client 是同步的，用 to_thread 避免阻塞事件循环。
    """
    from calculate_logic.rag.generate_answer import get_llm_client
    t0 = time.time()
    try:
        llm = get_llm_client()
        def _ping():
            return llm.invoke("hi", config={"max_tokens": 1})
        await asyncio.to_thread(_ping)
        _log_elapsed("LLM API ping", t0, True)
    except Exception as e:
        _log_elapsed("LLM API ping", t0, False, str(e))


async def _warmup_fast_llm_api_ping():
    """预热 Fast LLM API 的 TCP/TLS 连接。"""
    from calculate_logic.rag.generate_answer import get_fast_llm_client
    t0 = time.time()
    try:
        llm = get_fast_llm_client()
        def _ping():
            return llm.invoke("hi", config={"max_tokens": 1})
        await asyncio.to_thread(_ping)
        _log_elapsed("Fast LLM API ping", t0, True)
    except Exception as e:
        _log_elapsed("Fast LLM API ping", t0, False, str(e))


# ================================================================
# 统一入口
# ================================================================

def run_sync_warmup():
    """执行所有同步预热（LLM 单例、连接池实例化）。

    在 startup 事件中同步调用，确保第一个请求到达前所有单例已就绪。
    """
    logger.info("[Warmup] 同步预热开始")
    _warmup_llm_clients()
    _warmup_jev_client()
    _warmup_retriever_pool()
    _warmup_embedding_pg_pool()
    _warmup_conversation_pg_pool()
    _warmup_redis_pool()
    logger.info("[Warmup] 同步预热完成")


async def run_async_warmup():
    """执行所有异步预热（外部 API TCP/TLS 建连）。

    并发执行所有异步预热任务，控制并发度避免 startup 过慢。
    在 startup 事件中 await 调用。
    """
    logger.info("[Warmup] 异步预热开始")

    tasks = [
        _warmup_embedding_api(),
        _warmup_rerank_api(),
        _warmup_llm_api_ping(),
        _warmup_fast_llm_api_ping(),
        _warmup_mineru_client(),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            task_name = tasks[i].__name__ if hasattr(tasks[i], '__name__') else f"task_{i}"
            logger.error(f"[Warmup] 异步预热任务 {task_name} 失败: {result}")

    logger.info("[Warmup] 异步预热完成，服务就绪")
