# Chunk Pipeline 切片链路

本文说明上传文档从「原始文件」到「向量入库」的多文件联动机制。链路横跨本目录三份核心模块：`chunk.py`（切分逻辑）、`embedding.py`（向量化与入库）、`routes.py`（调度胶水），外加 `fetch_knowledge/parse_file.py`（源文件解析）与 `config.py`（公共日志/路径）。

---

## 一、模块职责与依赖方向

依赖是**单向**的：`embedding.py` 和 `routes.py` 都 import `chunk.py`，而 `chunk.py` 不依赖任何本目录其它模块（只依赖 `config.py` 的日志/路径）。

```text
        config.py  (公共日志 logger、项目根路径 PROJECT_ROOT、.env 加载)
           ▲
           │ 依赖
   ┌───────┴──────────────┐
chunk.py                 embedding.py
(纯切分逻辑、数据模型)   (向量化 EmbeddingModel + 入库 store_*)
   ▲                        ▲
   │ 依赖(仅数据模型)        │ 依赖(数据模型 + 切分产物)
   └──────── routes.py(调度胶水：import 二者并撮合)
```

- **`chunk.py`**：定义公共数据模型（`ChunkOutput`、`SemanticChunkOutput`）、分块参数（父块 2000 字 / 前后 ±3 句 overlap、最小子块 `_MIN_CHUNK_LENGTH`、相似度阈值 `_SIMILARITY_THRESHOLD`）、Markdown 表格/插图原子块识别、父块→语义子块切分算法。**自身不发起任何网络/DB 调用**。
- **`embedding.py`**：实现 `EmbeddingModel`（OpenAI 风格异步向量化客户端）与入库函数（`store_semantic_chunks` 等）。消费 `chunk.py` 产出的 `SemanticChunkOutput`。
- **`routes.py`**：提供 `ChunkPipeline`（同步 CLI 式入口）与上传后台协程 `_background_process`（异步 Web 入口），把上两层用「依赖注入」撮合成完整链路。

---

## 二、联动核心：依赖注入（`EmbeddingFunction`）

`chunk.py` 刻意不直接 import `embedding.py`，而是定义一个**函数签名契约**，让调用方把向量化函数注入进来：

```python
# chunk.py:290 —— 仅签名，不实现
EmbeddingFunction = Callable[[Sequence[str]], "Coroutine[Any, Any, list[list[float]]]"]
```

语义：「给我一个 async 函数，输入一批文本（`list[str]`），返回一批向量（`list[list[float]]`）。」

- `build_semantic_chunks(..., embedder: EmbeddingFunction)` 接收这个函数并在**切分流程内多次调用**。
- 实际注入的是 `routes.py` 里的 `embedding_model.embed`（见下）。
- **作用**：切分算法（chunk）与向量化实现（embedding）解耦——若更换向量模型，只需改 `embedding.py`，`chunk.py` 无需变动。

---

## 三、完整数据流（一次上传）

`routes.py` 同时 import 两方并承担撮合（同步入口 `ChunkPipeline.run`：

```python
# routes.py:53-77
father_chunks   = chunk_markdown_text(text=parsed_text, ...)         # chunk: 父块
embedding_model = EmbeddingModel()                                   # embedding
semantic_chunks = build_semantic_chunks(father_chunks,
                                        embedder=embedding_model.embed)  # chunk+embedding
resolve_child_image_urls(semantic_chunks, str(path))                 # chunk: 插图URL
summary = store_semantic_chunks(semantic_chunks, ...)                # embedding: 入库
```

异步 Web 入口 `_background_process` 以同一批调用构成后台协程，同步阻塞段一律用 `await asyncio.to_thread(...)` 隔离（避免阻塞事件循环）。

阶段时序（各阶段实际调用了注入的 `embed` 至少两次）：

| 阶段 | 承担方 | 说明 |
|---|---|---|
| ① 解析源文件 | `fetch_knowledge/parse_file.py` `DocumentParser.parse_file` | PDF/Office/图片→Markdown 文本 |
| ② 父块切分 | `chunk.py` `chunk_markdown_text` | 整句满 2000 字切分，±3 句 overlap，表格/插图原子块不切断 |
| ③ 句级原子单元向量化 | `embedding.py` `embed`（经 `_split_parent_semantically` 注入调用） | 为每句取 4096 维向量，用于**判断语义切分边界** |
| ④ 语义子块合并 | `chunk.py` `_merge_semantic_units` + `_merge_short_chunks` | 依据向量相似度把句归并成语义子块 |
| ⑤ 子块向量化 | `embedding.py` `embed` | 为最终子块取高维向量，产 `SemanticChunkOutput.embedding` |
| ⑥ 插图 URL 回填 | `chunk.py` `resolve_child_image_urls` | 读 `anchors.json`，给「图片对应文字」子块填可渲染 URL |
| ⑦ 入库 | `embedding.py` `store_semantic_chunks` | 将 `SemanticChunkOutput` 列表写入 PG（依赖 DB 元信息/去重键） |

> 关键点：同一个注入的 `embed` 同时服务「怎么切」（③）与「查得到」（⑤），绝非仅做入库。

---

## 四、接口清单

### 对外公共接口（供 `routes.py` 或业务引用）

| 模块/函数 | 签名要点 | 作用 |
|---|---|---|
| `chunk.chunk_markdown_text(text, file_path, upload_time, update_time)` | 输入原文，输出 `list[ChunkOutput]` | 父块切分 |
| `chunk.build_semantic_chunks(father_chunks, *, similarity_threshold, embedder)` | 输入父块 + 注入 embedder，输出 `list[SemanticChunkOutput]` | 语义子块切分（联动核心） |
| `chunk.resolve_child_image_urls(semantic_chunks, save_path)` | 就地回填插图 URL | 插图关联 |
| `embedding.EmbeddingModel(model/base_url/access_code/...)` | 读 `.env`：`EMBEDDING_MODEL`/`BASE_URL`/`ACCESSCODE` | 向量化客户端构造 |
| `embedding.EmbeddingModel.embed(texts: Sequence[str])` | 异步，输入文本批量，输出 `list[list[float]]`（4096 维） | 向量化 |
| `embedding.store_semantic_chunks(chunks, *, schema, table, upsert_key)` | 输入子块列表，写库，返回摘要 dict | 入库 |

### 数据模型

- `chunk.ChunkOutput`：父块模型（`chunk_index`/`child_content`/`file_path`/`file_name`/`text_hash` 等）。
- `chunk.SemanticChunkOutput`：语义子块模型，在父块之上增加 `group_id`（父块ID）、`internal_id`（子块序号）、`parent_content`（父块原文）、`related_content_summary`、`related_image_url`、`embedding`（4096 维）、`embedding_dim`。

---

## 五、库表

链路涉及 PG 知识库切片表（表名见 `.env` `PG_MYAGENT_KB_TABLE`，默认 `myagent_kb_chunks_mgmt`）。`embedding.py` 入库时动态读取表元信息（列类型 `_load_column_types`、去重键 `_load_upsert_key`），按 `UNIQUE(file_name, text_hash)` 做内容级去重覆盖。表结构定义于 `table_data/create_pg_tables.py`（`embedding vector(4096)`、`content_tsv` 等）。

表为**删除重建**：`CREATE TABLE IF NOT EXISTS` 不会改旧表列类型，旧表若为 `vector(1024)` 需重建为 `vector(4096)` 后再入库。

---

## 六、注意事项

- **维度一致性**：`embedding.py`、`retrieval_rerank.py` 与库表列均为 4096 维；向量化**不传 `dimensions`**（qwen3-embedding-8b 拒绝 matryoshka 降维），直接取原生 4096。改动任一侧须同步更新。
- **依赖方向**：不得在 `chunk.py` 中 import `embedding.py`，否则破坏解耦（会形成循环依赖与测试困难）。
- **embedder 可重入**：`embed` 在一次切分流程中被调用多次（阶段③⑤），须保证批量输入/线程安全。
- **同步阻塞隔离**：Web 入口中对同步部分（解析、入库）必须使用 `await asyncio.to_thread(...)`，否则阻塞事件循环，拖垮并发（多人同时上传/提问时尤其明显）。
- **`.env` 缺失**：`EmbeddingModel` 构造时若缺 `ACCESSCODE`/`BASE_URL`/`EMBEDDING_MODEL` 会抛 `RuntimeError`。
- **图片 URL**：插图可渲染 URL 依赖 `anchors.json` 存在且图片位于 MinerU 输出目录下，否则返回空串。