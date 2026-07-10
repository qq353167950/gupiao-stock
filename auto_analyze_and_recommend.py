"""
自动分析推荐引擎
基于真实分析结果生成推荐，完成后通过已配置渠道推送

调用方式：
- 定时调度：app/scheduler.py 在收盘后以协程方式直接调用（同进程共享并发信号量）
- 手动执行：python auto_analyze_and_recommend.py [quick|standard|deep] [morning|noon]
"""
import sys
from pathlib import Path

# 保证脚本直跑时能导入 app 包（基于文件位置动态解析，不依赖部署路径）
BASE_DIR = Path(__file__).parent.resolve()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import asyncio
import html
import math
from datetime import timedelta
from typing import Dict, List

from app.config import (
    STOCKS_PER_SECTOR,
    DAILY_ANALYSIS_TARGET_COUNT,
    STRONG_RECOMMEND_LIMIT,
    RECOMMEND_LIMIT,
    OBSERVE_LIMIT,
    MAX_CONCURRENT_TASKS,
    NOTIFY_ON_ANALYSIS_COMPLETE,
    PUBLIC_BASE_URL,
    now_cn,
)
from app.batch_state import save_batch_state, load_batch_state, mark_batch_done
from app.database import SessionLocal, DailyRecommendation, AnalysisTask
from app.task_manager import create_task, format_stock_display_name
from app.recommendation_engine import calculate_composite_score, evaluate_recommendation

# 各深度单只股票预估耗时（分钟），用于计算最大等待时间
PER_TASK_MINUTES = {"quick": 7, "standard": 25, "deep": 50}


def _determine_target_date() -> str:
    """确定推荐目标日期。

    - 分析在交易日凌晨（开盘前）完成 → 推荐给今天
    - 其余情况（收盘后启动、深夜完成）→ 推荐给下一个交易日
    """
    from app.trading_calendar import is_trading_day, get_next_trading_day

    now = now_cn()
    if is_trading_day(now) and now.hour < 9:
        return now.strftime("%Y-%m-%d")

    next_trading = get_next_trading_day(now)
    if next_trading:
        return next_trading.strftime("%Y-%m-%d")
    # 极端兜底：日历不可用时按自然日+1
    return (now + timedelta(days=1)).strftime("%Y-%m-%d")


async def _wait_for_tasks(task_ids: List[str], depth: str) -> Dict[str, int]:
    """轮询等待任务批次进入终态（completed/failed），返回统计。

    终态判断同时计入 failed，避免个别任务失败导致空等到最大时限。
    """
    if not task_ids:
        return {"completed": 0, "failed": 0}

    per_task = PER_TASK_MINUTES.get(depth, 25)
    # 并发执行下的预估总时长 + 60 分钟余量
    estimated_minutes = math.ceil(len(task_ids) / max(1, MAX_CONCURRENT_TASKS)) * per_task
    max_wait_minutes = estimated_minutes + 60

    print(f"   并发数 {MAX_CONCURRENT_TASKS}，预计 {estimated_minutes} 分钟，最长等待 {max_wait_minutes} 分钟")

    completed = failed = 0
    for wait_count in range(1, max_wait_minutes + 1):
        await asyncio.sleep(60)

        db = SessionLocal()
        try:
            completed = db.query(AnalysisTask).filter(
                AnalysisTask.task_id.in_(task_ids),
                AnalysisTask.status == "completed"
            ).count()
            failed = db.query(AnalysisTask).filter(
                AnalysisTask.task_id.in_(task_ids),
                AnalysisTask.status == "failed"
            ).count()
        finally:
            db.close()

        done = completed + failed
        progress = (done / len(task_ids)) * 100
        if wait_count % 5 == 0 or done == len(task_ids):
            print(f"   进度: 完成{completed} 失败{failed} / {len(task_ids)} "
                  f"({progress:.1f}%) - 已等待 {wait_count} 分钟")

        if done == len(task_ids):
            print(f"\n✅ 批次全部进入终态（成功 {completed}，失败 {failed}）")
            break
    else:
        print(f"\n⚠️  达到最大等待时限：完成 {completed}，失败 {failed}，"
              f"未终态 {len(task_ids) - completed - failed}")
        print("   将基于已完成的分析生成推荐")

    return {"completed": completed, "failed": failed}


def _generate_recommendations(
    analysis_tasks: List[dict], target_date: str, recommendation_type: str
) -> Dict[str, List[dict]]:
    """按细分板块生成推荐并写库，返回 {细分板块: [推荐dict]}。

    全局去重：股票池中 6 只股票横跨两个细分板块（如科大讯飞属"软件互联网"+"人工智能"），
    先按全板块候选统一排序，每只股票只归入其排名最先出现的板块，避免重复推荐。
    """
    from app.stock_pool import A_STOCK_POOL

    db = SessionLocal()
    try:
        # 清除目标日期的旧推荐（重跑幂等）
        db.query(DailyRecommendation).filter(
            DailyRecommendation.date == target_date,
            DailyRecommendation.recommendation_type == recommendation_type
        ).delete()
        db.commit()

        batch_task_ids = [t['task_id'] for t in analysis_tasks]

        # 第一遍：收集各细分板块的候选（含评分），暂不写库
        sector_candidates: Dict[str, List[dict]] = {}
        for sub_sector, stocks_in_pool in A_STOCK_POOL.items():
            sub_sector_task_ids = [
                t['task_id'] for t in analysis_tasks if t['ticker'] in stocks_in_pool
            ]
            if not sub_sector_task_ids:
                continue

            sub_sector_tasks = db.query(AnalysisTask).filter(
                AnalysisTask.task_id.in_(sub_sector_task_ids),
                AnalysisTask.status == "completed"
            ).all()
            if not sub_sector_tasks:
                continue

            scored = []
            for task in sub_sector_tasks:
                composite_score, reason, period = calculate_composite_score(task)
                evaluation = evaluate_recommendation(task)
                if evaluation["recommendation_level"] == "回避":
                    continue
                scored.append({
                    "task": task,
                    "score": composite_score,
                    "reason": reason,
                    "period": period,
                    "evaluation": evaluation,
                })
            scored.sort(key=lambda x: x["score"], reverse=True)
            sector_candidates[sub_sector] = scored

        # 第二遍：按候选评分从高到低全局去重，再按推荐层级名额截断。
        all_candidates = [
            (sub_sector, item)
            for sub_sector, items in sector_candidates.items()
            for item in items
        ]
        all_candidates.sort(key=lambda x: x[1]["score"], reverse=True)

        used_tickers = set()
        level_limits = {
            "强推荐": STRONG_RECOMMEND_LIMIT,
            "推荐": RECOMMEND_LIMIT,
            "观察": OBSERVE_LIMIT,
        }
        level_counts = {level: 0 for level in level_limits}
        sector_picks: Dict[str, List[dict]] = {}
        skipped_candidates = []
        for sub_sector, item in all_candidates:
            ticker = item["task"].ticker
            if ticker in used_tickers:
                continue
            used_tickers.add(ticker)
            level = item["evaluation"]["recommendation_level"]
            if level not in level_limits:
                skipped_candidates.append({
                    "ticker": ticker,
                    "name": format_stock_display_name(item["task"].name, ticker),
                    "score": item["score"],
                    "level": level,
                    "reason": item["evaluation"].get("exclude_reason") or "层级未进入展示范围",
                })
                continue
            if level_counts[level] >= level_limits[level]:
                skipped_candidates.append({
                    "ticker": ticker,
                    "name": format_stock_display_name(item["task"].name, ticker),
                    "score": item["score"],
                    "level": level,
                    "reason": f"{level}名额已满",
                })
                continue
            picks = sector_picks.setdefault(sub_sector, [])
            picks.append(item)
            level_counts[level] += 1

        # 第三遍：按板块写库（保持 A_STOCK_POOL 定义顺序，排名连续）
        sub_sector_recommendations: Dict[str, List[dict]] = {}
        global_rank = 1
        for sub_sector in A_STOCK_POOL:
            picks = sector_picks.get(sub_sector)
            if not picks:
                continue

            sub_sector_recs = []
            for item in picks:
                task = item["task"]

                bullish_ratio = None
                if task.bullish_count and task.total_voters:
                    bullish_ratio = task.bullish_count / task.total_voters

                reason_text = f"{item['period']} · {item['reason']}"
                evaluation = item["evaluation"]

                recommendation = DailyRecommendation(
                    date=target_date,
                    ticker=task.ticker,
                    name=format_stock_display_name(task.name, task.ticker),
                    rank=global_rank,
                    score=item["score"],
                    dcf_discount=task.dcf_discount,
                    bullish_ratio=bullish_ratio,
                    reason=reason_text,
                    recommendation_type=recommendation_type,
                    is_archived=False
                )
                db.add(recommendation)
                global_rank += 1

                sub_sector_recs.append({
                    'name': format_stock_display_name(task.name, task.ticker),
                    'ticker': task.ticker,
                    'score': item["score"],
                    'reason': reason_text,
                    'sector': sub_sector,
                    'risk_level': task.risk_level,
                    'recommendation_level': evaluation["recommendation_level"],
                    'data_quality': evaluation["data_quality"],
                    'factor_scores': evaluation["factor_scores"],
                })

            sub_sector_recommendations[sub_sector] = sub_sector_recs

        db.commit()
        if skipped_candidates:
            sub_sector_recommendations["__skipped__"] = skipped_candidates
        return sub_sector_recommendations
    finally:
        db.close()


def _write_batch_report(
    target_date: str,
    recommendation_type: str,
    analysis_tasks: List[dict],
    stats: Dict[str, int],
    recommendations: List[dict],
) -> str:
    """生成批量推荐最终 HTML 报告，返回可通过 /reports 访问的相对路径。"""
    from app.config import SKILL_REPORTS_DIR, PUBLIC_BASE_URL, now_cn

    report_dir = SKILL_REPORTS_DIR / f"batch-{target_date}-{recommendation_type}"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / "recommendations-report.html"

    task_rows = []
    db = SessionLocal()
    try:
        task_ids = [t["task_id"] for t in analysis_tasks if t.get("task_id")]
        tasks = db.query(AnalysisTask).filter(AnalysisTask.task_id.in_(task_ids)).all() if task_ids else []
        task_map = {task.task_id: task for task in tasks}
        for item in analysis_tasks:
            task = task_map.get(item.get("task_id"))
            name = format_stock_display_name(
                (task.name if task else item.get("name")),
                item.get("ticker", ""),
            )
            evaluation = evaluate_recommendation(task) if task else None
            score = evaluation["final_score"] if evaluation else None
            task_rows.append({
                "name": name,
                "ticker": item.get("ticker", ""),
                "status": task.status if task else "missing",
                "score": score,
                "risk_level": task.risk_level if task else "",
                "recommendation_level": evaluation["recommendation_level"] if evaluation else "未分析",
                "data_quality": evaluation["data_quality"] if evaluation else "D",
                "exclude_reason": evaluation.get("exclude_reason") if evaluation else "任务缺失",
                "factor_scores": evaluation["factor_scores"] if evaluation else {},
                "report_path": task.report_path if task else "",
            })
    finally:
        db.close()

    rec_rows = sorted(
        recommendations,
        key=lambda r: r.get("score") if isinstance(r.get("score"), (int, float)) else 0,
        reverse=True,
    )
    generated_at = now_cn().strftime("%Y-%m-%d %H:%M:%S")

    def fmt_score(value):
        return f"{value:.1f}" if isinstance(value, (int, float)) else "-"

    rec_html = "".join(
        f"""
        <tr>
          <td>{idx}</td>
          <td>{html.escape(rec.get('name') or rec.get('ticker', ''))}</td>
          <td>{html.escape(rec.get('sector', ''))}</td>
          <td>{html.escape(rec.get('recommendation_level', ''))}</td>
          <td>{fmt_score(rec.get('score'))}</td>
          <td>{html.escape(rec.get('data_quality', ''))}</td>
          <td>{html.escape(rec.get('risk_level') or '')}</td>
          <td>{html.escape(rec.get('reason') or '')}</td>
        </tr>
        """
        for idx, rec in enumerate(rec_rows, 1)
    ) or '<tr><td colspan="8" class="empty">本批次暂无股票通过推荐筛选</td></tr>'

    skipped_rows = [
        {
            "name": item.get("name") or item.get("ticker", ""),
            "score": item.get("score"),
            "recommendation_level": item.get("level", ""),
            "data_quality": "",
            "exclude_reason": item.get("reason", "综合排名靠后"),
        }
        for item in recommendations
        if item.get("skipped")
    ]
    not_selected_rows = [row for row in task_rows if row["recommendation_level"] in ("谨慎", "回避")] + skipped_rows
    not_selected_html = "".join(
        f"""
        <tr>
          <td>{html.escape(row['name'])}</td>
          <td>{fmt_score(row['score'])}</td>
          <td>{html.escape(row['recommendation_level'])}</td>
          <td>{html.escape(row['data_quality'])}</td>
          <td>{html.escape(row['exclude_reason'] or '综合排名靠后')}</td>
        </tr>
        """
        for row in not_selected_rows
    ) or '<tr><td colspan="5" class="empty">本批次无明确淘汰项</td></tr>'

    task_html = "".join(
        f"""
        <tr>
          <td>{html.escape(row['name'])}</td>
          <td>{html.escape(row['status'])}</td>
          <td>{html.escape(row['recommendation_level'])}</td>
          <td>{fmt_score(row['score'])}</td>
          <td>{html.escape(row['data_quality'])}</td>
          <td>{html.escape(row['risk_level'] or '')}</td>
          <td>{'<a href="/reports/' + html.escape(row['report_path']) + '" target="_blank">查看个股报告</a>' if row['report_path'] else '-'}</td>
        </tr>
        """
        for row in task_rows
    )

    report_file.write_text(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(target_date)} 批量推荐分析报告</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f4ef; color: #1f2421; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 32px 20px; }}
    .card {{ background: #fff; border: 1px solid #e7e1d7; border-radius: 18px; padding: 24px; margin-bottom: 20px; box-shadow: 0 10px 30px rgba(0,0,0,.04); }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    h2 {{ margin: 0 0 16px; font-size: 20px; }}
    .meta {{ color: #6b7280; margin: 0; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 20px; }}
    .stat {{ background: #fbf9f5; border-radius: 14px; padding: 16px; }}
    .stat strong {{ display: block; font-size: 26px; color: #c4612f; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 12px; border-bottom: 1px solid #eee7dc; text-align: left; vertical-align: top; }}
    th {{ color: #5c635d; font-size: 13px; background: #fbf9f5; }}
    a {{ color: #c4612f; text-decoration: none; font-weight: 600; }}
    .empty {{ text-align: center; color: #6b7280; padding: 28px; }}
    .note {{ line-height: 1.7; color: #5c635d; }}
  </style>
</head>
<body>
  <main>
    <section class="card">
      <h1>{html.escape(target_date)} 批量推荐分析报告</h1>
      <p class="meta">推荐类型：{html.escape(recommendation_type)} · 生成时间：{generated_at}</p>
      <div class="stats">
        <div class="stat"><span>分析任务</span><strong>{len(analysis_tasks)}</strong></div>
        <div class="stat"><span>成功完成</span><strong>{stats.get('completed', 0)}</strong></div>
        <div class="stat"><span>失败/超时</span><strong>{stats.get('failed', 0)}</strong></div>
        <div class="stat"><span>入选推荐</span><strong>{len(recommendations)}</strong></div>
      </div>
    </section>
    <section class="card">
      <h2>筛选规则说明</h2>
      <p class="note">系统先按三池模型生成每日约 {DAILY_ANALYSIS_TARGET_COUNT} 只候选股票：动量池捕捉市场热点，质量池用免费行情数据代理中长期候选，轮动池补充近期未分析股票。分析完成后按基本面、估值、成长、资金、趋势、风险、催化七类因子生成最终推荐分，并标记数据完整度 A/B/C/D。最终展示名额为强推荐 {STRONG_RECOMMEND_LIMIT} 只、推荐 {RECOMMEND_LIMIT} 只、观察 {OBSERVE_LIMIT} 只。</p>
    </section>
    <section class="card">
      <h2>最终推荐</h2>
      <table><thead><tr><th>排名</th><th>股票</th><th>板块</th><th>层级</th><th>评分</th><th>数据完整度</th><th>风险</th><th>推荐理由</th></tr></thead><tbody>{rec_html}</tbody></table>
    </section>
    <section class="card">
      <h2>未入选原因</h2>
      <table><thead><tr><th>股票</th><th>评分</th><th>层级</th><th>数据完整度</th><th>原因</th></tr></thead><tbody>{not_selected_html}</tbody></table>
    </section>
    <section class="card">
      <h2>批量分析明细</h2>
      <table><thead><tr><th>股票</th><th>状态</th><th>层级</th><th>评分</th><th>数据完整度</th><th>风险</th><th>个股报告</th></tr></thead><tbody>{task_html}</tbody></table>
    </section>
    <section class="card">
      <p class="note">平台地址：{html.escape(PUBLIC_BASE_URL)}。以上内容仅供研究参考，投资决策请结合自身风险承受能力。</p>
    </section>
  </main>
</body>
</html>
""", encoding="utf-8")

    return f"{report_dir.name}/{report_file.name}"


async def auto_analyze_and_recommend(
    depth: str = "standard",
    recommendation_type: str = "morning",
    resume_state: dict = None,
):
    """
    自动分析并生成推荐

    Args:
        depth: 分析深度（quick/standard/deep）
        recommendation_type: 推荐类型（morning/noon）
        resume_state: 断点续跑状态（app.batch_state.load_batch_state 的返回值）。
            提供时跳过选股，复用状态中的股票清单：已 completed 的任务直接复用，
            其余（重启僵尸/失败/丢失）重建任务，实现批次级断点续跑。
    """
    from app.stock_pool import get_major_sector
    from app import notifier

    resuming = bool(resume_state)
    if resuming:
        # 续跑沿用原批次的深度与推荐类型，保证任务口径一致
        depth = resume_state.get("depth") or depth
        recommendation_type = resume_state.get("recommendation_type") or recommendation_type

    print(f"\n{'='*70}")
    print(f"🔥 {'♻️ 断点续跑' if resuming else '开始'}"
          f"{'早盘' if recommendation_type == 'morning' else '午盘'}推荐流程")
    print(f"   时间: {now_cn().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   深度: {depth} | 每日目标分析数: {DAILY_ANALYSIS_TARGET_COUNT}")
    print(f"{'='*70}\n")

    loop = asyncio.get_running_loop()

    if resuming:
        # 续跑：跳过选股，直接复用状态中的股票清单
        print("📊 步骤1：断点续跑，复用上次批次的股票清单...")
        flat_stocks = [
            s for s in resume_state["stocks"]
            if isinstance(s, dict) and s.get("ticker")
        ]
        target_date = resume_state.get("target_date") or _determine_target_date()
        print(f"✓ 恢复 {len(flat_stocks)} 只股票（目标日期 {target_date}）\n")
    else:
        # 1. 获取热门股票（同步 requests 逐批抓取耗时数十秒，放线程池避免阻塞事件循环）
        # 惰性导入：market_data 依赖 pandas，续跑路径与测试环境无需加载
        from app.market_data import get_sector_hot_stocks

        print("📊 步骤1：获取市场热门股票...")
        sector_stocks = await loop.run_in_executor(
            None, lambda: get_sector_hot_stocks(
                top_n_per_major_sector=STOCKS_PER_SECTOR,
                target_total=DAILY_ANALYSIS_TARGET_COUNT,
            )
        )

        if not sector_stocks:
            print("⚠️  未能选出热门股票，流程终止")
            return

        total = sum(len(stocks) for stocks in sector_stocks.values())
        print(f"✓ 选出 {len(sector_stocks)} 个大板块，共 {total} 只热门股票\n")

        # 扁平化 + 跨板块去重（同一股票只分析一次），并预估推荐目标日期
        flat_stocks = []
        seen = set()
        for sector, stocks in sector_stocks.items():
            for stock in stocks:
                if stock['ticker'] in seen:
                    continue
                seen.add(stock['ticker'])
                flat_stocks.append({
                    'ticker': stock['ticker'],
                    'name': stock['name'],
                    'sector': sector,
                    'task_id': None,
                })
        target_date = _determine_target_date()

    # 2. 创建分析任务（续跑时：已 completed 的任务直接复用，其余重建）
    print("🔍 步骤2：创建分析任务...")

    # 续跑时批量查询原任务状态，已完成的直接复用（不重新排队）
    reusable: Dict[str, str] = {}  # task_id -> status
    if resuming:
        old_ids = [s['task_id'] for s in flat_stocks if s.get('task_id')]
        if old_ids:
            db = SessionLocal()
            try:
                rows = db.query(AnalysisTask.task_id, AnalysisTask.status).filter(
                    AnalysisTask.task_id.in_(old_ids)
                ).all()
                reusable = {tid: status for tid, status in rows}
            finally:
                db.close()

    analysis_tasks = []
    pending_task_ids = []  # 本次真正需要等待的任务（不含复用的已完成任务）
    for stock in flat_stocks:
        old_id = stock.get('task_id')
        if old_id and reusable.get(old_id) == "completed":
            stock_entry = {
                'task_id': old_id,
                'sector': stock.get('sector', ''),
                'ticker': stock['ticker'],
                'name': stock.get('name', stock['ticker']),
            }
            analysis_tasks.append(stock_entry)
            print(f"  ♻️ {stock_entry['name']} ({stock['ticker']}) 已完成，复用任务 {old_id}")
            continue
        try:
            task_id = create_task(stock['ticker'], depth=depth, name=stock.get('name'))
            stock['task_id'] = task_id  # 回写清单，供落盘后续跑使用
            analysis_tasks.append({
                'task_id': task_id,
                'sector': stock.get('sector', ''),
                'ticker': stock['ticker'],
                'name': stock.get('name', stock['ticker']),
            })
            pending_task_ids.append(task_id)
            print(f"  ✓ {stock.get('name', stock['ticker'])} ({stock['ticker']}) - 任务ID: {task_id}")
        except Exception as e:
            print(f"  ✗ {stock.get('name', stock['ticker'])} 创建任务失败: {e}")

    if not analysis_tasks:
        print("⚠️  没有成功创建任何分析任务，流程终止")
        return

    reused = len(analysis_tasks) - len(pending_task_ids)
    print(f"\n✓ 共 {len(analysis_tasks)} 个分析任务"
          f"（新建 {len(pending_task_ids)}，复用已完成 {reused}）")

    # 落盘批次状态：重启后 scheduler 据此断点续跑
    save_batch_state(
        target_date=target_date,
        recommendation_type=recommendation_type,
        depth=depth,
        stocks=flat_stocks,
    )

    # 3. 等待分析完成（只等本次新建的任务）
    print(f"\n⏳ 步骤3：等待分析完成...")
    stats = await _wait_for_tasks(pending_task_ids, depth)
    stats["completed"] += reused  # 汇总口径包含复用的已完成任务

    # 4. 生成推荐（按细分板块）
    print(f"\n📋 步骤4：生成推荐（按细分板块）...")
    print(f"   推荐目标日期：{target_date}")

    sub_sector_recommendations = _generate_recommendations(
        analysis_tasks, target_date, recommendation_type
    )

    # 推荐已写库，批次核心目标达成 → 标记 done（后续推送失败不应触发整批重跑）
    mark_batch_done(load_batch_state() or {
        "target_date": target_date,
        "recommendation_type": recommendation_type,
        "depth": depth,
        "stocks": flat_stocks,
    })

    # 5. 输出结果
    print(f"\n{'='*70}")
    print(f"✅ {'早盘' if recommendation_type == 'morning' else '午盘'}推荐生成完成！")
    print(f"{'='*70}\n")

    all_recs = []
    skipped_recs = []
    major_sectors: Dict[str, Dict[str, List[dict]]] = {}
    for sub_sector, recs in sub_sector_recommendations.items():
        if sub_sector == "__skipped__":
            skipped_recs = [{**rec, "skipped": True} for rec in recs]
            continue
        all_recs.extend(recs)
        major = get_major_sector(sub_sector)
        major_sectors.setdefault(major, {})[sub_sector] = recs

    for major_sector, sub_sectors in major_sectors.items():
        print(f"【{major_sector}】")
        for sub_sector, recs in sub_sectors.items():
            print(f"  ├─ {sub_sector}")
            for i, rec in enumerate(recs, 1):
                emoji = "🥇" if i == 1 else "⭐"
                print(f"     {emoji} {i}. {rec['name']} ({rec['ticker']})")
                print(f"        评分: {rec['score']:.1f} | {rec['reason']}")
        print()

    print(f"共 {len(sub_sector_recommendations)} 个细分板块，{len(all_recs)} 只推荐股票")
    batch_report_path = _write_batch_report(
        target_date, recommendation_type, analysis_tasks, stats, all_recs + skipped_recs
    )
    print(f"批量推荐最终报告：{PUBLIC_BASE_URL}/reports/{batch_report_path}")
    print(f"\n🌐 访问平台查看完整推荐：{PUBLIC_BASE_URL}")

    # 6. 完成推送（可通过 NOTIFY_ON_ANALYSIS_COMPLETE=false 关闭，只保留早盘定时推送）
    if NOTIFY_ON_ANALYSIS_COMPLETE:
        print(f"\n📨 步骤5：推送分析结果...")
        top_stocks = sorted(
            all_recs,
            key=lambda r: r['score'] if isinstance(r.get('score'), (int, float)) else 0,
            reverse=True,
        )
        summary = notifier.format_analysis_summary(
            date=target_date,
            total=len(analysis_tasks),
            completed=stats["completed"],
            failed=stats["failed"],
            top_stocks=top_stocks,
        )
        digest = notifier.format_daily_digest(target_date, all_recs)
        # notifier.send 为同步 requests 调用（含失败重试 sleep），走线程池避免阻塞事件循环
        await loop.run_in_executor(
            None,
            notifier.send,
            f"📊 {target_date} 分析完成（{len(all_recs)} 只推荐）",
            f"{summary}\n\n---\n\n{digest}",
        )


if __name__ == "__main__":
    # 从命令行参数获取配置
    depth = sys.argv[1] if len(sys.argv) > 1 else "standard"
    rec_type = sys.argv[2] if len(sys.argv) > 2 else "morning"

    if depth not in ("quick", "standard", "deep"):
        print(f"❌ 无效深度: {depth}（可选: quick/standard/deep）")
        sys.exit(1)

    asyncio.run(auto_analyze_and_recommend(depth, rec_type))
