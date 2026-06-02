import pandas as pd

# 检查 603213 的日线高点和周线高点
df = pd.read_parquet('data/parquet/daily_bars/603213.SH.parquet').sort_values('trade_date')
df['trade_date'] = pd.to_datetime(df['trade_date'])

# 周线重采样 (W-FRI)
weekly = df.resample('W-FRI', on='trade_date').agg({
    'open':'first','high':'max','low':'min','close':'last','volume':'sum'
}).dropna().reset_index()

# 最近 52 周
lookback = 52
recent_weekly = weekly.tail(lookback)

# 查找高于当前收盘价 14.70 的摆动高点
current_close = 14.70
highs = recent_weekly['high'].tolist()
dates = recent_weekly['trade_date'].tolist()

swing_highs = []
for i in range(1, len(highs)-1):
    if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]:
        if highs[i] > current_close:
            swing_highs.append((str(dates[i])[:10], float(highs[i])))

print('=== 最近 52 周内 > 14.70 的摆动高点 ===')
for d, h in swing_highs:
    hr = (h/current_close - 1)*100
    print(f'  {d}: high={h:.4f}, headroom={hr:.2f}%')

print()
print(f'52周最高价: {max(highs):.2f}')

# 检查高点 15.35 的日线详情
print()
print('=== 2025年7月22日附近日线 ===')
july = df[(df['trade_date'] >= '2025-07-01') & (df['trade_date'] <= '2025-07-31')]
for _, r in july.iterrows():
    print(f'  {str(r["trade_date"])[:10]}: O={r["open"]:.3f}, H={r["high"]:.3f}, L={r["low"]:.3f}, C={r["close"]:.3f}')
