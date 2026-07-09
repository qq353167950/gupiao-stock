"""
批量分析批次状态落盘 · 断点续跑支撑

背景：auto_analyze_and_recommend 的批次是纯内存流程，进程重启后
（更新代码/宕机/OOM）批次上下文全部丢失——僵尸任务由
recover_zombie_tasks 标 failed，但"这批要分析哪些股票、哪些已完成"
无人记得，只能整批重来。

本模块把批次快照写入 data/.batch-state.json：
- 批次启动选股完成后写入（stocks + task_ids + target_date）
- 全流程成功结束后标记 done；时效丢失（开盘前补不完）标记 abandoned
- 重启后由 decide_resume_action 统一决策：立即续跑 / 延后到 15:00 后 /
  去除批次等新一轮——续跑时复用已完成任务、只重建未完成的任务

单文件单批次：收盘后分析每天最多一批，后一批直接覆盖前一批。
"""
import json
import os
import tempfile
from datetime import datetime
from typing import List, Optional

from app.config import DATA_DIR, now_cn

BATCH_STATE_FILE = DATA_DIR / ".batch-state.json"

# 推荐时效截止：目标日开盘前（北京时间 9:00）。
# 9:00 起进入交易时段窗口（9:00-15:00 不启动续跑），推荐必须在此之前就绪；
# 预估跑不完就整批去除（abandoned），等当天 15:10 的新一轮分析。
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 0

# 交易时段窗口（北京时间 9:00-15:00）：该时段内不启动批量续跑
MARKET_SESSION_START_HOUR = 9
MARKET_SESSION_END_HOUR = 15


def save_batch_state(
    target_date: str,
    recommendation_type: str,
    depth: str,
    stocks: List[dict],
    status: str = "running",
) -> None:
    """原子写入批次状态。

    stocks 每项: {ticker, name, sector, task_id}。
    写失败仅告警不抛异常——落盘是增强能力，不能阻断分析主流程。
    """
    state = {
        "target_date": target_date,
        "recommendation_type": recommendation_type,
        "depth": depth,
        "status": status,
        "stocks": stocks,
        "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        # 与状态文件同目录的临时文件 + replace：原子替换（跨盘 rename 会失败），
        # 避免写一半崩溃留下损坏 JSON
        fd, tmp_path = tempfile.mkstemp(
            dir=str(BATCH_STATE_FILE.parent), prefix=".batch-state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, ensure_ascii=False, indent=2))
            os.replace(tmp_path, BATCH_STATE_FILE)
        except BaseException:
            # 清理残留临时文件后再抛给外层统一告警
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"⚠️  批次状态写入失败（重启后无法断点续跑）: {e}")


def load_batch_state() -> Optional[dict]:
    """读批次状态，不存在/损坏/结构异常返回 None"""
    if not BATCH_STATE_FILE.exists():
        return None
    try:
        state = json.loads(BATCH_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("stocks"), list):
            return None
        return state
    except Exception:
        return None


def mark_batch_done(state: dict) -> None:
    """批次全流程结束后标记 done（保留文件用于排障，覆盖式写入）"""
    save_batch_state(
        target_date=state.get("target_date", ""),
        recommendation_type=state.get("recommendation_type", ""),
        depth=state.get("depth", ""),
        stocks=state.get("stocks", []),
        status="done",
    )


def mark_batch_abandoned(state: dict) -> None:
    """批次去除标记 abandoned：预估开盘前补不完/已开盘，续跑失去时效。

    去除后该批次彻底出局（重启不再评估），等当天 15:10 收盘分析开新一轮；
    保留文件供 _diagnose_no_recommendation 解释"今天为什么没推荐"。
    """
    save_batch_state(
        target_date=state.get("target_date", ""),
        recommendation_type=state.get("recommendation_type", ""),
        depth=state.get("depth", ""),
        stocks=state.get("stocks", []),
        status="abandoned",
    )


# 各深度单只股票预估耗时（分钟），与 auto_analyze_and_recommend.PER_TASK_MINUTES
# 保持同一口径（batch_state 不反向依赖批量模块，避免循环导入）
PER_TASK_MINUTES = {"quick": 7, "standard": 25, "deep": 50}


def estimate_remaining_minutes(state: dict) -> int:
    """估算续跑还需多少分钟：未完成股票数 / 并发数 × 单只耗时。

    已 completed 的任务续跑时直接复用，不计入耗时；task_id 为空或
    非 completed 的都要重跑。这里不查库（纯函数便于测试），保守假设
    快照中所有股票都要重跑——高估无害（放弃续跑），低估会白跑。
    """
    from app.config import MAX_CONCURRENT_TASKS

    per_task = PER_TASK_MINUTES.get(state.get("depth"), 25)
    count = len(state.get("stocks") or [])
    import math
    return math.ceil(count / max(1, MAX_CONCURRENT_TASKS)) * per_task


def decide_resume_action(state: Optional[dict], now=None) -> str:
    """纯函数：决定未完成批次的处置动作（启动决策与执行前二次评估共用）。

    返回四种动作：
    - "resume"  立即续跑：时效充足且不在交易时段窗口
    - "defer"   延后续跑：批次时效未过，但现在处于 9:00-15:00 窗口
                （仅发生在目标日之前的休市日盘中重启，如周六上午续周一的批次），
                调用方应安排到当天 15:00 后再续
    - "abandon" 去除批次：预估无法在目标日开盘（9:00）前补完、目标日已过
                或快照数据不可信——调用方应 mark_batch_abandoned 后等
                当天 15:10 收盘分析开新一轮（绝不隔天续跑旧批次）
    - "none"    无事可做：无快照 / 已 done / 已 abandoned / stocks 为空

    判定顺序刻意先时效后窗口：目标日当天 9:00 后必然先命中时效不足
    （deadline 即 9:00）判为 abandon，自然满足"今天不能还续跑昨天的批次"。
    """
    if not state or state.get("status") != "running":
        return "none"
    if not state.get("stocks"):
        return "none"

    target_date = state.get("target_date") or ""
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return "abandon"  # 目标日期缺失/格式错误，快照不可信，去除等新一轮

    now = now or now_cn()
    deadline = target.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE)
    # naive/aware 对齐：now_cn 带时区语义已剥离，测试传入的 datetime 可能带 tzinfo
    if getattr(now, "tzinfo", None) is not None:
        deadline = deadline.replace(tzinfo=now.tzinfo)

    from datetime import timedelta
    finish_estimate = now + timedelta(minutes=estimate_remaining_minutes(state))
    if finish_estimate > deadline:
        return "abandon"

    # 交易时段窗口（9:00-15:00）不启动续跑。走到这里时效必然充足，
    # 意味着目标日在未来（目标日当天 9:00 后已被上面判为 abandon）
    if MARKET_SESSION_START_HOUR <= now.hour < MARKET_SESSION_END_HOUR:
        return "defer"

    return "resume"
