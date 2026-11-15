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
  "tags": ["HR政策", "请假流程"],
  "tag_source": "llm_auto",   // 或 "manual"
  "tagged_at": "2026-11-15T10:00:00Z"
}
```

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
- 首轮对话后自动生成会话标题（LLM一句话总结）

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
1. 前端弹窗：文件名 → 更多选项（期限、图片展示、子块重写、文件描述）→ 确认
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

**解析产物**：`parse_result/{uid}/{file_stem}/merged.md` + `anchors.json`

### 4.2 切片与嵌入

**两阶段切片**：

1. **父块（粗切）**：按句子累积，目标2000字/块，相邻块重叠3句。表格和图片描述块作为原子单元不被切断，超500字的表格按行分段（保留表头）。

2. **子块（语义细切）**：对每个父块内的句子级单元嵌入，相邻单元余弦相似度≥0.90则合并，<50字的短块并入相邻块。最终子块再嵌入一次。

**父子关系**：`group_id` = 父块chunk_index，`internal_id` = 子块在父块中的序号，`parent_content` = 完整父块文本（随子块冗余存储，供rerank用）。

**嵌入**：OpenAI兼容API，模型由 `EMBEDDING_MODEL` 环境变量配置，向量维度1024。入库时 UPSERT（冲突键 `uid, file_name, text_hash`），写入后刷新 `content_tsv`（jieba分词 + `to_tsvector('simple')`）。

### 4.3 子块重写

可选功能（上传时 "子块重写" 开关）。检测子块中的代词（它/它们/其/该/此/这/那/本/上述...），用父块上下文做指代消解，让子块可独立理解。仅改写 `child_content`，`parent_content` 保留原文。注意：重写在嵌入之后执行，嵌入用的是重写前的文本。

### 4.4 文件打标

**触发时机**：文件注入完成后。

**两种方式**（注入完成后在 `routes.py` 的 `_background_process` / `_background_rechunk` 中分支）：
- **用户手填**：上传弹窗"更多选项"中的"文件描述"选填框，填了则调用 `write_manual_description()` 直接写入 `metadata`（`tag_source: "manual"`），跳过LLM打标
- **LLM自动打标**：用户没填时，fire-and-forget `asyncio.create_task(generate_file_description())`，异步调用 fast LLM 读取文件前3个父块内容（前2000字），生成1-2句描述 + 3-5个标签，写入 `metadata`（`tag_source: "llm_auto"`）

**保护机制**：`generate_file_description()` 内部先调用 `has_manual_description()` 检查，如果已有手填描述（`tag_source == "manual"`）则跳过LLM打标，不覆盖。

**用途**：
- 文件描述 → 注入 `node_route_intent`，让 LLM 判断问题是否需要检索
- 标签 → 存入 metadata，未来用于检索过滤

### 4.5 知识库管理功能

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
- `parse_result/{uid}/{file_stem}/merged.md` — 目录名用 `file_name` 的 stem（`Path(file_name).stem.replace(" ", "_")`）
- `kb_chunks` 表：`file_name` = 展示名（主键之一），`original_name` = 原始上传文件名（仅审计，不用于路径查找）

解析器 `parse_file` 和 `miner_u_parse` 均用 `file_path`（即 `save_path`）的 stem 做 parse_result 目录名，不再传入 `display_name=original_name`。`_has_parse_result` / `_merged_md_path` / `_read_merged_md` 统一用 `file_name` 的 stem 定位，不再需要 `original_name` 参数，也不兼容旧目录结构。

**前端展示文件名**：前端文件列表展示用 `file_name`（用户修改后的展示名），排序和搜索也基于 `file_name`。`original_name` 不参与前端展示。

---

## 五、知识检索

### 5.1 双塔混合检索

**向量塔**：pgvector 余弦距离（`<=>` 操作符），取 top-10 候选。过滤条件：embedding非空 + 未过期 + uid在用户和管理员范围内。

**关键词塔**：内存 BM25 索引（进程级单例），jieba 分词，IDF = `log((n+1)/df)`，BM25 参数 k1=1.5, b=0.75。取 top-10 候选。文件上传/删除时通过 `invalidate_kb_bm25()` 失效，下次检索时惰性重建。

**RRF融合**：`score = vector_weight/(k+vector_rank) + keyword_weight/(k+bm25_rank)`，权重 0.6/0.4，k=60。融合后取 top-10 送入 reranker。

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

`do_retrieval` 根据 `search_strategy` 调整 `candidate_top_k`（向量/BM25 各自初选数）和 `coarse_top_k`（RRF 融合后送 reranker 的条数），将 `planned_top_k` 传为 `rerank_top_k`（rerank 后最终返回上限）。

但 `search()` 内部有兜底：`candidate_top_k = max(candidate_top_k, coarse_top_k, rerank_top_k, SEARCH_FINAL_MAX_TOP_K)`，其中 `SEARCH_FINAL_MAX_TOP_K = 15`。因此 `candidate_top_k` 实际至少为 15，LLM 的调整只在 broad 策略且 `planned_top_k >= 8` 时才能超过 15。

**三种策略的公式：**

| 策略 | candidate_top_k（do_retrieval 传入） | coarse_top_k（do_retrieval 传入） | rerank_top_k |
|---|---|---|---|
| direct | 默认 10 | 默认 10 | planned_top_k |
| broad | max(15, top_k×2) | max(12, top_k×2) | planned_top_k |
| precise | max(8, top_k) | max(8, top_k) | planned_top_k |

**经 search() 兜底后的实际值：**

| 策略 | planned_top_k | candidate（向量/BM25各选） | coarse（送rerank） | rerank（最终上限） |
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
- `candidate_top_k`（向量/BM25 各自初选数）在 direct 和 precise 策略下始终被兜底到 15，LLM 无法影响；只有 broad 且 `planned_top_k >= 8` 时才超过 15
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

`difficulty` 和 `search_strategy` 是两个独立维度——一个简单但模糊的问题可能是 `difficulty=simple` + `search_strategy=broad`。`planned_top_k` 最终钳制在 2-15。LLM 失败时回退为 `planned_top_k=5` + `search_strategy=direct` + 原始 query 不改写。

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
     → node_route_intent（基于文件描述判断意图路由，Jev Noul + LLM兜底）
          ├─ chitchat → node_generate（无检索上下文）→ END
          ├─ tool → node_generate（无检索上下文，bind_tools）→ [tool_node → node_generate] → END
          ├─ rag → node_faq_match →[命中? END]
          │                        └→ node_plan_retrieval → node_retrieve → node_generate → END
          └─ hybrid → (同 rag 链路，node_generate 始终根据 tool_attempts 决定是否 bind_tools)

注：敏感/场景检测在图外并行预判（非图节点），敏感命中则跳过主图返回固定话术，场景命中则注入 scene 字段。
```

### 6.2 各节点职责

| 节点 | 职责 | 判断方式 | 输出 |
|---|---|---|---|
| `node_route_intent` | 基于文件描述判断是否需要检索 | Jev Noul(两个问题:needs_rag,needs_tool) + LLM兜底 | `route: rag/tool/chitchat/hybrid` + `file_descriptions` |
| `node_faq_match` | BM25快速匹配FAQ | BM25Plus + KB语料IDF + 双阈值(coverage+ratio) | `mode: faq/rag` + 可能直接返回答案 |
| `node_plan_retrieval` | 指代消解 + 难度评估 + 检索参数规划 | Fast LLM输出JSON | `query`, `planned_top_k`, `search_strategy`, `target_files` |
| `node_retrieve` | 执行混合检索 | 向量+BM25+RRF+rerank+断崖检测 | `context`, `sources` |
| `node_generate` | 生成最终回答 / 调用工具 | Main LLM + bind_tools | `final_answer` 或 `tool_calls` |
| `tool_node` | 执行工具调用 | LangGraph ToolNode | `ToolMessage` |

### 6.3 预图并行判定

在主图启动前，并行执行两个判定（等待成本≈慢者一趟）：

- **敏感话题检测**（`_judge_sensitive`）：Jev Noul + LLM兜底，命中则跳过主图，返回固定话术
- **场景检测**（`_judge_scene`）：Jev Choice + LLM兜底，命中则注入场景模板到 `node_generate` 的 system prompt

### 6.4 工具循环

`node_generate` 绑定4个HR工具，LLM可自主决定是否调用。工具结果通过 `tool_node → node_generate` 回流，LLM基于工具结果重新生成回答。连续工具尝试上限3次，超过后停用工具，强制自然语言兜底。

### 6.5 状态持久化

- **LangGraph Checkpoint**：PostgresSaver + 连接池（min1/max10），对话状态跨请求恢复
- **对话历史**：每轮完成后写入 `myagent_conversations_history`（user消息 + assistant消息各一行）
- **会话管理**：`myagent_sessions` 表，每用户上限10个，超出软删最早的

### 6.6 SSE 流式输出

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

知识库文档和用户查询共用一套 jieba 分词，避免关键词错位。BM25 的 IDF 从知识库全文语料计算，FAQ 的 IDF 也复用知识库语料（FAQ量少时自身IDF失真）。

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

敏感检测、场景检测、意图路由均优先用 TypeSafe Noul（概率输出+校准），超时/异常时降级为 LLM few-shot。Noul 延迟低、成本零，LLM兜底保证可用性。

### 11.9 工具调用安全阀

连续工具尝试上限3次，超过后停用工具、强制自然语言兜底——防止 LLM 陷入"调工具→失败→再调工具"的死循环。

### 11.10 文件描述驱动的意图路由

LLM 在检索前先看到知识库文件描述列表，判断问题是否属于知识库覆盖范围——闲聊和工具类问题跳过检索，节省资源和延迟。

### 11.11 Docker VirtioFS 写入保护

macOS Docker Desktop 的 VirtioFS/fakeowner 文件系统在覆盖写入已存在文件时可能触发 `Resource deadlock avoided`（宿主机 Spotlight 等进程持有文件句柄）。所有文件覆盖写入点统一加了先 `unlink` 再 `open("w")` 的保护。

---

## 十二、死代码清单

Agentic RAG 升级（计划 1）执行时已清理大部分死代码。以下为最终状态：

| # | 文件 | 内容 | 状态 | 说明 |
|---|---|---|---|---|
| 1 | `generate_answer.py` | `query_rewrite` 函数 | ✅ 已删除 | 已被 `node_plan_retrieval` 替代 |
| 2 | `generate_answer.py` | `_detect_language` 函数 | ✅ 保留 | **正在使用**（`_build_initial_state` 中调用，检测用户提问语种） |
| 3 | `generate_answer.py` | `import operator` | ✅ 已删除 | — |
| 4 | `generate_answer.py` | `from langgraph.graph import MessagesState` | ✅ 已删除 | — |
| 5 | `generate_answer.py` | `AnyMessage` import | ✅ 已删除 | — |
| 6 | `generate_answer.py` | `context_config` 字段 | ✅ 已删除 | — |
| 7 | `generate_answer.py` | `context_mode` 字段 | ✅ 已删除 | — |
| 8 | `generate_answer.py` | `image` 字段 | ✅ 已删除 | — |
| 9 | `retrieval_rerank.py` | `batch_search` 方法 | ✅ 已删除 | — |
| 10 | `retrieval_rerank.py` | `_get_conn` + `_return_conn` | ✅ 已删除 | — |
| 11 | `generate_answer.py` | `NODE_HINTS["query_rewrite"]` | ✅ 已删除 | — |
| 12 | `sso_did/auth.py` | `get_current_user_optional` | ✅ 已删除 | 从未被调用 |
| 13 | `table_data/user_profile.py` | 空文件 | ✅ 保留 | 预留扩展 |

---

## 十三、可优化之处

### 13.1 子块重写与嵌入的顺序问题

**现状**：`rewrite_chunks_with_coref` 在 `build_semantic_chunks`（含嵌入）之后执行。即嵌入用的是重写前的文本，但 `text_hash` 更新为重写后的。这意味着向量空间和文本空间不一致——检索时用 query 嵌入匹配的是旧文本的向量，但返回的是新文本。

**建议**：将重写移到嵌入之前，或重写后重新嵌入受影响的子块。

### 13.2 BM25 索引全量重建

**现状**：每次文件上传/删除都 `invalidate_kb_bm25()`，下次检索时全量重建（加载所有 chunk 的 child_content）。文件多时重建耗时可观。

**建议**：改为增量更新——上传时追加文档到索引，删除时移除对应文档。或改用 PG 的 `tsvector` + `ts_query` 做关键词检索（已有 `content_tsv` 索引但未在检索中使用）。

### 13.3 PG 连接管理

**现状**：多处使用 `psycopg.connect()` 一次性连接（_pg_conn, _kb_conn, _task_conn），每次操作开关一次连接。高频操作（如文件描述加载、对话历史写入）的连接开销不可忽略。

**建议**：统一使用 `ConnectionPool`，像 checkpointer 那样管理连接池。

### 13.4 .env.example 过时

**现状**：`.env.example` 仍使用旧的 `PG_HRSSC_*` 前缀命名，与实际 `.env` 的 `PG_MYAGENT_*` 不一致。缺少 `PG_MYAGENT_SESSIONS_TABLE`、`ADMIN_UID` 等新变量。`EMBEDDING_DIM` 示例值 4096 与实际 1024 不符。

**建议**：以当前 `.env` 为基准更新 `.env.example`。

### 13.5 content_tsv 索引未用于检索

**现状**：`kb_chunks` 表有 `content_tsv`（TSVECTOR）和 GIN 索引，入库时用 jieba 分词填充。但检索时关键词塔用的是内存 BM25，完全没有用到 PG 的全文检索能力。

**建议**：可以将关键词塔改为 PG `ts_query` + `ts_rank`，省去内存 BM25 索引的构建和维护成本；或者保留 BM25 但利用 content_tsv 做 PG 侧的预过滤。

### 13.6 target_files 未被消费

**现状**：`node_plan_retrieval` 输出 `target_files`（预估命中文件），存入 state 但没有任何节点读取。注释说"未来用于标签过滤"。

**建议**：要么实现标签过滤（`WHERE metadata->'tags' ?| target_tags`），要么暂时移除该字段避免无用的 LLM token 消耗。

### 13.7 重新切片不保留文件描述

**现状**：`_background_rechunk` 传 `file_description=""`，走 LLM 打标分支。如果文件之前有手填描述，`has_manual_description` 会保护它——但如果之前是 LLM 自动打标的，重新切片会重新打标，可能覆盖之前的描述。

**建议**：重新切片前先读取已有 metadata，如果是 `llm_auto` 则可以选择保留或重新生成，而不是无条件重新生成。

### 13.8 前端无框架的维护成本

**现状**：4400+行原生 JS + HTML 单文件，无组件化、无状态管理、无构建工具。

**建议**：如果项目持续迭代，考虑迁移到轻量框架（如 Alpine.js + 模板拆分，或 Vue 单文件），降低维护成本。当前规模尚可接受，但每次改动都需要全局搜索定位。

### 13.9 会话标题生成时机

**现状**：首轮对话后异步生成标题（`_generate_session_title`），但如果生成失败，标题永远为空。

**建议**：增加重试或在用户首次查看会话列表时懒生成。

### 13.10 多worker下的BM25索引一致性

**现状**：BM25 索引是进程级单例。在 `--workers 4` 的生产模式下，每个 worker 有自己的 BM25 索引。文件上传后 `invalidate_kb_bm25()` 只失效当前 worker 的索引，其他 worker 的索引仍然是旧的。

**建议**：使用 Redis pub/sub 或 PG LISTEN/NOTIFY 跨 worker 通知索引失效；或改用 PG 侧全文检索（天然跨 worker 一致）。

### 13.11 parse_result 目录名用 stem 去后缀，同 stem 不同后缀文件会撞车

**现状**：解析器 `_office_output_dir` 用 `Path(file_path).stem.replace(" ", "_")` 做 `parse_result` 目录名，丢弃了文件后缀。如果同一用户上传了 `data.xlsx` 和 `data.pdf`，两个文件的 parse_result 目录都是 `parse_result/{uid}/data/`，后者会覆盖前者的 `merged.md`。

已统一用 `file_name`（用户修改后的展示名）的 stem 做 parse_result 目录名，upload_kb、parse_result、PG 三处命名一致。`original_name` 仅保留在 PG 中做审计，不参与路径查找。旧数据和兼容旧目录结构的逻辑已清除。

**影响范围**：
- `parse_file.py` `_office_output_dir` — 用 `file_path` 的 stem
- `miner_u_parse.py` — 用 `pdf_path` 的 stem
- `routes.py` `_has_parse_result` / `_merged_md_path` / `_read_merged_md` — 用 `file_name` 的 stem

**建议**：如果未来文件量增大或撞车频发，将目录名改为 `stem + ext`（如 `data.xlsx` 而非 `data`），同步修改所有引用点。
