import sys
import os
import re
import asyncio
import logging
import mimetypes
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv())

# ================= 路径配置及import内部功能 ===============
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from calculate_logic.rag.generate_answer import router as chat_router, close_checkpointer, preload_faq_index
from table_data.chat_tag import tag_loop
from calculate_logic.chunk_pipeline.routes import router as chunk_router
from table_data.chat_report import router as report_router
from calculate_logic.chunk_pipeline.routes import recover_queued_tasks
from sso_did.auth_api import router as auth_router
from sso_did.users_api import router as users_router
from sso_did.roles_api import router as roles_router
from sso_did.permissions_api import router as permissions_router
from sso_did.auth import get_current_user, TokenData
from calculate_logic.fetch_knowledge.faq_mgmt import router as faq_router
from calculate_logic.rag.scene_template_mgmt import router as scene_template_router

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "hrssc_main.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("HrsscMain")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    from concurrent_log_handler import ConcurrentRotatingFileHandler
    file_handler = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)

# ================= 创建 FastAPI 应用 =================
app = FastAPI(title="HRSSC")

# ================= CORS 配置 =================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(chunk_router)
app.include_router(report_router)
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(roles_router)
app.include_router(permissions_router)
app.include_router(faq_router)
app.include_router(scene_template_router)


# ================= DID 业务异常处理 =================
# ValueError → 返回 HTTP 200 但 body.code=400，对齐前端 getData 的 code==200 判定逻辑
@app.exception_handler(ValueError)
async def value_error_handler(request, exc: ValueError):
    logger.warning(f"[DID] Business error: {exc}")
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=200, content={"code": 400, "message": str(exc), "data": None})


# ================= 启动恢复：处理遗留的排队任务 =================
@app.on_event("startup")
async def _recover_queued_on_startup():
    """进程重启后，把上次崩溃遗留的排队任务重新推进。

    - 把仍处于“处理中”状态（saving/parsing/...）的任务标记为 failed（崩溃时未完成）；
    - 若有 queued 任务则触发一个，其结束后会链式推进后续。
    多 worker 启动时各 worker 都会执行，但 _claim_next_queued_task 用
    FOR UPDATE SKIP LOCKED 保证不会重复启动同一排队任务。
    """
    try:
        await recover_queued_tasks()
    except Exception as e:
        logger.warning(f"[Startup] 恢复排队任务失败: {e}")


@app.on_event("startup")
async def _preload_faq_on_startup():
    """启动预热 FAQ BM25 索引。

    每个 worker 进程独立持有 FAQ 缓存，全部需预构建，避免首个请求
    现场对 kb 全量语料分词算 IDF（同步执行会阻塞事件循环数秒）。
    同步构建，保证第一个请求到达时索引已就绪。
    """
    try:
        preload_faq_index()
    except Exception as e:
        logger.warning(f"[Startup] FAQ索引预热失败（首次请求时按需加载）: {e}")


@app.on_event("startup")
async def _start_chat_tag_loop():
    """启动定时打标后台任务，每 N 小时对未打标的 user 消息做业务标签+情绪标注。

    只在第一个 worker 上启动（用全局标记避免多 worker 重复）。
    """
    if not getattr(app, "_tag_loop_started", False):
        app._tag_loop_started = True
        asyncio.create_task(tag_loop())
        logger.info("[Startup] 定时打标任务已启动")


@app.on_event("shutdown")
async def _close_checkpointer_on_shutdown():
    """关闭 LangGraph checkpoint 的 PG 连接池，避免 worker 退出时连接泄漏。"""
    try:
        close_checkpointer()
    except Exception as e:
        logger.warning(f"[Shutdown] 关闭 checkpoint 连接池失败: {e}")


# ================= HRSSC 应用API =================
# 修正 .js 等 MIME 类型（部分环境下默认映射为 application/json，会导致浏览器拒绝执行脚本）
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

# 挂载 app 目录为静态资源：index.html 通过相对路径引用 did-auth.js 等本地脚本，
# 用 StaticFiles 统一托管，MIME 与 HEAD 请求由 Starlette 正确处理。
APP_DIR = os.path.join(PROJECT_ROOT, "app")
FRONTEND_ASSETS_DIR = os.path.join(APP_DIR, "front_end_assets")
app.mount("/static", StaticFiles(directory=APP_DIR), name="static")
if os.path.isdir(FRONTEND_ASSETS_DIR):
    app.mount("/front_end_assets", StaticFiles(directory=FRONTEND_ASSETS_DIR), name="front_end_assets")

@app.get("/")
async def root():
    """返回主页面。"""
    return FileResponse(os.path.join(APP_DIR, "index.html"))

@app.get("/did-auth.js")
async def did_auth_js():
    """前端身份鉴权脚本（index.html 通过相对路径 <script src="did-auth.js"> 引用）。"""
    return FileResponse(os.path.join(APP_DIR, "did-auth.js"),
                        media_type="application/javascript")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "HRSSC"}


# ================= 素材访问接口（JWT 鉴权） =================
# 知识库切片内容提取自图片素材时，chunk.url 记录的是这里的地址。
# 静态挂载不走鉴权，故用受控接口：登录用户方可访问，目录(含文档子目录)与文件名白名单校验防目录穿越。
# 图片落在 <PARSE_RESULT_DIR>/<doc_dir>/<category>/<filename>：
#   - extracted/images：插图（MinerU 从页面抽取的图片）
#   - render/page_renders：整页渲染图（纯图片页的整页快照）
# URL 形态：/api/media/{category}/{doc_dir}/{filename}
from calculate_logic.fetch_knowledge.parse_file import PARSE_RESULT_DIR

MEDIA_OUTPUT_ROOT = str(PARSE_RESULT_DIR)
MEDIA_CATEGORY_DIRS = {
    "extracted": "images",
    "render":    "page_renders",
    "store":     "media_store",
}
# 允许中文、字母、数字、点、下划线、连字符、空格的目录/文件名，防目录穿越
_MEDIA_FILENAME_RE = re.compile(r"^[\w\u4e00-\u9fff ][\w\u4e00-\u9fff ._-]*$")


@app.get("/api/media/{category}/{doc_dir}/{filename}")
async def get_media_file(category: str, doc_dir: str, filename: str, _: TokenData = Depends(get_current_user)):
    """返回知识库素材文件（图片等）。需携带 Bearer Token。"""
    sub = MEDIA_CATEGORY_DIRS.get(category)
    if not sub:
        raise HTTPException(status_code=404, detail="Unknown media category")
    if ".." in doc_dir or ".." in filename or not _MEDIA_FILENAME_RE.match(doc_dir) or not _MEDIA_FILENAME_RE.match(filename):
        raise HTTPException(status_code=404, detail="Invalid path or filename")

    media_dir = os.path.join(MEDIA_OUTPUT_ROOT, doc_dir, sub)
    file_path = os.path.join(media_dir, filename)
    # 兜底确认解析后仍位于目标目录内，防目录穿越
    if not os.path.realpath(file_path).startswith(os.path.realpath(MEDIA_OUTPUT_ROOT) + os.sep):
        raise HTTPException(status_code=404, detail="Invalid path")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="Media not found")

    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(file_path, media_type=media_type)


# ================= 启动服务 =================
if __name__ == "__main__":
    import uvicorn
    # 直接传 app 对象，确保 python3 app/app.py 也能正常工作
    # 多 Worker 模式需要命令行启动：uvicorn app.app:app --host 10.85.212.73 --port 8099 --workers 4
    uvicorn.run(app, host="10.85.212.73", port=8099, log_level="info")