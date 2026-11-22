# 场景模板与 FAQ 合并设计文档

> 目标：将 `myagent_faq` 表和 `myagent_scene_templates` 表合并为一张表，通过 `answer_type` 字段区分行为类型。消除 FAQ BM25 内存缓存的多 worker 一致性问题，统一管理入口。

---

## 1. 现状

### 1.1 两张表

**FAQ 表**（`myagent_faq`）：
```
id, question, answer, created_at, updated_at
```
- 存预设 Q&A 对
- 命中后直接返回 answer，跳过 LLM
- 用内存 BM25 匹配（有缓存一致性问题）

**场景模板表**（`myagent_scene_templates`）：
```
id, scene_id, name, trigger, answer_template, answer_mode, followup_prompt, followup_mode, created_at, updated_at
```
- 存场景触发规则
- 命中后注入 answer_template 到 LLM prompt，仍走 RAG
- 用 Jev Choice / LLM 判断（每次查库，无缓存问题）

### 1.2 对话流程

```
用户提问
    │
    ├─ sens_task（敏感词检测，~1.9s）  ┐
    ├─ scene_task（场景判断，~1.9s）   ├─ 三者并行
    └─ plan_task（检索规划，~4.1s）    ┘
         │
    scene_task 命中 → 注入 scene_id 到 state
         │
    plan_task 完成 → route_plan_retrieval 判断走向
         │
         ├─ target_tags 非空 → node_faq_match（BM25 匹配）
         │    ├─ 命中 → mode=faq → END（直接返回答案）
         │    └─ 未命中 → node_retrieve
         ├─ target_tags 空 + skip → node_retrieve
         └─ target_tags 空 → node_no_kb_hit
              │
         node_retrieve → node_generate（LLM 生成，消费 scene 注入的模板）
```

### 1.3 核心问题

| 问题 | 说明 |
|------|------|
| FAQ BM25 内存缓存 | 4 个 worker 各持一份，CRUD 后只清当前 worker，其他 worker 不感知 |
| IDF 过时 | KB 变化后无失效机制，需重启刷新 |
| 导入路径错误 | `_invalidate_faq_cache` 里 `from src import generate_answer`，实际路径不对，可能根本没生效 |
| 两套独立机制 | FAQ 和场景模板各自一套表、一套匹配逻辑、一套前端管理，维护成本高 |

---

## 2. 合并设计

### 2.1 统一表结构

复用 `myagent_scene_templates` 表（已有），调整字段：

```sql
-- 新增字段
ALTER TABLE myagent_scene_templates 
  ADD COLUMN answer_type TEXT NOT NULL DEFAULT 'llm';    -- 'faq' = 直接返回, 'llm' = 注入prompt走RAG

-- 删除字段（answer_mode 的 literal/prompt 区分已由 answer_type 替代）
ALTER TABLE myagent_scene_templates DROP COLUMN answer_mode;
ALTER TABLE myagent_scene_templates DROP COLUMN followup_mode;
```

最终字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | BIGSERIAL PK | 自增主键 |
| `scene_id` | TEXT UNIQUE | 场景标识（用于 Jev criteria key） |
| `name` | TEXT | 场景名称 |
| `trigger` | TEXT | 触发描述（用户自填，喂给 Jev 判断） |
| `answer_type` | TEXT | **新增**。`'faq'` = 直接返回 answer_template；`'llm'` = 注入 answer_template 到 LLM prompt |
| `answer_template` | TEXT | answer_type='faq' 时为固定答案；answer_type='llm' 时为注入规则 |
| `followup_prompt` | TEXT | 追问提示（两种类型都可选，后处理追加） |
| `created_at` | TIMESTAMPTZ | 创建时间 |
| `updated_at` | TIMESTAMPTZ | 更新时间 |

**已删除字段**：
- `answer_mode`：原 `literal`/`prompt` 区分，`literal` 本质就是 FAQ 行为，已由 `answer_type` 替代
- `followup_mode`：业务代码中从未使用，追问统一走后处理追加

### 2.2 删除 FAQ 表

```sql
DROP TABLE myagent_faq;
```

FAQ 表的数据迁移到 scene_templates（如果有的话）：
```sql
INSERT INTO myagent_scene_templates (scene_id, name, trigger, answer_type, answer_template)
SELECT 
  'faq_' || id,
  LEFT(question, 50),
  question,           -- trigger 直接用 question 文本
  'faq',              -- answer_type = faq
  answer              -- answer_template 存固定答案
FROM myagent_faq;
```

### 2.3 条目数上限

**48 条**。在后端 CRUD 接口和前端新建时校验。Jev Choice 上限 255，48 条留足余量保证准确率。

### 2.4 统一后的对话流程

```
用户提问
    │
    ├─ sens_task（敏感词检测，~1.9s）  ┐
    ├─ scene_task（场景判断，~1.9s）   ├─ 三者并行
    └─ plan_task（检索规划，~4.1s）    ┘
         │
    scene_task 返回结果：
      → Jev Choice 判断命中哪个 scene_id
      → 查该 scene 的 answer_type
         ├─ answer_type='faq' → 直接返回 answer_template（mode=faq → END）
         ├─ answer_type='llm' → 注入 scene_id 到 state，继续走 RAG
         └─ 未命中任何场景 → 继续走 RAG
    │
    plan_task 完成 → route_plan_retrieval 判断走向
         │
         ├─ scene 命中 faq → 已在 scene_task 返回，直接 END
         ├─ scene 命中 llm 或未命中 → route_plan_retrieval 正常路由
         │    ├─ target_tags 非空 → node_retrieve
         │    ├─ target_tags 空 + skip → node_retrieve
         │    └─ target_tags 空 → node_no_kb_hit
         │
    node_retrieve → node_generate（消费 scene llm 模板）
```

**关键变化**：
- `node_faq_match` 节点删除
- FAQ 命中逻辑前移到 `scene_task`（`_judge_scene` 内部）
- `route_plan_retrieval` 不再有 `node_faq_match` 分支
- `route_faq_match` 条件边删除

### 2.5 `_judge_scene` 改造

当前 `_judge_scene` 只返回 `scene_id`。改造后需要同时返回 `scene_id` 和 `answer_type`，让调用方知道是短路返回还是注入走 RAG。

```python
async def _judge_scene(query: str, scenes: list[dict] = None) -> tuple[str, str]:
    """判断 query 命中哪个场景。
    
    返回 (scene_id, answer_type)：
    - 命中且 answer_type='faq' → 调用方直接返回 answer_template
    - 命中且 answer_type='llm' → 调用方注入 scene_id 走 RAG
    - 未命中 → ("", "")
    """
```

### 2.6 scene_task 调用方改造（stream_response）

```python
# 当前（第 1562-1578 行）：
scene_task = asyncio.create_task(_judge_scene(message, scenes))
await asyncio.gather(sens_task, scene_task, return_exceptions=True)
scene_id = scene_task.result()
if scene_id:
    initial_state["scene"] = scene_id

# 改造后：
scene_task = asyncio.create_task(_judge_scene(message, scenes))
await asyncio.gather(sens_task, scene_task, return_exceptions=True)
scene_id, answer_type = scene_task.result()

if answer_type == "faq":
    # FAQ 类型：直接返回预设答案，不走主图
    scene_obj = next((s for s in scenes if s["id"] == scene_id), None)
    if scene_obj:
        faq_answer = scene_obj["answer_template"]
        # 下发答案 + done（和当前 node_faq_match 命中的处理一致）
        hit = True
        ctx["final_mode"] = "faq"
        ctx["full_answer"] = faq_answer
        # followup 追加（如果配了）
        # ... 
elif scene_id:
    # LLM 类型：注入 scene_id，继续走 RAG
    initial_state["scene"] = scene_id
    ctx["scene_id"] = scene_id
```

---

## 3. 后端改动

### 3.1 `table_data/create_all_tables.py`

| 改动 | 说明 |
|------|------|
| 删除 FAQ 表 DDL | 删除 `CREATE TABLE {FAQ_TABLE}` 和索引 |
| 场景模板表加字段 | DDL 中加 `answer_type TEXT NOT NULL DEFAULT 'llm'` |
| FAQ_TABLE 从 ALL_TABLES 移除 | 第 21 行 |
| SCENE_SEED 加 answer_type | 示例种子数据加 `"answer_type": "llm"` |
| 删除 FAQ_TABLE 环境变量引用 | 如果有的话 |

### 3.2 `calculate_logic/rag/scene_template_mgmt.py`

| 改动 | 说明 |
|------|------|
| `SceneTemplateCreate` 模型加字段 | 加 `answer_type: str = "llm"`、`followup_prompt: str = ""` |
| `SceneTemplateCreate` 模型删字段 | 删除 `answer_mode`、`followup_mode`（已废弃） |
| `list_scene_templates` 查询调整 | SELECT 改为 `answer_type, followup_prompt`，移除 `answer_mode`；返回对应调整 |
| `create_scene_template` 调整 | INSERT 改为 `answer_type, followup_prompt`，移除 `answer_mode`；新增 48 条上限校验和 scene_id 去重校验 |
| `update_scene_template` 调整 | UPDATE 改为 `answer_type, followup_prompt`，移除 `answer_mode` |

### 3.3 `calculate_logic/fetch_knowledge/faq_mgmt.py`

**整个文件删除**。

- FAQ 路由 `/api/v1/faq` 全部移除
- `_invalidate_faq_cache`、`_renumber_ids` 等函数不再需要

### 3.4 `app/app.py`

| 改动 | 说明 |
|------|------|
| 删除 faq_router import | 第 28 行 `from calculate_logic.fetch_knowledge.faq_mgmt import router as faq_router` |
| 删除 faq_router 注册 | 第 66 行 `app.include_router(faq_router)` |

### 3.5 `calculate_logic/rag/generate_answer.py`

这是改动最大的文件。

#### 删除的代码

| 位置 | 内容 |
|------|------|
| 第 32 行 | `REQUIRED_ENV_VARS` 中移除 `PG_MYAGENT_FAQ_TABLE`, `FAQ_COVERAGE_THRESHOLD`, `FAQ_RATIO_THRESHOLD` |
| 第 619-741 行 | 整个 FAQ 匹配区块：`_faq_cache`、`_FaqBM25`、`_load_faq`、`_calc_idf_from_corpus`、`_get_faq_bm25`、`_aget_faq_bm25`、`preload_faq_index`、`node_faq_match` |
| 第 1128 行 | `builder.add_node("node_faq_match", node_faq_match)` |
| 第 1139-1142 行 | `route_plan_retrieval` 的 `node_faq_match` 分支 |
| 第 1145-1158 行 | `route_faq_match` 函数和条件边 |
| 第 1519-1527 行 | 主图事件循环中 `node_faq_match` 的输出处理 |
| 第 922-925 行 | `node_generate` 中 `answer_mode == "literal"` 的分支处理（已由 answer_type='faq' 短路替代） |

#### 修改的代码

| 位置 | 改动 |
|------|------|
| 第 32 行 | `REQUIRED_ENV_VARS` 移除 `PG_MYAGENT_FAQ_TABLE`, `FAQ_COVERAGE_THRESHOLD`, `FAQ_RATIO_THRESHOLD` |
| 第 399 行 | `route_plan_retrieval` 返回类型移除 `node_faq_match`，改为 `Literal["node_no_kb_hit", "node_retrieve"]` |
| 第 404、411-412 行 | 删除 `target_tags 非空 → node_faq_match` 的分支，target_tags 非空直接走 `node_retrieve` |
| 第 505-530 行 | `_load_scenes` 的 SELECT 改为 `answer_type, followup_prompt`，移除 `answer_mode` |
| 第 515-525 行 | scenes 列表的 dict 构造改为 `answer_type`，移除 `answer_mode` |
| 第 533 行 | `_judge_scene` 返回类型改为 `tuple[str, str]`，返回 `(scene_id, answer_type)` |
| 第 564-571 行 | Jev 命中后，查对应 scene 的 `answer_type`，一起返回 |
| 第 918-935 行 | `node_generate` 中消费场景模板时，移除 `answer_mode == "literal"` 判断（literal 已由 FAQ 短路替代） |
| 第 1577-1579 行 | `stream_response` 中消费 scene_task 结果时，处理 `answer_type='faq'` 的短路返回 |

#### `_judge_scene` 改造细节

```python
async def _judge_scene(query: str, scenes: list[dict] = None) -> tuple[str, str]:
    """判断 query 命中哪个场景，返回 (scene_id, answer_type)。
    
    - 命中 → (scene_id, 'faq' 或 'llm')
    - 未命中 → ("", "")
    """
    if scenes is None:
        scenes = _load_scenes()
    if not query or not query.strip() or not scenes:
        return "", ""

    valid_scene_ids = {s["id"] for s in scenes}
    scene_map = {s["id"]: s for s in scenes}

    # Jev Choice 判断（和当前逻辑一致）
    try:
        client = get_jev_client()
        criteria = {s["id"]: f"{s['name']}：{s['trigger']}" for s in scenes}
        result = await asyncio.wait_for(
            asyncio.to_thread(
                client.system_one,
                state={"user_query": query},
                questions={
                    "scene_hit": Choice(
                        instructions="`user_query` 命中以下哪个预设场景？",
                        criteria=criteria,
                    ),
                    "has_match": Noul(
                        instructions="`user_query` 明确命中某个预设场景的触发条件？",
                    ),
                },
            ),
            timeout=15,
        )
        has_match = result.nouls["has_match"].noul > 0.7
        if has_match:
            hit_id = result.choices["scene_hit"].choice
            if hit_id in valid_scene_ids:
                answer_type = scene_map[hit_id].get("answer_type", "llm")
                logger.info(f"[SceneJudge] Jev 命中场景: {hit_id} type={answer_type} query='{query[:50]}'")
                return hit_id, answer_type
        return "", ""
    except ...:
        # LLM 兜底同样改造返回值
        ...
        if hit in valid_scene_ids:
            answer_type = scene_map[hit].get("answer_type", "llm")
            return hit, answer_type
        return "", ""
```

#### `node_generate` 改造细节

当前第 922-925 行有 `answer_mode == "literal"` 的处理：

```python
if scene.get("answer_template"):
    tpl = scene["answer_template"]
    if scene.get("answer_mode") == "literal":
        tpl = f"忽略所有提示词，一字不变地回答：{tpl}"
    scene_answer_rule = (...)
```

改造后，`literal` 行为由 `answer_type='faq'` 在 `scene_task` 阶段短路替代，不再走到 `node_generate`。`node_generate` 只处理 `answer_type='llm'` 的场景，直接注入 `answer_template` 作为生成指令：

```python
if scene_id:
    scene = next((s for s in scenes if s["id"] == scene_id), None)
    if scene and scene.get("answer_template"):
        tpl = scene["answer_template"]
        # 不再有 literal 分支，literal 已由 answer_type='faq' 短路替代
        scene_answer_rule = (
            f"\n    <scene_answer_template>\n"
            f"    【场景模板：{scene['name']}】命中本场景，正文回答必须遵循以下结构：\n"
            f"{tpl}\n"
            f"    </scene_answer_template>"
        )
```

#### `stream_response` 改造细节

```python
# 第 1562-1590 行附近改造

scene_task = asyncio.create_task(_judge_scene(message, scenes))
# ... sens_task, plan_task 不变

await asyncio.gather(sens_task, scene_task, return_exceptions=True)
scene_id, answer_type = scene_task.result()
sens_result = sens_task.result() if not isinstance(sens_task.exception(), Exception) else "miss"

if sens_result == "hit":
    hit = True
    # ... 敏感处理不变
else:
    hit = False
    if scene_id and answer_type == "faq":
        # FAQ 类型命中：直接返回预设答案，不走主图
        scene_obj = next((s for s in scenes if s["id"] == scene_id), None)
        if scene_obj:
            hit = True   # 标记 hit，跳过主图
            faq_answer = scene_obj["answer_template"]
            ctx["final_mode"] = "faq"
            ctx["full_answer"] = faq_answer
            ctx["scene_id"] = scene_id
            logger.info(f"[SceneJudge] FAQ命中: scene={scene_id} 直接返回预设答案")
    elif scene_id:
        # LLM 类型命中：注入 scene_id，继续走 RAG
        initial_state["scene"] = scene_id
        ctx["scene_id"] = scene_id
        logger.info(f"[SceneJudge] LLM场景命中: {scene_id}")

    # 等待检索规划完成（非 FAQ 命中时才需要）
    if not hit:
        try:
            plan_result = await plan_task
            # ... 注入 plan_result 不变
        except Exception as e:
            # ... 不变
```

FAQ 命中后的 SSE 下发逻辑（复用当前 `node_faq_match` 的处理方式）：

```python
# 在主图事件循环之前，如果 hit 且 final_mode='faq'，直接下发
if hit and ctx.get("final_mode") == "faq":
    # 和当前第 1519-1527 行逻辑一致
    yield f"data: {json.dumps({'type': 'content', 'content': ctx['full_answer']})}\n\n"
    
    # followup 追加（如果配了 followup_prompt）
    scene_obj = next((s for s in scenes if s["id"] == scene_id), None)
    related_questions = []
    if scene_obj and scene_obj.get("followup_prompt"):
        followup_q = scene_obj["followup_prompt"].strip()
        if followup_q:
            related_questions.append(followup_q)
    
    yield f"data: {json.dumps({'type': 'done', 'sources': [], 'related_questions': related_questions})}\n\n"
    _persist_turn(ctx)
```

### 3.6 `app/warmup.py`

| 改动 | 说明 |
|------|------|
| 删除 `_warmup_bm25_indices` 函数 | 第 200-209 行 |
| 删除调用 | 第 228 行 `_warmup_redis_pool()` 附近 |
| 删除 `preload_faq_index` import | 第 202 行 |

### 3.7 `.env.example`

| 改动 | 说明 |
|------|------|
| 删除 `PG_MYAGENT_FAQ_TABLE` | 第 50 行 |
| 删除 `FAQ_COVERAGE_THRESHOLD` 和注释 | 第 68-70 行 |
| 删除 `FAQ_RATIO_THRESHOLD` 和注释 | 第 71-72 行 |
| 删除 `NODE_HINTS` 中的 `node_faq_match` | 第 101 行 |

### 3.8 `requirements.txt`

| 改动 | 说明 |
|------|------|
| 删除 `rank-bm25` | 如果只有 FAQ 在用的话（需确认是否有其他引用） |

---

## 4. 前端改动

### 4.1 侧边栏导航

当前 `SIDEBAR_NAV`（第 2224-2229 行）中 `faq` 导航项标签是"场景配置"，指向 `panel-faq`。合并后不需要改动导航项本身——它已经是场景配置的入口，只是内部内容需要调整。

### 4.2 `switchPage` 触发

当前第 2206 行：`if (pageId === 'faq') loadSceneConfigList();`

不需要改动——仍然加载场景配置列表。

### 4.3 KPI 统计卡片

当前三个 KPI（第 1678-1706 行）：
- 场景总数
- 精准匹配
- 模糊匹配

改为：
- 场景总数
- 直接回答（answer_type='faq' 的数量）
- LLM生成（answer_type='llm' 的数量）

### 4.4 列表表头

当前表头（第 1719-1727 行）：
```
编号 | 场景名称 | 匹配模式 | 响应模式 | 场景文本 | 响应内容 | 操作
```

改为：
```
编号 | 场景名称 | 响应类型 | 场景文本 | 响应内容 | 追问 | 操作
```

- 删除"匹配模式"列（exact/fuzzy 概念废弃）
- "响应模式"改为"响应类型"（FAQ/LLM）
- 新增"追问"列（显示 followup_prompt 的摘要）

### 4.5 编辑弹窗

当前弹窗字段（第 1746-1783 行）：
1. 场景名称
2. 匹配模式（精准/模糊）
3. 场景文本（trigger）
4. 响应模式（严格/发散）
5. 响应内容

改为：
1. **场景名称**（不变）
2. **响应类型**（新增，替代"匹配模式"）：直接回答 / LLM生成
   - "直接回答" = answer_type='faq'，命中后直接返回答案
   - "LLM生成" = answer_type='llm'，命中后注入规则走 RAG
3. **场景文本**（trigger，不变）：描述触发条件
   - placeholder 统一为："描述什么情况下触发此场景，如：用户询问产假、陪产假、生育津贴等"
4. **响应内容**（根据响应类型动态变化）：
   - answer_type='faq' 时：placeholder = "输入固定回答内容，将一字不差地返回给用户"
   - answer_type='llm' 时：placeholder = "输入响应模式/指令，如：回答分三部分：1.政策规定 2.申请材料 3.申请流程"
5. **追问提示**（新增，原 followup_prompt，之前在表中存在但前端没暴露）：
   - 可选填
   - placeholder = "可选，命中场景后追加的追问提示"

**删除的字段**：
- "匹配模式"（精准/模糊）：概念废弃，Jev 统一做语义判断
- "响应模式"（严格/发散）：由"响应类型"替代，`literal` = FAQ，`prompt` = LLM

### 4.6 弹窗 JS 逻辑

#### `openSceneConfigForm`（第 4162-4186 行）

```javascript
function openSceneConfigForm(sceneKey) {
    const mask = document.getElementById('sceneConfigMask');
    const title = document.getElementById('sceneConfigModalTitle');
    if (sceneKey) {
        _editingSceneConfigId = sceneKey;
        const s = _sceneConfigList.find(x => x.scene_id === sceneKey) || {};
        title.textContent = '编辑场景';
        document.getElementById('scInputName').value = s.name || '';
        _setSegToggle('scAnswerTypeToggle', s.answer_type || 'llm');
        document.getElementById('scInputMatchText').value = s.trigger || '';
        document.getElementById('scInputResponseText').value = s.answer_template || '';
        document.getElementById('scInputFollowup').value = s.followup_prompt || '';
    } else {
        _editingSceneConfigId = null;
        title.textContent = '新建场景';
        document.getElementById('scInputName').value = '';
        _setSegToggle('scAnswerTypeToggle', 'llm');
        document.getElementById('scInputMatchText').value = '';
        document.getElementById('scInputResponseText').value = '';
        document.getElementById('scInputFollowup').value = '';
    }
    updateAnswerTypeHint();
    mask.style.display = 'flex';
}
```

#### `submitSceneConfigForm`（第 4193-4235 行）

```javascript
async function submitSceneConfigForm() {
    // ... 校验逻辑不变
    const answerType = _getSegToggle('scAnswerTypeToggle');  // 'faq' 或 'llm'
    const payload = {
        name,
        answer_type: answerType,
        trigger: matchText,
        answer_template: responseText,
        followup_prompt: followupText,
    };
    // ... 提交逻辑不变
}
```

**注意**：payload 中不再包含 `answer_mode`，前端不再发送该字段。

#### `renderSceneConfigTable`（第 4109-4136 行）

```javascript
function renderSceneConfigTable() {
    // ...
    tbody.innerHTML = sorted.map(s => {
        const answerType = s.answer_type || 'llm';
        const typeTag = answerType === 'faq' 
            ? '<span class="scene-tag scene-tag-faq">直接回答</span>'
            : '<span class="scene-tag scene-tag-llm">LLM生成</span>';
        const triggerText = s.trigger || '';
        const responseText = s.answer_template || '';
        const followupText = s.followup_prompt || '';
        // ... 渲染行
    }).join('');
}
```

#### `updateSceneConfigKpi`（第 4086-4098 行）

```javascript
function updateSceneConfigKpi() {
    const total = _sceneConfigList.length;
    const faqCount = _sceneConfigList.filter(s => s.answer_type === 'faq').length;
    const llmCount = _sceneConfigList.filter(s => s.answer_type === 'llm').length;
    // 更新三个 KPI
}
```

#### 新增条目数校验

```javascript
async function submitSceneConfigForm() {
    // 新建时检查上限
    if (!_editingSceneConfigId && _sceneConfigList.length >= 48) {
        await showAlert('上限', `场景数量已达上限（48条），请先删除不需要的场景`);
        return;
    }
    // ...
}
```

### 4.7 列表行标签样式

新增 CSS 标签样式（或复用现有的 `scene-tag` 类）：

```css
.scene-tag-faq { background: rgba(var(--c-brand-rgb), 0.1); color: var(--c-brand); }
.scene-tag-llm { background: rgba(99, 102, 241, 0.1); color: #6366f1; }
```

### 4.8 权限

当前 `hasPermission('faq:save')` / `hasPermission('faq:delete')` 在 `did-auth.js` 中始终返回 `true`。权限 key 可以改为 `scene:save` / `scene:delete`，但因为 `hasPermission` 始终返回 `true`，实际上不影响功能。统一改为 `scene:save` / `scene:delete` 更清晰。

---

## 5. 数据迁移

### 5.1 存量 FAQ 数据迁移

存量 FAQ 表无数据，不需要迁移。直接删除 FAQ 表。

### 5.2 存量场景模板数据迁移

给现有场景模板数据补充 `answer_type` 默认值，并删除废弃字段：

```sql
-- 加字段
ALTER TABLE myagent_scene_templates ADD COLUMN answer_type TEXT NOT NULL DEFAULT 'llm';

-- 删废弃字段
ALTER TABLE myagent_scene_templates DROP COLUMN answer_mode;
ALTER TABLE myagent_scene_templates DROP COLUMN followup_mode;
```

### 5.3 表结构变更

```sql
DROP TABLE myagent_faq;
```

### 5.4 scene_id 去重规则

合并后 scene_id 由前端生成（基于 name），需要去重校验：

- **前端**：新建时，`scene_id = name.replace(/\s+/g, '_').toLowerCase()`，提交前检查是否和已有场景的 scene_id 重复
- **后端**：`create_scene_template` 接口做二次校验，`SELECT id FROM myagent_scene_templates WHERE scene_id = %s`，重复则返回 400
- **修改时**：如果 name 变了导致 scene_id 变了，同样需要检查重复（当前 scene_id 是 UNIQUE 约束，DB 层会拦截，但需要给用户友好提示）

---

## 6. 实施步骤

| 步骤 | 文件 | 改动内容 | 工作量 |
|------|------|----------|--------|
| 1 | `table_data/create_all_tables.py` | 删除 FAQ 表 DDL，场景表加 answer_type、删 answer_mode/followup_mode，更新 SEED | ~20 行 |
| 2 | `calculate_logic/rag/scene_template_mgmt.py` | 加 answer_type/followup_prompt 字段，删 answer_mode/followup_mode，加 48 条上限和 scene_id 去重校验 | ~25 行 |
| 3 | `calculate_logic/fetch_knowledge/faq_mgmt.py` | **删除整个文件** | -187 行 |
| 4 | `app/app.py` | 删除 faq_router import 和注册 | -2 行 |
| 5 | `calculate_logic/rag/generate_answer.py` | 删除 FAQ 区块，改造 _judge_scene，改造 node_generate（删 literal 分支），改造 stream_response，改造路由 | ~-120 行，+50 行 |
| 6 | `app/warmup.py` | 删除 BM25 预热 | -10 行 |
| 7 | `.env.example` | 删除 FAQ 相关配置 | -8 行 |
| 8 | `requirements.txt` | 不改动（保留 rank-bm25，避免 docker 重新下载依赖） | 0 行 |
| 9 | `app/index.html` | KPI 改造，表头改造，弹窗改造（加追问框、删匹配模式/响应模式），JS 逻辑改造，scene_id 去重 | ~100 行改动 |
| 10 | `app/did-auth.js` | 权限 key 改名（可选） | ~4 行 |
| **合计** | 8 个文件 | | ~-200 行，+200 行 |

---

## 7. 删除清单

| 删除项 | 文件 | 原因 |
|--------|------|------|
| `myagent_faq` 表 | create_all_tables.py | 合并到 scene_templates |
| `faq_mgmt.py` 整个文件 | calculate_logic/fetch_knowledge/ | 功能合并到 scene_template_mgmt |
| `faq_router` | app.py | 路由随文件删除 |
| `_faq_cache` | generate_answer.py | 不再需要内存缓存 |
| `_FaqBM25` | generate_answer.py | 不再用 BM25 |
| `_load_faq` | generate_answer.py | 不再从 PG 加载 FAQ |
| `_calc_idf_from_corpus` | generate_answer.py | 不再算 IDF |
| `_get_faq_bm25` / `_aget_faq_bm25` | generate_answer.py | 不再获取 BM25 索引 |
| `preload_faq_index` | generate_answer.py | 不再预热 |
| `node_faq_match` | generate_answer.py | 节点删除，逻辑前移到 scene_task |
| `route_faq_match` | generate_answer.py | 条件边删除 |
| `_warmup_bm25_indices` | warmup.py | 不再预热 BM25 |
| `FAQ_COVERAGE_THRESHOLD` | .env.example | 阈值不再需要 |
| `FAQ_RATIO_THRESHOLD` | .env.example | 阈值不再需要 |
| `PG_MYAGENT_FAQ_TABLE` | .env.example | 表已删除 |
| `node_faq_match` in NODE_HINTS | .env.example | 节点已删除 |
| `rank-bm25` | requirements.txt | 不删除（避免 docker 重新下载依赖，留着无害） |

---

## 8. 已确认的设计决策

1. **`rank-bm25` 依赖保留** — 虽然不再使用，但从 requirements.txt 删除会触发 docker 重新下载全部依赖，成本不值得。保留不动 ✅
2. **存量 FAQ 数据迁移** — FAQ 表无数据，不需要迁移，直接删除表 ✅
3. **`answer_mode` 字段删除** — `literal` 本质就是 FAQ 行为（一字不变输出），由 `answer_type='faq'` 短路替代；`prompt` 就是 LLM 行为，由 `answer_type='llm'` 替代。字段完全多余，删除 ✅
4. **`followup_mode` 字段删除** — 业务代码中从未使用，追问统一走后处理追加，删除 ✅
5. **followup_prompt 前端编辑入口** — 加上，在弹窗中暴露为可选填的"追问提示"输入框 ✅
6. **scene_id 去重** — 前端提交前检查 + 后端二次校验 + DB UNIQUE 约束兜底 ✅
