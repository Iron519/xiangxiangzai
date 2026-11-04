import math
import os
import re
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, Sequence

from pydantic import BaseModel, Field

from calculate_logic.chunk_pipeline.config import logger, PROJECT_ROOT

# ================= 父块分块参数 =================
# 父块按"整句集合满 CHUNK_SIZE 字"为界切分；相邻两块的上下文重叠 CHUNK_OVERLAP_SENTENCES 句话。
CHUNK_SIZE = 2000
CHUNK_OVERLAP_SENTENCES = 3

# 中文/英文句号、分号等作为分句边界
_SENTENCE_RE = re.compile(
    r"(?<=[。！？!?；;])\s+|(?<=[。！？!?；;])(?=[^\s])|(?<=[A-Za-z]\.)\s+(?=[A-Z0-9一-鿿])"
)

# ================= 公共数据模型 =================
class ChunkOutput(BaseModel):
    """切片输出。"""
    chunk_index: int = Field(description="切片索引，全局唯一")
    child_content: str = Field(description="切片内容")
    file_path: str = Field(description="源文件路径")
    file_name: str = Field(description="源文件名")
    text_hash: str = Field(description="内容哈希值")
    upload_time: Optional[str] = Field(default=None, description="上传时间")
    update_time: Optional[str] = Field(default=None, description="更新时间")
    expire_time: Optional[str] = Field(default=None, description="到期时间")
    if_show: bool = Field(default=False, description="该文件图片是否可展示")


class SemanticChunkOutput(BaseModel):
    """语义子块输出，`child_content` 为子块，`parent_content` 为父块原文。"""
    chunk_index: int = Field(description="切片索引，全局唯一")
    child_content: str = Field(description="切片内容")
    file_path: str = Field(description="源文件路径")
    file_name: str = Field(description="源文件名")
    text_hash: str = Field(description="内容哈希值")
    group_id: int = Field(description="父块ID")
    internal_id: str = Field(description="父块内部子块ID")
    parent_content: str = Field(description="关联内容，如父块、QA的答案")
    related_content_summary: str | None = Field(default=None, description="关联内容摘要")
    related_image_url: str = Field(default="", description="该子块对应插图的可渲染URL(空表示无图)")
    embedding: list[float] = Field(default_factory=list, description="向量嵌入(4096维)")
    embedding_dim: int = Field(default=4096, description="向量维度")
    upload_time: Optional[str] = Field(default=None, description="上传时间")
    update_time: Optional[str] = Field(default=None, description="更新时间")
    expire_time: Optional[str] = Field(default=None, description="到期时间")
    if_show: bool = Field(default=False, description="该文件图片是否可展示")


# ============================================================
# Markdown 表格块识别与保护
# 表格作为不可分割原子单元，避免被机械切割成孤儿数据行（丢失表头/列名）
# 超长表格会按 _MAX_TABLE_CHARS 拆成多个表格段，每段都保留原表头
# ============================================================
_TABLE_LINE_RE = re.compile(r'^\s*\|.*\|\s*$')
_TABLE_SEP_RE = re.compile(r'^\s*\|[\s:]*-{2,}.*\|\s*$')
_MAX_TABLE_CHARS = 500  # 单个表格块上限字符数，超过则拆段（每段保留表头）


# ============================================================
# 块元素识别与提取
# 表格 / 插图描述块是不可分割的原子单元，父块切分时整体保留、绝不切断
# ============================================================
@dataclass
class _Segment:
    """父块的最小词元：普通句子或块元素。"""
    text: str
    atomic: bool = False  # True 表示整块元素（表格/插图），不许再被切断或与词元合并切割


def _split_table_block(block_lines: list[str]) -> list[_Segment]:
    """把一张表格切成若干个 ≤ _MAX_TABLE_CHARS 字符的表格段。

    规则：
    - 表头行（含分隔行）作为每段的公共前缀，保证拆出的每个表格都能独立读表头；
    - 从第二行数据开始逐行并入当前段，累计（表头 + 已并入数据行）超过上限即切出新段；
    - 表头本身超过上限、或表格行数过少（无数据行可拆）时，保持整表不拆；
    - 返回的每段都是 atomic 原子单元。
    """
    if len(block_lines) < 2:
        return [_Segment("\n".join(block_lines), atomic=True)]

    header: list[str] = [block_lines[0]]
    if _TABLE_SEP_RE.match(block_lines[1]):
        header.append(block_lines[1])
    header_len = sum(len(h) for h in header)
    data_lines = block_lines[len(header):]

    # 表头超限或没有可拆的数据行，整表保留
    if header_len > _MAX_TABLE_CHARS or len(block_lines) <= len(header) + 1:
        return [_Segment("\n".join(block_lines), atomic=True)]

    pieces: list[_Segment] = []
    current: list[str] = []
    current_len = header_len
    for dl in data_lines:
        if current and current_len + len(dl) > _MAX_TABLE_CHARS:
            pieces.append(_Segment("\n".join(header + current), atomic=True))
            current = []
            current_len = header_len
        current.append(dl)
        current_len += len(dl)
    if current:
        pieces.append(_Segment("\n".join(header + current), atomic=True))
    return pieces


def _extract_block_elements(text: str) -> list[_Segment]:
    """把文本按行扫描，切成"普通文本（后续拆句）"与"块元素（表格/插图描述块）"的序列。

    块元素：
    - 表格：连续的 | 开头的行，且其中至少含一行分隔行（| --- |）；
      超过 _MAX_TABLE_CHARS 字符的表格会拆成多个表格段，每段保留原表头；
    - 插图引用块：以 > 开头、且含【插图描述】的引用块。
    返回按原文顺序排列的 _Segment 列表；块元素的 atomic=True。
    """
    segments: list[_Segment] = []
    buffer: list[str] = []  # 待处理的普通行缓存

    def flush_buffer():
        if buffer:
            segments.append(_Segment("\n".join(buffer).strip()))
            buffer.clear()

    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # 表格块：从当前行起连续 | 开头的行，且包含分隔行
        if _TABLE_LINE_RE.match(line):
            block_lines = [line]
            j = i + 1
            while j < n and _TABLE_LINE_RE.match(lines[j]):
                block_lines.append(lines[j])
                j += 1
            if any(_TABLE_SEP_RE.match(line2) for line2 in block_lines):
                flush_buffer()
                segments.extend(_split_table_block(block_lines))
                i = j
                continue
            # 非真表格（无分隔行）：按普通行处理
            buffer.append(line)
            i += 1
            continue

        # 插图引用块：以 > 开头且含【插图描述】或【图片】的连续引用块
        # 同时将紧随其后的图片链接 ![](url) 合并为同一 atomic 单元，防止切分分离
        if line.strip().startswith(">") and ("【插图描述】" in line or "【图片】" in line or "【图片描述】" in line):
            block_lines = [line]
            j = i + 1
            while j < n and lines[j].strip().startswith(">"):
                block_lines.append(lines[j])
                j += 1
            # 向后看一行：若是图片 markdown ![](...)，并入同一块元素
            if j < n and re.match(r'^\s*!\[[^\]]*\]\([^)]+\)\s*$', lines[j].strip()):
                block_lines.append(lines[j])
                j += 1
            flush_buffer()
            segments.append(_Segment("\n".join(block_lines), atomic=True))
            i = j
            continue

        buffer.append(line)
        i += 1

    flush_buffer()
    return segments


def _split_sentences(text: str) -> list[str]:
    """把普通文本按句号/问号/感叹号/分号等切成分句，去掉空串。"""
    parts = _SENTENCE_RE.split(text)
    return [s.strip() for s in parts if s and s.strip()]


def _chunk_sentence_segments(
    segments: list[_Segment],
    *,
    size: int,
    overlap_sentences: int,
) -> list[str]:
    """把"句子 + 块元素(表格/插图)"按单位把父块切分到位。

    规则：
    - 以句子 / 块元素为不可分割单位（句子按句号边界分，块元素整体保留）；
    - 依序累积单位，直到这些单位的字数之和 ≥ size（2000）才切为一个父块，
      避免出现远小于 size 的父块。块元素也算入字数，且整体放进父块、绝不中途割断；
    - overlap：每个父块开头带上紧邻上一父块末尾的 overlap_sentences 个句子、
      末尾带上下一父块开头的 overlap_sentences 个句子；
      块元素不参与 overlap（前/后若是块元素，则对应一侧不留 overlap，也不把块元素当句子拉进上下文）。
    """
    def sentence_units() -> list[_Segment]:
        seq: list[_Segment] = []
        for seg in segments:
            if seg.atomic:
                seq.append(seg)
            else:
                for s in _split_sentences(seg.text):
                    if s:
                        seq.append(_Segment(s))
        return seq

    units = sentence_units()
    if not units:
        return []

    # 1) 累积切父块：单位字数之和到达 size 才切，块元素也计数
    chunks: list[list[_Segment]] = []
    current: list[_Segment] = []
    current_len = 0
    for u in units:
        current.append(u)
        current_len += len(u.text)
        if current_len >= size:
            chunks.append(current)
            current = []
            current_len = 0
    if current:
        chunks.append(current)

    # 2) overlap：相邻父块前后各叠 overlap_sentences 个句子（块元素不留）
    pieces: list[str] = []
    for i, chunk in enumerate(chunks):
        lead: list[str] = []
        if overlap_sentences > 0 and i > 0 and chunks[i - 1] and not chunks[i - 1][-1].atomic:
            for u in reversed(chunks[i - 1]):
                if u.atomic:
                    break
                lead.append(u.text)
                if len(lead) >= overlap_sentences:
                    break
            lead.reverse()

        tail: list[str] = []
        if overlap_sentences > 0 and i < len(chunks) - 1 and chunks[i + 1] and not chunks[i + 1][0].atomic:
            for u in chunks[i + 1]:
                if u.atomic:
                    break
                tail.append(u.text)
                if len(tail) >= overlap_sentences:
                    break

        block = "\n".join(lead + [u.text for u in chunk] + tail).strip()
        if block:
            pieces.append(block)
    return pieces


# ============================================================
# 父级切片（粗切）
# ============================================================
def chunk_markdown_text(
    text: str,
    file_path: str | Path,
    upload_time: Optional[str] = None,
    update_time: Optional[str] = None,
    expire_time: Optional[str] = None,
    if_show: bool = False,
) -> list[ChunkOutput]:
    """按"句子 + 块元素"单位切分父块，块元素（表格/插图）始终保持完整。

    规则：
    - 普通文本先按句号/分号等切分句子，句子与块元素（表格/插图）都是不可分割单位；
    - 依序累积单位，直到这些单位的字数之和 ≥ CHUNK_SIZE（2000）才切为一个父块；
    - 相邻两块首尾各重叠 CHUNK_OVERLAP_SENTENCES 句话作为上下文（块元素不参与 overlap）；
    - 表格 / 插图描述块整体保留、计入父块字数，绝不切断；
    - 超过 _MAX_TABLE_CHARS（500）字符的表格在提取阶段被拆成多个表格段，
      每段都继承原表头，保证拆出的每个子表格都能独立读表头。

    expire_time: 可选，过期时间（ISO 字符串）。None 表示永久有效。
    if_show: 该文件图片是否可展示（文件级开关，冗余到每个切块）。
    """
    path = Path(file_path)
    normalized_text = text.strip()
    if not normalized_text:
        return []

    segments = _extract_block_elements(normalized_text)
    raw_pieces = _chunk_sentence_segments(
        segments,
        size=CHUNK_SIZE,
        overlap_sentences=CHUNK_OVERLAP_SENTENCES,
    )

    chunks: list[ChunkOutput] = []
    for content in raw_pieces:
        content = content.strip()
        if not content:
            continue
        chunks.append(
            ChunkOutput(
                chunk_index=len(chunks),
                child_content=content,
                file_path=str(path),
                file_name=path.name,
                text_hash=sha256(content.encode("utf-8")).hexdigest(),
                upload_time=upload_time,
                update_time=update_time,
                expire_time=expire_time,
                if_show=if_show,
            )
        )
    return chunks



# ============================================================
# 语义子块（精切）
# ============================================================
_MIN_CHUNK_LENGTH = 50
_SIMILARITY_THRESHOLD = 0.90
EmbeddingFunction = Callable[[Sequence[str]], "Coroutine[Any, Any, list[list[float]]]"]


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个向量的余弦相似度。"""
    if len(left) != len(right):
        raise ValueError("向量维度不一致，无法计算余弦相似度。")
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(v * v for v in left))
    right_norm = math.sqrt(sum(v * v for v in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _average_embedding(embeddings: Sequence[Sequence[float]]) -> list[float]:
    """计算一组向量的逐维平均值。"""
    if not embeddings:
        return []
    dimension = len(embeddings[0])
    totals = [0.0] * dimension
    for emb in embeddings:
        for idx, val in enumerate(emb):
            totals[idx] += val
    count = float(len(embeddings))
    return [v / count for v in totals]


def _merge_semantic_units(
    units: Sequence[str], embeddings: Sequence[Sequence[float]], *,
    similarity_threshold: float,
) -> list[str]:
    """仅根据向量相似度，把原子单元合并成最终子块。"""
    if len(units) != len(embeddings):
        raise ValueError("语义单元数量与 embedding 数量不一致。")
    if not units:
        return []

    merged: list[str] = []
    current_units = [units[0]]
    current_embeddings = [list(embeddings[0])]

    for idx in range(1, len(units)):
        candidate = units[idx]
        candidate_embedding = list(embeddings[idx])
        similarity = _cosine_similarity(_average_embedding(current_embeddings), candidate_embedding)

        if similarity < similarity_threshold:
            merged.append("\n".join(current_units).strip())
            current_units = [candidate]
            current_embeddings = [candidate_embedding]
        else:
            current_units.append(candidate)
            current_embeddings.append(candidate_embedding)

    merged.append("\n".join(current_units).strip())
    return merged


def _merge_short_chunks(units: Sequence[str], *, min_length: int) -> list[str]:
    """将过短子块并入相邻子块。"""
    normalized = [u.strip() for u in units if u.strip()]
    if len(normalized) <= 1:
        return normalized

    pending = list(normalized)
    merged: list[str] = []
    idx = 0
    while idx < len(pending):
        current = pending[idx]
        if len(current) >= min_length:
            merged.append(current)
            idx += 1
            continue
        if idx + 1 < len(pending):
            pending[idx + 1] = f"{current}\n{pending[idx + 1]}".strip()
            idx += 1
            continue
        if merged:
            merged[-1] = f"{merged[-1]}\n{current}".strip()
            idx += 1
            continue
        merged.append(current)
        idx += 1
    return merged


def _collect_atomic_units(text: str) -> list[str]:
    """
    把文本切成句级原子单元。
    表格/插图描述块作为单个原子单元整体保留，不参与句级切分，
    避免被拆成无法独立检索的碎片。
    """
    units: list[str] = []
    for seg in _extract_block_elements(text):
        if not seg.text.strip():
            continue
        if seg.atomic:
            units.append(seg.text)
            continue
        sentences = _split_sentences(seg.text)
        units.extend(sentences if len(sentences) > 1 else [seg.text.strip()])
    return units


async def _split_parent_semantically(
    content: str, *, similarity_threshold: float, embedder: EmbeddingFunction,
) -> list[str]:
    """先切成句级原子单元，再仅根据向量相似度合并为语义子块。"""
    parent_content = content.strip()
    if not parent_content:
        return [parent_content]

    units = _collect_atomic_units(parent_content)
    if not units:
        return [parent_content]
    if len(units) == 1:
        return [units[0]]

    embeddings = await embedder(units)
    merged_units = _merge_semantic_units(units, embeddings, similarity_threshold=similarity_threshold)
    return _merge_short_chunks(merged_units, min_length=_MIN_CHUNK_LENGTH)


async def build_semantic_chunks(
    father_chunks: Sequence[ChunkOutput | dict[str, Any]],
    *,
    similarity_threshold: float = _SIMILARITY_THRESHOLD,
    embedder: EmbeddingFunction,
) -> list[SemanticChunkOutput]:
    """基于父块生成可召回的语义子块。"""
    normalized_fathers = [
        c if isinstance(c, ChunkOutput) else ChunkOutput.model_validate(c)
        for c in father_chunks
    ]
    if not normalized_fathers:
        logger.info("未检测到父块，直接返回空结果")
        return []

    logger.info(f"开始语义切分，父块数量: {len(normalized_fathers)}")

    semantic_chunks: list[SemanticChunkOutput] = []
    for father in normalized_fathers:
        child_texts = await _split_parent_semantically(
            father.child_content, similarity_threshold=similarity_threshold, embedder=embedder,
        )
        logger.info(f"父块 {father.chunk_index} 切分得到子块数: {len(child_texts)}")
        embeddings = await embedder(child_texts)

        for child_order, (child_text, embedding) in enumerate(zip(child_texts, embeddings, strict=True)):
            normalized_child = child_text.strip()
            semantic_chunks.append(SemanticChunkOutput(
                chunk_index=len(semantic_chunks),
                child_content=normalized_child,
                file_path=father.file_path,
                file_name=father.file_name,
                text_hash=sha256(normalized_child.encode("utf-8")).hexdigest(),
                group_id=father.chunk_index,
                internal_id=str(child_order),
                parent_content=father.child_content,
                related_content_summary=None,
                embedding=list(embedding),
                embedding_dim=len(embedding),
                upload_time=father.upload_time,
                update_time=father.update_time,
                expire_time=father.expire_time,
                if_show=father.if_show,
            ))

    return semantic_chunks


# ============================================================
# 插图(anchors.json)读取 + image_url 换算
# 入库时给"由图片描述出来的文字"所对应的 child_chunk 填上可渲染的图片 URL
# ============================================================
from calculate_logic.fetch_knowledge.parse_file import PARSE_RESULT_DIR

MINERU_OUTPUT_DIR = str(PARSE_RESULT_DIR)


def _image_abs_to_url(abs_path: str) -> str:
    """把落盘图片的绝对路径换算成可被网页渲染的 URL(/api/media/{category}/{doc}/{file})。

    FastAPI 的 /api/media(见 app/app.py)以 <MINERU_OUTPUT_DIR>/<doc>/<category>/<file>
    服务图片(JWT 鉴权)。类别由图片所在子目录推断：
      - .../<doc>/images/page_*.jpg        -> extracted
      - .../<doc>/page_renders/page_*.png  -> render
    无法换算时返回空串。
    """
    if not abs_path:
        return ""
    try:
        rel = os.path.relpath(str(abs_path), MINERU_OUTPUT_DIR).replace(os.sep, "/")
    except Exception:
        return ""
    if rel.startswith(".."):
        return ""  # 图片不在 MinerU 输出目录下，无法对外提供
    parts = rel.split("/")
    if len(parts) < 3:
        return ""
    doc_dir, category_dir, filename = parts[0], parts[1], parts[-1]
    category = {"images": "extracted", "page_renders": "render"}.get(category_dir)
    if not category:
        return ""  # 未知类别目录，不对外提供
    return f"/api/media/{category}/{doc_dir}/{filename}"


def load_document_anchors(save_path: str) -> list[dict]:
    """读取某份 PDF 解析产出的 anchors.json(文字块↔达标插图绝对路径)。

    save_path 为入库的源文件路径；其文件名前缀(file_stem)对应 MinerU 产物目录，
    即 <MINERU_OUTPUT_DIR>/<file_stem>/anchors.json。文件缺失/解析失败返回 []。
    """
    try:
        file_stem = Path(save_path).stem
        anchors_path = Path(MINERU_OUTPUT_DIR) / file_stem / "anchors.json"
        if not anchors_path.exists():
            return []
        with open(anchors_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("anchors", []) if isinstance(data, dict) else []
    except Exception as e:
        logger.warning(f"[ImageURL] 读取 anchors.json 失败: {e}")
        return []


def _anchor_image_urls(anchor: dict) -> list[str]:
    """把某锚点的绝对图片路径换算成可渲染 URL 列表。"""
    urls = []
    for p in anchor.get("image_paths") or []:
        u = _image_abs_to_url(p)
        if u:
            urls.append(u)
    return urls


def resolve_child_image_urls(semantic_chunks: Sequence[SemanticChunkOutput], save_path: str) -> None:
    """按"文本包含匹配"在父块层面给插图填 URL，并让该父块下所有子块继承(就地修改)。

    规则：
    - 以每个父块(group_id)为单位做匹配：若该父块的 parent_content(父块原文)包含某锚点
      text(或短于锚点 text 时反被包含)，则该父块命中此插图；
    - 父子块继承：父块命中后，该父块(group_id)下的所有子块都填上同一 related_image_url；
    - 锚点 text 过短的不参与匹配，避免误伤(如纯数字/无意义片段)。
    """
    anchors = [
        a for a in load_document_anchors(save_path)
        if a.get("text") and a.get("image_paths")
    ]
    if not anchors:
        logger.info("[ImageURL] 无锚点数据，跳过图片 URL 映射")
        return
    logger.info(f"[ImageURL] 加载锚点 {len(anchors)} 条，开始文本匹配")
    # 预处理：仅保留文本足够长且确有可渲染图的锚点
    useful = []
    for a in anchors:
        urls = _anchor_image_urls(a)
        if not urls:
            continue
        text = (a.get("text") or "").strip()
        if len(text) < 10:  # 过短不匹配
            continue
        useful.append((text, urls[0]))
    if not useful:
        return

    # 按父块去重：父块原文匹配一次，命中结果由该组所有子块继承
    group_map: dict[int, str] = {}  # group_id -> 命中 image_url
    for chunk in semantic_chunks:
        gid = getattr(chunk, "group_id", None)
        if gid is None or gid in group_map:
            continue
        content = (chunk.parent_content or "").strip()
        if not content:
            continue
        # 双向包含匹配：父块原文 包含锚点 text，或锚点 text 包含父块原文
        for text, url in useful:
            if text in content or (len(text) >= len(content) and content in text):
                group_map[gid] = url
                break

    # 子块继承：把父块命中的 url 填到该组每个子块(未赋值者)
    if not group_map:
        logger.info("[ImageURL] 无父块命中图片锚点")
        return
    for chunk in semantic_chunks:
        if getattr(chunk, "related_image_url", ""):
            continue
        gid = getattr(chunk, "group_id", None)
        if gid in group_map:
            chunk.related_image_url = group_map[gid]
    logger.info(f"[ImageURL] 映射完成: {len(group_map)} 个父块命中图片，已填充子块 URL")
