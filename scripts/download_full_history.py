"""Download full A-share history from 2018-01-01.

Uses per-day bulk queries to stay under 6000-row API limit.
"""
import sys, gc, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore
from winstan.adapters.tushare_client import build_tushare_pro

WORKERS = 8


def main() -> None:
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    _, pro = build_tushare_pro(config.data.tushare_token)

    cached = set(store.list_cached_symbols("daily_bars"))

    # Generate trading days from 2018-01-01 to 2021-12-31
    start = date(2018, 1, 1)
    end = date(2021, 12, 31)
    all_dates = []
    current = start
    while current <= end:
        all_dates.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)

    print(f"Fetching {len(all_dates)} calendar days from {start} to {end}...")

    t0 = time.perf_counter()
    total_rows = 0
    days_with_data = 0

    # Step 1: Fetch all days
    daily_frames: dict[str, pd.DataFrame] = {}
    for i, day_str in enumerate(all_dates):
        try:
            raw = pro.daily(
                trade_date=day_str,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
        except Exception:
            raw = None
        if raw is not None and not raw.empty:
            raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
            daily_frames[day_str] = raw
            days_with_data += 1
            total_rows += len(raw)
        if (i + 1) % 100 == 0:
            elapsed = time.perf_counter() - t0
            print(f"  [{i+1}/{len(all_dates)}] {days_with_data} trading days, {total_rows} rows, {elapsed:.0f}s")

    api_elapsed = time.perf_counter() - t0
    print(f"\nAPI done: {days_with_data} trading days, {total_rows} rows, {api_elapsed:.0f}s")

    if not daily_frames:
        print("No data returned")
        return

    # Step 2: Merge all days into per-symbol DataFrames
    # Group by symbol across all daily frames
    print("Grouping by symbol...")
    symbol_data: dict[str, list[pd.DataFrame]] = {}
    for day_str, frame in daily_frames.items():
        for sym, grp in frame.groupby("symbol"):
            if sym in cached:
                if sym not in symbol_data:
                    symbol_data[sym] = []
                symbol_data[sym].append(grp)

    # Step 3: Multi-threaded merge into parquet
    pending = list(symbol_data.keys())
    print(f"Merging into {len(pending)} symbols with {WORKERS} workers...")

    ok = fail = 0
    rows_written = 0
    t1 = time.perf_counter()

    def merge_one(symbol: str) -> dict:
        try:
            new_data = clean_daily_bars(pd.concat(symbol_data[symbol], ignore_index=True))
            new_data["source"] = "tushare"
            cached_data = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
            merged = clean_daily_bars(pd.concat([cached_data, new_data], ignore_index=True))
            if "trade_date" in merged.columns:
                merged = merged.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
            if "adj_factor" in merged.columns:
                merged = merged.drop(columns=["adj_factor"])
            store.write_symbol_frame("daily_bars", symbol, merged)
            return {"symbol": symbol, "status": "ok", "rows": len(new_data)}
        except Exception as exc:
            return {"symbol": symbol, "status": "error", "error": str(exc)[:100]}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(merge_one, sym): sym for sym in pending}
        for future in as_completed(futures):
            r = future.result()
            if r["status"] == "ok":
                ok += 1
                rows_written += r["rows"]
            else:
                fail += 1
            done = ok + fail
            if done % 500 == 0:
                elapsed = time.perf_counter() - t1
                rate = done / (elapsed / 60) if elapsed > 0 else 0
                print(f"  [{done}/{len(pending)}] ok={ok} err={fail} rows={rows_written} elapsed={elapsed:.0f}s rate={rate:.0f}/min")

    merge_elapsed = time.perf_counter() - t1
    print(f"\n=== Done ===")
    print(f"API: {api_elapsed:.0f}s, Merge: {merge_elapsed:.0f}s")
    print(f"Updated: {ok}, Errors: {fail}, Rows: {rows_written}")


if __name__ == "__main__":
    main()
