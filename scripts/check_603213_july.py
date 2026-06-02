"""Verify 603213.SH data around 2025-07-22."""
import pandas as pd

df = pd.read_parquet("data/parquet/daily_bars/603213.SH.parquet")
print(f"Total rows: {len(df)}")
print(f"Date range: {df['trade_date'].min()} ~ {df['trade_date'].max()}")

target = pd.Timestamp("2025-07-22")
row = df[df["trade_date"] == target]
if not row.empty:
    print(f"\n2025-07-22 data:")
    print(f"  Open:   {row['open'].values[0]}")
    print(f"  High:   {row['high'].values[0]}")
    print(f"  Low:    {row['low'].values[0]}")
    print(f"  Close:  {row['close'].values[0]}")
    print(f"  Volume: {row['volume'].values[0]}")
    print(f"  Source: {row['source'].values[0]}")
else:
    print("\n2025-07-22 无数据")

# Check what days of week these are
print("\n2025-07-16 ~ 2025-07-25:")
mask = (df["trade_date"] >= pd.Timestamp("2025-07-16")) & (df["trade_date"] <= pd.Timestamp("2025-07-25"))
subset = df[mask].copy()
subset["dow"] = subset["trade_date"].dt.dayofweek  # Mon=0, Sun=6
dow_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
subset["dow_name"] = subset["dow"].map(dow_names)
print(subset[["trade_date", "dow_name", "open", "high", "low", "close"]].to_string())
