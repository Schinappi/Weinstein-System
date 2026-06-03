"""Re-apply forward adjustment to all daily bar parquet files.

For each stock, fetches adj_factor from Tushare and applies:
  adj_price = raw_price * (adj_factor / latest_adj_factor)
"""
from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.adapters.tushare_client import build_tushare_pro
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore


def apply_forward_adjustment(frame: pd.DataFrame, adj_map: dict[str, float], latest_af: float) -> pd.DataFrame:
    """Apply forward adjustment using pre-fetched adj_factor values."""
    working = frame.copy()
    working["adj_factor"] = working["trade_date"].astype(str).map(adj_map)
    working["adj_factor"] = pd.to_numeric(working["adj_factor"], errors="coerce").fillna(latest_af)

    valid = working["adj_factor"].notna() & (working["adj_factor"] != 0)
    ratio = pd.Series(1.0, index=working.index, dtype=float)
    ratio.loc[valid] = working.loc[valid, "adj_factor"] / latest_af

    for col in ("open", "high", "low", "close"):
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce") * ratio

    return working


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-idx", type=int, default=1, help="Starting index (1-based)")
    parser.add_argument("--end-idx", type=int, default=None, help="Ending index (inclusive)")
    args = parser.parse_args()

    config = load_config("config/strategy.yaml")
    parquet_store = ParquetStore(config.parquet_root)
    token = config.data.tushare_token
    if not token:
        print("ERROR: No Tushare token configured")
        return

    _, pro = build_tushare_pro(token)

    symbols = parquet_store.list_cached_symbols("daily_bars")
    total = len(symbols)
    print(f"Found {total} cached symbols")

    start = max(1, args.start_idx) - 1  # convert to 0-based
    end = min(args.end_idx or total, total)
    symbols = symbols[start:end]
    print(f"Processing symbols [{args.start_idx}..{args.end_idx or total}] ({len(symbols)} stocks)")

    adjusted = 0
    skipped = 0
    failed: list[str] = []
    total_start = time.perf_counter()
    adj_api_time = 0.0

    for idx, symbol in enumerate(symbols, start=args.start_idx):
        try:
            frame = clean_daily_bars(parquet_store.read_symbol_frame("daily_bars", symbol))
            if frame.empty:
                skipped += 1
                continue

            frame = frame.sort_values("trade_date").reset_index(drop=True)

            # Fetch adj_factor from Tushare
            af_start = time.perf_counter()
            af_frame, af_err = None, None
            try:
                resp = pro.adj_factor(ts_code=symbol, fields="ts_code,trade_date,adj_factor")
                if resp is not None and not resp.empty:
                    af_frame = resp
            except Exception as e:
                af_err = str(e)
            adj_api_time += time.perf_counter() - af_start

            if af_frame is None or af_frame.empty:
                # No adj_factor means no corporate actions → raw = adjusted
                skipped += 1
                continue

            af_map = dict(zip(af_frame["trade_date"].astype(str), af_frame["adj_factor"]))
            latest_af = float(af_frame["adj_factor"].iloc[-1])

            if latest_af == 1.0:
                # No actual adjustment needed
                skipped += 1
                continue

            # Apply forward adjustment
            adjusted_frame = apply_forward_adjustment(frame, af_map, latest_af)

            # Clean and write
            cleaned = clean_daily_bars(adjusted_frame)
            parquet_store.write_symbol_frame("daily_bars", symbol, cleaned)
            adjusted += 1

            if idx % 500 == 0 or idx == len(symbols):
                elapsed = time.perf_counter() - total_start
                print(f"[{idx}/{len(symbols)}] adjusted={adjusted} skipped={skipped} failed={len(failed)} "
                      f"elapsed={elapsed:.0f}s api={adj_api_time:.0f}s")

        except Exception as exc:
            print(f"[{idx}/{len(symbols)}] {symbol}: FAILED — {exc}")
            failed.append(symbol)

        gc.collect()

    elapsed = time.perf_counter() - total_start
    print(f"\n=== Done ===")
    print(f"Total: {len(symbols)}")
    print(f"Adjusted: {adjusted}")
    print(f"Skipped (no adj_factor or af=1.0): {skipped}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"Failed symbols: {failed}")
    print(f"Elapsed: {elapsed:.0f}s (API: {adj_api_time:.0f}s)")


if __name__ == "__main__":
    main()
