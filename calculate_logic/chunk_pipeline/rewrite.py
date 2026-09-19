"""LLM 指代消解 + 重写模块 —— 切片后、embedding 前对每个子块做指代消解和内容重写。

目标：让 child_content 脱离 parent_content 也能独立理解，提升向量检索和 rerank 精度。
- 把"它"、"该政策"、"上述规定"等代词替换为具体指代内容
- 补充省略主语、修正断句
- 保留 Markdown 格式和图片链接不变
- 原文保留在 parent_content 中，重写结果写回 child_content
"""

import asyncio
import logging
import os
import re

import httpx
from dotenv import find_dotenv, load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from typing import Sequence

from calculate_logic.chunk_pipeline.config import logger
from calculate_logic.chunk_pipeline.chunk import SemanticChunkOutput

load_dotenv(find_dotenv())

# ==================== LLM 客户端（懒加载） ====================
_rewrite_llm: ChatOpenAI | None = None

def _get_rewrite_llm() -> ChatOpenAI:
    global _rewrite_llm
    if _rewrite_llm is None:
        _rewrite_llm = ChatOpenAI(
            model=os.getenv("TEXT_MODEL", ""),
            api_key=os.getenv("ACCESSCODE", ""),
            base_url=os.getenv("BASE_URL", ""),
            http_client=httpx.Client(verify=False, timeout=60),
            temperature=0.3,
        )
        logger.info(f"[Rewrite] LLM 初始化: model={os.getenv('TEXT_MODEL', '')}")
    return _rewrite_llm


# ==================== 并发控制 ====================
_REWRITE_SEM = asyncio.Semaphore(5)  # 最多 5 个子块同时重写，避免抢占 embedding 算力


# ==================== 系统提示词 ====================
_SYSTEM_PROMPT = """你是一个指代消解和文本重写专家。给定一个文档片段（子块）和它的完整上下文（父块），你需要对子块做指代消解和重写，使子块脱离父块上下文也能被独立理解。

具体操作：
1. **代词替换**：把"它"、"该政策"、"上述规定"、"此假期"、"其"、"该津贴"等代词，替换为父块上下文中指代的具体名词。例如：
   - "它的申领条件" → "生育津贴的申领条件"
   - "该政策规定" → "陪产假政策规定"
   - "上述规定" → "《员工手册》中的相关规定"
2. **补充省略主语**：如果句子缺少主语，从父块上下文推断并补上。
3. **修正断句**：如果因切片导致句子不完整，补全使其通顺。
4. **保持不变的部分**：
   - Markdown 格式（加粗、列表、表格、图片链接 ![](...) 等）原样保留
   - 不要添加新信息，不要删减实质内容
   - 不要加任何解释、前缀或后缀
5. 如果子块已经完整清晰、没有任何代词或省略，可以原样返回。

直接输出重写后的文本，不要加任何标注。"""


def _build_user_prompt(child_content: str, parent_content: str) -> str:
    return f"""请对以下子块做指代消解和重写。

<父块上下文>
{parent_content}
</父块上下文>

<子块（需要重写的内容）>
{child_content}
</子块>

请输出重写后的子块文本："""


# ==================== 核心：单个子块重写 ====================
async def _rewrite_one(child_content: str, parent_content: str) -> str:
    """对单个子块做 LLM 指代消解+重写，返回重写后的文本。"""
    # 太短的子块（如纯表格、图片链接）不重写
    stripped = child_content.strip()
    if len(stripped) < 15:
        return child_content
    # 纯图片链接行不重写
    if re.fullmatch(r"\s*!\[[^\]]*\]\([^)]+\)\s*", stripped):
        return child_content

    llm = _get_rewrite_llm()
    async with _REWRITE_SEM:
        try:
            response = await llm.ainvoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(content=_build_user_prompt(child_content, parent_content)),
                ],
                extra_body={"max_tokens": 2048},
            )
            rewritten = (response.content or "").strip()
            if rewritten and len(rewritten) > 10:
                return rewritten
            return child_content
        except Exception as e:
            logger.warning(f"[Rewrite] 重写失败，保留原文: {e}")
            return child_content


# ==================== 批量重写入口 ====================
async def rewrite_chunks_with_coref(
    chunks: Sequence[SemanticChunkOutput],
    *,
    skip_short: bool = True,
) -> None:
    """对一批语义子块做指代消解+重写（就地修改 child_content）。

    - chunks: build_semantic_chunks 的输出
    - skip_short: 跳过过短子块（< 30 字符），不浪费 LLM 调用
    - 原文始终保留在 parent_content 中，重写结果写回 child_content
    - 并发控制：最多 5 个同时重写
    """
    if not chunks:
        return

    # 按父块分组，同一父块的子块共享 parent_content 作为上下文
    total = len(chunks)
    rewrite_count = 0
    skip_count = 0

    # 构建重写任务列表
    tasks = []
    indices_to_rewrite = []
    for i, chunk in enumerate(chunks):
        child = chunk.child_content.strip()
        parent = chunk.parent_content or child

        if skip_short and len(child) < 30:
            skip_count += 1
            continue
        if re.fullmatch(r"\s*!\[[^\]]*\]\([^)]+\)\s*", child):
            skip_count += 1
            continue

        tasks.append(_rewrite_one(child, parent))
        indices_to_rewrite.append(i)

    if not tasks:
        logger.info(f"[Rewrite] 全部 {total} 个子块过短或为图片链接，跳过重写")
        return

    logger.info(f"[Rewrite] 开始重写: {len(tasks)}/{total} 个子块（跳过 {skip_count} 个）")

    # 并发执行，带进度日志
    results = []
    for i, coro in enumerate(tasks):
        result = await coro
        results.append(result)
        if (i + 1) % 20 == 0:
            logger.info(f"[Rewrite] 进度: {i + 1}/{len(tasks)}")

    # 写回 child_content + 更新 text_hash
    from hashlib import sha256
    for idx, rewritten in zip(indices_to_rewrite, results):
        chunk = chunks[idx]
        original = chunk.child_content
        if rewritten != original:
            chunk.child_content = rewritten
            chunk.text_hash = sha256(rewritten.encode("utf-8")).hexdigest()
            rewrite_count += 1

    logger.info(f"[Rewrite] 完成: 重写 {rewrite_count}/{len(tasks)} 个子块，跳过 {skip_count} 个，总 {total} 个")
