from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from app.database import get_db, AnalysisTask, DailyRecommendation
from app.task_manager import create_task, get_task_status
from app.config import ANALYSIS_DEPTHS
from datetime import datetime

router = APIRouter()


class AnalyzeRequest(BaseModel):
    ticker: str
    depth: str = "standard"


class AnalyzeResponse(BaseModel):
    task_id: str
    status: str
    estimated_time: int


@router.post("/analyze", response_model=AnalyzeResponse)
async def start_analysis(request: AnalyzeRequest):
    """开始分析股票

    统一走 create_task：与批量分析共享并发信号量，
    手动请求同样受 MAX_CONCURRENT_TASKS 限流（超出并发的任务显示"排队中"）。
    """
    ticker = request.ticker.strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="股票代码不能为空")

    if request.depth not in ANALYSIS_DEPTHS:
        raise HTTPException(status_code=400, detail="无效的分析深度")

    task_id = create_task(ticker, depth=request.depth)

    return {
        "task_id": task_id,
        "status": "pending",
        "estimated_time": ANALYSIS_DEPTHS[request.depth]["estimated_time"],
    }


@router.get("/task/{task_id}")
async def get_task(task_id: str):
    """获取任务状态"""
    status = get_task_status(task_id)
    
    if not status:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    return status


@router.get("/history")
async def get_history(limit: int = 10, db: Session = Depends(get_db)):
    """获取历史记录"""
    tasks = db.query(AnalysisTask)\
        .filter(AnalysisTask.status == "completed")\
        .order_by(AnalysisTask.completed_at.desc())\
        .limit(limit)\
        .all()
    
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
    today = datetime.now().strftime("%Y-%m-%d")

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
    
    # 获取历史推荐
    history_recs = db.query(RecommendationHistory)\
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
    
    # 转换为列表并限制数量
    history_list = list(history_by_date.values())[:limit]
    
    return {
        "history": history_list,
        "count": len(history_list)
    }
