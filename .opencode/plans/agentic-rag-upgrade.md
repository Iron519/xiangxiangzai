# Agentic RAG 升级实施计划

## 概述

将当前的"固定流水线 RAG"升级为"LLM 自主决策的 Agentic RAG"。

核心变化：
1. **文件打标**：注入完成时生成文件描述+标签，存入 `kb_chunks.metadata` JSONB。用户可在上传弹窗"更多选项"中选填文件描述，填了则跳过 LLM 打标，没填才触发 LLM 自动打标
2. **意图路由**：`node_route_intent` 基于文件描述判断是否需要检索（Jev Noul + LLM 兜底）
3. **检索策略规划**：`node_plan_retrieval` 替代 `query_rewrite`，除指代消解外还规划检索数量和策略

---

## 实施步骤

### 步骤 1：新建 `calculate_logic/rag/file_tagging.py`

文件描述生成与加载模块。

**功能函数：**

- `generate_file_description(uid, file_name, original_name)` — 异步函数
  - 读取该文件前3个 parent_content（前2000字）
  - 调用 `get_fast_llm_client()` 生成 description + tags
  - 输出 JSON：`{"description": "...", "tags": ["标签1", ...]}`
  - UPDATE `kb_chunks.metadata` 为 `{"description": "...", "tags": [...], "tag_source": "llm_auto", "tagged_at": "ISO时间"}`
  - 超时20秒，失败返回 None

- `tag_file_async(uid, file_name, original_name)` — 异步包装器，fire-and-forget

- `load_file_descriptions(uid, admin_uid)` — 同步函数
  - `SELECT DISTINCT file_name, metadata FROM kb_chunks WHERE uid IN (...) AND metadata->'description' IS NOT NULL`
  - 返回 `[{"file_name": "...", "description": "...", "tags": [...]}, ...]`

**依赖：** 复用现有的 `get_fast_llm_client` 模式（ChatOpenAI + httpx），独立 logger

---

### 步骤 2：修改 `calculate_logic/chunk_pipeline/routes.py`

#### 2a. 上传接口新增 `file_description` 参数

在 `upload_file` 函数签名（~line 509）新增参数：

```python
@router.post("/chunk/upload")
async def upload_file(file: UploadFile = File(...), display_name: str = Form(None),
                      expire_time: str = Form(None), if_show: bool = Form(False),
                      rewrite_mode: str = Form("none"),
                      overwrite: bool = Form(False),
                      file_description: str = Form(None),  # 新增：用户选填的文件描述
                      current_user: TokenData = Depends(get_current_user)):
```

将 `file_description` 传给 `_background_process` 和 `_background_rechunk`。

#### 2b. 注入完成后根据是否有用户描述决定打标方式

**改动点 1：首次注入完成（~line 398）**

```python
# 现有代码：
invalidate_kb_bm25()

# 新增：文件描述写入
if file_description and file_description.strip():
    # 用户手填了描述，直接写入 metadata，跳过 LLM
    metadata = json.dumps({
        "description": file_description.strip(),
        "tags": [],
        "tag_source": "manual",
        "tagged_at": datetime.now(timezone.utc).isoformat()
    })
    with _kb_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {KB_TABLE} SET metadata = %s::jsonb WHERE uid = %s AND file_name = %s",
                (metadata, uid, save_name)
            )
        conn.commit()
    logger.info(f"[FileTagging] 用户手填描述已写入: uid={uid} file={save_name}")
else:
    # 用户没填，异步 LLM 打标
    from calculate_logic.rag.file_tagging import tag_file_async
    asyncio.create_task(tag_file_async(uid, save_name, original_name))
```

**改动点 2：重新切片注入完成（~line 499，`_background_rechunk` 函数内）**

重新切片时保留原有的 file_description（从 kb_chunks.metadata 读取已有描述），如果已有 `tag_source=manual` 的描述则保留不重新打标；如果是 `llm_auto` 则重新打标。

---

### 步骤 3：扩展 ChatState（`generate_answer.py:121-136`）

```python
class ChatState(TypedDict):
    # ... 现有字段全部保留 ...
    route: Optional[str]               # 新增：意图路由结果 (rag/tool/chitchat/hybrid)
    file_descriptions: Optional[list]  # 新增：该用户的文件描述列表
    planned_top_k: Optional[int]       # 新增：LLM 规划的检索切片数量
    search_strategy: Optional[str]     # 新增：检索策略 (direct/broad/precise)
    target_files: Optional[list]       # 新增：预估命中文件（未来用于标签过滤）
```

同时修改 `_build_initial_state`（~line 1137）增加新字段的默认值。

---

### 步骤 4：新增 `node_route_intent` 节点（`generate_answer.py`）

**位置：** 在 `if_follow` 函数之后、`query_rewrite` 函数之前定义

**逻辑：**

```python
async def node_route_intent(state: ChatState) -> ChatState:
    user_query = state["messages"][-1].content
    uid = (state.get("user") or {}).get("uid", "")
    
    # 加载文件描述
    from calculate_logic.rag.file_tagging import load_file_descriptions
    admin_uid = os.getenv("ADMIN_UID", "")
    file_descriptions = load_file_descriptions(uid, admin_uid)
    
    if not file_descriptions:
        # 无文件描述 → 默认走 rag（兼容老数据）
        return {"route": "rag", "file_descriptions": []}
    
    # 构建文件列表文本
    file_list_text = "\n".join(
        f"- {f['file_name']}: {f['description']}"
        for f in file_descriptions
    )
    
    # 优先用 Jev Noul 判断
    try:
        client = get_jev_client()
        result = await asyncio.wait_for(
            asyncio.to_thread(
                client.system_one,
                state={
                    "user_query": user_query,
                    "file_descriptions": file_list_text,
                },
                questions={
                    "needs_rag": Noul(
                        instructions="`user_query` 的问题能否从 `file_descriptions` 描述的文件中找到答案？",
                    ),
                    "needs_tool": Noul(
                        instructions="`user_query` 是否需要查询用户的个人动态数据（假期额度、请假记录、工时、个人信息）？",
                    ),
                },
            ),
            timeout=10,
        )
        rag_noul = result.nouls["needs_rag"].noul
        tool_noul = result.nouls["needs_tool"].noul
        
        if rag_noul > 0.5 and tool_noul > 0.5:
            route = "hybrid"
        elif rag_noul > 0.5:
            route = "rag"
        elif tool_noul > 0.5:
            route = "tool"
        else:
            route = "chitchat"
        
        logger.info(f"[RouteIntent] Jev rag={rag_noul:.4f} tool={tool_noul:.4f} -> {route}")
        return {"route": route, "file_descriptions": file_descriptions}
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"[RouteIntent] Jev 不可用，降级为 LLM: {e}")
    
    # LLM 兜底：fast LLM few-shot 分类
    prompt = f"""判断用户问题的意图路由。

【知识库文件】
{file_list_text}

【用户问题】
{user_query}

【判断标准】
- rag: 问题属于上述知识库文件涵盖的主题
- tool: 需要查询个人动态数据（假期、工时、个人信息）
- chitchat: 闲聊/寒暄/感谢/情绪安抚
- hybrid: 既需要知识库又需要工具

只输出一个词：rag / tool / chitchat / hybrid"""

    try:
        llm = get_fast_llm_client()
        response = await asyncio.wait_for(
            llm.ainvoke(
                [HumanMessage(content=prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 10},
            ),
            timeout=10,
        )
        route = response.content.strip().lower()
        if route not in ("rag", "tool", "chitchat", "hybrid"):
            route = "rag"
        logger.info(f"[RouteIntent] LLM 兜底 -> {route}")
        return {"route": route, "file_descriptions": file_descriptions}
    except Exception as e:
        logger.warning(f"[RouteIntent] LLM 兜底也失败，默认走 rag: {e}")
        return {"route": "rag", "file_descriptions": file_descriptions}
```

**条件边路由函数：**

```python
def route_intent(state: ChatState) -> str:
    intent = state.get("route", "rag")
    if intent == "chitchat":
        return "node_generate"
    if intent == "tool":
        return "node_generate"
    return "node_faq_match"  # rag 和 hybrid 都先走 FAQ 匹配
```

---

### 步骤 5：新增 `node_plan_retrieval` 节点（替代 `query_rewrite`）

**位置：** 在 `node_route_intent` 之后定义，保留原有 `query_rewrite` 函数不删除（兼容）

**逻辑：**

```python
async def node_plan_retrieval(state: ChatState) -> ChatState:
    last_message = state["messages"][-1].content
    is_followup = state.get("is_followup", False)
    file_descriptions = state.get("file_descriptions", [])
    
    history_messages = _clean_history_messages(state.get("messages", []))[-(rewrite_n * 2 + 1):-1]
    history_text = "\n".join(history_messages)
    
    file_list_text = "\n".join(
        f"- {f['file_name']}: {f['description']} [标签: {', '.join(f.get('tags', []))}]"
        for f in file_descriptions
    ) if file_descriptions else "（无文件描述）"
    
    prompt = f"""你是一个检索策略规划器。请分析用户问题并制定检索策略。

【知识库文件】
{file_list_text}

【历史对话】
{history_text if is_followup else "（无）"}

【用户当前问题】
{last_message}

请完成以下任务：
1. 如果是追问（有代词/省略），先做指代消解，把问题补全为独立完整的问题
2. 评估问题难度：simple(单一事实) / moderate(需上下文关联) / complex(多条件/多步骤)
3. 决定检索切片数量：
   - simple: 3-5
   - moderate: 5-8
   - complex: 8-12
4. 选择检索策略：
   - direct: 问题明确，直接检索
   - broad: 问题模糊或涉及多个主题，扩大范围
   - precise: 问题涉及具体数值/条款，精确检索
5. 预估可能命中的文件（基于文件描述，可为空）

输出JSON，格式：
{{"rewritten_query": "...", "difficulty": "simple", "reasoning": "...", "planned_top_k": 5, "search_strategy": "direct", "target_files": ["文件名"]}}

只输出JSON，不要其他内容。"""

    llm = get_fast_llm_client()
    try:
        response = await asyncio.wait_for(
            llm.ainvoke(
                [SystemMessage(content="你是一个检索策略规划器，只输出JSON。"),
                 HumanMessage(content=prompt)],
                extra_body={"enable_thinking": False, "max_tokens": 300},
            ),
            timeout=15,
        )
        raw = response.content.strip()
        # 清理 markdown 包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        plan = json.loads(raw)
        
        rewritten_query = plan.get("rewritten_query", "").strip() or last_message
        planned_top_k = int(plan.get("planned_top_k", 5))
        planned_top_k = max(2, min(15, planned_top_k))  # 钳制在 2-15
        search_strategy = plan.get("search_strategy", "direct")
        if search_strategy not in ("direct", "broad", "precise"):
            search_strategy = "direct"
        target_files = plan.get("target_files", [])
        
        logger.info(f"[PlanRetrieval] query='{rewritten_query[:50]}' difficulty={plan.get('difficulty')} top_k={planned_top_k} strategy={search_strategy}")
        
        return {
            "query": rewritten_query,
            "planned_top_k": planned_top_k,
            "search_strategy": search_strategy,
            "target_files": target_files,
        }
    except Exception as e:
        logger.error(f"[PlanRetrieval] 规划失败，回退基础改写: {e}")
        # 回退：只做指代消解（调用原有 query_rewrite 逻辑的简化版）
        if is_followup:
            # 简单回退：用历史对话让 LLM 做指代消解
            return {"query": last_message, "planned_top_k": 5, "search_strategy": "direct", "target_files": []}
        return {"query": last_message, "planned_top_k": 5, "search_strategy": "direct", "target_files": []}
```

---

### 步骤 6：修改 `node_retrieve`（`generate_answer.py:759-774`）

读取 state 中的 `planned_top_k` 和 `search_strategy`，传给 `do_retrieval`。

```python
async def node_retrieve(state: ChatState) -> ChatState:
    query = state.get("query", "")
    uid = (state.get("user") or {}).get("uid", "") if isinstance(state.get("user"), dict) else ""
    planned_top_k = state.get("planned_top_k") or 5
    search_strategy = state.get("search_strategy", "direct")
    
    logger.info(f"[Retrieve] 开始检索：'{query[:50]}' uid={uid} top_k={planned_top_k} strategy={search_strategy}")

    try:
        context, sources = await do_retrieval(query, uid=uid, top_k=planned_top_k, strategy=search_strategy)
    except Exception as e:
        logger.error(f"[Retrieve] 检索失败，返回空结果: {e}")
        context, sources = "", []
    logger.info(f"[Retrieve] 检索完成，sources数量={len(sources)}")

    return {"context": context, "sources": sources}
```

同时修改 `do_retrieval` 函数（~line 741）：

```python
async def do_retrieval(query: str, uid: str = "", top_k: int = 5, strategy: str = "direct") -> dict:
    # 根据 strategy 调整检索参数
    if strategy == "broad":
        result = await retriever.search(query, uid=uid, rerank_top_k=top_k, 
                                         candidate_top_k=max(15, top_k * 2),
                                         coarse_top_k=max(12, top_k * 2))
    elif strategy == "precise":
        result = await retriever.search(query, uid=uid, rerank_top_k=top_k,
                                         candidate_top_k=max(8, top_k),
                                         coarse_top_k=max(8, top_k))
    else:  # direct
        result = await retriever.search(query, uid=uid, rerank_top_k=top_k)
    ...
```

---

### 步骤 7：修改 `PgHybridRetriever.search`（`retrieval_rerank.py:868`）

`search()` 方法已经支持 `candidate_top_k`、`coarse_top_k`、`rerank_top_k` 等参数，**无需改动函数签名**。

**TOP_K 参数体系说明：**

| 参数 | 当前值 | 谁决定 | 作用 |
|---|---|---|---|
| `SEARCH_CANDIDATE_TOP_K` | 10 | 写死常量 | 向量/BM25 各自初选候选数 |
| `SEARCH_COARSE_TOP_K` | 10 | 写死常量 | RRF 融合后送入 reranker 的候选数 |
| `SEARCH_RERANK_TOP_K` | 3 | 写死常量（升级后由 LLM 动态传入） | rerank 后最终返回条数（dynamic 模式下为上限） |
| `SEARCH_FINAL_MAX_TOP_K` | 10 | 写死常量 | 动态 top-K 断崖检测的最大上限（安全阀） |

**升级后的数据流：**
1. `node_plan_retrieval` 的 LLM 输出 `planned_top_k`（钳制在 2-15）
2. `node_retrieve` 将 `planned_top_k` 传给 `do_retrieval` 的 `top_k` 参数
3. `do_retrieval` 根据 `search_strategy` 调整 `candidate_top_k` 和 `coarse_top_k`，将 `top_k` 传为 `rerank_top_k`
4. `search()` 内部的断崖检测在 `rerank_top_k` 上限内做动态截断

**`SEARCH_FINAL_MAX_TOP_K` 10→15 的用意：**

`SEARCH_FINAL_MAX_TOP_K` 是断崖检测的安全阀，防止极端情况返回过多结果。`_rerank_select` 中的动态选择逻辑会在这个上限内做断崖截断。

当 LLM 规划 `planned_top_k=12`（complex 问题），传为 `rerank_top_k=12`，但如果 `SEARCH_FINAL_MAX_TOP_K=10`，断崖检测会被截断到 10，LLM 的规划就被忽略了。因此需要将 `SEARCH_FINAL_MAX_TOP_K` 调整为 15，确保它 >= LLM 可能规划的最大值（钳制上限 15）。

**改动：** `retrieval_rerank.py:116`

```python
SEARCH_FINAL_MAX_TOP_K = 15  # 从 10 调整为 15，匹配 LLM 规划的 top_k 上限
```

---

### 步骤 8：修改 `node_generate`（`generate_answer.py:821-1032`）

适配"无检索上下文"模式（chitchat/tool 路径）。

**改动点：**

1. **读取 route 判断模式：**
```python
route = state.get("route", "rag")
is_chitchat = (route == "chitchat")
is_tool_only = (route == "tool")
```

2. **chitchat 模式：**
   - `retrieved_context` 为空（已经是空字符串）
   - 不 bind_tools
   - system prompt 中 `<retrieved_context>` 为空，LLM 自然走闲聊
   - 但需要修改 prompt 规则：chitchat 时不强制"必须依据上下文回答"

3. **tool 模式：**
   - `retrieved_context` 为空
   - bind_tools
   - system prompt 中 `<retrieved_context>` 为空，LLM 依赖工具结果回答

4. **system prompt 适配：**
```python
if not retrieved_context:
    # 无检索上下文（chitchat/tool 模式）
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace(
        "你必须且只能依据 <retrieved_context> 标签内的资料回答，绝对禁止编造任何不在资料里的内容。资料里查不到的：坦诚相告，别硬编乱造。",
        "如果没有提供 <retrieved_context> 资料，请根据你的能力直接回答用户问题。"
    )
```

---

### 步骤 9：前端上传弹窗新增"文件描述"选填项（`app/index.html`）

#### 9a. 在"更多选项"折叠区新增文本输入框（~line 1740，rewriteMode 选项之后）

在 `renameMoreSection` 的 `</div>` 之前插入：

```html
<label class="modal-field-label" style="margin-top:12px">文件描述 <span style="color:var(--c-text-3);font-weight:400">（选填）</span></label>
<input type="text" class="modal-input" id="fileDescInput"
       placeholder="选填，帮助 AI 更快定位文件内容"
       style="margin-top:4px" />
```

#### 9b. 在 `doUpload()` 函数中追加发送逻辑（~line 2132，rewriteMode append 之后）

```javascript
const fileDesc = document.getElementById('fileDescInput').value.trim();
if (fileDesc) {
    formData.append('file_description', fileDesc);
}
```

#### 9c. 弹窗打开/关闭时清空 `fileDescInput`

在 `handleFile()` 函数（~line 2064）打开弹窗时，和 `renameCancel` 点击时（~line 2090），将 `fileDescInput.value` 置空（和 `renameInput` 一起清空）。

---

### 步骤 10：Graph 拓扑重构（`generate_answer.py:1037-1109`）

```python
builder = StateGraph(ChatState)

# 节点
builder.add_node("classify_turn_type", if_follow)
builder.add_node("node_route_intent", node_route_intent)       # 新增
builder.add_node("node_faq_match", node_faq_match)
builder.add_node("node_plan_retrieval", node_plan_retrieval)   # 新增（替代 query_rewrite）
builder.add_node("node_retrieve", node_retrieve)
builder.add_node("tool_node", tool_node)
builder.add_node("node_generate", node_generate)

# 入口
builder.set_entry_point("classify_turn_type")

# 边
builder.add_edge("classify_turn_type", "node_route_intent")    # 改：先走意图路由

# 意图路由条件边
builder.add_conditional_edges(
    "node_route_intent",
    route_intent,
    {
        "node_generate": "node_generate",     # chitchat / tool
        "node_faq_match": "node_faq_match",   # rag / hybrid
    },
)

# FAQ 匹配
builder.add_conditional_edges(
    "node_faq_match",
    route_faq_match,
    {"node_plan_retrieval": "node_plan_retrieval", END: END},  # 改：query_rewrite → node_plan_retrieval
)

# RAG 链路
builder.add_edge("node_plan_retrieval", "node_retrieve")       # 改
builder.add_edge("node_retrieve", "node_generate")

# 工具循环
builder.add_conditional_edges("node_generate", tools_condition, {"tools": "tool_node", END: END})
builder.add_edge("tool_node", "node_generate")

# 编译（不变）
graph = builder.compile(checkpointer=checkpointer)
```

**注意：** 原有的 `query_rewrite` 节点不再加入图中，但函数保留不删除（兼容/备用）。

---

### 步骤 11：前端 NODE_HINTS（`generate_answer.py:149-156`）

```python
_DEFAULT_NODE_HINTS = {
    "classify_turn_type": "正在理解您的问题",
    "node_route_intent": "正在分析问题方向",      # 新增
    "query_rewrite": "正在分析上下文",            # 保留（备用）
    "node_plan_retrieval": "正在规划检索策略",    # 新增
    "node_retrieve": "正在检索知识库",
    "node_faq_match": "正在匹配常见问题",
    "node_generate": "正在生成回答",
    "tool_node": "正在调用工具",
}
```

---

### 步骤 12：stream_response 适配（`generate_answer.py:1280-1346`）

`on_chain_end` 事件处理中，node_name 需要适配新节点：

- `node_route_intent` 的 output 不含 final_answer，不需要特殊处理
- `node_plan_retrieval` 的 output 不含 final_answer，不需要特殊处理
- `node_generate` 的处理逻辑**不变**（它仍然是终端节点）
- `node_faq_match` 的处理逻辑**不变**

**唯一需要确认的：** `on_chain_start` 事件中 `NODE_HINTS` 已覆盖新节点名（步骤10已处理）。

---

## 改动文件清单

| # | 文件 | 类型 | 改动内容 |
|---|---|---|---|
| 1 | `calculate_logic/rag/file_tagging.py` | 新建 | 文件描述生成+加载（LLM 自动打标） |
| 2 | `calculate_logic/chunk_pipeline/routes.py` | 修改 | 上传接口新增 file_description 参数；注入完成后根据用户是否填描述决定打标方式（手填→直接写入 / 未填→LLM 异步打标）（2处） |
| 3 | `app/index.html` | 修改 | 上传弹窗"更多选项"新增"文件描述"选填文本框；doUpload() 追加发送 file_description；弹窗打开/关闭时清空 |
| 4 | `calculate_logic/rag/generate_answer.py` | 修改 | ChatState扩展、新增 node_route_intent 和 node_plan_retrieval 节点、修改 node_retrieve/node_generate、重构 graph 拓扑、更新 NODE_HINTS |
| 5 | `calculate_logic/rag/retrieval_rerank.py` | 修改 | SEARCH_FINAL_MAX_TOP_K 从 10 调整为 15（匹配 LLM 规划的 top_k 上限） |

## 风险与兜底

| 风险 | 兜底方案 |
|---|---|
| 用户没填文件描述 + LLM 打标失败 | metadata 保持 `{}`，node_route_intent 查不到描述时默认走 rag |
| 用户填了文件描述但太短/不靠谱 | 不影响检索质量，只影响意图路由的判断精度（可接受） |
| node_route_intent Jev 超时 | 降级为 fast LLM few-shot |
| node_route_intent LLM 也失败 | 默认走 rag（和现有行为一致） |
| node_plan_retrieval JSON 解析失败 | 回退为基础改写（用原始query + 默认top_k=5 + direct策略） |
| 检索结果为空（无文件描述的老数据） | node_generate 现有逻辑已能处理空 context |
| BM25 索引未含 metadata | 不影响，标签过滤这次不做 |
| 重新切片时丢失已有文件描述 | 重新切片前先读取 metadata，如果是 manual 则保留，如果是 llm_auto 则重新生成 |

## 不变的部分

- `if_follow` 节点逻辑不变
- `node_faq_match` 节点逻辑不变
- `query_rewrite` 函数保留不删除（备用，但不加入图中）
- `node_generate` 的工具循环逻辑不变
- 敏感话题检测、场景检测逻辑不变
- streaming SSE 事件处理逻辑基本不变
- 检索的 hybrid vector+BM25+RRF+rerank 核心流程不变
