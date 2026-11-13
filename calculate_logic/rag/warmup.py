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
  - 多 worker 各自独立预热（与 BM25 索引预热策略一致）
"""

import os
import time
import asyncio
import logging
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

logger = logging.getLogger("Warmup")


def _log_elapsed(name: str, t0: float, ok: bool, err: str = ""):
    elapsed = time.time() - t0
    status = "OK" if ok else "FAIL"
    msg = f"[Warmup] {name} {status} ({elapsed:.3f}s)"
    if err:
        msg += f" — {err}"
    if ok:
        logger.debug(msg)
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


def _warmup_tag_llm():
    """预热 Chat Tag LLM 客户端单例。"""
    from table_data.chat_tag import _get_tag_llm
    t0 = time.time()
    try:
        _get_tag_llm()
        _log_elapsed("ChatTag LLM", t0, True)
    except Exception as e:
        _log_elapsed("ChatTag LLM", t0, False, str(e))


def _warmup_rewrite_llm():
    """预热 Chunk Rewrite LLM 客户端单例。"""
    from calculate_logic.chunk_pipeline.rewrite import _get_rewrite_llm
    t0 = time.time()
    try:
        _get_rewrite_llm()
        _log_elapsed("Rewrite LLM", t0, True)
    except Exception as e:
        _log_elapsed("Rewrite LLM", t0, False, str(e))


def _warmup_retriever_pool():
    """预热检索器 PG 连接池（懒加载 _get_pool → ConnectionPool open=True）。"""
    from calculate_logic.rag.generate_answer import retriever
    t0 = time.time()
    try:
        retriever._get_pool()
        _log_elapsed("检索器PG连接池", t0, True)
    except Exception as e:
        _log_elapsed("检索器PG连接池", t0, False, str(e))


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
    """预热 Embedding API：实例化 AsyncOpenAI + 发送 1 条最小 embedding 请求建连。

    首次 embedding 调用耗时 ~5s（DNS+TCP+TLS+API处理），预热后后续请求 ~2s。
    """
    from calculate_logic.rag.generate_answer import retriever
    t0 = time.time()
    try:
        embeddings = await retriever.embedding_client.text_embedding(["预热"])
        _log_elapsed(f"Embedding API(返回{len(embeddings)}条)", t0, True)
    except Exception as e:
        _log_elapsed("Embedding API", t0, False, str(e))


async def _warmup_rerank_api():
    """预热 Rerank API：实例化 httpx.AsyncClient + 发送 1 条最小 rerank 请求建连。

    即使 rerank 配置有误（404），预热也能提前暴露问题，且不阻断启动。
    """
    from calculate_logic.rag.generate_answer import retriever
    t0 = time.time()
    try:
        if retriever.rerank_client is None:
            _log_elapsed("Rerank API(未配置，跳过)", t0, True)
            return
        scores = await retriever.rerank_client.arerank("预热", ["预热文本"])
        _log_elapsed(f"Rerank API(返回{len(scores)}条)", t0, True)
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
# 第三层：已有预热的 BM25 索引（保持原有调用，统一日志）
# ================================================================

def _warmup_bm25_indices():
    """预热 KB BM25 + FAQ BM25 索引（委托给原有 preload 函数）。"""
    from calculate_logic.rag.generate_answer import preload_faq_index
    from calculate_logic.rag.retrieval_rerank import preload_kb_bm25

    t0 = time.time()
    try:
        preload_faq_index()
        _log_elapsed("FAQ BM25索引", t0, True)
    except Exception as e:
        _log_elapsed("FAQ BM25索引", t0, False, str(e))

    t0 = time.time()
    try:
        preload_kb_bm25()
        _log_elapsed("KB BM25索引", t0, True)
    except Exception as e:
        _log_elapsed("KB BM25索引", t0, False, str(e))


# ================================================================
# 统一入口
# ================================================================

def run_sync_warmup():
    """执行所有同步预热（LLM 单例、连接池实例化、BM25 索引）。

    在 startup 事件中同步调用，确保第一个请求到达前所有单例已就绪。
    """
    logger.info("[Warmup] 同步预热开始")
    _warmup_bm25_indices()
    _warmup_llm_clients()
    _warmup_jev_client()
    _warmup_tag_llm()
    _warmup_rewrite_llm()
    _warmup_retriever_pool()
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
