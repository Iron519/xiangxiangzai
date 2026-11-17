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

#### 1.7. 解析 - 编辑结果

用户是可以在前端页面手动编辑解析全文的，并且系统将直接将编辑后的文本写回 merged.md 文件（覆盖原文件）。

### 2. 知识切片















