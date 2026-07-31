"""Bulk forward adjustment: fetch ALL adj_factors in 1 API call, then apply locally.

Uses Tushare's bulk adj_factor API (no ts_code param) to get all stocks'
adj_factor histories in ONE call. Then processes each stock's parquet locally.
"""
from __future__ import annotations

import gc
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.adapters.tushare_client import build_tushare_pro
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore

WORKERS = 8


def apply_forward_adjustment(frame: pd.DataFrame, adj_series: pd.DataFrame) -> pd.DataFrame:
    """Apply Tushare-style forward adjustment to daily bar data.

    Formula: adjusted_price = raw_price * (adj_factor_on_date / latest_adj_factor)
    """
    if frame.empty or adj_series.empty:
        return frame

    working = frame.copy()

    # Build adj_factor lookup: trade_date -> adj_factor
    af_map = dict(zip(adj_series["trade_date"].astype(str), adj_series["adj_factor"]))
    working["_adj"] = working["trade_date"].astype(str).map(af_map)
    working["_adj"] = pd.to_numeric(working["_adj"], errors="coerce")

    latest_af = float(adj_series["adj_factor"].iloc[-1])
    if latest_af == 0 or pd.isna(latest_af):
        working = working.drop(columns=["_adj"], errors="ignore")
        return working

    # Fill missing adj_factors with latest (for dates after last adj_factor update)
    working["_adj"] = working["_adj"].fillna(latest_af)
    working["_adj"] = working["_adj"].replace(0, latest_af)

    ratio = working["_adj"] / latest_af

    for col in ("open", "high", "low", "close"):
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce") * ratio

    # Drop temp columns
    working = working.drop(columns=["_adj"], errors="ignore")
    # Drop stored adj_factor to avoid future confusion — we apply adjustment directly
    working = working.drop(columns=["adj_factor"], errors="ignore")
    return working


def process_one(symbol: str, store: ParquetStore, bulk_adj: dict[str, pd.DataFrame]) -> dict:
    """Process one stock: read parquet, apply adjustment, write back."""
    try:
        frame = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
        if frame.empty:
            return {"symbol": symbol, "status": "empty"}

        adj_series = bulk_adj.get(symbol)
        if adj_series is None or adj_series.empty:
            return {"symbol": symbol, "status": "no_adj"}

        adjusted = apply_forward_adjustment(frame, adj_series)
        cleaned = clean_daily_bars(adjusted)
        store.write_symbol_frame("daily_bars", symbol, cleaned)
        return {"symbol": symbol, "status": "adjusted"}
    except Exception as exc:
        return {"symbol": symbol, "status": "error", "error": str(exc)[:80]}


def main() -> None:
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    token = config.data.tushare_token
    if not token:
        print("ERROR: No Tushare token")
        return
    _, pro = build_tushare_pro(token)

    # Step 1: Bulk fetch ALL adj_factors for ALL stocks (ONE API call)
    print("Step 1: Bulk fetching all adj_factors (one API call)...")
    t0 = time.perf_counter()
    try:
        bulk = pro.adj_factor(fields="ts_code,trade_date,adj_factor")
    except Exception as e:
        print(f"ERROR: Bulk adj_factor failed: {e}")
        print("The API may be rate-limited (1 call/hour). Please wait and retry.")
        return

    if bulk is None or bulk.empty:
        print("ERROR: Bulk adj_factor returned empty")
        return

    bulk["trade_date"] = bulk["trade_date"].astype(str)
    print(f"  Fetched {len(bulk)} rows, {bulk.ts_code.nunique()} symbols, "
          f"date range {bulk.trade_date.min()} ~ {bulk.trade_date.max()}, "
          f"elapsed {time.perf_counter() - t0:.1f}s")

    # Group by symbol
    print("Step 2: Grouping adj_factor data by symbol...")
    t1 = time.perf_counter()
    bulk_adj: dict[str, pd.DataFrame] = {}
    for sym, grp in bulk.groupby("ts_code"):
        bulk_adj[sym] = grp.sort_values("trade_date").reset_index(drop=True)
    print(f"  {len(bulk_adj)} symbols with adj_factor data, elapsed {time.perf_counter() - t1:.1f}s")

    # Step 3: Process each cached stock
    symbols = store.list_cached_symbols("daily_bars")
    print(f"Step 3: Processing {len(symbols)} cached stocks with {WORKERS} workers...")

    adjusted = empty = no_adj = errors = 0
    t2 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(process_one, sym, store, bulk_adj): sym for sym in symbols}
        for future in as_completed(futures):
            r = future.result()
            status = r["status"]
            if status == "adjusted":
                adjusted += 1
            elif status == "empty":
                empty += 1
            elif status == "no_adj":
                no_adj += 1
            else:
                errors += 1
                if errors <= 10:
                    print(f"  ERROR {r['symbol']}: {r.get('error', '?')}")

            done = adjusted + empty + no_adj + errors
            if done % 1000 == 0:
                elapsed = time.perf_counter() - t2
                rate = done / (elapsed / 60) if elapsed > 0 else 0
                remaining = len(symbols) - done
                eta = remaining / rate if rate > 0 else 0
                print(f"  [{done}/{len(symbols)}] adj={adjusted} empty={empty} no_adj={no_adj} err={errors} "
                      f"elapsed={elapsed:.0f}s rate={rate:.0f}/min ETA={eta:.0f}min")
                gc.collect()

    elapsed = time.perf_counter() - t2
    total = time.perf_counter() - t0
    print(f"\nStep 3 done: adj={adjusted} empty={empty} no_adj={no_adj} errors={errors} "
          f"elapsed={elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"Total time: {total:.0f}s ({total/60:.1f}min)")


if __name__ == "__main__":
    main()
