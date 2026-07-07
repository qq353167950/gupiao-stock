"""
市场数据获取模块
使用腾讯财经API获取A股实时行情（与deep-analysis相同的数据源）
"""
import requests
import pandas as pd
from typing import List, Dict
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
        if df.empty:
            return []
        
        # 筛选指定板块的股票
        df = df[df['code'].isin([s.split('.')[0] for s in sector_stocks])].copy()
        
        if df.empty:
            return []
        
        # 过滤条件
        df = df[
            (df['change_pct'] > 0) & (df['change_pct'] <= 10) &  # 上涨但不过度
            (df['price'] > 0) &  # 有效价格
            (~df['name'].str.contains('ST', na=False))  # 排除ST
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
        
        # 返回结果
        results = []
        for _, row in df.iterrows():
            # 添加市场后缀
            code_with_market = row['code']
            if code_with_market.startswith('6'):
                code_with_market += '.SH'
            else:
                code_with_market += '.SZ'
            
            results.append({
                'ticker': code_with_market,
                'name': row['name'],
                'price': float(row['price']),
                'change_pct': float(row['change_pct']),
                'volume': float(row['volume']),
                'amount': float(row['amount']),
                'selected_reason': f"涨幅{row['change_pct']:.2f}% · 成交{row['amount']:.0f}万"
            })
        
        return results
        
    except Exception as e:
        print(f"选股失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def get_sector_hot_stocks(top_n_per_major_sector: int = 20) -> Dict[str, List[Dict]]:
    """
    获取各大板块热门股票
    
    Args:
        top_n_per_major_sector: 每个大板块取TOP N（默认20）
    
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
    
    # 为每个大板块选股
    all_hot_stocks = {}
    
    for major_sector, stocks in major_sector_stocks.items():
        print(f"\n正在选取 {major_sector} 板块热门股票...")
        
        # 选股
        hot_stocks = select_stocks_by_momentum(stocks, df, top_n=top_n_per_major_sector)
        
        if hot_stocks:
            all_hot_stocks[major_sector] = hot_stocks
            print(f"  ✓ 选出 {len(hot_stocks)} 只:")
            for i, stock in enumerate(hot_stocks[:5], 1):  # 只显示前5只
                print(f"    {i}. {stock['name']}: {stock['selected_reason']}")
            if len(hot_stocks) > 5:
                print(f"    ... 还有 {len(hot_stocks)-5} 只")
        else:
            print(f"  ✗ 未找到符合条件的股票")
    
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
