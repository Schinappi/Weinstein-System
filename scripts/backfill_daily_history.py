"""Backfill cached daily bars to the configured start date."""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local

import pandas as pd

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.adapters.tushare_client import build_tushare_pro
from winstan.storage.parquet_store import ParquetStore


WORKERS = 1
thread_state = local()


def _client(token: str | None):
    if not hasattr(thread_state, "pro"):
        thread_state.pro = build_tushare_pro(token)[1]
    return thread_state.pro


def _fetch(day: str, token: str | None) -> pd.DataFrame:
    frame = _client(token).daily(
        trade_date=day,
        fields="ts_code,trade_date,open,high,low,close,vol,amount",
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    return frame.rename(columns={"ts_code": "symbol", "vol": "volume"})


def main() -> None:
    config = load_config(project_root / "config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    token = config.data.tushare_token
    symbols = set(store.list_cached_symbols("daily_bars"))
    start = config.data.effective_start_date.replace("-", "")
    end = "20210103"
    try:
        calendar = build_tushare_pro(token)[1].trade_cal(
            exchange="SSE", start_date=start, end_date=end, is_open="1", fields="cal_date"
        )
        days = sorted(calendar["cal_date"].dropna().astype(str).unique()) if calendar is not None else []
    except Exception:
        days = [value.strftime("%Y%m%d") for value in pd.bdate_range(start=start, end=end)]
    print(f"Backfilling {len(symbols)} symbols across {len(days)} trading days", flush=True)
    started = time.perf_counter()
    frames: list[pd.DataFrame] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_fetch, day, token): day for day in days}
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                frame = future.result()
            except Exception as exc:
                failures += 1
                print(f"fetch failed day={futures[future]} reason={exc}", flush=True)
                continue
            if not frame.empty:
                frames.append(frame)
            if index % 50 == 0 or index == len(days):
                print(f"fetched {index}/{len(days)} days rows={sum(len(x) for x in frames)}", flush=True)
    if not frames:
        raise RuntimeError("no historical rows returned")
    history = clean_daily_bars(pd.concat(frames, ignore_index=True))
    history = history[history["symbol"].isin(symbols)]
    history["source"] = "tushare"
    grouped = {symbol: frame for symbol, frame in history.groupby("symbol", sort=False)}
    updated = 0
    rows_added = 0
    for index, symbol in enumerate(sorted(symbols), start=1):
        new_rows = grouped.get(symbol)
        if new_rows is None or new_rows.empty:
            continue
        cached = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
        merged = clean_daily_bars(pd.concat([cached, new_rows], ignore_index=True))
        before = set(pd.to_datetime(cached["trade_date"], errors="coerce").dropna())
        merged = merged.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
        store.write_symbol_frame("daily_bars", symbol, merged)
        updated += 1
        rows_added += sum(value not in before for value in pd.to_datetime(new_rows["trade_date"], errors="coerce"))
        if index % 500 == 0:
            print(f"merged {index}/{len(symbols)} symbols", flush=True)
    print(f"done updated={updated} rows_added={rows_added} fetch_failures={failures} elapsed={time.perf_counter()-started:.1f}s", flush=True)


if __name__ == "__main__":
    main()
