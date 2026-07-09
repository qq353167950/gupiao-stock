"""
定时任务调度器（仅交易日执行，自动跳过休市日）
- 收盘后分析（默认 15:10）：获取热门股票并批量深度分析，完成后生成次日推荐
- 早盘推送（默认 08:20）：将当日推荐通过已配置渠道推送给用户
- 磁盘清理（默认 03:30）：清理过期报告 / skill 缓存 / 日志 / 历史任务记录

时间均可通过环境变量覆盖：AFTER_MARKET_ANALYSIS_TIME / MORNING_PUSH_TIME / CLEANUP_TIME

错过补跑：APScheduler 为纯内存调度，重启后只会等下一个触发点——若服务在
触发时刻宕机/重启（如 15:10 后才拉代码重启），当天任务会静默丢失。
因此每个任务启动时把"今天已尝试"记入 data/.scheduler-state.json，
服务启动时检查"今天该跑的点已过但未尝试"的任务并安排一次性补跑。
"""
import asyncio
import json
import shutil
import time
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import (
    AFTER_MARKET_ANALYSIS_TIME,
    MORNING_PUSH_TIME,
    CLEANUP_TIME,
    DAILY_ANALYSIS_DEPTH,
    DATA_DIR,
    SKILL_REPORTS_DIR,
    SKILL_CACHE_DIR,
    LOGS_DIR,
    REPORT_RETENTION_DAYS,
    CACHE_RETENTION_DAYS,
    LOG_RETENTION_DAYS,
    TASK_RETENTION_DAYS,
    TZ_SHANGHAI,
    now_cn,
    parse_hhmm,
)

# 调度器显式绑定北京时间：不依赖服务器 TZ，海外容器上 15:10/08:20/03:30
# 均按北京时间触发（与交易时段语义一致）
scheduler = AsyncIOScheduler(timezone=TZ_SHANGHAI)

# 批量分析后台任务强引用（防 GC）
_batch_tasks: set = set()

# ─── 补跑状态（记录各任务当天是否已尝试执行）───
SCHEDULER_STATE_FILE = DATA_DIR / ".scheduler-state.json"

# 早盘推送补跑截止（北京时间小时）：收盘后再推"早盘推荐"已无意义
MORNING_PUSH_CATCHUP_CUTOFF_HOUR = 15


def _read_sched_state() -> dict:
    """读补跑状态文件 {job_id: "YYYY-MM-DD"}，不存在或损坏返回空 dict"""
    if not SCHEDULER_STATE_FILE.exists():
        return {}
    try:
        return json.loads(SCHEDULER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _mark_attempt(job_id: str):
    """记录任务今天已尝试执行（attempt 语义：启动即记，中途失败不重跑——
    反复重启场景下避免同一天把重型批量分析反复拉起）"""
    state = _read_sched_state()
    state[job_id] = now_cn().strftime("%Y-%m-%d")
    try:
        SCHEDULER_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"⚠️  调度状态写入失败（重启后可能重复补跑）: {e}")


def _catchup_decisions(state: dict, now) -> list:
    """纯函数：返回今天已过触发点但未尝试过的 job_id 列表（供启动补跑）。

    - daily_cleanup：每天
    - after_market_analysis：仅周一至五（是否交易日由任务内部判断）
    - morning_push：仅周一至五，且未过 15:00 截止（收盘后推"早盘推荐"无意义）
    """
    analysis_hm = parse_hhmm(AFTER_MARKET_ANALYSIS_TIME, "15:10")
    push_hm = parse_hhmm(MORNING_PUSH_TIME, "08:20")
    cleanup_hm = parse_hhmm(CLEANUP_TIME, "03:30")

    today = now.strftime("%Y-%m-%d")
    now_hm = (now.hour, now.minute)
    is_weekday = now.weekday() < 5

    due = []
    if state.get("daily_cleanup") != today and now_hm >= cleanup_hm:
        due.append("daily_cleanup")
    if is_weekday and state.get("after_market_analysis") != today and now_hm >= analysis_hm:
        due.append("after_market_analysis")
    if (is_weekday and state.get("morning_push") != today
            and now_hm >= push_hm and now.hour < MORNING_PUSH_CATCHUP_CUTOFF_HOUR):
        due.append("morning_push")
    return due


def archive_old_recommendations():
    """归档今日之前的所有未归档推荐到历史表"""
    from app.database import SessionLocal, DailyRecommendation, RecommendationHistory

    db = SessionLocal()
    try:
        today = now_cn().strftime("%Y-%m-%d")

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

    _mark_attempt("after_market_analysis")
    print(f"\n{'='*70}")
    print(f"📈 收盘后分析 - {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")
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


def _diagnose_no_recommendation(today: str) -> str:
    """当日推荐为空时诊断具体原因，返回给用户看的白话说明。

    区分四种情况（按诊断优先级）：
    1. 批次跑完但无一入选 → 筛选太严/全是高风险，属正常结果
    2. 批次被中断且已去除（开盘前补不完）→ 服务重启导致
    3. 有分析任务但全部失败 → 数据源/环境故障
    4. 什么都没有 → 昨日收盘分析根本没启动（服务未运行）
    """
    from app.batch_state import load_batch_state, decide_resume_action
    from app.database import SessionLocal, AnalysisTask

    state = load_batch_state()

    # 情况 1/2：有批次快照且目标日就是今天
    if state and state.get("target_date") == today:
        if state.get("status") == "done":
            return ("昨日分析已正常完成，但没有股票通过筛选"
                    "（评分不足或均被判定为高风险），今日暂无推荐。")
        # abandoned=启动时已判定去除；running+abandon=尚未来得及标记的同类情况
        if (state.get("status") == "abandoned"
                or (state.get("status") == "running"
                    and decide_resume_action(state) == "abandon")):
            done_count = 0
            task_ids = [s.get("task_id") for s in state.get("stocks", []) if s.get("task_id")]
            if task_ids:
                db = SessionLocal()
                try:
                    done_count = db.query(AnalysisTask).filter(
                        AnalysisTask.task_id.in_(task_ids),
                        AnalysisTask.status == "completed",
                    ).count()
                finally:
                    db.close()
            total = len(state.get("stocks", []))
            return (f"昨日批量分析中途被中断（完成 {done_count}/{total} 只），"
                    f"且剩余分析已无法在开盘前补完，今日暂无推荐。"
                    f"下一批分析将于今日收盘后自动启动。")

    # 情况 3/4：看昨日 20 小时内是否创建过系统分析任务
    db = SessionLocal()
    try:
        since = now_cn() - timedelta(hours=20)
        recent = db.query(AnalysisTask).filter(
            AnalysisTask.created_at >= since,
            AnalysisTask.owner_user_id.is_(None),  # 系统批量任务（手动分析有归属）
        ).all()
    finally:
        db.close()

    if not recent:
        return ("昨日收盘后未执行批量分析（服务可能未运行），今日暂无推荐。"
                "下一批分析将于今日收盘后自动启动。")

    failed = sum(1 for t in recent if t.status == "failed")
    if failed == len(recent):
        return (f"昨日批量分析已执行但 {failed} 只全部失败"
                f"（可能是数据源不可用或分析超时），今日暂无推荐。请检查服务日志。")

    return "分析可能仍在进行中或未生成有效结果，今日暂无推荐。"


def _build_today_digest() -> tuple:
    """查询当日推荐并格式化为推送摘要，返回 (标题, 正文, 推荐条数)"""
    from app.database import SessionLocal, DailyRecommendation, AnalysisTask
    from app.stock_pool import get_stock_category
    from app import notifier

    today = now_cn().strftime("%Y-%m-%d")
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
    if rec_dicts:
        content = notifier.format_daily_digest(today, rec_dicts)
    else:
        # 无推荐时诊断具体原因，替代笼统的"分析可能未完成"
        reason = _diagnose_no_recommendation(today)
        content = f"**{today}** 暂无推荐\n\n{reason}"
    return title, content, len(rec_dicts)


async def morning_push_job():
    """早盘推荐推送任务 - 仅交易日执行"""
    from app.trading_calendar import is_trading_day
    from app import notifier

    _mark_attempt("morning_push")
    print(f"\n{'='*70}")
    print(f"🌅 早盘推荐推送 - {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    # 判断今天是否为交易日
    if not is_trading_day():
        print("⚠️  今天非交易日，跳过推送")
        print(f"\n{'='*70}\n")
        return

    try:
        loop = asyncio.get_running_loop()
        # DB 查询与格式化为同步操作，放线程池避免阻塞事件循环
        title, content, count = await loop.run_in_executor(None, _build_today_digest)

        if count > 0:
            print(f"✅ 早盘推荐共 {count} 只股票，开始推送...")
        else:
            print("⚠️  早盘推荐尚未生成（分析可能未完成），推送提醒消息")

        # notifier.send 为同步 requests 调用（含失败重试 sleep），必须走线程池
        results = await loop.run_in_executor(None, notifier.send, title, content)
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


def _do_cleanup_sync():
    """同步清理逻辑（删目录/删日志/清库），供 cleanup_job 放入线程池执行"""
    from app.database import SessionLocal, AnalysisTask, RecommendationHistory

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

    # 4. 数据库历史记录（任务表 + 推荐历史表，防止无限增长拖慢查询）
    db = SessionLocal()
    try:
        cutoff_dt = now_cn() - timedelta(days=TASK_RETENTION_DAYS)
        deleted = db.query(AnalysisTask).filter(
            AnalysisTask.created_at < cutoff_dt
        ).delete()
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
        deleted_hist = db.query(RecommendationHistory).filter(
            RecommendationHistory.date < cutoff_date
        ).delete()
        db.commit()
        print(f"   数据库清理: 删除 {deleted} 条任务记录、{deleted_hist} 条推荐历史（超过 {TASK_RETENTION_DAYS} 天）")
    finally:
        db.close()


async def cleanup_job():
    """每日磁盘与数据库清理 + Skill 更新检查"""
    _mark_attempt("daily_cleanup")
    print(f"\n{'='*70}")
    print(f"🧹 每日清理 - {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    try:
        loop = asyncio.get_running_loop()
        # 磁盘删除与 DB 清理为同步重 I/O，放线程池避免阻塞事件循环
        await loop.run_in_executor(None, _do_cleanup_sync)

        # Skill 更新检查（skill_manager 内部按 SKILL_UPDATE_INTERVAL_DAYS 限频，
        #    未到间隔时仅打印跳过；git 操作在线程池执行，不阻塞事件循环）
        from app.skill_manager import check_and_update_skill
        await loop.run_in_executor(None, check_and_update_skill, True)

    except Exception as e:
        print(f"❌ 清理任务失败: {e}")
        import traceback
        traceback.print_exc()

    print(f"\n{'='*70}\n")


async def resume_batch_job():
    """断点续跑：重启后恢复上次未完成的批量分析。

    场景：批量分析进行中服务重启（更新代码/宕机/OOM），僵尸任务已被
    recover_zombie_tasks 标 failed，但批次快照（data/.batch-state.json）
    仍是 running——据此复用已完成任务、重建未完成任务，继续走完推荐流程。

    执行前用 decide_resume_action 二次评估：从"安排续跑"到"真正执行"存在
    延迟（启动 60 秒就绪期 / defer 等到 15:00 后），期间时效可能已经丢失
    （如 8:59 安排、9:01 执行且预估撞上 9:00 截止），按最新决策处置。
    """
    from app.batch_state import (
        load_batch_state, decide_resume_action, mark_batch_abandoned,
    )
    from auto_analyze_and_recommend import auto_analyze_and_recommend

    state = load_batch_state()
    action = decide_resume_action(state)
    if action == "abandon":
        # 时效已丢失：去除批次，等当天 15:10 收盘分析开新一轮
        mark_batch_abandoned(state)
        print(f"🗑️  批次已去除：无法在目标日 {state.get('target_date')} 开盘（9:00）前补完，"
              f"等待收盘后新一轮分析")
        return
    if action == "defer":
        # 执行时刻落入 9:00-15:00 窗口（如 8:59 安排、9:00 后就绪）：顺延到 15:00 后
        from app.batch_state import MARKET_SESSION_END_HOUR
        run_at = now_cn().replace(
            hour=MARKET_SESSION_END_HOUR, minute=0, second=30, microsecond=0
        )
        scheduler.add_job(
            resume_batch_job,
            trigger="date",
            run_date=run_at,
            id="resume_batch",
            name="断点续跑批量分析（15:00 后）",
            replace_existing=True,
        )
        print(f"⏸️  当前处于 9:00-15:00 时段，批量续跑顺延至 {run_at.strftime('%H:%M')}")
        return
    if action != "resume":
        return

    print(f"\n{'='*70}")
    print(f"♻️  断点续跑批量分析 - {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   目标日期: {state.get('target_date')} | 深度: {state.get('depth')}")
    print(f"{'='*70}\n")

    try:
        await auto_analyze_and_recommend(
            depth=state.get("depth") or DAILY_ANALYSIS_DEPTH,
            recommendation_type=state.get("recommendation_type") or "morning",
            resume_state=state,
        )
    except Exception as e:
        print(f"❌ 断点续跑失败: {e}")
        import traceback
        traceback.print_exc()


def start_scheduler():
    """启动调度器（时间由环境变量配置）"""
    analysis_h, analysis_m = parse_hhmm(AFTER_MARKET_ANALYSIS_TIME, "15:10")
    push_h, push_m = parse_hhmm(MORNING_PUSH_TIME, "08:20")
    cleanup_h, cleanup_m = parse_hhmm(CLEANUP_TIME, "03:30")

    # 收盘后分析（自动判断交易日；trigger 必须显式传时区——
    # CronTrigger 未传 timezone 时在实例化瞬间固化服务器本地时区，不继承 scheduler 设置）
    scheduler.add_job(
        after_market_analysis_job,
        trigger=CronTrigger(day_of_week='mon-fri', hour=analysis_h, minute=analysis_m,
                            timezone=TZ_SHANGHAI),
        id='after_market_analysis',
        name=f'收盘后分析（{analysis_h:02d}:{analysis_m:02d}）',
        replace_existing=True
    )

    # 早盘推荐推送（自动判断交易日）
    scheduler.add_job(
        morning_push_job,
        trigger=CronTrigger(day_of_week='mon-fri', hour=push_h, minute=push_m,
                            timezone=TZ_SHANGHAI),
        id='morning_push',
        name=f'早盘推荐推送（{push_h:02d}:{push_m:02d}）',
        replace_existing=True
    )

    # 每日清理（每天执行，与是否交易日无关）
    scheduler.add_job(
        cleanup_job,
        trigger=CronTrigger(hour=cleanup_h, minute=cleanup_m, timezone=TZ_SHANGHAI),
        id='daily_cleanup',
        name=f'每日清理（{cleanup_h:02d}:{cleanup_m:02d}）',
        replace_existing=True
    )

    scheduler.start()
    print("✅ 定时任务调度器已启动（北京时间）")
    print(f"   - 收盘后分析：每交易日 {analysis_h:02d}:{analysis_m:02d}（深度: {DAILY_ANALYSIS_DEPTH}）")
    print(f"   - 早盘推送：  每交易日 {push_h:02d}:{push_m:02d}")
    print(f"   - 磁盘清理：  每天 {cleanup_h:02d}:{cleanup_m:02d}")
    print("   - 自动跳过休市日")

    # ─── 断点续跑：上次批量分析被重启打断时按最新决策处置 ───
    # 与下方"错过补跑"互斥：续跑本身会走完整个推荐流程，
    # 再补跑 after_market_analysis 会重复拉起一整批分析。
    from app.batch_state import (
        load_batch_state, decide_resume_action, estimate_remaining_minutes,
        mark_batch_abandoned, MARKET_SESSION_END_HOUR,
    )
    _batch_state = load_batch_state()
    _resume_action = decide_resume_action(_batch_state)
    if _resume_action == "resume":
        scheduler.add_job(
            resume_batch_job,
            trigger="date",
            run_date=now_cn() + timedelta(seconds=60),
            id="resume_batch",
            name="断点续跑批量分析",
            replace_existing=True,
        )
        print(f"   ♻️  检测到未完成的批量分析（预估还需 "
              f"{estimate_remaining_minutes(_batch_state)} 分钟），60 秒后断点续跑")
    elif _resume_action == "defer":
        # 9:00-15:00 时段不启动续跑：安排到当天 15:00 后再续。
        # 走到这里说明目标日在未来（休市日盘中重启），时效由执行前二次评估把关
        run_at = now_cn().replace(
            hour=MARKET_SESSION_END_HOUR, minute=0, second=30, microsecond=0
        )
        scheduler.add_job(
            resume_batch_job,
            trigger="date",
            run_date=run_at,
            id="resume_batch",
            name="断点续跑批量分析（15:00 后）",
            replace_existing=True,
        )
        print(f"   ⏸️  检测到未完成的批量分析，当前处于 9:00-15:00 时段不启动，"
              f"已安排 {run_at.strftime('%H:%M')} 续跑")
    elif _resume_action == "abandon":
        # 预估开盘（9:00）前补不完或目标日已过：整批去除，
        # 等当天 15:10 收盘分析开新一轮——绝不隔天续跑旧批次
        mark_batch_abandoned(_batch_state)
        print(f"   🗑️  上次批量分析未完成且无法在目标日 "
              f"{_batch_state.get('target_date')} 开盘（9:00）前补完，已去除该批次"
              f"（等待收盘后新一轮分析）")

    # ─── 错过补跑：今天已过触发点但从未尝试的任务，安排一次性执行 ───
    # 场景：15:10 后才重启服务（更新代码/宕机恢复），Cron 只会等明天，
    # 当天的收盘分析会静默丢失。延迟 60 秒执行，让应用完全就绪（skill 检查等）。
    _job_funcs = {
        "daily_cleanup": cleanup_job,
        "after_market_analysis": after_market_analysis_job,
        "morning_push": morning_push_job,
    }
    missed = _catchup_decisions(_read_sched_state(), now_cn())
    if _resume_action in ("resume", "defer") and "after_market_analysis" in missed:
        missed.remove("after_market_analysis")  # 续跑已覆盖，避免双批
    for job_id in missed:
        run_at = now_cn() + timedelta(seconds=60)
        scheduler.add_job(
            _job_funcs[job_id],
            trigger="date",
            run_date=run_at,
            id=f"catchup_{job_id}",
            name=f"补跑-{job_id}",
            replace_existing=True,
        )
    if missed:
        print(f"   ⏰ 检测到今日错过的任务，60 秒后补跑: {', '.join(missed)}")


def stop_scheduler():
    """停止调度器"""
    scheduler.shutdown()
    print("❌ 定时任务调度器已停止")
