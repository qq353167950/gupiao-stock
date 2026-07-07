"""
定时任务调度器（仅交易日执行，自动跳过休市日）
- 收盘后分析（默认 15:10）：获取热门股票并批量深度分析，完成后生成次日推荐
- 早盘推送（默认 08:20）：将当日推荐通过已配置渠道推送给用户
- 磁盘清理（默认 03:30）：清理过期报告 / skill 缓存 / 日志 / 历史任务记录

时间均可通过环境变量覆盖：AFTER_MARKET_ANALYSIS_TIME / MORNING_PUSH_TIME / CLEANUP_TIME
"""
import asyncio
import shutil
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import (
    AFTER_MARKET_ANALYSIS_TIME,
    MORNING_PUSH_TIME,
    CLEANUP_TIME,
    DAILY_ANALYSIS_DEPTH,
    SKILL_REPORTS_DIR,
    SKILL_CACHE_DIR,
    LOGS_DIR,
    REPORT_RETENTION_DAYS,
    CACHE_RETENTION_DAYS,
    LOG_RETENTION_DAYS,
    TASK_RETENTION_DAYS,
    parse_hhmm,
)

scheduler = AsyncIOScheduler()

# 批量分析后台任务强引用（防 GC）
_batch_tasks: set = set()


def archive_old_recommendations():
    """归档今日之前的所有未归档推荐到历史表"""
    from app.database import SessionLocal, DailyRecommendation, RecommendationHistory

    db = SessionLocal()
    try:
        today = datetime.now().strftime("%Y-%m-%d")

        # 查找今日之前的全部未归档推荐（比只查"昨天"健壮：停机一天也不会漏归档）
        old_recs = db.query(DailyRecommendation).filter(
            DailyRecommendation.date < today,
            DailyRecommendation.is_archived == False
        ).all()

        if old_recs:
            for rec in old_recs:
                history = RecommendationHistory(
                    date=rec.date,
                    ticker=rec.ticker,
                    name=rec.name,
                    rank=rec.rank,
                    score=rec.score,
                    dcf_discount=rec.dcf_discount,
                    bullish_ratio=rec.bullish_ratio,
                    reason=rec.reason,
                    recommendation_type=rec.recommendation_type
                )
                db.add(history)
                rec.is_archived = True

            db.commit()
            print(f"✅ 已归档 {len(old_recs)} 条历史推荐")
    finally:
        db.close()


async def after_market_analysis_job():
    """收盘后分析任务 - 仅交易日执行"""
    from app.trading_calendar import is_trading_day

    print(f"\n{'='*70}")
    print(f"📈 收盘后分析 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # 判断今天是否为交易日
    if not is_trading_day():
        print("⚠️  今天非交易日，跳过分析")
        print(f"\n{'='*70}\n")
        return

    try:
        # 归档历史推荐
        print("📦 归档历史推荐...")
        archive_old_recommendations()
        print()

        # 进程内启动批量分析协程（与 Web 手动分析共享并发信号量，路径零依赖）
        from auto_analyze_and_recommend import auto_analyze_and_recommend

        print(f"启动后台批量分析（深度: {DAILY_ANALYSIS_DEPTH}）...")
        task = asyncio.get_running_loop().create_task(
            auto_analyze_and_recommend(depth=DAILY_ANALYSIS_DEPTH, recommendation_type="morning")
        )
        _batch_tasks.add(task)
        task.add_done_callback(_batch_tasks.discard)

        print("✅ 后台批量分析已启动")
        print(f"   预计明天早上 {MORNING_PUSH_TIME} 前完成并推送推荐")

    except Exception as e:
        print(f"❌ 启动分析任务失败: {e}")
        import traceback
        traceback.print_exc()

    print(f"\n{'='*70}\n")


def _build_today_digest() -> tuple:
    """查询当日推荐并格式化为推送摘要，返回 (标题, 正文, 推荐条数)"""
    from app.database import SessionLocal, DailyRecommendation, AnalysisTask
    from app.stock_pool import get_stock_category
    from app import notifier

    today = datetime.now().strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        recs = db.query(DailyRecommendation).filter(
            DailyRecommendation.date == today,
            DailyRecommendation.is_archived == False
        ).order_by(DailyRecommendation.rank).all()

        rec_dicts = []
        for rec in recs:
            item = rec.to_dict()
            item["sector"] = get_stock_category(rec.ticker)
            # 补充最近一次分析的风险等级
            task = db.query(AnalysisTask).filter(
                AnalysisTask.ticker == rec.ticker,
                AnalysisTask.status == "completed"
            ).order_by(AnalysisTask.completed_at.desc()).first()
            if task and task.risk_level:
                item["risk_level"] = task.risk_level
            rec_dicts.append(item)
    finally:
        db.close()

    title = f"📈 {today} 早盘股票推荐（{len(rec_dicts)} 只）" if rec_dicts \
        else f"📈 {today} 早盘推荐（暂无数据）"
    content = notifier.format_daily_digest(today, rec_dicts)
    return title, content, len(rec_dicts)


async def morning_push_job():
    """早盘推荐推送任务 - 仅交易日执行"""
    from app.trading_calendar import is_trading_day
    from app import notifier

    print(f"\n{'='*70}")
    print(f"🌅 早盘推荐推送 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # 判断今天是否为交易日
    if not is_trading_day():
        print("⚠️  今天非交易日，跳过推送")
        print(f"\n{'='*70}\n")
        return

    try:
        title, content, count = _build_today_digest()

        if count > 0:
            print(f"✅ 早盘推荐共 {count} 只股票，开始推送...")
        else:
            print("⚠️  早盘推荐尚未生成（分析可能未完成），推送提醒消息")

        results = notifier.send(title, content)
        if results:
            ok = sum(1 for v in results.values() if v)
            print(f"   推送结果: {ok}/{len(results)} 个渠道成功")

    except Exception as e:
        print(f"❌ 早盘推送失败: {e}")
        import traceback
        traceback.print_exc()

    print(f"\n{'='*70}\n")


def _remove_old_dirs(base_dir, retention_days: int, skip_prefix: str = "_") -> int:
    """删除 base_dir 下 mtime 超过 retention_days 的子目录，返回删除数量"""
    if not base_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for item in base_dir.iterdir():
        # 跳过下划线开头的内部目录（如 .cache/_global 存放 skill 全局状态）
        if not item.is_dir() or item.name.startswith(skip_prefix):
            continue
        try:
            if item.stat().st_mtime < cutoff:
                shutil.rmtree(item)
                removed += 1
        except Exception as e:
            print(f"   ⚠️  删除 {item.name} 失败: {e}")
    return removed


async def cleanup_job():
    """每日磁盘与数据库清理 + Skill 更新检查"""
    from app.database import SessionLocal, AnalysisTask

    print(f"\n{'='*70}")
    print(f"🧹 每日清理 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    try:
        # 1. 过期报告目录
        removed = _remove_old_dirs(SKILL_REPORTS_DIR, REPORT_RETENTION_DAYS)
        print(f"   报告清理: 删除 {removed} 个超过 {REPORT_RETENTION_DAYS} 天的报告目录")

        # 2. skill 数据缓存
        removed = _remove_old_dirs(SKILL_CACHE_DIR, CACHE_RETENTION_DAYS)
        print(f"   缓存清理: 删除 {removed} 个超过 {CACHE_RETENTION_DAYS} 天的缓存目录")

        # 3. 过期日志文件
        removed = 0
        if LOGS_DIR.exists():
            cutoff = time.time() - LOG_RETENTION_DAYS * 86400
            for log_file in LOGS_DIR.glob("*.log*"):
                try:
                    if log_file.stat().st_mtime < cutoff:
                        log_file.unlink()
                        removed += 1
                except Exception:
                    pass
        print(f"   日志清理: 删除 {removed} 个超过 {LOG_RETENTION_DAYS} 天的日志文件")

        # 4. 数据库历史任务记录
        db = SessionLocal()
        try:
            cutoff_dt = datetime.now() - timedelta(days=TASK_RETENTION_DAYS)
            deleted = db.query(AnalysisTask).filter(
                AnalysisTask.created_at < cutoff_dt
            ).delete()
            db.commit()
            print(f"   数据库清理: 删除 {deleted} 条超过 {TASK_RETENTION_DAYS} 天的任务记录")
        finally:
            db.close()

        # 5. Skill 更新检查（skill_manager 内部按 SKILL_UPDATE_INTERVAL_DAYS 限频，
        #    未到间隔时仅打印跳过；git 操作在线程池执行，不阻塞事件循环）
        from app.skill_manager import check_and_update_skill
        await asyncio.get_running_loop().run_in_executor(
            None, check_and_update_skill, True
        )

    except Exception as e:
        print(f"❌ 清理任务失败: {e}")
        import traceback
        traceback.print_exc()

    print(f"\n{'='*70}\n")


def start_scheduler():
    """启动调度器（时间由环境变量配置）"""
    analysis_h, analysis_m = parse_hhmm(AFTER_MARKET_ANALYSIS_TIME, "15:10")
    push_h, push_m = parse_hhmm(MORNING_PUSH_TIME, "08:20")
    cleanup_h, cleanup_m = parse_hhmm(CLEANUP_TIME, "03:30")

    # 收盘后分析（自动判断交易日）
    scheduler.add_job(
        after_market_analysis_job,
        trigger=CronTrigger(day_of_week='mon-fri', hour=analysis_h, minute=analysis_m),
        id='after_market_analysis',
        name=f'收盘后分析（{analysis_h:02d}:{analysis_m:02d}）',
        replace_existing=True
    )

    # 早盘推荐推送（自动判断交易日）
    scheduler.add_job(
        morning_push_job,
        trigger=CronTrigger(day_of_week='mon-fri', hour=push_h, minute=push_m),
        id='morning_push',
        name=f'早盘推荐推送（{push_h:02d}:{push_m:02d}）',
        replace_existing=True
    )

    # 每日清理（每天执行，与是否交易日无关）
    scheduler.add_job(
        cleanup_job,
        trigger=CronTrigger(hour=cleanup_h, minute=cleanup_m),
        id='daily_cleanup',
        name=f'每日清理（{cleanup_h:02d}:{cleanup_m:02d}）',
        replace_existing=True
    )

    scheduler.start()
    print("✅ 定时任务调度器已启动")
    print(f"   - 收盘后分析：每交易日 {analysis_h:02d}:{analysis_m:02d}（深度: {DAILY_ANALYSIS_DEPTH}）")
    print(f"   - 早盘推送：  每交易日 {push_h:02d}:{push_m:02d}")
    print(f"   - 磁盘清理：  每天 {cleanup_h:02d}:{cleanup_m:02d}")
    print("   - 自动跳过休市日")


def stop_scheduler():
    """停止调度器"""
    scheduler.shutdown()
    print("❌ 定时任务调度器已停止")
