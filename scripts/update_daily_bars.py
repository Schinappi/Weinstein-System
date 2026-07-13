from __future__ import annotations

import argparse
import contextlib
import io
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
from winstan.adapters.tushare_adapter import TushareAdapter
from winstan.adapters.tushare_client import build_tushare_pro
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config, normalize_date_like
from winstan.pipeline.universe import build_universe
from winstan.pipeline.screener import WeinsteinScreener
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore


PROGRESS_LOG_INTERVAL_SECONDS = 300
BULK_BACKFILL_MAX_DAYS = 31



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


def _days_between(start_date: str, end_date: str) -> int:
    return max(0, (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days)



def _symbol_sample_text(symbols: list[str], limit: int = 5) -> str:
    if not symbols:
        return ""
    sample = symbols[:limit]
    suffix = "" if len(symbols) <= limit else f" ... +{len(symbols) - limit}"
    return ",".join(sample) + suffix



def _is_open_trade_day(config, value: str | None) -> bool:
    trade_date = normalize_date_like(value, date.today().isoformat()) or date.today().isoformat()
    trade_date_compact = trade_date.replace("-", "")
    primary_source = str(getattr(config.data, "primary_source", "") or "").lower()
    if primary_source == "baostock":
        try:
            import baostock as bs

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                login_result = bs.login()
            if getattr(login_result, "error_code", "") == "0":
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        calendar = bs.query_trade_dates(start_date=trade_date, end_date=trade_date)
                    if getattr(calendar, "error_code", "") == "0":
                        rows: list[list[str]] = []
                        while calendar.next():
                            rows.append(calendar.get_row_data())
                        if rows:
                            frame = pd.DataFrame(rows, columns=getattr(calendar, "fields", ["calendar_date", "is_trading_day"]))
                            return bool(
                                not frame.empty
                                and "is_trading_day" in frame.columns
                                and str(frame.iloc[0]["is_trading_day"]) == "1"
                            )
                finally:
                    try:
                        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            bs.logout()
                    except Exception:
                        pass
        except Exception:
            pass
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


def _resolve_open_trade_dates(router: DataSourceRouter, start_date: str, end_date: str) -> list[str]:
    start_compact = start_date.replace("-", "")
    end_compact = end_date.replace("-", "")
    for adapter in (router.primary, router.fallback):
        if adapter is None:
            continue
        fetch_trade_dates = getattr(adapter, "_fetch_open_trade_dates", None)
        if callable(fetch_trade_dates):
            try:
                values = fetch_trade_dates(start_compact, end_compact)
            except Exception:
                values = []
            if values:
                return [pd.to_datetime(value).date().isoformat() for value in values]
    return [value.date().isoformat() for value in pd.bdate_range(start=start_date, end=end_date)]


def _effective_trade_end_date(router: DataSourceRouter, end_date: str) -> str:
    lookback_start = (date.fromisoformat(end_date) - timedelta(days=14)).isoformat()
    trade_dates = _resolve_open_trade_dates(router, lookback_start, end_date)
    valid_dates = [trade_date for trade_date in trade_dates if trade_date <= end_date]
    if valid_dates:
        return valid_dates[-1]
    fallback_dates = [value.date().isoformat() for value in pd.bdate_range(end=end_date, periods=1)]
    return fallback_dates[-1] if fallback_dates else end_date


def _select_trade_date_fetcher(router: DataSourceRouter):
    fast_sources = {"tushare", "chinadata"}
    adapter = router.primary
    if adapter is not None and getattr(adapter, "source_name", "") in fast_sources:
        return adapter
    return None



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


def _load_latest_trade_dates(parquet_store: ParquetStore, dataset: str) -> dict[str, str]:
    dataset_dir = parquet_store.root / dataset
    files = [str(path).replace("\\", "/") for path in dataset_dir.glob("*.parquet")]
    if not files:
        return {}

    query = (
        "SELECT symbol, CAST(MAX(CAST(trade_date AS TIMESTAMP)) AS DATE) AS latest_trade_date "
        "FROM read_parquet($1, union_by_name=true) "
        "WHERE symbol IS NOT NULL AND trade_date IS NOT NULL "
        "GROUP BY symbol"
    )
    with DuckDBStore(Path(":memory:")).connect() as conn:
        frame = conn.execute(query, [files]).fetchdf()
    if frame.empty:
        return {}

    latest_by_symbol: dict[str, str] = {}
    for row in frame.itertuples(index=False):
        symbol = str(getattr(row, "symbol", "") or "").strip()
        latest = _to_iso_date(getattr(row, "latest_trade_date", None))
        if symbol and latest:
            latest_by_symbol[symbol] = latest
    return latest_by_symbol



def _planned_start_date(latest_trade_dates: dict[str, str], symbol: str, fallback_start_date: str) -> str:
    latest = latest_trade_dates.get(symbol)
    if not latest:
        return fallback_start_date
    # Advance to the next trading update window so single-day runs can hit
    # the adapter's "trade_date batch" fast path instead of re-fetching the
    # latest cached day for every symbol.
    planned = _next_day(latest)
    return planned if not _is_after(fallback_start_date, planned) else fallback_start_date



def _merge_and_write(parquet_store: ParquetStore, dataset: str, symbol: str, new_rows: pd.DataFrame, config=None, adjust_type: str | None = None) -> dict[str, float | int]:
    read_started_counter = time.perf_counter()
    cached = clean_daily_bars(parquet_store.read_symbol_frame(dataset, symbol))
    read_runtime_seconds = _round_seconds(time.perf_counter() - read_started_counter)

    merge_started_counter = time.perf_counter()
    merged = clean_daily_bars(pd.concat([cached, new_rows], ignore_index=True))

    # Re-apply forward adjustment only when adjust_type == "forward".
    # When adjust_type is "none", preserve raw unadjusted prices and
    # strip any stale adj_factor left by previous forward-adjusted runs.
    if dataset == "daily_bars" and adjust_type == "forward":
        # When a corporate action changed the latest adj_factor the
        # cached historical rows still carry the old value.  Refresh
        # the full adj_factor series first, then re-apply adjustment.
        if config is not None and _needs_adj_factor_refresh(cached, new_rows):
            merged = _refresh_adj_factors_on_merged(symbol, merged, config)
            merged = _reapply_forward_adjustment(merged, symbol)

    # Strip adj_factor when not in forward mode — prevents stale
    # factor columns from triggering adjustment on future incremental runs.
    if "adj_factor" in merged.columns and adjust_type != "forward":
        merged = merged.drop(columns=["adj_factor"])

    added_rows = max(0, len(merged) - len(cached))
    cache_changed = not merged.reset_index(drop=True).equals(cached.reset_index(drop=True))
    merge_runtime_seconds = _round_seconds(time.perf_counter() - merge_started_counter)

    write_runtime_seconds = 0.0
    if cache_changed:
        write_started_counter = time.perf_counter()
        parquet_store.write_symbol_frame(dataset, symbol, merged)
        write_runtime_seconds = _round_seconds(time.perf_counter() - write_started_counter)
    return {
        "added_rows": int(added_rows),
        "cache_changed": int(cache_changed),
        "cache_read_runtime_seconds": read_runtime_seconds,
        "merge_runtime_seconds": merge_runtime_seconds,
        "write_runtime_seconds": write_runtime_seconds,
    }


def _reapply_forward_adjustment(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Re-compute forward-adjusted OHLC for *all* rows in *frame*.

    When a corporate action changes the latest ``adj_factor``, historical
    rows that were cached before the action still carry the old factor.
    Simply calling :meth:`TushareAdapter._apply_forward_adjustment` on the
    merged dataset fixes this: each row is rescaled with
    ``adj_factor / latest_adj_factor``.
    """
    if frame.empty or "adj_factor" not in frame.columns:
        return frame
    # _apply_forward_adjustment expects a "ts_code" column.
    working = frame.copy()
    if "ts_code" not in working.columns and "symbol" in working.columns:
        working["ts_code"] = working["symbol"]
    return TushareAdapter._apply_forward_adjustment(working)


def _needs_adj_factor_refresh(cached: pd.DataFrame, new_rows: pd.DataFrame) -> bool:
    """Return True when *new_rows* carry a different latest adj_factor."""
    if "adj_factor" not in cached.columns or "adj_factor" not in new_rows.columns:
        return False
    cached_af = pd.to_numeric(cached["adj_factor"], errors="coerce")
    new_af = pd.to_numeric(new_rows["adj_factor"], errors="coerce")
    if cached_af.dropna().empty or new_af.dropna().empty:
        return False
    return float(cached_af.max()) != float(new_af.max())


def _refresh_adj_factors_on_merged(symbol: str, merged: pd.DataFrame, config) -> pd.DataFrame:
    """Re-fetch *symbol*'s adj_factor for its full history and merge into *merged*."""
    token = config.data.tushare_token if config else None
    if not token:
        return merged
    start_date = config.data.effective_start_date
    end_date = config.data.effective_end_date
    try:
        _, pro = build_tushare_pro(token)
        adj_frame = pro.adj_factor(
            ts_code=symbol,
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            fields="ts_code,trade_date,adj_factor",
        )
    except Exception:
        return merged

    if adj_frame is None or adj_frame.empty:
        return merged

    adj_frame["trade_date"] = pd.to_datetime(adj_frame["trade_date"], format="%Y%m%d", errors="coerce")
    working = merged.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    # Drop stale adj_factor from all rows, re-merge fresh values.
    working = working.drop(columns=["adj_factor"], errors="ignore")
    working = working.merge(
        adj_frame[["ts_code", "trade_date", "adj_factor"]].rename(columns={"ts_code": "symbol"}),
        on=["symbol", "trade_date"],
        how="left",
    )
    return working



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



def _build_stock_summary(scanned: int, planned: int, up_to_date: int) -> dict[str, int | float]:
    return {
        "stock_symbols_scanned": scanned,
        "stock_symbols_planned": planned,
        "stock_symbols_up_to_date": up_to_date,
        "stock_symbols_processed": 0,
        "stock_symbols_updated": 0,
        "stock_symbols_fetch_empty": 0,
        "stock_symbols_failed": 0,
        "stock_rows_added": 0,
        **_stock_timing_fields(),
    }


def _combine_stock_summaries(base: dict[str, int | float], extra: dict[str, int | float]) -> dict[str, int | float]:
    combined = dict(base)
    for key, value in extra.items():
        if key == "stock_symbols_scanned":
            combined[key] = max(int(combined.get(key, 0)), int(value or 0))
        elif key in {"stock_symbols_planned", "stock_symbols_up_to_date", "stock_symbols_processed", "stock_symbols_updated", "stock_symbols_fetch_empty", "stock_symbols_failed", "stock_rows_added", "stock_fetch_groups"}:
            combined[key] = int(combined.get(key, 0)) + int(value or 0)
        elif key in _stock_timing_fields():
            combined[key] = float(combined.get(key, 0.0)) + float(value or 0.0)
        else:
            combined[key] = value
    return combined


def _update_stock_daily_bars_legacy(
    router: DataSourceRouter,
    parquet_store: ParquetStore,
    grouped_symbols: dict[str, list[str]],
    scanned_count: int,
    up_to_date: int,
    dry_run: bool,
    end_date: str,
) -> dict[str, int | float]:
    summary = _build_stock_summary(scanned_count, sum(len(batch_symbols) for batch_symbols in grouped_symbols.values()), up_to_date)
    if dry_run or not grouped_symbols:
        return summary

    progress_started_counter = time.perf_counter()
    next_progress_counter = progress_started_counter + PROGRESS_LOG_INTERVAL_SECONDS
    group_items = sorted(grouped_symbols.items())
    total_groups = len(group_items)
    print(
        "[update_daily_bars] stock batches planned "
        f"groups={total_groups} symbols={summary['stock_symbols_planned']} up_to_date={summary['stock_symbols_up_to_date']} mode=legacy"
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
            f"group={batch_index}/{total_groups} start_date={batch_start_date} end_date={end_date} symbols={len(batch_symbols)} sample={batch_sample} mode=legacy"
        )
        try:
            fetch_started_counter = time.perf_counter()
            fetched = router.fetch_daily_bars(batch_symbols, start_date=batch_start_date, end_date=end_date)
            batch_fetch_runtime_seconds = time.perf_counter() - fetch_started_counter
            summary["stock_fetch_runtime_seconds"] += batch_fetch_runtime_seconds
        except Exception as exc:
            print(
                "[update_daily_bars] stock fetch failed "
                f"group={batch_index}/{total_groups} start_date={batch_start_date} symbols={len(batch_symbols)} sample={batch_sample} reason={exc} mode=legacy"
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
            merge_result = _merge_and_write(parquet_store, "daily_bars", symbol, new_rows, config=router.config, adjust_type=router.config.data.adjust_type)
            summary["stock_cache_read_runtime_seconds"] += float(merge_result["cache_read_runtime_seconds"])
            summary["stock_merge_runtime_seconds"] += float(merge_result["merge_runtime_seconds"])
            summary["stock_write_runtime_seconds"] += float(merge_result["write_runtime_seconds"])
            batch_cache_read_runtime_seconds += float(merge_result["cache_read_runtime_seconds"])
            batch_merge_runtime_seconds += float(merge_result["merge_runtime_seconds"])
            batch_write_runtime_seconds += float(merge_result["write_runtime_seconds"])
            added_rows = int(merge_result["added_rows"])
            cache_changed = bool(merge_result["cache_changed"])
            if not cache_changed:
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
            f"write={_round_seconds(batch_write_runtime_seconds)}s mode=legacy"
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


def _update_stock_daily_bars_by_trade_date(
    router: DataSourceRouter,
    parquet_store: ParquetStore,
    grouped_symbols: dict[str, list[str]],
    scanned_count: int,
    up_to_date: int,
    dry_run: bool,
    end_date: str,
) -> dict[str, int | float]:
    summary = _build_stock_summary(scanned_count, sum(len(batch_symbols) for batch_symbols in grouped_symbols.values()), up_to_date)
    if dry_run or not grouped_symbols:
        return summary

    earliest_start = min(grouped_symbols)
    trade_dates = _resolve_open_trade_dates(router, earliest_start, end_date)
    trade_dates = [trade_date for trade_date in trade_dates if trade_date >= earliest_start]
    if not trade_dates:
        return summary

    active_symbols: list[str] = []
    pending_by_symbol: dict[str, list[pd.DataFrame]] = defaultdict(list)
    failed_symbols: set[str] = set()
    grouped_items = sorted((start, sorted(batch_symbols)) for start, batch_symbols in grouped_symbols.items())
    start_cursor = 0

    progress_started_counter = time.perf_counter()
    next_progress_counter = progress_started_counter + PROGRESS_LOG_INTERVAL_SECONDS
    trade_date_fetcher = _select_trade_date_fetcher(router)
    fetcher_name = getattr(trade_date_fetcher, "source_name", "") if trade_date_fetcher is not None else "router"
    print(
        "[update_daily_bars] stock batches planned "
        f"groups={len(grouped_items)} symbols={summary['stock_symbols_planned']} up_to_date={summary['stock_symbols_up_to_date']} mode=trade_date fetcher={fetcher_name}"
    )

    for trade_index, trade_date in enumerate(trade_dates, start=1):
        while start_cursor < len(grouped_items) and grouped_items[start_cursor][0] <= trade_date:
            active_symbols.extend(grouped_items[start_cursor][1])
            start_cursor += 1

        if not active_symbols:
            continue

        day_symbols = sorted(set(active_symbols))
        day_sample = _symbol_sample_text(day_symbols)
        group_started_counter = time.perf_counter()
        day_fetch_runtime_seconds = 0.0
        day_clean_runtime_seconds = 0.0
        day_split_runtime_seconds = 0.0
        print(
            "[update_daily_bars] stock fetch start "
            f"trade_date={trade_date} day={trade_index}/{len(trade_dates)} symbols={len(day_symbols)} sample={day_sample} mode=trade_date"
        )

        try:
            fetch_started_counter = time.perf_counter()
            if trade_date_fetcher is not None:
                fetched = trade_date_fetcher.fetch_daily_bars(
                    day_symbols,
                    start_date=trade_date,
                    end_date=trade_date,
                    adjust_type=router.config.data.adjust_type,
                )
            else:
                fetched = router.fetch_daily_bars(day_symbols, start_date=trade_date, end_date=trade_date)
            day_fetch_runtime_seconds = time.perf_counter() - fetch_started_counter
            summary["stock_fetch_runtime_seconds"] += day_fetch_runtime_seconds
        except Exception as exc:
            print(
                "[update_daily_bars] stock fetch failed "
                f"trade_date={trade_date} symbols={len(day_symbols)} sample={day_sample} reason={exc} mode=trade_date"
            )
            failed_symbols.update(day_symbols)
            summary["stock_fetch_groups"] += 1
            next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)
            continue

        clean_started_counter = time.perf_counter()
        fetched = clean_daily_bars(fetched)
        day_clean_runtime_seconds = time.perf_counter() - clean_started_counter
        summary["stock_clean_runtime_seconds"] += day_clean_runtime_seconds

        split_started_counter = time.perf_counter()
        fetched_by_symbol = {
            str(symbol): group.copy()
            for symbol, group in fetched.groupby("symbol", sort=False)
        } if not fetched.empty else {}
        day_split_runtime_seconds = time.perf_counter() - split_started_counter
        summary["stock_split_runtime_seconds"] += day_split_runtime_seconds
        summary["stock_fetch_groups"] += 1

        for symbol, group in fetched_by_symbol.items():
            pending_by_symbol[symbol].append(group)

        group_runtime_seconds = _round_seconds(time.perf_counter() - group_started_counter)
        print(
            "[update_daily_bars] stock fetch done "
            f"trade_date={trade_date} symbols={len(day_symbols)} fetched_symbols={len(fetched_by_symbol)} rows={len(fetched)} sample={day_sample} "
            f"elapsed={group_runtime_seconds}s "
            f"fetch={_round_seconds(day_fetch_runtime_seconds)}s "
            f"clean={_round_seconds(day_clean_runtime_seconds)}s "
            f"split={_round_seconds(day_split_runtime_seconds)}s mode=trade_date"
        )

    for symbol in sorted({sym for batch_symbols in grouped_symbols.values() for sym in batch_symbols}):
        new_row_groups = pending_by_symbol.get(symbol, [])
        if not new_row_groups:
            summary["stock_symbols_processed"] += 1
            if symbol in failed_symbols:
                summary["stock_symbols_failed"] += 1
            else:
                summary["stock_symbols_fetch_empty"] += 1
            next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)
            continue

        new_rows = clean_daily_bars(pd.concat(new_row_groups, ignore_index=True))
        merge_result = _merge_and_write(parquet_store, "daily_bars", symbol, new_rows, config=router.config, adjust_type=router.config.data.adjust_type)
        summary["stock_cache_read_runtime_seconds"] += float(merge_result["cache_read_runtime_seconds"])
        summary["stock_merge_runtime_seconds"] += float(merge_result["merge_runtime_seconds"])
        summary["stock_write_runtime_seconds"] += float(merge_result["write_runtime_seconds"])
        added_rows = int(merge_result["added_rows"])
        cache_changed = bool(merge_result["cache_changed"])
        summary["stock_symbols_processed"] += 1
        if symbol in failed_symbols:
            summary["stock_symbols_failed"] += 1
        if not cache_changed:
            summary["stock_symbols_fetch_empty"] += 1
            next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)
            continue

        summary["stock_symbols_updated"] += 1
        summary["stock_rows_added"] += added_rows
        next_progress_counter = _maybe_print_stock_progress(summary, progress_started_counter, next_progress_counter)

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


def _update_stock_daily_bars(
    router: DataSourceRouter,
    parquet_store: ParquetStore,
    symbols: list[str],
    start_date: str,
    end_date: str,
    dry_run: bool,
) -> dict[str, int | float]:
    effective_end_date = _effective_trade_end_date(router, end_date)
    trade_date_fetcher = _select_trade_date_fetcher(router)
    latest_trade_dates = _load_latest_trade_dates(parquet_store, "daily_bars")
    recent_grouped_symbols: dict[str, list[str]] = defaultdict(list)
    legacy_grouped_symbols: dict[str, list[str]] = defaultdict(list)
    up_to_date = 0
    for symbol in symbols:
        symbol_start_date = _planned_start_date(latest_trade_dates, symbol, start_date)
        if _is_after(symbol_start_date, effective_end_date):
            up_to_date += 1
            continue
        if _days_between(symbol_start_date, effective_end_date) <= BULK_BACKFILL_MAX_DAYS:
            recent_grouped_symbols[symbol_start_date].append(symbol)
        else:
            legacy_grouped_symbols[symbol_start_date].append(symbol)

    if trade_date_fetcher is None and recent_grouped_symbols:
        for group_start_date, batch_symbols in recent_grouped_symbols.items():
            legacy_grouped_symbols[group_start_date].extend(batch_symbols)
        recent_grouped_symbols = defaultdict(list)

    summary = _build_stock_summary(len(symbols), 0, up_to_date)
    if dry_run:
        summary["stock_symbols_planned"] = sum(len(v) for v in recent_grouped_symbols.values()) + sum(len(v) for v in legacy_grouped_symbols.values())
        return summary

    recent_scanned = sum(len(v) for v in recent_grouped_symbols.values())
    legacy_scanned = sum(len(v) for v in legacy_grouped_symbols.values())
    recent_summary = _update_stock_daily_bars_by_trade_date(
        router=router,
        parquet_store=parquet_store,
        grouped_symbols=recent_grouped_symbols,
        scanned_count=recent_scanned,
        up_to_date=0,
        dry_run=dry_run,
        end_date=effective_end_date,
    )
    legacy_summary = _update_stock_daily_bars_legacy(
        router=router,
        parquet_store=parquet_store,
        grouped_symbols=legacy_grouped_symbols,
        scanned_count=legacy_scanned,
        up_to_date=0,
        dry_run=dry_run,
        end_date=effective_end_date,
    )

    summary = _combine_stock_summaries(summary, recent_summary)
    summary = _combine_stock_summaries(summary, legacy_summary)
    summary["stock_symbols_scanned"] = len(symbols)
    summary["stock_symbols_up_to_date"] = up_to_date
    return summary



def _update_index_daily_bars(
    router: DataSourceRouter,
    parquet_store: ParquetStore,
    symbol: str,
    start_date: str,
    end_date: str,
    dry_run: bool,
) -> dict[str, object]:
    effective_end_date = _effective_trade_end_date(router, end_date)
    latest_trade_dates = _load_latest_trade_dates(parquet_store, "index_bars")
    planned_start_date = _planned_start_date(latest_trade_dates, symbol, start_date)
    summary: dict[str, object] = {
        "index_symbol": symbol,
        "index_planned": not _is_after(planned_start_date, effective_end_date),
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
    if _is_after(planned_start_date, effective_end_date) or dry_run:
        return summary

    try:
        fetch_started_counter = time.perf_counter()
        fetched = router.fetch_index_daily_bars(symbol, start_date=planned_start_date, end_date=effective_end_date)
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
    summary["index_updated"] = bool(merge_result["cache_changed"])
    summary["index_rows_added"] = added_rows
    summary["index_cache_read_runtime_seconds"] = float(merge_result["cache_read_runtime_seconds"])
    summary["index_merge_runtime_seconds"] = float(merge_result["merge_runtime_seconds"])
    summary["index_write_runtime_seconds"] = float(merge_result["write_runtime_seconds"])
    if not summary["index_updated"]:
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


def _rerun_screener(config) -> dict[str, object]:
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
    parser.add_argument("--skip-phase1", action="store_true", help="Skip rerunning the Weinstein screener after data update.")
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
        should_run_phase1 = phase1_requested and (stock_summary["stock_symbols_updated"] > 0 or bool(index_summary["index_updated"]))
        phase1_started_counter = time.perf_counter()
        if should_run_phase1:
            phase1_summary = _rerun_screener(config)
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
