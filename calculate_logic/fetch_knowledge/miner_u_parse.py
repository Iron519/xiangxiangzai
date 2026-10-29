import os
import re
import json
import logging
import shutil
import fitz
import base64
import mimetypes
import requests
from pathlib import Path
from urllib3.exceptions import InsecureRequestWarning
from openai import OpenAI
from dotenv import load_dotenv, find_dotenv
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
load_dotenv(find_dotenv())

# ================= 环境变量配置 =================
REQUIRED_ENV_VARS = ["ACCESSCODE", "BASE_URL", "VLM_MODEL", "MINERU_URL", "MINERU_API_KEY", "WORK_PATH"]
config = {name: os.getenv(name, "").strip() for name in REQUIRED_ENV_VARS}

# 解析结果统一输出目录
PARSE_RESULT_DIR = Path(config.get("WORK_PATH", "/root/iron/MyAgent")) / "calculate_logic" / "fetch_knowledge" / "parse_result"
PARSE_RESULT_DIR.mkdir(parents=True, exist_ok=True)

# ================= 运行日志配置 —— 直接复用DocumentParser =================
logger = logging.getLogger("DocumentParser")
def ensure_logger_handler():  # 仅为"单独运行本文件"时提供控制台输出兜底，不影响被 parse_file 导入时的日志合并
    if not logger.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(_handler)
        logger.setLevel(logging.INFO)

class MinerUProcessor:
    def __init__(
        self,
        pdf_path: str,
        output_dir: str | Path = str(PARSE_RESULT_DIR),
        page_pdf_dir: str | Path = "page_pdfs",
        page_result_dir: str | Path = "page_results",
        img_dir: str | Path = "images",
        page_render_dir: str | Path = "page_renders",
        # ====== 判断页面是否以图片为主的阈值（AND) =====
        image_only_ratio_threshold: float = 0.5,  # S(图片并集)/S(页面) >= 该值
        image_only_max_text_chars: int = 1200,  # 页面内文字数 <= 该值
        image_only_max_text_lines: int = 30,  # 页面内文字行数 <= 该值
        # ====== 插图可供输出给用户的过滤阈值（AND）=====
        min_image_area_ratio: float = 0.03,  # 插图 bbox 面积 / 页面面积 >= 该值才达标（过滤 logo/小装饰）
        min_image_side_px: int = 30,  # 插图宽、高最小值（像素），任一小于则该插图不达标
        access_code: str = config["ACCESSCODE"],
        mineru_url: str = config["MINERU_URL"],
        mineru_api_key: str = config["MINERU_API_KEY"],
        base_url: str = config["BASE_URL"],
        vlm_model: str = config["VLM_MODEL"],
    ):
        self.pdf_path = str(pdf_path)
        self.file_stem = Path(self.pdf_path).stem.replace(" ", "_")  # 文档名（不含扩展名），空格替换为下划线避免 URL 问题
        self.output_dir = Path(output_dir)

        # 每份 PDF 单独一个子目录，所有中间/最终产物都挂在该文档目录下，防止不同文档的 page_results/images/merged.md 相互覆盖
        self.doc_dir = self.output_dir / self.file_stem
        self.page_pdf_dir = self.doc_dir / page_pdf_dir
        self.page_result_dir = self.doc_dir / page_result_dir
        self.img_dir = self.doc_dir / img_dir
        self.page_render_dir = self.doc_dir / page_render_dir

        self.image_only_ratio_threshold = image_only_ratio_threshold
        self.image_only_max_text_chars = image_only_max_text_chars
        self.image_only_max_text_lines = image_only_max_text_lines
        self.min_image_area_ratio = min_image_area_ratio
        self.min_image_side_px = min_image_side_px

        # 记录"图片本地路径 -> 是否有业务意义(AI判断)"，供 build_page_anchors 建立锚点
        self._meaning_by_path = {}

        # 记录"原始图片文件名 -> 落盘本地路径"，供 build_page_anchors 把 middle_json 的image_path(原始名)解析成落盘路径，使过滤/锚点/内联渲染三者的图片路径口径一致。
        self._basename_to_local = {}

        self.access_code = access_code
        self.mineru_url = mineru_url
        self.mineru_api_key = mineru_api_key
        self.base_url = base_url
        self.vlm_model = vlm_model

        # 建目录 + 清空本份文档上次遗留的旧产物，避免残留/覆盖污染
        self._prepare_document_dirs()

    # ================= 0. 解析生成文档的目录准备与打扫 =================
    def _prepare_document_dirs(self):
        """建立本份文档的产物目录；若已存在旧产物则先清空，保证每次解析结果干净、不残留上一份。"""
        self.doc_dir.mkdir(parents=True, exist_ok=True)
        for d in [self.page_pdf_dir, self.page_result_dir, self.img_dir, self.page_render_dir]:
            if d.exists():
                shutil.rmtree(d)  # 清掉本份文档上次解析遗留的全部文件
            d.mkdir(parents=True, exist_ok=True)
        # 清掉本份文档上次的整篇拼接结果
        merged_md_path = self.doc_dir / "merged.md"
        if merged_md_path.exists():
            merged_md_path.unlink()
        logger.info(f"[MinerU] 已准备文档输出目录（已清空旧产物）：{self.doc_dir}")

    # ================= 1. 拆分 PDF 为单页，并存入目录 ==========================
    def split_pdf_to_pages(self):
        doc = fitz.open(self.pdf_path)
        file_stem = Path(self.pdf_path).stem
        page_files = []  # 页码+单页文件组成的列表
        for idx in range(len(doc)):
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=idx, to_page=idx)
            out_path = self.page_pdf_dir / f"{file_stem}_page_{idx + 1:04d}.pdf"  # 将PDF单页存入指定目录
            new_doc.save(out_path)
            new_doc.close()
            page_files.append({"page_no": idx + 1, "pdf_path": str(out_path)})
        doc.close()
        return page_files

    # =================== 2. 调用 MinerU解析单页PDF ======================
    def call_mineru(self, pdf_path: str):
        headers = {"apikey": self.mineru_api_key,}
        data = {
            "return_images": True,
            "return_middle_json": True,
            "return_content_list": True,
        }
        with open(pdf_path, "rb") as f:
            files = {"files": f}
            response = requests.post(
                self.mineru_url,
                headers=headers,
                files=files,
                data=data,
                verify=False,
                timeout=300,
            )
        response.raise_for_status()
        return response.json()

    # =================== 3. 从 MinerU 返回结果中取单页数据，并按元素类型划分 ==========================
    @staticmethod
    def extract_doc_data_from_mineru_result(result: dict):
        if not result or "results" not in result or not result["results"]:
            return {
                "md_content": "",
                "content_list": [],
                "middle_json": None,
                "images": {},
            }
        doc_name = list(result["results"].keys())[0]
        doc_data = result["results"][doc_name] or {}
        return {
            "md_content": doc_data.get("md_content", "") or "",
            "content_list": doc_data.get("content_list", []) or [],
            "middle_json": doc_data.get("middle_json"),
            "images": doc_data.get("images", {}) or {},
        }

    # ================= 4. 处理页面内元素数据并计算该页是否仅包含图片 ====================
    @staticmethod
    def ensure_json_obj(data):  # 将MinerU 的 middle_json 统一转成 Python 对象
        if data is None:
            return None
        if isinstance(data, (dict, list)):
            return data
        if isinstance(data, str):
            text = data.strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except Exception:
                return None
        return None

    @staticmethod
    def normalize_bbox(bbox):  # 将 bbox 规范成 [x0, y0, x1, y1]
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            return None
        try:
            x0, y0, x1, y1 = map(float, bbox[:4])
            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            return [x0, y0, x1, y1]
        except Exception:
            return None

    @staticmethod
    def box_area(bbox):
        b = MinerUProcessor.normalize_bbox(bbox)
        if not b:
            return 0.0
        x0, y0, x1, y1 = b
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)

    @staticmethod
    def union_bbox_area(bboxes):  # 计算多个 bbox 的并集面积，避免重复累加重叠区域
        rects = []
        for bbox in bboxes:
            b = MinerUProcessor.normalize_bbox(bbox)
            if not b:
                continue
            x0, y0, x1, y1 = b
            if x1 > x0 and y1 > y0:
                rects.append((x0, y0, x1, y1))
        if not rects:
            return 0.0
        xs = sorted(set([r[0] for r in rects] + [r[2] for r in rects]))
        total_area = 0.0
        for i in range(len(xs) - 1):
            x_left = xs[i]
            x_right = xs[i + 1]
            width = x_right - x_left
            if width <= 0:
                continue
            intervals = []
            for x0, y0, x1, y1 in rects:
                if x0 <= x_left and x1 >= x_right:
                    intervals.append((y0, y1))
            if not intervals:
                continue
            intervals.sort()
            merged = []
            for start, end in intervals:
                if not merged or start > merged[-1][1]:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            height_sum = sum(max(0.0, end - start) for start, end in merged)
            total_area += width * height_sum
        return total_area

    @staticmethod
    def find_page_size_from_middle_json(middle_json_obj):  # 获取页面尺寸的方法1：尝试从 MinerU middle_json 中寻找
        if middle_json_obj is None:
            return None
        candidates = []
        def walk(obj):
            if isinstance(obj, dict):
                candidates.append(obj)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)
        walk(middle_json_obj)
        for d in candidates:
            if not isinstance(d, dict):
                continue
            for key in ["page_info", "page", "page_size"]:
                v = d.get(key)
                if isinstance(v, dict):
                    w = v.get("width") or v.get("w") or v.get("page_width")
                    h = v.get("height") or v.get("h") or v.get("page_height")
                    if w and h:
                        try:
                            return float(w), float(h)
                        except Exception:
                            pass
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    try:
                        return float(v[0]), float(v[1])
                    except Exception:
                        pass
            w = d.get("width") or d.get("page_width") or d.get("w")
            h = d.get("height") or d.get("page_height") or d.get("h")
            if w and h:
                try:
                    return float(w), float(h)
                except Exception:
                    pass
        return None

    @staticmethod
    def get_page_area_from_pdf(pdf_path: str):  # 获取页面尺寸的方法2：用 PDF 原生页面尺寸获取页面面积
        doc = fitz.open(pdf_path)
        page = doc[0]
        width = page.rect.width
        height = page.rect.height
        area = width * height
        doc.close()
        return width, height, area

    @staticmethod
    def is_image_like_block(block: dict):  # 判断一个 MinerU block 是否是图片块
        if not isinstance(block, dict):
            return False
        values = []
        for key in ["type", "category", "block_type", "layout_type", "kind"]:
            v = block.get(key)
            if isinstance(v, str):
                values.append(v.lower())
        image_keywords = ["image", "figure", "fig", "picture", "pic"]
        if any(any(k in v for k in image_keywords) for v in values):
            return True
        for key in ["img_path", "image_path", "img", "image", "figure_path"]:
            if block.get(key):
                return True
        return False

    @staticmethod
    def extract_image_bboxes_from_middle_json(middle_json):  # 从 middle_json 递归提取图片块 bbox
        obj = MinerUProcessor.ensure_json_obj(middle_json)
        if obj is None:
            return []
        image_bboxes = []
        def get_bbox(d: dict):
            for key in ["bbox", "box", "layout_bbox", "position"]:
                v = d.get(key)
                if isinstance(v, (list, tuple)) and len(v) >= 4:
                    return list(v[:4])
            poly = d.get("poly")
            if isinstance(poly, (list, tuple)) and len(poly) >= 8:
                try:
                    xs = [float(poly[i]) for i in range(0, len(poly), 2)]
                    ys = [float(poly[i]) for i in range(1, len(poly), 2)]
                    return [min(xs), min(ys), max(xs), max(ys)]
                except Exception:
                    return None
            return None
        def walk(x):
            if isinstance(x, dict):
                if MinerUProcessor.is_image_like_block(x):
                    bbox = get_bbox(x)
                    if bbox:
                        image_bboxes.append(bbox)
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for item in x:
                    walk(item)
        walk(obj)
        return image_bboxes

    @staticmethod
    def count_text_from_md(md_content: str):  # 用 MinerU md_content 粗略统计文本量（会去掉 Markdown 图片引用）
        if not md_content:
            return 0, 0
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", md_content)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        meaningful_lines = [
            line
            for line in lines
            if not line.isdigit()
            and len(line) > 3
            and not re.match(r"^[-|:\s]+$", line)
        ]
        char_count = len("".join(meaningful_lines))
        line_count = len(meaningful_lines)
        return char_count, line_count

    def judge_image_only_by_mineru(  # 判断该页是否仅包含图片（图片和文字的占比阈值）
        self,
        *,
        pdf_path: str,
        md_content: str,
        middle_json,
        images_dict: dict,
    ):
        image_count = len(images_dict or {})
        char_count, line_count = self.count_text_from_md(md_content)
        middle_json_obj = self.ensure_json_obj(middle_json)
        page_size = self.find_page_size_from_middle_json(middle_json_obj)

        if page_size:
            width, height = page_size
            page_area = width * height
            area_source = "mineru_middle_json"
        else:
            width, height, page_area = self.get_page_area_from_pdf(pdf_path)
            area_source = "pdf_native"
        image_bboxes = self.extract_image_bboxes_from_middle_json(middle_json_obj)
        image_area = self.union_bbox_area(image_bboxes)
        image_ratio = image_area / page_area if page_area > 0 else 0.0
        is_image_only = False
        if image_count > 0 and image_ratio >= self.image_only_ratio_threshold:
            if (
                char_count <= self.image_only_max_text_chars
                and line_count <= self.image_only_max_text_lines
            ):
                is_image_only = True
        debug_info = {
            "image_count": image_count,
            "image_bbox_count": len(image_bboxes),
            "image_area": round(image_area, 2),
            "page_width": round(width, 2),
            "page_height": round(height, 2),
            "page_area": round(page_area, 2),
            "area_source": area_source,
            "image_ratio": round(image_ratio, 4),
            "char_count": char_count,
            "line_count": line_count,
            "is_image_only": is_image_only,
        }
        return is_image_only, debug_info

    # ======================== 5. 在正式处理单页之前，先准备单页各项属性的信息 =================================
    def prepare_one_page_before_process(self, page_info: dict):
        page_no = page_info["page_no"]
        pdf_path = page_info["pdf_path"]

        result = self.call_mineru(pdf_path)
        raw_save_path = self.page_result_dir / f"page_{page_no:04d}_mineru.json"  # 把页面的原始内容存入文件
        with open(raw_save_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        doc_data = self.extract_doc_data_from_mineru_result(result)
        md_content = doc_data["md_content"]
        content_list = doc_data["content_list"]
        middle_json = doc_data["middle_json"]
        images_dict = doc_data["images"]

        is_image_only, debug_info = self.judge_image_only_by_mineru(  # 判断单页内是否以图片为主
            pdf_path=pdf_path,
            md_content=md_content,
            middle_json=middle_json,
            images_dict=images_dict,
        )

        debug_save_path = self.page_result_dir / f"page_{page_no:04d}_debug.json"
        with open(debug_save_path, "w", encoding="utf-8") as f:
            json.dump(debug_info, f, ensure_ascii=False, indent=2)

        logger.debug(
            f"[MinerU] 第 {page_no} 页预判断 "
            f"image_count={debug_info['image_count']}, "
            f"image_bbox_count={debug_info['image_bbox_count']}, "
            f"image_ratio={debug_info['image_ratio']}, "
            f"char_count={debug_info['char_count']}, "
            f"line_count={debug_info['line_count']}, "
            f"is_image_only={debug_info['is_image_only']}, "
            f"area_source={debug_info['area_source']}"
        )

        return {
            "page_no": page_no,
            "pdf_path": pdf_path,
            "is_image_only": is_image_only,
            "debug_info": debug_info,
            "md_content": md_content,
            "content_list": content_list,
            "middle_json": middle_json,
            "images_dict": images_dict,
            "mineru_result": result,
        }

    # ======================= 6. 渲染整页 PDF 为图片 ==================================
    def render_page_pdf_to_image(self, page_pdf_path: str, page_no: int, zoom: float = 2.0):
        doc = fitz.open(page_pdf_path)
        page = doc[0]
        mat = fitz.Matrix(zoom, zoom)  # 放大2倍
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out_path = self.page_render_dir / f"page_{page_no:04d}_render.png"
        pix.save(out_path)
        doc.close()
        return str(out_path)

    # ======================== 7. Markdown 图片引用处理 =================================
    @staticmethod
    def extract_image_refs(md_content: str):  # 通过正则取出图片路径
        pattern = r"!\[[^\]]*\]\(([^)]+)\)"
        return re.findall(pattern, md_content or "")

    @staticmethod  # 判断图片格式
    def guess_ext_from_bytes(img_bytes: bytes):
        if img_bytes.startswith(b"\x89PNG"):
            return "png"
        elif img_bytes.startswith(b"\xff\xd8"):
            return "jpg"
        elif img_bytes.startswith(b"GIF87a") or img_bytes.startswith(b"GIF89a"):
            return "gif"
        elif img_bytes.startswith(b"BM"):
            return "bmp"
        elif img_bytes.startswith(b"RIFF") and b"WEBP" in img_bytes[:16]:
            return "webp"
        return "bin"

    def save_images_from_result(self, images_dict, md_refs, page_no: int):  # 将 MinerU 返回的 images 字典落盘，并建立：markdown 图片引用 -> 本地文件路径
        saved_local_paths = []
        ref_to_local = {}

        for idx, (key, value) in enumerate((images_dict or {}).items(), start=1):
            if not value:
                continue
            if isinstance(value, str) and value.startswith("data:image"):
                header, b64_data = value.split(",", 1)
                mime = header.split(":", 1)[1].split(";", 1)[0]
                ext_guess = mimetypes.guess_extension(mime)
                ext = ext_guess.lstrip(".") if ext_guess else None
            else:
                b64_data = value
                ext = None
            try:
                img_bytes = base64.b64decode(b64_data)
            except Exception:
                logger.warning(f"[MinerU] 第 {page_no} 页图片 base64 解析失败，key={key}")
                continue
            if not ext:
                ext = self.guess_ext_from_bytes(img_bytes)
            key_name = Path(str(key)).name if key else f"img_{idx}.{ext}"
            if "." not in key_name:
                key_name = f"{key_name}.{ext}"
            out_name = f"page_{page_no:04d}_{key_name}"  # 图片文件名
            out_path = self.img_dir / out_name
            with open(out_path, "wb") as f:
                f.write(img_bytes)
            # middle_json 的 image_path/锚点引用是原始文件名(如 501fbc....jpg)，这里记录其对应的落盘路径，供 build_page_anchors 用统一落盘路径把"达标插图"正确写进 anchors.json。
            self._basename_to_local[Path(str(key)).name] = str(out_path)
            saved_local_paths.append(
                {
                    "origin_key": key,
                    "origin_basename": Path(str(key)).name if key else "",
                    "local_path": str(out_path),
                }
            )
        remaining_saved = saved_local_paths.copy()

        for ref in md_refs:  # 图片key与markdown的引用路径不同，需要一一对应。1. 先按 basename 精确匹配
            ref_name = Path(ref).name
            matched = None
            for item in remaining_saved:
                if item["origin_basename"] == ref_name:
                    matched = item
                    break
            if matched:
                ref_to_local[ref] = matched["local_path"]
                remaining_saved.remove(matched)

        unmatched_refs = [ref for ref in md_refs if ref not in ref_to_local]   # 再按顺序兜底匹配
        for ref, item in zip(unmatched_refs, remaining_saved):
            ref_to_local[ref] = item["local_path"]
        return ref_to_local

    # ====================== 8. 调用 VLM 进行解析 ===================================
    def call_vlm_describe_image(
        self,
        image_path: str,
        page_no: int | None = None,
        whole_page: bool = False,
    ):
        ext = Path(image_path).suffix.lower().lstrip(".") or "png"
        mime = mimetypes.types_map.get(f".{ext}", f"image/{ext}")

        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        if whole_page:
            prompt = (
                "请将这张 PDF 页面截图无损转写为高质量中文 Markdown 文本。\n"
                "要求：\n"
                "1. 保留页面中的标题、段落、表格、流程、关键说明；\n"
                "2. 如果是扫描件或整页图片，请尽可能提取其中的业务信息；\n"
                "3. 不要描述无意义的装饰元素；\n"
                "4. 不要臆测看不清的内容；\n"
                "5. 只输出 Markdown 正文，不要输出解释。"
            )
        else:
            prompt = (
                "请分析这张图片并输出 JSON，格式严格如下：\n"
                "{\"meaningful\": true或false, \"desc\": \"...\"}\n"
                "其中：\n"
                "1. meaningful：判断该图是否'有业务/内容价值'——流程图、架构图、组织图、表格、"
                "数据截图、实物照片等为 true；纯 logo、图标、装饰性元素、二维码、水印、无内容装饰为 false。\n"
                "2. desc：对图片做准确、客观、简洁但信息充分的中文描述；若是有价值图，"
                "流程图/架构图概括结构与关键节点，表格/截图提取可见关键信息。\n"
                "3. 只输出 JSON，不要任何解释或 Markdown 标记。"
            )

        
        client = OpenAI(api_key=self.access_code, base_url=self.base_url, timeout=180)
        response = client.chat.completions.create(
            model=self.vlm_model,
            temperature=0.1,
            messages=[{"role": "user", 
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]}],
        )
        try:
            content = response.choices[0].message.content
            if isinstance(content, list):
                texts = []
                for x in content:
                    if isinstance(x, dict) and x.get("type") == "text":
                        texts.append(x.get("text", ""))
                content = "".join(texts)
            content = str(content or "").strip()

            if whole_page:
                return content  # 整页转写：直接返回正文文本

            # 单图描述：预期为 JSON {"meaningful": bool, "desc": "..."}
            desc = content
            meaningful = True  # 解析失败时保守保留（不误伤）
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    desc = str(parsed.get("desc") or "").strip() or desc
                    meaningful = bool(parsed.get("meaningful", True))
            except Exception:
                # 若 VLM 未按 JSON 返回，退化为把整段当描述、按"有意义"处理
                pass
            return desc, meaningful
        except Exception as e:
            logger.warning(f"[MinerU] 第 {page_no} 页 VLM 返回结构非标准：{e}")
            raise

    # ======================== 9. 替换 Markdown 中图片为描述 =================================
    @staticmethod
    def replace_images_with_descriptions(
        md_content,
        descriptions,
        output_dir=None,
        ref_to_local=None,
        exclude_local_paths=None,
    ):
        """把 middle_json中的 "![...](ref)" 图片替换为 "> 【插图描述】内容" + 内联图片标记。

        规则：
        - 有描述：输出 "> 【插图描述】描述" 引用块，并在其后内联 "[](<可访问URL>)"，
          让图片随正文文本一起入库、在 chat/检索中直接展示。
        - 无描述：图片若无内联可控路径则仅消除引用(不污染正文)；若可换算成 URL 则保留内联图。
        - URL 由《绝对路径→/api/media URL》换算，经 /api/media(JWT 鉴权)服务。
        - exclude_local_paths：传入"判定不达标图片"的落盘路径集合，这些图片不再内联
          URL（仍保留描述文字），使正文内联图与锚点过滤规则口径一致。
        """
        descriptions = descriptions or {}
        ref_to_local = ref_to_local or {}
        exclude = set(exclude_local_paths or [])

        def _img_url(ref: str) -> str:
            local = ref_to_local.get(ref) or ref_to_local.get(Path(ref).name)
            if local and str(local) not in exclude:
                return MinerUProcessor.image_abs_to_url(str(local), output_dir) if output_dir else _image_abs_to_url_legacy(local, output_dir)
            return ""

        def repl(match):
            full_ref = match.group(1)
            desc = descriptions.get(full_ref)
            url = _img_url(full_ref)
            block = f"\n\n> 【插图描述】{desc}\n\n" if desc else "\n\n"
            if url:
                block = f"\n\n> 【插图描述】{desc}\n\n![]({url})\n\n" if desc else f"\n\n![]({url})\n\n"
            return block
        pattern = r"!\[[^\]]*\]\(([^)]+)\)"  #  :把每个 "![...](路径)" 图片替换为 "> 【插图描述】内容" + 内联图片
        return re.sub(pattern, repl, md_content or "")

    @staticmethod
    def image_abs_to_url(abs_path: str, output_dir: str) -> str:
        """把落盘图片的绝对路径换算成可被网页渲染的 URL(/api/media/{category}/{doc}/{file})。

        FastAPI 的 /api/media(见 app/app.py)以 <output_dir>/<category>/<file> 服务图片(JWT 鉴权)。
        类别由图片所在子目录推断：
          - .../<doc>/images/page_*.jpg        -> extracted
          - .../<doc>/page_renders/page_*.png  -> render
        无法换算时返回空串。
        """
        if not abs_path or not output_dir:
            return ""
        try:
            rel = os.path.relpath(str(abs_path), output_dir).replace(os.sep, "/")
        except Exception:
            return ""
        if rel.startswith(".."):
            return ""
        parts = rel.split("/")
        if len(parts) < 3:
            return ""
        doc_dir, category_dir, filename = parts[0], parts[1], parts[-1]
        category = {"images": "extracted", "page_renders": "render"}.get(category_dir)
        if not category:
            return ""
        # URL-encode 路径组件，防止空格、中文等特殊字符导致 404
        from urllib.parse import quote
        doc_dir_enc = quote(doc_dir, safe="")
        filename_enc = quote(filename, safe="")
        return f"/api/media/{category}/{doc_dir_enc}/{filename_enc}"

    @staticmethod
    def _image_abs_to_url_legacy(abs_path: str, output_dir: str) -> str:
        """兼容旧逻辑的 URL 转换：尝试从绝对路径推断 doc_dir 和 category。"""
        return MinerUProcessor.image_abs_to_url(abs_path, output_dir)

    # ======================== 10. Markdown跨页拼接方法 —— 首选：基于 MinerU 解析返回的块类型 =================================
    # MinerU 的 middle_json 给出了每个语义块的类型（title/text/image/table...）和阅读顺序（index），用它们判断跨页是否断段，决定拼接方法，准确度远高于按字符的启发式判断
    @staticmethod
    def extract_block_types_from_middle_json(middle_json):  # 从单页 middle_json 提取按阅读顺序排列的块类型序列
        """提取单页中按阅读顺序（index）排列的块类型列表。

        结构来源：middle_json -> pdf_info[0] -> para_blocks[] -> {type, index, lines}
        返回如 ["title", "text", "image", "text", "table"]；解析失败返回空列表。
        供 should_force_break_between_pages 使用。
        """
        obj = MinerUProcessor.ensure_json_obj(middle_json)
        if not obj:
            return []
        try:
            pdf_info = obj.get("pdf_info") or []
            page_info = pdf_info[0] if pdf_info else {}
            blocks = page_info.get("para_blocks") or []
        except Exception:
            return []
        # 块按 index（浮点阅读顺序）排序，缺失时排到末尾，避免打乱真实阅读顺序
        def sort_key(b):
            try:
                return float(b.get("index"))
            except Exception:
                return float("inf")
        ordered = sorted(blocks, key=sort_key)
        types = []
        for b in ordered:
            btype = b.get("type")
            if isinstance(btype, str) and btype:  # 只收集有意义的类型
                types.append(btype)
        return types

    @staticmethod
    def should_force_break_between_pages(prev_types, curr_types):  # 判断跨页边界是否必须断段
        """根据上一页末尾块类型与下一页开头块类型，决定跨页是否强制断段。

        - 边界出现"强块级"类型（标题/图片/表格/列表/公式）→ 必须断段（返回 True）。
          这类元素语义上各自独立，绝不应当和下一页正文续接。
        - 两侧都是普通 text → 不强行断段（返回 False），交给 smart_join_md 根据
          句子结尾字符做细粒度续接/断段判断。
        - 缺少数任一页的结构信息（如旧页面、解析失败的兜底页）→ 返回 False，
          退回到原 smart_join_md 的字符逻辑，避免无依据地暴力加空行。
        """
        if not prev_types or not curr_types:  # 任一页无结构信息，交由字符逻辑兜底
            return False
        strong_types = {"title", "image", "table", "list", "formula"}
        prev_last = prev_types[-1]  # 上一页末尾块的类型（types 已按阅读顺序排序）
        curr_first = curr_types[0]  # 下一页开头块的类型
        if prev_last in strong_types or curr_first in strong_types:
            return True
        return False

    def join_page_md(self, prev_text, curr_text, prev_types, curr_types):  # 跨页拼接入口：结构优先、字符兜底
        """对两个相邻页面的 Markdown 进行拼接。

        优先级：
        1) 先利用 middle_json 的块类型（should_force_break_between_pages）
           判断跨页边界是否必须断段（标题/图片/表格等情况）；
        2) 若不强制断段，则退回原 smart_join_md 的字符级智能拼接，
           处理"text 对 text"时的续接、句号断段等情况。

        这样就把容易误伤的字符猜测，限制在"两侧都是普通 text"这一局部场景。
        """
        if not prev_text:
            return curr_text
        if not curr_text:
            return prev_text
        if MinerUProcessor.should_force_break_between_pages(prev_types, curr_types):
            # 跨页边界遇到强块级元素，强制断段，避免标题/表格/图片被正文续接
            return MinerUProcessor.normalize_md_text(prev_text) + "\n\n" + MinerUProcessor.normalize_md_text(curr_text)
        # 否则交回基于字符/句子结尾的智能拼接
        return MinerUProcessor.smart_join_md(prev_text, curr_text)

    # ============ 11. 插图锚点：text ↔ image 一一对应（供检索返回图片）============
    # 目标：解析时不仅产出文字，还保留"某段文字对应的可检索插图路径"，以及各插图被过滤的原因，
    # 产出 anchors.json 供下游检索/入库使用。过滤条件见 __init__ 中可直接调整的阈值。

    @staticmethod
    def get_page_area_from_middle_json(middle_json):  # 从 middle_json 取页面宽高
        """返回 (page_width, page_height)，解析失败返回 (None, None)。"""
        obj = MinerUProcessor.ensure_json_obj(middle_json)
        if not obj:
            return None, None
        try:
            pdf_info = obj.get("pdf_info") or []
            page_info = pdf_info[0] if pdf_info else {}
            size = page_info.get("page_size") or []
            if isinstance(size, (list, tuple)) and len(size) >= 2:
                return float(size[0]), float(size[1])
        except Exception:
            return None, None
        return None, None

    @staticmethod
    def extract_image_blocks_from_middle_json(middle_json):  # 从 middle_json 提取按阅读顺序排列的图片块及其 bbox/路径
        """返回 [{index, bbox, image_path}]，按阅读顺序排列；解析失败返回 []。
        图片块的 image_path 可能嵌套在 blocks 子树中，这里做递归查找。"""
        obj = MinerUProcessor.ensure_json_obj(middle_json)
        if not obj:
            return []
        try:
            pdf_info = obj.get("pdf_info") or []
            page_info = pdf_info[0] if pdf_info else {}
            blocks = page_info.get("para_blocks") or []
        except Exception:
            return []

        def find_image_path(node):  # 递归从块内找任意 image_path
            if isinstance(node, dict):
                p = node.get("image_path")
                if isinstance(p, str) and p:
                    return p
                for v in node.values():
                    hit = find_image_path(v)
                    if hit:
                        return hit
            elif isinstance(node, list):
                for it in node:
                    hit = find_image_path(it)
                    if hit:
                        return hit
            return None

        def get_bbox(node):
            for key in ["bbox", "box", "layout_bbox"]:
                v = node.get(key)
                if isinstance(v, (list, tuple)) and len(v) >= 4:
                    return list(v[:4])
            return None

        def sort_key(b):
            try:
                return float(b.get("index"))
            except Exception:
                return float("inf")

        ordered = sorted(blocks, key=sort_key)
        result = []
        for b in ordered:
            btype = (b.get("type") or "").lower()
            if btype not in {"image", "figure", "figure_group"}:
                continue
            image_path = find_image_path(b)
            if not image_path:
                continue
            bbox = get_bbox(b)
            result.append({
                "index": b.get("index"),
                "bbox": bbox,
                "image_path": image_path,
            })
        return result

    def filter_image_meta(self, image_meta, page_no):  # 按"面积比 + 像素边长 + AI意义"过滤单张插图，返回是否达标及原因
        """入参 image_meta 需含 bbox、page_w、page_h；返回 (是否达标, 原因dict)。"""
        bbox = image_meta.get("bbox")
        page_w = image_meta.get("page_w")
        page_h = image_meta.get("page_h")
        reasons = {}

        if bbox and page_w and page_h:
            x0, y0, x1, y1 = bbox
            w = x1 - x0
            h = y1 - y0
            # 面积比
            page_area = page_w * page_h
            area_ratio = (w * h) / page_area if page_area > 0 else 0.0
            reasons["area_ratio"] = round(area_ratio, 4)
            if area_ratio < self.min_image_area_ratio:
                reasons["area_ratio_pass"] = False
            else:
                reasons["area_ratio_pass"] = True
            # 像素边长（用页面尺寸当绝对像素近似）
            reasons["side_w"] = round(w, 1)
            reasons["side_h"] = round(h, 1)
            side_pass = (w >= self.min_image_side_px) and (h >= self.min_image_side_px)
            reasons["side_pass"] = side_pass
        else:
            reasons["bbox_missing"] = True
            reasons["area_ratio_pass"] = False
            reasons["side_pass"] = False

        # AI 意义（由调用侧 VLM 判断结果传入）
        meaningful = image_meta.get("meaningful")
        if meaningful is None:
            meaningful = True  # 未知时保守保留（不误伤）
        reasons["meaningful"] = bool(meaningful)

        # 三者同时达标才算可检索插图
        pass_all = (
            reasons.get("area_ratio_pass", False)
            and reasons.get("side_pass", False)
            and reasons["meaningful"]
        )
        return pass_all, reasons

    def build_page_anchors(self, middle_json, page_no):  # 构建单页的"文字块 ↔ 可达标插图路径"锚点列表
        """返回单页的锚点清单（含每块文字对应哪些达标插图路径）、过滤记录。
        结构：{"anchors":[{page_no, block_index, type, text, image_paths}],
               "filters":[{page_no, image_path, pass_all, reasons}]}
        规则：
          - 按阅读顺序 (index) 遍历 para_blocks；
          - text/title/list/table 块作为"文本锚点"；
          - image 块作为"插图"，只有 面积比+像素+AI意义 三者全达标才可挂到"当前文本锚点"下；
          - 1:1 对应：一张达标插图挂到它之前的最近的文本块（同块可有多张图，chunk 后可 n:1）。
        AI 意义由调用侧在 process_one_page 里基于 VLM 描述回填到 self._meaning_by_path[path]。
        """
        anchors = []
        filters = []
        page_w, page_h = self.get_page_area_from_middle_json(middle_json)

        obj = MinerUProcessor.ensure_json_obj(middle_json)
        blocks = []
        if obj:
            try:
                pi = (obj.get("pdf_info") or [])[0] if (obj.get("pdf_info") or []) else {}
                blocks = pi.get("para_blocks") or []
            except Exception:
                blocks = []

        def sort_key(b):
            try:
                return float(b.get("index"))
            except Exception:
                return float("inf")

        def find_image_path(node):  # 递归找块内任意 image_path
            if isinstance(node, dict):
                p = node.get("image_path")
                if isinstance(p, str) and p:
                    return p
                for v in node.values():
                    hit = find_image_path(v)
                    if hit:
                        return hit
            elif isinstance(node, list):
                for it in node:
                    hit = find_image_path(it)
                    if hit:
                        return hit
            return None

        meaningful_map = getattr(self, "_meaning_by_path", {}) or {}

        # 单遍遍历（按阅读顺序），文本块成为锚点，其后首次出现的达标插图挂到当前锚点
        current_anchor = None
        for b in sorted(blocks, key=sort_key):
            btype = (b.get("type") or "").lower()

            if btype in {"text", "title", "list", "table"}:
                text = self._block_text(b)
                if text:  # 空文本块不作为锚点
                    current_anchor = {
                        "page_no": page_no,
                        "type": btype,
                        "block_index": b.get("index"),
                        "text": text,
                        "image_paths": [],
                    }
                    anchors.append(current_anchor)

            elif btype in {"image", "figure", "figure_group"}:
                image_path = find_image_path(b)
                if not image_path:
                    continue
                # middle_json 的 image_path 是原始文件名(如 501fbc....jpg)，统一解析成落盘路径，
                # 使过滤、锚点、_meaning_by_path 三者的图片路径口径一致，
                # 保证达标图的原图能正确写进 anchors.json。
                local_path = self._basename_to_local.get(Path(image_path).name, None)
                if local_path:
                    image_path = str(local_path)
                bbox = b.get("bbox")
                meta = {"bbox": bbox, "page_w": page_w, "page_h": page_h,
                        "meaningful": meaningful_map.get(image_path, True)}
                pass_all, reasons = self.filter_image_meta(meta, page_no)
                filters.append({
                    "page_no": page_no,
                    "image_path": image_path,
                    "pass_all": pass_all,
                    "reasons": reasons,
                })
                # 只有达标的插图才挂到当前文本锚点，供检索返回
                if pass_all:
                    if current_anchor is not None:
                        current_anchor["image_paths"].append(image_path)
                    else:
                        # 图出现在任何文本之前（罕见），仍以独立纯图锚点保留
                        anchors.append({
                            "page_no": page_no,
                            "type": "image",
                            "block_index": b.get("index"),
                            "text": "",
                            "image_paths": [image_path],
                        })

        return {"anchors": anchors, "filters": filters}

    @staticmethod
    def _block_text(block):  # 把块的 lines/spans 拼成文本
        parts = []
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                c = span.get("content")
                if c:
                    parts.append(c)
        return "".join(parts).strip()

    # ====================== 11. Markdown跨页拼接方法 —— 兜底：基于页首/页尾的字符  ===================================
    @staticmethod
    def normalize_md_text(text: str):
        if not text:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text.strip()

    @staticmethod
    def is_block_element(text: str):  # 检测"某一行开头"是否为块级元素标记（标题#、引用>、列表-/*/+/数字、表格|、代码块```、分隔线---、图片）
        if not text:
            return False
        block_patterns = [
            r"^#{1,6}\s+",  # 以1-6个#开头的标题块
            r"^>\s+",  # 以> 开头的引用块
            r"^-\s+",  # 以- 开头的无序列表块
            r"^\*\s+",  # 以 * 开头的无序列表块
            r"^\+\s+",  # 以+ 开头的无序列表块
            r"^\d+\.\s+",  # 以 1. 等数字开头的有序列表块
            r"^\|",  # 以|开头的表格行
            r"^```",  # 以``` 开头的代码块围栏
            r"^---+$",  # 整行都是短横线的分隔线（横向规则线 <hr>）
            r"^!\[[^\]]*\]\(",  # Markdown 图片引用开头
        ]
        stripped = text.strip()
        return any(re.match(p, stripped) for p in block_patterns)

    @staticmethod
    def get_last_meaningful_char(text: str):  # 取markdown最后一个非空字符
        if not text:
            return ""
        stripped = text.rstrip()
        return stripped[-1] if stripped else ""

    @staticmethod
    def smart_join_md(prev_text: str, curr_text: str):  # middle_json失效时的兜底方案：字符级判断跨页是否出现逻辑割裂，启发式选择拼接方法
        prev_text = MinerUProcessor.normalize_md_text(prev_text)
        curr_text = MinerUProcessor.normalize_md_text(curr_text)
        if not prev_text:
            return curr_text
        if not curr_text:
            return prev_text

        curr_first_line = curr_text.strip().split("\n")[0] if curr_text.strip() else ""
        if MinerUProcessor.is_block_element(curr_first_line):
            return prev_text + "\n\n" + curr_text

        last_char = MinerUProcessor.get_last_meaningful_char(prev_text)
        first_char = curr_text[:1] if curr_text else ""

        unfinished_endings = {"，", "、", ",", "(", "（", "[", "【", "“", '"', "/", "-"}
        strong_sentence_endings = {"。", "！", "？", "!", "?"}
        medium_endings = {"；", ";", "：", ":"}

        if last_char in unfinished_endings:
            return prev_text + curr_text

        if last_char in strong_sentence_endings:
            return prev_text + "\n\n" + curr_text

        if last_char in medium_endings:
            return prev_text + "\n" + curr_text

        if re.match(r"[\u4e00-\u9fffA-Za-z0-9）)\]】]", last_char):
            if first_char in {"，", "、", ",", "。", "；", ";", "：", ":", "）", ")", "】", "]"}:
                return prev_text + curr_text

        return prev_text + curr_text


    # ====================== 12. 正式处理单页 ===================================
    def process_one_page(self, page_context: dict):
        page_no = page_context["page_no"]
        pdf_path = page_context["pdf_path"]

        is_image_only = page_context["is_image_only"]
        debug_info = page_context["debug_info"]

        md_content = page_context["md_content"]
        content_list = page_context["content_list"]
        middle_json = page_context["middle_json"]
        images_dict = page_context["images_dict"]

        # 1. 如果是 image_only：整页渲染后走 VLM
        if is_image_only:
            logger.info(f"[MinerU] 第 {page_no} 页判定为图片页（image_only），走 VLM 整页识别")
            render_img_path = None  # 整页渲染图路径；渲染失败时保持 None（不进锚点）
            try:
                render_img_path = self.render_page_pdf_to_image(
                    pdf_path,
                    page_no=page_no,
                )

                logger.debug(f"[MinerU] 第 {page_no} 页已渲染整页图片：{render_img_path}")

                vlm_md = self.call_vlm_describe_image(
                    render_img_path,
                    page_no=page_no,
                    whole_page=True,
                )

                final_md = self.normalize_md_text(vlm_md)

            except Exception as e:
                logger.warning(f"[MinerU] 第 {page_no} 页整页 VLM 识别失败，回退使用 MinerU md_content：{e}")
                final_md = self.normalize_md_text(md_content)

            page_md_path = self.page_result_dir / f"page_{page_no:04d}_final.md"

            # 整页图片也作为可检索插图纳入锚点，且内联进 md 让它随父块入库
            render_anchors = []
            if render_img_path:
                render_img_url = self.image_abs_to_url(str(render_img_path), str(self.output_dir))
                # 内联整页渲染图：放在正文末尾
                final_md = final_md.rstrip() + f"\n\n![]({render_img_url})\n"
                render_anchors = [{
                    "page_no": page_no,
                    "type": "image_only",
                    "block_index": None,
                    "text": final_md,  # 存整页识别全文，供入库做精确文本包含匹配
                    "image_paths": [render_img_path],
                }]

            with open(page_md_path, "w", encoding="utf-8") as f:
                f.write(final_md)

            return {
                "page_no": page_no,
                "is_image_only": True,
                "debug_info": debug_info,
                "md_content": final_md,
                "content_list": content_list,
                "middle_json": middle_json,
                "image_desc_map": {},
                "anchors": render_anchors,
                "filters": [],
            }

        # 2. 如果不是 image_only：使用 MinerU md_content，并对内部图片做 VLM 描述回填
        md_refs = self.extract_image_refs(md_content)

        ref_to_local = self.save_images_from_result(
            images_dict,
            md_refs,
            page_no=page_no,
        )

        logger.info(f"[MinerU] 第 {page_no} 页为正常页，走 MinerU 提取，待描述图片 {len(ref_to_local)} 张")

        descriptions = {}
        self._meaning_by_path = {}  # 每次处理一页时清空，仅记录本页图片的意义判断

        for ref, local_img_path in ref_to_local.items():
            try:
                logger.debug(f"[MinerU] 第 {page_no} 页图片描述中：{local_img_path}")

                desc, meaningful = self.call_vlm_describe_image(
                    local_img_path,
                    page_no=page_no,
                    whole_page=False,
                )

                descriptions[ref] = desc
                self._meaning_by_path[local_img_path] = meaningful

            except Exception as e:
                logger.warning(f"[MinerU] 第 {page_no} 页图片描述失败：ref={ref}, error={e}")
                descriptions[ref] = "该图片描述生成失败。"
                self._meaning_by_path[local_img_path] = True  # 失败时保守保留

        # 基于 middle_json 构建本页"文字↔插图"锚点（含过滤记录）
        # 说明：须在 _meaning_by_path 回填之后调用，滤波判断才会包含 AI 意义。
        page_anchor_data = self.build_page_anchors(middle_json, page_no)

        replaced_md = self.replace_images_with_descriptions(
            md_content,
            descriptions,
            output_dir=str(self.output_dir),
            ref_to_local=ref_to_local,
            exclude_local_paths=[
                f["image_path"]
                for f in page_anchor_data.get("filters", [])
                if not f.get("pass_all")
            ],
        )
        final_md = self.normalize_md_text(replaced_md)

        page_md_path = self.page_result_dir / f"page_{page_no:04d}_final.md"
        with open(page_md_path, "w", encoding="utf-8") as f:
            f.write(final_md)

        return {
            "page_no": page_no,
            "is_image_only": False,
            "debug_info": debug_info,
            "md_content": final_md,
            "content_list": content_list,
            "middle_json": middle_json,
            "image_desc_map": descriptions,
            "anchors": page_anchor_data.get("anchors", []),
            "filters": page_anchor_data.get("filters", []),
        }

    # ======================== 12. 合并分页结果 =================================
    def merge_page_results(self, page_results):
        page_results = sorted(page_results, key=lambda x: x["page_no"])

        final_data = {
            "md_content": "",
            "content_list": [],
            "middle_json_list": [],
            "page_image_desc_map": {},
            "page_debug_info": {},
            "page_is_image_only_map": {},
            "anchors": [],   # 全篇"文字↔插图"锚点汇总（供检索返回图片）
            "filters": [],   # 全篇插图过滤记录（含每张图是否达标及原因）
        }

        merged_md = ""
        prev_page_types = []  # 上一页的块类型序列，用于跨页断段判断（见 join_page_md）

        for item in page_results:
            page_no = item["page_no"]
            page_md = item.get("md_content", "") or ""
            # 从该页 middle_json 提取语义块类型序列，供结构化断段判断
            page_types = self.extract_block_types_from_middle_json(item.get("middle_json"))

            # 用"结构优先、字符兜底"的方式拼接当前页与已合并的内容
            merged_md = self.join_page_md(merged_md, page_md, prev_page_types, page_types)

            content_list = item.get("content_list", [])
            if isinstance(content_list, list):
                final_data["content_list"].extend(content_list)

            middle_json = item.get("middle_json")
            if middle_json is not None:
                final_data["middle_json_list"].append(
                    {
                        "page_no": page_no,
                        "middle_json": middle_json,
                    }
                )

            page_key = f"page_{page_no:04d}"

            final_data["page_image_desc_map"][page_key] = item.get("image_desc_map", {})
            final_data["page_debug_info"][page_key] = item.get("debug_info", {})
            final_data["page_is_image_only_map"][page_key] = item.get("is_image_only", False)

            # 汇总本页的插图锚点与过滤记录
            for a in item.get("anchors", []) or []:
                final_data["anchors"].append(a)
            for f in item.get("filters", []) or []:
                final_data["filters"].append(f)

            prev_page_types = page_types if page_types else prev_page_types  # 保存供下一页使用

        final_data["md_content"] = merged_md

        return final_data

    def write_anchors_json(self, merged):  # 把合并结果的插图锚点/过滤记录落盘为 anchors.json（与 merged.md 平行）
        """把 merged(merge_page_results 的产物) 中的插图锚点与过滤记录写出 anchors.json，
        供入库(child_chunk 填 image_url)与检索使用。返回 anchors 路径。

        注意：入库走 parse_file._parse_pdf_pipeline（不用 process()），那里也会调用本方法，
        保证入库时磁盘上同样有 anchors.json 可供读取。
        """
        anchors_path = self.doc_dir / "anchors.json"
        with open(anchors_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "anchors": merged.get("anchors", []),
                    "filters": merged.get("filters", []),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        logger.info(
            f"[MinerU] 插图锚点已输出到：{anchors_path} "
            f"(锚点 {len(merged.get('anchors', []))} 条，过滤记录 {len(merged.get('filters', []))} 条)"
        )
        return anchors_path

    def write_merged_md(self, text: str) -> Path:
        """把整篇 Markdown 文本落盘为 merged.md（与 anchors.json 平行），
        供前端"查看/编辑解析结果"读取。text 为空时也写（便于编辑后空内容占位）。
        """
        merged_md_path = self.doc_dir / "merged.md"
        with open(merged_md_path, "w", encoding="utf-8") as f:
            f.write(text or "")
        logger.info(f"[MinerU] 整篇 Markdown 已输出到：{merged_md_path}")
        return merged_md_path

    # ======================= 14. 主流程 ==================================
    def process(self):
        logger.info("[MinerU] ========== 第一步：逐页拆 PDF ==========")
        page_files = self.split_pdf_to_pages()
        logger.info(f"[MinerU] 共拆出 {len(page_files)} 页")

        logger.info("[MinerU] ========== 第二步：逐页 MinerU 预判断 + VLM/MinerU 分流 ==========")

        all_page_results = []

        for page_info in page_files:
            page_no = page_info["page_no"]
            logger.debug(f"[MinerU] 正在处理第 {page_no}/{len(page_files)} 页")

            try:
                # 1. 正式处理前先准备 page_context
                # 这里已经包含 is_image_only 字段
                page_context = self.prepare_one_page_before_process(page_info)

                # 2. 根据 page_context["is_image_only"] 决定走 VLM 还是 MinerU
                one_page_result = self.process_one_page(page_context)

                all_page_results.append(one_page_result)

            except Exception as e:
                logger.error(f"[MinerU] 第 {page_no} 页处理失败：{e}")

                all_page_results.append(
                    {
                        "page_no": page_no,
                        "is_image_only": False,
                        "debug_info": {
                            "error": str(e),
                        },
                        "md_content": "",
                        "content_list": [],
                        "middle_json": None,
                        "image_desc_map": {},
                        "anchors": [],
                        "filters": [],
                    }
                )

        logger.info("[MinerU] ========== 第三步：按页顺序拼接 ==========")
        merged = self.merge_page_results(all_page_results)
        merged_md_path = self.doc_dir / "merged.md"
        with open(merged_md_path, "w", encoding="utf-8") as f:
            f.write(merged["md_content"])
        logger.info(f"[MinerU] 拼接完成，整篇 Markdown 已输出到：{merged_md_path}")

        # 输出与 merged.md 平行的插图锚点文件（含锚点清单与过滤记录），供检索返回图片/入库使用
        self.write_anchors_json(merged)

        return merged


# =========================================================
# 入口函数
# =========================================================

def main():
    ensure_logger_handler()  # 独立运行本文件时补充控制台日志兜底；被 parse_file 导入时该 logger 已有 handler，不会重复添加
    PDF_PATH = "/root/iron/Abort_MyAgent_Doc/data/test_data/myagent_file/汇算清缴操作指南.pdf"

    processor = MinerUProcessor(
        pdf_path=PDF_PATH,
        output_dir=str(PARSE_RESULT_DIR),
    )

    processor.process()


if __name__ == "__main__":
    main()