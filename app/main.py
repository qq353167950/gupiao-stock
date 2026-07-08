from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from app.config import APP_NAME, VERSION, STATIC_DIR, TEMPLATES_DIR, SKILL_REPORTS_DIR
from app.api.analyze import router as analyze_router
from app.task_manager import get_task_status


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时
    print("🚀 启动应用...")

    # 恢复上次进程遗留的僵尸任务（running/pending → failed）
    from app.database import recover_zombie_tasks
    recover_zombie_tasks()

    # 检查并安装/更新内置 skill（内置模式自动安装，按间隔检查更新）
    from app.skill_manager import ensure_skills_ready
    ensure_skills_ready()
    
    # 启动定时任务调度器
    print(f"\n启动定时任务调度器...")
    from app.scheduler import start_scheduler
    start_scheduler()
    
    yield
    
    # 关闭时
    print("👋 关闭应用...")
    from app.scheduler import stop_scheduler
    stop_scheduler()


# 创建 FastAPI 应用
app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)

# 挂载静态文件
# 无条件创建后挂载：static 为空目录时 git 不跟踪，克隆/解压后可能缺失，直接挂载会启动即崩
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 挂载报告目录（来自 deep-analysis）
# 无条件创建后挂载：首次启动时目录尚不存在，若跳过挂载会导致后续生成的报告全部 404
SKILL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=str(SKILL_REPORTS_DIR)), name="reports")

# 模板
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 注册路由
app.include_router(analyze_router, prefix="/api", tags=["分析"])


# 网站图标（内联 SVG，上升K线主题）
# 以路由而非静态文件提供：报告页为 skill 生成的独立 HTML（无法插 link 标签），
# 浏览器对其默认请求 /favicon.ico，缺失会在访问日志刷 404
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#667eea"/>'
    '<path d="M6 22 L13 15 L18 19 L26 9" stroke="#fff" stroke-width="3" '
    'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M20 9 H26 V15" stroke="#fff" stroke-width="3" '
    'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """网站图标（浏览器自动请求，无此路由则每次访问都产生 404 日志）"""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """首页"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/glossary", response_class=HTMLResponse)
async def glossary_page(request: Request):
    """股票名词解释页面"""
    return templates.TemplateResponse(request, "glossary.html")


@app.get("/analyze", response_class=HTMLResponse)
async def analyze_page(request: Request, task_id: str = None):
    """分析页面"""
    return templates.TemplateResponse(request, "analyze.html", {
        "task_id": task_id
    })


@app.get("/report/{task_id}", response_class=HTMLResponse)
async def report_page(request: Request, task_id: str):
    """报告页面"""
    status = get_task_status(task_id)
    
    if not status:
        return HTMLResponse(content="<h1>任务不存在</h1>", status_code=404)
    
    return templates.TemplateResponse(request, "report.html", {
        "task": status
    })


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "app": APP_NAME, "version": VERSION}


if __name__ == "__main__":
    import uvicorn
    from app.config import HOST, PORT
    
    uvicorn.run(app, host=HOST, port=PORT)
