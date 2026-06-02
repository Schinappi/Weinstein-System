"""Fix the error stocks by rewriting their parquet files with a clean schema."""
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

TODAY_DT = pd.Timestamp("2026-05-29")
PARQUET_DIR = Path("data/parquet/daily_bars")

# Define a clean schema matching 5188 successfully written files
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

files = sorted(os.listdir(PARQUET_DIR))
fixed = 0
errors = 0

for fname in files:
    if not fname.endswith(".parquet"):
        continue
    fpath = PARQUET_DIR / fname
    # Check if readable with our schema
    try:
        df = pd.read_parquet(fpath)
        # Try writing with explicit schema
        table = pa.Table.from_pandas(df, schema=SCHEMA)
        continue  # No issue
    except Exception:
        pass
    
    # Need to fix this file
    try:
        df = pd.read_parquet(fpath)
        # Preserve 5/29 data from Tushare if already written
        today_df = df[df["trade_date"] == TODAY_DT] if "trade_date" in df.columns else pd.DataFrame()
        
        # Re-write with clean schema
        for col in SCHEMA.names:
            if col not in df.columns:
                df[col] = None
        
        df = df[SCHEMA.names]
        
        # Convert types
        str_cols = ["symbol", "source"]
        float_cols = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]
        
        for c in str_cols:
            if c in df.columns:
                df[c] = df[c].astype(str)
        for c in float_cols:
            if c in df.columns and c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0).astype(float)
        
        if "trade_date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["trade_date"])
        
        # Write with explicit schema
        table = pa.Table.from_pandas(df, schema=SCHEMA)
        pq.write_table(table, str(fpath))
        fixed += 1
    except Exception as e:
        errors += 1
        if errors <= 5:
            print(f"Cannot fix {fname}: {str(e)[:100]}")

print(f"Fixed: {fixed}, Still errors: {errors}")
