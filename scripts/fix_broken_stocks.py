"""Re-download full history for 317 broken stocks."""
import os
import sys
import time
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SRC_ROOT = Path("src").resolve()
sys.path.insert(0, str(SRC_ROOT))
from winstan.config import load_config
from winstan.adapters.tushare_client import build_tushare_pro

PARQUET_DIR = Path("data/parquet/daily_bars")
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

config = load_config(Path("config/strategy.yaml"))
_, pro = build_tushare_pro(config.data.tushare_token)

# Find stocks that need re-download (missing parquet files entirely)
missing = []
for fname in sorted(os.listdir(PARQUET_DIR)):
    sym = fname.replace(".parquet", "")
    try:
        df = pd.read_parquet(f"{PARQUET_DIR}/{fname}")
        has_today = (pd.to_datetime(df["trade_date"]) == pd.Timestamp("2026-05-29")).any() if not df.empty else False
    except:
        has_today = False
    if not has_today:
        # Check if it's one of the deleted stocks or just never had 5/29 data
        # We'll just re-download all of them
        pass

# Simpler: just check which stocks have NO data (deleted files)
# Compare parquet files with Tushare universe to find missing symbols
print("Checking which stocks are missing...")

# Get full universe from existing parquet + dir listing
existing_symbols = set(f.replace(".parquet", "") for f in os.listdir(PARQUET_DIR) if f.endswith(".parquet"))
print(f"Existing parquet files: {len(existing_symbols)}")

# Get symbols that Tushare returned today
print("Fetching Tushare daily data for today to find full universe...")
today_data = pro.daily(trade_date="20260529")
all_symbols = set(today_data["ts_code"].tolist())
print(f"Tushare universe: {len(all_symbols)}")

missing_in_parquet = all_symbols - existing_symbols
print(f"Missing symbols (need re-download): {len(missing_in_parquet)}")
print(f"Sample: {sorted(missing_in_parquet)[:20]}")

if not missing_in_parquet:
    print("No stocks need re-download!")
    sys.exit(0)

# Re-download from Tushare
START = "20220101"
END = "20260529"
done = 0
errors = 0
total = len(missing_in_parquet)
t0 = time.time()

for i, sym in enumerate(sorted(missing_in_parquet)):
    try:
        df = pro.daily(ts_code=sym, start_date=START, end_date=END)
        if df.empty:
            errors += 1
            continue
        
        # Get adj_factor
        try:
            adj = pro.adj_factor(ts_code=sym, trade_date=END)
            adj_factor = float(adj["adj_factor"].values[0]) if not adj.empty else 1.0
        except:
            adj_factor = 1.0
        
        # Build dataframe with correct schema
        rows = []
        for _, r in df.iterrows():
            rows.append({
                "symbol": sym,
                "trade_date": pd.Timestamp(str(r["trade_date"])),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("vol", 0)),
                "amount": float(r.get("amount", 0)),
                "adj_factor": adj_factor,
                "source": "tushare",
            })
        
        result_df = pd.DataFrame(rows).sort_values("trade_date")
        table = pa.Table.from_pandas(result_df, schema=SCHEMA)
        pq.write_table(table, str(PARQUET_DIR / f"{sym}.parquet"))
        done += 1
        
    except Exception as e:
        errors += 1
        if errors <= 5:
            print(f"  Error {sym}: {str(e)[:100]}")
    
    if (i + 1) % 50 == 0:
        elapsed = time.time() - t0
        print(f"  Progress: {i+1}/{total}, done={done}, errors={errors}, elapsed={elapsed:.0f}s")

elapsed = time.time() - t0
print(f"\nDone! Success: {done}, Errors: {errors}, Elapsed: {elapsed:.0f}s")
