"""Check tushare data coverage in parquet files."""
import os
import pandas as pd

parquet_dir = "data/parquet/daily_bars"
files = sorted(os.listdir(parquet_dir))

sample_symbols = ["600519.SH", "000001.SZ", "000002.SZ", "000333.SZ", "600036.SH"]

for sym in sample_symbols:
    df = pd.read_parquet(f"{parquet_dir}/{sym}.parquet")
    tushare = df[df["source"] == "tushare"]
    tickflow = df[df["source"] == "tickflow"]
    print(f'{sym}: tushare rows={len(tushare)}, range={tushare["trade_date"].min()}~{tushare["trade_date"].max()}, tickflow rows={len(tickflow)}, range={tickflow["trade_date"].min()}~{tickflow["trade_date"].max()}')
    tushare_dates = set(tushare["trade_date"])
    tickflow_dates = set(tickflow["trade_date"])
    overlap = tushare_dates & tickflow_dates
    if overlap:
        print(f"  Overlap dates: {sorted(overlap)[:10]} ...")

# Broad sample
all_min = []
all_max = []
all_rows = []
missing_529 = 0
for fname in files[:500]:
    df = pd.read_parquet(f"{parquet_dir}/{fname}")
    if "source" not in df.columns:
        continue
    tushare = df[df["source"] == "tushare"]
    if not tushare.empty:
        all_min.append(tushare["trade_date"].min())
        all_max.append(tushare["trade_date"].max())
        all_rows.append(len(tushare))
        if tushare["trade_date"].max() < "2026-05-29":
            missing_529 += 1

import numpy as np
print(f"\n--- Sample {len(all_min)} stocks ---")
print(f"Min date coverage: min={min(all_min)}, max of mins={max(all_min)}")
print(f"Max date coverage: min={min(all_max)}, max={max(all_max)}")
print(f"Missing 5/29 data: {missing_529}/{len(all_min)}")
print(f"Avg tushare rows/stock: {np.mean(all_rows):.0f}")
