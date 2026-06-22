"""Smart forward adjustment: bulk fetch latest adj_factor, then per-stock
only for stocks that actually had corporate actions (adj_factor != 1.0)."""
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

from winstan.adapters.tushare_client import build_tushare_pro
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore

WORKERS = 8


def apply_forward_adjustment(frame: pd.DataFrame, adj_frame: pd.DataFrame) -> pd.DataFrame:
    working = frame.copy()
    af_map = dict(zip(adj_frame["trade_date"].astype(str), adj_frame["adj_factor"]))
    working["adj_factor"] = working["trade_date"].astype(str).map(af_map)
    working["adj_factor"] = pd.to_numeric(working["adj_factor"], errors="coerce")
    latest_af = float(adj_frame["adj_factor"].iloc[-1])
    if latest_af == 1.0:
        return frame
    working["adj_factor"] = working["adj_factor"].fillna(latest_af)
    valid = working["adj_factor"].notna() & (working["adj_factor"] != 0)
    ratio = pd.Series(1.0, index=working.index, dtype=float)
    ratio.loc[valid] = working.loc[valid, "adj_factor"] / latest_af
    for col in ("open", "high", "low", "close"):
        if col in working.columns:
            working[col] = pd.to_numeric(working[col], errors="coerce") * ratio
    return working


def process_one(symbol: str, store: ParquetStore, pro) -> dict:
    try:
        frame = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
        if frame.empty:
            return {"symbol": symbol, "status": "skipped"}
        frame = frame.sort_values("trade_date").reset_index(drop=True)
        af_frame = pro.adj_factor(ts_code=symbol, fields="ts_code,trade_date,adj_factor")
        if af_frame is None or af_frame.empty:
            return {"symbol": symbol, "status": "skipped"}
        adjusted = apply_forward_adjustment(frame, af_frame)
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
        print("ERROR: No token"); return
    _, pro = build_tushare_pro(token)

    # Step 1: bulk fetch latest adj_factor for all stocks
    print("Step 1: bulk fetching latest adj_factor...")
    t0 = time.perf_counter()
    bulk = pro.adj_factor(fields="ts_code,trade_date,adj_factor")
    if bulk is None or bulk.empty:
        print("ERROR: bulk adj_factor failed"); return
    # Keep only the latest adj_factor per stock
    bulk["trade_date"] = pd.to_datetime(bulk["trade_date"], format="%Y%m%d")
    latest = bulk.sort_values("trade_date").groupby("ts_code").tail(1)
    needs_adj = latest[latest["adj_factor"] != 1.0]
    needs_symbols = set(needs_adj["ts_code"].tolist())
    print(f"  {len(needs_symbols)} stocks need adjustment (adj_factor != 1.0) out of {len(latest)} total ({time.perf_counter()-t0:.0f}s)")

    if not needs_symbols:
        print("No stocks need adjustment. Done.")
        return

    # Step 2: multi-threaded per-stock adj_factor fetch + apply
    all_symbols = store.list_cached_symbols("daily_bars")
    pending = [s for s in all_symbols if s in needs_symbols]
    print(f"Step 2: processing {len(pending)} stocks with {WORKERS} workers...")

    adjusted = skipped = errors = 0
    t1 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(process_one, sym, store, pro): sym for sym in pending}
        for future in as_completed(futures):
            r = future.result()
            s = r["status"]
            if s == "adjusted": adjusted += 1
            elif s == "skipped": skipped += 1
            else:
                errors += 1
                print(f"  ERROR {r['symbol']}: {r.get('error','?')}")
            done = adjusted + skipped + errors
            if done % 200 == 0:
                elapsed = time.perf_counter() - t1
                rate = done / (elapsed / 60) if elapsed > 0 else 0
                print(f"  [{done}/{len(pending)}] adj={adjusted} skip={skipped} err={errors} elapsed={elapsed:.0f}s rate={rate:.0f}/min")

    elapsed = time.perf_counter() - t1
    print(f"\nDone: adj={adjusted} skip={skipped} err={errors} elapsed={elapsed:.0f}s ({elapsed/60:.1f}min)")


if __name__ == "__main__":
    main()
