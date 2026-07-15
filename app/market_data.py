"""
市场数据获取模块
使用腾讯财经API获取A股实时行情（与deep-analysis相同的数据源）
"""
import requests
import pandas as pd
from typing import List, Dict, Set
from datetime import datetime
import time


def get_a_share_realtime_tencent() -> pd.DataFrame:
    """
    使用腾讯财经API获取A股实时行情
    
    返回字段：
    - code: 股票代码
    - name: 股票名称
    - price: 最新价
    - change_pct: 涨跌幅(%)
    - prev_close: 昨收
    - open: 今开
    - high: 最高
    - low: 最低
    - volume: 成交量(手)
    - amount: 成交额(万元)
    """
    try:
        from app.stock_pool import get_all_stocks
        
        all_stocks = get_all_stocks()
        results = []
        
        print(f"正在获取 {len(all_stocks)} 只股票的实时行情...")
        
        # 批量获取（每次50只）
        batch_size = 50
        for i in range(0, len(all_stocks), batch_size):
            batch = all_stocks[i:i+batch_size]
            
            # 构建腾讯API的股票代码
            symbols = []
            for stock in batch:
                code = stock.split('.')[0]
                if code.startswith('6'):
                    symbols.append(f'sh{code}')
                else:
                    symbols.append(f'sz{code}')
            
            url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
            
            try:
                r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    text = r.content.decode("gbk", errors="replace")
                    
                    for line in text.strip().split('\n'):
                        if '=' not in line:
                            continue
                        
                        try:
                            # 解析数据
                            symbol = line.split('=')[0].split('_')[-1]
                            content = line.split('"')[1]
                            parts = content.split('~')
                            
                            if len(parts) < 35:
                                continue
                            
                            def _f(idx):
                                try:
                                    v = parts[idx].strip()
                                    return float(v) if v and v != "-" else 0
                                except:
                                    return 0
                            
                            price = _f(3)
                            prev_close = _f(4)
                            
                            # 计算涨跌幅
                            change_pct = 0
                            if prev_close > 0:
                                change_pct = ((price - prev_close) / prev_close) * 100
                            
                            # 提取原始代码（去掉sh/sz前缀）
                            code = symbol[2:]
                            
                            results.append({
                                'code': code,
                                'name': parts[1],
                                'price': price,
                                'change_pct': change_pct,
                                'prev_close': prev_close,
                                'open': _f(5),
                                'high': _f(33),
                                'low': _f(34),
                                'volume': _f(6),  # 成交量（手）
                                'amount': _f(37) / 10000,  # 成交额转换为万元
                            })
                        
                        except Exception as e:
                            continue
                
            except Exception as e:
                print(f"  批次 {i//batch_size + 1} 获取失败: {e}")
                continue
            
            # 避免请求过快
            time.sleep(0.5)
        
        df = pd.DataFrame(results)
        print(f"✓ 成功获取 {len(df)} 只股票行情")
        return df
        
    except Exception as e:
        print(f"获取A股行情失败: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def _with_market_suffix(code: str) -> str:
    return f"{code}.SH" if code.startswith('6') else f"{code}.SZ"


def _stock_row_to_dict(row, reason: str, source_pool: str) -> Dict:
    code_with_market = _with_market_suffix(row['code'])
    return {
        'ticker': code_with_market,
        'name': row['name'],
        'price': float(row['price']),
        'change_pct': float(row['change_pct']),
        'volume': float(row['volume']),
        'amount': float(row['amount']),
        'source_pool': source_pool,
        'selected_reason': reason,
    }


def _base_trade_filter(sector_stocks: List[str], df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    filtered = df[df['code'].isin([s.split('.')[0] for s in sector_stocks])].copy()
    if filtered.empty:
        return filtered
    return filtered[
        (filtered['price'] > 0) &
        (filtered['amount'] > 0) &
        (~filtered['name'].str.contains('ST', na=False))
    ]


def select_stocks_by_momentum(sector_stocks: List[str], df: pd.DataFrame, top_n: int = 3) -> List[Dict]:
    """
    基于涨跌幅和成交量选股
    
    策略：
    1. 涨跌幅在 0% 到 +10% 之间（上涨但不过度）
    2. 成交量较大（排名前50%）
    3. 排除ST股票
    4. 按涨跌幅排序
    """
    try:
        df = _base_trade_filter(sector_stocks, df)
        
        if df.empty:
            return []
        
        # 过滤条件：上涨但不过热，兼顾活跃度
        df = df[
            (df['change_pct'] > 0) & (df['change_pct'] <= 10)
        ]
        
        if df.empty:
            return []
        
        # 成交量筛选（取前50%）
        volume_threshold = df['volume'].quantile(0.5)
        df = df[df['volume'] >= volume_threshold]
        
        if df.empty:
            return []
        
        # 按涨跌幅排序
        df = df.sort_values('change_pct', ascending=False).head(top_n)
        
        return [
            _stock_row_to_dict(
                row,
                f"动量池：涨幅{row['change_pct']:.2f}% · 成交{row['amount']:.0f}万",
                "momentum",
            )
            for _, row in df.iterrows()
        ]
        
    except Exception as e:
        print(f"选股失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def select_stocks_by_quality_proxy(sector_stocks: List[str], df: pd.DataFrame, top_n: int = 2) -> List[Dict]:
    """免费数据质量代理池：用成交额、价格有效性和不过热约束挑选中长期候选。"""
    try:
        df = _base_trade_filter(sector_stocks, df)
        if df.empty:
            return []
        df = df[(df['change_pct'] >= -3) & (df['change_pct'] <= 6)].copy()
        if df.empty:
            return []
        # 免费实时行情没有稳定财务指标，先用成交额和不过热程度做分析前质量代理。
        df['quality_proxy_score'] = df['amount'].rank(pct=True) * 70 + (6 - df['change_pct'].abs()).clip(lower=0) / 6 * 30
        df = df.sort_values('quality_proxy_score', ascending=False).head(top_n)
        return [
            _stock_row_to_dict(
                row,
                f"质量池：成交活跃 · 涨跌幅{row['change_pct']:.2f}% · 成交{row['amount']:.0f}万",
                "quality",
            )
            for _, row in df.iterrows()
        ]
    except Exception as e:
        print(f"质量池选股失败: {e}")
        return []


def select_stocks_by_rotation(sector_stocks: List[str], df: pd.DataFrame,
                              recently_analyzed: Set[str], top_n: int = 1) -> List[Dict]:
    """轮动覆盖池：优先补充近期未分析且流动性尚可的股票。"""
    try:
        df = _base_trade_filter(sector_stocks, df)
        if df.empty:
            return []
        df['ticker'] = df['code'].apply(_with_market_suffix)
        df = df[~df['ticker'].isin(recently_analyzed)].copy()
        if df.empty:
            return []
        volume_threshold = df['volume'].quantile(0.4)
        df = df[(df['volume'] >= volume_threshold) & (df['change_pct'] >= -5) & (df['change_pct'] <= 8)]
        df = df.sort_values(['amount', 'volume'], ascending=False).head(top_n)
        return [
            _stock_row_to_dict(
                row,
                f"轮动池：近期未分析 · 成交{row['amount']:.0f}万",
                "rotation",
            )
            for _, row in df.iterrows()
        ]
    except Exception as e:
        print(f"轮动池选股失败: {e}")
        return []


def get_whole_market_realtime_eastmoney(max_pages: int = 60, page_size: int = 100) -> pd.DataFrame:
    """拉取全市场 A 股实时行情（东财 clist 接口，覆盖沪深主板/创业板/科创板）。

    与固定股票池的腾讯接口互补：这里不预设股票清单，直接分页拉全市场几千只股票，
    用于「尽量多的股票里海选」。字段与 get_a_share_realtime_tencent 对齐（code/name/
    price/change_pct/volume/amount，另含 turnover 换手率），供预筛复用同一套逻辑。

    Args:
        max_pages: 最大翻页数（page_size=100 时 60 页≈6000 只，足够覆盖全市场）
        page_size: 每页数量
    """
    # fs 过滤：m:0 t:6 深主板, m:0 t:80 创业板, m:1 t:2 沪主板, m:1 t:23 科创板
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    # 字段：f12 代码 f14 名称 f2 最新价 f3 涨跌幅 f5 成交量(手) f6 成交额(元) f8 换手率
    fields = "f12,f14,f2,f3,f5,f6,f8"
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"

    results = []
    print(f"正在从东财拉取全市场 A 股行情（最多 {max_pages} 页）...")
    for page in range(1, max_pages + 1):
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f6",  # 按成交额排序：靠前页即为高流动性股票
            "fs": fs,
            "fields": fields,
        }
        try:
            r = requests.get(base_url, params=params, timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"  第 {page} 页 HTTP {r.status_code}，停止翻页")
                break
            payload = r.json()
            diff = (payload.get("data") or {}).get("diff")
            if not diff:
                break  # 无更多数据

            # diff 可能是 list（新版）或 dict（旧版），统一成列表
            rows = diff if isinstance(diff, list) else list(diff.values())
            for item in rows:
                code = str(item.get("f12", "")).strip()
                if not code or len(code) != 6:
                    continue

                def _num(key):
                    v = item.get(key)
                    try:
                        return float(v) if v not in ("-", "", None) else 0.0
                    except (TypeError, ValueError):
                        return 0.0

                results.append({
                    "code": code,
                    "name": str(item.get("f14", "")).strip(),
                    "price": _num("f2"),
                    "change_pct": _num("f3"),
                    "volume": _num("f5"),
                    "amount": _num("f6") / 10000.0,  # 元 → 万元，与腾讯接口口径一致
                    "turnover": _num("f8"),  # 换手率(%)
                })
            time.sleep(0.2)  # 轻微限速，避免触发风控
        except Exception as e:
            print(f"  第 {page} 页获取失败: {e}")
            break

    df = pd.DataFrame(results)
    if not df.empty:
        df = df.drop_duplicates(subset=["code"])
    print(f"✓ 全市场拉取完成，共 {len(df)} 只股票")
    return df


def prescreen_whole_market(df: pd.DataFrame, target_total: int,
                           recently_analyzed: Set[str] = None) -> List[Dict]:
    """对全市场行情用纯行情指标预筛出 target_total 只候选，交给 uzi-skill 深度分析。

    这是「海选」第一层（不消耗昂贵的 skill 分析），用免费实时行情快速缩圈：
    - 硬过滤：剔除 ST/退市/停牌（价格或成交额为 0）与异常股
    - 打分：成交额（流动性）主导 + 温和上涨动量 + 换手活跃度，
      过热（涨幅>9.5% 接近涨停）与深跌（<-6%）适度降权
    近期已分析过的股票轻度降权（鼓励覆盖更多标的），但不硬性排除。
    """
    if df is None or df.empty:
        return []
    recently_analyzed = recently_analyzed or set()

    d = df.copy()
    # 硬过滤：有效价格 + 有成交 + 非 ST/退市
    d = d[
        (d["price"] > 0) &
        (d["amount"] > 0) &
        (~d["name"].astype(str).str.contains("ST", na=False)) &
        (~d["name"].astype(str).str.contains("退", na=False))
    ]
    if d.empty:
        return []

    # 成交额分位（流动性核心指标）
    d["amount_rank"] = d["amount"].rank(pct=True)
    turnover = d["turnover"] if "turnover" in d.columns else pd.Series(0, index=d.index)

    def _score(row) -> float:
        score = row["amount_rank"] * 60.0  # 流动性主导
        cp = row["change_pct"]
        # 温和上涨最优（0~6%），过热或深跌降权
        if 0 <= cp <= 6:
            score += 20.0 * (1 - abs(cp - 3) / 3)
        elif 6 < cp <= 9.5:
            score += 6.0
        elif cp > 9.5:
            score += 2.0  # 接近涨停，追高风险
        elif -6 <= cp < 0:
            score += 8.0 * (1 + cp / 6)
        # 换手活跃度（2%~15% 为宜）
        tv = row.get("turnover", 0) or 0
        if 2 <= tv <= 15:
            score += 15.0
        elif tv > 15:
            score += 5.0
        # 近期已分析轻度降权，鼓励覆盖更多标的
        ticker = _with_market_suffix(row["code"])
        if ticker in recently_analyzed:
            score -= 8.0
        return score

    d["prescreen_score"] = d.apply(_score, axis=1)
    d = d.sort_values("prescreen_score", ascending=False).head(max(1, target_total))

    return [
        _stock_row_to_dict(
            row,
            f"全市场海选：成交{row['amount']:.0f}万 · 涨跌{row['change_pct']:.2f}%"
            + (f" · 换手{row['turnover']:.1f}%" if row.get('turnover') else ""),
            "whole_market",
        )
        for _, row in d.iterrows()
    ]


def get_whole_market_candidates(target_total: int = 100) -> Dict[str, List[Dict]]:
    """全市场海选入口：拉全市场行情 → 纯行情预筛 → 按大板块归类返回。

    返回结构与 get_sector_hot_stocks 一致（{大板块: [stocks]}），使
    auto_analyze_and_recommend 的下游流程无需区分选股来源。不在固定板块
    映射内的股票归入「其他」大板块。
    """
    from app.stock_pool import get_stock_category, get_major_sector

    df = get_whole_market_realtime_eastmoney()
    if df.empty:
        print("⚠️  全市场行情获取失败，回退到固定股票池选股")
        return get_sector_hot_stocks(target_total=target_total)

    recently_analyzed = _get_recently_analyzed_tickers(days=7)
    candidates = prescreen_whole_market(df, target_total, recently_analyzed)
    if not candidates:
        print("⚠️  全市场预筛无结果，回退到固定股票池选股")
        return get_sector_hot_stocks(target_total=target_total)

    grouped: Dict[str, List[Dict]] = {}
    for stock in candidates:
        sub_sector = get_stock_category(stock["ticker"])
        major = get_major_sector(sub_sector) if sub_sector != "其他" else "其他"
        grouped.setdefault(major, []).append(stock)

    print(f"✓ 全市场海选完成：{len(candidates)} 只候选，覆盖 {len(grouped)} 个大板块")
    return grouped


def _get_recently_analyzed_tickers(days: int = 7) -> Set[str]:
    try:
        from datetime import timedelta
        from app.config import now_cn
        from app.database import SessionLocal, AnalysisTask

        db = SessionLocal()
        try:
            cutoff = now_cn() - timedelta(days=days)
            rows = db.query(AnalysisTask.ticker).filter(AnalysisTask.created_at >= cutoff).all()
            return {row[0] for row in rows}
        finally:
            db.close()
    except Exception:
        return set()


def get_sector_hot_stocks(top_n_per_major_sector: int = 20, target_total: int = None) -> Dict[str, List[Dict]]:
    """
    获取各大板块热门股票
    
    Args:
        top_n_per_major_sector: 每个大板块取TOP N（默认20）
        target_total: 全市场目标数量，传入后按三池候选质量截断到该数量
    
    返回: {
        "科技创新": [{"ticker": "688981.SH", "name": "中芯国际", ...}, ...],
        ...
    }
    """
    from app.stock_pool import A_STOCK_POOL, get_major_sector
    
    # 先获取所有股票的实时行情
    print("正在获取A股实时行情...")
    df = get_a_share_realtime_tencent()
    
    if df.empty:
        print("⚠️  未能获取行情数据")
        return {}
    
    # 按大板块分组
    major_sector_stocks = {}
    for sub_sector, stocks in A_STOCK_POOL.items():
        major_sector = get_major_sector(sub_sector)
        if major_sector not in major_sector_stocks:
            major_sector_stocks[major_sector] = []
        major_sector_stocks[major_sector].extend(stocks)
    
    # 去重
    for sector in major_sector_stocks:
        major_sector_stocks[sector] = list(set(major_sector_stocks[sector]))
    
    recently_analyzed = _get_recently_analyzed_tickers(days=7)

    # 为每个大板块按三池选股：动量 40%、质量 40%、轮动 20%
    all_hot_stocks = {}
    
    overflow_candidates = []

    for major_sector, stocks in major_sector_stocks.items():
        print(f"\n正在选取 {major_sector} 板块热门股票...")
        
        momentum_n = max(1, round(top_n_per_major_sector * 0.4))
        quality_n = max(1, round(top_n_per_major_sector * 0.4))
        rotation_n = max(0, top_n_per_major_sector - momentum_n - quality_n)

        pools = [
            select_stocks_by_momentum(stocks, df, top_n=momentum_n),
            select_stocks_by_quality_proxy(stocks, df, top_n=quality_n),
            select_stocks_by_rotation(stocks, df, recently_analyzed, top_n=rotation_n),
        ]
        hot_stocks = []
        seen = set()
        for pool in pools:
            for stock in pool:
                if stock['ticker'] in seen:
                    continue
                seen.add(stock['ticker'])
                hot_stocks.append(stock)
        if len(hot_stocks) < top_n_per_major_sector:
            fallback = select_stocks_by_momentum(stocks, df, top_n=top_n_per_major_sector * 2)
            for stock in fallback:
                if stock['ticker'] in seen:
                    continue
                seen.add(stock['ticker'])
                hot_stocks.append(stock)
                if len(hot_stocks) >= top_n_per_major_sector:
                    break
        
        if hot_stocks:
            all_hot_stocks[major_sector] = hot_stocks
            for stock in hot_stocks:
                pool_bonus = {"momentum": 3, "quality": 2, "rotation": 1}.get(stock.get("source_pool"), 0)
                stock["selection_score"] = stock.get("amount", 0) + stock.get("change_pct", 0) * 1000 + pool_bonus * 100000
                overflow_candidates.append((major_sector, stock))
            print(f"  ✓ 选出 {len(hot_stocks)} 只:")
            for i, stock in enumerate(hot_stocks[:5], 1):  # 只显示前5只
                print(f"    {i}. {stock['name']}: {stock['selected_reason']}")
            if len(hot_stocks) > 5:
                print(f"    ... 还有 {len(hot_stocks)-5} 只")
        else:
            print(f"  ✗ 未找到符合条件的股票")

    if target_total and overflow_candidates:
        overflow_candidates.sort(key=lambda x: x[1].get("selection_score", 0), reverse=True)
        picked_by_sector = {sector: [] for sector in all_hot_stocks}
        used = set()
        # 先每个有候选的大板块保底 1 只，避免积极风格下单板块过度集中。
        for sector in all_hot_stocks:
            for cand_sector, stock in overflow_candidates:
                if cand_sector == sector and stock["ticker"] not in used:
                    picked_by_sector[sector].append(stock)
                    used.add(stock["ticker"])
                    break
        for sector, stock in overflow_candidates:
            if len(used) >= target_total:
                break
            if stock["ticker"] in used:
                continue
            picked_by_sector.setdefault(sector, []).append(stock)
            used.add(stock["ticker"])
        all_hot_stocks = {sector: stocks for sector, stocks in picked_by_sector.items() if stocks}
        print(f"\n✓ 三池候选全局截断：目标 {target_total} 只，实际 {sum(len(v) for v in all_hot_stocks.values())} 只")
    
    return all_hot_stocks


if __name__ == "__main__":
    print("=" * 60)
    print("测试市场数据获取")
    print("=" * 60)
    
    print("\n1. 获取A股实时行情...")
    df = get_a_share_realtime_tencent()
    
    if not df.empty:
        print(f"\n获取到 {len(df)} 只股票")
        print("\n前10只股票：")
        print(df[['code', 'name', 'price', 'change_pct', 'volume']].head(10))
        
        print("\n2. 测试选股功能...")
        result = get_sector_hot_stocks()
        
        print(f"\n" + "=" * 60)
        print(f"选股完成！共 {len(result)} 个板块，{sum(len(v) for v in result.values())} 只股票")
        print("=" * 60)
    else:
        print("未能获取行情数据")
