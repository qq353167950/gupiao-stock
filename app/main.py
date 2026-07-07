from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import json
import asyncio
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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """首页"""
    return templates.TemplateResponse(request, "index.html")


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


@app.websocket("/ws/task/{task_id}")
async def websocket_task(websocket: WebSocket, task_id: str):
    """WebSocket 实时推送任务进度"""
    await websocket.accept()
    
    try:
        while True:
            # 获取任务状态
            status = get_task_status(task_id)
            
            if not status:
                await websocket.send_json({
                    "type": "error",
                    "message": "任务不存在"
                })
                break
            
            # 发送进度
            await websocket.send_json({
                "type": "progress",
                "data": status
            })
            
            # 任务完成或失败，停止推送
            if status["status"] in ["completed", "failed"]:
                break
            
            # 等待5秒再次查询
            await asyncio.sleep(5)
            
    except WebSocketDisconnect:
        pass


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "app": APP_NAME, "version": VERSION}


if __name__ == "__main__":
    import uvicorn
    from app.config import HOST, PORT
    
    uvicorn.run(app, host=HOST, port=PORT)
