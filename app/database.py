from datetime import timedelta
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import DATABASE_URL, REPORT_EXPIRE_HOURS, now_cn

Base = declarative_base()

class AnalysisTask(Base):
    """分析任务表"""
    __tablename__ = "analysis_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(50), unique=True, nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)  # 推荐接口按 ticker 查最近分析
    name = Column(String(100))
    depth = Column(String(20), default="standard")  # quick/standard/deep
    status = Column(String(20), default="pending", index=True)  # pending/running/completed/failed
    progress = Column(Integer, default=0)
    current_stage = Column(String(100))
    score = Column(Float)
    dcf_discount = Column(Float)
    bullish_count = Column(Integer)
    total_voters = Column(Integer)
    report_path = Column(String(500))
    error_message = Column(String(500))
    # 增强分析字段
    enhanced_result = Column(Text)  # JSON格式的完整增强分析结果
    composite_score = Column(Float)  # 综合评分（0-100）
    risk_level = Column(String(50))  # 风险等级
    created_at = Column(DateTime, default=now_cn, index=True)  # 清理任务按时间删除（北京时间）
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    expires_at = Column(DateTime)
    
    def to_dict(self):
        result = {
            "id": self.id,
            "task_id": self.task_id,
            "ticker": self.ticker,
            "name": self.name,
            "depth": self.depth,
            "status": self.status,
            "progress": self.progress,
            "current_stage": self.current_stage,
            "score": self.score,
            "dcf_discount": self.dcf_discount,
            "bullish_count": self.bullish_count,
            "total_voters": self.total_voters,
            "report_url": f"/reports/{self.report_path}" if self.report_path else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "error_message": self.error_message,
            # 增强分析结果
            "composite_score": self.composite_score,
            "risk_level": self.risk_level
        }
        
        # 解析增强分析结果
        if self.enhanced_result:
            try:
                import json
                result["enhanced_result"] = json.loads(self.enhanced_result)
            except (ValueError, TypeError) as e:
                # 解析失败保留原始字符串缺席，但打日志便于排障
                print(f"⚠️  enhanced_result JSON 解析失败 (task {self.task_id}): {e}")
        
        return result
    
    def set_expires(self):
        """设置过期时间"""
        if self.completed_at:
            self.expires_at = self.completed_at + timedelta(hours=REPORT_EXPIRE_HOURS)


class DailyRecommendation(Base):
    """每日推荐"""
    __tablename__ = "daily_recommendations"
    
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)  # 推荐日期 YYYY-MM-DD
    ticker = Column(String, index=True)  # 股票代码
    name = Column(String)  # 股票名称
    rank = Column(Integer)  # 排名
    score = Column(Float)  # 评分
    dcf_discount = Column(Float)  # DCF折扣
    bullish_ratio = Column(Float)  # 看多比例
    reason = Column(String)  # 推荐理由
    recommendation_type = Column(String, default="morning")  # 推荐类型：morning(早盘)
    is_archived = Column(Boolean, default=False)  # 是否已归档
    created_at = Column(DateTime, default=now_cn)
    
    def to_dict(self):
        return {
            "ticker": self.ticker,
            "name": self.name,
            "rank": self.rank,
            "score": self.score,
            "dcf_discount": self.dcf_discount,
            "bullish_ratio": self.bullish_ratio,
            "reason": self.reason,
            "recommendation_type": self.recommendation_type,
            "date": self.date,
            "is_archived": self.is_archived
        }


class RecommendationHistory(Base):
    """推荐历史归档"""
    __tablename__ = "recommendation_history"
    
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String, index=True)  # 推荐日期
    ticker = Column(String)  # 股票代码
    name = Column(String)  # 股票名称
    rank = Column(Integer)  # 排名
    score = Column(Float)  # 评分
    dcf_discount = Column(Float)  # DCF折扣
    bullish_ratio = Column(Float)  # 看多比例
    reason = Column(String)  # 推荐理由
    recommendation_type = Column(String)  # 推荐类型
    archived_at = Column(DateTime, default=now_cn)  # 归档时间（北京时间）
    
    def to_dict(self):
        return {
            "ticker": self.ticker,
            "name": self.name,
            "rank": self.rank,
            "score": self.score,
            "dcf_discount": self.dcf_discount,
            "bullish_ratio": self.bullish_ratio,
            "reason": self.reason,
            "date": self.date,
            "archived_at": self.archived_at.strftime("%Y-%m-%d %H:%M:%S") if self.archived_at else None
        }


# 创建数据库引擎
# check_same_thread=False：分析在线程池执行；timeout=30：写锁等待上限
engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """每个新连接启用 WAL 与 busy_timeout。

    WAL 模式下读写不互斥：Web 查询任务状态与分析线程写进度可并发进行，
    消除 standard 深度下长达数十分钟的读写锁竞争。
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")  # WAL 下的推荐档位：断电最多丢最后一次事务，不损坏库
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 创建所有表
Base.metadata.create_all(bind=engine)


def recover_zombie_tasks() -> int:
    """服务启动时恢复僵尸任务：把上次进程遗留的 running/pending 标记为 failed。

    分析在进程内线程池执行，进程重启（更新/崩溃/OOM）后这些任务不可能再推进，
    不清理则前端永远显示"运行中"，且批量等待循环会空等到超时。
    返回恢复数量。
    """
    db = SessionLocal()
    try:
        zombies = db.query(AnalysisTask).filter(
            AnalysisTask.status.in_(["running", "pending"])
        ).all()
        for task in zombies:
            task.status = "failed"
            task.error_message = "服务重启导致任务中断，请重新发起分析"
            task.completed_at = now_cn()
        if zombies:
            db.commit()
            print(f"♻️  已恢复 {len(zombies)} 个僵尸任务（上次进程遗留的 running/pending → failed）")
        return len(zombies)
    finally:
        db.close()


# 数据库依赖
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
