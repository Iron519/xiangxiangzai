import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np
import psycopg
from concurrent_log_handler import ConcurrentRotatingFileHandler
from dotenv import find_dotenv, load_dotenv
from openai import AsyncOpenAI
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from calculate_logic.rag.tokenizer import tokenize_for_tsquery

load_dotenv(find_dotenv())

# ================= 路径配置及 import 内部功能 =================
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "retrieval_rerank.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logger = logging.getLogger("RetrievalRerank")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = ConcurrentRotatingFileHandler(
        LOG_PATH, "a", 10 * 1024 * 1024, 5,
        encoding='utf-8', lock_file_directory="/tmp",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.setLevel(logging.INFO)


# ================= 文本清理工具 =================
# 匹配 Markdown 图片 ![alt](url) —— 去除空格容错
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\s*\([^)]+\)\s*")


def _strip_md_images(text: str) -> str:
    """把文本里的 Markdown 图片标记全部移除，保留其它内容。"""
    if not text:
        return ""
    return _MD_IMG_RE.sub("", text)


# ================= 配置参数 =================
PG_HOST = os.getenv("PG_HOST", "")               # PostgreSQL 主机地址
PG_PORT = int(os.getenv("PG_PORT", "5432"))      # PostgreSQL 端口
PG_DATABASE = os.getenv("PG_DATABASE", "")       # PostgreSQL 数据库名
PG_USER = os.getenv("PG_USER", "")               # PostgreSQL 用户名
PG_PASSWORD = os.getenv("PG_PASSWORD", "")       # PostgreSQL 密码
TABLE_NAME = os.getenv("PG_MYAGENT_KB_TABLE", "")  # 知识库表名
TS_CONFIG = os.getenv("TS_CONFIG", "")           # 全文检索中文分词配置（如 jiebacfg）
BASE_URL = os.getenv("BASE_URL", "")             # LLM / Embedding / Rerank 服务基础地址
ACCESSCODE = os.getenv("ACCESSCODE", "")         # API 鉴权密钥

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")  # Embedding 模型名称
RERANK_MODEL = os.getenv("RERANK_MODEL", "")     # Rerank 模型名称
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "4096"))    # Embedding 向量维度（qwen3-embedding-8b 原生 4096）
MAX_TEXT_LENGTH = 1024                           # Embedding 输入文本最大字符数
EMBEDDING_TIMEOUT = 30.0                         # Embedding 请求超时（秒）
RERANK_TIMEOUT = 30.0                            # Rerank 请求超时（秒）
VERIFY_SSL = False                               # 是否验证 SSL 证书（False 则跳过，用于内网自签名证书）

SEARCH_VECTOR_WEIGHT = 0.6                       # 向量语义检索权重：越大越依赖语义相似度
SEARCH_KEYWORD_WEIGHT = 0.4                      # 关键词检索权重：越大越依赖字面匹配
SEARCH_CANDIDATE_TOP_K = 20                      # 向量/关键词各自初选候选数（取 top-K 再 RRF 融合）
SEARCH_COARSE_TOP_K = 20                         # RRF 融合后返回的粗排条数（送入 reranker 的候选数）
SEARCH_RERANK_TOP_K = 3                          # rerank 后最终返回条数（fixed 模式下；dynamic 模式仅为上限）
SEARCH_RRF_K = 30                                # RRF 融合常数 = weight/(k+rank)：k 越大排名差异的分数影响越小；30 让 top 排名差异更显著

SEARCH_DYNAMIC_TOPK = True                       # 是否启用动态 top-K（断崖检测自动截断，否则固定取 RERANK_TOP_K 条）
SEARCH_FINAL_MIN_TOP_K = 2                       # 动态 top-K 最少保留条数
SEARCH_FINAL_MAX_TOP_K = 10                      # 动态 top-K 最多保留条数
SEARCH_RELATIVE_SCORE_RATIO = 0.25               # 相对分数兜底：保留下界 ≥ 最高分×ratio 的所有结果；0.25 宽松，保留更多弱相关结果
SEARCH_MIN_GAP = float("1e-5")                   # 断崖阈值：相邻分数差超过此值视为"崖"
SEARCH_GAP_RATIO = 1.5                           # 断崖判定倍率：最大间隙/其余平均间隙 ≥ 此值才触发；1.5 更难触发断崖，保留更多结果

MAX_CONCURRENT_SEARCHES = 10                     # 批量检索最大并发数


# ================= 数据模型 =================
class ChunkHit:
    """单条检索结果切片"""
    def __init__(self, chunk_index: int, child_content: str, file_path: str, file_name: str,
                 text_hash: str, is_qa: bool, metadata: dict, group_id: int, internal_id: int,
                 parent_content: str, related_content_summary: Optional[str] = None,
                 similarity: float = 0.0, rerank_score: Optional[float] = None,
                 upload_time: Optional[str] = None, update_time: Optional[str] = None,
                 expire_time: Optional[str] = None, image_url: Optional[str] = None):
        self.chunk_index = chunk_index
        self.child_content = child_content
        self.file_path = file_path
        self.file_name = file_name
        self.text_hash = text_hash
        self.is_qa = is_qa
        self.metadata = metadata
        self.group_id = group_id
        self.internal_id = internal_id
        self.parent_content = parent_content
        self.related_content_summary = related_content_summary
        self.similarity = similarity
        self.rerank_score = rerank_score
        self.upload_time = upload_time
        self.update_time = update_time
        self.expire_time = expire_time
        self.image_url = image_url


class HybridSearchResponse:
    """混合检索完整响应"""
    def __init__(self, query_text: str, query_time: str,
                 vector_weight: float, keyword_weight: float,
                 candidate_top_k: int, coarse_top_k: int,
                 rerank_top_k: int, rrf_k: int,
                 vector_mode: str, keyword_mode: str, keyword_config: str,
                 coarse_results: List[ChunkHit], coarse_contents: List[str],
                 final_results: List[ChunkHit], final_parent_contents: List[str],
                 adaptive_selected_count: int, adaptive_selection_reason: str):
        self.query_text = query_text
        self.query_time = query_time
        self.vector_weight = vector_weight
        self.keyword_weight = keyword_weight
        self.candidate_top_k = candidate_top_k
        self.coarse_top_k = coarse_top_k
        self.rerank_top_k = rerank_top_k
        self.rrf_k = rrf_k
        self.vector_mode = vector_mode
        self.keyword_mode = keyword_mode
        self.keyword_config = keyword_config
        self.coarse_results = coarse_results
        self.coarse_contents = coarse_contents
        self.final_results = final_results
        self.final_parent_contents = final_parent_contents
        self.adaptive_selected_count = adaptive_selected_count
        self.adaptive_selection_reason = adaptive_selection_reason


# ================= Embedding Client =================
class CustomEmbeddingClient:
    """调用外部 embedding 服务（OpenAI SDK 风格，与入库侧 chunk_pipeline/embedding.py 一致）"""
    def __init__(self, config: Dict[str, Any]) -> None:
        self.url = config.get("url", "")
        self.model = config.get("model", "")
        self.accesscode = config.get("accesscode", "")
        self.max_text_length = int(config.get("max_text_length", 1024))
        self.timeout = float(config.get("timeout", 30))
        self.verify_ssl = config.get("verify_ssl", True)
        if isinstance(self.verify_ssl, str):
            self.verify_ssl = self.verify_ssl.lower() == "true"

        # OpenAI 风格异步客户端：base_url 自动拼接 /embeddings，鉴权自动带 Bearer 前缀
        self.async_client = AsyncOpenAI(
            api_key=self.accesscode,
            base_url=self.url,
            http_client=httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            ),
        )

        logger.info(f"Embedding初始化: model={self.model}")

    async def text_embedding(self, texts: List[str]) -> List:
        """向量化。返回 np.ndarray 列表（每元素 float32）。"""
        clean = [t[:self.max_text_length] for t in texts if t and t.strip()]
        if not clean:
            return []
        # 直接取模型原生维度（qwen3-embedding-8b 为 4096，不支持 dimensions 降维，
        # 故不传 dimensions），与入库侧及 PG embedding vector(4096) 保持一致。
        try:
            response = await self.async_client.embeddings.create(
                model=self.model,
                input=clean,
            )
            embeddings = [np.array(it.embedding, dtype=np.float32) for it in response.data if it.embedding]
            return embeddings
        except Exception as e:
            logger.error(f"Embedding API 调用失败: {e}")
            return []

    async def close(self) -> None:
        await self.async_client.close()


# ================= Rerank Client =================
class CustomRerankClient:
    """调用外部 rerank 服务（OpenAI SDK 风格，走 /rerank 端点）"""
    def __init__(self, config: Dict[str, Any]) -> None:
        self.url = config.get("url", "")
        self.model = config.get("model", "")
        self.accesscode = config.get("accesscode", "")
        self.timeout = float(config.get("timeout", 30))
        self.verify_ssl = config.get("verify_ssl", True)
        if isinstance(self.verify_ssl, str):
            self.verify_ssl = self.verify_ssl.lower() == "true"

        # OpenAI 风格异步客户端：复用其鉴权(Bearer)与连接管理，请求相对路径 /rerank
        self.async_client = AsyncOpenAI(
            api_key=self.accesscode,
            base_url=self.url,
            http_client=httpx.AsyncClient(
                verify=self.verify_ssl,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            ),
        )

        if self.url:
            logger.info(f"Rerank初始化: url={self.url}, model={self.model}")

    def _build_payload(self, query: str, texts: List[str]) -> Dict[str, Any]:
        # OpenAI rerank 规约：documents 字段，非 texts
        payload = {"query": query, "documents": texts, "top_n": len(texts)}
        if self.model:
            payload["model"] = self.model
        return payload

    @staticmethod
    def _parse_scores(body: Any) -> Dict[int, float]:
        items = []
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            items = body.get("data") or body.get("results") or body.get("output") or []

        scores = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            score = item.get("score", item.get("relevance_score"))
            if idx is not None and score is not None:
                scores[int(idx)] = float(score)
        return scores

    async def arerank(self, query: str, texts: List[str]) -> Dict[int, float]:
        """异步 rerank"""
        if not self.url or not texts:
            return {}
        try:
            response = await self.async_client.post(
                "/rerank",
                cast_to=httpx.Response,
                body=self._build_payload(query, texts),
            )
            response.raise_for_status()
            return self._parse_scores(response.json())
        except Exception as e:
            logger.warning(f"Rerank API 请求失败: {e}")
            return {}

    async def close(self) -> None:
        await self.async_client.close()


# ================= 关键词预处理（共享 tokenizer，与入库端一致） =================
# 入库端用 tokenize_for_tsv → to_tsvector('simple', ...)
# 检索端用 tokenize_for_tsquery → to_tsquery('simple', ...)
# 两端共享同一份 jieba 自定义词典，保证 tsv 存储与查询分词完全一致。


# ================= PgHybridRetriever =================
class PgHybridRetriever:
    """PostgreSQL 混合检索器"""
    def __init__(self, db_config: Dict[str, Any], embedding_client: CustomEmbeddingClient,
                 rerank_client: Optional[CustomRerankClient] = None,
                 table_name: str = TABLE_NAME, ts_config: str = TS_CONFIG):
        self.db_config = {
            "host": db_config["host"],
            "dbname": db_config["dbname"],
            "user": db_config.get("user") or db_config.get("user_name"),
            "password": db_config["password"],
            "port": int(db_config.get("port", 5432)),
            "row_factory": dict_row,
        }
        self.embedding_client = embedding_client
        self.rerank_client = rerank_client
        self.table_name = self._validate(table_name)
        self.ts_config = self._validate(ts_config)
        logger.info(f"检索器初始化: table={self.table_name}, ts_config={self.ts_config}")

    @classmethod
    def from_env(cls, table_name: str = TABLE_NAME, ts_config: str = TS_CONFIG) -> "PgHybridRetriever":
        return cls(
            db_config={
                "host": PG_HOST,
                "port": PG_PORT,
                "dbname": PG_DATABASE,
                "user": PG_USER,
                "password": PG_PASSWORD,
            },
            embedding_client=CustomEmbeddingClient({
                "url": BASE_URL,
                "model": EMBEDDING_MODEL,
                "accesscode": ACCESSCODE,
                "max_text_length": MAX_TEXT_LENGTH,
                "timeout": EMBEDDING_TIMEOUT,
                "verify_ssl": VERIFY_SSL,
            }),
            rerank_client=CustomRerankClient({
                "url": BASE_URL,
                "model": RERANK_MODEL,
                "accesscode": ACCESSCODE,
                "timeout": RERANK_TIMEOUT,
                "verify_ssl": VERIFY_SSL,
            }) if BASE_URL else None,
            table_name=table_name,
            ts_config=ts_config,
        )

    # ----------------- 数据库检索 -----------------
    def _get_conn(self) -> psycopg.Connection:
        conn = psycopg.connect(**self.db_config)
        register_vector(conn)
        return conn

    def _coarse_search(self, conn: psycopg.Connection, query_text: str, query_time: datetime,
                       query_embedding: List[float], vector_weight: float, keyword_weight: float,
                       candidate_top_k: int, coarse_top_k: int, rrf_k: int) -> List[Dict[str, Any]]:
        # Python jieba 预分词（与入库端共享 tokenizer），用 simple 配置保证整词匹配
        tsquery_text = tokenize_for_tsquery(query_text)

        params = {
            "query_text": query_text,
            "query_time": query_time,
            "query_embedding": Vector(query_embedding),
            "vector_weight": vector_weight,
            "keyword_weight": keyword_weight,
            "candidate_top_k": candidate_top_k,
            "coarse_top_k": coarse_top_k,
            "rrf_k": rrf_k,
            "tsquery_text": tsquery_text,
        }

        # 关键词检索：如果 jieba 分词结果非空，用 to_tsquery 精确 OR 匹配；
        # 否则回退到空结果集（不影响纯向量检索）。
        keyword_cte = f"""
        keyword_hits AS (
            SELECT chunk_index,
                   row_number() OVER (ORDER BY keyword_rank_score ASC, update_time DESC NULLS LAST) AS keyword_rank
            FROM (
                SELECT c.chunk_index, c.update_time,
                       ts_rank(c.content_tsv, to_tsquery('simple', %(tsquery_text)s), 32) AS keyword_rank_score
                FROM {self.table_name} c
                WHERE c.content_tsv IS NOT NULL AND c.content_tsv @@ to_tsquery('simple', %(tsquery_text)s)
                  AND (c.expire_time IS NULL OR c.expire_time > %(query_time)s)
                ORDER BY keyword_rank_score DESC, c.update_time DESC NULLS LAST
                LIMIT %(candidate_top_k)s
            ) ranked_keyword
        ),
        """ if tsquery_text else """
        keyword_hits AS (
            SELECT NULL::bigint AS chunk_index, NULL::bigint AS keyword_rank WHERE FALSE
        ),
        """

        sql = f"""
        WITH vector_hits AS (
            SELECT chunk_index,
                   row_number() OVER (ORDER BY vector_distance ASC, update_time DESC NULLS LAST) AS vector_rank
            FROM (
                SELECT c.chunk_index, c.update_time,
                       c.embedding <=> %(query_embedding)s AS vector_distance
                FROM {self.table_name} c
                WHERE c.embedding IS NOT NULL
                  AND (c.expire_time IS NULL OR c.expire_time > %(query_time)s)
                ORDER BY vector_distance ASC, c.update_time DESC NULLS LAST
                LIMIT %(candidate_top_k)s
            ) ranked_vector
        ),
        {keyword_cte}
        merged AS (
            SELECT COALESCE(v.chunk_index, k.chunk_index) AS chunk_index,
                   COALESCE(%(vector_weight)s / (%(rrf_k)s + v.vector_rank), 0.0)
                   + COALESCE(%(keyword_weight)s / (%(rrf_k)s + k.keyword_rank), 0.0) AS hybrid_score
            FROM vector_hits v FULL OUTER JOIN keyword_hits k USING (chunk_index)
        )
         SELECT c.chunk_index, c.child_content, c.file_path, c.file_name, c.text_hash,
                FALSE::boolean AS is_qa, c.metadata, c.group_id, c.internal_id,
                c.parent_content, NULL::text AS related_content_summary,
                c.related_image_url, m.hybrid_score AS similarity,
                c.upload_time, c.update_time, c.expire_time, c.if_show
        FROM merged m JOIN {self.table_name} c ON c.chunk_index = m.chunk_index
        ORDER BY similarity DESC, c.update_time DESC NULLS LAST
        LIMIT %(coarse_top_k)s
        """

        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def _sync_coarse(self, query_text: str, query_time: datetime, query_embedding: List[float],
                     vector_weight: float, keyword_weight: float,
                     candidate_top_k: int, coarse_top_k: int, rrf_k: int) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            return self._coarse_search(conn, query_text, query_time, query_embedding,
                                       vector_weight, keyword_weight, candidate_top_k, coarse_top_k, rrf_k)

    # ----------------- 结果处理 -----------------
    @staticmethod
    def _fmt_time(value: Any) -> Any:
        """datetime 转 ISO 字符串；非 datetime 原样返回。"""
        return value.isoformat() if isinstance(value, datetime) else value

    def _row_to_hit(self, row: Dict[str, Any]) -> ChunkHit:
        md = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
        if_show = row.get("if_show") is not False
        child = row["child_content"]
        parent = row["parent_content"]
        if not if_show:
            child = _strip_md_images(child)
            parent = _strip_md_images(parent)
        return ChunkHit(
            chunk_index=int(row["chunk_index"]),
            child_content=child,
            file_path=row["file_path"],
            file_name=row["file_name"],
            text_hash=row["text_hash"],
            is_qa=bool(row["is_qa"]),
            metadata=md,
            group_id=int(row["group_id"]),
            internal_id=int(row["internal_id"]),
            parent_content=parent,
            related_content_summary=row.get("related_content_summary"),
            similarity=float(row["similarity"]),
            upload_time=self._fmt_time(row.get("upload_time")),
            update_time=self._fmt_time(row.get("update_time")),
            expire_time=self._fmt_time(row.get("expire_time")),
            image_url=(None if not if_show else (row.get("related_image_url") or None)),
        )

    def _dedup(self, hits: List[ChunkHit], top_k: Optional[int] = None) -> List[ChunkHit]:
        """按 file_name+group_id 语义组去重（同组只保留最高分块），text_hash 做二级去重。"""
        selected, seen_group, seen_hash = [], set(), set()
        for h in hits:
            group_key = f"{h.file_name}:{h.group_id}"
            if group_key in seen_group:
                continue
            if h.text_hash in seen_hash:
                continue
            seen_group.add(group_key)
            seen_hash.add(h.text_hash)
            selected.append(h)
            if top_k is not None and len(selected) >= top_k:
                break
        return selected

    def _cliff_select(self, hits: List[ChunkHit], min_k: int = SEARCH_FINAL_MIN_TOP_K,
                      max_k: int = SEARCH_FINAL_MAX_TOP_K, fallback_k: int = SEARCH_RERANK_TOP_K,
                      ratio: float = SEARCH_RELATIVE_SCORE_RATIO,
                      min_gap: float = SEARCH_MIN_GAP,
                      gap_ratio: float = SEARCH_GAP_RATIO) -> Tuple[List[ChunkHit], str]:
        """
        基于相邻分数断崖的动态 Top-K。
        scores = [0.91, 0.88, 0.86, 0.84, 0.51, 0.49]
        gaps   = [0.03, 0.02, 0.02, 0.33, 0.02]  →  最大 gap=0.33 → 返回前 4 条
        触发条件（需同时满足）：
        1. max_gap > SEARCH_MIN_GAP（默认 1e-5）
        2. 截断位置 ≥ min_k（默认 1）
        3. 最大间隙 ≥ 其他平均间隙 × gap_ratio（默认 2.0）

        若未触发断崖，依次尝试：
        - 相对分数兜底：保留 ≥ 最高分 × 0.72 的所有结果
        - Fallback：取默认的 rerank_top_k 条
        """
        if not hits:
            return [], "empty_hits"

        def _score(h: ChunkHit) -> float:
            return h.rerank_score if h.rerank_score is not None else h.similarity

        sorted_hits = sorted(hits, key=_score, reverse=True)
        scores = np.array([_score(h) for h in sorted_hits], dtype=float)
        n = len(sorted_hits)

        min_k = max(1, min(min_k, n))
        max_k = max(min_k, min(max_k, n))
        fallback_k = max(min_k, min(fallback_k, max_k))

        if n <= min_k:
            return sorted_hits, f"hit_count_le_min_top_k, selected={n}"

        if float(np.max(scores) - np.min(scores)) <= min_gap:
            k = min(fallback_k, n)
            return sorted_hits[:k], f"flat_scores, selected={k}"

        gaps = scores[:-1] - scores[1:]
        if len(gaps) > 0:
            max_idx = int(np.argmax(gaps))
            max_gap = float(gaps[max_idx])
            avg_other = float(np.mean(np.delete(gaps, max_idx))) if len(gaps) > 1 else 0.0
            pos = max_idx + 1

            cliff_hit = (
                max_gap > min_gap
                and pos >= min_k
                and (avg_other <= min_gap or max_gap >= avg_other * gap_ratio)
            )
            if cliff_hit:
                k = max(min_k, min(pos, max_k, n))
                return sorted_hits[:k], (
                    f"neighbor_score_cliff, max_gap={max_gap:.6f}, "
                    f"avg_other_gap={avg_other:.6f}, gap_ratio={gap_ratio}, selected={k}"
                )

        top_score = float(scores[0])
        if top_score > 0:
            threshold = top_score * ratio
            k = max(min_k, min(int(np.sum(scores >= threshold)), max_k, n))
            return sorted_hits[:k], (
                f"relative_score_fallback, ratio={ratio}, "
                f"threshold={threshold:.6f}, selected={k}"
            )

        k = min(fallback_k, n)
        return sorted_hits[:k], f"fallback_top_k, selected={k}"

    def _rerank_select(self, coarse_results: List[ChunkHit], rerank_top_k: int,
                       score_map: Optional[Dict[int, float]] = None) -> Tuple[List[ChunkHit], str]:
        """写入 rerank_score → 混合排序（rerank + 向量相似度）→ 动态 Top-K → 去重"""
        results = coarse_results

        if score_map:
            indexed = list(enumerate(coarse_results))
            for idx, hit in indexed:
                if idx in score_map:
                    hit.rerank_score = float(score_map[idx])

            # 混合排序：rerank 分数权重 0.7，向量相似度权重 0.3
            # 当 rerank 对中英混合文档打分偏低时，向量相似度能拉回语义相关的结果
            RERANK_WEIGHT = 0.7
            VECTOR_WEIGHT = 0.3

            def _hybrid_score(hit: ChunkHit) -> float:
                rs = hit.rerank_score if hit.rerank_score is not None else 0.0
                vs = hit.similarity if hit.similarity else 0.0
                return RERANK_WEIGHT * rs + VECTOR_WEIGHT * vs

            indexed.sort(key=lambda item: (_hybrid_score(item[1]), item[1].update_time or ""), reverse=True)
            results = [h for _, h in indexed]
        else:
            results.sort(key=lambda h: (h.similarity, h.update_time or ""), reverse=True)

        if SEARCH_DYNAMIC_TOPK:
            results, reason = self._cliff_select(results)
        else:
            results = results[:rerank_top_k]
            reason = f"dynamic_topk_disabled, fixed_top_k={rerank_top_k}"

        results = self._dedup(results, top_k=None)

        return results, reason

    def _build_resp(self, query_text: str, query_time: datetime,
                    vector_weight: float, keyword_weight: float,
                    candidate_top_k: int, coarse_top_k: int, rerank_top_k: int, rrf_k: int,
                    coarse_results: List[ChunkHit], final_results: List[ChunkHit],
                    reason: str) -> HybridSearchResponse:
        return HybridSearchResponse(
            query_text=query_text,
            query_time=query_time.isoformat(),
            vector_weight=vector_weight,
            keyword_weight=keyword_weight,
            candidate_top_k=candidate_top_k,
            coarse_top_k=coarse_top_k,
            rerank_top_k=rerank_top_k,
            rrf_k=rrf_k,
            vector_mode="pgvector",
            keyword_mode="rum",
            keyword_config="simple",
            coarse_results=coarse_results,
            coarse_contents=[i.child_content for i in coarse_results],
            final_results=final_results,
            final_parent_contents=[i.parent_content for i in final_results],
            adaptive_selected_count=len(final_results),
            adaptive_selection_reason=reason,
        )

    # ----------------- 检索入口 -----------------
    async def search(self, query_text: str, query_time: Optional[datetime] = None,
                     vector_weight: float = SEARCH_VECTOR_WEIGHT,
                     keyword_weight: float = SEARCH_KEYWORD_WEIGHT,
                     candidate_top_k: int = SEARCH_CANDIDATE_TOP_K,
                     coarse_top_k: int = SEARCH_COARSE_TOP_K,
                     rerank_top_k: int = SEARCH_RERANK_TOP_K,
                     rrf_k: int = SEARCH_RRF_K) -> HybridSearchResponse:
        logger.info(f"[search] 查询开始: query_text={query_text[:50]}...")

        query_text = query_text.strip()
        if not query_text:
            raise ValueError("query_text 不能为空")

        ws = vector_weight + keyword_weight
        if ws <= 0:
            raise ValueError("权重之和必须 > 0")
        vector_weight, keyword_weight = vector_weight / ws, keyword_weight / ws

        query_time = query_time or datetime.now(timezone.utc)
        if query_time.tzinfo is None:
            query_time = query_time.replace(tzinfo=timezone.utc)

        candidate_top_k = max(candidate_top_k, coarse_top_k, rerank_top_k, SEARCH_FINAL_MAX_TOP_K)

        embeddings = await self.embedding_client.text_embedding([query_text])
        if not embeddings:
            raise RuntimeError("查询向量化失败")
        query_emb = embeddings[0].astype(float).tolist()

        loop = asyncio.get_event_loop()
        rows = await loop.run_in_executor(
            None,
            lambda: self._sync_coarse(query_text, query_time, query_emb,
                                      vector_weight, keyword_weight, candidate_top_k, coarse_top_k, rrf_k),
        )

        coarse_results = [self._row_to_hit(r) for r in rows]

        score_map = {}
        if self.rerank_client is not None and coarse_results:
            # rerank 用 child_content（经过指代消解后语义完整），给 reranker 精准的语义匹配
            rerank_query = f"[Bilingual context: Chinese and English content may coexist] {query_text}"
            rerank_texts = [h.child_content for h in coarse_results if h.child_content]
            score_map = await self.rerank_client.arerank(rerank_query, rerank_texts)

        final_results, reason = self._rerank_select(coarse_results, rerank_top_k, score_map)

        logger.info(f"[search] 查询完成: 粗排{len(coarse_results)}条, 最终{len(final_results)}条, reason={reason}")
        return self._build_resp(query_text, query_time, vector_weight, keyword_weight,
                                candidate_top_k, coarse_top_k, rerank_top_k, rrf_k,
                                coarse_results, final_results, reason)

    # ----------------- 批量检索 -----------------
    async def batch_search(self, queries: List[str],
                           vector_weight: float = SEARCH_VECTOR_WEIGHT,
                           keyword_weight: float = SEARCH_KEYWORD_WEIGHT,
                           candidate_top_k: int = SEARCH_CANDIDATE_TOP_K,
                           coarse_top_k: int = SEARCH_COARSE_TOP_K,
                           rerank_top_k: int = SEARCH_RERANK_TOP_K,
                           rrf_k: int = SEARCH_RRF_K,
                           max_concurrent: int = MAX_CONCURRENT_SEARCHES) -> List[HybridSearchResponse]:
        logger.info(f"[batch_search] 查询开始: {len(queries)}个查询, 并发={max_concurrent}")

        sem = asyncio.Semaphore(max_concurrent)

        async def _one(query: str) -> HybridSearchResponse:
            async with sem:
                return await self.search(query, None, vector_weight, keyword_weight,
                                         candidate_top_k, coarse_top_k, rerank_top_k, rrf_k)

        raw = await asyncio.gather(*[_one(q) for q in queries], return_exceptions=True)
        results = []
        for q, r in zip(queries, raw):
            if isinstance(r, Exception):
                logger.error(f"[batch_search] 查询失败 '{q}': {r}")
            else:
                results.append(r)

        logger.info(f"[batch_search] 查询完成: 成功{len(results)}/{len(queries)}个")
        return results

    # ----------------- 工具方法 -----------------
    @staticmethod
    def _validate(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"非法标识符: {value}")
        return value


# ================= 本地测试 =================
if __name__ == "__main__":
    print("=" * 80)
    print("RAG 检索测试：邻值断崖动态 Top-K 版")
    print("=" * 80)

    async def main():
        retriever = PgHybridRetriever.from_env()
        result = await retriever.search("如何开放考勤权限？")

        print(f"\n查询: {result.query_text}")
        print(f"粗排数量: {len(result.coarse_results)}")
        print(f"最终数量: {len(result.final_results)}")
        print(f"动态选择原因: {result.adaptive_selection_reason}")

        print("\n粗排分数:")
        for i, hit in enumerate(result.coarse_results, 1):
            print(f"  [{i}] chunk_index={hit.chunk_index}, similarity={hit.similarity:.6f}, "
                  f"rerank_score={hit.rerank_score}, file={hit.file_name}")

        print("\n最终结果:")
        for i, hit in enumerate(result.final_results, 1):
            print(f"\n[{i}] {hit.file_name}")
            print(f"    chunk_index={hit.chunk_index}, similarity={hit.similarity:.6f}, "
                  f"rerank_score={hit.rerank_score}")
            print(f"    parent_content: {hit.parent_content[:800]}")

        await retriever.embedding_client.close()
        if retriever.rerank_client is not None:
            await retriever.rerank_client.close()

    asyncio.run(main())