"""
批量分析批次状态落盘 · 断点续跑支撑

背景：auto_analyze_and_recommend 的批次是纯内存流程，进程重启后
（更新代码/宕机/OOM）批次上下文全部丢失——僵尸任务由
recover_zombie_tasks 标 failed，但"这批要分析哪些股票、哪些已完成"
无人记得，只能整批重来。

本模块把批次快照写入 data/.batch-state.json：
- 批次启动选股完成后写入（stocks + task_ids + target_date）
- 全流程成功结束后标记 done
- 重启后若状态仍有效（is_batch_resumable），批次流程复用已完成任务、
  只重建未完成的任务，实现断点续跑

单文件单批次：收盘后分析每天最多一批，后一批直接覆盖前一批。
"""
import json
import os
import tempfile
from typing import List, Optional

from app.config import DATA_DIR, now_cn

BATCH_STATE_FILE = DATA_DIR / ".batch-state.json"

# 续跑截止（北京时间小时）：目标日收盘临近后再补"当日推荐"已无意义，
# 与 scheduler.MORNING_PUSH_CATCHUP_CUTOFF_HOUR 语义一致
BATCH_RESUME_CUTOFF_HOUR = 15


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


def is_batch_resumable(state: Optional[dict], now=None) -> bool:
    """纯函数：判断批次状态是否值得续跑。

    条件：
    - 状态存在且 status == running（done 表示已正常收尾）
    - stocks 非空
    - 目标日期未过：target_date > 今天，或 == 今天且未到 15 点
      （目标日收盘临近后推荐已失去时效，放弃续跑等下一批）
    """
    if not state or state.get("status") != "running":
        return False
    if not state.get("stocks"):
        return False

    target_date = state.get("target_date") or ""
    now = now or now_cn()
    today = now.strftime("%Y-%m-%d")
    if target_date > today:
        return True
    if target_date == today and now.hour < BATCH_RESUME_CUTOFF_HOUR:
        return True
    return False
