from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import time

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from winstan.adapters.factory import DataSourceRouter
from winstan.adapters.tushare_client import build_tushare_pro
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config, normalize_date_like
from winstan.pipeline.universe import build_universe
from winstan.pipeline.screener import WeinsteinScreener
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore


PROGRESS_LOG_INTERVAL_SECONDS = 300



def _to_iso_date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.to_datetime(value).date().isoformat()



def _next_day(value: str) -> str:
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()



def _is_after(left: str, right: str) -> bool:
    return date.fromisoformat(left) > date.fromisoformat(right)



def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")



def _round_seconds(value: float) -> float:
    return round(value, 2)



def _symbol_sample_text(symbols: list[str], limit: int = 5) -> str:
    if not symbols:
        return ""
    sample = symbols[:limit]
    suffix = "" if len(symbols) <= limit else f" ... +{len(symbols) - limit}"
    return ",".join(sample) + suffix



def _is_open_trade_day(config, value: str | None) -> bool:
    trade_date = normalize_date_like(value, date.today().isoformat()) or date.today().isoformat()
    trade_date_compact = trade_date.replace("-", "")
    try:
        _, pro = build_tushare_pro(config.data.tushare_token)
        calendar = pro.trade_cal(
            exchange="SSE",
            start_date=trade_date_compact,
            end_date=trade_date_compact,
            is_open="1",
            fields="cal_date",
        )
        return isinstance(calendar, pd.DataFrame) and not calendar.empty and "cal_date" in calendar.columns
    except Exception:
        return date.fromisoformat(trade_date).weekday() < 5



def _status_path(config) -> Path:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    return config.logs_dir / "incremental_update_status.json"



def _write_status(config, payload: dict[str, object]) -> None:
    _status_path(config).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")



def _resolve_stock_symbols(router: DataSourceRouter, parquet_store: ParquetStore, config, include_missing: bool) -> list[str]:
    cached_symbols = parquet_store.list_cached_symbols("daily_bars")
    if not include_missing:
        return cached_symbols
    try:
        raw_universe = router.fetch_stock_universe()
        universe = build_universe(raw_universe, config)
        universe_symbols = universe["symbol"].dropna().astype(str).tolist() if not universe.empty else []
    except Exception:
        universe_symbols = []
    return sorted(set(cached_symbols) | set(universe_symbols))



def _planned_start_date(parquet_store: ParquetStore, dataset: str, symbol: str, fallback_start_date: str) -> str:
    cached = clean_daily_bars(parquet_store.read_symbol_frame(dataset, symbol))
    if cached.empty:
        return fallback_start_date
    latest = _to_iso_date(cached["trade_date"].max())
    if not latest:
        return fallback_start_date
    return _next_day(latest)



def _merge_and_write(parquet_store: ParquetStore, dataset: str, symbol: str, new_rows: pd.DataFrame) -> dict[str, float | int]:
    read_started_counter = time.perf_counter()
    cached = clean_daily_bars(parquet_store.read_symbol_frame(dataset, symbol))
    read_runtime_seconds = _round_seconds(time.perf_counter() - read_started_counter)

    merge_started_counter = time.perf_counter()
    merged = clean_daily_bars(pd.concat([cached, new_rows], ignore_index=True))
    added_rows = max(0, len(merged) - len(cached))
    merge_runtime_seconds = _round_seconds(time.perf_counter() - merge_started_counter)

    write_runtime_seconds = 0.0
    if added_rows > 0:
        write_started_counter = time.perf_counter()
        parquet_store.write_symbol_frame(dataset, symbol, merged)
        write_runtime_seconds = _round_seconds(time.perf_counter() - write_started_counter)
    return {
        "added_rows": int(added_rows),
        "cache_read_runtime_seconds": read_runtime_seconds,
        "merge_runtime_seconds": merge_runtime_seconds,
        "write_runtime_seconds": write_runtime_seconds,
    }



def _stock_timing_fields() -> dict[str, float | int]:
    return {
        "stock_fetch_groups": 0,
        "stock_fetch_runtime_seconds": 0.0,
        "stock_clean_runtime_seconds": 0.0,
        "stock_split_runtime_seconds": 0.0,
        "stock_cache_read_runtime_seconds": 0.0,
        "stock_merge_runtime_seconds": 0.0,
        "stock_write_runtime_seconds": 0.0,
    }



def _round_timing_fields(summary: dict[str, object], keys: list[str]) -> None:
    for key in keys:
        summary[key] = _round_seconds(float(summary.get(key, 0.0)))



def _print_stock_progress(summary: dict[str, int], started_counter: float) -> None:
    elapsed_seconds = _round_seconds(time.perf_counter() - started_counter)
    print(
        "[update_daily_bars] stock progress "
        f"elapsed={elapsed_seconds}s "
        f"processed={summary['stock_symbols_processed']}/{summary['stock_symbols_planned']} "
        f"success={summary['stock_symbols_updated']} "
        f"empty={summary['stock_symbols_fetch_empty']} "
        f"failed={summary['stock_symbols_failed']}"
    )



def _maybe_print_stock_progress(summary: dict[str, int], started_counter: float, next_progress_counter: float) -> float:
    current_counter = time.perf_counter()
    if current_counter >= next_progress_counter:
        _print_stock_progress(summary, started_counter)
        return current_counter + PROGRESS_LOG_INTERVAL_SECONDS
    return next_progress_counter



def _update_stock_daily_bars(
    router: DataSourceRouter,
    parquet_store: ParquetStore,
    symbols: list[str],
    start_date: str,
    end_date: str,
    dry_run: bool,
) -> dict[str, int]:
    grouped_symbols: dict[str, list[str]] = defaultdict(list)
    up_to_date = 0
    for symbol in symbols:
        symbol_start_date = _planned_start_date(parquet_store, "daily_bars", symbol, start_date)
        if _is_after(symbol_start_date, end_date):
            up_to_date += 1
            continue
        grouped_symbols[symbol_start_date].append(symbol)

    summary = {
        "stock_symbols_scanned": len(symbols),
        "stock_symbols_planned": sum(len(batch_symbols) for batch_symbols in grouped_symbols.values()),
        "stock_symbols_up_to_date": up_to_date,
        "stock_symbols_processed": 0,
        "stock_symbols_updated": 0,
        "stock_symbols_fetch_empty": 0,
        "stock_symbols_failed": 0,
        "stock_rows_added": 0,
        **_stock_timing_fields(),
    }
    if dry_run:
        return summary

    progress_started_counter = time.perf_counter()
    next_progress_counter = progress_started_counter + PROGRESS_LOG_INTERVAL_SECONDS
    group_items = sorted(grouped_symbols.items())
    total_groups = len(group_items)
    print(
        "[update_daily_bars] stock batches planned "
        f"groups={total_groups} symbols={summary['stock_symbols_planned']} up_to_date={summary['stock_symbols_up_to_date']}"
    )

    for batch_index, (batch_start_date, batch_symbols) in enumerate(group_items, start=1):
        group_started_counter = time.perf_counter()
        batch_fetch_runtime_seconds = 0.0
        batch_clean_runtime_seconds = 0.0
        batch_split_runtime_seconds = 0.0
        batch_cache_read_runtime_seconds = 0.0
        batch_merge_runtime_seconds = 0.0
        batch_write_runtime_seconds = 0.0
        batch_symbols_updated = 0
        batch_symbols_empty = 0
        batch_rows_added = 0
        batch_sample = _symbol_sample_text(batch_symbols)
        print(
            "[update_daily_bars] stock fetch start "
            f"group={batch_index}/{total_groups} start_date={batch_start_date} end_date={end_date} symbols={len(batch_symbols)} sample={batch_sample}"
        )
        try:
            fetch_started_counter = time.perf_counter()
            fetched = router.fetch_daily_bars(batch_symbols, start_date=batch_start_date, end_date=end_date)
            batch_fetch_runtime_seconds = time.perf_counter() - fetch_started_counter
            summary["stock_fetch_runtime_seconds"] += batch_fetch_runtime_seconds
        except Exception as exc:
            print(
                "[update_daily_bars] stock fetch failed "
                f"group={batch_index}/{total_groups} start_date={batch_start_date} symbols={len(batch_symbols)} sample={batch_sample} reason={exc}"
            )
            summary["stock_symbols_processed"] += len(batch_symbols)
            summary["stock_symbols_failed"] += len(batch_symbols)
            next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)
            continue

        clean_started_counter = time.perf_counter()
        fetched = clean_daily_bars(fetched)
        batch_clean_runtime_seconds = time.perf_counter() - clean_started_counter
        summary["stock_clean_runtime_seconds"] += batch_clean_runtime_seconds

        split_started_counter = time.perf_counter()
        fetched_by_symbol = {
            str(symbol): group.copy()
            for symbol, group in fetched.groupby("symbol", sort=False)
        } if not fetched.empty else {}
        batch_split_runtime_seconds = time.perf_counter() - split_started_counter
        summary["stock_split_runtime_seconds"] += batch_split_runtime_seconds
        summary["stock_fetch_groups"] += 1

        for symbol in batch_symbols:
            new_rows = fetched_by_symbol.get(symbol)
            if new_rows is None or new_rows.empty:
                summary["stock_symbols_processed"] += 1
                summary["stock_symbols_fetch_empty"] += 1
                batch_symbols_empty += 1
                next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)
                continue
            merge_result = _merge_and_write(parquet_store, "daily_bars", symbol, new_rows)
            summary["stock_cache_read_runtime_seconds"] += float(merge_result["cache_read_runtime_seconds"])
            summary["stock_merge_runtime_seconds"] += float(merge_result["merge_runtime_seconds"])
            summary["stock_write_runtime_seconds"] += float(merge_result["write_runtime_seconds"])
            batch_cache_read_runtime_seconds += float(merge_result["cache_read_runtime_seconds"])
            batch_merge_runtime_seconds += float(merge_result["merge_runtime_seconds"])
            batch_write_runtime_seconds += float(merge_result["write_runtime_seconds"])
            added_rows = int(merge_result["added_rows"])
            if added_rows <= 0:
                summary["stock_symbols_processed"] += 1
                summary["stock_symbols_fetch_empty"] += 1
                batch_symbols_empty += 1
                next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)
                continue
            summary["stock_symbols_processed"] += 1
            summary["stock_symbols_updated"] += 1
            summary["stock_rows_added"] += added_rows
            batch_symbols_updated += 1
            batch_rows_added += added_rows
            next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)

        group_runtime_seconds = _round_seconds(time.perf_counter() - group_started_counter)
        print(
            "[update_daily_bars] stock fetch done "
            f"group={batch_index}/{total_groups} start_date={batch_start_date} symbols={len(batch_symbols)} fetched_symbols={len(fetched_by_symbol)} rows={len(fetched)} updated={batch_symbols_updated} empty={batch_symbols_empty} added_rows={batch_rows_added} sample={batch_sample} "
            f"elapsed={group_runtime_seconds}s "
            f"fetch={_round_seconds(batch_fetch_runtime_seconds)}s "
            f"clean={_round_seconds(batch_clean_runtime_seconds)}s "
            f"split={_round_seconds(batch_split_runtime_seconds)}s "
            f"cache_read={_round_seconds(batch_cache_read_runtime_seconds)}s "
            f"merge={_round_seconds(batch_merge_runtime_seconds)}s "
            f"write={_round_seconds(batch_write_runtime_seconds)}s"
        )

    if summary["stock_symbols_planned"] > 0:
        _print_stock_progress(summary, progress_started_counter)

    _round_timing_fields(
        summary,
        [
            "stock_fetch_runtime_seconds",
            "stock_clean_runtime_seconds",
            "stock_split_runtime_seconds",
            "stock_cache_read_runtime_seconds",
            "stock_merge_runtime_seconds",
            "stock_write_runtime_seconds",
        ],
    )

    return summary



def _update_index_daily_bars(
    router: DataSourceRouter,
    parquet_store: ParquetStore,
    symbol: str,
    start_date: str,
    end_date: str,
    dry_run: bool,
) -> dict[str, object]:
    planned_start_date = _planned_start_date(parquet_store, "index_bars", symbol, start_date)
    summary: dict[str, object] = {
        "index_symbol": symbol,
        "index_planned": not _is_after(planned_start_date, end_date),
        "index_updated": False,
        "index_rows_added": 0,
        "index_failed": False,
        "index_fetch_empty": False,
        "index_fetch_runtime_seconds": 0.0,
        "index_clean_runtime_seconds": 0.0,
        "index_cache_read_runtime_seconds": 0.0,
        "index_merge_runtime_seconds": 0.0,
        "index_write_runtime_seconds": 0.0,
    }
    if _is_after(planned_start_date, end_date) or dry_run:
        return summary

    try:
        fetch_started_counter = time.perf_counter()
        fetched = router.fetch_index_daily_bars(symbol, start_date=planned_start_date, end_date=end_date)
        summary["index_fetch_runtime_seconds"] = time.perf_counter() - fetch_started_counter
    except Exception as exc:
        print(f"index fetch skipped: {symbol}, reason={exc}")
        summary["index_failed"] = True
        return summary

    clean_started_counter = time.perf_counter()
    fetched = clean_daily_bars(fetched)
    summary["index_clean_runtime_seconds"] = time.perf_counter() - clean_started_counter
    if fetched.empty:
        summary["index_fetch_empty"] = True
        _round_timing_fields(
            summary,
            [
                "index_fetch_runtime_seconds",
                "index_clean_runtime_seconds",
                "index_cache_read_runtime_seconds",
                "index_merge_runtime_seconds",
                "index_write_runtime_seconds",
            ],
        )
        return summary

    merge_result = _merge_and_write(parquet_store, "index_bars", symbol, fetched)
    added_rows = int(merge_result["added_rows"])
    summary["index_updated"] = added_rows > 0
    summary["index_rows_added"] = added_rows
    summary["index_cache_read_runtime_seconds"] = float(merge_result["cache_read_runtime_seconds"])
    summary["index_merge_runtime_seconds"] = float(merge_result["merge_runtime_seconds"])
    summary["index_write_runtime_seconds"] = float(merge_result["write_runtime_seconds"])
    if added_rows <= 0:
        summary["index_fetch_empty"] = True
    _round_timing_fields(
        summary,
        [
            "index_fetch_runtime_seconds",
            "index_clean_runtime_seconds",
            "index_cache_read_runtime_seconds",
            "index_merge_runtime_seconds",
            "index_write_runtime_seconds",
        ],
    )
    return summary


def _rerun_phase1(config) -> dict[str, object]:
    result = WeinsteinScreener(config).run()
    results = result.get("results", pd.DataFrame())
    latest_trade_date = None
    if isinstance(results, pd.DataFrame) and not results.empty and "trade_date" in results.columns:
        latest_trade_date = _to_iso_date(results["trade_date"].max())
    return {
        "phase1_ran": True,
        "phase1_results_count": int(len(results)) if isinstance(results, pd.DataFrame) else 0,
        "phase1_candidate_count": int(len(result.get("candidates", []))),
        "phase1_stage1_count": int(len(result.get("top_n", []))),
        "phase1_stage2_count": int(len(result.get("stage2_top_n", []))),
        "phase1_latest_trade_date": latest_trade_date,
        "phase1_summary": result.get("summary", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally update cached stock and index daily bars.")
    parser.add_argument("--config", default="config/strategy.yaml", help="Path to strategy config file.")
    parser.add_argument("--include-missing", action="store_true", help="Also fetch symbols not yet cached locally.")
    parser.add_argument("--skip-index", action="store_true", help="Skip benchmark index daily bar update.")
    parser.add_argument("--skip-phase1", action="store_true", help="Skip rerunning the Phase1 screener after data update.")
    parser.add_argument("--skip-non-trading-day", action="store_true", help="Exit successfully without updating when the target end date is not an open trading day.")
    parser.add_argument("--dry-run", action="store_true", help="Only plan updates without writing data.")
    parser.add_argument("--limit", type=int, default=0, help="Limit the number of stock symbols processed for testing.")
    args = parser.parse_args()

    started_at = _now_iso()
    started_counter = time.perf_counter()
    config = None
    try:
        config = load_config(Path(args.config))
        if args.skip_non_trading_day and not _is_open_trade_day(config, config.data.effective_end_date):
            skipped_summary = {
                "config": str(args.config),
                "started_at": started_at,
                "finished_at": _now_iso(),
                "success": True,
                "dry_run": args.dry_run,
                "include_missing": args.include_missing,
                "skip_index": args.skip_index,
                "skip_phase1": args.skip_phase1,
                "skip_non_trading_day": args.skip_non_trading_day,
                "phase1_requested": False,
                "phase1_skipped_reason": "non_trading_day",
                "end_date": config.data.effective_end_date,
                "skipped_non_trading_day": True,
                "total_runtime_seconds": _round_seconds(time.perf_counter() - started_counter),
            }
            _write_status(config, skipped_summary)
            print(json.dumps(skipped_summary, ensure_ascii=False, indent=2, default=str))
            return
        router = DataSourceRouter(config)
        parquet_store = ParquetStore(config.parquet_root)
        duckdb_store = DuckDBStore(config.duckdb_path)

        symbols = _resolve_stock_symbols(router, parquet_store, config, args.include_missing)
        if args.limit > 0:
            symbols = symbols[: args.limit]

        stock_started_counter = time.perf_counter()
        stock_summary = _update_stock_daily_bars(
            router=router,
            parquet_store=parquet_store,
            symbols=symbols,
            start_date=config.data.effective_start_date,
            end_date=config.data.effective_end_date,
            dry_run=args.dry_run,
        )
        stock_runtime_seconds = _round_seconds(time.perf_counter() - stock_started_counter)

        index_started_counter = time.perf_counter()
        if args.skip_index:
            index_summary = {
                "index_symbol": config.market.benchmark_symbol,
                "index_planned": False,
                "index_updated": False,
                "index_rows_added": 0,
                "index_failed": False,
                "index_fetch_empty": False,
            }
        else:
            index_summary = _update_index_daily_bars(
                router=router,
                parquet_store=parquet_store,
                symbol=config.market.benchmark_symbol,
                start_date=config.data.effective_start_date,
                end_date=config.data.effective_end_date,
                dry_run=args.dry_run,
            )
        index_runtime_seconds = _round_seconds(time.perf_counter() - index_started_counter)

        if not args.dry_run:
            duckdb_store.refresh_parquet_view("daily_bars", str(config.parquet_root / "daily_bars" / "*.parquet"))
            if not args.skip_index:
                duckdb_store.refresh_parquet_view("index_bars", str(config.parquet_root / "index_bars" / "*.parquet"))

        phase1_requested = not args.dry_run and not args.skip_phase1
        should_run_phase1 = phase1_requested and (stock_summary["stock_rows_added"] > 0 or bool(index_summary["index_updated"]))
        phase1_started_counter = time.perf_counter()
        if should_run_phase1:
            phase1_summary = _rerun_phase1(config)
            phase1_skipped_reason = ""
        else:
            phase1_summary = {
                "phase1_ran": False,
                "phase1_results_count": 0,
                "phase1_candidate_count": 0,
                "phase1_stage1_count": 0,
                "phase1_stage2_count": 0,
                "phase1_latest_trade_date": None,
                "phase1_summary": {},
            }
            if args.dry_run:
                phase1_skipped_reason = "dry_run"
            elif args.skip_phase1:
                phase1_skipped_reason = "skip_phase1"
            else:
                phase1_skipped_reason = "no_new_data"
        phase1_runtime_seconds = _round_seconds(time.perf_counter() - phase1_started_counter)

        summary = {
            "config": str(args.config),
            "started_at": started_at,
            "finished_at": _now_iso(),
            "success": True,
            "dry_run": args.dry_run,
            "include_missing": args.include_missing,
            "skip_index": args.skip_index,
            "skip_phase1": args.skip_phase1,
            "skip_non_trading_day": args.skip_non_trading_day,
            "phase1_requested": phase1_requested,
            "phase1_skipped_reason": phase1_skipped_reason,
            "end_date": config.data.effective_end_date,
            "stock_update_runtime_seconds": stock_runtime_seconds,
            "index_update_runtime_seconds": index_runtime_seconds,
            "phase1_runtime_seconds": phase1_runtime_seconds,
            "total_runtime_seconds": _round_seconds(time.perf_counter() - started_counter),
            **stock_summary,
            **index_summary,
            **phase1_summary,
        }
        _write_status(config, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        if config is not None:
            failure_summary = {
                "config": str(args.config),
                "started_at": started_at,
                "finished_at": _now_iso(),
                "success": False,
                "dry_run": args.dry_run,
                "include_missing": args.include_missing,
                "skip_index": args.skip_index,
                "skip_phase1": args.skip_phase1,
                "skip_non_trading_day": args.skip_non_trading_day,
                "error": str(exc),
                "total_runtime_seconds": _round_seconds(time.perf_counter() - started_counter),
            }
            _write_status(config, failure_summary)
        raise


if __name__ == "__main__":
    main()
