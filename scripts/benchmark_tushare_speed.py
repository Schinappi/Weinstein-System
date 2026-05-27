from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.adapters.tushare_adapter import TushareAdapter
from winstan.config import load_config


DEFAULT_SYMBOLS = [
    "000001.SZ",
    "000002.SZ",
    "000333.SZ",
    "000651.SZ",
    "002415.SZ",
    "300750.SZ",
    "300760.SZ",
    "600036.SH",
    "600276.SH",
    "600519.SH",
    "600887.SH",
    "601012.SH",
    "601318.SH",
    "601398.SH",
    "601857.SH",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Tushare daily bar fetch speed.")
    parser.add_argument("--config", default="config/strategy.yaml")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to test.")
    parser.add_argument("--count", type=int, default=10, help="Number of default symbols to test when --symbols is omitted.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--mode", choices=["batch", "single"], default="batch")
    return parser.parse_args()



def _select_symbols(raw_symbols: str, count: int) -> list[str]:
    if raw_symbols.strip():
        return [item.strip() for item in raw_symbols.split(",") if item.strip()]
    return DEFAULT_SYMBOLS[: max(1, min(count, len(DEFAULT_SYMBOLS)))]



def _run_single_mode(
    adapter: TushareAdapter,
    symbols: list[str],
    start_date: str,
    end_date: str,
    adjust_type: str,
) -> tuple[list[dict[str, object]], float]:
    results: list[dict[str, object]] = []
    started = time.perf_counter()

    for index, symbol in enumerate(symbols, start=1):
        symbol_started = time.perf_counter()
        row_count = 0
        status = "ok"
        error = ""
        try:
            frame = adapter.fetch_daily_bars([symbol], start_date=start_date, end_date=end_date, adjust_type=adjust_type)
            row_count = 0 if frame is None else len(frame)
            if row_count <= 0:
                status = "empty"
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            status = "error"
            error = str(exc)
        elapsed = round(time.perf_counter() - symbol_started, 2)
        print(f"[{index}/{len(symbols)}] {symbol} status={status} rows={row_count} elapsed={elapsed}s")
        results.append({
            "symbol": symbol,
            "status": status,
            "rows": row_count,
            "elapsed_seconds": elapsed,
            "error": error,
        })

    return results, time.perf_counter() - started



def _run_batch_mode(
    adapter: TushareAdapter,
    symbols: list[str],
    start_date: str,
    end_date: str,
    adjust_type: str,
) -> tuple[list[dict[str, object]], float]:
    started = time.perf_counter()
    frame = adapter.fetch_daily_bars(symbols, start_date=start_date, end_date=end_date, adjust_type=adjust_type)
    total_elapsed = time.perf_counter() - started

    if frame is None or frame.empty:
        results = [
            {
                "symbol": symbol,
                "status": "empty",
                "rows": 0,
                "elapsed_seconds": round(total_elapsed, 2),
                "error": "",
            }
            for symbol in symbols
        ]
        print(f"[batch] status=empty symbols={len(symbols)} rows=0 elapsed={round(total_elapsed, 2)}s")
        return results, total_elapsed

    counts = frame.groupby("symbol", sort=False).size().to_dict()
    print(
        f"[batch] status=ok symbols={len(symbols)} fetched_symbols={len(counts)} rows={len(frame)} elapsed={round(total_elapsed, 2)}s"
    )
    results = []
    for symbol in symbols:
        row_count = int(counts.get(symbol, 0))
        status = "ok" if row_count > 0 else "empty"
        print(f"[symbol] {symbol} status={status} rows={row_count}")
        results.append({
            "symbol": symbol,
            "status": status,
            "rows": row_count,
            "elapsed_seconds": round(total_elapsed, 2),
            "error": "",
        })
    return results, total_elapsed



def main() -> int:
    args = _parse_args()
    config = load_config(args.config)
    token = os.getenv(config.data.tushare_token_env)
    adapter = TushareAdapter(token=token, data_config=config.data)

    end_date = args.end_date or date.today().isoformat()
    start_date = args.start_date or (date.today() - timedelta(days=14)).isoformat()
    symbols = _select_symbols(args.symbols, args.count)

    results: list[dict[str, object]] = []
    started = time.perf_counter()

    print(
        f"benchmark start symbols={len(symbols)} start_date={start_date} end_date={end_date} "
        f"configured_calls_per_minute={config.data.tushare_calls_per_minute} mode={args.mode}"
    )

    if args.mode == "single":
        results, total_elapsed = _run_single_mode(
            adapter,
            symbols,
            start_date,
            end_date,
            config.data.adjust_type,
        )
    else:
        results, total_elapsed = _run_batch_mode(
            adapter,
            symbols,
            start_date,
            end_date,
            config.data.adjust_type,
        )

    success_count = sum(1 for item in results if item["status"] == "ok")
    empty_count = sum(1 for item in results if item["status"] == "empty")
    error_count = sum(1 for item in results if item["status"] == "error")
    effective_requests_per_minute = round((len(symbols) / total_elapsed) * 60, 2) if total_elapsed > 0 else None
    average_seconds_per_symbol = round(total_elapsed / len(symbols), 2) if symbols else None

    print(
        json.dumps(
            {
                "symbols_tested": len(symbols),
                "success_count": success_count,
                "empty_count": empty_count,
                "error_count": error_count,
                "total_elapsed_seconds": round(total_elapsed, 2),
                "average_seconds_per_symbol": average_seconds_per_symbol,
                "effective_requests_per_minute": effective_requests_per_minute,
                "configured_calls_per_minute": config.data.tushare_calls_per_minute,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
