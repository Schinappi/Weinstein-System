"""Verify the re-fetched data for today."""
import os
import pandas as pd
import pyarrow.parquet as pq

PARQUET_DIR = "data/parquet/daily_bars"
TODAY_DT = pd.Timestamp("2026-05-29")

# 1. Check sample stocks
print("=== Sample stock verification ===")
for sym in ["600519.SH", "000001.SZ", "000333.SZ", "600036.SH"]:
    df = pd.read_parquet(f"{PARQUET_DIR}/{sym}.parquet")
    today = df[df["trade_date"] == TODAY_DT]
    last_rows = df.tail(5)
    print(f"\n{sym}:")
    print(f"  5/29 data present: {not today.empty}")
    if not today.empty:
        r = today.iloc[0]
        print(f"  close={r['close']}, open={r['open']}, high={r['high']}, low={r['low']}, vol={r['volume']}, source={r['source']}, adj_factor={r['adj_factor']}")
    print(f"  Total rows: {len(df)}")
    print(f"  Last 3 dates: {df['trade_date'].tail(3).tolist()}")

# 2. Count stocks with/without 5/29 data
print("\n=== Coverage check ===")
files = sorted(os.listdir(PARQUET_DIR))
has_today = 0
missing_today = 0
for fname in files:
    if not fname.endswith(".parquet"):
        continue
    try:
        df = pd.read_parquet(f"{PARQUET_DIR}/{fname}")
        if (df["trade_date"] == TODAY_DT).any():
            has_today += 1
        else:
            missing_today += 1
    except:
        missing_today += 1

print(f"Has 5/29 data: {has_today}")
print(f"Missing 5/29: {missing_today}")

# 3. Check index
print("\n=== Index verification ===")
for idx_sym in ["000906.SH", "000001.SH"]:
    idx_path = f"data/parquet/index_bars/{idx_sym}.parquet"
    if os.path.exists(idx_path):
        df = pd.read_parquet(idx_path)
        today = df[df["trade_date"] == TODAY_DT]
        print(f"{idx_sym}: 5/29 data={not today.empty}")
        if not today.empty:
            print(f"  close={today.iloc[0]['close']}")
        print(f"  Total rows: {len(df)}, last dates: {df['trade_date'].tail(3).tolist()}")

# 4. Check duplicate trade_dates
print("\n=== Duplicate check (sample 500) ===")
dup_found = 0
for fname in sorted(os.listdir(PARQUET_DIR))[:500]:
    try:
        df = pd.read_parquet(f"{PARQUET_DIR}/{fname}")
        dup = df["trade_date"].duplicated(keep=False)
        if dup.any():
            dup_found += 1
            print(f"  {fname.replace('.parquet','')}: {dup.sum()} duplicates")
    except:
        pass
print(f"Files with duplicates: {dup_found}/500")
