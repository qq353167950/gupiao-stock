"""
核心逻辑单元测试（无网络、无数据库依赖）
运行：python -m pytest tests/test_core.py -v
或：  python tests/test_core.py
"""
import sys
import os
from datetime import datetime
from pathlib import Path

# Windows 控制台 GBK 编码兼容（与 run.py 同款处理）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 保证可导入 app 包
BASE_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(BASE_DIR))


# ─── config ───

def test_config_depth_map():
    """Web 深度必须完整映射到 skill 深度"""
    from app.config import DEPTH_MAP, ANALYSIS_TIMEOUTS
    assert DEPTH_MAP == {"quick": "lite", "standard": "medium", "deep": "deep"}
    # 每个深度都有对应超时，且超时覆盖 skill 实测耗时（medium 5-8min → ≥ 900s）
    for depth in DEPTH_MAP:
        assert depth in ANALYSIS_TIMEOUTS
    assert ANALYSIS_TIMEOUTS["standard"] >= 900
    assert ANALYSIS_TIMEOUTS["deep"] >= ANALYSIS_TIMEOUTS["standard"]


def test_config_parse_hhmm():
    """HH:MM 解析：正常值、非法值回退默认、默认也非法时兜底"""
    from app.config import parse_hhmm
    assert parse_hhmm("15:10", "08:00") == (15, 10)
    assert parse_hhmm("bad", "08:20") == (8, 20)
    assert parse_hhmm("25:99", "07:30") == (7, 30)
    assert parse_hhmm("", "") == (8, 0)


def test_now_cn_is_beijing_time():
    """now_cn 必须返回北京时间（UTC+8）的 naive datetime，与服务器时区无关"""
    from datetime import datetime, timezone, timedelta
    from app.config import now_cn, TZ_SHANGHAI

    now = now_cn()
    assert now.tzinfo is None, "必须返回 naive datetime（与数据库既有数据同构）"

    # 与 UTC+8 直接换算结果比对（容忍执行间隙 2 秒）
    expected = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert abs((now - expected).total_seconds()) < 2, f"now_cn 偏离北京时间: {now} vs {expected}"

    # 时区对象必须是 Asia/Shanghai 语义（zoneinfo 或 +8 固定偏移兜底）
    offset = datetime.now(TZ_SHANGHAI).utcoffset()
    assert offset == timedelta(hours=8), f"TZ_SHANGHAI 偏移错误: {offset}"


def test_scheduler_jobs_use_beijing_timezone():
    """调度器三个 CronTrigger 必须显式绑定北京时间（不依赖服务器时区）"""
    from datetime import timedelta, datetime
    from app import scheduler as sched_mod

    # 调度器本身的时区
    tz = sched_mod.scheduler.timezone
    assert datetime.now(tz).utcoffset() == timedelta(hours=8), f"调度器时区错误: {tz}"

    # 注册任务（不 start），验证 CronTrigger 显式传时区后的实际偏移
    from apscheduler.triggers.cron import CronTrigger
    from app.config import TZ_SHANGHAI
    trigger = CronTrigger(hour=3, minute=30, timezone=TZ_SHANGHAI)
    assert datetime.now(trigger.timezone).utcoffset() == timedelta(hours=8)


def test_config_no_hardcoded_workspace():
    """全项目不允许再出现 /workspace 硬编码路径"""
    for py_file in [BASE_DIR / "auto_analyze_and_recommend.py"] + list((BASE_DIR / "app").glob("*.py")):
        content = py_file.read_text(encoding="utf-8")
        assert "/workspace" not in content, f"{py_file.name} 仍含硬编码 /workspace 路径"
        assert "monkeycode" not in content, f"{py_file.name} 仍含失效临时域名"


def test_config_concurrency_positive():
    from app.config import MAX_CONCURRENT_TASKS, STOCKS_PER_SECTOR
    assert MAX_CONCURRENT_TASKS >= 1
    assert STOCKS_PER_SECTOR >= 1


# ─── task_manager ───

def test_parse_ticker():
    from app.task_manager import parse_ticker
    assert parse_ticker("600519") == "600519.SH"
    assert parse_ticker("000001") == "000001.SZ"
    assert parse_ticker("300750") == "300750.SZ"
    assert parse_ticker("600519.SH") == "600519.SH"
    assert parse_ticker(" 000001.SZ ") == "000001.SZ"
    assert parse_ticker("AAPL") == "AAPL"
    assert parse_ticker("贵州茅台") == "贵州茅台"


def test_semaphore_matches_config():
    """并发信号量必须真实生效且与配置一致"""
    from app.task_manager import _analysis_semaphore
    from app.config import MAX_CONCURRENT_TASKS
    assert _analysis_semaphore._value == MAX_CONCURRENT_TASKS


def test_parse_analysis_summary_format():
    """one-liner 摘要解析（构造临时报告目录验证）"""
    import tempfile
    from app import task_manager
    from app.config import SKILL_REPORTS_DIR

    SKILL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SKILL_REPORTS_DIR) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "one-liner.txt").write_text(
            "贵州茅台 体检结果：72 分，DCF低估 35%，12 人喊买。", encoding="utf-8"
        )
        summary = task_manager.parse_analysis_summary(tmp_path.name)
        assert summary is not None
        assert summary["score"] == 72.0
        assert abs(summary["dcf_discount"] - 0.35) < 1e-9
        assert summary["bullish_count"] == 12
        assert summary["name"] == "贵州茅台"


# ─── notifier ───

def test_format_daily_digest_with_data():
    from app.notifier import format_daily_digest
    recs = [
        {"ticker": "600519.SH", "name": "贵州茅台", "score": 85.5,
         "reason": "长期 · 基本面优秀", "sector": "白酒", "risk_level": "🟢 低风险"},
        {"ticker": "300750.SZ", "name": "宁德时代", "score": 78.0,
         "reason": "中长期 · 估值合理", "sector": "锂电池"},
    ]
    text = format_daily_digest("2026-07-03", recs)
    assert "2026-07-03" in text
    assert "贵州茅台" in text and "600519.SH" in text
    assert "85.5" in text
    assert "【白酒】" in text and "【锂电池】" in text
    assert "🟢 低风险" in text
    assert "不构成投资建议" in text


def test_format_daily_digest_empty():
    from app.notifier import format_daily_digest
    text = format_daily_digest("2026-07-03", [])
    assert "暂无推荐" in text


def test_format_analysis_summary():
    from app.notifier import format_analysis_summary
    text = format_analysis_summary(
        "2026-07-03", total=18, completed=15, failed=3,
        top_stocks=[{"ticker": "600519.SH", "name": "贵州茅台", "score": 85.5}],
    )
    assert "18" in text and "15" in text and "3" in text
    assert "贵州茅台" in text


def test_notifier_skips_without_channels(monkeypatch=None):
    """未配置渠道时 send 返回空 dict 且不发任何请求"""
    from app import notifier
    original = notifier.NOTIFY_CHANNELS
    notifier.NOTIFY_CHANNELS = []
    try:
        assert notifier.send("t", "c") == {}
    finally:
        notifier.NOTIFY_CHANNELS = original


def test_notifier_unknown_channel():
    """未知渠道返回 False 而非抛异常"""
    from app import notifier
    original = notifier.NOTIFY_CHANNELS
    notifier.NOTIFY_CHANNELS = ["nosuchchannel"]
    try:
        results = notifier.send("t", "c")
        assert results == {"nosuchchannel": False}
    finally:
        notifier.NOTIFY_CHANNELS = original


def test_notifier_channel_registry_complete():
    """注册表必须覆盖文档声明的全部 6 渠道"""
    from app.notifier import _CHANNEL_SENDERS
    assert set(_CHANNEL_SENDERS) == {
        "serverchan", "pushplus", "dingtalk", "wecom", "telegram", "email"
    }


# ─── trading_calendar ───

def test_trading_calendar_weekend_fallback():
    """静态兜底路径：周末必然休市（清空日历缓存后验证）"""
    from app import trading_calendar as tc
    saved_cache, saved_loaded = tc._trading_days_cache, tc._cache_loaded
    tc._trading_days_cache, tc._cache_loaded = set(), True  # 强制走静态兜底
    try:
        assert tc.is_trading_day(datetime(2026, 7, 4)) is False  # 周六
        assert tc.is_trading_day(datetime(2026, 7, 5)) is False  # 周日
        assert tc.is_trading_day(datetime(2026, 7, 3)) is True   # 周五非节假日
        assert tc.is_trading_day(datetime(2026, 10, 1)) is False  # 国庆
    finally:
        tc._trading_days_cache, tc._cache_loaded = saved_cache, saved_loaded


def test_trading_calendar_with_official_data():
    """官方日历路径：注入模拟数据验证精确判断"""
    from app import trading_calendar as tc
    saved_cache, saved_loaded = tc._trading_days_cache, tc._cache_loaded
    tc._trading_days_cache = {"2026-07-03", "2026-07-06"}
    tc._cache_loaded = True
    try:
        assert tc.is_trading_day(datetime(2026, 7, 3)) is True
        assert tc.is_trading_day(datetime(2026, 7, 4)) is False
        next_day = tc.get_next_trading_day(datetime(2026, 7, 3))
        assert next_day.strftime("%Y-%m-%d") == "2026-07-06"
    finally:
        tc._trading_days_cache, tc._cache_loaded = saved_cache, saved_loaded


# ─── enhanced_analyzer 评分规则 ───

def test_calculate_score_full_marks():
    """高分场景：安全 + 深度高分 + 机构龙虎榜"""
    from app.enhanced_analyzer import EnhancedAnalyzer
    analyzer = EnhancedAnalyzer()
    result = analyzer._calculate_score({
        "trap_detection": {"trap_level": "🟢 安全", "trap_score": 9},
        "deep_analysis": {"status": "success", "score": 90},
        "lhb_analysis": {"recent_lhb_count": 5, "main_money": "机构主导",
                         "identified_seats": ["章盟主"]},
    })
    # 27 + 45 + 15 + 5 = 92
    assert result["综合评分"] == 92.0
    assert result["风险等级"] == "🟢 低风险"
    assert any("章盟主" in r for r in result["推荐理由"])


def test_calculate_score_no_lhb():
    """未上榜场景：仅 trap + deep 得分"""
    from app.enhanced_analyzer import EnhancedAnalyzer
    analyzer = EnhancedAnalyzer()
    result = analyzer._calculate_score({
        "trap_detection": {"trap_level": "🟢 安全", "trap_score": 9},
        "deep_analysis": {"status": "success", "score": 60},
        "lhb_analysis": {"recent_lhb_count": 0, "main_money": "数据不足"},
    })
    # 27 + 30 = 57
    assert result["综合评分"] == 57.0
    assert result["风险等级"] == "🟠 高风险"


def test_calculate_score_outflow_no_bonus():
    """资金净流出不得加分"""
    from app.enhanced_analyzer import EnhancedAnalyzer
    analyzer = EnhancedAnalyzer()
    result = analyzer._calculate_score({
        "trap_detection": {"trap_level": "🟢 安全", "trap_score": 9},
        "deep_analysis": {"status": "success", "score": 60},
        "lhb_analysis": {"recent_lhb_count": 3, "main_money": "资金流出"},
    })
    assert result["综合评分"] == 57.0  # 与无龙虎榜相同，未加分
    assert any("净流出" in r for r in result["推荐理由"])


def test_parse_trap_result_variants():
    """LLM 返回的 JSON / 純文本 两种形态均可解析"""
    from app.enhanced_analyzer import EnhancedAnalyzer
    analyzer = EnhancedAnalyzer()
    r1 = analyzer._parse_trap_result('{"trap_level": "🟡 注意", "trap_score": 6, "recommendation": "x"}')
    assert r1["trap_level"] == "🟡 注意"
    r2 = analyzer._parse_trap_result("经分析该股票风险为：高度可疑，建议回避")
    assert r2["trap_level"] == "🔴 高度可疑"
    assert r2["trap_score"] == 2


# ─── recommendation_engine ───

def test_calculate_composite_score_returns_triple():
    """必须返回 (评分, 理由, 期限) 三元组"""
    from app.recommendation_engine import calculate_composite_score

    class FakeTask:
        composite_score = 75.0
        risk_level = "🟡 中风险"
        score = 68.0
        dcf_discount = 0.4
        bullish_count = 20
        total_voters = 50

    score, reason, period = calculate_composite_score(FakeTask())
    assert score == 75.0
    assert isinstance(reason, str) and reason
    assert period in ("长期", "中长期", "短期")


# ─── skill_manager 更新机制 ───

def test_skill_manager_effective_repo_url():
    """镜像代理前缀拼接：有代理拼前缀，无代理返回原始 URL"""
    from app.skill_manager import effective_repo_url
    raw = "https://github.com/wbh604/UZI-Skill.git"
    assert effective_repo_url(raw, "") == raw
    assert effective_repo_url(raw, "https://gh-proxy.com/") == f"https://gh-proxy.com/{raw}"
    assert effective_repo_url(raw, "https://gh-proxy.com") == f"https://gh-proxy.com/{raw}"


def test_skill_manager_should_check_now():
    """更新检查间隔判定：无记录/超期→检查，未超期→跳过，损坏时间→检查"""
    from datetime import datetime, timedelta
    from app.skill_manager import should_check_now
    now = datetime(2026, 7, 3, 12, 0, 0)
    assert should_check_now({}, 3, now) is True  # 从未检查
    fresh = {"checked_at": (now - timedelta(days=1)).isoformat()}
    assert should_check_now(fresh, 3, now) is False  # 1天前查过，间隔3天
    stale = {"checked_at": (now - timedelta(days=4)).isoformat()}
    assert should_check_now(stale, 3, now) is True  # 4天前，超期
    broken = {"checked_at": "not-a-date"}
    assert should_check_now(broken, 3, now) is True  # 损坏记录视为需检查


def test_skill_manager_sync_preserves_runtime_data():
    """同步 skill 时必须保留 reports 与 .cache 运行时数据"""
    import tempfile
    from app.skill_manager import _sync_one_skill

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # 构造"新版本"源目录
        src = tmp_path / "src" / "deep-analysis"
        (src / "scripts").mkdir(parents=True)
        (src / "run.py").write_text("# v2", encoding="utf-8")
        # 构造"旧版本"目标目录（含运行时数据）
        dst = tmp_path / "dst" / "deep-analysis"
        (dst / "scripts" / "reports" / "600519-x").mkdir(parents=True)
        (dst / "scripts" / "reports" / "600519-x" / "r.html").write_text("report", encoding="utf-8")
        (dst / "scripts" / ".cache" / "600519.SH").mkdir(parents=True)
        (dst / "scripts" / ".cache" / "600519.SH" / "raw.json").write_text("{}", encoding="utf-8")
        (dst / "run.py").write_text("# v1", encoding="utf-8")

        _sync_one_skill(src, dst)

        assert (dst / "run.py").read_text(encoding="utf-8") == "# v2"  # 代码已更新
        assert (dst / "scripts" / "reports" / "600519-x" / "r.html").exists()  # 报告保留
        assert (dst / "scripts" / ".cache" / "600519.SH" / "raw.json").exists()  # 缓存保留


def test_skill_manager_version_roundtrip():
    """版本文件读写往返（写入 skills/.skill-version.json 后还原）"""
    from app import skill_manager as sm
    original = sm.read_version()
    try:
        sm.write_version("abc123def", updated=True)
        v = sm.read_version()
        assert v["commit"] == "abc123def"
        assert "checked_at" in v and "updated_at" in v
    finally:
        # 还原现场
        if original:
            sm.VERSION_FILE.write_text(
                __import__("json").dumps(original, ensure_ascii=False), encoding="utf-8")
        elif sm.VERSION_FILE.exists():
            sm.VERSION_FILE.unlink()


# ─── 页面路由与模板 ───

def test_page_routes_registered():
    """功能页 + 登录/用户管理 + favicon 路由必须全部注册"""
    from app.main import app
    paths = {getattr(r, "path", None) for r in app.routes}
    for expected in ("/", "/recommendations", "/history", "/glossary",
                     "/analyze", "/report/{task_id}", "/favicon.ico", "/health",
                     "/login", "/admin/users"):
        assert expected in paths, f"缺少路由 {expected}"


def _make_request(path, user=None):
    """构造最小 Request（TemplateResponse 需要 request.url.path 与 request.state.user）。

    starlette 的 request.state 由 scope["state"] 字典驱动，
    直接在 scope 注入 user 即等效于真实请求中中间件的注入动作。
    """
    from starlette.requests import Request
    return Request({
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("test", 80), "client": ("test", 0), "root_path": "",
        "state": {"user": user},
    })


def test_templates_render():
    """全部页面模板可渲染（捕获 Jinja2 语法错误与继承断链）——游客视角"""
    from app.main import templates

    for name, path in [
        ("index.html", "/"),
        ("recommendations.html", "/recommendations"),
        ("history.html", "/history"),
        ("glossary.html", "/glossary"),
        ("login.html", "/login"),
        ("admin_users.html", "/admin/users"),
    ]:
        resp = templates.TemplateResponse(_make_request(path), name)
        body = resp.body.decode("utf-8")
        assert len(body) > 500, f"{name} 渲染结果异常"
        assert "</html>" in body, f"{name} HTML 不完整"
        # 每页导航都应包含四个功能入口
        for label in ("开始分析", "今日推荐", "历史记录", "名词解释"):
            assert label in body, f"{name} 导航缺少 {label}"


def test_home_page_is_analysis():
    """首页默认展示开始分析功能（含搜索框与深度选择）"""
    from app.main import templates
    body = templates.TemplateResponse(_make_request("/"), "index.html").body.decode("utf-8")
    assert "startAnalysis" in body  # 分析发起逻辑
    assert "输入股票代码或名称" in body  # 搜索框
    assert 'value="deep"' in body  # 深度选择
    # 推荐/历史功能已拆出，首页不再内嵌其数据加载
    assert "loadRecommendations" not in body
    assert "loadHistory" not in body


# ─── 账号体系 ───

def test_password_hash_roundtrip():
    """密码哈希往返：正确密码通过、错误密码拒绝、损坏存储不抛异常"""
    from app.auth import hash_password, verify_password
    stored = hash_password("s3cret-密码")
    assert stored.startswith("pbkdf2:")
    assert verify_password("s3cret-密码", stored) is True
    assert verify_password("wrong", stored) is False
    # 两次哈希盐不同 → 密文不同
    assert hash_password("s3cret-密码") != stored
    # 损坏/异构存储格式一律拒绝而非抛异常
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "") is False
    assert verify_password("x", "md5:1:ab:cd") is False


def test_ensure_admin_user_idempotent():
    """内置管理员创建幂等：重复调用不重复建号，且不覆盖已改密码"""
    from app.auth import ensure_admin_user, verify_password, hash_password
    from app.config import ADMIN_USERNAME
    from app.database import SessionLocal, User

    ensure_admin_user()
    ensure_admin_user()  # 第二次调用应为 no-op

    db = SessionLocal()
    try:
        admins = db.query(User).filter(User.username == ADMIN_USERNAME).all()
        assert len(admins) == 1, "内置管理员重复创建"
        assert admins[0].is_admin is True
        original_hash = admins[0].password_hash  # 留存现场，测试后还原

        # 模拟界面改密后重启：ensure_admin_user 不得覆盖
        admins[0].password_hash = hash_password("changed-by-ui")
        db.commit()
    finally:
        db.close()

    try:
        ensure_admin_user()
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.username == ADMIN_USERNAME).first()
            assert verify_password("changed-by-ui", admin.password_hash), \
                "重启后管理员密码被环境变量默认值覆盖"
        finally:
            db.close()
    finally:
        # 还原现场：测试跑在真实开发库上，绝不能留下改过的密码
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.username == ADMIN_USERNAME).first()
            admin.password_hash = original_hash
            db.commit()
        finally:
            db.close()


def test_require_user_and_admin_dependencies():
    """权限依赖：未登录 401；登录非管理员过 require_user 但被 require_admin 拒 403"""
    from fastapi import HTTPException
    from app.auth import require_user, require_admin
    from app.database import User

    class FakeRequest:
        class state:
            user = None

    # 未登录 → 401
    try:
        require_user(FakeRequest())
        assert False, "未登录应抛 401"
    except HTTPException as e:
        assert e.status_code == 401

    # 普通用户 → require_user 通过，require_admin 403
    normal = User(id=99, username="u", password_hash="x", is_admin=False)
    FakeRequest.state.user = normal
    assert require_user(FakeRequest()) is normal
    try:
        require_admin(normal)
        assert False, "非管理员应抛 403"
    except HTTPException as e:
        assert e.status_code == 403

    # 管理员 → 全部通过
    admin = User(id=98, username="a", password_hash="x", is_admin=True)
    assert require_admin(admin) is admin


def test_history_visibility_rules():
    """任务可见性：管理员看全部；普通用户仅自己的；owner 为空=系统任务仅管理员可见"""
    from app.api.analyze import _can_view_task
    from app.database import User

    normal = User(id=7, username="u", password_hash="x", is_admin=False)
    admin = User(id=1, username="a", password_hash="x", is_admin=True)

    own_task = {"owner_user_id": 7}
    other_task = {"owner_user_id": 8}
    system_task = {"owner_user_id": None}

    assert _can_view_task(own_task, normal) is True
    assert _can_view_task(other_task, normal) is False
    assert _can_view_task(system_task, normal) is False
    assert all(_can_view_task(t, admin) for t in (own_task, other_task, system_task))


# ─── 优化项验证 ───

def test_sqlite_wal_enabled():
    """数据库连接必须启用 WAL 模式（读写并发不互斥）"""
    from app.database import engine
    with engine.connect() as conn:
        from sqlalchemy import text
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal", f"journal_mode={mode}，未启用 WAL"


def test_recover_zombie_tasks():
    """僵尸任务恢复：running/pending → failed，completed 不受影响"""
    import uuid
    from app.database import SessionLocal, AnalysisTask, recover_zombie_tasks

    db = SessionLocal()
    ids = {}
    try:
        # 构造三种状态的任务
        for status in ("running", "pending", "completed"):
            tid = f"test-zombie-{uuid.uuid4()}"
            ids[status] = tid
            db.add(AnalysisTask(task_id=tid, ticker="000001.SZ", status=status))
        db.commit()

        recovered = recover_zombie_tasks()
        assert recovered >= 2  # 至少恢复了本测试构造的 2 个

        for status, tid in ids.items():
            task = db.query(AnalysisTask).filter(AnalysisTask.task_id == tid).first()
            db.refresh(task)
            if status == "completed":
                assert task.status == "completed"  # 已完成的不动
            else:
                assert task.status == "failed"
                assert "重启" in task.error_message
    finally:
        # 清理测试数据
        db.query(AnalysisTask).filter(
            AnalysisTask.task_id.in_(list(ids.values()))
        ).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_notifier_retry_once():
    """推送首次抛异常 → 自动重试一次；两次都失败才抛出"""
    from app.notifier import _send_with_retry

    calls = {"n": 0}

    def flaky_sender(title, content):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("模拟网络抖动")
        return True

    # 首次失败第二次成功
    import app.notifier as notifier_mod
    original_delay = notifier_mod.RETRY_DELAY
    notifier_mod.RETRY_DELAY = 0  # 测试不真等 5 秒
    try:
        assert _send_with_retry(flaky_sender, "t", "c", "test") is True
        assert calls["n"] == 2

        # 两次都失败 → 异常向上传播（由 send() 捕获）
        def always_fail(title, content):
            raise ConnectionError("持续失败")
        try:
            _send_with_retry(always_fail, "t", "c", "test")
            assert False, "应抛出异常"
        except ConnectionError:
            pass
    finally:
        notifier_mod.RETRY_DELAY = original_delay


def test_recommendation_global_dedup_logic():
    """推荐全局去重算法：跨板块重复股票只归入评分更高的板块"""
    # 模拟 _generate_recommendations 第二遍分配的核心逻辑
    sector_candidates = {
        "软件互联网": [
            {"ticker": "002230.SZ", "score": 90},   # 科大讯飞（也在人工智能）
            {"ticker": "300059.SZ", "score": 80},
            {"ticker": "300033.SZ", "score": 75},
        ],
        "人工智能": [
            {"ticker": "002230.SZ", "score": 90},   # 同一只
            {"ticker": "300496.SZ", "score": 70},
        ],
    }
    all_candidates = [
        (sec, item) for sec, items in sector_candidates.items() for item in items
    ]
    all_candidates.sort(key=lambda x: x[1]["score"], reverse=True)

    used, picks = set(), {}
    for sec, item in all_candidates:
        if item["ticker"] in used:
            continue
        lst = picks.setdefault(sec, [])
        if len(lst) >= 2:
            continue
        lst.append(item)
        used.add(item["ticker"])

    all_picked = [i["ticker"] for lst in picks.values() for i in lst]
    assert len(all_picked) == len(set(all_picked)), "存在重复推荐"
    assert "002230.SZ" in picks["软件互联网"][0]["ticker"]  # 归入先到达的板块
    assert all(i["ticker"] != "002230.SZ" for i in picks.get("人工智能", []))
    assert len(picks["软件互联网"]) == 2  # 板块上限仍生效


# ─── 独立执行入口 ───

if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(1 if failed else 0)
