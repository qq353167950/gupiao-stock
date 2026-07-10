"""
分析任务管理
- 并发控制：批量分析（定时推荐，owner 为空）与 Web 手动分析（owner 非空）
  使用相互独立的 asyncio.Semaphore 通道，互不排队——推荐批次跑满时用户仍可即时分析
- 数据库写入：全部走短会话（_update_task），避免长事务导致 SQLite 锁竞争
"""
import asyncio
import uuid
import json
import re
import concurrent.futures
from typing import Optional, Dict, Any

from app.config import SKILL_REPORTS_DIR, MAX_CONCURRENT_TASKS, MANUAL_CONCURRENT_TASKS, now_cn

from app.database import SessionLocal, AnalysisTask
from app.enhanced_analyzer import EnhancedAnalyzer

# 双通道并发限流：批量与手动各自独立排队，峰值总并发 = 两者之和
# （Python 3.10+ 的 Semaphore 不再绑定创建时的事件循环，模块级创建安全）
_batch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
_manual_semaphore = asyncio.Semaphore(MANUAL_CONCURRENT_TASKS)

# 后台任务强引用，防止 asyncio.Task 被垃圾回收中途取消
_background_tasks: set = set()

# 分析子进程专用共享线程池：容量与两通道总并发一致，保证任一通道
# 拿到信号量后立即有线程可用，不会跨通道互相占用
_analysis_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_TASKS + MANUAL_CONCURRENT_TASKS,
    thread_name_prefix="analysis",
)


def parse_ticker(input_str: str) -> Optional[str]:
    """解析股票代码或名称，返回标准格式的代码"""
    # 移除空格
    input_str = input_str.strip()

    # 如果是标准格式（已带后缀）
    if re.match(r'^\d{6}\.(SH|SZ)$', input_str):
        return input_str

    # 如果只是6位数字，需要判断市场（简化处理：6开头=SH，其他=SZ）
    if re.match(r'^\d{6}$', input_str):
        if input_str.startswith('6'):
            return f"{input_str}.SH"
        else:
            return f"{input_str}.SZ"

    # 美股（字母组成，统一转大写以兼容小写输入）
    if re.match(r'^[A-Za-z]+$', input_str):
        return input_str.upper()

    # 中文名称或其他格式，直接返回让 deep-analysis 处理
    return input_str


def format_stock_display_name(name: Optional[str], ticker: str) -> str:
    """统一股票展示名：股票名称（股票号码）。"""
    clean_name = (name or "").strip()
    clean_ticker = (ticker or "").strip()
    if not clean_name:
        return clean_ticker
    if clean_ticker and clean_ticker not in clean_name:
        return f"{clean_name}（{clean_ticker}）"
    return clean_name


def _update_task(task_id: str, **fields) -> bool:
    """短会话更新任务字段，返回是否成功（任务不存在返回 False）"""
    db = SessionLocal()
    try:
        task = db.query(AnalysisTask).filter(AnalysisTask.task_id == task_id).first()
        if not task:
            return False
        for key, value in fields.items():
            setattr(task, key, value)
        db.commit()
        return True
    finally:
        db.close()


def _get_task_field(task_id: str, field: str):
    """短会话读取单个任务字段"""
    db = SessionLocal()
    try:
        task = db.query(AnalysisTask).filter(AnalysisTask.task_id == task_id).first()
        return getattr(task, field, None) if task else None
    finally:
        db.close()


async def run_analysis(task_id: str, ticker: str, depth: str = "standard",
                       manual: bool = False):
    """运行股票分析任务（增强版：集成多维度分析，按通道限流）。

    Args:
        manual: True 走手动通道（Web 用户发起），False 走批量通道（定时推荐）。
            两通道信号量独立，互不排队。
    """
    if _get_task_field(task_id, "status") is None:
        return

    semaphore = _manual_semaphore if manual else _batch_semaphore
    channel_label = "手动" if manual else "批量"

    # 排队等待所属通道的并发槽位
    _update_task(task_id, status="pending", current_stage=f"排队中（等待{channel_label}通道并发槽位）")

    async with semaphore:
        try:
            _update_task(
                task_id,
                status="running",
                started_at=now_cn(),
                progress=5,
                current_stage="初始化增强分析引擎",
            )

            analyzer = EnhancedAnalyzer()

            _update_task(task_id, progress=10, current_stage="阶段1: 风险检测")

            # 进度模拟（在后台独立更新，仅用于前端展示）
            async def simulate_progress():
                stages = [
                    (20, "阶段1: trap-detector 风险扫描"),
                    (40, "阶段2: deep-analysis 深度分析"),
                    (60, "阶段2: 财务建模与评委评审"),
                    (80, "阶段3: lhb-analyzer 龙虎榜验证"),
                    (90, "综合评分与生成报告"),
                ]
                for progress, stage in stages:
                    await asyncio.sleep(30)
                    if _get_task_field(task_id, "status") != "running":
                        break
                    _update_task(task_id, progress=progress, current_stage=stage)

            progress_task = asyncio.create_task(simulate_progress())

            task_name = _get_task_field(task_id, "name") or ticker

            # 在共享线程池执行同步分析（避免阻塞事件循环）
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    _analysis_executor,
                    analyzer.analyze_stock,
                    ticker,
                    task_name,
                    depth,
                )
            finally:
                progress_task.cancel()

            if result:
                _update_task(task_id, progress=95, current_stage="处理分析结果")

                fields: Dict[str, Any] = {
                    "enhanced_result": json.dumps(result, ensure_ascii=False),
                    "composite_score": result.get("综合评分", 0),
                    "risk_level": result.get("风险等级", "未知"),
                }

                # 从deep-analysis结果中提取报告路径与分数
                deep_analysis = result.get("deep_analysis") or {}
                if deep_analysis.get("report_path"):
                    fields["report_path"] = deep_analysis["report_path"]
                    if isinstance(deep_analysis.get("score"), (int, float)):
                        fields["score"] = deep_analysis["score"]

                    # 补充解析报告摘要（DCF/评委看多数等）
                    report_dir = deep_analysis.get("report_dir")
                    summary = parse_analysis_summary(report_dir) if report_dir else None
                    if summary:
                        if "score" not in fields and summary.get("score") is not None:
                            fields["score"] = summary["score"]
                        fields["dcf_discount"] = summary.get("dcf_discount")
                        fields["bullish_count"] = summary.get("bullish_count")
                        fields["total_voters"] = summary.get("total_voters")
                        if summary.get("name"):
                            fields["name"] = summary["name"]

                fields.update(
                    progress=100,
                    current_stage="完成",
                    status="completed",
                    completed_at=now_cn(),
                )
                _update_task(task_id, **fields)

                # 设置报告过期时间（需要 completed_at 已写入）
                db = SessionLocal()
                try:
                    task = db.query(AnalysisTask).filter(AnalysisTask.task_id == task_id).first()
                    if task:
                        task.set_expires()
                        db.commit()
                finally:
                    db.close()
            else:
                _update_task(
                    task_id,
                    status="failed",
                    error_message="分析引擎返回空结果（可能超时或数据源不可用，详见日志）",
                )

        except Exception as e:
            _update_task(task_id, status="failed", error_message=str(e)[:500])


def parse_analysis_summary(report_dir: str) -> Optional[Dict[str, Any]]:
    """解析分析摘要（one-liner.txt）"""
    if not report_dir:
        return None
    summary_file = SKILL_REPORTS_DIR / report_dir / "one-liner.txt"

    if not summary_file.exists():
        return None

    try:
        content = summary_file.read_text(encoding='utf-8')

        # 简单解析（格式：中山公用 体检结果：54 分，观望偏空 · 4 派看空。）
        result = {}

        # 提取股票名称（首行第一个词）
        first_line = content.split('\n')[0].strip()
        name_match = re.match(r'^([一-龥A-Za-z0-9]+)', first_line)
        if name_match:
            result['name'] = name_match.group(1)

        # 提取分数
        score_match = re.search(r'(\d+)\s*分', content)
        if score_match:
            result['score'] = float(score_match.group(1))

        # 提取 DCF 低估比例
        dcf_match = re.search(r'低估\s*(\d+)%', content)
        if dcf_match:
            result['dcf_discount'] = float(dcf_match.group(1)) / 100

        # 提取看多人数
        bullish_match = re.search(r'(\d+)\s*人喊买', content)
        if bullish_match:
            result['bullish_count'] = int(bullish_match.group(1))
            result['total_voters'] = 50  # 默认50位评委

        return result

    except Exception:
        return None


def create_task(ticker: str, depth: str = "standard", owner_user_id: int = None,
                name: str = None) -> str:
    """创建分析任务并调度执行。

    Args:
        owner_user_id: 发起者用户 id。手动分析必填（历史记录按归属过滤，
            并走独立的手动并发通道）；定时批量分析留空（系统任务，仅管理员
            可见，走批量并发通道）。
        name: 股票名称。批量任务传入后用于列表与报告展示。

    必须在运行中的事件循环内调用（FastAPI 处理器 / asyncio.run 上下文）。
    """
    task_id = str(uuid.uuid4())
    parsed_ticker = parse_ticker(ticker)

    db = SessionLocal()
    try:
        task = AnalysisTask(
            task_id=task_id,
            ticker=parsed_ticker or ticker,
            name=format_stock_display_name(name, parsed_ticker or ticker) if name else None,
            depth=depth,
            status="pending",
            owner_user_id=owner_user_id,
        )
        db.add(task)
        db.commit()
    finally:
        db.close()

    # 在当前事件循环中调度分析；owner 非空 = 用户手动发起 → 手动通道
    loop = asyncio.get_running_loop()
    bg_task = loop.create_task(run_analysis(
        task_id, parsed_ticker or ticker, depth,
        manual=owner_user_id is not None,
    ))
    # 强引用防止 GC，完成后自动移除
    _background_tasks.add(bg_task)
    bg_task.add_done_callback(_background_tasks.discard)

    return task_id


def get_task_status(task_id: str) -> Optional[Dict[str, Any]]:
    """获取任务状态"""
    db = SessionLocal()
    try:
        task = db.query(AnalysisTask).filter(AnalysisTask.task_id == task_id).first()
        return task.to_dict() if task else None
    finally:
        db.close()
