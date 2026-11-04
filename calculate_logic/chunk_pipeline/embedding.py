import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx
import psycopg
from psycopg import sql
from openai import AsyncOpenAI

from calculate_logic.chunk_pipeline.config import logger
from calculate_logic.chunk_pipeline.chunk import SemanticChunkOutput
from calculate_logic.rag.tokenizer import tokenize_for_tsv

# ============================================================
# Embedding 模型调用（异步，带并发限流，避免与对话侧抢算力）
# ============================================================
# 上传侧批量向量化最多并发 2 批，给对话侧检索向量化留出算力
_embedding_sem = asyncio.Semaphore(2)


class EmbeddingModel:
    """OpenAI 风格 Embedding 客户端（参考 test.py 的 OpenAI 调用方式）。

    参数从 .env 读取：
    - ACCESSCODE     : access key（对应 OpenAI api_key）
    - BASE_URL       : OpenAI 兼容网关（对应 OpenAI base_url）
    - EMBEDDING_MODEL: 模型名，如 qwen3-embedding-8b
    - EMBEDDING_DIM  : 向量维度，如 4096（qwen3-embedding-8b 原生输出，不降维）
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        access_code: str | None = None,
        batch_size: int = 20,
        timeout: float = 30.0,
    ):
        self.model = (os.getenv("EMBEDDING_MODEL") or model or "").strip()
        self.base_url = (os.getenv("BASE_URL") or base_url or "").strip()
        self.access_code = (os.getenv("ACCESSCODE") or access_code or "").strip()
        self.batch_size = batch_size
        self.timeout = timeout

        if not self.access_code:
            raise RuntimeError("未在 .env 中找到 ACCESSCODE。")
        if not self.base_url:
            raise RuntimeError("未在 .env 中找到 BASE_URL。")
        if not self.model:
            raise RuntimeError("未在 .env 中找到 EMBEDDING_MODEL。")

        # OpenAI 风格异步客户端；verify=False 用于内网自签名网关
        self.async_client = AsyncOpenAI(
            api_key=self.access_code,
            base_url=self.base_url,
            http_client=httpx.AsyncClient(
                verify=False,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            ),
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        clean_texts = [text.strip() for text in texts if text.strip()]
        if not clean_texts:
            logger.info("[Embedding] 跳过：没有可用文本")
            return []

        logger.info(f"[Embedding] 开始向量化，文本数: {len(clean_texts)}")
        vectors: list[list[float]] = []
        max_chars_per_batch = 800
        max_chars_single = 800
        current_batch: list[str] = []
        current_chars = 0

        for text in clean_texts:
            text_len = len(text)
            if text_len > max_chars_single:
                logger.warning(f"单条文本超长({text_len} 字符)，截断嵌入: {text[:40]}...")
                text = text[:max_chars_single]
                text_len = max_chars_single

            if current_batch and current_chars + text_len > max_chars_per_batch:
                async with _embedding_sem:
                    response = await self.async_client.embeddings.create(
                        model=self.model,
                        input=current_batch,
                    )
                vectors.extend(item.embedding for item in response.data if item.embedding)
                current_batch = []
                current_chars = 0

            current_batch.append(text)
            current_chars += text_len

            if len(current_batch) >= self.batch_size:
                async with _embedding_sem:
                    response = await self.async_client.embeddings.create(
                        model=self.model,
                        input=current_batch,
                    )
                vectors.extend(item.embedding for item in response.data if item.embedding)
                current_batch = []
                current_chars = 0

        if current_batch:
            async with _embedding_sem:
                response = await self.async_client.embeddings.create(
                    model=self.model,
                    input=current_batch,
                )
            vectors.extend(item.embedding for item in response.data if item.embedding)

        logger.info(f"[Embedding] 完成，生成向量数: {len(vectors)}")

        return vectors

    async def aclose(self) -> None:
        await self.async_client.close()


# ============================================================
# 数据库写入
# ============================================================
@dataclass(slots=True)
class TableMeta:
    """目标表的必要元信息。"""
    column_types: dict[str, str]
    upsert_key: tuple[str, ...]


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _to_record(chunk: SemanticChunkOutput) -> dict[str, Any]:
    return {
        "chunk_index": chunk.chunk_index,
        "child_content": chunk.child_content,
        "file_path": chunk.file_path,
        "file_name": chunk.file_name,
        "text_hash": chunk.text_hash,
        "group_id": chunk.group_id,
        "internal_id": chunk.internal_id,
        "parent_content": chunk.parent_content,
        "related_image_url": chunk.related_image_url,
        "embedding": list(chunk.embedding),
        "embedding_dim": chunk.embedding_dim,
        "upload_time": _parse_timestamp(chunk.upload_time),
        "update_time": _parse_timestamp(chunk.update_time),
        "expire_time": _parse_timestamp(chunk.expire_time),
        "if_show": chunk.if_show,
    }


def _load_column_types(conn: psycopg.Connection, *, schema: str, table: str) -> dict[str, str]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, udt_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        return {cn: ut for cn, ut in cursor.fetchall()}


def _load_upsert_key(conn: psycopg.Connection, *, schema: str, table: str) -> tuple[str, ...]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
            "WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position",
            (schema, table),
        )
        primary_key = tuple(cn for (cn,) in cursor.fetchall())
        if primary_key:
            return primary_key

        cursor.execute(
            "SELECT tc.constraint_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
            "WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'UNIQUE' "
            "ORDER BY tc.constraint_name, kcu.ordinal_position",
            (schema, table),
        )
        rows = cursor.fetchall()
        if rows:
            first_constraint = rows[0][0]
            return tuple(cn for constraint_name, cn in rows if constraint_name == first_constraint)

    raise RuntimeError(f"目标表 {schema}.{table} 缺少主键或唯一约束，无法执行 UPSERT。")


def _load_table_meta(conn: psycopg.Connection, *, schema: str, table: str) -> TableMeta:
    column_types = _load_column_types(conn, schema=schema, table=table)
    if not column_types:
        raise RuntimeError(f"目标表不存在: {schema}.{table}")

    required_columns = {
        "chunk_index", "child_content", "file_path", "file_name", "text_hash",
        "group_id", "internal_id", "parent_content", "related_image_url",
        "embedding", "embedding_dim", "content_tsv",
        "upload_time", "update_time", "expire_time", "if_show",
    }
    missing = sorted(required_columns - set(column_types))
    if missing:
        raise RuntimeError(f"目标表缺少必要字段: {', '.join(missing)}")

    return TableMeta(column_types=column_types, upsert_key=_load_upsert_key(conn, schema=schema, table=table))


def _truncate_table(conn: psycopg.Connection, *, schema: str, table: str) -> None:
    conn.execute(sql.SQL("TRUNCATE TABLE {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))


def _load_max_chunk_index(conn: psycopg.Connection, *, schema: str, table: str) -> int | None:
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT MAX(chunk_index) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))
        )
        value = cursor.fetchone()[0]
    return int(value) if value is not None else None


def _offset_chunk_indices(
    chunks: Sequence[SemanticChunkOutput], *, base_max_chunk_index: int | None,
) -> list[SemanticChunkOutput]:
    if not chunks:
        return []
    incoming_min = min(c.chunk_index for c in chunks)
    next_index = 0 if base_max_chunk_index is None else base_max_chunk_index + 1
    offset = next_index - incoming_min
    if offset == 0:
        return list(chunks)
    return [c.model_copy(update={"chunk_index": c.chunk_index + offset}) for c in chunks]


def _refresh_content_tsv(conn: psycopg.Connection, *, schema: str, table: str,
                          column_types: Mapping[str, str],
                          min_chunk_index: int | None = None,
                          max_chunk_index: int | None = None) -> None:
    """更新 content_tsv：Python jieba 分词 + to_tsvector('simple', ...)，保证与检索端分词一致。

    simple 配置不二次分词，按空格切分，每个 jieba token 保持整词存入 tsv。
    """
    if "content_tsv" not in column_types:
        raise RuntimeError("目标表缺少必要字段: content_tsv")

    # 构造范围条件
    if min_chunk_index is not None and max_chunk_index is not None:
        range_where = sql.SQL("WHERE chunk_index BETWEEN %s AND %s")
        range_params = (min_chunk_index, max_chunk_index)
    else:
        range_where = sql.SQL("")
        range_params = ()

    # 1. SELECT 出 chunk_index + child_content + parent_content
    select_sql = sql.SQL("SELECT chunk_index, child_content, COALESCE(parent_content, '') FROM {}.{} {}").format(
        sql.Identifier(schema), sql.Identifier(table), range_where,
    )
    with conn.cursor() as cur:
        cur.execute(select_sql, range_params)
        rows = cur.fetchall()

    if not rows:
        return

    # 2. Python jieba 分词，逐行构造 tokenized 文本
    updates = []
    for chunk_index, child_content, parent_content in rows:
        combined = (child_content or "") + " " + (parent_content or "")
        tokenized = tokenize_for_tsv(combined)
        if tokenized:
            updates.append((tokenized, chunk_index))

    # 3. 逐行 UPDATE content_tsv = to_tsvector('simple', tokenized_text)
    with conn.cursor() as cur:
        for tokenized, chunk_index in updates:
            cur.execute(
                sql.SQL("UPDATE {}.{} SET content_tsv = to_tsvector('simple', %s) WHERE chunk_index = %s").format(
                    sql.Identifier(schema), sql.Identifier(table),
                ),
                (tokenized, chunk_index),
            )


def _coerce_record_types(record: dict[str, Any], column_types: Mapping[str, str]) -> dict[str, Any]:
    coerced = dict(record)
    if column_types.get("internal_id") in {"int2", "int4", "int8"}:
        coerced["internal_id"] = int(coerced["internal_id"])
    if column_types.get("embedding") == "vector":
        coerced["embedding"] = "[" + ",".join(format(float(v), ".17g") for v in coerced["embedding"]) + "]"
    return coerced


def _upsert_chunks(
    conn: psycopg.Connection, chunks: Sequence[SemanticChunkOutput], *,
    schema: str, table: str, table_meta: TableMeta,
) -> tuple[int, int, list[int]]:
    """执行 UPSERT 并返回 (inserted, updated, affected_chunk_indexes)。"""
    insert_columns = (
        "chunk_index", "child_content", "file_path", "file_name", "text_hash",
        "group_id", "internal_id", "parent_content", "related_image_url",
        "embedding", "embedding_dim",
        "upload_time", "update_time", "expire_time", "if_show",
    )
    embedding_value = (
        sql.SQL("CAST(%(embedding)s AS vector)")
        if table_meta.column_types.get("embedding") == "vector"
        else sql.SQL("%(embedding)s")
    )
    values = [
        embedding_value if col == "embedding" else sql.SQL(f"%({col})s")
        for col in insert_columns
    ]
    update_columns = [col for col in insert_columns if col not in table_meta.upsert_key]
    if not update_columns:
        raise RuntimeError("目标表主键覆盖了全部写入字段，无法生成更新语句。")

    statement = sql.SQL(
        "INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {} RETURNING chunk_index, (xmax = 0) AS inserted"
    ).format(
        sql.Identifier(schema), sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in insert_columns),
        sql.SQL(", ").join(values),
        sql.SQL(", ").join(sql.Identifier(c) for c in table_meta.upsert_key),
        sql.SQL(", ").join(
            sql.Composed([sql.Identifier(c), sql.SQL(" = EXCLUDED."), sql.Identifier(c)])
            for c in update_columns
        ),
    )

    inserted = 0
    updated = 0
    affected_chunk_indexes: list[int] = []
    with conn.cursor() as cursor:
        for chunk in chunks:
            cursor.execute(statement, _coerce_record_types(_to_record(chunk), table_meta.column_types))
            ci, was_inserted = cursor.fetchone()
            affected_chunk_indexes.append(int(ci))
            inserted += int(was_inserted)
            updated += int(not was_inserted)
    return inserted, updated, affected_chunk_indexes


def store_semantic_chunks(
    chunks: Sequence[SemanticChunkOutput | Mapping[str, Any]],
    *,
    schema: str | None = None, table: str | None = None,
    truncate: bool = False,
    upsert_key: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """将语义子块列表写入 PostgreSQL，并返回写入摘要。

    Parameters
    ----------
    upsert_key : 显式指定冲突检测列，默认自动检测主键。
                 推荐使用 ("file_name", "text_hash") 实现内容级去重。
    """
    normalized_chunks = [
        c if isinstance(c, SemanticChunkOutput) else SemanticChunkOutput.model_validate(c)
        for c in chunks
    ]

    pg_schema = schema or "public"
    pg_table = table or os.getenv("PG_MYAGENT_KB_TABLE")

    conn = psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )

    with conn:
        table_meta = _load_table_meta(conn, schema=pg_schema, table=pg_table)
        if upsert_key is not None:
            logger.info(f"使用显式 upsert_key={upsert_key} 覆盖自动检测的 {table_meta.upsert_key}")
            table_meta.upsert_key = tuple(upsert_key)

        if truncate:
            logger.info(f"清空目标表: {pg_schema}.{pg_table}")
            _truncate_table(conn, schema=pg_schema, table=pg_table)

        chunks_to_store: list[SemanticChunkOutput] = normalized_chunks
        if not truncate:
            # 始终为 chunk_index 分配全局唯一递增 ID（即使 upsert_key 不是 chunk_index）
            current_max = _load_max_chunk_index(conn, schema=pg_schema, table=pg_table)
            chunks_to_store = _offset_chunk_indices(normalized_chunks, base_max_chunk_index=current_max)
            if chunks_to_store:
                idx_desc = "chunk_index" if table_meta.upsert_key == ("chunk_index",) else "chunk_index（辅助）"
                logger.info(
                    f"已将本批 {idx_desc} 平移到 {chunks_to_store[0].chunk_index} 起始。"
                )

        inserted, updated, affected_indexes = _upsert_chunks(
            conn, chunks_to_store, schema=pg_schema, table=pg_table, table_meta=table_meta,
        )
        # 使用实际受影响的行刷新 content_tsv（精确范围，无论 upsert_key 是什么）
        affected_min = min(affected_indexes) if affected_indexes else None
        affected_max = max(affected_indexes) if affected_indexes else None
        logger.info(f"刷新全文检索字段: {pg_schema}.{pg_table}.content_tsv（影响 {len(affected_indexes)} 行，范围 {affected_min}~{affected_max}）")
        _refresh_content_tsv(
            conn, schema=pg_schema, table=pg_table, column_types=table_meta.column_types,
            min_chunk_index=affected_min, max_chunk_index=affected_max,
        )
        conn.commit()

    return {
        "schema": pg_schema, "table": pg_table,
        "chunk_count": len(normalized_chunks),
        "inserted": inserted, "updated": updated,
    }
