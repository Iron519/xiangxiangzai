# 象象仔（MyAgent）系统设计文档

## 产品定位

服务小微客户的智能客服产品，面向需要7×24智能客服且回答内容基于内部知识的小微商家。产品并非直接用户触点，而是管理端，其各项功能支持解耦——包括用户触点（chat API）和管理功能。

## 产品受众

需要7×24智能客服，且回答内容基于内部知识的小微商家。

---

## 一、系统架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        前端（index.html）                        │
│  登录/注册 │ 对话(SSE) │ 知识库管理 │ FAQ管理 │ 场景模板 │ 报表  │
└──────────────────────┬──────────────────────────────────────────┘
                       │ HTTP + SSE
┌──────────────────────┴──────────────────────────────────────────┐
│                    FastAPI（app/app.py）                         │
│  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌──────┐ ┌───────┐ ┌────┐ │
│  │Auth路由  │ │Chunk路由  │ │Chat路由│ │FAQ   │ │Scene  │ │报表│ │
│  │/api/v1/  │ │/chunk/    │ │/chat/  │ │路由  │ │路由   │ │路由│ │
│  │auth,users│ │/kb/       │ │/session│ │      │ │       │ │    │ │
│  └────┬─────┘ └─────┬────┘ └───┬────┘ └───┬──┘ └───┬───┘ └─┬──┘ │
│       │             │          │          │        │       │    │
│  ┌────┴─────┐ ┌─────┴────┐ ┌───┴────┐ ┌───┴──┐ ┌───┴───┐ ┌─┴──┐ │
│  │Redis     │ │PG+磁盘   │ │PG+     │ │PG    │ │PG     │ │PG  │ │
│  │session   │ │kb_chunks │ │LangGra │ │faq   │ │scene  │ │conv│ │
│  └──────────┘ │upload_kb │ │ph      │ └──────┘ └───────┘ └────┘ │
│               │parse_res  │ │checkpoi│                          │
│               └──────────┘ │nt(PG)  │                          │
│                            └────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、数据库

### 2.1 总览

数据库为 PostgreSQL 18 + pgvector 扩展，共 7 张业务表。所有表名通过环境变量配置，支持多实例隔离。

| 表名 | 环境变量 | 用途 |
|---|---|---|
| `myagent_user` | `PG_MYAGENT_USER_TABLE` | 用户账号（注册审批制） |
| `myagent_kb_chunks` | `PG_MYAGENT_KB_TABLE` | 知识库切片（含向量、全文检索、元数据） |
| `myagent_sessions` | `PG_MYAGENT_SESSIONS_TABLE` | 对话会话（每用户上限10个） |
| `myagent_conversations_history` | `PG_MYAGENT_CONVERSATIONS_TABLE` | 对话历史（含标签、情绪、token统计） |
| `myagent_upload_tasks` | `PG_MYAGENT_UPLOAD_TASKS_TABLE` | 上传任务跟踪（完成后30秒自动清空） |
| `myagent_faq` | `PG_MYAGENT_FAQ_TABLE` | 常见问题库 |
| `myagent_scene_templates` | `PG_MYAGENT_SCENE_TABLE` | 场景模板 |

### 2.2 用户表 `myagent_user`

```sql
CREATE TABLE myagent_user (
    id              bigserial PRIMARY KEY,
    uid             varchar(5)  NOT NULL UNIQUE DEFAULT lpad(nextval('user_uid_seq')::text, 5, '0'),
    username        varchar(30) NOT NULL,
    password_hash   varchar(200) NOT NULL,
    apply_reason    text         NOT NULL DEFAULT '',
    approved        boolean      NOT NULL DEFAULT false,
    remark          text         NOT NULL DEFAULT '',
    deleted         boolean      NOT NULL DEFAULT false,
    create_time     timestamp    NOT NULL DEFAULT now(),
    update_time     timestamp    NOT NULL DEFAULT now()
);
```

- uid 为5位零填充字符串，由序列 `user_uid_seq` 自增生成（00001, 00002...）
- 管理员 uid=00000，建表时硬编码插入，不参与序列自增
- 注册时 `approved=false`，需管理员批准后才能登录
- 软删（`deleted=true`），不物理删除

### 2.3 知识库切片表 `myagent_kb_chunks`

```sql
CREATE TABLE myagent_kb_chunks (
    chunk_index           BIGINT PRIMARY KEY,
    uid                   VARCHAR(5) NOT NULL DEFAULT '',
    original_name         TEXT NOT NULL DEFAULT '',
    child_content         TEXT NOT NULL,
    file_path             TEXT NOT NULL,
    file_name             TEXT NOT NULL,
    text_hash             TEXT NOT NULL,
    file_size             BIGINT DEFAULT 0,
    metadata              JSONB DEFAULT '{}',          -- 文件描述+标签+打标来源
    group_id              BIGINT DEFAULT 0,             -- 父块ID
    internal_id           TEXT DEFAULT '',              -- 子块在父块中的序号
    parent_content        TEXT DEFAULT '',              -- 完整父块文本（rerank上下文）
    related_image_url     TEXT DEFAULT '',              -- 关联图片URL
    if_show               BOOLEAN DEFAULT TRUE,         -- 是否展示文件内图片
    embedding             vector(1024),                 -- 向量
    embedding_dim         INT DEFAULT 1024,
    content_tsv           TSVECTOR DEFAULT NULL,        -- jieba分词+全文检索
    upload_time           TIMESTAMPTZ DEFAULT NULL,
    update_time           TIMESTAMPTZ DEFAULT NULL,
    expire_time           TIMESTAMPTZ DEFAULT NULL,     -- 过期时间
    extra_info            TEXT DEFAULT '',
    UNIQUE (uid, file_name, text_hash)
);
```

**索引**：GIN(content_tsv) + B-tree(uid) + B-tree(uid, file_name, text_hash) + B-tree(expire_time)

**metadata JSONB 结构**：
```json
{
  "description": "该文档介绍了BMW员工请假流程",
  "tags": ["HR政策", "请假流程", "员工手册"],
  "tag_source": "llm_auto",
  "tagged_at": "2026-11-15T10:00:00Z",
  "tag_reasoning": "文档内容覆盖请假流程和HR政策，涉及员工手册相关条款"
}
```

- `tag_source`：`"manual"`（用户手填）或 `"llm_auto"`（LLM 自动打标）
- `tag_reasoning`：仅 `"llm_auto"` 时存在，LLM 打标推理过程
- `tags`：≥3 个标签，用于检索 SQL 标签过滤和意图判断

### 2.4 会话表 `myagent_sessions`

```sql
CREATE TABLE myagent_sessions (
    id              BIGSERIAL PRIMARY KEY,
    uid             VARCHAR(5) NOT NULL,
    session_id      TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    last_message_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    turn_count      INT NOT NULL DEFAULT 0,
    deleted         BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (uid, session_id)
);
```

- 每用户上限10个会话，超出时软删最早的
- 首轮对话后异步生成会话标题（LLM 生成 10 字以内标题），生成失败时以用户问题前 6 字兜底

### 2.5 对话历史表 `myagent_conversations_history`

```sql
CREATE TABLE myagent_conversations_history (
    id            BIGSERIAL PRIMARY KEY,
    uid           VARCHAR(5) NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL,
    turn_count    INT NOT NULL DEFAULT 0,
    role          TEXT NOT NULL,              -- 'user' 或 'assistant'
    content       TEXT NOT NULL DEFAULT '',
    tags          TEXT DEFAULT NULL,          -- LLM自动打标（30类业务标签）
    emotion       TEXT DEFAULT NULL,          -- LLM情绪分类（4类）
    sources       JSONB DEFAULT NULL,         -- 检索来源
    mode          TEXT DEFAULT NULL,          -- rag/faq/sensitive/tool
    duration_ms   INT DEFAULT NULL,
    prompt_token  BIGINT DEFAULT 0,
    completion_token BIGINT DEFAULT 0,
    total_token   BIGINT DEFAULT 0,
    user_judgement TEXT DEFAULT NULL,          -- 'like'/'dislike'
    judgement_detail TEXT DEFAULT NULL,        -- 踩的理由
    related_questions JSONB DEFAULT NULL,     -- 相关追问
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 2.6 上传任务表 `myagent_upload_tasks`

临时任务跟踪表，文件注入完成后30秒自动清空。

```sql
CREATE TABLE myagent_upload_tasks (
    task_id         TEXT PRIMARY KEY,
    uid             VARCHAR(5) NOT NULL,
    original_name   TEXT NOT NULL,
    save_name       TEXT NOT NULL,
    save_path       TEXT NOT NULL,
    file_size       BIGINT DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'saving',
    father_chunks   INT DEFAULT 0,
    semantic_chunks INT DEFAULT 0,
    chunk_count     INT DEFAULT 0,
    inserted        INT DEFAULT 0,
    updated         INT DEFAULT 0,
    error           TEXT DEFAULT NULL,
    expire_time     TIMESTAMPTZ DEFAULT NULL,
    if_show         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**状态生命周期**：saving → parsing → parsed → chunking → embedding → ingesting → completed/failed/cancelled

### 2.7 FAQ表 `myagent_faq` 和 场景模板表 `myagent_scene_templates`

FAQ 为简单的问答对（id, question, answer），删除时自动重排 id。

场景模板存储触发条件、回答模板、追问提示，支持 prompt 注入和 literal 字面输出两种模式。

---

## 三、鉴权与用户体系

### 3.1 认证机制

**非 JWT**，使用不透明随机令牌（`secrets.token_urlsafe(32)`）存储在 Redis 中：

```
登录 → bcrypt验证密码 → 检查approved → 生成随机token → Redis SETEX session:{token} (TTL=240h)
请求 → Authorization: Bearer {token} → Redis GET session:{token} → 反序列化TokenData
登出 → Redis DEL session:{token}
```

**TokenData**：`uid` + `username` + `approved` + `is_admin`（属性，`uid == ADMIN_UID`）

### 3.2 SSE 鉴权

浏览器 `EventSource` 不支持自定义 Header，SSE 端点通过 `?token=xxx` 查询参数传递令牌，`get_current_user_sse` 依赖先尝试 Header 再回退到 query param。

### 3.3 注册审批制

1. 用户提交注册（username + password + apply_reason），`approved=false`
2. 管理员在用户管理页面看到待审批列表
3. 管理员批准后 `approved=true`，用户方可登录
4. 两层防滥用：注册审批 + 后台用量监测

### 3.4 Admin 共享知识库

管理员（uid=00000）上传的文件对所有用户可见，检索时 `WHERE uid IN (user_uid, admin_uid)`。非管理员不可编辑/删除/重新切片管理员的共享文件。

---

## 四、知识管理

### 4.1 文件上传与解析

**支持格式**：PDF, DOCX, PPTX, XLSX, XLS, TXT, MD, CSV, JPEG, JPG, PNG

**上传流程**：
1. 前端弹窗：文件名 → 更多选项（期限、图片展示、文件描述）→ 确认
2. 后端保存文件到 `upload_kb/{uid}/{display_name}`
3. 创建任务记录，启动后台协程 `_background_process`
4. 前端通过 SSE 实时监控进度（每1.5秒推送一次）

**解析策略**（按格式分派）：

| 格式 | 解析方式 |
|---|---|
| TXT/MD/CSV | 自动检测编码（UTF-8→GB18030→GBK→...），读取原文，短文本直接保留，长文本增强为Markdown |
| PDF | 按页拆分 → MinerU 云API 布局解析 → 图片为主页用 VLM 整页识别 → 合并 |
| DOCX | python-docx 原生解析（段落+表格按阅读顺序），嵌入图片用 VLM 识别 |
| PPTX | LibreOffice 转 PDF → 走 PDF 管线 |
| XLSX/XLS | pandas 读取所有 sheet，openpyxl 提取单元格内嵌图片 → VLM 识别 |
| JPG/PNG | VLM 直接描述图片内容 |

**解析产物**：`parse_result/{uid}/{file_name_with_ext}/merged.md` + `anchors.json`

### 4.2 切片与嵌入

**两阶段切片**：

1. **父块（粗切）**：按句子累积，目标2000字/块，相邻块重叠3句。表格和图片描述块作为原子单元不被切断，超500字的表格按行分段（保留表头）。

2. **子块（语义细切）**：对每个父块内的句子级单元嵌入，相邻单元余弦相似度≥0.90则合并，<50字的短块并入相邻块。最终子块再嵌入一次。

**父子关系**：`group_id` = 父块chunk_index，`internal_id` = 子块在父块中的序号，`parent_content` = 完整父块文本（随子块冗余存储，供rerank用）。

**嵌入**：OpenAI兼容API，模型由 `EMBEDDING_MODEL` 环境变量配置，向量维度1024。入库时 UPSERT（冲突键 `uid, file_name, text_hash`），写入后刷新 `content_tsv`（jieba分词 + `to_tsvector('simple')`）。

### 4.3 文件打标

**触发时机**：文件注入完成后。

**两种方式**（注入完成后在 `routes.py` 的 `_background_process` 中分支）：
- **用户手填**：上传弹窗"更多选项"中的"文件描述"选填框，填了则调用 `write_manual_description()` 直接写入 `metadata`（`tag_source: "manual"`），跳过LLM打标
- **LLM自动打标**：用户没填时，fire-and-forget `asyncio.create_task(generate_file_description())`，异步调用 fast LLM 读取文件前5个父块内容（前4000字），输出 reasoning + 1-2句描述 + ≥3个标签，写入 `metadata`（`tag_source: "llm_auto"`）

**打标质量保障**：
- 标签至少3个、最多5个，不允许为空列表
- LLM 输出 `reasoning` 字段解释打标理由，存入 `metadata.tag_reasoning`
- 标签不足3个时记录 warning，标签为空时跳过写入等待下次重试
- `max_tokens` 提升到 400（含 reasoning），超时 30s

**保护机制**：`generate_file_description()` 内部先调用 `has_manual_description()` 检查，如果已有手填描述（`tag_source == "manual"`）则跳过LLM打标，不覆盖。

**重新切片**：`_background_rechunk` 删除旧切片后插入新切片，不重新调用 LLM 打标。由于 `store_semantic_chunks` 不写入 metadata 列，重新切片后 metadata 重置为 `{}`（文件描述和标签丢失，需用户重新手填或等待下次上传时自动打标）。

**用途**：
- 文件描述 → 注入 `node_plan_retrieval`，让 LLM 结合文件描述选择相关标签
- 标签 → 存入 metadata，用于检索 SQL 过滤（`WHERE metadata->'tags' ?| array[...]`）

### 4.4 知识库管理功能

| 操作 | API | 说明 |
|---|---|---|
| 文件列表 | `GET /kb/files` | 三来源齐备才展示：PG有切片 + 磁盘有原文件 + parse_result有merged.md |
| 数据卡片 | `GET /kb/stats` | 文档数、切片数、总大小 |
| 查看切片 | `GET /kb/file/{file_name}/parents` | 父子块结构，抽屉式展示 |
| 查看全文 | `GET /kb/file/{file_name}/parsed-text` | 读取 merged.md |
| 编辑全文 | `PUT /kb/file/{file_name}/parsed-text` | 写回 merged.md |
| 编辑后重切片 | `POST /kb/file/{file_name}/rechunk` | 跳过解析，直接用编辑后的 merged.md 切片+嵌入+入库 |
| 删除文件 | `DELETE /kb/file/{file_name}` | 删PG切片 + 删parse_result + 保留upload_kb原文件（审计） |
| 取消上传 | `POST /kb/task/{task_id}/cancel` | 仅解析阶段可取消 |
| 删除任务记录 | `DELETE /kb/task/{task_id}` | 清理 failed/cancelled 记录 |

**文件描述路径解析**：`_kb_file_row` 优先从 `file_path` 路径中提取上传者 uid（`/app/upload_kb/{uid}/xxx`），避免 `upload_tasks` 表清空后拿不到正确 uid 的问题。

**文件命名统一**：upload_kb、parse_result、PG 三处以用户修改后的展示名（`display_name_final`，即 `file_name` / `save_name`）为准：
- `upload_kb/{uid}/{file_name}` — 物理文件名
- `parse_result/{uid}/{file_name_with_ext}/merged.md` — 目录名用 `file_name` 的完整名（`Path(file_name).name.replace(" ", "_")`，保留后缀）
- `kb_chunks` 表：`file_name` = 展示名（主键之一），`original_name` = 原始上传文件名（仅审计，不用于路径查找）

解析器 `parse_file` 和 `miner_u_parse` 均用 `file_path`（即 `save_path`）的完整文件名（`Path(file_path).name.replace(" ", "_")`，保留后缀）做 parse_result 目录名。`_has_parse_result` / `_merged_md_path` / `_read_merged_md` 统一用 `file_name` 的完整名定位。

**前端展示文件名**：前端文件列表展示用 `file_name`（用户修改后的展示名），排序和搜索也基于 `file_name`。`original_name` 不参与前端展示。

---

## 五、知识检索

### 5.1 双塔混合检索

**向量塔**：pgvector 余弦距离（`<=>` 操作符），取 top-10 候选。过滤条件：embedding非空 + 未过期 + uid在用户和管理员范围内。

**关键词塔**：PG 全文检索（`content_tsv` + `to_tsquery('simple')` + `ts_rank`），jieba 分词。取 top-K 候选。无需维护内存索引，天然跨 worker 一致、实时生效。

**RRF融合**：`score = vector_weight/(k+vector_rank) + keyword_weight/(k+keyword_rank)`，权重 0.6/0.4，k=60。融合后取 top-K 送入 reranker。

### 5.2 Rerank

调用阿里云 DashScope rerank API，对粗排候选重新打分。query 加双语前缀 `[Bilingual context: Chinese and English content may coexist]`。候选≤3条时跳过 rerank。

### 5.3 动态 Top-K（断崖检测）

rerank 后不固定取 N 条，而是通过断崖检测自适应决定返回数量：

1. 按混合分数排序（`0.7*rerank_score + 0.3*similarity`）
2. 计算相邻分数间距，找最大间距位置
3. 如果最大间距 ≥ 其余间距均值的1.5倍且位置≥2 → 在该处截断（"断崖"）
4. 无断崖时用相对分数兜底：保留 ≥ 最高分×0.25 的所有结果
5. 最终钳制在 [2, 15] 范围内

### 5.4 兄弟块扩展

对每个最终命中的子块，取同父块（group_id）中的前一块 + 当前块 + 后一块（共3个子块）拼接为 context，节省 LLM 输入长度同时保留上下文连贯性。sources 中保留完整父块供前端展示。

### 5.5 检索策略

`node_plan_retrieval` 的 LLM 根据问题难度输出 `search_strategy` 和 `planned_top_k`（钳制在 2-15）。

`do_retrieval` 根据 `search_strategy` 调整 `candidate_top_k`（向量/关键词各自初选数）和 `coarse_top_k`（RRF 融合后送 reranker 的条数），将 `planned_top_k` 传为 `rerank_top_k`（rerank 后最终返回上限）。`target_tags` 传给检索器做 SQL 标签过滤。

但 `search()` 内部有兜底：`candidate_top_k = max(candidate_top_k, coarse_top_k, rerank_top_k, SEARCH_FINAL_MAX_TOP_K)`，其中 `SEARCH_FINAL_MAX_TOP_K = 15`。因此 `candidate_top_k` 实际至少为 15，LLM 的调整只在 broad 策略且 `planned_top_k >= 8` 时才能超过 15。

**三种策略的公式：**

| 策略 | candidate_top_k（do_retrieval 传入） | coarse_top_k（do_retrieval 传入） | rerank_top_k |
|---|---|---|---|
| direct | 默认 10 | 默认 10 | planned_top_k |
| broad | max(15, top_k×2) | max(12, top_k×2) | planned_top_k |
| precise | max(8, top_k) | max(8, top_k) | planned_top_k |

**经 search() 兜底后的实际值：**

| 策略 | planned_top_k | candidate（向量/关键词各选） | coarse（送rerank） | rerank（最终上限） |
|---|---|---|---|---|
| direct | 3 | 15 | 10 | 3 |
| direct | 5 | 15 | 10 | 5 |
| direct | 8 | 15 | 10 | 8 |
| direct | 12 | 15 | 10 | 12 |
| direct | 15 | 15 | 10 | 15 |
| broad | 3 | 15 | 12 | 3 |
| broad | 5 | 15 | 12 | 5 |
| broad | 8 | 16 | 16 | 8 |
| broad | 12 | 24 | 24 | 12 |
| broad | 15 | 30 | 30 | 15 |
| precise | 3 | 15 | 8 | 3 |
| precise | 5 | 15 | 8 | 5 |
| precise | 8 | 15 | 8 | 8 |
| precise | 12 | 15 | 12 | 12 |
| precise | 15 | 15 | 15 | 15 |

**关键观察：**
- `candidate_top_k`（向量/关键词各自初选数）在 direct 和 precise 策略下始终被兜底到 15，LLM 无法影响；只有 broad 且 `planned_top_k >= 8` 时才超过 15
- `coarse_top_k`（送 reranker 的条数）在 direct 下固定 10，broad 下随 `planned_top_k` 增长，precise 下从 8 起随 `planned_top_k` 增长
- `rerank_top_k`（rerank 后最终返回上限）完全由 LLM 的 `planned_top_k` 决定，但实际返回条数还受断崖检测动态截断影响（见 5.3 节）

**`planned_top_k` 和 `search_strategy` 的生成方式：**

两者均由 `node_plan_retrieval` 调用 Fast LLM 一次性生成（输出 JSON），不是硬编码指定的。LLM 根据问题内容自主判断：

- `difficulty`：问题难度评估
  - `simple`（单一事实查询）→ `planned_top_k` 建议 3-5
  - `moderate`（需上下文关联）→ `planned_top_k` 建议 5-8
  - `complex`（多条件/多步骤）→ `planned_top_k` 建议 8-12
- `search_strategy`：检索策略
  - `direct`：问题明确，直接检索
  - `broad`：问题模糊或涉及多个主题，扩大范围
  - `precise`：问题涉及具体数值/条款，精确检索

`difficulty` 和 `search_strategy` 是两个独立维度——一个简单但模糊的问题可能是 `difficulty=simple` + `search_strategy=broad`。`planned_top_k` 最终钳制在 2-15。LLM 失败时回退为 `planned_top_k=5` + `search_strategy=direct` + `target_tags=[]` + 原始 query 不改写。

**标签匹配机制：**

`node_plan_retrieval` 从 PG 加载用户和管理员知识库的文件描述和标签池（`SELECT DISTINCT file_name, metadata FROM kb_chunks WHERE ...`），LLM 基于指代消解后的 `rewritten_query` 从标签池中选择相关标签，输出 `target_tags`。标签为空表示不需要检索（闲聊/工具类问题），非空则用于检索 SQL 过滤。

**指代消解（查询重写）机制：**

`node_plan_retrieval` 同时承担指代消解职责，将追问中的代词/省略补全为独立完整的问题：

- **是否结合历史对话**：始终注入最近的历史对话，由 LLM 自主判断是否需要做指代消解（无需前置追问判定节点）
- **结合轮数**：`rewrite_n = 4`，即取最近 4 轮历史对话（8 条消息：4 条用户 + 4 条助手），通过 `_clean_history_messages` 过滤掉工具消息和空内容后拼接，无历史对话时传"（无）"
- **输出**：LLM 输出的 `rewritten_query` 字段，如果为空则回退为原始用户问题
- **历史对话轮数常量**（`generate_answer.py`）：
  - `rewrite_n = 4`：查询重写/指代消解时结合的历史对话轮数
  - `generate_n = 3`：生成回答时结合的历史对话轮数

---

## 六、对话主链路

### 6.1 LangGraph 拓扑

```
入口 → 敏感检测(并行) + 场景检测(并行)
     → node_plan_retrieval（指代消解 + 标签匹配 + 检索参数规划，Fast LLM）
           ├─ 知识库无文件 → node_no_kb_hit → END
           ├─ target_tags 为空 → node_generate（闲聊/工具由LLM自主处理）→ [tool_node → node_generate] → END
           └─ target_tags 非空 → node_faq_match →[命中? END]
                                 └→ node_retrieve →[context为空? node_no_kb_hit → END]
                                                   └→ node_generate → END

注：敏感/场景检测在图外并行预判（非图节点），敏感命中则跳过主图返回固定话术，场景命中则注入 scene 字段。
```

### 6.2 各节点职责

| 节点 | 职责 | 判断方式 | 输出 |
|---|---|---|---|
| `node_plan_retrieval` | 指代消解 + 标签匹配 + 难度评估 + 检索参数规划 | Fast LLM输出JSON | `query`, `target_tags`, `planned_top_k`, `search_strategy` |
| `node_faq_match` | BM25快速匹配FAQ | BM25Plus + KB语料IDF + 双阈值(coverage+ratio) | `mode: faq/rag` + 可能直接返回答案 |
| `node_retrieve` | 执行混合检索（标签过滤） | 向量+PG全文检索+RRF+rerank+断崖检测 | `context`, `sources` |
| `node_no_kb_hit` | 知识库未命中兜底 | 知识库无文件 或 检索context为空时触发 | `final_answer`（固定话术，不经LLM） |
| `node_generate` | 生成最终回答 / 调用工具 | Main LLM + bind_tools；`is_chitchat` = 无检索上下文 | `final_answer` 或 `tool_calls` |
| `tool_node` | 执行工具调用 | LangGraph ToolNode | `ToolMessage` |

**知识库未命中处理**：两种情况走 `node_no_kb_hit` 节点直接返回 `FALLBACK_NO_KB_HIT` 话术，不经过 LLM 生成——避免 LLM 凭自身知识"编造"回答：
1. 知识库无文件（`file_descriptions` 为空）→ `node_plan_retrieval` 直接路由到 `node_no_kb_hit`
2. 知识库有文件但检索结果为空（`target_tags` 非空，`node_retrieve` 返回 context 为空）→ `node_retrieve` 后路由到 `node_no_kb_hit`

**ChatState 关键字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `query` | Optional[str] | LLM 指代消解后的查询，覆盖原始问题 |
| `target_tags` | Optional[list] | LLM 从标签池选择的标签，为空表示不需要检索 |
| `planned_top_k` | Optional[int] | LLM 规划的检索切片数量（2-15） |
| `search_strategy` | Optional[str] | 检索策略：direct/broad/precise |
| `context` | str | 检索到的上下文（兄弟块拼接） |
| `mode` | str | 当前对话意图：rag/faq/sensitive/tool |
| `scene` | Optional[str] | 场景判定结果（场景id） |

**`node_generate` 闲聊判断**：`is_chitchat = not retrieved_context.strip()`——当 `target_tags` 为空（闲聊/工具路径）时，`node_plan_retrieval` 直接路由到 `node_generate`，此时无检索上下文，LLM system prompt 中对闲聊和 RAG 模式有不同的回答风格和追问生成规则。当 `target_tags` 非空但检索结果为空时，走 `node_no_kb_hit` 兜底，不会进入 `node_generate`。

### 6.3 预图并行判定

在主图启动前，并行执行两个判定（等待成本≈慢者一趟）：

- **敏感话题检测**（`_judge_sensitive`）：Jev Noul + LLM兜底，命中则跳过主图，返回固定话术
- **场景检测**（`_judge_scene`）：Jev Choice + LLM兜底，命中则注入场景模板到 `node_generate` 的 system prompt

### 6.4 工具循环

`node_generate` 绑定4个HR工具，LLM可自主决定是否调用。工具结果通过 `tool_node → node_generate` 回流，LLM基于工具结果重新生成回答。连续工具尝试上限3次，超过后停用工具，强制自然语言兜底。

### 6.5 状态持久化

- **LangGraph Checkpoint**：PostgresSaver + 专用连接池（min1/max10，延迟 open），对话状态跨请求恢复
- **对话历史**：每轮完成后写入 `myagent_conversations_history`（user消息 + assistant消息各一行）
- **会话管理**：`myagent_sessions` 表，每用户上限10个，超出软删最早的

### 6.6 PG 连接池架构

所有 PG 访问统一使用 `psycopg_pool.ConnectionPool`，不再使用一次性 `psycopg.connect()`：

| 模块 | 连接池参数 | 用途 |
|---|---|---|
| `generate_answer.py` | min1/max5, autocommit | 对话历史、会话管理、session API |
| `generate_answer.py`（checkpointer） | min1/max10, 延迟 open | LangGraph checkpoint 读写 |
| `retrieval_rerank.py` | min1/max5 | 向量+全文检索 |
| `routes.py` | min1/max5 | 文件列表、上传任务、重切片 |
| `file_tagging.py` | min1/max5 | 文件打标、描述加载 |

连接池在模块级懒加载（首次调用时创建单例），`prepare_threshold=0` 避免 prepared statement 缓存问题。

### 6.7 SSE 流式输出

前端通过 `POST /chat/stream` 发起对话，后端返回 `StreamingResponse`（SSE格式）：

| 事件类型 | 字段 | 触发时机 |
|---|---|---|
| `start` | session_id, turn_count, tips | 流开始 |
| `thinking` | hint | 每个节点启动时（从NODE_HINTS取文案） |
| `content` | content | LLM流式token（`on_chat_model_stream` + tag=node_generate） |
| `tool_query` | tool, hint | node_generate产出tool_calls时 |
| `done` | sources, related_questions | 终端节点完成 |

**架构**：producer-consumer 队列模式。`run_main()` 协程跑 `graph.astream_events()` 推事件到队列，主循环消费队列 yield 给客户端。60秒超时兜底。

---

## 七、工具系统

4个工具均调用 BMW 内部 HR API，通过 `@tool` 装饰器注册为 LangGraph 工具：

| 工具 | API | 功能 | 必填参数 |
|---|---|---|---|
| `query_leave_quota` | POST /hrappbestyou/.../leaveQuotas | 查询假期额度（年假/病假/无薪假） | empNo, startDate, endDate, leaveType |
| `query_leave_record` | POST /hrappbestyou/.../leaveAbsence | 查询请假记录 | empNo, startDate, endDate, leaveType |
| `query_working_time` | POST /hrappbestyou/.../wta | 查询工时账户 | empNo, yearmonth |
| `query_personal_info` | GET /hrappbestyou/.../profile | 查询个人信息 | empNo |

所有工具：跳过SSL验证、30秒超时、失败返回固定兜底文案。LLM在 `node_generate` 中自主决定调用，工具结果回流后重新生成回答。

---

## 八、FAQ与场景模板

### 8.1 FAQ

- 管理端 CRUD（`/api/v1/faq`），删除时自动重排 id
- 检索时用 BM25Plus 索引匹配，IDF 来自知识库全文语料（而非FAQ自身），避免FAQ数量少时IDF失真
- 双阈值匹配：coverage（IDF加权覆盖率）≥ 阈值 AND ratio（TOP1/(TOP1+TOP2)）≥ 阈值
- 命中则直接返回预设答案，跳过检索和生成

### 8.2 场景模板

- 管理端 CRUD（`/api/v1/scene-templates`）
- 每个模板：`trigger`（触发条件描述）+ `answer_template`（回答模板）+ `answer_mode`（prompt/literal）+ `followup_prompt`（追问提示）
- 检测方式：Jev Choice 多分类 + LLM兜底
- 命中后：answer_template 注入 `node_generate` 的 system prompt（literal模式则一字不变输出），followup_prompt 在后处理追加到相关追问列表

---

## 九、数据报表

### 9.1 对话标签与情绪

后台定时任务（每6小时），用 LLM 对历史用户消息打标：
- **标签**：30类业务标签（休假、考勤、薪资、福利、购车...）
- **情绪**：4类（positive/neutral/negative/urgent）

### 9.2 报表页面

`GET /stats/overview?start_date=&end_date=` 返回：
- 总览KPI：使用人数、问答条数、Token消耗、平均耗时
- 每日趋势：用户数、消息数、Token、耗时（Chart.js折线图）
- Token按模型分布
- 负反馈统计：踩的次数、占比、理由分布

管理员看全局数据，普通用户只看自己的。

---

## 十、前端架构

### 10.1 技术栈

单页应用（`index.html`，4400+行），无框架，原生JS + CSS。外部依赖仅 `marked.min.js`（Markdown渲染）和 `chart.js`（图表）。

### 10.2 页面结构

| 页面 | 功能 |
|---|---|
| 首页 | 三步引导卡片 |
| 对话 | 消息流 + 输入框 + 相关追问 + 来源引用 + 点赞/踩 |
| 知识库 | KPI卡片 + 上传区 + 文件列表 + 切片详情抽屉 + 全文编辑抽屉 |
| 场景模板 | 模板表格 + 增删改弹窗 |
| FAQ | 问答表格 + 增删改弹窗 |
| 数据报表 | KPI + 趋势图 + Token分布 + 负反馈 |

### 10.3 鉴权

`did-auth.js` 管理：
- Token 存 localStorage，通过 `apiFetch()` 统一注入 `Authorization: Bearer` header
- 401 自动跳登录
- 登录/注册/改密码/改昵称
- SSE 用 `EventSource` + `?token=xxx` 查询参数

---

## 十一、定制的优化策略

### 11.1 关键词统一

知识库文档和用户查询共用一套 jieba 分词，避免关键词错位。关键词塔使用 PG `content_tsv` + `ts_rank` 全文检索（jieba 分词写入 tsvector）。FAQ 的 IDF 复用知识库语料（FAQ量少时自身IDF失真）。

### 11.2 双语上下文前缀

rerank query 加 `[Bilingual context: Chinese and English content may coexist]` 前缀，提示 reranker 中英文混排场景。

### 11.3 断崖检测自适应Top-K

不固定返回N条检索结果，而是根据分数分布自适应截断——分数出现"断崖"（急剧下降）处即为相关/不相关的分界线。避免固定N条时要么太少要么引入噪音。

### 11.4 兄弟块扩展

检索命中的子块只取自己内容时上下文不完整，取同父块的前后各1个子块拼接（共3块），在LLM输入长度和上下文连贯性之间取平衡。

### 11.5 表格/图片原子保护

切片时表格和图片描述块作为原子单元不被句子级切断，超500字的表格按行分段但保留表头。

### 11.6 语义合并

子块不是按固定字数切分，而是嵌入后按余弦相似度≥0.90合并相邻句子，短于50字的块并入邻居——让每个子块在语义上自洽。

### 11.7 敏感话题+场景模板并行预判

在主图启动前并行执行（而非串行），等待成本≈慢者一趟，几乎不增加延迟。

### 11.8 Jev Noul 概率判断

敏感检测、场景检测优先用 TypeSafe Noul（概率输出+校准），超时/异常时降级为 LLM few-shot。Noul 延迟低、成本零，LLM兜底保证可用性。

### 11.9 工具调用安全阀

连续工具尝试上限3次，超过后停用工具、强制自然语言兜底——防止 LLM 陷入"调工具→失败→再调工具"的死循环。

### 11.10 标签过滤驱动的意图路由

`node_plan_retrieval` 作为图入口，从 PG 加载文件描述和标签池，LLM 在一次调用中完成指代消解 → 标签匹配 → 检索参数规划。三种分流路径：
- 知识库无文件 → `node_no_kb_hit`（兜底话术，不经 LLM）
- `target_tags` 为空 → `node_generate`（闲聊/工具，LLM 自主处理）
- `target_tags` 非空 → FAQ 匹配 → 检索（SQL 按 `metadata->'tags' ?| array[...]` 过滤）→ 检索为空走 `node_no_kb_hit`，非空走 `node_generate`

### 11.11 Docker VirtioFS 写入保护

macOS Docker Desktop 的 VirtioFS/fakeowner 文件系统在覆盖写入已存在文件时可能触发 `Resource deadlock avoided`（宿主机 Spotlight 等进程持有文件句柄）。所有文件覆盖写入点统一加了先 `unlink` 再 `open("w")` 的保护。

---

## 十二、环境配置

所有配置通过环境变量管理，`.env.example` 为模板。关键变量分组：

| 分组 | 变量 | 说明 |
|---|---|---|
| 运行目录 | `WORK_PATH` | 容器内项目根目录（`/app`） |
| LLM | `BASE_URL`, `ACCESSCODE` | OpenAI 兼容 API 地址和密钥 |
| 模型 | `TEXT_MODEL`, `VLM_MODEL`, `FAST_MODEL` | 文本/多模态/快速模型名 |
| Embedding | `EMBEDDING_MODEL`, `EMBEDDING_DIM` | 嵌入模型和维度（1024） |
| Rerank | `RERANK_MODEL`, `RERANK_BASE_URL` | 重排序模型 |
| TypeSafe | `TYPESAFE_API_KEY` | Jev Noul/Choice API key |
| PG | `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD` | PostgreSQL 连接 |
| PG 表名 | `PG_MYAGENT_KB_TABLE`, `PG_MYAGENT_SESSIONS_TABLE` 等 | 业务表名 |
| Redis | `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` | Session 存储 |
| 鉴权 | `ADMIN_UID`, `ADMIN_PASSWORD`, `SESSION_TTL_HOURS` | 管理员和会话配置 |
| FAQ 阈值 | `FAQ_COVERAGE_THRESHOLD`, `FAQ_RATIO_THRESHOLD` | FAQ 匹配阈值 |
| 兜底话术 | `FALLBACK_*`, `ABORT_REPLY`, `SENSITIVE_REPLY` | 各类异常话术（LLM错误/系统错误/超时/空响应/未命中KB/工具失败/中止/敏感） |
| 前端提示 | `NODE_HINTS`, `TOOL_QUERY_HINTS`, `WAITING_TIPS` | SSE 等待提示文案（JSON） |
