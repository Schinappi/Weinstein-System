"""Update index daily bars for today from Tushare."""
import sys
from pathlib import Path
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SRC_ROOT = Path("src").resolve()
sys.path.insert(0, str(SRC_ROOT))
from winstan.config import load_config
from winstan.adapters.tushare_client import build_tushare_pro

config = load_config(Path("config/strategy.yaml"))
_, pro = build_tushare_pro(config.data.tushare_token)

TODAY = "20260529"
TODAY_DT = pd.Timestamp("2026-05-29")
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

# Fetch index data from Tushare
for idx_code in ["000001.SH", "000906.SH"]:
    print(f"Fetching {idx_code}...")
    idx = pro.index_daily(ts_code=idx_code, start_date=TODAY, end_date=TODAY)
    if idx.empty:
        print(f"  No data for {idx_code}")
        continue
    
    r = idx.iloc[0]
    row = pd.DataFrame([{
        "symbol": idx_code,
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
    
    # Merge with existing
    idx_path = Path(f"data/parquet/index_bars/{idx_code}.parquet")
    if idx_path.exists():
        existing = pd.read_parquet(idx_path)
        existing = existing[existing["trade_date"] != TODAY_DT]
        combined = pd.concat([existing, row], ignore_index=True)
    else:
        combined = row
    
    table = pa.Table.from_pandas(combined, schema=SCHEMA)
    pq.write_table(table, str(idx_path))
    print(f"  {idx_code}: close={r['close']}, total rows={len(combined)}")

print("Index update done!")
