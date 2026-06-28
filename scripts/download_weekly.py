"""Download full weekly bar history for all A-shares from 2018.

Uses pro.weekly(trade_date=Friday) bulk query — one call per week for all stocks.
"""
import sys, gc, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore
from winstan.adapters.tushare_client import build_tushare_pro

WORKERS = 8


def main() -> None:
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    _, pro = build_tushare_pro(config.data.tushare_token)

    # Generate all Fridays from 2018-01-05 to today
    start = date(2021, 1, 1)  # 最近5年
    end = date.today()
    fridays = []
    current = start
    while current <= end:
        if current.weekday() == 4:  # Friday
            fridays.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)

    print(f"Fetching {len(fridays)} weeks from {fridays[0]} to {fridays[-1]} ...")

    t0 = time.perf_counter()
    all_frames: list[pd.DataFrame] = []
    fetched = 0

    for i, day_str in enumerate(fridays):
        try:
            raw = pro.weekly(
                trade_date=day_str,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
        except Exception as e:
            print(f"  {day_str}: skip ({e})")
            continue
        if raw is not None and not raw.empty:
            raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
            raw["source"] = "tushare"
            raw["adj_factor"] = 1.0
            keep = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "source"]
            raw = raw[[c for c in keep if c in raw.columns]]
            all_frames.append(raw)
            fetched += 1
        if (i + 1) % 50 == 0:
            elapsed = time.perf_counter() - t0
            print(f"  [{i+1}/{len(fridays)}] {fetched} weeks fetched, {elapsed:.0f}s")

    api_elapsed = time.perf_counter() - t0
    if not all_frames:
        print("No data returned")
        return

    full = pd.concat(all_frames, ignore_index=True)
    print(f"\nAPI: {fetched} weeks, {len(full)} rows, {full['symbol'].nunique()} symbols, {api_elapsed:.0f}s")

    # Group by symbol and write
    print(f"Writing to parquet with {WORKERS} workers...")
    grouped = {sym: grp.copy() for sym, grp in full.groupby("symbol")}

    ok = fail = 0
    t1 = time.perf_counter()

    def write_one(symbol: str) -> dict:
        try:
            frame = grouped[symbol].sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
            store.write_symbol_frame("weekly_bars", symbol, frame)
            return {"symbol": symbol, "status": "ok"}
        except Exception as exc:
            return {"symbol": symbol, "status": "error", "error": str(exc)[:100]}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(write_one, sym): sym for sym in grouped}
        for future in as_completed(futures):
            r = future.result()
            if r["status"] == "ok": ok += 1
            else: fail += 1
            if (ok + fail) % 500 == 0:
                elapsed = time.perf_counter() - t1
                rate = (ok + fail) / (elapsed / 60) if elapsed > 0 else 0
                print(f"  [{ok+fail}/{len(grouped)}] ok={ok} err={fail} elapsed={elapsed:.0f}s rate={rate:.0f}/min")

    merge_elapsed = time.perf_counter() - t1
    print(f"\n=== Done ===")
    print(f"API: {api_elapsed:.0f}s, Write: {merge_elapsed:.0f}s")
    print(f"Written: {ok}, Errors: {fail}")
    print(f"Data: {len(full)} weekly bars, {len(grouped)} symbols")


if __name__ == "__main__":
    main()
