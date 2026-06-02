"""Verify 603213.SH raw data after full Tushare re-download."""
import pandas as pd

df = pd.read_parquet("data/parquet/daily_bars/603213.SH.parquet")
print(f"Total rows: {len(df)}")
print(f"Date range: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
print(f"All source: {df['source'].unique()}")
print(f"Sample adj_factor: {df['adj_factor'].unique()[:5]}")

# Check 2025-07-22
target = pd.Timestamp("2025-07-22")
row = df[df["trade_date"] == target]
if not row.empty:
    print(f"\n=== 2025-07-22 (用户验证关键点) ===")
    print(f"  Open:   {row['open'].values[0]}")
    print(f"  High:   {row['high'].values[0]}  ← 用户说是 15.35")
    print(f"  Low:    {row['low'].values[0]}")
    print(f"  Close:  {row['close'].values[0]}")
    print(f"  Volume: {row['volume'].values[0]}")
    print(f"  Source: {row['source'].values[0]}")
    print(f"  adj_factor: {row['adj_factor'].values[0]}")
    
    expected_high = 15.35
    actual_high = row['high'].values[0]
    diff = abs(actual_high - expected_high)
    print(f"\n  High -> 预期: {expected_high}, 实际: {actual_high:.2f}, 差值: {diff:.4f}")
    if diff < 0.01:
        print("  ✅ 完全一致！")
    else:
        print(f"  ❌ 有差异: {diff:.4f}")

# Check that NO weekend data exists
print("\n=== 检查是否有非交易日数据 ===")
df["dow"] = df["trade_date"].dt.dayofweek
weekend = df[df["dow"].isin([5, 6])]
print(f"周末/非交易日行数: {len(weekend)}")
if len(weekend) > 0:
    print(f"有问题！非交易日数据: {weekend['trade_date'].head(5).tolist()}")
else:
    print("✅ 没有非交易日数据！")

# Quick check around July 2025
mask = (df["trade_date"] >= pd.Timestamp("2025-07-16")) & (df["trade_date"] <= pd.Timestamp("2025-07-25"))
print(f"\n=== 2025-07-16 ~ 2025-07-25 ===")
subset = df[mask].copy()
dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
subset["dow"] = subset["trade_date"].dt.dayofweek
print(subset[["trade_date", "open", "high", "low", "close"]].to_string())

# 文件统计
import os
files = os.listdir("data/parquet/daily_bars/")
print(f"\n=== 总文件数: {len(files)} ===")
