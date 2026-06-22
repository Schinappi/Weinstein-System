"""Temporary script: check Tushare raw data for 603213.SH"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from winstan.config import load_config
from winstan.adapters.tushare_client import build_tushare_pro

config = load_config("config/strategy.yaml")
_, pro = build_tushare_pro(config.data.tushare_token)

# Raw daily data for 2025-07-22
daily = pro.daily(ts_code="603213.SH", start_date="20250722", end_date="20250722")
print("=== Tushare daily (unadjusted) 2025-07-22 ===")
print(daily.to_string())

# Adj factor for full history
adj = pro.adj_factor(ts_code="603213.SH")
print("\n=== Adj factor ===")
print(f"Rows: {len(adj)}")
print(adj.head(10).to_string())
print("...")
print(adj.tail(10).to_string())

# Forward-adjusted daily data for 2025-07-22
qfq = pro.daily(ts_code="603213.SH", start_date="20250722", end_date="20250722", adj="qfq")
print("\n=== Tushare daily (qfq 前复权) 2025-07-22 ===")
print(qfq.to_string())
