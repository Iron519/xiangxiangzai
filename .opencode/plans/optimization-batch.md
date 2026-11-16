# 批量优化计划

## 13.1 子块重写与嵌入的顺序问题

**结论**：直接移除子块重写功能（`rewrite_chunks_with_coref`）。

**原因**：语义切分时相邻句子余弦相似度 ≥ 0.90 即合并，含代词的句子与其指代对象高度相似，会被吸收到同一子块中，代词指代对象已在子块内，无需跨块消解。重写在语义切分前提下是多余的。

**改动点**：
- `routes.py`：`_background_process` 和 `_background_rechunk` 中移除 `await rewrite_chunks_with_coref(...)` 调用
- `routes.py`：`upload_file` 函数签名移除 `rewrite_mode` 参数（或保留但不传给后台）
- `routes.py`：`_background_process` 和 `_background_rechunk` 签名移除 `rewrite_mode` 参数
- `routes.py`：`kb_file_rechunk` 中 `_background_rechunk` 调用移除 `rewrite_mode`
- 前端 `index.html`：上传弹窗移除"子块重写"选项 UI
- `rewrite.py`：文件保留但标记为废弃，或直接删除
- ChatState 和相关参数清理

**状态**：✅ 已完成

---

## 13.2 + 13.5 + 13.10 关键词塔从内存 BM25 切换为 PG 全文检索

**结论**：用 PG `tsvector` + `ts_query` + `ts_rank` 替代内存 BM25 索引，一次性解决 13.2（全量重建）、13.5（content_tsv 未利用）、13.10（多 worker 不一致）三个问题。

**原因**：
- 入库时已用 jieba 分词写入 `content_tsv`（TSVECTOR + GIN 索引），但检索时关键词塔用的是内存 BM25，完全没有用到
- 内存 BM25 每次文件变更需全量重建，多 worker 下各进程索引不一致
- PG 全文检索天然实时生效、跨 worker 一致、无需维护内存索引
- 打分精度：`ts_rank` 比 BM25 略简单（无 k1/b 参数），但知识库场景下文档切片长度差异不大，影响可忽略

**改动点**：
- `retrieval_rerank.py`：
  - 删除 `_KbBM25Index` 类、`_kb_bm25` 单例、`preload_kb_bm25()`、`invalidate_kb_bm25()`
  - `_coarse_search` 中关键词塔改为 PG `ts_query` + `ts_rank` 查询，替代 `_kb_bm25.search()`
  - RRF 融合逻辑不变（向量塔 + 关键词塔各自 top-K 后 RRF 融合）
  - 查询分词复用 `tokenize_for_tsquery`（已有），生成 `tsquery` 语法
- `routes.py`：移除 `invalidate_kb_bm25()` 调用（3处）
- `app.py`：移除 `preload_kb_bm25` 启动预热调用
- `generate_answer.py`：FAQ BM25 索引仍用内存 BM25（FAQ 量小，IDF 来自 KB 语料——需改为从 PG content_tsv 算 IDF，或改用 PG 全文检索）

**状态**：✅ 已完成

---

## 13.3 PG 连接管理

**结论**：将 `generate_answer.py`、`routes.py`、`file_tagging.py` 中的 `psycopg.connect()` 一次性连接替换为共享 `ConnectionPool`。

**原因**：当前 `_pg_conn()` / `_kb_conn()` / `_task_conn()` 每次操作开关一次连接（5-10ms/次），连接池取连接约 0.1ms。`retrieval_rerank.py` 和 checkpointer 已用 `ConnectionPool`，统一为科学做法。

**改动点**：
- 新建共享连接池模块（或在各文件中创建模块级 `ConnectionPool` 单例），复用已有的 PG 环境变量配置
- `generate_answer.py`：`_pg_conn()` 替换为连接池，`_save_conversation_to_pg`、`_create_session_row`、`_verify_session_owner`、`_enforce_session_limit`、`_update_session_timestamp`、`_generate_session_title`、session API 等所有 PG 操作改用 `with pool.connection() as conn`
- `routes.py`：`_kb_conn()` / `_task_conn()` 替换为连接池
- `file_tagging.py`：`_pg_conn()` 替换为连接池
- 连接池参数：`min_size=1, max_size=5, kwargs={"autocommit": True, "prepare_threshold": 0}`（与 `retrieval_rerank.py` 一致）

**状态**：✅ 已完成

---

## 13.4 .env.example 过时

**结论**：以当前 `.env` 为基准更新 `.env.example`。

**改动点**：
- 前缀统一为 `PG_MYAGENT_*`
- 补全缺失变量：`PG_MYAGENT_SESSIONS_TABLE`、`ADMIN_UID` 等
- `EMBEDDING_DIM` 改为 1024
- 新增 `ABORT_REPLY` 等本次迭代新增的环境变量
- 敏感信息用占位符替代

**状态**：✅ 已完成

---

## 13.6 标签过滤 + 意图路由合并

**结论**：用标签过滤替代 `target_files`，并将 `node_route_intent` 合并到 `node_plan_retrieval` 中，图节点再减少一个。

**原因**：
- `target_files` 依赖 LLM 记住文件名，标签过滤更自然——LLM 从标签池选相关标签，PG 按 `metadata->'tags'` JSONB 过滤
- `target_tags` 为空 = 不需要检索，天然替代 `node_route_intent` 的 `needs_rag` 判断
- `needs_tool` 判断是多余的——工具调用由 `node_generate` 的 LLM 自主决定（`bind_tools` + `tools_condition`），不需要前置预判

**新流程**：
1. `node_plan_retrieval` 作为图入口（FAQ 匹配之前的节点）
2. 节点开头从 PG 加载所有文件的标签汇总（`SELECT DISTINCT metadata->'tags' FROM kb_chunks WHERE ...`）
3. LLM 同一次调用中：
   - 先做指代消解，输出 `rewritten_query`（结合历史对话，以用户提问为主要依据）
   - 基于 `rewritten_query`（不是原始问题）从标签池中选择相关标签，输出 `target_tags`
   - 输出 `planned_top_k` / `search_strategy`
4. `target_tags` 为空 → 直接走 `node_generate`（闲聊/工具由 LLM 自己处理）
5. `target_tags` 不为空 → 走 FAQ 匹配 → 检索 → 生成
6. 检索 SQL 中用 `target_tags` 过滤：`WHERE metadata->'tags' ?| array['标签1','标签2']`

**改动点**：
- `generate_answer.py`：
  - 删除 `node_route_intent` 函数和 `route_intent` 条件边
  - `node_plan_retrieval` 新增标签加载 + `target_tags` 输出
  - `node_plan_retrieval` 的 prompt 重构：指代消解 → 标签匹配（依赖消解后的 query）→ 检索参数
  - 图拓扑：入口改为 `node_plan_retrieval`，条件边按 `target_tags` 是否为空分流
  - `ChatState`：`route` 字段移除，`target_files` 改为 `target_tags`
  - `do_retrieval` / `node_retrieve`：接收 `target_tags`，传给检索器做 SQL 过滤
- `retrieval_rerank.py`：`search()` 方法新增 `target_tags` 参数，向量塔和关键词塔的 SQL 加 `metadata->'tags' ?| array[...]` 过滤
- `file_tagging.py`：打标质量提升（见下方独立条目）

**状态**：✅ 已完成

---

## 打标质量提升

**结论**：打标必须严格准确，每个文件强制有标签，LLM 打标时需给出显式 reasoning。

**原因**：标签现在是检索过滤和意图判断的基础，打标不准会导致漏检索或误检索。当前打标只读前 2000 字、标签可为空、无 reasoning，质量不可控。

**改动点**：
- `file_tagging.py` 的 prompt：
  - 要求输出 `reasoning` 字段，解释为什么打这些标签
  - 标签至少 3 个，不允许为空
  - 读取内容从前 3 个父块（2000 字）增加到前 5 个父块（约 4000 字），覆盖更多内容
- `generate_file_description`：标签为空时不写入 metadata，记录 warning 等待下次重试

**状态**：✅ 已完成

---

## 13.7 重新切片不保留文件描述

**结论**：重新切片时直接保留原有 metadata，不重新打标。

**原因**：重新切片只改切片粒度，文件原文没变，文件描述和标签不应变化。当前 `_background_rechunk` 传 `file_description=""` 走 LLM 打标分支，会覆盖已有的 LLM 自动描述。

**改动点**：
- `routes.py`：`_background_rechunk` 中移除打标分支（`write_manual_description` / `generate_file_description` 调用），metadata 保持不变

**状态**：✅ 已完成

---

## 13.8 前端无框架的维护成本

**结论**：暂不处理。当前功能稳定后维护频率不高，迁移成本大于收益。

**状态**：跳过

---

## 13.9 会话标题生成时机

**结论**：生成失败时以用户问题的前 6 个字作为标题兜底。

**改动点**：
- `generate_answer.py`：`_generate_session_title` 的 except 块中，将 `return user_message[:10]` 改为 `return user_message[:6]`（当前是截取前 10 字，改为前 6 字）

**状态**：✅ 已完成

---

## 13.11 parse_result 目录名改为保留后缀

**结论**：parse_result 目录名从 `Path(name).stem`（去后缀）改为 `Path(name).name`（保留后缀），避免同 stem 不同后缀文件撞车。

**原因**：`data.xlsx` 和 `data.pdf` 的 stem 都是 `data`，parse_result 目录会撞车。保留后缀后目录名分别为 `data.xlsx` 和 `data.pdf`，天然隔离。

**改动点**：
- `parse_file.py`：`_office_output_dir` 中 `Path(file_path).stem.replace(" ", "_")` → `Path(file_path).name.replace(" ", "_")`
- `parse_file.py`：`_write_image_only_anchor` 中 `Path(file_path).stem.replace(" ", "_")` → `Path(file_path).name.replace(" ", "_")`
- `miner_u_parse.py`：`Path(self.pdf_path).stem` → `Path(self.pdf_path).name`，`_name_stem.replace(" ", "_")` 改为 `_name.replace(" ", "_")`
- `routes.py`：`_has_parse_result` 中 `Path(file_name).stem.replace(" ", "_")` → `Path(file_name).name.replace(" ", "_")`
- `routes.py`：`_merged_md_path` 中 `Path(file_name).stem.replace(" ", "_")` → `Path(file_name).name.replace(" ", "_")`
- 旧数据可删除，无需迁移

**状态**：✅ 已完成

---

（后续优化项追加在此）
