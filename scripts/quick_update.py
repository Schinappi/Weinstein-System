"""Quick incremental daily bar update - no adj_factor waste"""
import sys, gc, time, glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore
from winstan.adapters.tushare_client import build_tushare_pro

WORKERS = 8
BATCH_REPORT = 1000

def main():
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    
    # Find all cached symbols
    symbols = store.list_cached_symbols("daily_bars")
    if not symbols:
        print("No cached symbols. Run fetch_unadjusted_init.py first.")
        return
    
    # Determine latest date in data
    df_first = store.read_symbol_frame("daily_bars", symbols[0])
    if df_first.empty:
        print("Cannot determine latest date")
        return
    latest_date = df_first['trade_date'].max()
    latest_str = latest_date.strftime('%Y%m%d')
    
    # Target end date
    from datetime import date, timedelta
    end_date = date.today().isoformat().replace('-', '')
    
    if latest_str >= end_date:
        print(f"Data already up-to-date: {latest_date.date()}")
        return
    
    start_date = (latest_date + timedelta(days=1)).strftime('%Y%m%d')
    print(f"Incrementally updating from {start_date} to {end_date} for {len(symbols)} symbols...")
    
    _, pro = build_tushare_pro(config.data.tushare_token)
    
    total = len(symbols)
    ok, empty, errors, total_rows, batch_ok = 0, 0, 0, 0, 0
    start_time = time.perf_counter()
    
    def fetch_and_merge(symbol):
        try:
            raw = pro.daily(
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
            if raw is None or raw.empty:
                return {"symbol": symbol, "status": "empty"}
            frame = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
            frame["symbol"] = symbol
            frame["source"] = "tushare"
            cleaned = clean_daily_bars(frame)
            if cleaned.empty:
                return {"symbol": symbol, "status": "empty"}
            # Merge with existing
            cached = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
            merged = clean_daily_bars(pd.concat([cached, cleaned], ignore_index=True))
            # Drop adj_factor if present
            if "adj_factor" in merged.columns:
                merged = merged.drop(columns=["adj_factor"])
            if not merged.equals(cached.reset_index(drop=True)):
                store.write_symbol_frame("daily_bars", symbol, merged)
                return {"symbol": symbol, "status": "ok", "rows": len(cleaned)}
            return {"symbol": symbol, "status": "unchanged"}
        except Exception as exc:
            return {"symbol": symbol, "status": "error", "error": str(exc)}
    
    import pandas as pd
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(fetch_and_merge, sym): sym for sym in symbols}
        for future in as_completed(futures):
            r = future.result()
            s = r["status"]
            if s == "ok":
                ok += 1; batch_ok += 1; total_rows += r["rows"]
            elif s in ("empty", "unchanged"):
                empty += 1
            else:
                errors += 1
                print(f"  ERROR {r['symbol']}: {r.get('error','?')}")
            if batch_ok >= BATCH_REPORT:
                elapsed = time.perf_counter() - start_time
                rate = ok / (elapsed / 60)
                print(f"  Progress: {ok}/{total} ok={ok} empty={empty} errors={errors} rows={total_rows} elapsed={elapsed:.0f}s rate={rate:.0f}/min")
                batch_ok = 0
            if ok % 200 == 0:
                gc.collect()
    
    elapsed = time.perf_counter() - start_time
    rate = ok / (elapsed / 60) if elapsed > 0 else 0
    print(f"\n=== Done ===")
    print(f"Updated: {ok}, Unchanged/Empty: {empty}, Errors: {errors}")
    print(f"Rows added: {total_rows}")
    print(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"Rate: {rate:.0f} stocks/min")

if __name__ == "__main__":
    main()
