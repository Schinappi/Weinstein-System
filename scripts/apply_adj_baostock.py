"""Batch forward-adjust daily bars using baostock adj_factor data.

Uses baostock per-stock adj_factor queries + multi-threaded processing.
baostock is slower than Tushare but has no hourly rate limit on adj_factor.
"""
from __future__ import annotations

import gc
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import baostock as bs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore

WORKERS = 8


def _symbol_to_baostock_code(symbol: str) -> str:
    """Convert Tushare symbol format to baostock format.

    000001.SZ -> sz.000001
    600000.SH -> sh.600000
    """
    parts = symbol.split(".")
    if len(parts) == 2:
        code, exchange = parts
        return f"{exchange.lower()}.{code}"
    return f"sz.{symbol}"


def get_adj_factor_map(bs_code: str) -> dict[str, float]:
    """Fetch adj_factor data from baostock and return a mapping of YYYY-MM-DD -> foreAdjustFactor.

    baostock returns one row per corporate action. foreAdjustFactor at the latest date = 1.0.
    For forward adjustment: adjusted_price = raw_price * foreAdjustFactor
    """
    try:
        rs = bs.query_adjust_factor(code=bs_code, start_date="2018-01-01", end_date="2026-12-31")
        if rs.error_code != "0":
            return {}
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return {}

        df = pd.DataFrame(rows, columns=rs.fields)
        df["dividOperateDate"] = pd.to_datetime(df["dividOperateDate"], errors="coerce")
        df["foreAdjustFactor"] = pd.to_numeric(df["foreAdjustFactor"], errors="coerce")
        df = df.sort_values("dividOperateDate")

        # Build a date -> factor mapping with forward fill
        # We need a factor for every trading day
        factor_map: dict[str, float] = {}
        for _, row in df.iterrows():
            date_str = row["dividOperateDate"].strftime("%Y-%m-%d")
            factor_map[date_str] = float(row["foreAdjustFactor"])
        return factor_map
    except Exception:
        return {}


def apply_forward_adjustment(frame: pd.DataFrame, factor_map: dict[str, float]) -> pd.DataFrame:
    """Apply forward adjustment using baostock foreAdjustFactor.

    Formula: adjusted_price = raw_price * foreAdjustFactor

    foreAdjustFactor is 1.0 at the latest date and decreases going backwards,
    reflecting the cumulative effect of corporate actions.
    """
    if frame.empty or not factor_map:
        return frame

    working = frame.copy()
    working["_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working = working.sort_values("_date").reset_index(drop=True)

    # Assign foreAdjustFactor to each row based on the most recent corporate action
    factor_dates = sorted(factor_map.keys())
    if not factor_dates:
        return frame

    # For each row, find the factor from the most recent corporate action date
    # that is <= the row's trade date
    factor_values = np.ones(len(working), dtype=float)

    for i, row_date in enumerate(working["_date"]):
        row_date_str = row_date.strftime("%Y-%m-%d") if pd.notna(row_date) else None
        if row_date_str is None:
            continue
        # Find the most recent factor date <= row_date
        assigned_factor = 1.0
        for fd in factor_dates:
            if fd <= row_date_str:
                assigned_factor = factor_map[fd]
            else:
                break
        factor_values[i] = assigned_factor

    # Apply adjustment
    for col in ("open", "high", "low", "close"):
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce") * factor_values

    # Drop temp columns
    working = working.drop(columns=["_date"], errors="ignore")
    return working


def process_one(symbol: str, store: ParquetStore) -> dict:
    """Process one stock: read raw data, get adj_factor from baostock, apply adjustment."""
    try:
        frame = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
        if frame.empty:
            return {"symbol": symbol, "status": "empty"}

        bs_code = _symbol_to_baostock_code(symbol)
        factor_map = get_adj_factor_map(bs_code)

        if not factor_map:
            return {"symbol": symbol, "status": "no_factor"}

        adjusted = apply_forward_adjustment(frame, factor_map)
        cleaned = clean_daily_bars(adjusted)
        store.write_symbol_frame("daily_bars", symbol, cleaned)
        return {"symbol": symbol, "status": "adjusted"}
    except Exception as exc:
        return {"symbol": symbol, "status": "error", "error": str(exc)[:80]}


def main() -> None:
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    symbols = store.list_cached_symbols("daily_bars")
    print(f"Total symbols: {len(symbols)}")

    # Login to baostock
    bs.login()

    adjusted = empty = no_factor = errors = 0
    t0 = time.perf_counter()

    try:
        # Process in batches to keep memory under control
        batch_size = 500
        for batch_start in range(0, len(symbols), batch_size):
            batch = symbols[batch_start:batch_start + batch_size]

            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                futures = {ex.submit(process_one, sym, store): sym for sym in batch}
                for future in as_completed(futures):
                    r = future.result()
                    status = r["status"]
                    if status == "adjusted":
                        adjusted += 1
                    elif status == "empty":
                        empty += 1
                    elif status == "no_factor":
                        no_factor += 1
                    else:
                        errors += 1
                        if errors <= 10:
                            print(f"  ERROR {r['symbol']}: {r.get('error', '?')}")

            done = adjusted + empty + no_factor + errors
            elapsed = time.perf_counter() - t0
            rate = done / (elapsed / 60) if elapsed > 0 else 0
            remaining = len(symbols) - done
            eta = remaining / rate if rate > 0 else 0
            print(f"  [{done}/{len(symbols)}] adj={adjusted} empty={empty} no_factor={no_factor} err={errors} "
                  f"elapsed={elapsed:.0f}s rate={rate:.0f}/min ETA={eta:.0f}min")

            gc.collect()
    finally:
        bs.logout()

    elapsed = time.perf_counter() - t0
    print(f"\nDone: adj={adjusted} empty={empty} no_factor={no_factor} errors={errors} elapsed={elapsed:.0f}s ({elapsed/60:.1f}min)")


if __name__ == "__main__":
    main()
