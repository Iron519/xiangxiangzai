import os
import re
import sys
import json
import logging
import base64
import tempfile
import asyncio
import shutil
from collections import Counter
from pathlib import Path
from concurrent_log_handler import ConcurrentRotatingFileHandler
import fitz  # PyMuPDF
fitz.TOOLS.mupdf_warnings = False  # 禁用 MuPDF 警告
from openai import AsyncOpenAI
import httpx
import aiofiles
import emoji
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

# ================= 运行路径配置 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if PROJECT_ROOT and __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 解析结果统一输出目录
PARSE_RESULT_DIR = Path(PROJECT_ROOT) / "calculate_logic" / "fetch_knowledge" / "parse_result"
PARSE_RESULT_DIR.mkdir(parents=True, exist_ok=True)

# ================= 引进MinerU对PDF的版式解析方法 =================
from calculate_logic.fetch_knowledge.miner_u_parse import MinerUProcessor, _get_vlm_async_client

# ================= DocumentParser 复用 miner_u_parse 的模块级 AsyncOpenAI 连接池 =================
# 避免每个 DocumentParser 实例创建独立客户端，TCP 连接全进程复用

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "parse_file.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("DocumentParser")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    file_handler = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)

# ================= LLM参数 =================
REQUIRED_ENV_VARS = ["BASE_URL", "ACCESSCODE", "TEXT_MODEL", "VLM_MODEL", "SOFFICE_PATH"]
config = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}


class DocumentParser:
    def __init__(
        self,
        access_code: str = config["ACCESSCODE"],
        base_url: str = config["BASE_URL"],
        text_model: str = config["TEXT_MODEL"],
        vlm_model: str = config["VLM_MODEL"],
        soffice_path: str = config["SOFFICE_PATH"],
        timeout: int = 300,
        max_concurrent_api: int = 5,
    ):
        self.access_code = access_code
        self.base_url = base_url
        self.text_model = text_model
        self.vlm_model = vlm_model
        self.soffice_path = soffice_path
        self._api_semaphore = asyncio.Semaphore(max_concurrent_api)

    # ============================================================
    # 解析文件统一入口 —— 根据文件类型分发路由
    # ============================================================
    async def parse_file(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        if not os.path.exists(file_path):
            logger.error(f"文件不存在：{file_path}")
            return ""
        ext = os.path.splitext(file_path)[1].lower()
        logger.info(f"文件格式为{ext}，开始解析{file_path}")
        try:
            if ext in ['.txt', '.md', '.csv']:
                full_text = await self._parse_text(file_path, display_name, uid=uid)
            elif ext == '.pdf':
                full_text = await self._parse_pdf_pipeline(file_path, display_name, uid=uid)
            elif ext in ['.jpeg', '.jpg', '.png']:
                full_text = await self._parse_image(file_path, display_name, uid=uid)
            elif ext in ['.docx']:
                full_text = await self._parse_word(file_path, display_name, uid=uid)
            elif ext == '.pptx':
                full_text = await self._parse_office_via_mineru(file_path, display_name, uid=uid)
            elif ext in ['.xlsx', '.xls']:
                full_text = await self._parse_excel(file_path, display_name, uid=uid)
            else:
                logger.warning(f"暂不支持的文件格式: {ext}")
                full_text = ""
        except Exception as e:
            logger.exception(f"文件解析失败: {e}")
            full_text = ""
        
        return full_text

    # ============================================================
    # 自动检测编码读取文本文件：UTF-8 → GB18030 → GBK → GB2312
    # ============================================================
    _ENCODING_FALLBACK = ['utf-8', 'gb18030', 'gbk', 'gb2312', 'big5', 'latin-1']

    async def _read_text_auto(self, file_path: str) -> str:
        async with aiofiles.open(file_path, 'rb') as f:
            raw_bytes = await f.read()
        for enc in self._ENCODING_FALLBACK:
            try:
                return raw_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw_bytes.decode('utf-8', errors='replace')

    # ============================================================
    # 纯文本文件解析，并使用_enhance_text_to_markdown转为markdown格式
    # ============================================================
    async def _parse_text(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        try:
            raw = await self._read_text_auto(file_path)
            if not raw or not raw.strip():
                return ""
            ext = os.path.splitext(file_path)[1].lower()
            if ext == '.md':
                text = raw.strip()
            elif len(raw.strip()) < 50:
                text = raw.strip()
            else:
                text = await self._enhance_text_to_markdown(raw)
            await self._write_merged_md(self._office_output_dir(file_path, display_name, uid), text)
            # 纯文本文件也需要生成 anchors.json（即便没有图片）
            await self._write_anchors_json(self._office_output_dir(file_path, display_name, uid), [])
            return text
        except PermissionError:
            logger.error(f"文件权限不足：{file_path}")
            return ""
        except Exception as e:
            logger.error(f"文本文件读取失败：{e}")
            return ""

    # ============================================================
    # PDF 解析，根据内容分别使用MinerU / VLM
    # ============================================================
    async def _parse_pdf_pipeline(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        minur_u_process = MinerUProcessor(file_path, display_name=display_name, uid=uid)
        all_page_results = []

        try:
            page_files = minur_u_process.split_pdf_to_pages()
            logger.info(f"--- 开始解析pdf，将其拆分为 {len(page_files)} 页 ---")

            async def _process_one_page(page_info):
                page_no = page_info["page_no"]
                try:
                    page_context = await minur_u_process.prepare_one_page_before_process(page_info)
                    logger.info(f"--- 正在处理 PDF 第 {page_no}/{len(page_files)} 页 ---")
                    if page_context["is_image_only"]:
                        logger.info(f"第 {page_no} 页为纯图片，启用整页 VLM 识别并建整页图锚点...")
                    else:
                        logger.info(f"第 {page_no} 页为正常 PDF，启用 MinerU 提取...")
                    page_json = await minur_u_process.process_one_page(page_context)
                    if page_json is None:
                        logger.error(f"第 {page_no} 页 MinerU 返回 None")
                        page_json = {
                            "page_no": page_no,
                            "is_image_only": False,
                            "debug_info": {"error": "MinerU 返回 None"},
                            "md_content": "",
                            "content_list": [],
                            "middle_json": None,
                            "image_desc_map": {},
                            "anchors": [],
                            "filters": [],
                        }
                    return page_json
                except Exception as e:
                    logger.error(f"第 {page_no} 页解析失败，跳过该页: {e}")
                    return {
                        "page_no": page_no,
                        "is_image_only": False,
                        "debug_info": {"error": str(e)},
                        "md_content": "",
                        "content_list": [],
                        "middle_json": None,
                        "image_desc_map": {},
                        "anchors": [],
                        "filters": [],
                    }

            all_page_results = await asyncio.gather(*[_process_one_page(pf) for pf in page_files])
            merged = minur_u_process.merge_page_results(all_page_results)
            await minur_u_process.write_anchors_json(merged)
            text = await self._enhance_text_to_markdown(merged.get("md_content", ""))
            await self._write_merged_md(self._office_output_dir(file_path, display_name, uid), text)
            return text

        except Exception as e:
            logger.exception(f"PDF解析失败: {e}")
            return ""

    # ============================================================
    # Office→PDF 统一走 MinerU：嵌入图片等同 PDF 一样做锚点映射落库
    # ============================================================
    async def _parse_office_via_mineru(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        tmp_dir = tempfile.mkdtemp(prefix="myagent_office_pdf_")
        try:
            pdf_path = os.path.join(tmp_dir, Path(file_path).stem + ".pdf")
            if not await self._convert_office_to_pdf(file_path, pdf_path):
                raise RuntimeError(f"Office→PDF 转换失败: {file_path}")
            if not os.path.exists(pdf_path):
                raise RuntimeError(f"Office→PDF 未产出文件: {file_path}")
            text = await self._parse_pdf_pipeline(pdf_path, display_name, uid=uid)
            if not text:
                raise RuntimeError(f"Office→PDF→MinerU 解析结果为空: {file_path}")
            return text
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ============================================================
    # 解析Excel：文本/表格用 pandas 原生解析；图片用 openpyxl 定位到单元格锚点，
    # 仅对该图做 VLM 识别，并把 VLM 文本原位插回表格对应位置；图片与内容映射落 anchors。
    # ============================================================
    def _extract_excel_cell_images(self, file_path: str) -> dict:
        """用 openpyxl 提取各工作表内嵌图片及其单元格锚点。

        返回 {sheet_name: [(row, col, img_bytes, img_index), ...]}，row/col 为 0 基单元格坐标。
        """
        result: dict[str, list] = {}
        try:
            from openpyxl import load_workbook
            wb = load_workbook(file_path, data_only=True)
            for sheet_name in wb.sheetnames:
                sheet = wb[sheet_name]
                if not hasattr(sheet, '_images') or not sheet._images:
                    continue
                for idx, img in enumerate(sheet._images):
                    try:
                        img_bytes = img._data()
                        if not img_bytes or len(img_bytes) < 100:
                            logger.warning(f"{sheet_name} 图片 {idx+1} 数据无效，跳过")
                            continue
                        anchor = getattr(img, "anchor", None)
                        row = col = None
                        if anchor is not None:
                            try:
                                col = int(getattr(anchor, "_from").col)
                                row = int(getattr(anchor, "_from").row)
                            except Exception:
                                col = row = None
                        result.setdefault(sheet_name, []).append((row, col, img_bytes, idx + 1))
                    except Exception as e:
                        logger.warning(f"{sheet_name} 图片 {idx+1} 提取失败: {e}")
            wb.close()
        except Exception as e:
            logger.warning(f"Excel 图片定位失败: {e}")
        return result

    async def _parse_excel(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        try:
            import pandas as pd
            from openpyxl import load_workbook

            out_dir = self._office_output_dir(file_path, display_name, uid)
            anchors: list = []
            cell_imgs = self._extract_excel_cell_images(file_path)

            sheets = pd.read_excel(file_path, sheet_name=None, header=None)
            full_text = []
            for name, df in sheets.items():
                logger.info(f"正在处理 Excel 工作表: {name}")
                df = df.dropna(how='all').dropna(axis=1, how='all').fillna("")
                table_2d = df.values.tolist()

                # 工作表内嵌图片 → 按(row,col)锚点做 VLM 识别，并原位插入到表格对应行
                sheet_images = cell_imgs.get(name, [])
                if sheet_images:
                    img_by_cell: dict[tuple, tuple[str, str]] = {}
                    trailing: list[tuple[str, str]] = []
                    for row, col, img_bytes, idx in sheet_images:
                        vlm_text, img_url = await self._process_office_image(
                            img_bytes, f"{name}_img_{idx}.png", out_dir, anchors,
                        )
                        if not vlm_text:
                            continue
                        if row is not None and col is not None:
                            img_by_cell[(int(row), int(col))] = (vlm_text, img_url)
                        else:
                            trailing.append((vlm_text, img_url))

                    for r_i, row_cells in enumerate(table_2d):
                        placed = False
                        for (a_row, a_col), (v_text, v_url) in img_by_cell.items():
                            if a_row == r_i:
                                if v_url:
                                    row_cells.append(f"【图片】{v_text}\n\n![]({v_url})")
                                else:
                                    row_cells.append(f"【图片】{v_text}")
                                placed = True
                        if placed:
                            pass
                    if trailing:
                        for t_text, t_url in trailing:
                            if t_url:
                                table_2d.append([f"【图片】{t_text}\n\n![]({t_url})"])
                            else:
                                table_2d.append([f"【图片】{t_text}"])

                text_table = self._convert_table_to_markdown(table_2d)
                if text_table:
                    full_text.append(f"\n\n{text_table}")

            text = "\n".join(full_text)
            text = self._clean_markdown_text(text)
            await self._write_office_outputs(out_dir, text, anchors)
            return text
        except Exception as e:
            logger.error(f"Excel解析失败: {e}")
            return ""

    # ============================================================
    # 图片解析
    # ============================================================
    async def _parse_image(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        try:
            async with aiofiles.open(file_path, "rb") as f:
                img_bytes = await f.read()
            desc = await self._call_vlm(img_bytes)
        except Exception as e:
            logger.error(f"图片解析失败: {e}")
            return ""
        await self._write_image_only_anchor(file_path, desc, uid=uid)
        out_dir = self._office_output_dir(file_path, display_name, uid)
        stem = Path(file_path).stem.replace(" ", "_")
        ext = os.path.splitext(file_path)[1].lower() or ".png"
        img_url = f"/api/media/extracted/{stem}/{stem}{ext}"
        md_text = f"> 【图片描述】{desc}\n\n![]({img_url})\n"
        await self._write_merged_md(out_dir, md_text)
        return desc

    async def _write_image_only_anchor(self, file_path: str, desc: str, uid: str = "") -> None:
        try:
            stem = Path(file_path).stem.replace(" ", "_")
            if uid:
                out_dir = PARSE_RESULT_DIR / uid / stem
            else:
                out_dir = PARSE_RESULT_DIR / stem
            img_dir = out_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            ext = os.path.splitext(file_path)[1].lower() or ".png"
            img_dst = img_dir / f"{stem}{ext}"
            if not img_dst.exists():
                shutil.copy2(file_path, img_dst)
            anchors = []
            if desc:
                anchors.append({
                    "page_no": 1,
                    "type": "image_only",
                    "block_index": None,
                    "text": desc,
                    "image_paths": [str(img_dst)],
                })
            await self._write_anchors_json(out_dir, anchors)
        except Exception as e:
            logger.warning(f"图片锚点落库失败: {e}")

    # ============================================================
    # 统一提取 Office 文件中的嵌入图片，调用 _call_vlm 识别内容
    # ============================================================
    # ============================================================
    # 将纯文本/半结构文本规整为 Markdown：HTML 表格→MD + 表格保护 + 归一化
    # ============================================================
    async def _enhance_text_to_markdown(self, text: str) -> str:
        """
        把文本规整为 RAG 友好的 Markdown，全程确定性、无 LLM 调用：
        1. HTML <table> → 标准 Markdown 表格（多级表头/整行整列标题扁平化）
        2. 既有 Markdown 表格归一化、空行规整、去 emoji
        """
        if not text or len(text.strip()) < 50:
            return text

        # HTML 表格先转 MD，再由 _clean_markdown_text 统一归一化（表格保护、空行、去 emoji）
        text = self._convert_html_tables_to_markdown(text)
        text = self._clean_markdown_text(text)
        return text

    # ============================================================
    # 图片标准化：统一转 RGB JPEG、限最长边、压缩质量，用于喂给 VLM
    # ============================================================
    @staticmethod
    def _normalize_image_bytes(img_bytes: bytes, max_side: int = 1568, quality: int = 85) -> bytes:
        try:
            from io import BytesIO
            from PIL import Image
        except Exception:
            return img_bytes  # 无 Pillow 时原样返回，退回旧行为
        try:
            im = Image.open(BytesIO(img_bytes))
            im.load()
        except Exception as e:
            logger.warning(f"图片标准化：Pillow 无法解码该图片，跳过: {e}")
            return b""
        w, h = im.size
        if w < 8 or h < 8:
            logger.warning(f"图片标准化：尺寸过小 {w}x{h}，疑似无效位图，跳过")
            return b""
        # 统一转 RGB（透明背景铺白）
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        # 等比缩小
        if max(w, h) > max_side:
            ratio = max_side / float(max(w, h))
            im = im.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
        out = BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()

    # ============================================================
    # 通过VLM识别图片内容，返回纯业务语义文本（Markdown格式），并调用_clean_markdown进行清洗
    # ============================================================
    async def _call_vlm(self, img_bytes: bytes) -> str:
        if not img_bytes or len(img_bytes) < 100:
            logger.error(f"[VLM] 图片数据无效：字节长度 {len(img_bytes) if img_bytes else 0}")
            return "[解析失败：图片数据无效]"

        img_bytes = self._normalize_image_bytes(img_bytes)
        if not img_bytes:
            logger.warning("[VLM] 图片标准化失败或为无效位图（可能是 EMF/SVG/损坏图），跳过")
            return ""

        logger.info(f"[VLM] 开始识别图片 ({len(img_bytes)} bytes)")

        base64_image = base64.b64encode(img_bytes).decode('utf-8')
        principle = """
你是一个资深的「企业级知识图谱提取引擎」，专为 RAG 系统的高质量文本切片服务。
你的任务是将输入的办公文档截图，转化为 **高信息还原度、强业务语义、可直接用于检索与推理的 Markdown 文本**。

【总体目标】
- 最大化业务语义的还原度，关键信息绝对不要遗漏！
- 最小化无效、重复或视觉相关信息
- 为后续的 Markdown 父子级分块与向量检索提供最优输入

【核心原则】
- **内容优先于结构**：宁可减少标题层级，也不要出现“标题 + 一句话”的空洞结构
- **语义完整优先**：相近、连续、因果相关的内容必须合并为完整段落
- **克制分级**：只有在内容本身足够丰富时，才允许拆分为多级标题

【工作规则】
1. **去视觉化**
   - 只保留业务语义，严禁描述排版、颜色、位置、大小、形状、装饰性元素
   - 禁止出现“图片中 / 图中 / 页面上 / 左右 / 上下”等视觉描述

2. **聚焦业务核心**
   必须优先提取并重组以下信息（按重要性排序）：
   - 文档主旨、业务目标、策略意图
   - 核心结论、关键判断、明确的行动项
   - 关键数据与指标（仅保留对业务决策有意义的结论）
   - 流程类内容：用清晰的逻辑关系描述（如 A → B → C）
   - 图表类内容：提炼业务含义（如“2023 Q3 营收同比增长 20%”）

3. **内容组织规范**
   - 默认使用 **连续自然段** 承载信息
   - 仅在以下场景使用列表：
     - 明确的步骤流程
     - 选项或方案对比
     - 不可合并的离散要点
   - 表格必须使用 **标准 Markdown 表格**，且 **整体保留，不做拆分**，并用 ** | --- | --- |  ** 做分隔符
   - 图片若仅为 Logo、二维码、印章、Icon 等非业务信息，用 **不超过 5 个字** 简要概括，不影响正文连贯性

4. **谨慎思考（内部约束）**
   - 在处理复杂逻辑图、流程图或多层因果关系时，你必须先进行完整、严谨的逻辑推理
   - 该推理过程仅用于保证输出质量，不应干扰最终正文内容

【输出要求】
- **绝对禁止**出现：“这张图片展示了…”、“图中可以看到…”等描述性废话
- **绝对禁止**出现emoji等表情包
- 使用 Markdown 语法：
  - `# ## ###` 表示层级
  - `-` 或 `1.` 表示列表
  - `**加粗**` 用于关键概念
  - ```code``` 用于代码或规则
- 最终输出为 JSON，格式如下：
  {
    "md_content": "转换后的 Markdown 正文内容",
    "thought": "复杂逻辑处理时的内部推理摘要"
  }

【强制约束】
- 下游系统 **只会使用 md_content**
- md_content 中 **不得包含任何思考过程、自我解释或多余说明**
- 除 JSON 外，不得输出任何额外内容
- 绝对不要包含任何 Markdown 代码块标签（如 ```json）
"""
        async with self._api_semaphore:
            try:
                client = await _get_vlm_async_client()
                response = await client.chat.completions.create(
                    model=self.vlm_model,
                    messages=[
                        {"role": "system", "content": principle},
                        {"role": "user", "content": [
                            {"type": "text", "text": "描述文件截图的主要内容，不要任何额外的解释！"},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64, {base64_image}"}}
                        ]}
                    ],
                    temperature=0.1
                )
                if response is None or not hasattr(response, 'choices') or not response.choices:
                    logger.error("VLM API 返回响应为空或格式异常")
                    return "[解析失败：VLM API 返回响应异常]"
                
                message = response.choices[0].message
                if message is None or not hasattr(message, 'content') or message.content is None:
                    logger.error("VLM API 返回消息内容为空")
                    return "[解析失败：VLM API 返回消息内容为空]"
                
                import json
                def _strip_code_fence(s: str) -> str:
                    """去掉 ```json / ``` 代码块包裹"""
                    if s.startswith("```json"):
                        return s.replace("```json", "", 1).replace("```", "").strip()
                    if s.startswith("```"):
                        return s.replace("```", "", 1).replace("```", "").strip()
                    return s.strip()

                def _extract_md_content_via_regex(text: str) -> str:  # JSON 解析失败时的兜底：用正则从文本里抽出 md_content 的值
                    # 找到 "md_content" 键的位置
                    m = re.search(r'"md_content"\s*:\s*"', text)
                    if not m:
                        return ""
                    start = m.end()  # 跳过开头的引号
                    # 值的结束：到下一个 "thought" 键之前，或到末尾
                    rest = text[start:]
                    # 找结束引号：优先匹配 "\s*,\s*"thought" 或 "\s*} 结尾
                    end_m = re.search(r'"\s*,\s*"thought"', rest) or re.search(r'"\s*}\s*$', rest)
                    if end_m:
                        value = rest[:end_m.start()]
                    else:
                        value = rest.rstrip().rstrip('"').rstrip(',')
                    # 反转义常见转义符，恢复成可读正文
                    value = (value.replace('\\n', '\n')
                                  .replace('\\t', '\t')
                                  .replace('\\"', '"')
                                  .replace('\\\\', '\\'))
                    return value.strip()

                def get_md_content(message_content):
                    obj = message_content
                    if obj is None:
                        return ""
                    raw = obj if isinstance(obj, str) else str(obj)
                    cleaned = _strip_code_fence(raw)
                    try:
                        if isinstance(cleaned, str):
                            parsed = json.loads(cleaned)
                            # 双重 JSON 包裹
                            if isinstance(parsed, str):
                                parsed = json.loads(parsed)
                        else:
                            parsed = cleaned
                        if isinstance(parsed, dict):
                            return parsed.get("md_content")
                    except Exception as e:
                        logger.warning(f"VLM 返回非严格 JSON，启用兜底解析: {e}")
                        return _extract_md_content_via_regex(cleaned)
                    return ""

                raw_content = get_md_content(message.content)
                logger.info(f"[VLM] 识别完成，产出文本长度: {len(raw_content or '')}")
                return self._clean_markdown_text(raw_content)
            except Exception as e:
                logger.error(f"VLM API 调用失败: {e}")
                return f"[解析失败：VLM API 调用异常 - {str(e)}]"

    # ============================================================
    # 将Office文档转换为PDF以便VLM介入兜底，支持docx/xlsx/pptx，调用系统安装的LibreOffice命令行工具soffice进行转换
    # ============================================================
    async def _convert_office_to_pdf(self, input_path: str, output_path: str) -> bool:
        if input_path.lower().endswith('.pdf'):
            shutil.copy(input_path, output_path)
            return True
        logger.info("尝试将 Office 文档转换为 PDF ...")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.soffice_path, "--headless", "--convert-to", "pdf",
                "--outdir", os.path.dirname(output_path) or ".", input_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=60)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("Office 转 PDF 超时（60s），进程已终止")
                return False
            expected_pdf_path = os.path.join(os.path.dirname(output_path) or ".", os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
            if os.path.exists(expected_pdf_path) and proc.returncode == 0:
                os.rename(expected_pdf_path, output_path)
                logger.info(f"[Office→PDF] 转换成功: {input_path} → {output_path}")
                return True
            stderr_data = await proc.stderr.read() if proc.stderr else b""
            logger.warning(f"Office 转 PDF 失败: {stderr_data.decode('utf-8', errors='ignore')}")
            return False
        except Exception as e:
            logger.warning(f"调用接口工具将office文件转pdf失败: {e}")
            return False

    # ============================================================
    # 将Pandas的2D表格转标准 Markdown 格式，用于word/xlsx的解析结果
    # ============================================================
    @staticmethod
    def _convert_table_to_markdown(table_2d: list) -> str:
        """将 2D 数组转换为标准 Markdown 表格，支持空单元格，自动对齐列数。"""
        if not table_2d or len(table_2d) < 2:
            return ""
            
        max_cols = max(len(row) for row in table_2d)
        
        header_idx, table_title = 0, []
        for i, row in enumerate(table_2d):
            non_empty = [str(c).strip() for c in row if c and str(c).strip()]
            if len(non_empty) >= max(2, max_cols * 0.3):
                header_idx = i
                break
            elif non_empty:
                table_title.extend(non_empty)
                
        if header_idx >= len(table_2d) - 1:
            return ""
            
        headers = table_2d[header_idx]
        valid_headers = []
        for col_idx in range(max_cols):
            val = ""
            if col_idx < len(headers):
                val = str(headers[col_idx]).strip().replace('\n', ' ') if headers[col_idx] is not None else ""
            if not val:
                val = " "
            valid_headers.append(val)
            
        lines = []
        if table_title:
            lines.append(f"**{' '.join(table_title)}**")
            
        lines.append("| " + " | ".join(valid_headers) + " |")
        lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        
        for row in table_2d[header_idx + 1:]:
            row_cells = []
            for col_idx in range(max_cols):
                if col_idx < len(row):
                    cell = row[col_idx]
                    val = str(cell).strip().replace('\n', ' ').replace('|', '\\|') if cell is not None else ""
                else:
                    val = ""
                row_cells.append(val)
            lines.append("| " + " | ".join(row_cells) + " |")
            
        return "\n".join(lines).strip()

    # ============================================================
    # 清洗Markdown文本：规范化表格、换行、空行、去除表情包
    # ============================================================
    def _clean_markdown_text(self, text: str) -> str:
        if not text:
            return text
        text = self._convert_html_tables_to_markdown(text)
        lines = text.splitlines()
        result = []
        table_buffer = []
        in_table = False

        def flush_table():
            nonlocal table_buffer
            if not table_buffer:
                return
            normalized = self._normalize_markdown_table(table_buffer)
            result.append(normalized if normalized else "\n".join(table_buffer))
            table_buffer = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("|"):
                table_buffer.append(stripped)
                in_table = True
                continue
            if in_table:
                flush_table()
                in_table = False
            result.append(line)

        if in_table:
            flush_table()
        text = "\n".join(result)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = emoji.replace_emoji(text, replace="")

        return text.strip()

    # ============================================================
    # 将 HTML 表格转换为 RAG 友好的 Markdown（完整支持 rowspan/colspan、多级表头、整行/整列标题）
    # ============================================================
    def _convert_html_tables_to_markdown(self, text: str) -> str:
        import html as html_mod
        from lxml import etree

        if not text:
            return text

        SEP = " / "  # 多级表头扁平化时的层级分隔符

        def clean_cell_text(s: str) -> str:  # 清洗单元格文本，避免破坏 Markdown 表格结构
            s = html_mod.unescape(s)
            s = re.sub(r'<br\s*/?>', ' ', s, flags=re.I)   # <br> -> 空格
            s = re.sub(r'<[^>]+>', '', s)                  # 去标签
            s = re.sub(r'\s+', ' ', s).strip()             # 压缩空白
            return s

        def cell_span(el):
            rs = int(el.get('rowspan', '1') or 1)
            cs = int(el.get('colspan', '1') or 1)
            return rs, cs

        def build_grid(trs):
            grid = []
            occ = {}  # (r,c) -> (origin_cell_dict, remaining_rows)
            for r, tr in enumerate(trs):
                cells = [c for c in tr if c.tag in ('td', 'th')]
                grid.append([])
                c = 0
                ci = 0
                while True:
                    if (r, c) in occ:
                        origin, rem = occ.pop((r, c))
                        carried = {
                            'text': origin['text'], 'rs': 0, 'cs': origin['cs'],
                            'cont': True, 'col': c,
                        }
                        grid[r].append(carried)
                        if rem > 1:
                            occ[(r + 1, c)] = (origin, rem - 1)
                        c += 1
                        continue
                    if ci >= len(cells):
                        break
                    cell = cells[ci]
                    ci += 1
                    rs, cs = cell_span(cell)
                    t = clean_cell_text(''.join(cell.itertext()))
                    for k in range(cs):
                        cell_dict = {
                            'text': t, 'rs': rs, 'cs': cs, 'cont': False, 'col': c,
                        }
                        grid[r].append(cell_dict)
                        if rs > 1:
                            occ[(r + 1, c)] = (cell_dict, rs - 1)
                        c += 1
            maxc = max((len(r) for r in grid), default=0)
            for r in grid:
                if len(r) < maxc:
                    pad = maxc - len(r)
                    r.extend([{'text': '', 'rs': 1, 'cs': 1, 'cont': False, 'col': len(r)}] * pad)
            return grid

        def convert_one(table_html: str) -> str:
            # 注意：不在此处对整段 HTML 做 html.unescape，否则会把文本里的裸 & 或 < 误当实体/标签，破坏表格结构（lxml 解析时已自行解码实体）。
            try:
                frag = etree.fromstring(table_html, etree.HTMLParser())
            except Exception:
                # 解析失败则原样返回，交给下游 normalize 兜底
                return table_html
            if frag is None:
                return table_html
            if frag.tag != 'table':
                t = frag.find('.//table')
                frag = t if t is not None else frag
            trs = frag.findall('tr')
            if not trs:
                return table_html

            grid = build_grid(trs)
            nrows = len(grid)
            if nrows == 0:
                return ""
            maxc = max((len(r) for r in grid), default=0)

            # 1) 纵向标题列：首行某 cell 的 rs >= 总行数（占满整列高度）→ 提取为 caption，整列删除。要求 nrows > 1，否则单行表格的每个 cell 都会误判为"占满整列"，导致表格被清空。
            col_full_height = set()
            if nrows > 1:
                for cell in grid[0]:
                    if cell['rs'] >= nrows and cell['col'] not in col_full_height:
                        col_full_height.add(cell['col'])
            titles = []
            for c in sorted(col_full_height):
                t = grid[0][c]['text'] if c < len(grid[0]) else ''
                if t and t not in titles:  # 去重：同一原始 cell 跨多列时只取一次
                    titles.append(t)
            if col_full_height:
                grid = [[cell for cell in row if cell['col'] not in col_full_height] for row in grid]
                for row in grid:
                    for ci, cell in enumerate(row):
                        cell['col'] = ci
                maxc = max((len(r) for r in grid), default=0)
                if maxc == 0:
                    return ("**" + " | ".join(titles) + "**\n") if titles else ""

            # 2) 横向标题行：原始 <tr> 只有 1 个 td/th 且其 colspan >= 当前列数、文本非空 → 整行合并标题
            htitles = []
            drop_rows = set()
            for ri, tr in enumerate(trs):
                cells = [cc for cc in tr if cc.tag in ('td', 'th')]
                if len(cells) == 1:
                    rs, cs = cell_span(cells[0])
                    txt = clean_cell_text(''.join(cells[0].itertext()))
                    if cs >= maxc and txt:
                        htitles.append(txt)
                        drop_rows.add(ri)
            if drop_rows:
                grid = [g for ri, g in enumerate(grid) if ri not in drop_rows]
                nrows = len(grid)
            titles = htitles + titles  # 横向标题在前，纵向标题在后
            if nrows == 0:
                return ("**" + " | ".join(titles) + "**\n") if titles else ""

            # 3) 在删除标题后重新计算行级标志
            row_has_rowspan = [
                any((not cell['cont']) and cell['rs'] > 1 for cell in row)
                for row in grid
            ]
            row_has_continuation = [
                any(cell['cont'] for cell in row)
                for row in grid
            ]
            any_multirow = any(row_has_rowspan)

            # 4) 划分表头/数据行
            if not any_multirow:
                header_rows_idx = [0]
                data_start = 1
            else:
                # 第一行既不承接上方 span、也不向下发起 rowspan → 第一条数据行
                data_start = nrows
                for i in range(nrows):
                    if not row_has_continuation[i] and not row_has_rowspan[i]:
                        data_start = i
                        break
                header_rows_idx = list(range(0, data_start)) if data_start > 0 else []
            if not header_rows_idx and data_start < nrows:
                header_rows_idx = [data_start]
                data_start += 1

            # 5) 多级表头扁平化为单表头行
            if header_rows_idx:
                header = []
                for c in range(maxc):
                    parts = []
                    for ri in header_rows_idx:
                        cell = grid[ri][c] if c < len(grid[ri]) else None
                        v = cell['text'] if cell else ''
                        if v and v not in parts:
                            parts.append(v)
                    header.append(SEP.join(parts))
                data_rows = grid[data_start:]
            else:
                header = [' '] * maxc
                data_rows = grid

            # 6) 拼接 Markdown
            out = []
            if titles:
                out.append("**" + " | ".join(titles) + "**")
                out.append("")
            out.append("| " + " | ".join(header) + " |")
            out.append("| " + " | ".join(["---"] * maxc) + " |")
            for row in data_rows:
                cells = [cell['text'].replace('|', r'\|') for cell in row]
                if len(cells) < maxc:
                    cells += [''] * (maxc - len(cells))
                out.append("| " + " | ".join(cells) + " |")
            return "\n".join(out)

        table_pattern = re.compile(r'<table[^>]*>.*?</table>', re.I | re.S)
        return table_pattern.sub(lambda m: convert_one(m.group(0)), text)

    def _strip_html_tags(self, text: str) -> str:
        """去除 HTML 标签，保留内部文本"""
        if not text:
            return ''
        # 替换 <br> 为换行
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        # 移除所有其他 HTML 标签
        text = re.sub(r'<[^>]+>', '', text)
        # 解码 HTML 实体
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&amp;', '&')
        text = text.replace('&quot;', '"')
        text = text.replace('&#39;', "'")
        text = text.replace('&apos;', "'")
        return text

    def _normalize_markdown_table(self, table_lines: list) -> str:
        import re

        if not table_lines:
            return ""

        def is_separator_line(line: str) -> bool:
            stripped = line.strip()
            if not (stripped.startswith("|") and stripped.endswith("|")):
                return False

            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if not cells:
                return False

            # 每个单元格只能由冒号和横线组成，且至少包含一个横线
            for cell in cells:
                if not re.fullmatch(r':?-+:?', cell):
                    return False
            return True

        # 1. 清洗：去空行、去所有分隔行
        data_lines = []
        for line in table_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if is_separator_line(stripped):
                continue
            data_lines.append(stripped)

        if not data_lines:
            return ""

        # 2. 解析每一行
        rows = []
        for line in data_lines:
            if not (line.startswith("|") and line.endswith("|")):
                continue
            parts = [c.strip() for c in line.split("|")[1:-1]]
            if parts:
                rows.append(parts)

        if not rows:
            return ""

        # 3. 对齐列数
        max_cols = max(len(row) for row in rows)
        if max_cols < 2:
            return "\n".join(data_lines)

        normalized_rows = []
        for row in rows:
            if len(row) < max_cols:
                row = row + [""] * (max_cols - len(row))
            elif len(row) > max_cols:
                row = row[:max_cols]
            normalized_rows.append(row)

        # 4. 重建标准 Markdown 表格
        result = []
        result.append("| " + " | ".join(normalized_rows[0]) + " |")
        result.append("| " + " | ".join(["---"] * max_cols) + " |")
        for row in normalized_rows[1:]:
            result.append("| " + " | ".join(row) + " |")

        return "\n".join(result)

    # ============================================================
    # Office 解析产物统一落盘：merged.md(整篇Markdown) + anchors.json(图↔内容映射)
    # ============================================================
    def _office_output_dir(self, file_path: str, display_name: str | None = None, uid: str = "") -> Path:
        stem = Path(display_name or file_path).stem
        stem = stem.replace(" ", "_")
        if uid:
            return PARSE_RESULT_DIR / uid / stem
        return PARSE_RESULT_DIR / stem

    def _save_anchor_image(self, out_dir: Path, img_bytes: bytes, img_name: str) -> str:
        img_dir = out_dir / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r'[^\w.\-]', "_", img_name or "image.png") or "image.png"
        dst = img_dir / safe_name
        if not dst.exists():
            dst.write_bytes(img_bytes)
            logger.info(f"[parse_file] 图片落盘: {dst} ({len(img_bytes)} bytes)")
        return str(dst)

    async def _write_merged_md(self, out_dir: Path, md_text: str) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "merged.md"
        async with aiofiles.open(p, "w", encoding="utf-8") as f:
            await f.write(md_text or "")
        logger.info(f"[parse_file] 整篇 Markdown 已输出到：{p}")
        return p

    async def _write_anchors_json(self, out_dir: Path, anchors: list) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "anchors.json"
        async with aiofiles.open(p, "w", encoding="utf-8") as f:
            await f.write(json.dumps({"anchors": anchors, "filters": []}, ensure_ascii=False, indent=2))
        logger.info(f"[parse_file] 图片锚点已输出到：{p} (锚点 {len(anchors)} 条)")
        return p

    async def _write_office_outputs(self, out_dir: Path, md_text: str, anchors: list) -> None:
        await self._write_merged_md(out_dir, md_text)
        await self._write_anchors_json(out_dir, anchors)

    # ============================================================
    # Word(.docx) 原生解析：正文+表格按阅读顺序提取文本；图片仅对该图做 VLM 识别，并把 VLM 文本在原位置替换图片占位；无图内容一律原生提取(不走 PDF)。
    # ============================================================
    def _iter_word_blocks(self, doc):
        """递归遍历 docx 正文/表格，按阅读顺序产出块。

        产出元素为 ('text', 段落文本) 或 (段落元素 p, run 文本段列表) 用于后续定位图片；为避免过度抽象，这里直接产出 (paragraph_el, ordered_segments)，由调用方把段落里的文本与图片按 run 顺序组装成带占位符的正文。
        """
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P

        parent_elm = doc.element.body
        for child in parent_elm.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, doc)
            elif isinstance(child, CT_Tbl):
                table = Table(child, doc)
                for row in table.rows:
                    for cell in row.cells:
                        # 单元格内可能再有段落/嵌套表格，逐元素递归
                        for item in cell._tc.iterchildren():
                            if isinstance(item, CT_P):
                                yield Paragraph(item, cell)
                            elif isinstance(item, CT_Tbl):
                                sub = Table(item, cell)
                                for r2 in sub.rows:
                                    for c2 in r2.cells:
                                        for it2 in c2._tc.iterchildren():
                                            if isinstance(it2, CT_P):
                                                yield Paragraph(it2, c2)

    def _word_run_images(self, paragraph, doc) -> list[tuple[bytes, str]]:
        """返回段落中嵌入图片的 (字节, 关系ID)。"""
        from docx.oxml.ns import qn
        images = []
        for blip in paragraph._p.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            if not rid:
                continue
            try:
                part = doc.part.related_parts[rid]
                images.append((part.blob, rid))
            except Exception as e:
                logger.warning(f"[Word图片] 关系 {rid} 读取失败: {e}")
        return images

    async def _parse_word(self, file_path: str, display_name: str | None = None, uid: str = "") -> str:
        from docx import Document

        out_dir = self._office_output_dir(file_path, display_name, uid)
        anchors: list = []
        md_lines: list[str] = []
        anchor_idx = 0

        try:
            doc = Document(file_path)
            for para in self._iter_word_blocks(doc):
                text = para.text or ""
                images = self._word_run_images(para, doc)
                if not images:
                    if text.strip():
                        md_lines.append(text.strip())
                    continue
                segs: list[str] = []
                if text.strip():
                    segs.append(text.strip())
                for img_bytes, rid in images:
                    vlm_text, img_url = await self._process_office_image(
                        img_bytes, f"image_{anchor_idx + 1}.png", out_dir, anchors,
                    )
                    if not vlm_text:
                        logger.info(f"[Word图片] {rid} VLM 未产出有效文本，跳过")
                        continue
                    anchor_idx += 1
                    if img_url:
                        segs.append(f"{vlm_text}\n\n![]({img_url})")
                    else:
                        segs.append(vlm_text)
                if segs:
                    md_lines.append("\n\n".join(segs))
        except Exception as e:
            logger.error(f"Word 原生解析失败: {e}")
            return ""

        text = "\n\n".join(md_lines)
        text = self._clean_markdown_text(text)
        await self._write_office_outputs(out_dir, text, anchors)
        return text

    # ============================================================
    # 统一提取与落盘 Office 图片：把 image_bytes 做 VLM 识别、原位置替换、落 anchors。
    # ============================================================
    def _image_url_from_path(self, out_dir: Path, img_path: str) -> str:
        """把落盘的图片绝对路径转为 /api/media URL。"""
        if not img_path:
            return ""
        try:
            rel = Path(img_path).relative_to(out_dir)
            # rel 形如 images/xxx.png
            parts = rel.parts
            if len(parts) >= 2 and parts[0] == "images":
                stem = out_dir.name
                filename = parts[-1]
                from urllib.parse import quote
                stem_enc = quote(stem, safe="")
                filename_enc = quote(filename, safe="")
                return f"/api/media/extracted/{stem_enc}/{filename_enc}"
        except Exception:
            pass
        return ""

    async def _process_office_image(
        self, img_bytes: bytes, img_name: str, out_dir: Path, anchors: list,
    ) -> tuple[str, str]:
        vlm_text = await self._call_vlm(img_bytes) or ""
        if not vlm_text:
            logger.info(f"[Office图片] {img_name} VLM 未产出有效文本，跳过")
            return "", ""
        img_path = self._save_anchor_image(out_dir, img_bytes, img_name)
        img_url = self._image_url_from_path(out_dir, img_path)
        anchors.append({
            "page_no": None,
            "block_index": None,
            "type": "image",
            "text": vlm_text,
            "image_paths": [img_path] if img_path else [],
            "image_name": img_name,
        })
        return vlm_text, img_url

# =====================================================================
# 本地测试
# =====================================================================
if __name__ == "__main__":
    import asyncio
    test_file = "/root/iron/Abort_MyAgent_Doc/data/test_data/Tutor Book_20251010_Summer Allowance Rule Sharing.xlsx"
    parser = DocumentParser()
    text = asyncio.run(parser.parse_file(test_file))
    output_file = os.path.splitext(test_file)[0] + ".md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"解析结果已输出到: {output_file}")