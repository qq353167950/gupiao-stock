"""
批量分析断点续跑机制测试
运行：python -m pytest tests/test_batch_resume.py -v
"""
import sys
import json
import uuid
from datetime import datetime
from pathlib import Path

# Windows 控制台 GBK 编码兼容（与 test_core.py 同款处理）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 保证可导入 app 包
BASE_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(BASE_DIR))


# ─── batch_state 落盘/读取 ───

def test_save_and_load_batch_state(tmp_path, monkeypatch):
    """状态可原子落盘并完整读回"""
    from app import batch_state
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")

    stocks = [
        {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": "t1"},
        {"ticker": "600519.SH", "name": "贵州茅台", "sector": "消费", "task_id": None},
    ]
    batch_state.save_batch_state("2026-07-10", "morning", "standard", stocks)

    state = batch_state.load_batch_state()
    assert state is not None
    assert state["target_date"] == "2026-07-10"
    assert state["recommendation_type"] == "morning"
    assert state["depth"] == "standard"
    assert state["status"] == "running"
    assert state["stocks"] == stocks


def test_load_batch_state_missing_or_corrupt(tmp_path, monkeypatch):
    """文件不存在/损坏 JSON/结构异常 → 一律返回 None，不抛异常"""
    from app import batch_state
    f = tmp_path / ".batch-state.json"
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", f)

    assert batch_state.load_batch_state() is None  # 不存在

    f.write_text("{broken json", encoding="utf-8")
    assert batch_state.load_batch_state() is None  # 损坏

    f.write_text(json.dumps({"target_date": "2026-07-10"}), encoding="utf-8")
    assert batch_state.load_batch_state() is None  # 缺 stocks 列表

    f.write_text(json.dumps({"stocks": "not-a-list"}), encoding="utf-8")
    assert batch_state.load_batch_state() is None  # stocks 类型错误


def test_mark_batch_done(tmp_path, monkeypatch):
    """标记 done 后状态不再参与续跑决策"""
    from app import batch_state
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")

    stocks = [{"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": "t1"}]
    batch_state.save_batch_state("2099-01-01", "morning", "standard", stocks)
    state = batch_state.load_batch_state()
    assert batch_state.decide_resume_action(
        state, datetime(2098, 12, 31, 20, 0)) == "resume"

    batch_state.mark_batch_done(state)
    state = batch_state.load_batch_state()
    assert state["status"] == "done"
    assert batch_state.decide_resume_action(
        state, datetime(2098, 12, 31, 20, 0)) == "none"


def test_mark_batch_abandoned(tmp_path, monkeypatch):
    """去除批次：标记 abandoned 后彻底出局，不再续跑也不再评估"""
    from app import batch_state
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")

    stocks = [{"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": "t1"}]
    batch_state.save_batch_state("2099-01-01", "morning", "standard", stocks)
    state = batch_state.load_batch_state()

    batch_state.mark_batch_abandoned(state)
    state = batch_state.load_batch_state()
    assert state["status"] == "abandoned"
    assert state["stocks"] == stocks  # 快照保留，供无推荐诊断解释原因
    assert batch_state.decide_resume_action(
        state, datetime(2098, 12, 31, 20, 0)) == "none"


# ─── decide_resume_action 纯函数判定 ───

def _make_state(target_date, status="running", stocks=None):
    return {
        "target_date": target_date,
        "recommendation_type": "morning",
        "depth": "standard",
        "status": status,
        "stocks": stocks if stocks is not None else [
            {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": "t1"}
        ],
    }


def test_decide_resume_action_time_window():
    """时效判定：现在 + 预估耗时 ≤ 目标日 9:00 才可续，超时去除（abandon）。

    standard 深度单只 25 分钟（默认并发 2）：1 只股票预估 25 分钟。
    """
    from app.batch_state import decide_resume_action

    # 目标日前一天 15:30 中断重启（收盘分析刚启动就挂）→ 充裕，立即续跑
    assert decide_resume_action(
        _make_state("2026-07-10"), datetime(2026, 7, 9, 15, 30)) == "resume"

    # 目标日当天 8:00 → 8:25 完成 < 9:00 截止 → 可续
    assert decide_resume_action(
        _make_state("2026-07-10"), datetime(2026, 7, 10, 8, 0)) == "resume"

    # 目标日当天 8:50 → 9:15 完成 > 9:00 截止 → 去除（哪怕只差几分钟）
    assert decide_resume_action(
        _make_state("2026-07-10"), datetime(2026, 7, 10, 8, 50)) == "abandon"

    # 目标日当天 9:40（已过开盘截止）→ 去除，不能今天还续跑昨天的批次
    assert decide_resume_action(
        _make_state("2026-07-10"), datetime(2026, 7, 10, 9, 40)) == "abandon"

    # 目标日当天盘中 11:00 / 14:00 → 时效早已丢失，去除（而非 defer）
    assert decide_resume_action(
        _make_state("2026-07-10"), datetime(2026, 7, 10, 11, 0)) == "abandon"
    assert decide_resume_action(
        _make_state("2026-07-10"), datetime(2026, 7, 10, 14, 0)) == "abandon"

    # 目标日已过 → 去除
    assert decide_resume_action(
        _make_state("2026-07-09"), datetime(2026, 7, 10, 8, 0)) == "abandon"


def test_decide_resume_action_market_session_defers():
    """9:00-15:00 时段不启动续跑：时效充足（目标日在未来）时顺延而非去除。

    场景：周六上午重启，批次目标日是下周一 → 时效充足但处于交易时段窗口。
    """
    from app.batch_state import decide_resume_action

    # 目标日 7-13（周一），7-11 周六 9:00/10:30/14:59 → defer（15:00 后再续）
    assert decide_resume_action(
        _make_state("2026-07-13"), datetime(2026, 7, 11, 9, 0)) == "defer"
    assert decide_resume_action(
        _make_state("2026-07-13"), datetime(2026, 7, 11, 10, 30)) == "defer"
    assert decide_resume_action(
        _make_state("2026-07-13"), datetime(2026, 7, 11, 14, 59)) == "defer"

    # 同日 8:00（窗口前）与 15:00（窗口后）→ 正常续跑
    assert decide_resume_action(
        _make_state("2026-07-13"), datetime(2026, 7, 11, 8, 0)) == "resume"
    assert decide_resume_action(
        _make_state("2026-07-13"), datetime(2026, 7, 11, 15, 0)) == "resume"


def test_decide_resume_action_scales_with_stock_count():
    """股票越多预估耗时越长，同一时间点大批次可能被去除"""
    from app.batch_state import decide_resume_action
    from app.config import MAX_CONCURRENT_TASKS

    # 20 只 standard：ceil(20/并发) × 25 分钟；并发 2 时 = 250 分钟
    big = _make_state("2026-07-10", stocks=[
        {"ticker": f"00000{i}.SZ", "name": f"股{i}", "sector": "测试", "task_id": None}
        for i in range(20)
    ])
    # 目标日 5:00：仅剩 240 分钟，250 > 240 → 去除（并发 ≤2 的默认配置下）
    if MAX_CONCURRENT_TASKS <= 2:
        assert decide_resume_action(big, datetime(2026, 7, 10, 5, 0)) == "abandon"
    # 前一天 20:00：还有 13 小时 → 可续
    assert decide_resume_action(big, datetime(2026, 7, 9, 20, 0)) == "resume"


def test_decide_resume_action_invalid_states():
    """None/done/abandoned/空 stocks → none；缺失或非法 target_date → abandon"""
    from app.batch_state import decide_resume_action

    now = datetime(2026, 7, 10, 8, 0)
    assert decide_resume_action(None, now) == "none"
    assert decide_resume_action(_make_state("2026-07-11", status="done"), now) == "none"
    assert decide_resume_action(_make_state("2026-07-11", status="abandoned"), now) == "none"
    assert decide_resume_action(_make_state("2026-07-11", stocks=[]), now) == "none"
    assert decide_resume_action(_make_state(""), now) == "abandon"
    assert decide_resume_action(_make_state("not-a-date"), now) == "abandon"


# ─── 续跑复用已完成任务（数据库集成） ───

def test_resume_reuses_completed_tasks():
    """续跑时已 completed 的任务应被识别为可复用（模拟批次内查询逻辑）"""
    from app.database import SessionLocal, AnalysisTask

    tid_done = f"test-resume-{uuid.uuid4()}"
    tid_fail = f"test-resume-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(AnalysisTask(task_id=tid_done, ticker="000001.SZ", status="completed"))
        db.add(AnalysisTask(task_id=tid_fail, ticker="600519.SH", status="failed"))
        db.commit()

        rows = db.query(AnalysisTask.task_id, AnalysisTask.status).filter(
            AnalysisTask.task_id.in_([tid_done, tid_fail])
        ).all()
        reusable = {tid: status for tid, status in rows}
        assert reusable[tid_done] == "completed"   # 复用
        assert reusable[tid_fail] == "failed"      # 重建
    finally:
        db.query(AnalysisTask).filter(
            AnalysisTask.task_id.in_([tid_done, tid_fail])
        ).delete(synchronize_session=False)
        db.commit()
        db.close()


# ─── 函数签名与调度接线 ───

def test_auto_analyze_signature_accepts_resume_state():
    """auto_analyze_and_recommend 必须接受 resume_state 参数"""
    import inspect
    from auto_analyze_and_recommend import auto_analyze_and_recommend
    params = inspect.signature(auto_analyze_and_recommend).parameters
    assert "resume_state" in params
    assert params["resume_state"].default is None


def test_scheduler_has_resume_batch_job():
    """scheduler 必须暴露 resume_batch_job 协程"""
    import asyncio
    from app.scheduler import resume_batch_job
    assert asyncio.iscoroutinefunction(resume_batch_job)


def test_resume_batch_job_noop_when_not_resumable(tmp_path, monkeypatch):
    """无可续跑状态时 resume_batch_job 直接返回，不触发批量分析"""
    import asyncio
    from app import batch_state
    from app import scheduler as sched_mod
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")

    called = []

    async def fake_run(**kwargs):
        called.append(kwargs)

    import auto_analyze_and_recommend as aar
    monkeypatch.setattr(aar, "auto_analyze_and_recommend", fake_run)

    asyncio.run(sched_mod.resume_batch_job())
    assert called == []


def test_resume_batch_job_abandons_expired_batch(tmp_path, monkeypatch):
    """时效已丢失的批次：resume_batch_job 去除批次（标 abandoned）且不启动分析"""
    import asyncio
    from app import batch_state
    from app import scheduler as sched_mod
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")

    # 目标日已成过去 → 无论何时执行都判 abandon
    batch_state.save_batch_state("2020-01-02", "morning", "standard", [
        {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": "t1"}
    ])

    called = []

    async def fake_run(**kwargs):
        called.append(kwargs)

    import auto_analyze_and_recommend as aar
    monkeypatch.setattr(aar, "auto_analyze_and_recommend", fake_run)

    asyncio.run(sched_mod.resume_batch_job())
    assert called == []  # 未启动批量分析
    state = batch_state.load_batch_state()
    assert state["status"] == "abandoned"  # 批次已去除，重启后不再评估


# ─── 端到端模拟：批次中断 → 重启续跑 ───

def test_resume_end_to_end(tmp_path, monkeypatch):
    """模拟中断后的续跑全流程：
    - completed 任务复用（不重建、不等待）
    - failed/僵尸任务重建
    - 推荐生成后批次标记 done
    """
    import asyncio
    from app import batch_state
    import auto_analyze_and_recommend as aar
    from app.database import SessionLocal, AnalysisTask

    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")
    # aar 模块内直接引用了这几个符号，逐一打补丁
    monkeypatch.setattr(aar, "save_batch_state", batch_state.save_batch_state)
    monkeypatch.setattr(aar, "load_batch_state", batch_state.load_batch_state)
    monkeypatch.setattr(aar, "mark_batch_done", batch_state.mark_batch_done)

    tid_done = f"test-e2e-{uuid.uuid4()}"
    tid_fail = f"test-e2e-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        db.add(AnalysisTask(task_id=tid_done, ticker="000001.SZ",
                            status="completed", score=80.0))
        db.add(AnalysisTask(task_id=tid_fail, ticker="600519.SH", status="failed"))
        db.commit()
    finally:
        db.close()

    # 上次批次快照：一只已完成、一只失败（重启僵尸被标 failed 的形态）
    resume_state = {
        "target_date": "2099-01-01",
        "recommendation_type": "morning",
        "depth": "standard",
        "status": "running",
        "stocks": [
            {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": tid_done},
            {"ticker": "600519.SH", "name": "贵州茅台", "sector": "消费", "task_id": tid_fail},
        ],
    }

    created, waited = [], []

    def fake_create_task(ticker, depth="standard", owner_user_id=None):
        created.append(ticker)
        return f"new-{ticker}"

    async def fake_wait(task_ids, depth):
        waited.extend(task_ids)
        return {"completed": len(task_ids), "failed": 0}

    def fake_gen(tasks, target_date, rec_type):
        return {}

    monkeypatch.setattr(aar, "create_task", fake_create_task)
    monkeypatch.setattr(aar, "_wait_for_tasks", fake_wait)
    monkeypatch.setattr(aar, "_generate_recommendations", fake_gen)
    monkeypatch.setattr(aar, "NOTIFY_ON_ANALYSIS_COMPLETE", False)

    try:
        asyncio.run(aar.auto_analyze_and_recommend(resume_state=resume_state))

        # 只重建了失败那只，已完成的直接复用
        assert created == ["600519.SH"]
        # 只等待新建任务，不等复用任务
        assert waited == ["new-600519.SH"]
        # 全流程结束后批次已标记 done，不会再次续跑
        state = batch_state.load_batch_state()
        assert state["status"] == "done"
        assert batch_state.decide_resume_action(state) == "none"
    finally:
        db = SessionLocal()
        try:
            db.query(AnalysisTask).filter(
                AnalysisTask.task_id.in_([tid_done, tid_fail])
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


# ─── 无推荐原因诊断 ───

def test_diagnose_no_recommendation_variants(tmp_path, monkeypatch):
    """无推荐时的推送文案必须区分：正常无入选 / 中断去除 / 未执行"""
    from app import batch_state
    from app import scheduler as sched_mod
    monkeypatch.setattr(batch_state, "BATCH_STATE_FILE", tmp_path / ".batch-state.json")

    today = "2026-07-09"

    # 批次 done 但无推荐 → 正常筛选无入选
    batch_state.save_batch_state(today, "morning", "standard", [
        {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": None}
    ], status="done")
    assert "没有股票通过筛选" in sched_mod._diagnose_no_recommendation(today)

    # 批次已被去除（abandoned）→ 中断说明
    batch_state.save_batch_state(today, "morning", "standard", [
        {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": None}
    ], status="abandoned")
    msg = sched_mod._diagnose_no_recommendation(today)
    assert "中断" in msg and "开盘前" in msg

    # 批次 running 但时效已丢（今天 08:20 推送时点，目标日=今天且早已超时）→ 同样中断说明
    batch_state.save_batch_state(today, "morning", "standard", [
        {"ticker": "000001.SZ", "name": "平安银行", "sector": "金融", "task_id": None}
    ], status="running")
    msg = sched_mod._diagnose_no_recommendation(today)
    assert "中断" in msg and "开盘前" in msg

    # 无批次快照且近期无系统任务 → 未执行说明
    batch_state.BATCH_STATE_FILE.unlink()
    msg = sched_mod._diagnose_no_recommendation(today)
    assert ("未执行批量分析" in msg) or ("全部失败" in msg) or ("进行中" in msg)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
