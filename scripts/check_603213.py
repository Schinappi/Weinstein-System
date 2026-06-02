"""Analyze 603213.SH without running the full screener."""
import sys
from pathlib import Path
import pandas as pd
import numpy as np

sys.path.insert(0, "src")
from winstan.config import load_config
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.adapters.factory import DataSourceRouter

config = load_config(Path("config/strategy.yaml"))

# Load daily data
daily = pd.read_parquet("data/parquet/daily_bars/603213.SH.parquet")
print(f"Total daily rows: {len(daily)}")
print(f"Source distribution: {daily['source'].value_counts().to_dict()}")
print(f"Date range: {daily['trade_date'].min()} ~ {daily['trade_date'].max()}")

# Build weekly
weekly = build_weekly_bars(daily)
print(f"\nTotal weekly rows: {len(weekly)}")
print("\nLast 15 weekly bars:")
print(weekly.tail(15)[["trade_date", "open", "high", "low", "close", "volume", "adj_factor"]].to_string())

# Calculate key Weinstein indicators
weekly = weekly.copy()
weekly["ma_10w"] = weekly["close"].rolling(10).mean()
weekly["ma_30w"] = weekly["close"].rolling(30).mean()
weekly["price_above_ma_30w"] = weekly["close"] > weekly["ma_30w"]
weekly["ma_30w_up"] = weekly["ma_30w"] > weekly["ma_30w"].shift(1)
weekly["ma_10w_above_ma_30w"] = weekly["ma_10w"] > weekly["ma_30w"]

print("\n=== 最近20周的关键指标 ===")
check_cols = ["trade_date", "close", "ma_10w", "ma_30w", "price_above_ma_30w", "ma_30w_up", "ma_10w_above_ma_30w"]
print(weekly.tail(20)[check_cols].to_string())

# Check Stage II conditions
last = weekly.iloc[-1]
print(f"\n=== Stage II 条件检查（最新周: {last['trade_date']}）===")
print(f"1. close > ma_30w ? {last['close']:.2f} > {last['ma_30w']:.2f} = {last['close'] > last['ma_30w']}")
print(f"2. ma_30w 上升? {last['ma_30w']:.2f} > {weekly.iloc[-2]['ma_30w']:.2f} = {last['ma_30w'] > weekly.iloc[-2]['ma_30w']}")
print(f"3. ma_10w > ma_30w ? {last['ma_10w']:.2f} > {last['ma_30w']:.2f} = {last['ma_10w'] > last['ma_30w']}")

# Also check if the tickflow date shift is causing issues
print("\n=== TickFlow 日期偏移影响 ===")
# Last 10 daily rows around the transition
tail = daily.tail(12)
print("最近日线（观察 date shift）:")
print(tail[["trade_date", "close", "source", "adj_factor"]].to_string())

# Check: if tickflow 5/26 close=14.73 should be 5/27
# And tushare 5/27 close=14.73 is duplicate
print("\nTickflow 5/26 close=14.73")
print("Tushare 5/27 close=14.73 (duplicate!)")
print("This means the weekly bar has 2 rows for 5/27 (duplicate), distorting the OHLC")

# Check recent weekly in detail
print("\n=== 最近4根周K线详情 ===")
recent = weekly.tail(4)
for _, r in recent.iterrows():
    print(f"Week ending {r['trade_date'].strftime('%Y-%m-%d')}:")
    print(f"  O={r['open']:.2f} H={r['high']:.2f} L={r['low']:.2f} C={r['close']:.2f}")
    print(f"  MA10={r['ma_10w']:.2f} MA30={r['ma_30w']:.2f}")
    print(f"  Vol={r['volume']:.0f}")

# RS line check
print("\n=== RS 相对强度 ===")
# Get market weekly
market_daily = pd.read_parquet("data/parquet/index_bars/000906.SH.parquet")
market_weekly = build_weekly_bars(market_daily)
print(f"Market weekly rows: {len(market_weekly)}")
print("Last 5 market weekly:")
print(market_weekly.tail(5)[["trade_date", "close"]].to_string())

# Calculate RS
weekly = weekly.reset_index(drop=True)
weekly["trade_date"] = pd.to_datetime(weekly["trade_date"])
market_weekly["trade_date"] = pd.to_datetime(market_weekly["trade_date"])

merged = pd.merge(weekly, market_weekly[["trade_date", "close"]], on="trade_date", how="left", suffixes=("", "_market"))
merged["rs_line"] = merged["close"] / merged["close_market"]
print("\nRS Line (4-week):")
print(merged.tail(4)[["trade_date", "close", "close_market", "rs_line"]].to_string())
