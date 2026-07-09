from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from app.auth import require_user
from app.database import get_db, AnalysisTask, DailyRecommendation, User
from app.task_manager import create_task, get_task_status
from app.config import ANALYSIS_DEPTHS, now_cn

router = APIRouter()


class AnalyzeRequest(BaseModel):
    ticker: str
    depth: str = "standard"


class AnalyzeResponse(BaseModel):
    task_id: str
    status: str
    estimated_time: int


@router.post("/analyze", response_model=AnalyzeResponse)
async def start_analysis(request: AnalyzeRequest, user: User = Depends(require_user)):
    """开始分析股票（需登录——游客只读，防止分析资源被滥用）

    统一走 create_task：手动分析走独立的手动并发通道（MANUAL_CONCURRENT_TASKS），
    与定时推荐批次（MAX_CONCURRENT_TASKS）互不排队；超出手动并发的任务显示"排队中"。
    """
    ticker = request.ticker.strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="股票代码不能为空")

    if request.depth not in ANALYSIS_DEPTHS:
        raise HTTPException(status_code=400, detail="无效的分析深度")

    task_id = create_task(ticker, depth=request.depth, owner_user_id=user.id)

    return {
        "task_id": task_id,
        "status": "pending",
        "estimated_time": ANALYSIS_DEPTHS[request.depth]["estimated_time"],
    }


def _can_view_task(task_dict: dict, user: User) -> bool:
    """任务可见性：管理员看全部；普通用户仅看自己发起的。

    owner 为空的任务（定时批量分析/历史遗留）视为系统任务，仅管理员可见。
    """
    if user.is_admin:
        return True
    return task_dict.get("owner_user_id") == user.id


@router.get("/task/{task_id}")
async def get_task(task_id: str, user: User = Depends(require_user)):
    """获取任务状态（需登录；普通用户仅可查看自己的任务）"""
    status = get_task_status(task_id)

    if not status:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not _can_view_task(status, user):
        raise HTTPException(status_code=403, detail="无权查看该任务")

    return status


@router.get("/history")
async def get_history(limit: int = 10, db: Session = Depends(get_db),
                      user: User = Depends(require_user)):
    """获取历史记录（需登录；普通用户仅自己的记录，管理员可见全部）"""
    query = db.query(AnalysisTask).filter(AnalysisTask.status == "completed")
    if not user.is_admin:
        query = query.filter(AnalysisTask.owner_user_id == user.id)
    tasks = query.order_by(AnalysisTask.completed_at.desc()).limit(limit).all()

    return {
        "records": [task.to_dict() for task in tasks]
    }


@router.get("/recommendations/today")
async def get_recommendations(rec_type: str = "all", db: Session = Depends(get_db)):
    """
    获取今日推荐（分板块）

    Args:
        rec_type: 推荐类型（morning/noon/all）
    """
    today = now_cn().strftime("%Y-%m-%d")

    query = db.query(DailyRecommendation).filter(
        DailyRecommendation.date == today,
        DailyRecommendation.is_archived == False
    )

    if rec_type in ["morning", "noon"]:
        query = query.filter(DailyRecommendation.recommendation_type == rec_type)

    recommendations = query.order_by(DailyRecommendation.rank).all()

    # 一次性取全部推荐股票的最新完成任务（消除逐条查询的 N+1）：
    # 按 completed_at 升序遍历，后写入覆盖先写入 → map 中留下的即每只股票最新一条
    from app.stock_pool import get_stock_category

    tickers = list({rec.ticker for rec in recommendations})
    latest_task_map = {}
    if tickers:
        tasks = db.query(AnalysisTask).filter(
            AnalysisTask.ticker.in_(tickers),
            AnalysisTask.status == "completed"
        ).order_by(AnalysisTask.completed_at.asc()).all()
        for task in tasks:
            latest_task_map[task.ticker] = task

    sectors = {}
    for rec in recommendations:
        sector = get_stock_category(rec.ticker)
        if sector not in sectors:
            sectors[sector] = []

        rec_dict = rec.to_dict()
        rec_dict['sector'] = sector
        # 计算板块内排名
        rec_dict['sector_rank'] = len(sectors[sector]) + 1
        rec_dict['level'] = "最推荐" if rec_dict['sector_rank'] == 1 else "推荐"

        # 补充增强分析结果
        task = latest_task_map.get(rec.ticker)
        if task:
            rec_dict['composite_score'] = task.composite_score
            rec_dict['risk_level'] = task.risk_level

        sectors[sector].append(rec_dict)

    return {
        "date": today,
        "type": rec_type,
        "sectors": sectors,
        "total": len(recommendations)
    }


@router.get("/recommendations/history")
async def get_recommendation_history(limit: int = 30, db: Session = Depends(get_db)):
    """
    获取历史推荐列表（按日期分组）
    
    Args:
        limit: 最多返回多少天的历史记录
    """
    from app.database import RecommendationHistory
    from app.stock_pool import get_stock_category

    # 先取最近 limit 天的日期，再查明细：避免历史表增长后全表加载拖慢接口
    recent_dates = [
        row[0] for row in db.query(RecommendationHistory.date)
        .distinct()
        .order_by(RecommendationHistory.date.desc())
        .limit(limit)
        .all()
    ]
    if not recent_dates:
        return {"history": [], "count": 0}

    history_recs = db.query(RecommendationHistory)\
        .filter(RecommendationHistory.date.in_(recent_dates))\
        .order_by(RecommendationHistory.date.desc(), RecommendationHistory.rank)\
        .all()
    
    # 按日期分组
    history_by_date = {}
    for rec in history_recs:
        date = rec.date
        if date not in history_by_date:
            history_by_date[date] = {
                'date': date,
                'sectors': {},
                'total': 0
            }
        
        sector = get_stock_category(rec.ticker)
        if sector not in history_by_date[date]['sectors']:
            history_by_date[date]['sectors'][sector] = []
        
        rec_dict = rec.to_dict()
        rec_dict['sector'] = sector
        rec_dict['sector_rank'] = len(history_by_date[date]['sectors'][sector]) + 1
        rec_dict['level'] = "最推荐" if rec_dict['sector_rank'] == 1 else "推荐"
        
        history_by_date[date]['sectors'][sector].append(rec_dict)
        history_by_date[date]['total'] += 1
    
    # 转换为列表（日期已在查询层限制为最近 limit 天）
    history_list = list(history_by_date.values())
    
    return {
        "history": history_list,
        "count": len(history_list)
    }
