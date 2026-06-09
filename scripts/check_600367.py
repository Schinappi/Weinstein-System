#!/usr/bin/env python3
"""Check 600367 红星发展 — weekly bars & stage determination."""

import sys
sys.path.insert(0, 'src')
from pathlib import Path
import duckdb
import pandas as pd

# Connect to DuckDB with weekly parquet
db_path = Path("data/winstan.db")
store_path = db_path  # DuckDBStore expects a path

conn = duckdb.connect(str(db_path))

# Check if we have the weekly bars pointed at the right parquet files
# First let's try to see what parquet files exist
import glob as g
weekly_files = g.glob("data/parquet/daily_bars/weekly/*.parquet")
daily_files = g.glob("data/parquet/daily_bars/*.parquet")
print(f"Weekly parquet files: {len(weekly_files)}")
print(f"Daily parquet files: {len(daily_files)}")
if weekly_files:
    print(f"Sample weekly: {weekly_files[0]}")

# Let's query the raw weekly data for 600367
weekly_glob = "data/parquet/daily_bars/weekly/*.parquet"
try:
    df = conn.execute(f"""
        SELECT * FROM read_parquet('{weekly_glob}')
        WHERE ts_code LIKE '600367%'
        ORDER BY week_end DESC
        LIMIT 30
    """).fetchdf()
    print(f"\n📊 Weekly bars for 600367.SH:")
    print(f"Columns: {list(df.columns)}")
    print(df.to_string(max_rows=50))
except Exception as e:
    print(f"Error querying weekly: {e}")
    # Try daily
    try:
        daily_glob = "data/parquet/daily_bars/*.parquet"
        df = conn.execute(f"""
            SELECT * FROM read_parquet('{daily_glob}')
            WHERE ts_code LIKE '600367%'
            ORDER BY trade_date DESC
            LIMIT 30
        """).fetchdf()
        print(f"\n📊 Daily bars for 600367.SH:")
        print(f"Columns: {list(df.columns)}")
        print(df.to_string(max_rows=50))
    except Exception as e2:
        print(f"Error querying daily: {e2}")
