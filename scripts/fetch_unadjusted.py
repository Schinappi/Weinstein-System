"""Re-fetch all daily bar data from Tushare with forward adjustment (adj='qfq').

Uses concurrent workers to parallelize API calls while respecting the
Tushare rate limit (400 calls/minute). Each stock = 1 API call returning
already-forward-adjusted OHLC data.
"""
from __future__ import annotations

import gc
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore


WORKERS = 8
BATCH_REPORT = 500


def fetch_and_write(symbol: str, pro, start_date: str, end_date: str, store: ParquetStore) -> dict:
    """Fetch forward-adjusted data for one symbol and write to parquet."""
    try:
        raw = pro.daily(
            ts_code=symbol,
            start_date=start_date,
            end_date=end_date,
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )
        if raw is None or raw.empty:
            return {"symbol": symbol, "status": "empty", "rows": 0}

        frame = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
        frame["symbol"] = symbol
        frame["source"] = "tushare"

        cleaned = clean_daily_bars(frame)
        if cleaned.empty:
            return {"symbol": symbol, "status": "clean_empty", "rows": 0}

        store.write_symbol_frame("daily_bars", symbol, cleaned)
        return {"symbol": symbol, "status": "ok", "rows": len(cleaned)}
    except Exception as exc:
        return {"symbol": symbol, "status": "error", "error": str(exc), "rows": 0}


def main() -> None:
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)

    from winstan.adapters.tushare_client import build_tushare_pro
    _, pro = build_tushare_pro(config.data.tushare_token)

    symbols = store.list_cached_symbols("daily_bars")
    print(f"Re-fetching {len(symbols)} symbols with UNADJUSTED (除权) data...")
    print(f"Workers: {WORKERS}, Rate limit: 400 calls/min")

    # Compact date strings
    start = config.data.effective_start_date.replace("-", "")
    end = config.data.effective_end_date.replace("-", "")

    total = len(symbols)
    ok = 0
    empty = 0
    errors = 0
    total_rows = 0
    start_time = time.perf_counter()

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(fetch_and_write, sym, pro, start, end, store): sym
            for sym in symbols
        }

        batch_ok = 0
        for future in as_completed(futures):
            result = future.result()
            sym = result["symbol"]
            status = result["status"]

            if status == "ok":
                ok += 1
                batch_ok += 1
                total_rows += result["rows"]
            elif status in ("empty", "clean_empty"):
                empty += 1
            else:
                errors += 1
                print(f"  ERROR {sym}: {result.get('error', 'unknown')}")

            if batch_ok >= BATCH_REPORT:
                elapsed = time.perf_counter() - start_time
                rate = ok / (elapsed / 60)
                print(f"  Progress: {ok}/{total} ({(ok/total)*100:.0f}%) "
                      f"ok={ok} empty={empty} errors={errors} "
                      f"rows={total_rows} elapsed={elapsed:.0f}s rate={rate:.0f}/min")
                batch_ok = 0

            # Garbage collect periodically
            if ok % 100 == 0:
                gc.collect()

    elapsed = time.perf_counter() - start_time
    rate = ok / (elapsed / 60)
    print(f"\n=== Done ===")
    print(f"Total: {total}")
    print(f"OK: {ok}")
    print(f"Empty: {empty}")
    print(f"Errors: {errors}")
    print(f"Total rows: {total_rows}")
    print(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"Rate: {rate:.0f} stocks/min")
    if errors:
        print("WARNING: Some stocks failed!")


if __name__ == "__main__":
    main()
