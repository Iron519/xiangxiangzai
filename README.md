# 象象仔设计文档

## 产品定位

服务小微客户的智能客服产品，但并非直接用户触点，而是管理端，其聊天功能可以单独解耦，部署在任何载体提供面向用户的对话功能。

## 产品受众

需要7*24的智能客服，并且不闲聊，只提供严格基于内部知识的信息，对回答的准确率有较高要求的小微商家。

---

接下来将以用户使用流程为顺序，逐节点解读其功能和设计理念。

## 〇、身份

首先划分不同的身份：
- IrOn：本产品开发者，唯一的admin权限拥有者，可以审批用户注册请求、发布所有用户共享的知识库文件、删除用户、管理用户额度等；
- 客户/管理员：通常指产品的消费者（小微商家），其可以使用产品的管理功能，维护知识库、查看数据报表和分析、洞察用户意向等；
- 用户：终端用户，只能使用聊天功能，其使用入口基于管理员（即商家）的触点设计，包括网页、企业微信等。

## 一、登陆

### 1. 客户登陆

账号 + 密码登录，由于产品消耗的是IrOn自己的千问大模型额度，为避免恶意注册和滥用，设计两层防御：

1. 用户注册+批准制，用户首次注册不能直接成功，而是会提交一个申请（需备注理由），IrOn通过数据库软删功能控制；
2. IrOn会监测用户用量，后台控制Token额度上限（待办）；
3. 但还有一个隐患（待办），就是由于没有唯一依据（比如手机号、微信号...）约束，用户可能多次注册，另外，由于没有设备管理功能，任何人都可以凭账号密码登陆他人账号（甚至包括admin）

### 2. 用户登陆

登录方式视客户需要开发，支持企业微信、自有网页这两种载体。

## 二、鉴权

权限分为横向和纵向——

**横向来讲：** 一个平台支持多位客户上传知识，定制特色流程，所以用户一定要与业务绑定，但是好就好在，不存在单一用户具有横跨多业务的情形（1:1），所以用户本身就能代表业务，无需维护映射关系；

**纵向来讲：** 一个业务下面，所有用户是否有权限看到所有模块（对话、报表、洞察、用户管理...），我们先明确这款产品的用户，是业务管理者而非使用者，所以他应能看到所有自身相关的内容，至于终端用户（chat user），只要使用chat的API即可

所以鉴权只需要一用户张，维护全部内容 —— 表名： `myagent_user`

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

而客户的终端用户与客户共用一个账号密码，相当于产品的 `access key`。由于一个 `access key` 只对应一个用户，无法区分用户，也就无法创建用户体系进行管理。

目前该问题无需解决，等有客户接入后，根据它自身的用户登录鉴权机制，再重建一份数据库，uid换成客户的终端用户，加以统计（待办）。

## 三、知识管理

### 1. 文件上传 & 解析

支持所有类型文件的上传和解析，解析结果为Markdown，其中有实质的图片也将保留。上传&解析的过程是异步并发，系统限制同时最多处理3份文件，第4个请求需排队，但不会阻塞进程，前面的文件上传完毕自动处理排队文件。

#### 1.1. 工具配置

上传&解析涉及将数据写入PG数据库、将PPT转化为PDF、调用MinerU解析pdf、调用VLM描述图片、调用Embedding模型做词嵌入：

连接对象 | 所在模块 | 连接到 | 作用 | 池大小
---|---|---|---|---
PG 连接池 | routes.py | PostgreSQL | 上传任务状态读写、取消清理 | 1~5
PG 连接池 | embedding.py | PostgreSQL | 切片入库（upsert + tsv 刷新） | 1~5
PG 连接池 | file_tagging.py | PostgreSQL | 文件打标（metadata 写入） | 1~5
HTTP 连接池 | miner_u_parse.py | MinerU API (MINERU_API_BASE) | 文件解析（PDF→markdown） | 20 conn / 10 keepalive
HTTP 连接池 | miner_u_parse.py (VLM) | LLM/VLM API (BASE_URL) | VLM 辅助识别（图片判断等） | 20 conn / 10 keepalive
HTTP 连接池 | embedding.py | Embedding API (BASE_URL) | 切片向量化 | 20 conn / 10 keepalive
LibreOffice | routes.py | 本地功能 | PPT -> PDF | 无需连接池

#### 1.2. 总体流程：

1）前端弹窗：修改文件名（display_name）→ 更多选项（文件期限、是否展示内部图片、手动文件描述）→ 确认

2）后端保存文件到 `upload_kb/{uid}/{display_name}`。 *注：`display_name`为前端展示文件名，前后端保持一致*

3）创建任务记录，启动后台协程 `_background_process`，用户上传文件后被异步调用，串行执行两个阶段：

- 解析阶段：调用 `DocumentParser().parse_file()` 将文件转为 markdown（PDF/Office 走 MinerU，纯文本直接读取），期间用户可在前端点击停止，停止则不仅任务，所有过程性文件也全部删除
- 注入阶段：切块 → 向量化（Embedding API）→ 入库（PG），写入 kb_chunks 表
- 打标阶段：调用`calculate_logic/rag/file_tagging.py`，生成文件的描述和标签，作为检索的过滤依据，直接决定用户查询的路线（chitchat or rag）

4）全程通过 `_set_task_status` 更新进度状态到PG表 `myagent_upload_tasks`，前端通过 SSE 端点每1.5秒从PG表读取最新状态推给前端，前端据此更新进度展示。

#### 1.3. 上传 - 前端操作

1）用户在`知识库`页面点击`上传文件`，将弹出文件选择窗口，仅支持单选

2）选中文件后，弹出`文件信息`弹窗，在输入框内展示文件名，用户可自由修改，如不修改，则以文件原名为准；如果存在同名文件，提醒重新命名或删除同名文件后，重新上传，不自动覆盖（待办）

3）点开`更多选项`后，有3个选项：

- 文件期限：文件的到期时间，默认永久有效，过期文件不会删除，但是将不被任何节点检索，形同删除
- 图片展示：文件内图片是否在对话时展示给用户，默认展示，无论选择展示与否，不影响解析阶段提取“图片-内容”锚点映射到anchor.json中，其之影响前端渲染
- 文件描述：对该文件的归纳性描述，选填，不填则由LLM生成，这将影响用户问询是否会启用检索，因为用户问询会结合该描述，再去和文件标签进行匹配，所以话术中提示用户若非必要，无需填写

4）用户可以查看admin和自己上传的文件，但仅能操作自己上传的文件，只有当以下3种来源的数据齐备时，前端页面才能看到该文件，否则将静默消失，3种来源为：

- **upload_kb** 的原文件
- **parse_result** 的解析结果/过程性文件
- **PG表** 中的 kb_chunks

用户可以查看：解析结果的全文Markdown、子块切片，另外还可以删除，当点击`删除`，PG表中、解析过程性文件、解析结果全部删除，但是原文件会得以保留（便于追溯）

#### 1.4. 上传 - 文件存储

用户上传后，系统会将文件存储到`upload_kb/{uid}/{display_name}`，并以修改后的文件名作为路径；

解析的中间结果会存储在`fetch_knowledge/parse_result/{uid}/{display_name}`中，`display_name`是带文件类型后缀的，因为需要避免同名不同类的文件冲突。目录中有两个文件是必然生成的——

- merged.md，这是最后的解析结果markdwon；
- anchors.json，这是建立图片与内容文字映射关系（即便没有图片也会生成）
- 其余文件则是当上传内容非纯文本时进行渲染和minerU识别的中间结果，只有当涉及ppt、pdf、image时才会生成


#### 1.5. 解析 - 解析流程

入口方法 `DocumentParser.parse_file()` 根据文件扩展名分发到 6 条处理路径：

```
parse_file(file_path, display_name, uid)
  入口方法：根据文件扩展名分发到6条处理路径，异常时返回空字符串
│
├── .txt / .md / .csv ──→ _parse_text
│   纯文本路径：直接读取文件内容，无需外部API
│                          ├── _read_text_auto
│   │   按顺序尝试编码探测：UTF-8 → GB18030 → GBK → GB-2312 → Big5 → Latin-1
│   │   第一个成功的编码即为文件编码，读取原文返回
│                          ├── [条件] _enhance_text_to_markdown
│   │   触发条件：ext != '.md' 且 文本长度 ≥ 50 字符
│   │   逻辑：将HTML表格转为Markdown表格，清洗多余空白/换行
│                          ├── _write_merged_md          → merged.md
│   └── _write_anchors_json(空)   → anchors.json（纯文本无图片，锚点为空列表）
│
├── .pdf ──→ _parse_pdf_pipeline
│   PDF管线：最核心的解析路径，调用MinerU云API + VLM辅助识别
│             ├── MinerUProcessor 初始化 + _prepare_document_dirs
│   │   创建解析目录：page_pdfs/ page_results/ images/ page_renders/
│   │   若目录已存在则清空旧产物，避免残留干扰
│             ├── split_pdf_to_pages
│   │   用PyMuPDF将PDF逐页拆分为独立单页PDF文件，返回页码+路径列表
│             ├── asyncio.gather(并发处理每页):
│   │   每页独立处理，互不阻塞，并发度由_mineru_sem(2)限制MinerU API调用
│             │     │
│             │     ├── prepare_one_page_before_process
│   │   单页预处理：调用MinerU解析 + 判断页面类型
│             │     │     ├── call_mineru
│   │   │   MinerU云API五步流程：
│   │   │   1. POST提交batch → 获取batch_id + upload_url（带重试）
│   │   │   2. PUT上传单页PDF字节到upload_url
│   │   │   3. GET轮询解析结果（最多120次×3s=360s），等待state=="done"
│   │   │   4. GET下载zip结果包
│   │   │   5. 解压组装：full.md→md_content, layout.json→middle_json, images→base64
│             │     │     └── judge_image_only_by_mineru
│   │   │   判断该页是否为纯图片页，三个AND条件全部满足才判定为True：
│   │   │   - 图片并集面积/页面面积 ≥ 0.5（避免重叠区域重复计算）
│   │   │   - 文字字符数 ≤ 1200
│   │   │   - 文字行数 ≤ 30
│             │     │
│             │     └── process_one_page
│   │   单页正式处理，按 is_image_only 分流到两条路径：
│             │           ├── [is_image_only=True] 纯图片页
│   │   │   MinerU对图片页的文本提取效果差，改用VLM整页转写
│             │           │     ├── render_page_pdf_to_image
│   │   │   │   将单页PDF渲染为PNG图片（zoom=2.0高清），存入page_renders/
│             │           │     ├── call_vlm_describe_image(whole_page=True)
│   │   │   │   VLM整页转写：prompt要求"将PDF页面截图无损转写为高质量中文Markdown"
│   │   │   │   失败时回退使用MinerU的md_content，不中断流程
│             │           │     └── 构建 image_only 锚点
│   │   │   │   整页渲染图作为可检索插图，建立锚点记录
│             │           │
│             │           └── [is_image_only=False] 正常文本页
│   │   │   MinerU文本提取效果好，VLM仅用于辅助描述单张插图
│             │                 ├── save_images_from_result
│   │   │   │   将MinerU返回的base64图片解码落盘到images/
│   │   │   │   建立Markdown引用路径 → 本地文件路径的映射
│             │                 ├── 并发 call_vlm_describe_image(whole_page=False)
│   │   │   │   对每张图片并发调用VLM（_vlm_sem限制最多5并发）
│   │   │   │   prompt要求返回JSON：{meaningful: bool, desc: string}
│   │   │   │   meaningful判断图片是否有业务价值（非装饰性/水印/空白）
│             │                 ├── build_page_anchors
│   │   │   │   遍历middle_json的块结构，建立"文字块↔插图"映射关系
│   │   │   │   每个锚点记录：page_no, block_index, type, text, image_paths
│   │   │   │   插图需通过三重过滤*才写入锚点
│             │                 └── replace_images_with_descriptions
│   │   │   │   将Markdown中的![](ref)替换为"> 【插图描述】{desc}\n\n![]({url})"
│   │   │   │   不达标的图片不内联URL，仅保留描述文字
│             │
│             ├── merge_page_results
│   │   按page_no排序，逐页拼接Markdown，使用两种策略：
│   │   - 结构优先**：检测middle_json块类型，强块级元素边界强制断段
│   │   - 字符兜底***：非强块级边界处用smart_join_md按标点智能续接
│   │   汇总所有页的anchors、filters、content_list、middle_json
│             ├── write_anchors_json         → anchors.json
│   ├── _enhance_text_to_markdown  (HTML表格→MD + 清洗，同_parse_text中的逻辑)
│             └── _write_merged_md           → merged.md
│
├── .jpeg / .jpg / .png ──→ _parse_image
│   纯图片路径：整张图交给VLM识别，不经过MinerU
│                           ├── _call_vlm
│   │   VLM识别图片内容，prompt要求"企业级知识图谱提取"
│   │   返回JSON：{md_content: 描述文本, thought: 推理过程}
│                           ├── _write_image_only_anchor
│   │   图片副本落盘到images/，生成单条image_only锚点
│   └── _write_merged_md         → merged.md（格式：图片描述 + 内联图引用）
│
├── .docx ──→ _parse_word
│   Word原生解析：用python-docx按阅读顺序遍历，不走PDF转换
│             ├── _iter_word_blocks
│   │   遍历文档body的XML子元素，按文档顺序输出段落和表格
│   │   段落输出为纯文本，表格输出为Markdown表格
│             ├── _word_run_images
│   │   从段落的OOXML中检测a:blip元素，提取内嵌图片的字节流
│   │   一个段落可能包含多张图片，逐张返回
│             ├── [有图片] _process_office_image
│   │   统一的Office图片处理流程：
│   │   1. _call_vlm 识别图片内容
│   │   2. _save_anchor_image 图片落盘到images/
│   │   3. 构建anchor条目（含VLM描述文本 + 图片路径）
│   │   4. 将VLM文本 + 图片URL替换到原文对应位置
│             ├── _clean_markdown_text
│   │   清理多余空行、修复Markdown格式
│             └── _write_office_outputs   → merged.md + anchors.json
│
├── .pptx ──→ _parse_office_via_mineru
│   PPT路径：先转PDF再复用完整PDF管线（PPT原生解析质量不稳定）
│             ├── _convert_office_to_pdf
│   │   调用本地LibreOffice：soffice --headless --convert-to pdf --outdir {tmp} {input}
│   │   超时60秒，超时则kill进程返回False
│   │   转换成功后os.rename到目标路径
│             └── _parse_pdf_pipeline
│   │   复用上述完整PDF管线（拆页→MinerU→VLM→合并→落盘）
│   │   解析完成后shutil.rmtree清理临时目录
│
└── .xlsx / .xls ──→ _parse_excel
    Excel原生解析：用pandas + openpyxl解析，不走PDF转换
                    ├── _extract_excel_cell_images
    │   用openpyxl加载工作簿，遍历每个工作表的_image属性
    │   提取图片字节流 + 从anchor的_from属性解析(row, col)单元格位置
                    ├── pd.read_excel(sheet_name=None)
    │   读取所有工作表为DataFrame，dropna清洗空行空列
                    ├── [有图片] _process_office_image → _call_vlm
    │   同Word的图片处理流程
    │   能定位到(row,col)的图片：在对应行追加图片描述 + 内联图引用
    │   无法定位的图片：作为新行追加到表格末尾
                    ├── _convert_table_to_markdown
    │   将2D数组（含图片行）转为标准Markdown表格格式
                    ├── _clean_markdown_text
                    └── _write_office_outputs   → merged.md + anchors.json
```

在一些解析中，有三处具备独到性：

- **三重过滤** ：PDF 正常页中的插图需同时满足：面积比≥0.03、像素边长≥30px、VLM判定有业务意义，才作为可检索插图内联到Markdown并建锚点；不达标的图片保留描述文字但不内联URL。Office 图片不过滤，全部建锚点。
- **结构优先** ：跨页拼接时，若上一页末尾或下一页开头是 title/image/table/list/formula 块类型，则强制断段，不与前页续接。
- **字符兜底** ：非强块级元素边界处，使用 `smart_join_md` 按标点符号做字符级智能续接，避免段落被截断。


#### 1.6. 解析 - 解析存储

所有路径的最终产物统一存储在 `parse_result/{uid}/{display_name}/` 下：

```
parse_result/{uid}/{display_name}/
  ├── merged.md               — 最终整篇Markdown（所有文件类型必生成）
  ├── anchors.json            — 图片锚点 + 过滤记录（所有文件类型必生成，纯文本为空）
  ├── page_pdfs/              — [仅PDF] 单页PDF
  ├── page_results/           — [仅PDF] 每页解析中间结果JSON + final.md
  ├── images/                 — [有图片时] 提取的图片文件
  └── page_renders/           — [仅PDF纯图片页] 整页渲染图
```

解析后的文件将通过`calculate_logic/rag/file_tagging.py`进行LLM打标，包括文件描述和文件标签，该节点为必然触发，但是如果用户已经自行填写过描述，则跳过描述归纳，由于标签和描述直接影响问答路径，所以通过显式输出reasoning的方法加强了生成可靠性。

#### 1.7. 解析 - 编辑结果

用户是可以在前端页面手动编辑解析全文的，并且系统将直接将编辑后的文本写回 merged.md 文件（覆盖原文件）。

### 2. 知识切片

知识切片流水线位于 `calculate_logic/chunk_pipeline/` 目录下，包含 4 个核心文件：

| 文件 | 职责 |
|---|---|
| `chunk_pipeline/config.py` | 切片流水线配置与日志 |
| `chunk_pipeline/chunk.py` | 切片算法核心（父块粗切 + 语义精切） |
| `chunk_pipeline/embedding.py` | 向量化与 PG 入库 |
| `chunk_pipeline/routes.py` | 切片 API 路由与后台流水线调度 |

#### 2.1. 总体流程

```
上传 → 解析 → 父块粗切 → 语义精切 → 图片映射 → 向量入库 → 异步打标
                                                         ↓
                                               检索消费（双塔检索→Rerank→LLM生成）
```

**入口**：`routes.py:upload_file()` → POST /chunk/upload → 启动 `_background_process()` 后台协程

**任务状态机**：`saving → parsing → parsed → chunking → embedding → ingesting → completed`

#### 2.2. 主流程

**Step 1: 文件上传**

- **文件**：`chunk_pipeline/routes.py` → `upload_file()`
- **动作**：保存原始文件到 `upload_kb/`，创建 task，启动异步流水线
- **状态**：`saving`
- **前端**：通过 SSE（`GET /kb/upload-sse`）实时推送进度

**Step 2: 文档解析**

- **文件**：`fetch_knowledge/parse_file.py` → `DocumentParser.parse_file()`
- **动作**：按文件类型分发解析（详见 1.5 节解析流程）
- **产出**：
  - `merged.md` — RAG 友好的 Markdown 全文（切片的直接输入）
  - `anchors.json` — 图片锚点映射（文字块 ↔ 图片路径）
- **状态**：`parsing → parsed`

**Step 3: 父块粗切（第一阶段切片）**

- **文件**：`chunk_pipeline/chunk.py` → `chunk_markdown_text()`
- **动作**：
  1. `_extract_block_elements()` — 提取"普通句子"与"块元素(表格/插图)"序列，块元素标记为 **atomic 不可切割**
  2. `_split_sentences()` — 按中英文句号、分号分句
  3. `_chunk_sentence_segments()` — 累积到 2000 字切一个父块，相邻父块**重叠 3 句**
- **特殊处理**：`_split_table_block()` — 超长表格(>500字)拆成多段，每段保留原表头
- **产出**：`List[ChunkOutput]`（每个含 chunk_index, child_content=父块全文, text_hash）
- **关键参数**：`CHUNK_SIZE=2000`, `CHUNK_OVERLAP_SENTENCES=3`, `_MAX_TABLE_CHARS=500`
- **状态**：`parsed → chunking`

**Step 4: 语义精切（第二阶段切片）**

- **文件**：`chunk_pipeline/chunk.py` → `build_semantic_chunks()` → `_split_parent_semantically()`
- **动作**：
  1. `_collect_atomic_units()` — 父块切成句级原子单元（表格/插图整体保留）
  2. 对每个原子单元调用 Embedding API 向量化
  3. `_merge_semantic_units()` — 相邻单元余弦相似度 **≥0.90 合并**为同一子块，<0.90 在此处切分
  4. `_merge_short_chunks()` — 过短子块(<50字)并入相邻子块
- **产出**：`List[SemanticChunkOutput]`（每个含 child_content, parent_content, group_id, internal_id, embedding）
- **关键参数**：`_SIMILARITY_THRESHOLD=0.90`, `_MIN_CHUNK_LENGTH=50`
- **状态**：`chunking`

**Step 5: 图片 URL 映射**

- **文件**：`chunk_pipeline/chunk.py` → `resolve_child_image_urls()`
- **动作**：
  1. `load_document_anchors()` — 读取 `anchors.json`（Step 2 产出）
  2. 对每个子块，按"**文本包含匹配**"策略检查是否包含某个图片锚点的关联文字，匹配则填充 `related_image_url`
- **目的**：检索时不仅返回文本，还能返回相关图片供前端 sources 展示

**Step 6: 向量化与入库**

- **文件**：`chunk_pipeline/embedding.py`
- **向量化**（状态 `embedding`）：
  - `EmbeddingModel.embed()` — 批量调用 Embedding API
  - 每批 ≤20 条，单条截断 ≤5000 字符，并发限流 Semaphore(2)
- **入库**（状态 `ingesting → completed`）：
  - `store_semantic_chunks()` 入口
  - `_offset_chunk_indices()` — 分配全局唯一递增 chunk_index
  - `_upsert_chunks()` — PG UPSERT（`ON CONFLICT (uid, file_name, text_hash) DO UPDATE`），内容级去重
  - `_refresh_content_tsv()` — 调用 `rag/tokenizer.py` 的 `tokenize_for_tsv()` 做 **jieba 分词** → `to_tsvector('simple', ...)` 写入 content_tsv
- **目标表**：`myagent_kb_chunks`（DDL 在 `table_data/create_all_tables.py`）

  | 字段 | 类型 | 说明 |
  |---|---|---|
  | chunk_index | BIGINT PK | 全局唯一递增 |
  | child_content | TEXT | 子块内容（检索单元） |
  | parent_content | TEXT | 父块原文（rerank + sources） |
  | group_id | BIGINT | 父块ID（查找兄弟子块） |
  | internal_id | TEXT | 父块内子块序号 |
  | embedding | vector(4096) | 向量嵌入 |
  | content_tsv | TSVECTOR | 全文检索列 |
  | related_image_url | TEXT | 图片URL |
  | text_hash | TEXT | 内容哈希 |
  | metadata | JSONB | 文件描述+标签 |
  | **UNIQUE** | (uid, file_name, text_hash) | **内容级去重键** |

**Step 7: 异步打标**

- **文件**：`rag/file_tagging.py` → `generate_file_description()`
- **动作**：入库完成后异步触发，用 LLM 生成文件描述和标签，写入 `metadata` JSONB
- **用途**：检索规划阶段读取标签池用于过滤

#### 2.3. 检索消费流程

切片数据的最终用途——检索与生成：

```
用户提问
  ↓
generate_answer.py: node_plan_retrieval()  ← LLM规划检索策略（读取metadata标签）
  ↓
node_retrieve() → do_retrieval()
  ↓
retrieval_rerank.py: PgHybridRetriever.search()
  ├── 向量塔: _vector_search() (pgvector 余弦距离 top-K)
  ├── 关键词塔: _keyword_search() (content_tsv + to_tsquery, jieba分词)
  ├── RRF融合: _rrf_fuse()
  ├── Rerank精排
  └── 断崖检测: _cliff_select() (动态Top-K)
  ↓
_fetch_sibling_child_contents()  ← 取命中子块 + 同group_id前后各1个兄弟子块（共3块）
  ↓
context = 3个子块拼接  →  LLM生成回答
sources = 完整parent_content + related_image_url  →  前端展示
```

#### 2.4. 重切片流程

用户编辑解析全文后可触发重切片，跳过解析阶段：

```
用户编辑全文 → PUT /kb/file/{file_name}/parsed-text (写回merged.md)
  ↓
POST /kb/file/{file_name}/rechunk → _background_rechunk()
  ↓
跳过解析，直接读 merged.md
  → chunk_markdown_text() 粗切
  → build_semantic_chunks() 精切
  → 删除旧切片 (DELETE FROM kb_chunks WHERE file_name=...)
  → store_semantic_chunks() 入库
```

#### 2.5. 核心设计理念

| 设计 | 说明 |
|---|---|
| **两阶段切片** | 父块(2000字)提供上下文窗口给 LLM；子块(语义紧凑)提供精准向量匹配 |
| **父子双存储** | `child_content` 用于检索和 context；`parent_content` 用于 rerank 和 sources 展示 |
| **兄弟子块扩展** | 检索命中1个子块 → context 拼接同父块前后兄弟(共3块)，兼顾精准与上下文完整 |
| **块元素原子化** | 表格/插图标记为 atomic 不可切割，保证不被截断 |
| **内容级去重** | `UNIQUE(uid, file_name, text_hash)` — 相同内容不重复入库 |
| **双塔检索** | 向量塔(语义) + 关键词塔(精确) → RRF 融合，兼顾语义相似和关键词命中 |

## 四、检索

检索系统基于 LangGraph 状态机编排，从用户提问到最终回答，经过检索规划、FAQ 匹配、混合检索、生成回答等环节，并辅以敏感话题检测、场景判定、流式输出等机制。

### 1. 概述

检索系统位于 `calculate_logic/rag/` 目录下，包含 7 个核心文件：

| 文件 | 职责 |
|---|---|
| `rag/generate_answer.py` | LangGraph 对话编排（状态机、节点、路由、流式输出、Session 管理） |
| `rag/retrieval_rerank.py` | 混合检索 + Rerank（双塔粗排 → RRF 融合 → Rerank 精排 → 断崖检测） |
| `rag/tokenizer.py` | 共享 jieba 分词（入库 tsv 与检索 tsquery 共用） |
| `rag/file_tagging.py` | 文件打标（LLM 生成描述+标签，供检索规划过滤） |
| `rag/scene_template_mgmt.py` | 场景模板管理（CRUD + 生成时模板注入） |
| `rag/warmup.py` | 预热（同步实例化 + 异步建连，消除冷启动） |
| `rag/token_usage.py` | Token 用量统计（统一提取 LangChain response 中的 token 消耗） |

#### 1.1. 总体流程

```
用户提问
  │
  ▼
chat_stream (入口)
  ├── Session 管理（新建/校验/超限软删）
  ├── 加载历史消息（Checkpoint 恢复）
  ├── 并行判定: _judge_sensitive + _judge_scene + _generate_session_title
  ├── 敏感命中 → 直接返回固定话术
  └── 启动主图: graph.astream_events → SSE 流式输出
                    │
                    ▼
              node_plan_retrieval (检索规划)
                    │
              route_plan_retrieval (条件路由)
              ┌─────┼──────────┐
          无文件 │  有标签    │  无标签+skip
              │     │          │
     node_no_kb_hit node_faq_match node_retrieve
              │     │          │
              │  route_faq_match route_retrieve_result
              │  ┌────┴───┐    ┌────┴────┐
              │  命中  未命中 空context  非空
              │   │      │      │        │
              │  END  node_retrieve node_no_kb_hit node_generate
              │           │                    │
              │    route_retrieve_result  tools_condition
              │     ┌────┴────┐         ┌────┴────┐
              │  空context  非空    tools      END
              │     │       │        │
              │ node_no_kb_hit node_generate ←─ tool_node (循环)
              │     │
              └────END
```

### 2. LangGraph 状态机

#### 2.1. 节点注册

| 节点 | 函数 | 行号 | 职责 |
|---|---|---|---|
| `node_plan_retrieval` | 入口节点 | 266 | 检索策略规划（指代消解 + 标签匹配 + 参数决策） |
| `node_faq_match` | FAQ 匹配 | 673 | BM25+IDF 加权匹配，命中直接返回 |
| `node_retrieve` | 执行检索 | 755 | 调用混合检索器 + 兄弟子块扩展 |
| `node_no_kb_hit` | 未命中兜底 | 788 | 返回固定话术，不经 LLM |
| `tool_node` | 工具节点 | 140 | 执行工具调用（假期/工时/个人信息查询） |
| `node_generate` | 生成回答 | 842 | System Prompt 构造 + 流式生成 + 工具循环 |

#### 2.2. 边和路由

| 起点 | 路由函数 | 条件 | 目标 |
|---|---|---|---|
| START | - | - | `node_plan_retrieval` |
| `node_plan_retrieval` | `route_plan_retrieval` | 无文件 → `node_no_kb_hit`；有标签 → `node_faq_match`；无标签+skip → `node_retrieve`；无标签+不skip → `node_no_kb_hit` | 条件分支 |
| `node_faq_match` | `route_faq_match` | mode=faq → END；否则 → `node_retrieve` | 条件分支 |
| `node_retrieve` | `route_retrieve_result` | context 空 → `node_no_kb_hit`；非空 → `node_generate` | 条件分支 |
| `node_no_kb_hit` | - | - | END |
| `node_generate` | `tools_condition` | 有 tool_calls → `tool_node`；无 → END | 条件分支 |
| `tool_node` | - | - | `node_generate`（工具循环） |

#### 2.3. ChatState 完整字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | str | 对话 ID |
| `messages` | `Annotated[list, add_messages]` | 历史消息（追加而非覆盖） |
| `context` | str | 检索到的上下文 |
| `sources` | list | 检索信息来源 |
| `mode` | str | 当前对话意图（rag/faq/sensitive/chitchat） |
| `final_answer` | str | AI 最终回答 |
| `turn_count` | int | 对话轮次计数 |
| `query` | `Optional[str]` | 当前查询（LLM 重写后覆盖原始问题） |
| `user` | `Optional[dict]` | 用户画像（uid/username/roles...） |
| `current_time` | `Optional[str]` | 当前时间（供 LLM 推导工具日期参数） |
| `language` | `Optional[str]` | 用户提问语种 |
| `scene` | `Optional[str]` | 场景判定结果（场景 id） |
| `scenes` | `Optional[list]` | 场景模板列表（入口一次性加载） |
| `file_descriptions` | `Optional[list]` | 用户文件描述列表（供检索规划） |
| `planned_top_k` | `Optional[int]` | LLM 规划的检索切片数量 |
| `search_strategy` | `Optional[str]` | 检索策略（direct/broad/precise） |
| `target_tags` | `Optional[list]` | LLM 规划的命中标签列表 |
| `skip_tag_filter` | `Optional[bool]` | 跳过标签过滤走全局检索 |

#### 2.4. Checkpoint 持久化

- 使用 `PostgresSaver` 做 LangGraph checkpoint，自动保存/恢复 `messages` 列表
- 自定义 `_AsyncPostgresSaverAdapter` 适配异步接口（委托到线程池）
- 连接池：`min_size=1, max_size=10`，`open=False` 延迟建立
- `checkpointer.setup()` 幂等建表

### 3. 检索规划

#### 3.1. node_plan_retrieval

检索规划是整个检索系统的"大脑"，用 Fast LLM 分析用户问题并制定检索策略。

**输入**：`state["messages"][-1].content`（最新用户消息）, `state["user"]["uid"]`

**处理流程**：

1. 加载用户文件描述：`load_file_descriptions(uid, ADMIN_UID)` — 含 admin 共享文件
2. 若无文件 → 返回默认规划（top_k=5, direct, 无标签）
3. 汇总所有文件的标签池
4. 清洗历史消息，取最近 `rewrite_n*2+1 = 9` 条（排除当前消息）
5. 调用 Fast LLM 规划

**LLM Prompt 核心要点**：

```
你是一个检索策略规划器。请分析用户问题并制定检索策略。

【历史对话】{history_text}
【用户当前问题】{last_message}
【知识库文件描述】{file_list_text}
【可用标签】{tags_text}

请完成以下任务（按顺序）：
1. 指代消解：基于历史对话和当前问题进行代词消解和省略补全
2. 基于改写结果，结合文件描述，从可用标签中选择相关标签
   - 有相关标签 → 填入 target_tags
   - 问题含术语但标签无法匹配 → target_tags=[], skip_tag_filter=true
   - 纯闲聊 → target_tags=[], skip_tag_filter=false
3. 评估问题难度：simple/moderate/complex
4. 决定检索切片数量：simple(3-5) / moderate(5-8) / complex(8-12)
5. 选择检索策略：direct/broad/precise

输出JSON: {"rewritten_query": "...", "target_tags": [...], "skip_tag_filter": false,
           "difficulty": "simple", "reasoning": "...", "planned_top_k": 5, "search_strategy": "direct"}
```

**LLM 参数**：`FAST_MODEL`, `enable_thinking=False`, `max_tokens=300`, `timeout=15s`

**输出**：`{query, planned_top_k, search_strategy, target_tags, skip_tag_filter, file_descriptions}`

**异常兜底**：回退为 `query=原始问题, top_k=5, direct, 无标签`

#### 3.2. route_plan_retrieval — 路由决策

| 条件 | 路由目标 | 含义 |
|---|---|---|
| `file_descriptions` 为空 | `node_no_kb_hit` | 知识库无文件，直接兜底 |
| `target_tags` 非空 | `node_faq_match` | 有标签 → 先尝试 FAQ 匹配 |
| `target_tags` 空 + `skip_tag_filter=True` | `node_retrieve` | 术语未匹配标签 → 全局检索 |
| `target_tags` 空 + `skip_tag_filter=False` | `node_no_kb_hit` | 问题与知识库无关（纯闲聊） |

**关键设计**：没有独立的 chitchat 检测节点，而是通过标签匹配结果隐式决定——纯闲聊（无标签+不skip）直接走兜底话术，避免浪费检索和生成资源。

### 4. FAQ 快速匹配

#### 4.1. BM25 + IDF 加权匹配

`node_faq_match` 使用 BM25Plus 算法匹配预设的 FAQ 问答对，命中则直接返回答案，跳过检索和生成。

**索引构建**（`_load_faq`，进程内缓存 + 双重检查锁）：
1. 从 PG 加载全部 FAQ Q&A
2. 从 KB 全量表加载 `parent_content` 构建 IDF
3. `_FaqBM25(BM25Plus)`：继承 BM25Plus，**用 KB 语料 IDF 覆盖 FAQ 自身 IDF** — 使 FAQ 匹配的词频权重基于全局语料而非仅 FAQ 内部

#### 4.2. 双阈值命中

```python
tokens = jieba.cut(query)          # 分词
scores = bm25.get_scores(tokens)   # BM25 得分
ranked = sorted(enumerate(scores), reverse=True)

# 绝对相关度：查询词的 IDF 加权覆盖率
coverage = hit_weight / total_weight

# 相对区分度：TOP1 / (TOP1 + TOP2)
ratio = top1_score / (top1_score + top2_score)

# 双阈值命中
if coverage >= FAQ_COVERAGE_THRESHOLD and ratio >= FAQ_RATIO_THRESHOLD:
    return FAQ答案   # mode=faq → END
else:
    → node_retrieve  # mode=rag，继续走检索
```

**设计要点**：双阈值兼顾"问题与 FAQ 的绝对相似度"和"与其他问题的区分度"，避免仅因词频碰巧高就误命中。

### 5. 混合检索

`retrieval_rerank.py` 的 `PgHybridRetriever` 实现向量+关键词双塔检索 → RRF 融合 → Rerank 精排 → 断崖检测动态 Top-K。

#### 5.1. 配置参数

| 参数 | 默认值 | 含义 |
|---|---|---|
| `SEARCH_VECTOR_WEIGHT` | 0.6 | 向量语义检索权重 |
| `SEARCH_KEYWORD_WEIGHT` | 0.4 | 关键词检索权重 |
| `SEARCH_CANDIDATE_TOP_K` | 10 | 向量/关键词各自初选候选数 |
| `SEARCH_COARSE_TOP_K` | 10 | RRF 融合后粗排条数（送入 reranker） |
| `SEARCH_RERANK_TOP_K` | 3 | rerank 后最终返回条数（fixed 模式） |
| `SEARCH_RRF_K` | 60 | RRF 融合常数（越大排名差异影响越小） |
| `SEARCH_DYNAMIC_TOPK` | True | 启用动态 top-K |
| `SEARCH_FINAL_MIN_TOP_K` | 2 | 动态 top-K 最少保留 |
| `SEARCH_FINAL_MAX_TOP_K` | 15 | 动态 top-K 最多保留 |
| `SEARCH_RELATIVE_SCORE_RATIO` | 0.25 | 相对分数兜底比例 |
| `SEARCH_GAP_RATIO` | 1.5 | 断崖判定倍率 |

#### 5.2. 向量塔检索（_vector_search）

**SQL**：

```sql
SELECT c.chunk_index, c.child_content, c.file_path, c.file_name, c.text_hash,
       c.metadata, c.group_id, c.internal_id, c.parent_content, c.related_image_url,
       1.0 - (c.embedding <=> %(query_embedding)s) AS similarity,
       c.upload_time, c.update_time, c.expire_time
FROM myagent_kb_chunks c
WHERE c.embedding IS NOT NULL
  AND (c.expire_time IS NULL OR c.expire_time > %(query_time)s)
  AND c.uid IN (%(uid)s, %(admin_uid)s)
  AND c.metadata->'tags' ?| %(target_tags)s   -- 标签过滤（可选）
ORDER BY c.embedding <=> %(query_embedding)s, c.update_time DESC NULLS LAST
LIMIT %(candidate_top_k)s
```

**关键点**：
- `<=>` 是 pgvector 余弦距离运算符，`similarity = 1.0 - distance`
- 排序：余弦距离 ASC + 更新时间 DESC
- 标签过滤：`metadata->'tags' ?| array` — JSONB 数组任一匹配
- `admin_uid` 共享：所有用户检索结果包含 admin 上传的文件
- `skip_tag_filter` 时跳过标签过滤，权重调整为 `vector:0.3 / keyword:0.7`（偏向关键词精确匹配）

#### 5.3. 关键词塔检索（_keyword_search）

**SQL**：

```sql
SELECT c.chunk_index, c.child_content, c.file_path, c.file_name, c.text_hash,
       c.metadata, c.group_id, c.internal_id, c.parent_content, c.related_image_url,
       ts_rank(c.content_tsv, to_tsquery('simple', %(tsquery)s)) AS similarity,
       c.upload_time, c.update_time, c.expire_time
FROM myagent_kb_chunks c
WHERE c.content_tsv @@ to_tsquery('simple', %(tsquery)s)
  AND (c.expire_time IS NULL OR c.expire_time > %(query_time)s)
  AND c.uid IN (%(uid)s, %(admin_uid)s)
  AND c.metadata->'tags' ?| %(target_tags)s   -- 标签过滤（可选）
ORDER BY ts_rank(c.content_tsv, to_tsquery('simple', %(tsquery)s)) DESC
LIMIT %(candidate_top_k)s
```

**关键点**：
- `content_tsv` 是入库时用 `to_tsvector('simple', tokenize_for_tsv(text))` 构建的全文检索向量
- `to_tsquery('simple', ...)` 用 `simple` 配置（不二次分词），由 `tokenize_for_tsquery` 生成 `词1 | 词2 | 词3` 格式
- `@@` 是全文检索匹配运算符，任一词匹配即命中
- `ts_rank` 按词频排序

#### 5.4. RRF 融合（_rrf_fuse）

**RRF (Reciprocal Rank Fusion) 公式**：

```
score(chunk) = Σ (weight_i / (rrf_k + rank_i(chunk)))
```

**具体实现**：

```python
# 向量塔: vector_weight=0.6, rrf_k=60
# 关键词塔: keyword_weight=0.4, rrf_k=60

for chunk_index in all_chunk_indices:
    score = 0.0
    if chunk_index in vector_rank:       # 1-based rank
        score += 0.6 / (60 + vector_rank[chunk_index])
    if chunk_index in keyword_rank:      # 1-based rank
        score += 0.4 / (60 + keyword_rank[chunk_index])
    merged.append((chunk_index, score))

merged.sort(key=lambda x: x[1], reverse=True)
merged = merged[:coarse_top_k]  # 取 top-coarse_top_k 送入 Rerank
```

#### 5.5. Rerank 精排

- **API**：阿里云 DashScope 原生 rerank API（`/api/v1/services/rerank/text-rerank/text-rerank`）
- **请求**：`{model, input: {query, documents}, parameters: {top_n, return_documents: false}}`
- **返回**：`{index: relevance_score}` 映射
- **中英混合前缀**：rerank_query 加前缀 `[Bilingual context: Chinese and English content may coexist]`，提升中英混合文档的打分质量
- **跳过条件**：候选 ≤3 条时跳过 Rerank，直接用 RRF 分数

#### 5.6. 混合排序

Rerank 完成后，用混合分数做最终排序：

```python
RERANK_WEIGHT = 0.7
VECTOR_WEIGHT = 0.3

hybrid_score = 0.7 * rerank_score + 0.3 * vector_similarity
```

**设计意图**：当 Rerank 对中英混合文档打分偏低时，向量相似度能拉回语义相关结果。

#### 5.7. 断崖检测动态 Top-K（_cliff_select）

不固定返回 N 条结果，而是根据 Rerank 分数的分布自动截断：

```
scores = [0.91, 0.88, 0.86, 0.84, 0.51, 0.49]
gaps   = [0.03, 0.02, 0.02, 0.33, 0.02]
                                  ↑ 最大间隙（断崖）
```

**算法流程**：

1. **分数平坦检测**：`max - min <= min_gap` → 取 `fallback_k` 条
2. **断崖检测**：
   - 计算相邻分数间隙 `gaps`
   - 找最大间隙位置 `max_idx`
   - 截断位置 `pos = max_idx + 1`
   - **触发条件**（需同时满足）：
     1. `max_gap > SEARCH_MIN_GAP (1e-5)`
     2. `pos >= min_k (2)`
     3. `avg_other <= min_gap` 或 `max_gap >= avg_other * gap_ratio (1.5)`
   - 触发 → 返回前 `pos` 条（限制在 `[min_k=2, max_k=15]`）
3. **相对分数兜底**：保留 `score >= top_score * 0.25` 的所有结果
4. **最终兜底**：取 `fallback_k` 条

#### 5.8. Top-K 的 4 层控制与断崖检测的配合

从 LLM 规划到最终返回，Top-K 经过 4 层逐步变换，planned_top_k 与断崖检测之间是**间接配合**而非直接约束关系。

**第 1 层：LLM 规划（node_plan_retrieval）**

LLM 根据问题难度评估输出 `planned_top_k`，代码 clamp 到 `[2, 15]`（`generate_answer.py:346`）：

| 难度 | 含义 | planned_top_k 范围 |
|---|---|---|
| simple | 单一事实查询 | 3-5 |
| moderate | 需上下文关联 | 5-8 |
| complex | 多条件/多步骤 | 8-12 |

异常兜底为 5。无文件时为 5（但不会走到检索）。

**第 2 层：策略放大（do_retrieval）**

`planned_top_k` 映射为 `rerank_top_k`，同时根据 `search_strategy` 放大候选池（`generate_answer.py:723-738`）：

| 策略 | candidate_top_k（双塔各取） | coarse_top_k（送入 Rerank） | rerank_top_k |
|---|---|---|---|
| broad | `max(15, top_k*2)` | `max(12, top_k*2)` | planned_top_k |
| precise | `max(8, top_k)` | `max(8, top_k)` | planned_top_k |
| direct | 默认 10 | 默认 10 | planned_top_k |

举例：`planned_top_k=8, strategy=broad` → `rerank_top_k=8, candidate_top_k=16, coarse_top_k=16`

**第 3 层：候选池修正（retriever.search）**

```python
candidate_top_k = max(candidate_top_k, coarse_top_k, rerank_top_k, SEARCH_FINAL_MAX_TOP_K)
# 即 candidate_top_k 至少为 15
```

向量塔和关键词塔各自取 `candidate_top_k` 条，RRF 融合后取 `coarse_top_k` 条送入 Rerank。

**第 4 层：断崖检测（_cliff_select）**

Rerank 后混合排序（`0.7*rerank + 0.3*vector`），然后断崖检测决定最终返回条数。

**关键发现**：当前 `SEARCH_DYNAMIC_TOPK=True`，`_rerank_select` 调用 `_cliff_select(results)` 时**没有传入 `rerank_top_k`**（`retrieval_rerank.py:718`），全部使用默认常量：

```
min_k=2, max_k=15, fallback_k=3, ratio=0.25, min_gap=1e-5, gap_ratio=1.5
```

这意味着 **planned_top_k 不直接约束最终返回条数**，两者的配合关系是间接的：

```
planned_top_k (LLM预估"要几条")
      │
      ↓ 通过 strategy 放大候选池
candidate_top_k / coarse_top_k ("准备多大候选池")
      │
      ↓ Rerank 打分 + 混合排序
断崖检测 (按分数分布裁剪，min=2 max=15)
      │
      ↓
最终返回条数 (由分数断崖决定，不由 planned_top_k 决定)
```

- **planned_top_k 的实际作用**：控制候选池大小——复杂问题 LLM 会规划更大的 top_k，strategy 放大后送入 Rerank 的候选更多，断崖检测有更多样本可分析
- **断崖检测的实际作用**：数据驱动裁剪——如果前 4 条分数 0.9+、第 5 条突然跌到 0.5，就在第 4 条截断，不管 LLM 规划的是 8 还是 12
- **设计意图**：LLM 无法预知检索结果的质量分布，所以让 LLM 负责"多准备候选"，让断崖检测负责"根据实际质量裁剪"，比"LLM 说要几条就返回几条"更可靠

**一个值得注意的细节**：如果断崖检测既没触发断崖、也没触发相对分数兜底（分数平坦的场景），fallback 返回 `fallback_k=3` 条（固定常量），而不是 `planned_top_k` 条。也就是说 LLM 规划了 top_k=10 但分数都差不多时，最终只返回 3 条。

**如果关闭动态 Top-K**（`SEARCH_DYNAMIC_TOPK=False`），第 4 层退化为固定截断 `results[:rerank_top_k]`，planned_top_k 直接决定返回条数。

#### 5.9. 完整流程

```
node_retrieve → do_retrieval
  │
  ├── 根据 search_strategy 调整参数:
  │     broad:   candidate_top_k=max(15, top_k*2), coarse_top_k=max(12, top_k*2)
  │     precise: candidate_top_k=max(8, top_k),    coarse_top_k=max(8, top_k)
  │     direct:  默认参数
  │
  ├── skip_tag_filter 时: effective_tags=None, vector:0.3/keyword:0.7
  │
  ▼
PgHybridRetriever.search()
  │
  ├── 1. Embedding 向量化: query_text → query_embedding (4096维)
  │
  ├── 2. 双塔粗排 (并行):
  │     ├── 向量塔: pgvector <=> top-K → vector_hits
  │     └── 关键词塔: to_tsquery @@ content_tsv top-K → keyword_hits
  │
  ├── 3. RRF 融合: weight/(60+rank) → top-coarse_top_k
  │
  ├── 4. Rerank 精排: DashScope API → relevance_score
  │     (候选≤3条时跳过)
  │
  ├── 5. 混合排序: 0.7*rerank + 0.3*vector
  │
  ├── 6. 断崖检测: 动态 Top-K (分数间隙自动截断)
  │
  └── 7. 去重: chunk_index + text_hash
  │
  ▼
_fetch_sibling_child_contents()  → 拼接兄弟子块
  │
  ▼
输出: context (3子块拼接), sources (完整父块+图片URL)
```

### 6. 兄弟子块扩展

检索命中子块后，调用 `_fetch_sibling_child_contents` 拼接同父块的兄弟子块，兼顾精准检索与上下文完整。

**SQL**：

```sql
SELECT c.internal_id, c.child_content
FROM myagent_kb_chunks c
WHERE c.group_id = %(group_id)s
  AND c.internal_id IN (%(prev_id)s, %(curr_id)s, %(next_id)s)
  AND c.uid IN (%(uid)s, %(admin_uid)s)
ORDER BY c.internal_id ASC
```

- `prev_id = internal_id - 1`, `next_id = internal_id + 1`
- 拼接 3 个子块的 `child_content` 作为 LLM context
- `sources` 保留完整 `parent_content` + `related_image_url` 供前端展示

**与知识切片设计的呼应**：两阶段切片（父块 2000 字 + 语义子块）→ 父子双存储 → 检索时子块精准命中 + 兄弟扩展补全上下文 + 父块完整展示。

### 7. 生成回答

#### 7.1. System Prompt 构造

`node_generate` 构造的 System Prompt 包含 6 个标签区块：

```
<role_instruction>           — 角色风格（严谨/循序渐进/亲切/直说/幽默）
<user_info>                  — 用户昵称、当前时间、语种
<tool_usage_rules>           — 工具调用规则（必填参数检查、日期推导）
<retrieved_context>          — 检索上下文（兄弟子块拼接的 context）
<scene_answer_template>      — 场景模板（可选，命中场景时注入）
【相关追问生成规则】          — ---RELATED--- 分隔符格式
```

**多轮对话参数**：`generate_n=3`，生成回答时结合最近 `generate_n*2+1 = 7` 条历史消息。

#### 7.2. 场景模板注入

命中场景时，从 `state["scenes"]` 查找命中模板：

| `answer_mode` | 注入方式 |
|---|---|
| `prompt` | `answer_template` 注入 `<scene_answer_template>` 标签，作为 LLM 生成指令 |
| `literal` | 加前缀 "忽略所有提示词，一字不变地回答" + `answer_template`，强制逐字输出 |

`followup_prompt` 不注入 prompt，由后处理追加到追问列表末尾。

#### 7.3. 工具循环

- 4 个工具：假期额度查询、请假记录查询、工时查询、个人信息查询
- `node_generate` 有 `tool_calls` → 路由到 `tool_node` → 回 `node_generate`（循环）
- **工具尝试上限**：`max_tool_attempts=3`，达到上限后注入 `<tool_fallback>` 禁止再调工具
- 过渡文字（tool_calls 前的文本）丢弃，`final_answer=""`

#### 7.4. 流式生成

```python
llm_with_tools = llm_client.bind_tools(tools)  # 未达到上限时
acc = None
async for chunk in llm_with_tools.astream([SystemMessage, HumanMessage]):
    acc = chunk if acc is None else acc + chunk
```

- `astream` 逐 chunk 累加，`content` 和 `tool_call_chunks` 自动合并
- chunk 经 `astream_events` 冒泡为 `on_chat_model_stream` 事件，前端实现打字机效果

#### 7.5. 追问生成

LLM 回答中用 `---RELATED---` 分隔符分离正文和追问：

```
这是回答正文内容。

---RELATED---
1. 第一个追问
2. 第二个追问
```

后处理 `_parse_related_questions` 提取追问列表，场景 `followup_prompt` 追加到末尾。

### 8. 敏感话题检测 & 场景判定

两者都采用**双层判断机制**，在主图启动前并行执行：

#### 8.1. 敏感话题检测（_judge_sensitive）

| 层次 | 机制 | 阈值/超时 |
|---|---|---|
| 第一层 | Jev (TypeSafe) Noul | `noul > 0.7` → hit，超时 15s |
| 第二层 | LLM 兜底（Jev 异常时降级） | 返回 `{"result": "hit"}` 或 `miss`，超时 10s，超时按 miss 放行 |

**敏感话题范围**：政治敏感、赌博、毒品药物、色情、暴力恐怖、诈骗、违法活动、侵犯隐私、黑客攻击

命中敏感话题 → 跳过主图，直接返回固定话术。

#### 8.2. 场景判定（_judge_scene）

| 层次 | 机制 | 阈值/超时 |
|---|---|---|
| 第一层 | Jev Choice + Noul | `has_match.noul > 0.7` 且 choice 在有效场景 ID 集合中 |
| 第二层 | LLM 兜底 | 返回 `{"scene": "场景id"}` 或 `{"scene": "none"}` |

命中场景 → 注入 `initial_state["scene"]`，后续 `node_generate` 消费模板。

### 9. 流式输出 & SSE

#### 9.1. SSE 事件类型

| type | 含义 | 数据 |
|---|---|---|
| `start` | 对话开始 | session_id, turn_count, tips |
| `thinking` | 节点执行中 | hint（提示文案，如"正在检索知识库"） |
| `content` | LLM 流式文本 | content（单个 chunk） |
| `tool_query` | 工具调用中 | tool, hint |
| `done` | 回答完成 | sources, related_questions |

#### 9.2. 架构

- **事件队列**：`asyncio.Queue` 作为主图事件循环和 SSE 响应体之间的缓冲
- **主图事件循环**：`graph.astream_events(initial_state, config, version="v2")`，捕获 `on_chain_start` / `on_chat_model_stream` / `on_chain_end` 三类事件
- **并行判定**：`_judge_sensitive` 和 `_judge_scene` 与主图并行执行，不阻塞检索
- **超时机制**：60s 无事件 → 强制中断 + 发 `FALLBACK_TIMEOUT`

### 10. 多轮对话 & Session 管理

#### 10.1. 多轮对话

| 机制 | 说明 |
|---|---|
| **Checkpoint 持久化** | `PostgresSaver` 自动保存/恢复 `messages` 列表 |
| **消息追加** | `Annotated[list, add_messages]` 使新消息追加而非覆盖 |
| **历史窗口（规划）** | `rewrite_n=4`，检索规划取最近 `rewrite_n*2+1 = 9` 条 |
| **历史窗口（生成）** | `generate_n=3`，生成回答取最近 `generate_n*2+1 = 7` 条 |
| **历史清洗** | `_clean_history_messages` 只保留 HumanMessage 和 AIMessage（无 tool_calls） |

#### 10.2. Session 管理

| 机制 | 说明 |
|---|---|
| **每用户最多 10 个 session** | 超限软删最旧 session + 清 checkpoint |
| **首轮自动标题** | Fast LLM 生成 10 字标题 |
| **Session 表** | `myagent_sessions`（uid, session_id, title, last_message_at, turn_count, deleted） |
| **对话持久化** | `myagent_conversations_history` 表，每轮写 2 行（user + assistant），assistant 行含 sources、related_questions、duration_ms、token 统计 |

### 11. 预热机制

FastAPI startup 时预热所有懒加载资源，确保首个请求零冷启动。

| 层次 | 策略 | 预热对象 |
|---|---|---|
| 第一层 | 同步实例化 | LLM 单例、Jev 客户端、PG 连接池、BM25 索引 |
| 第二层 | 异步建连 | Embedding API、Rerank API、LLM API（TCP/TLS 连接） |
| 第三层 | 预构建索引 | FAQ BM25 索引（KB 语料 IDF） |

- 并发执行：`asyncio.gather(*tasks, return_exceptions=True)` — 单个失败不阻断
- 多 worker 各自独立预热

### 12. 核心设计理念

| 设计 | 说明 |
|---|---|
| **LangGraph 状态机编排** | 非线性流水线，条件路由 + 工具循环，节点间可跳转/回退/终止 |
| **标签驱动路由** | 检索规划的标签匹配结果决定走 FAQ/检索/兜底，隐式实现 chitchat vs rag 分流 |
| **双塔混合检索** | 向量塔(语义) + 关键词塔(精确) → RRF 融合，兼顾语义相似和关键词命中 |
| **断崖检测动态 Top-K** | 按 Rerank 分数间隙自动截断，不固定返回数，避免无关结果稀释上下文 |
| **兄弟子块扩展** | 命中 1 个子块 → context 拼接同父块前后兄弟(共 3 块)，兼顾精准与上下文完整 |
| **双层判断** | Jev TypeSafe(快) + LLM 兜底(可靠)，敏感检测和场景判定均采用此机制 |
| **场景模板注入** | literal 直出 vs prompt 引导，满足固定话术和灵活生成两种需求 |
| **admin 知识共享** | 所有用户检索结果含 admin 上传的文件，实现全局知识库共享 |
| **skip_tag_filter 权重调整** | 跳过标签过滤时偏向关键词检索(0.3:0.7)，术语更可能精确匹配 |
| **中英混合 Rerank 前缀** | 加 `[Bilingual context]` 前缀提升中英混合文档的 Rerank 打分质量 |

### 13. 连接池管理

检索环节涉及 PG 数据库、Embedding API、Rerank API、LLM API 等多个外部连接，均通过连接池或单例客户端管理。

#### 13.1. 连接池清单

| 连接对象 | 所在模块 | 类型 | 池大小配置 | 创建方式 | 关闭方式 |
|---|---|---|---|---|---|
| 检索 PG 池 | `retrieval_rerank.py` | psycopg ConnectionPool | `min=1, max=5` | 懒加载 `_get_pool()` | **无显式关闭** |
| Embedding HTTP 池 | `retrieval_rerank.py` | AsyncOpenAI + httpx.AsyncClient | `max_conn=100, keepalive=50` | 构造函数创建（随 retriever 实例化） | **无显式关闭** |
| Rerank HTTP 池 | `retrieval_rerank.py` | httpx.AsyncClient | `max_conn=100, keepalive=50` | 懒加载 `_get_client()` | **无显式关闭** |
| 主 LLM 客户端 | `generate_answer.py` | ChatOpenAI + httpx.Client | `max_conn=20, keepalive=10` | 全局单例 `get_llm_client()` | **无显式关闭** |
| 快速 LLM 客户端 | `generate_answer.py` | ChatOpenAI + httpx.Client | `max_conn=20, keepalive=10` | 全局单例 `get_fast_llm_client()` | **无显式关闭** |
| Jev (TypeSafe) 客户端 | `generate_answer.py` | TypeSafeClient | SDK 内部管理 | 全局单例 `get_jev_client()` | **无显式关闭** |
| 对话持久化 PG 池 | `generate_answer.py` | psycopg ConnectionPool | `min=1, max=5` | 懒加载 `_get_pg_pool()` | **无显式关闭** |
| Checkpoint PG 池 | `generate_answer.py` | psycopg ConnectionPool | `min=1, max=10` | 模块加载时立即创建 | `close_checkpointer()`（app.py shutdown） |
| 打标 LLM 客户端 | `file_tagging.py` | ChatOpenAI + httpx.Client | `max_conn=20, keepalive=10` | 全局单例（独立实例） | **无显式关闭** |
| 打标 PG 池 | `file_tagging.py` | psycopg ConnectionPool | `min=1, max=5` | 懒加载（独立实例） | **无显式关闭** |
| 场景模板 PG 连接 | `scene_template_mgmt.py` | psycopg.connect（**裸连接**） | **无连接池** | 每次 API 请求创建 | `with` 语句自动关闭 |

#### 13.2. 预热机制（warmup.py）

FastAPI startup 时预热所有懒加载资源，确保首个请求零冷启动：

| 层次 | 策略 | 预热对象 |
|---|---|---|
| 第一层 | 同步实例化 | LLM 单例、Jev 客户端、检索器 PG 池、入库 PG 池、Redis 池、BM25 索引 |
| 第二层 | 异步建连 | Embedding API、Rerank API、主/快速 LLM API（TCP/TLS 连接） |

- 并发执行：`asyncio.gather(*tasks, return_exceptions=True)` — 单个失败不阻断
- **未预热的连接**：对话持久化 PG 池（首次对话请求时懒加载）、打标 PG 池（首次打标时懒加载）、场景模板 PG 连接（每次请求裸连接）

#### 13.3. 存在的问题

1. **场景模板模块使用裸连接**：`scene_template_mgmt.py` 的 `_pg_conn()` 每次调用 `psycopg.connect()` 创建新连接，无连接池，频繁建连开销大。该模块用于 CRUD 场景模板（低频操作），实际压力不大，但建议改为连接池。

2. **重复的连接池实例**：`file_tagging.py` 和 `generate_answer.py` 各自创建了独立的 Fast LLM 客户端和 PG 连接池，指向同一服务，未共享。共存在 2 个 Fast LLM 客户端 + 3 个 PG 连接池（检索/对话/打标），加上 Checkpoint 池共 4 个 PG 池。

3. **缺少显式关闭**：除 Checkpoint 池外，其余连接池/客户端均无显式关闭逻辑，依赖进程退出时 OS 回收。`retrieval_rerank.py` 中 `close()` 方法仅在 `__main__` 测试块中调用，生产代码未调用。

4. **Checkpointer 是唯一规范管理的连接池**：`_checkpointer_pool` 在模块加载时创建，`close_checkpointer()` 在 `app.py` shutdown 事件中调用，是唯一有完整生命周期的连接池。


### 13. 生成节点全流程 & 并行优化

#### 13.1. 时序总览

一次完整 RAG 对话从用户提问到回答结束，经历 4 个阶段：

```
                          ┌─ sens_task (~1.9s) ──┐
用户提问                   ├─ scene_task (~1.9s) ─┤  并行阶段
  │                       └─ plan_task (~4.1s) ──┘
  ▼                        │
stream_response()          ▼ (gather sens+scene 先完成, plan 最后完成)
  ├── 并行判定+规划          │
  │   (三者并行, ~4.1s)     ▼
  ├── 敏感命中? → 固定话术   │
  │   否 →                  ▼
  ├── graph.astream_events  │
  │   ├── node_plan_retrieval → 跳过(预计算) ── 0ms
  │   ├── route_plan_retrieval               ── ~0ms
  │   ├── node_faq_match / node_retrieve     ── ~5.7s (embedding+rerank)
  │   ├── route_retrieve_result              ── ~0ms
  │   └── node_generate (流式)               ── ~12s (LLM 生成)
  │       └── [有 tool_calls] → tool_node → node_generate (循环)
  └── done

总计: ~4.1s + ~5.7s + ~12s ≈ 21.8s (优化前: ~1.9s + ~4.1s + ~5.7s + ~12s ≈ 23.7s)
```

#### 13.2. 并行优化原理

`stream_response()` 中，三个独立任务无依赖关系，可安全并行：

| 任务 | 模型 | 耗时 | 依赖 |
|---|---|---|---|
| `_judge_sensitive` | Jev / Fast LLM | ~1.9s | 仅依赖 message |
| `_judge_scene` | Jev / Fast LLM | ~1.9s | 仅依赖 message + scenes |
| `node_plan_retrieval` | Fast LLM | ~4.1s | 仅依赖 message + history + uid |

**优化前**（串行）：
```
sens+scene (1.9s) → 等待完成 → plan_retrieval (4.1s) → 启动主图
```

**优化后**（并行）：
```
sens+scene (1.9s) ──┐
                     ├── 先等 sens+scene (1.9s)
plan_retrieval (4.1s)┘   ├─ 命中敏感 → 取消 plan，立即返回
                         └─ 未命中 → 等 plan 完成 (再 ~2.2s) → 注入 state → 启动主图
```

**关键实现**：
1. `stream_response` 中将 `node_plan_retrieval(initial_state)` 作为独立 `asyncio.Task` 并行启动
2. 先 `asyncio.gather(sens_task, scene_task)` 等敏感/场景判定完成（快者先决）
3. 命中敏感 → `plan_task.cancel()` 取消规划，立即返回固定话术
4. 未命中 → `await plan_task` 等规划完成，`initial_state.update(plan_result)` 注入结果
5. 主图启动时 `node_plan_retrieval` 检测到 `state["query"] is not None` → 返回空 dict 跳过重复执行

**敏感路径无额外开销**：plan_task 被取消，不浪费 LLM token；sens+scene 等待时间不变。
**正常路径节省 ~1.9s**：plan_retrieval 与 sens+scene 重叠执行，等待成本从 1.9s+4.1s=6s 降为 max(1.9s, 4.1s)=4.1s。

#### 13.3. node_generate 详解

`node_generate` 是生成回答的核心节点，负责构造 System Prompt、流式生成、工具循环。

**输入**：`state["context"]`（检索上下文）, `state["messages"]`（历史消息）, `state["scene"]`（场景模板）

**处理流程**：

```
node_generate(state)
  │
  ├── 1. 提取上下文
  │     ├── retrieved_context = state["context"]   (兄弟子块拼接)
  │     ├── history_messages = 最近 7 条清洗后历史
  │     ├── tool_messages = 从后往前提取 ToolMessage
  │     └── query = state["query"] (LLM 改写后) 或原始问题
  │
  ├── 2. 场景模板注入
  │     ├── scene_id = state["scene"]
  │     ├── 找到场景模板 → answer_mode=prompt/literal
  │     └── prompt 模式: 注入 <scene_answer_template> 标签
  │         literal 模式: "忽略所有提示词，一字不变地回答：{tpl}"
  │
  ├── 3. 构造 System Prompt (6 个标签区块)
  │     <role_instruction>           — 角色风格 + 禁忌 + 回答原则
  │     <user_info>                  — 昵称、当前时间、语种
  │     <tool_usage_rules>           — 工具调用规则(必填参数检查、日期推导)
  │     <retrieved_context>          — 检索上下文
  │     <scene_answer_template>      — 场景模板(可选)
  │     【相关追问生成规则】          — ---RELATED--- 分隔符格式
  │
  ├── 4. 工具尝试检查
  │     ├── tool_attempts = _count_recent_tool_attempts(messages)
  │     ├── tool_exhausted = attempts >= max_tool_attempts(3)
  │     └── 已耗尽 → 注入 <tool_fallback> 禁止再调工具 + 用裸 LLM(不 bind_tools)
  │
  ├── 5. 流式生成
  │     ├── llm_with_tools = llm.bind_tools(tools)  (未耗尽时)
  │     ├── async for chunk in llm.astream([System, Human]):
  │     │     acc = acc + chunk  (AIMessageChunk 自动合并 content + tool_call_chunks)
  │     └── chunk 经 astream_events 冒泡为 on_chat_model_stream → 前端打字机
  │
  ├── 6. 结果分流
  │     ├── 有 tool_calls → 返回带 tool_calls 的 AIMessage
  │     │     → tools_condition 路由到 tool_node → 回 node_generate (循环)
  │     │     → 过渡文字丢弃 (final_answer="")
  │     └── 无 tool_calls → 返回纯文本 AIMessage
  │         → tools_condition 路由到 END
  │         → content 已在流式过程中发给前端
  │
  └── 7. Token 提取 (在 stream_response 的 on_chain_end 事件中)
        ├── response_metadata.token_usage (非流式)
        ├── usage_metadata (流式累加)
        └── model_name 从 response_metadata 提取 → token_acc.add()
```

#### 13.4. 工具循环机制

```
node_generate (pass 1)
  │ LLM 输出: AIMessage(content="让我查一下", tool_calls=[{name: "query_leave_quota", args: {...}}])
  │
  ▼ tools_condition 检测到 tool_calls → 路由到 tool_node
  │
tool_node
  │ 执行工具: query_leave_quota(uid, start_date, end_date) → ToolMessage(content="年假余额: 5天")
  │
  ▼ add_edge("tool_node", "node_generate") → 回到 node_generate
  │
node_generate (pass 2)
  │ 历史消息现在含 ToolMessage → tool_text 提取工具结果注入 prompt
  │ LLM 输出: AIMessage(content="您的年假余额是5天...", tool_calls=None)
  │
  ▼ tools_condition 无 tool_calls → 路由到 END
```

**保护机制**：`max_tool_attempts=3`，达到上限后注入 `<tool_fallback>` 禁止再调工具，强制用自然语言兜底。

#### 13.5. Token 统计与多模型处理

一次对话可能涉及多个 LLM 模型（Fast Model 规划 + Text Model 生成），Token 按模型分别记录：

```
TokenAccumulator (contextvars 隔离, 每请求独立)
  │
  ├── node_plan_retrieval → log_token_usage("node_plan_retrieval", response, FAST_MODEL)
  │   → token_acc.add("qwen3.8-flash", prompt=X, completion=Y, total=Z)
  │
  ├── node_generate → 手动提取 model_name + token_usage
  │   → token_acc.add("qwen3.32b", prompt=X, completion=Y, total=Z)
  │
  └── _persist_turn 落库:
      ├── primary_model = token_acc.primary_model (token 最多的模型)
      ├── 主记录: primary_model 的 token + 完整回答内容
      └── extra_model_tokens: 其他模型仅写 token-only 记录 (content 为空)
```

**数据库表** `myagent_conversations_history`：
- 主记录: `model_name` 有值, `content` 有回答, `prompt_tokens`/`completion_tokens`/`total_tokens` 有值
- 额外模型记录: `model_name` 有值, `content` 为空, 仅 token 列有值
- `get_model_token_breakdown`: `GROUP BY TRIM(model_name)` 统计各模型 token 总量

### 14. 连接池分析

#### 14.1. 连接池清单

| 连接对象 | 所在模块 | 连接到 | 池配置 | 模式 | 用途 |
|---|---|---|---|---|---|
| LLM 主客户端 | `generate_answer.py:73` | LLM API (BASE_URL) | 20 conn / 10 keepalive | sync+async | 主模型生成 (node_generate) |
| LLM 快客户端 | `generate_answer.py:92` | LLM API (BASE_URL) | 20 conn / 10 keepalive | sync+async | 快模型规划 (node_plan_retrieval) |
| Jev 客户端 | `generate_answer.py:111` | TypeSafe API | 内部 httpx | async | 敏感/场景判定 |
| Embedding 客户端 | `retrieval_rerank.py:195` | Embedding API | 100 conn / 50 keepalive | async | 查询向量化 |
| Rerank 客户端 | `retrieval_rerank.py:255` | DashScope API | 100 conn / 50 keepalive | async | 检索精排 |
| Checkpointer 池 | `generate_answer.py:1135` | PostgreSQL | 1~10 conn | sync | LangGraph checkpoint |
| 业务 PG 池 | `psycopg_pool` (全局) | PostgreSQL | 1~5 conn | sync | 对话/Session/数据读写 |

#### 14.2. 优化项

**已优化**：LLM 客户端添加 `http_async_client`

```
优化前: ChatOpenAI(http_client=httpx.Client(...))
  → ainvoke/astream 使用默认 httpx.AsyncClient (无连接池限制, 每次新建连接)

优化后: ChatOpenAI(
    http_client=httpx.Client(...),          # sync 调用用
    http_async_client=httpx.AsyncClient(...), # async 调用用 (ainvoke/astream)
)
  → async 调用复用 keepalive 连接, 避免每次 TCP/TLS 握手
  → 连接池 20 conn / 10 keepalive, 与 sync 客户端对称
```

**效果**：
- `node_plan_retrieval` 的 `llm.ainvoke()` → 复用 async keepalive 连接
- `node_generate` 的 `llm.astream()` → 复用 async keepalive 连接
- 首次请求后 TCP/TLS 连接保持, 后续请求省去握手开销 (~200ms/次)

#### 14.3. 当前池大小评估

| 客户端 | 当前池 | 并发场景 | 评估 |
|---|---|---|---|
| LLM 主/快 (各 20 conn) | 20/10 | 每用户 1 轮对话占 2 连接 (plan+generate) | 10 并发用户够用 |
| Embedding (100 conn) | 100/50 | 每次检索 1 连接 | 远超需求 |
| Rerank (100 conn) | 100/50 | 每次检索 1 连接 | 远超需求 |
| Checkpointer (10 conn) | 1~10 | 每轮对话 2 次 (读+写) | 够用 |
| 业务 PG (5 conn) | 1~5 | 每轮对话 3~4 次 (session+history) | 可能偏紧, 高并发时可调大 |

**结论**：当前池大小对小微商家场景（个位数并发）充足，无需调整。如未来并发增长，优先调大业务 PG 池 (5→10) 和 LLM 池 (20→50)。

#### 14.4. 连接池共享分析

```
LLM API (BASE_URL)
  ├── get_llm_client()      → httpx.Client (sync) + httpx.AsyncClient (async)  ← 独立池
  └── get_fast_llm_client() → httpx.Client (sync) + httpx.AsyncClient (async)  ← 独立池
```

主模型和快模型虽然连接同一个 BASE_URL，但各自维护独立的连接池。这意味着：
- 优势：互不阻塞，plan_retrieval 和 generate 各有专属连接
- 劣势：无法共享 keepalive 连接，理论上最多浪费 10 条 keepalive 连接
- **结论**：不值得合并——两者使用频率和并发模式不同，独立池避免互相影响更可靠

Embedding 和 Rerank 客户端连接不同 API 端点，各自独立池是合理的。


---

## §15 标签分布 Decimal 序列化问题与修复

### 问题现象

前端数据监控页面报错：`加载失败：Object of type Decimal is not JSON serializable`，标签分布看板无内容。

### 根因分析

PostgreSQL 聚合函数的返回类型规则：

| SQL 聚合函数 | 输入类型 | 返回类型 | psycopg3 Python 映射 |
|---|---|---|---|
| `COUNT(*)` | — | `bigint` | `int` |
| `SUM(bigint)` | `bigint` | `numeric` | **`Decimal`** |
| `AVG(int)` | `int` | `numeric` | **`Decimal`** |
| `ROUND(numeric, n)` | `numeric` | `numeric` | **`Decimal`** |

`get_tag_distribution` 改为 `UNION ALL` 子查询后，内层 `COUNT(*) AS cnt` 返回 `bigint`（→ Python `int`），外层 `SUM(cnt)` 对 `bigint` 求和，PostgreSQL 返回 `numeric` 类型，psycopg3 将其映射为 Python `Decimal`。

FastAPI 的 `JSONResponse` 默认使用 `json.dumps`，而 `json.dumps` 不支持 `Decimal` 类型序列化，导致整个 `/stats/overview` 接口 500 报错。

> **关键**：此错误发生在 FastAPI 响应序列化层（`JSONResponse.render()`），而非 endpoint 函数内部，因此 endpoint 的 `try/except` 无法捕获。

### 影响范围审计

对 `chat_report.py` 所有查询函数逐一检查 SUM/AVG 使用情况：

| 函数 | 聚合表达式 | 返回类型 | Python 转换 | 状态 |
|---|---|---|---|---|
| `get_total_stats` | `COALESCE(SUM(total_token), 0)` | `numeric` | `int(row[6] or 0)` | ✅ 安全 |
| `get_total_stats` | `AVG(duration_ms)` | `numeric` | `float(round(row[5], 1))` | ✅ 安全 |
| `get_mode_stats` | 仅 `COUNT(*)` | `bigint` | 无需转换 | ✅ 安全 |
| `get_daily_stats` | `COALESCE(SUM(total_token), 0)` | `numeric` | `int(row[4] or 0)` | ✅ 安全 |
| `get_daily_stats` | `ROUND(AVG(duration_ms), 1)` | `numeric` | `float(round(row[3], 1))` | ✅ 安全 |
| `get_trending_sessions` | 仅 `COUNT(*)`/`MAX` | `bigint` | 无需转换 | ✅ 安全 |
| `get_emotion_distribution` | 仅 `COUNT(*)` | `bigint` | 无需转换 | ✅ 安全 |
| **`get_tag_distribution`** | **`SUM(cnt)`** | **`numeric`** | **无** | **❌ BUG** |
| `get_negative_trend` | `COUNT(*) FILTER` | `bigint` | `int(row[1] or 0)` | ✅ 安全 |
| `get_model_token_breakdown` | `COALESCE(SUM(total_token), 0)` | `numeric` | `int(row[1])` | ✅ 安全 |
| `get_negative_feedback_stats` | 仅 `COUNT(*)` | `bigint` | 无需转换 | ✅ 安全 |
| `get_hourly_distribution` | 仅 `COUNT(*)` | `bigint` | 无需转换 | ✅ 安全 |
| `get_duration_trend` | `ROUND(AVG(duration_ms), 1)` | `numeric` | `float(row[1])` | ✅ 安全 |

**唯一问题点**：`get_tag_distribution` 第 346 行 `return [{"name": row[0], "value": row[1]} ...]`，`row[1]` 为 `Decimal` 未转换。

### 修复

```python
# 修复前
return [{"name": row[0], "value": row[1]} for row in rows]

# 修复后
return [{"name": row[0], "value": int(row[1])} for row in rows]
```

### 防御性建议

1. **SQL 层面**：涉及 `SUM` 的查询统一加 `::bigint` 转换，如 `SUM(cnt)::bigint AS total`，从源头避免 `numeric` 类型
2. **Python 层面**：所有从 psycopg 取出的聚合结果，显式用 `int()` 或 `float()` 包装，不依赖隐式类型推断
3. **全局兜底**（可选）：为 FastAPI 注册自定义 JSON 序列化器，将 `Decimal` 自动转为 `int`/`float`：
   ```python
   from decimal import Decimal
   from fastapi.responses import JSONResponse
   import json

   class DecimalJSONResponse(JSONResponse):
       def render(self, content) -> bytes:
           return json.dumps(content, ensure_ascii=False, default=lambda o: int(o) if isinstance(o, Decimal) else str(o)).encode()
   ```
   但此方案会掩盖类型问题，建议仅作为最后一道防线。

### 历史背景

旧版 `get_tag_distribution` 直接使用 `COUNT(*) AS count`（返回 `bigint` → `int`），不存在 Decimal 问题。改为 `UNION ALL` 统计空标签为"闲聊"后，外层引入 `SUM(cnt)` 才触发此问题——典型的"重构引入类型变更"陷阱。

---

## §16 用户画像系统

### 设计目标

从用户历史对话中自动提取画像（基础信息、性格、偏好、最近关注），定时更新，在生成回答时注入 LLM prompt 实现个性化回复。

### 数据存储

`myagent_user` 表新增 2 列：

| 列名 | 类型 | 说明 |
|---|---|---|
| `profile` | `JSONB` | 完整画像 JSON |
| `profile_updated_at` | `TIMESTAMPTZ` | 上次画像更新时间 |

画像 JSON 结构：

```json
{
  "basic_info": {
    "name": "张三",
    "region": "北京",
    "age_group": "青年",
    "life_stage": "育儿"
  },
  "personality": "沟通简洁直接，关注实操细节",
  "preferences": ["偏好中文回答", "喜欢步骤式指引"],
  "recent_topics": ["年假余额", "考勤补卡流程"],
  "communication_style": "提问简短，习惯追问细节"
}
```

### 定时更新机制

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `USER_PROFILE_INTERVAL_HOURS` | `12` | 后台循环周期（小时） |

- **后台 asyncio 任务**：app 启动时创建，每 `USER_PROFILE_INTERVAL_HOURS` 小时执行一次
- **扫描逻辑**：只处理 `approved=true, deleted=false` 且有自 `profile_updated_at` 之后新对话的用户
- **速率限制**：逐用户处理，每个用户间隔 1 秒
- **最小门槛**：用户消息总数 ≥ 5 条才开始生成画像
- **最大消息量**：每次提取最多喂 100 条用户消息，避免 token 溢出

### 增量更新流程

```
首次生成：
  拉取全部用户消息（最近 100 条）→ LLM 提取 → 写入 profile

后续更新：
  读取老画像 profile JSON
  + 拉取 profile_updated_at 之后的新用户消息
  → LLM 合并更新（保留有效旧信息 + 补充新信息）
  → 写入新 profile + 更新 profile_updated_at
```

LLM 使用 `FAST_MODEL`（小模型），`temperature=0.1`，输出纯 JSON。

### LLM 提取 Prompt 设计

```
你是一个用户画像分析助手。根据用户的历史对话记录，提取或更新用户画像。

<existing_profile>{老画像 JSON 或"无"}</existing_profile>
<recent_conversations>{对话列表}</recent_conversations>

规则：
1. 保留 existing_profile 中仍有效的信息
2. recent_topics 只保留最近 5-10 个，过时移除
3. 未提及的字段保留旧值
4. 不记录手机号/身份证等 PII
5. 返回纯 JSON
```

对话格式化为 `[MM-DD HH:mm] 用户消息内容`，仅取 `role='user'` 的消息（assistant 回复对画像提取价值不大）。

### 生成回答时的引用

`generate_answer.py` 的 `stream_response` 中，构建 `user_dict` 时读取 `profile`：

```python
from sso_did.user_profile import get_user_profile
profile = get_user_profile(uid)
if profile:
    user_dict["profile"] = profile
```

`node_generate` 的 `<user_info>` 块扩展为：

```
<user_info>
【用户信息】
- 昵称: 张三
- 当前时间: 2026-11-20 10:30:00
- 用户语种: zh
- 地区: 北京
- 年龄段: 青年
- 人生阶段: 育儿
- 性格特征: 沟通简洁直接，关注实操细节
- 偏好: 偏好中文回答, 喜欢步骤式指引
- 最近关注: 年假余额, 考勤补卡流程
- 沟通风格: 提问简短，习惯追问细节
</user_info>
```

`_render_profile()` 函数将 JSON 渲染为可读文本行，无画像时返回空字符串（不影响原有逻辑）。

### API 接口

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/profile/{uid}` | 管理员或本人 | 查看用户画像 |
| POST | `/api/v1/profile/{uid}/refresh` | 仅管理员 | 手动触发画像更新 |

### 文件清单

| 文件 | 改动 |
|---|---|
| `sso_did/user_profile.py` | 完整实现：PG 连接池、LLM 提取、定时循环、API 路由 |
| `calculate_logic/rag/generate_answer.py` | `_render_profile()` 函数、`stream_response` 读取 profile、`<user_info>` 展开画像 |
| `app/app.py` | 导入 `profile_router` / `start_profile_loop` / `stop_profile_loop` / `close_profile_pool`；注册路由；startup 启动循环；shutdown 停止循环+关闭连接池 |
| `table_data/create_all_tables.py` | `myagent_user` CREATE TABLE 加 `profile` + `profile_updated_at` 列 |
| `.env` / `.env.example` | `USER_PROFILE_INTERVAL_HOURS=12` |

### 防御性设计

- **画像读取失败不阻断对话**：`stream_response` 中 `get_user_profile` 包裹 try/except，失败时 `user_dict` 不含 `profile`，`<user_info>` 退化为原有行为
- **LLM 提取失败不覆盖老画像**：`_extract_profile` 返回 None 时跳过写入，保留旧画像
- **JSON 解析容错**：LLM 输出可能包含 markdown 代码块标记（\`\`\`json），提取时自动剥离
- **后台循环异常隔离**：单个用户更新失败不影响其他用户，循环本身异常不会崩溃

### 存量数据库迁移 SQL

```sql
ALTER TABLE myagent_user ADD COLUMN IF NOT EXISTS profile jsonb DEFAULT NULL;
ALTER TABLE myagent_user ADD COLUMN IF NOT EXISTS profile_updated_at timestamptz DEFAULT NULL;
```
