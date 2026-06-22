"""Bulk daily bar updater — shared by quick_update.py and preclose_and_phase1.py.

Uses a single ``pro.daily(start_date=..., end_date=...)`` call (no ts_code)
to fetch the latest bars for ALL A-shares, then merges into per-symbol parquet
files in parallel.
"""

from __future__ import annotations

import gc
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.storage.parquet_store import ParquetStore

WORKERS = 8


def bulk_update_recent(
    pro,
    store: ParquetStore,
    days_back: int = 5,
    end_date: str | date | None = None,
) -> dict:
    """Fetch recent daily bars for ALL cached symbols in ONE API call,
    then merge into each symbol's parquet file.

    Parameters
    ----------
    pro:
        Tushare-compatible pro client (must support ``pro.daily(start_date=, end_date=)``
        without ``ts_code``).
    store:
        ParquetStore pointing at the data root.
    days_back:
        Number of calendar days to look back from *end_date* (or today).
    end_date:
        End date string ``YYYYMMDD`` or ``date`` object.  Defaults to today.

    Returns
    -------
    dict
        ``{updated, unchanged, errors, rows_added, api_seconds, merge_seconds}``
    """
    if end_date is None:
        end_dt = date.today()
    elif isinstance(end_date, date):
        end_dt = end_date
    else:
        end_dt = date.fromisoformat(end_date)

    start_dt = date.today() - timedelta(days=days_back)

    # ── Step 1: Per-day queries (avoids API 6000-row cap with range queries) ──
    t0 = time.perf_counter()
    frames: list[pd.DataFrame] = []
    # Iterate calendar days backwards; skip weekends automatically via empty results
    current = end_dt
    while current >= start_dt:
        day_str = current.strftime("%Y%m%d")
        try:
            day_frame = pro.daily(
                trade_date=day_str,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
        except Exception:
            day_frame = None
        if day_frame is not None and not day_frame.empty:
            frames.append(day_frame)
            print(f"[daily_updater] {day_str}: {len(day_frame)} stocks")
        current -= timedelta(days=1)

    api_elapsed = time.perf_counter() - t0

    if not frames:
        print(f"[daily_updater] all days returned empty ({start_dt}→{end_dt})")
        return {
            "updated": 0, "unchanged": 0, "errors": 0,
            "rows_added": 0, "api_seconds": api_elapsed, "merge_seconds": 0,
        }

    raw = pd.concat(frames, ignore_index=True)
    raw = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
    raw["source"] = "tushare"
    n_symbols = raw["symbol"].nunique()
    n_days = raw["trade_date"].nunique()
    print(
        f"[daily_updater] fetched {len(raw)} rows, {n_symbols} symbols, "
        f"{n_days} days in {len(frames)} API calls, {api_elapsed:.1f}s"
    )

    # Group by symbol
    grouped = {sym: grp.copy() for sym, grp in raw.groupby("symbol")}
    cached_symbols = set(store.list_cached_symbols("daily_bars"))
    pending = [sym for sym in grouped if sym in cached_symbols]
    skipped = len(grouped) - len(pending)
    if skipped:
        print(f"[daily_updater] skipping {skipped} symbols not in cache")

    # ── Step 2: Multi-threaded merge ──
    t1 = time.perf_counter()
    ok = empty = errors = 0
    total_rows = 0

    def _merge_one(symbol: str) -> dict:
        try:
            new_data = clean_daily_bars(grouped[symbol])
            if new_data.empty:
                return {"symbol": symbol, "status": "empty"}

            cached = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
            merged = clean_daily_bars(pd.concat([cached, new_data], ignore_index=True))
            if "trade_date" in merged.columns:
                merged = merged.sort_values("trade_date").drop_duplicates(
                    subset=["trade_date"], keep="last"
                )
            if "adj_factor" in merged.columns:
                merged = merged.drop(columns=["adj_factor"])

            if merged.equals(cached.reset_index(drop=True)):
                return {"symbol": symbol, "status": "unchanged"}

            store.write_symbol_frame("daily_bars", symbol, merged)
            return {"symbol": symbol, "status": "ok", "rows": len(new_data)}
        except Exception as exc:
            return {"symbol": symbol, "status": "error", "error": str(exc)}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_merge_one, sym): sym for sym in pending}
        for future in as_completed(futures):
            r = future.result()
            s = r["status"]
            if s == "ok":
                ok += 1
                total_rows += r.get("rows", 0)
            elif s in ("empty", "unchanged"):
                empty += 1
            else:
                errors += 1
            if (ok + empty + errors) % 1000 == 0:
                gc.collect()

    merge_elapsed = time.perf_counter() - t1
    print(
        f"[daily_updater] merged: updated={ok} unchanged={empty} errors={errors} "
        f"rows_added={total_rows} merge={merge_elapsed:.0f}s"
    )
    return {
        "updated": ok,
        "unchanged": empty,
        "errors": errors,
        "rows_added": total_rows,
        "api_seconds": api_elapsed,
        "merge_seconds": merge_elapsed,
    }
