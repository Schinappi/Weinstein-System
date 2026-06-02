"""Batch fetch today's daily bars from Tushare and write to parquet files."""
import os
import sys
import time
from pathlib import Path

import pandas as pd

SRC_ROOT = Path("src").resolve()
sys.path.insert(0, str(SRC_ROOT))
from winstan.config import load_config
from winstan.adapters.tushare_client import build_tushare_pro

TODAY = "20260529"
TODAY_DT = pd.Timestamp("2026-05-29")
PARQUET_DIR = Path("data/parquet/daily_bars")
BATCH_SIZE = 2000

config = load_config(Path("config/strategy.yaml"))
_, pro = build_tushare_pro(config.data.tushare_token)

print("Fetching ALL stocks daily data for 2026-05-29 from Tushare...")
t0 = time.time()
df = pro.daily(trade_date=TODAY)
elapsed = time.time() - t0
print(f"Tushare returned {len(df)} rows in {elapsed:.2f}s")

if df.empty:
    print("ERROR: No data returned!")
    sys.exit(1)

print("Fetching adj_factor...")
adj_t0 = time.time()
adj_df = pro.adj_factor(trade_date=TODAY)
adj_elapsed = time.time() - adj_t0
print(f"Adj factor: {len(adj_df)} rows in {adj_elapsed:.2f}s")

# Merge adj_factor
merged = pd.merge(df, adj_df[["ts_code", "adj_factor"]], on="ts_code", how="left")
merged["adj_factor"] = merged["adj_factor"].fillna(1.0)

# Build output rows
rows = []
for _, r in merged.iterrows():
    rows.append({
        "symbol": r["ts_code"],
        "trade_date": TODAY_DT,
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
        "volume": float(r.get("tushare_vol", 0) if "tushare_vol" in merged.columns else r.get("vol", 0)),
        "amount": float(r.get("tushare_amount", 0) if "tushare_amount" in merged.columns else r.get("amount", 0)),
        "adj_factor": float(r["adj_factor"]),
        "source": "tushare",
    })

print(f"Total rows: {len(rows)}")

# Write to parquet files - use same schema as existing files
written = 0
errors = 0
error_msgs = set()

for i in range(0, len(rows), BATCH_SIZE):
    batch = rows[i:i+BATCH_SIZE]
    batch_t0 = time.time()
    
    for row_data in batch:
        symbol = row_data["symbol"]
        fpath = PARQUET_DIR / f"{symbol}.parquet"
        try:
            new_row = pd.DataFrame([row_data])
            
            if fpath.exists():
                existing = pd.read_parquet(fpath)
                # Remove any existing row for today
                existing = existing[existing["trade_date"] != TODAY_DT]
                if not existing.empty:
                    # Preserve schema by joining on columns
                    updated = pd.concat([existing, new_row], ignore_index=True)
                else:
                    updated = new_row
            else:
                updated = new_row
            
            updated.to_parquet(fpath, index=False)
            written += 1
        except Exception as e:
            errors += 1
            msg = str(e)[:80]
            if msg not in error_msgs:
                error_msgs.add(msg)
                print(f"  Error {symbol}: {msg}")
    
    batch_elapsed = time.time() - batch_t0
    remaining = len(rows) - (i + len(batch))
    print(f"  Batch {i//BATCH_SIZE + 1}: {len(batch)} stocks, {batch_elapsed:.1f}s, total written={written}, remaining≈{remaining}")

print(f"\nDone! Written: {written}, Errors: {errors}")

# Update index data
print("\nUpdating index data...")
try:
    idx = pro.index_daily(ts_code="000001.SH", start_date=TODAY, end_date=TODAY)
    if not idx.empty:
        r = idx.iloc[0]
        idx_row = pd.DataFrame([{
            "symbol": "000906.SH",
            "trade_date": TODAY_DT,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["vol"]),
            "amount": float(r["amount"]),
            "adj_factor": 1.0,
            "source": "tushare",
        }])
        
        for sym in ["000906.SH", "000001.SH"]:
            idx_path = Path(f"data/parquet/index_bars/{sym}.parquet")
            if idx_path.exists():
                existing_idx = pd.read_parquet(idx_path)
                existing_idx = existing_idx[existing_idx["trade_date"] != TODAY_DT]
                final_idx = pd.concat([existing_idx, idx_row], ignore_index=True)
            else:
                final_idx = idx_row.copy()
                final_idx["symbol"] = sym
            final_idx.to_parquet(idx_path, index=False)
            print(f"  {sym}: close={r['close']}")
    else:
        print("No index data returned")
except Exception as e:
    print(f"Index update error: {e}")

print("\nAll done!")
