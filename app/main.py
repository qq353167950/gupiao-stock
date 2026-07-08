from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from starlette.middleware.sessions import SessionMiddleware
from app.config import (
    APP_NAME, VERSION, STATIC_DIR, TEMPLATES_DIR, SKILL_REPORTS_DIR,
    SESSION_MAX_AGE, load_session_secret,
)
from app.api.analyze import router as analyze_router
from app.api.auth import router as auth_router
from app.task_manager import get_task_status


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时
    print("🚀 启动应用...")

    # 恢复上次进程遗留的僵尸任务（running/pending → failed）
    from app.database import recover_zombie_tasks
    recover_zombie_tasks()

    # 确保内置管理员账号存在（仅管理员可开设其他账号）
    from app.auth import ensure_admin_user
    ensure_admin_user()

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


# ─── 会话与用户解析 ───
# 注意执行顺序：@app.middleware 的注册先于 add_middleware(SessionMiddleware)，
# 使 SessionMiddleware 位于洋葱外层先解析签名 cookie，本中间件才能读 request.session。

_USER_LOOKUP_SKIP_PREFIXES = ("/static", "/favicon.ico", "/health")


@app.middleware("http")
async def inject_user(request: Request, call_next):
    """把会话中的 user_id 解析为 User 对象挂到 request.state.user。

    每请求查库（而非把用户信息塞进 cookie）保证删号/改权限立即生效；
    静态资源与健康检查跳过，避免无谓的数据库查询。

    /reports 静态报告目录在此统一加登录门禁：报告含完整分析结论，
    游客只能浏览推荐列表，点击详情报告必须先登录（StaticFiles 挂载
    应用无法使用路由依赖，只能在中间件层拦截）。
    """
    request.state.user = None
    if not request.url.path.startswith(_USER_LOOKUP_SKIP_PREFIXES):
        user_id = request.session.get("user_id")
        if user_id:
            from app.auth import get_user_by_id
            user = get_user_by_id(user_id)
            if user is None:
                request.session.clear()  # 账号已被删除 → 会话立即作废
            request.state.user = user

    if request.url.path.startswith("/reports") and request.state.user is None:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=302)

    return await call_next(request)


app.add_middleware(
    SessionMiddleware,
    secret_key=load_session_secret(),
    max_age=SESSION_MAX_AGE,
    same_site="lax",
)

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
app.include_router(auth_router, prefix="/api", tags=["认证"])


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
    """首页（默认展示开始分析）"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/recommendations", response_class=HTMLResponse)
async def recommendations_page(request: Request):
    """今日推荐页面"""
    return templates.TemplateResponse(request, "recommendations.html")


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """历史记录页面"""
    return templates.TemplateResponse(request, "history.html")


@app.get("/glossary", response_class=HTMLResponse)
async def glossary_page(request: Request):
    """股票名词解释页面"""
    return templates.TemplateResponse(request, "glossary.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    """登录页面（已登录则直接回跳）"""
    if request.state.user is not None:
        return RedirectResponse(next if next.startswith("/") else "/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"next": next})


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    """账号设置页面（修改自己的密码，需登录）"""
    if request.state.user is None:
        return RedirectResponse("/login?next=/account", status_code=302)
    return templates.TemplateResponse(request, "account.html")


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    """用户管理页面（仅管理员；未登录跳登录页，非管理员回首页）"""
    user = request.state.user
    if user is None:
        return RedirectResponse("/login?next=/admin/users", status_code=302)
    if not user.is_admin:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "admin_users.html")


@app.get("/analyze", response_class=HTMLResponse)
async def analyze_page(request: Request, task_id: str = None):
    """分析页面（进度与详情含分析产出，需登录）"""
    user = request.state.user
    if user is None:
        next_url = f"/analyze?task_id={task_id}" if task_id else "/analyze"
        return RedirectResponse(f"/login?next={next_url}", status_code=302)

    # 与报告页同规则：普通用户仅可打开自己的任务（API 层同样校验，这里提前给出明确提示）
    if task_id:
        status = get_task_status(task_id)
        if status and not user.is_admin and status.get("owner_user_id") != user.id:
            return HTMLResponse(content="<h1>无权查看该任务</h1>", status_code=403)

    return templates.TemplateResponse(request, "analyze.html", {
        "task_id": task_id
    })


@app.get("/report/{task_id}", response_class=HTMLResponse)
async def report_page(request: Request, task_id: str):
    """报告页面（完整分析结论，需登录；普通用户仅可看自己的任务）"""
    user = request.state.user
    if user is None:
        return RedirectResponse(f"/login?next=/report/{task_id}", status_code=302)

    status = get_task_status(task_id)

    if not status:
        return HTMLResponse(content="<h1>任务不存在</h1>", status_code=404)
    # 与 API 同规则：管理员看全部，普通用户仅自己的任务（owner 为空=系统任务）
    if not user.is_admin and status.get("owner_user_id") != user.id:
        return HTMLResponse(content="<h1>无权查看该报告</h1>", status_code=403)

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
