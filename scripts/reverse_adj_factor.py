"""Batch reverse forward-adjusted prices in all daily bar parquet files.

Forward adjustment formula (Tushare):
  adjusted_price = raw_price * (adj_factor / latest_adj_factor)

Reversal:
  raw_price = adjusted_price / (adj_factor / latest_adj_factor)
            = adjusted_price * (latest_adj_factor / adj_factor)

After reversal, the adj_factor column is dropped so future incremental
updates never re-apply forward adjustment when adjust_type is "none".
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore


def reverse_forward_adjustment(frame: pd.DataFrame) -> pd.DataFrame:
    """Reverse forward adjustment in-place using the stored adj_factor column."""
    if frame.empty or "adj_factor" not in frame.columns:
        return frame

    working = frame.copy()
    working["adj_factor"] = pd.to_numeric(working["adj_factor"], errors="coerce")

    # Sort by trade_date then compute latest adj_factor per symbol
    if "trade_date" in working.columns:
        working["_sort_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        working = working.sort_values(["symbol", "_sort_date"]).reset_index(drop=True)

    # Latest adj_factor per symbol
    latest_af = working.groupby("symbol")["adj_factor"].transform("last")
    valid = working["adj_factor"].notna() & latest_af.notna() & (latest_af != 0)

    # Reverse ratio: latest_adj_factor / adj_factor
    ratio = pd.Series(1.0, index=working.index, dtype=float)
    ratio.loc[valid] = latest_af.loc[valid] / working.loc[valid, "adj_factor"]

    print(f"  Reversing: adj_factor range [{working['adj_factor'].min():.4f}, {working['adj_factor'].max():.4f}], "
          f"ratio range [{ratio.min():.4f}, {ratio.max():.4f}], "
          f"valid rows: {valid.sum()}/{len(working)}")

    for col in ("open", "high", "low", "close"):
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce") * ratio

    # Drop adj_factor and temp columns
    working = working.drop(columns=["adj_factor", "_sort_date"], errors="ignore")
    return working


def main() -> None:
    config = load_config("config/strategy.yaml")
    parquet_store = ParquetStore(config.parquet_root)

    symbols = parquet_store.list_cached_symbols("daily_bars")
    print(f"Found {len(symbols)} cached symbols")

    reversed_count = 0
    skipped_count = 0
    failed_symbols: list[str] = []
    total_start = time.perf_counter()

    for idx, symbol in enumerate(symbols, start=1):
        try:
            frame = clean_daily_bars(parquet_store.read_symbol_frame("daily_bars", symbol))
            if frame.empty:
                skipped_count += 1
                continue

            if "adj_factor" not in frame.columns:
                skipped_count += 1
                continue

            print(f"[{idx}/{len(symbols)}] {symbol}: {len(frame)} rows, reversing...")
            reversed_frame = reverse_forward_adjustment(frame)
            parquet_store.write_symbol_frame("daily_bars", symbol, reversed_frame)
            reversed_count += 1

            if idx % 500 == 0:
                elapsed = time.perf_counter() - total_start
                print(f"\n--- Progress: {idx}/{len(symbols)} reversed={reversed_count} skipped={skipped_count} "
                      f"elapsed={elapsed:.1f}s ---\n")
        except Exception as exc:
            print(f"[{idx}/{len(symbols)}] {symbol}: FAILED — {exc}")
            failed_symbols.append(symbol)

        gc.collect()

    elapsed = time.perf_counter() - total_start
    print(f"\n=== Done ===")
    print(f"Total: {len(symbols)} symbols")
    print(f"Reversed: {reversed_count}")
    print(f"Skipped (no adj_factor or empty): {skipped_count}")
    print(f"Failed: {len(failed_symbols)}")
    if failed_symbols:
        print(f"Failed symbols: {failed_symbols}")
    print(f"Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
