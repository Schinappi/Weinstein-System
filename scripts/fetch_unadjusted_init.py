"""Initialize stock list in parquet store and fetch unadjusted data"""
import sys, gc, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.storage.parquet_store import ParquetStore
from winstan.adapters.tushare_client import build_tushare_pro

WORKERS = 8
BATCH_REPORT = 500

def fetch_and_write(symbol, pro, start_date, end_date, store):
    """Fetch unadjusted data for one symbol and write to parquet."""
    try:
        raw = pro.daily(
            ts_code=symbol,
            start_date=start_date,
            end_date=end_date,
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )
        if raw is None or raw.empty:
            return {"symbol": symbol, "status": "empty", "rows": 0}
        frame = raw.rename(columns={"ts_code": "symbol", "vol": "volume"})
        frame["symbol"] = symbol
        frame["source"] = "tushare"
        cleaned = clean_daily_bars(frame)
        if cleaned.empty:
            return {"symbol": symbol, "status": "clean_empty", "rows": 0}
        store.write_symbol_frame("daily_bars", symbol, cleaned)
        return {"symbol": symbol, "status": "ok", "rows": len(cleaned)}
    except Exception as exc:
        return {"symbol": symbol, "status": "error", "error": str(exc), "rows": 0}

def main():
    config = load_config("config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    
    from winstan.adapters.factory import DataSourceRouter
    router = DataSourceRouter(config)
    
    print("Fetching stock universe...")
    raw_universe = router.fetch_stock_universe()
    from winstan.pipeline.universe import build_universe
    universe = build_universe(raw_universe, config)
    symbols = universe["symbol"].dropna().astype(str).tolist() if not universe.empty else []
    # Also restore any previously cached symbols that might exist
    cached = store.list_cached_symbols("daily_bars")
    all_symbols = sorted(set(symbols) | set(cached))
    print(f"Universe: {len(symbols)} stocks, cached: {len(cached)}, total: {len(all_symbols)}")
    
    if not all_symbols:
        print("No symbols to fetch!")
        return
    
    _, pro = build_tushare_pro(config.data.tushare_token)
    start = config.data.effective_start_date.replace("-", "")
    end = config.data.effective_end_date.replace("-", "")
    
    total = len(all_symbols)
    ok, empty, errors, total_rows = 0, 0, 0, 0
    start_time = time.perf_counter()
    batch_ok = 0
    
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(fetch_and_write, sym, pro, start, end, store): sym
            for sym in all_symbols
        }
        for future in as_completed(futures):
            result = future.result()
            status = result["status"]
            if status == "ok":
                ok += 1; batch_ok += 1; total_rows += result["rows"]
            elif status in ("empty", "clean_empty"):
                empty += 1
            else:
                errors += 1
                print(f"  ERROR {result['symbol']}: {result.get('error','?')}")
            if batch_ok >= BATCH_REPORT:
                elapsed = time.perf_counter() - start_time
                rate = ok / (elapsed / 60)
                print(f"  Progress: {ok}/{total} ({ok*100//total}%) ok={ok} empty={empty} errors={errors} rows={total_rows} elapsed={elapsed:.0f}s rate={rate:.0f}/min")
                batch_ok = 0
            if ok % 100 == 0:
                gc.collect()
    
    elapsed = time.perf_counter() - start_time
    rate = ok / (elapsed / 60) if elapsed > 0 else 0
    print(f"\n=== Done ===")
    print(f"Total: {total}")
    print(f"OK: {ok}")
    print(f"Empty: {empty}")
    print(f"Errors: {errors}")
    print(f"Total rows: {total_rows}")
    print(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"Rate: {rate:.0f} stocks/min")

if __name__ == "__main__":
    main()
