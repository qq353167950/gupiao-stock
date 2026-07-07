"""
交易日判断模块
数据源优先级：
1. akshare tool_trade_date_hist_sina（新浪官方交易日历，覆盖全年份）→ 缓存到 data/trade_calendar.json
2. 本地缓存（接口失败时复用，缓存 30 天内有效）
3. 静态兜底：周末 + 手工维护的节假日表（网络完全不可用时的最后防线）
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import DATA_DIR

CALENDAR_CACHE_FILE = DATA_DIR / "trade_calendar.json"
CACHE_MAX_AGE_DAYS = 30  # 缓存超过 30 天强制刷新
RETRY_COOLDOWN_SECONDS = 3600  # 加载失败后的重试冷却（避免每次调用都打网络请求）

# 静态兜底节假日表（仅在 akshare 与缓存都不可用时生效，每年初核对更新一次）
FALLBACK_HOLIDAYS = [
    # 2026 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 2026 春节
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 2026 清明节
    "2026-04-05", "2026-04-06", "2026-04-07",
    # 2026 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03",
    # 2026 端午节
    "2026-06-25", "2026-06-26", "2026-06-27",
    # 2026 中秋节
    "2026-10-01", "2026-10-02", "2026-10-03",
    # 2026 国庆节
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
]

# 进程内缓存，避免重复读盘/调接口
_trading_days_cache: set = set()
_cache_loaded = False
_last_load_attempt = 0.0  # 上次加载尝试时间戳（失败重试冷却用）


def _load_from_akshare() -> set:
    """从 akshare 拉取全量交易日历"""
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    days = set(str(d) for d in df["trade_date"].astype(str))
    return days


def _load_from_cache_file() -> tuple:
    """读本地缓存，返回 (交易日集合, 缓存写入时间)；无缓存返回 (set(), None)"""
    if not CALENDAR_CACHE_FILE.exists():
        return set(), None
    try:
        payload = json.loads(CALENDAR_CACHE_FILE.read_text(encoding="utf-8"))
        cached_at = datetime.fromisoformat(payload["cached_at"])
        return set(payload["trading_days"]), cached_at
    except Exception:
        return set(), None


def _save_cache_file(days: set):
    """写本地缓存"""
    try:
        CALENDAR_CACHE_FILE.write_text(
            json.dumps(
                {"cached_at": datetime.now().isoformat(), "trading_days": sorted(days)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️  交易日历缓存写入失败（忽略）: {e}")


def _ensure_calendar_loaded():
    """确保进程内交易日历已加载：缓存新鲜则直接用，否则拉 akshare 刷新。

    加载失败进入冷却期（RETRY_COOLDOWN_SECONDS），期间走静态兜底，
    冷却结束后自动重试——长运行服务在网络恢复后无需重启即可恢复精确日历。
    """
    global _trading_days_cache, _cache_loaded, _last_load_attempt
    if _cache_loaded and _trading_days_cache:
        return
    # 失败冷却期内不重试
    if _cache_loaded and (time.time() - _last_load_attempt) < RETRY_COOLDOWN_SECONDS:
        return
    _last_load_attempt = time.time()

    cached_days, cached_at = _load_from_cache_file()
    cache_fresh = (
        cached_at is not None
        and (datetime.now() - cached_at) < timedelta(days=CACHE_MAX_AGE_DAYS)
    )

    if cache_fresh:
        _trading_days_cache = cached_days
        _cache_loaded = True
        return

    # 缓存过期或不存在 → 尝试 akshare 刷新
    try:
        days = _load_from_akshare()
        if days:
            _trading_days_cache = days
            _save_cache_file(days)
            _cache_loaded = True
            print(f"✅ 交易日历已从 akshare 刷新（{len(days)} 个交易日）")
            return
    except Exception as e:
        print(f"⚠️  akshare 交易日历获取失败: {e}")

    # akshare 失败 → 过期缓存也比没有强
    if cached_days:
        _trading_days_cache = cached_days
        _cache_loaded = True
        print("⚠️  使用过期的本地交易日历缓存")
        return

    # 彻底没有数据 → 留空集合，is_trading_day 走静态兜底
    _cache_loaded = True
    print("⚠️  无交易日历数据，降级为 周末+静态节假日表 判断")


def is_trading_day(date=None) -> bool:
    """
    判断是否为A股交易日

    Args:
        date: datetime对象，默认为今天

    Returns:
        bool: True表示交易日，False表示休市
    """
    if date is None:
        date = datetime.now()

    date_str = date.strftime("%Y-%m-%d")

    _ensure_calendar_loaded()

    # 优先用官方日历精确判断
    if _trading_days_cache:
        return date_str in _trading_days_cache

    # 静态兜底：周末 + 节假日表
    if date.weekday() >= 5:  # 5=周六, 6=周日
        return False
    return date_str not in FALLBACK_HOLIDAYS


def get_next_trading_day(date=None):
    """
    获取下一个交易日

    Args:
        date: datetime对象，默认为今天

    Returns:
        datetime: 下一个交易日；30 天内找不到返回 None
    """
    if date is None:
        date = datetime.now()

    next_day = date + timedelta(days=1)

    # 最多找30天
    for _ in range(30):
        if is_trading_day(next_day):
            return next_day
        next_day += timedelta(days=1)

    return None


def days_until_next_trading_day(date=None):
    """
    距离下一个交易日的天数

    Args:
        date: datetime对象，默认为今天

    Returns:
        int: 天数；找不到返回 None
    """
    if date is None:
        date = datetime.now()

    next_trading = get_next_trading_day(date)
    if next_trading:
        return (next_trading.date() - date.date()).days
    return None


if __name__ == "__main__":
    today = datetime.now()

    print("=" * 70)
    print("A股交易日判断")
    print("=" * 70)

    print(f"\n今天: {today.strftime('%Y-%m-%d %A')}")
    print(f"是否交易日: {'是' if is_trading_day(today) else '否'}")

    if not is_trading_day(today):
        next_trading = get_next_trading_day(today)
        if next_trading:
            print(f"\n下一个交易日: {next_trading.strftime('%Y-%m-%d %A')}")
            print(f"距离天数: {days_until_next_trading_day(today)} 天")

    print("\n近期交易日：")
    current = today
    for i in range(10):
        is_trading = "✓" if is_trading_day(current) else "✗"
        print(f"  {is_trading} {current.strftime('%Y-%m-%d %A')}")
        current += timedelta(days=1)
