"""
Full re-download of ALL daily bars from Tushare - RAW (unadjusted) prices.
Each batch writes to a numbered subdirectory. Final merge combines all batches.
"""
import os
import sys
import time
import gc
import shutil
from pathlib import Path
from collections import defaultdict

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SRC_ROOT = Path("src").resolve()
sys.path.insert(0, str(SRC_ROOT))
from winstan.config import load_config
from winstan.adapters.tushare_client import build_tushare_pro

config = load_config(Path("config/strategy.yaml"))
_, pro = build_tushare_pro(config.data.tushare_token)

PARQUET_DIR = Path("data/parquet/daily_bars")
START = "20220101"
END = "20260529"
DAYS_PER_BATCH = 100

SCHEMA = pa.schema([
    pa.field("symbol", pa.large_string()),
    pa.field("trade_date", pa.timestamp("us")),
    pa.field("open", pa.float64()),
    pa.field("high", pa.float64()),
    pa.field("low", pa.float64()),
    pa.field("close", pa.float64()),
    pa.field("volume", pa.float64()),
    pa.field("amount", pa.float64()),
    pa.field("adj_factor", pa.float64()),
    pa.field("source", pa.large_string()),
])


def write_batch(symbol_rows_map, batch_dir):
    """Write batch data to a numbered subdirectory."""
    batch_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for sym, rows in symbol_rows_map.items():
        if not rows:
            continue
        try:
            rows.sort(key=lambda x: x["trade_date"])
            df = pd.DataFrame(rows)
            table = pa.Table.from_pandas(df, schema=SCHEMA)
            pq.write_table(table, str(batch_dir / f"{sym}.parquet"))
            written += 1
        except Exception as e:
            pass
    return written


def merge_all_batches(batch_dirs, output_dir):
    """Read all batch files per stock, merge, deduplicate, write to output."""
    # Build mapping: symbol -> list of batch files
    stock_files = defaultdict(list)
    for bd in batch_dirs:
        if not bd.exists():
            continue
        for fname in os.listdir(bd):
            if fname.endswith(".parquet"):
                stock_files[fname.replace(".parquet", "")].append(bd / fname)
    
    print(f"  Merging {len(stock_files)} stocks from {len(batch_dirs)} batches...")
    merge_t0 = time.time()
    written = 0
    errors = 0
    
    for idx, (sym, files) in enumerate(sorted(stock_files.items())):
        try:
            frames = [pd.read_parquet(f) for f in files]
            combined = pd.concat(frames, ignore_index=True)
            combined = combined.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
            combined = combined.reset_index(drop=True)
            
            table = pa.Table.from_pandas(combined, schema=SCHEMA)
            pq.write_table(table, str(output_dir / f"{sym}.parquet"))
            written += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  Merge error {sym}: {str(e)[:80]}")
        
        if (idx + 1) % 1000 == 0:
            print(f"  Merged {idx+1}/{len(stock_files)}...")
    
    merge_time = time.time() - merge_t0
    print(f"  Merge done: {written} stocks, {errors} errors, {merge_time:.0f}s")
    return written, errors


# Step 1: Get trading calendar
print("Step 1: Getting trading calendar...")
t0 = time.time()
cal = pro.trade_cal(exchange="SSE", start_date=START, end_date=END, is_open="1")
if cal.empty:
    print("ERROR: No trading calendar!")
    sys.exit(1)

trade_dates = sorted(cal["cal_date"].tolist())
print(f"  {len(trade_dates)} trading days ({time.time()-t0:.1f}s)")

# Step 2: Get adj_factor for all stocks
print("\nStep 2: Getting adj_factor for all stocks...")
t0 = time.time()
adj_all = pro.adj_factor(trade_date=END)
if adj_all.empty:
    adj_all = pro.adj_factor(trade_date=trade_dates[-1])
adj_factor_map = {}
if not adj_all.empty:
    for _, r in adj_all.iterrows():
        adj_factor_map[r["ts_code"]] = float(r["adj_factor"])
print(f"  Got {len(adj_factor_map)} adj_factors ({time.time()-t0:.1f}s)")

# Step 3: Clean old data
temp_root = PARQUET_DIR.parent / "daily_bars_temp"
if temp_root.exists():
    shutil.rmtree(temp_root)

existing = [f for f in os.listdir(PARQUET_DIR) if f.endswith(".parquet")]
if existing:
    print(f"\nStep 3: Deleting {len(existing)} old files...")
    for fname in existing:
        (PARQUET_DIR / fname).unlink()

# Step 4: Fetch by trading day, write to batch dirs
print(f"\nStep 4: Fetching {len(trade_dates)} days...")
overall_t0 = time.time()
day_count = 0
batch_round = 0
fetch_time = 0.0
batch_dirs = []

for batch_start in range(0, len(trade_dates), DAYS_PER_BATCH):
    batch_dates = trade_dates[batch_start:batch_start + DAYS_PER_BATCH]
    batch_round += 1
    batch_dir = temp_root / f"batch_{batch_round:02d}"
    batch_dirs.append(batch_dir)
    
    batch_t0 = time.time()
    batch_data = defaultdict(list)
    batch_row_count = 0
    
    for td in batch_dates:
        try:
            df = pro.daily(trade_date=td)
            if df.empty:
                day_count += 1
                continue
            for _, r in df.iterrows():
                sym = r["ts_code"]
                adj = adj_factor_map.get(sym, 1.0)
                batch_data[sym].append({
                    "symbol": sym,
                    "trade_date": pd.Timestamp(str(td)),
                    "open": float(r.get("open", 0)),
                    "high": float(r.get("high", 0)),
                    "low": float(r.get("low", 0)),
                    "close": float(r.get("close", 0)),
                    "volume": float(r.get("vol", 0)),
                    "amount": float(r.get("amount", 0)),
                    "adj_factor": adj,
                    "source": "tushare",
                })
            batch_row_count += len(df)
            day_count += 1
        except Exception:
            day_count += 1
    
    write_start = time.time()
    w = write_batch(batch_data, batch_dir)
    fetch_time += time.time() - batch_t0
    batch_elapsed = time.time() - batch_t0
    total_elapsed = time.time() - overall_t0
    
    pct = min(day_count, len(trade_dates)) / len(trade_dates) * 100
    eta = (total_elapsed / max(day_count, 1)) * (len(trade_dates) - day_count)
    
    print(f"  Batch {batch_round}: days {batch_start+1}-{min(batch_start+DAYS_PER_BATCH, len(trade_dates))} "
          f"| {batch_row_count:,} rows | {batch_elapsed:.0f}s | {pct:.0f}% ~{eta:.0f}s")
    
    del batch_data
    gc.collect()

fetch_time = time.time() - overall_t0

print(f"\nStep 5: Merging {len(batch_dirs)} batches...")
mw, me = merge_all_batches(batch_dirs, PARQUET_DIR)

# Clean up temp dirs
print("\nCleaning up temp directories...")
shutil.rmtree(temp_root, ignore_errors=True)

total_time = time.time() - overall_t0
file_count = len([f for f in os.listdir(PARQUET_DIR) if f.endswith(".parquet")])

print(f"\n{'='*55}")
print(f"  RE-DOWNLOAD COMPLETE!")
print(f"  Trading days: {day_count}/{len(trade_dates)}")
print(f"  Fetch time: {fetch_time:.0f}s ({fetch_time/60:.1f}min)")
print(f"  Total time: {total_time:.0f}s ({total_time/60:.1f}min)")
print(f"  Stock files: {file_count}")
print(f"  Merge errors: {me}")
print(f"  Prices: RAW (unadjusted)!")
print(f"{'='*55}")
