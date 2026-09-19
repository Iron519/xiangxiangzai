# fetch_knowledge

本目录负责**把上传的原始文档提取为可供检索的知识内容**，产出两类结果：供 RAG 使用的知识 Markdown 文本（含插图锚点），以及免于 LLM 检索的 FAQ 问答对。供上层 `chunk_pipeline/` 的切片入库链路消费。

核心机制：按文件后缀分发，PDF 及 Office 走 MinerU 版式解析 + VLM 图片描述，产出 `merged.md` 与 `anchors.json`；FAQ 走独立的 PG 增删改查接口。

---

## 一、模块职责与联动

依赖方向（`parse_file.py` import `miner_u_parse.py`；`faq_mgmt.py` 独立）：

```text
chunk_pipeline/  (消费 parse_file 产出的 Markdown 去切块/入库)
   │
parse_file.py  DocumentParser —— 文件解析统一入口（按后缀分发）
   │  import 并调用
   ▼
miner_u_parse.py  MinerUProcessor —— PDF/Office 版式解析、逐页分流、插图锚点
   │
faq_mgmt.py  FaqManagement —— FAQ 增删改查路由（独立，不依赖上面两个）
```

- **`parse_file.py`**：对外暴露 `DocumentParser`，其 `parse_file(file_path)` 是**解析统一入口**，按后缀把 `.txt/.md/.csv`、`.pdf`、`.jpeg/.jpg/.png`、`.docx`、`.pptx`、`.xlsx/.xls` 分发到对应私有方法。PDF/Office 委托 `MinerUProcessor`。
- **`miner_u_parse.py`**：`MinerUProcessor` 负责 PDF 拆页、逐页预判断（纯图页分流整页 VLM）、调用 MinerU 服务提取版式、过滤插图并产出锚点、跨页拼接、落盘 `merged.md` 与 `anchors.json`。
- **`faq_mgmt.py`**：FastAPI 路由模块，`/api/v1/faq` 前缀的 FAQ 增删改查，写 PG 表。

## 二、接口清单

### `parse_file.py` — DocumentParser

| 方法 | 签名 | 作用 |
|---|---|---|
| `DocumentParser.parse_file(file_path: str) -> str` | 公开统一入口 | 按后缀分发解析，返回整篇 Markdown 文本；解析失败返回 `""` |
| （内部）`_parse_text/_parse_pdf_pipeline/_parse_word/_parse_excel/_parse_image` | 私有 | 分别处理各类格式 |
| （内部）`_enhance_text_to_markdown(text)` | 私有 | LLM 把原始文本整理为 Markdown |
| （内部）`_write_merged_md/…` | 私有 | 落盘产物 |

异步 Web 链路中，调用方 `chunk_pipeline/routes.py` 以 `await asyncio.to_thread(DocumentParser().parse_file, save_path)` 调用（因 `parse_file` 为同步、含阻塞 LLM/子进程调用）。

### `miner_u_parse.py` — MinerUProcessor

| 方法 | 签名 | 作用 |
|---|---|---|
| `prepare_one_page_before_process(page_info)` | 逐页预判断 | 识别纯图片页（整页 VLM）vs 正常 PDF（MinerU） |
| `process_one_page(page_context)` | 逐页处理 | 输出单页 `page_json`（md_content / middle_json / anchors / filters） |
| `merge_page_results(page_results)` | 跨页合并 | 结构优先拼接各页 |
| `write_anchors_json(merged)` | 落盘 | 写插图锚点到 `anchors.json` |

单页处理失败时返回 `None`，由 `parse_file._parse_pdf_pipeline` 兜底为空页而非中止整份。

### `faq_mgmt.py` — FAQ 路由（鉴权：按权限码）

| 方法 路径 | 功能 | 访问条件 |
|---|---|---|
| `GET /api/v1/faq` | 查询全部 FAQ（按 id 正序，从 1 连续） | 权限 `faq:read` |
| `POST /api/v1/faq` | 新增单条 FAQ，返回新 id | 权限 `faq:save` |
| `PUT /api/v1/faq/{faq_id}` | 修改单条 FAQ | 权限 `faq:save` |
| `DELETE /api/v1/faq/{faq_id}` | 删除单条，并重排剩余行 id 从 1 连续 | 权限 `faq:delete` |

写操作后均调用 `_invalidate_faq_cache()` 使相关缓存失效。

## 三、库表用途（faq_mgmt.py）

- **FAQ 表**（表名 `.env` `PG_HRSSC_FAQ_TABLE`，默认 `hrssc_faq_mgmt`）：存 FAQ 问答对（`id/question/answer/created_at/updated_at`）。删除时 `_renumber_ids` 用临时映射表重排 id 连续并重置自增序列。表结构定义于 `table_data/create_pg_tables.py`。

`parse_file`/`miner_u_parse` **不写任何库表**，只产出文件系统产物（见「注意事项」），切片入库由 `chunk_pipeline/` 承接。

## 四、可维护项及维护方法

### 1. FAQ 增删改查（讲习班通过接口）
- 权限：`faq:read` / `faq:save` / `faq:delete`。
- 查询全部：`GET /api/v1/faq`。
- 新增：`POST /api/v1/faq`，JSON `{"question": "...", "answer": "..."}`（同为 400 校验非空）。
- 修改：`PUT /api/v1/faq/{id}`，JSON 同上；id 不存在返回 404。
- 删除：`DELETE /api/v1/faq/{id}`，删除后剩余行 id 自动重排连续。
- 验证：调 `GET /api/v1/faq` 确认。
- 生效说明：写操后即时生效并刷新缓存，无需重启。

### 2. MinerU / 解析参数调整（改源码参数）
- 插图规模与「达标插图」过滤阈值集中在 `MinerUProcessor.__init__`（面积比/像素边长/AI 意义），可调影响插图是否入锚点。
- `DocumentParser.__init__` 可调 `timeout`、`max_concurrent_api`（内部信号量上限）。
- 修改后重启服务生效。

## 五、注意事项

- **产物落盘，不写库**：解析产出 `merged.md`（整篇 Markdown，供前端查看解析结果）与 `anchors.json`（插图锚点，供入库填 `related_image_url`），位于 `{WORK_PATH}/output_data/miner_u_output/<文档名>/` 下，每份文档单独子目录防覆盖。
- **与 chunk_pipeline 联动**：`parse_file` 产出的 Markdown 是 `chunk_pipeline/` 切块的输入；`anchors.json` 由 `chunk.resolve_child_image_urls` 读取回填插图 URL。改动 `anchors.json` 的字段/路径约定需同步两侧。
- **同步阻塞调用**：`parse_file` 内 LLM（OpenAI 同步客户端）、PDF 转换（`soffice`/`subprocess.run`）均为同步阻塞；Web 调用方必须用 `asyncio.to_thread` 隔离，否则阻塞事件循环。
- **MinerU 依赖外部服务**：`MINERU_URL`/`MINERU_API_KEY` 指向 MinerU 版式解析服务；`VLM_MODEL`/`TEXT_MODEL` 走 `BASE_URL` 网关。缺失会在构造时缺值，需 `.env` 配齐。
- **纯图片页整页处理**：当单页图片并集面积超阈值时视为整图页，整页 VLM 描述，避免多图切割割裂整体逻辑（如流程图/甘特图）。
- **图片不进正文**：图片一律不写入 `merged.md` 正文（避免污染），只保留描述文字，图片本身由 `anchors.json`/`related_image_url` 旁路提供。
- **现有 README 增补说明**：本文件在旧「处理流程」概述基础上，补充接口清单、库表、维护方法与联动细节。