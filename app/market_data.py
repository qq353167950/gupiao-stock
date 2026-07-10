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
