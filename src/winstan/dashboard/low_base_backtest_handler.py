"""Low-base backtest endpoint using historical daily slices."""
from __future__ import annotations

import threading
import time
import uuid

import pandas as pd

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig
from winstan.dashboard.box_backtest_handler import (
    _fill_names,
    _is_scan_symbol_allowed,
    _lookup_name,
    _normalize_symbol,
    _optional_float,
    _resolve_display_target_date,
)
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.rules.base_oscillation import LOOKBACK_DAYS
from winstan.rules.low_base import compute_low_base_quality


_low_base_scan_jobs: dict[str, dict] = {}
_low_base_scan_jobs_by_date: dict[str, str] = {}
_low_base_scan_result_cache: dict[str, dict] = {}
_low_base_scan_lock = threading.Lock()
LOW_BASE_SCAN_TOP_N = 100
LOW_BASE_SCAN_RESULT_VERSION = 4


def _is_current_low_base_scan_result(payload: dict | None) -> bool:
    return isinstance(payload, dict) and int(payload.get("low_base_scan_result_version") or 0) == LOW_BASE_SCAN_RESULT_VERSION


def run_low_base_backtest_for_symbols(
    store,
    config: AppConfig,
    symbols_str: str,
    target_date: str,
    reuse_scan: bool = False,
    force_refresh: bool = False,
    name_lookup=None,
    snapshot_loader=None,
    snapshot_saver=None,
) -> dict:
    lines = [line.strip() for line in symbols_str.replace("\r\n", "\n").split("\n") if line.strip()]
    sym_date_pairs: list[tuple[str, str]] = []
    for line in lines:
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            symbol = parts[0].strip().upper()
            dt = parts[1].strip()
        elif target_date:
            symbol = parts[0].strip().upper() if parts else ""
            dt = target_date
        else:
            continue
        if symbol:
            sym_date_pairs.append((_normalize_symbol(symbol), dt))

    resolved_target_date = _resolve_display_target_date(sym_date_pairs, fallback=target_date)
    if not sym_date_pairs:
        if target_date:
            if reuse_scan:
                return _get_or_start_low_base_scan_job(
                    store,
                    config,
                    target_date,
                    force_refresh=force_refresh,
                    name_lookup=name_lookup,
                    snapshot_loader=snapshot_loader,
                    snapshot_saver=snapshot_saver,
                )
            return _start_low_base_scan_job(
                store,
                config,
                target_date,
                name_lookup=name_lookup,
                snapshot_saver=snapshot_saver,
            )
        return {"items": [], "error": "请输入代码和日期", "count": 0}

    items = [
        _evaluate_symbol(store, config, symbol, dt_str, name_lookup=name_lookup, include_non_candidate=True)
        for symbol, dt_str in sym_date_pairs
    ]
    items.sort(key=lambda item: item.get("low_base_score") or 0, reverse=True)
    return {
        "items": items,
        "count": len(items),
        "target_date": resolved_target_date,
        "mode": "manual",
        "error": "",
        "low_base_scan_result_version": LOW_BASE_SCAN_RESULT_VERSION,
    }


def run_low_base_backtest_scan(
    store,
    config: AppConfig,
    target_date: str,
    name_lookup=None,
    job_id: str | None = None,
) -> dict:
    cutoff = pd.Timestamp(target_date)
    if pd.isna(cutoff):
        return {"items": [], "error": f"无效日期: {target_date}", "count": 0}

    all_symbols = [symbol for symbol in store.list_cached_symbols("daily_bars") if _is_scan_symbol_allowed(symbol, config)]
    scored_items: list[dict[str, object]] = []
    candidates_total = 0
    processed = 0
    _update_low_base_scan_job_progress(job_id, processed=0, total=len(all_symbols), candidates_total=0)

    started_at = time.perf_counter()
    for symbol in all_symbols:
        try:
            item = _evaluate_symbol(store, config, symbol, target_date, name_lookup=name_lookup, include_non_candidate=True)
            if item.get("low_base_score") is not None:
                scored_items.append(item)
                if item.get("low_base_candidate"):
                    candidates_total += 1
        except Exception:
            pass
        finally:
            processed += 1
            _update_low_base_scan_job_progress(
                job_id,
                processed=processed,
                total=len(all_symbols),
                candidates_total=candidates_total,
            )

    scored_items.sort(
        key=lambda item: (
            float(item.get("low_base_score") or 0),
            float(item.get("low_base_score_volatility") or 0),
            float(item.get("low_base_score_bottom_stability") or 0),
        ),
        reverse=True,
    )
    return {
        "items": scored_items[:LOW_BASE_SCAN_TOP_N],
        "count": len(scored_items[:LOW_BASE_SCAN_TOP_N]),
        "target_date": target_date,
        "scanned": len(all_symbols),
        "mode": "scan",
        "elapsed": round(time.perf_counter() - started_at, 1),
        "candidates_total": candidates_total,
        "error": "",
        "low_base_scan_result_version": LOW_BASE_SCAN_RESULT_VERSION,
    }


def get_low_base_scan_status(job_id: str) -> dict:
    with _low_base_scan_lock:
        job = _low_base_scan_jobs.get(job_id)
    if job is None:
        return {"status": "not_found"}
    if job.get("status") == "done":
        return job["result"]
    if job.get("status") == "error":
        return {
            "status": "error",
            "job_id": job_id,
            "target_date": job.get("target_date"),
            "error": str(job.get("error") or "low-base scan failed"),
        }
    started_at = float(job.get("started_at") or time.time())
    return {
        "status": job.get("status", "running"),
        "job_id": job_id,
        "target_date": job.get("target_date"),
        "started_at": started_at,
        "elapsed_seconds": round(max(0.0, time.time() - started_at), 1),
        "processed": int(job.get("processed") or 0),
        "total": int(job.get("total") or 0),
        "candidates_total": int(job.get("candidates_total") or 0),
    }


def _evaluate_symbol(
    store,
    config: AppConfig,
    symbol: str,
    target_date: str,
    *,
    name_lookup=None,
    include_non_candidate: bool,
) -> dict[str, object]:
    cutoff = pd.Timestamp(target_date)
    if pd.isna(cutoff):
        return {"symbol": symbol, "error": f"无效日期: {target_date}"}

    daily = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
    if daily.empty:
        return {"symbol": symbol, "name": _lookup_name(symbol, name_lookup=name_lookup), "error": "无数据"}
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    daily = daily[daily["trade_date"] <= cutoff].copy()
    if len(daily) < 60:
        return {
            "symbol": symbol,
            "name": _lookup_name(symbol, name_lookup=name_lookup),
            "error": f"仅{len(daily)}根日线",
            "available_days": len(daily),
        }

    available_days_total = len(daily)
    daily = daily.sort_values("trade_date").tail(LOOKBACK_DAYS).copy()
    weekly_cut = build_weekly_bars(daily)
    if len(weekly_cut) < 16:
        return {
            "symbol": symbol,
            "name": _lookup_name(symbol, name_lookup=name_lookup),
            "error": f"仅{len(weekly_cut)}周",
            "available_weeks": len(weekly_cut),
            "available_days": len(daily),
        }

    result = compute_low_base_quality(weekly_cut, config, daily=daily)
    candidate = bool(result.get("low_base_candidate"))
    if not include_non_candidate and not candidate:
        return {"symbol": symbol, "low_base_candidate": False}

    return {
        "symbol": symbol,
        "name": _lookup_name(symbol, name_lookup=name_lookup),
        "latest_date": str(daily["trade_date"].max().date()),
        "available_days": len(daily),
        "available_days_total": available_days_total,
        "available_weeks": len(weekly_cut),
        "close": _optional_float(daily.sort_values("trade_date").iloc[-1].get("close")),
        "error": "",
        **_serialize_low_base_result(result),
    }


def _serialize_low_base_result(result: dict[str, object]) -> dict[str, object]:
    fields = [
        "low_base_score",
        "low_base_grade",
        "low_base_reason",
        "low_base_candidate",
        "low_base_support_price",
        "low_base_lower",
        "low_base_upper",
        "low_base_top_price",
        "low_base_base_start_date",
        "low_base_base_end_date",
        "low_base_base_range",
        "low_base_duration_bars",
        "low_base_duration_weeks",
        "low_base_duration_unit",
        "low_base_score_prior_decline",
        "low_base_score_duration",
        "low_base_score_volatility",
        "low_base_score_volume",
        "low_base_score_bottom_stability",
        "low_base_score_breakout_readiness",
        "low_base_prior_decline_pct",
        "low_base_prior_decline_start_date",
        "low_base_prior_decline_end_date",
        "low_base_prior_decline_range",
        "low_base_volatility_contraction_ratio",
        "low_base_volatility_recent_pct",
        "low_base_volatility_baseline_pct",
        "low_base_volatility_range",
        "low_base_volume_decay_ratio",
        "low_base_volume_recent_avg",
        "low_base_volume_baseline_avg",
        "low_base_volume_range",
        "low_base_recent_amount_avg",
        "low_base_liquidity_threshold",
        "low_base_liquidity_ok",
        "low_base_liquidity_range",
        "low_base_direction_volume_ratio",
        "low_base_direction_latest_volume",
        "low_base_direction_base_avg_volume",
        "low_base_score_direction_volume",
        "low_base_direction_volume_range",
        "low_base_touch_count",
        "low_base_touch_low_progress_pct",
        "low_base_recent_intraday_break_pct",
        "low_base_recent_close_break_pct",
        "low_base_false_break_count",
        "low_base_bottom_stability_range",
        "low_base_distance_to_top_pct",
        "low_base_ema_slope_20_pct",
        "low_base_abnormal_volume_ratio",
        "low_base_breakout_readiness_range",
        "low_base_approach_gap_pct",
        "low_base_avg_penetration_pct",
        "low_base_avg_swing_pct",
        "low_base_support_active",
    ]
    serialized: dict[str, object] = {}
    for field in fields:
        value = result.get(field)
        if field.endswith("_candidate") or field.endswith("_ok") or field.endswith("_active"):
            serialized[field] = value if value is None else bool(value)
        elif field.endswith("_date") or field.endswith("_range") or field.endswith("_grade") or field.endswith("_reason") or field.endswith("_unit"):
            serialized[field] = str(value or "")
        elif field.endswith("_bars") or field.endswith("_weeks") or field.endswith("_count"):
            serialized[field] = int(value or 0)
        else:
            serialized[field] = _optional_float(value)
    serialized["low_base_score"] = _optional_float(result.get("low_base_score")) or 0.0
    serialized["low_base_grade"] = str(result.get("low_base_grade") or "C")
    serialized["low_base_reason"] = str(result.get("low_base_reason") or "")
    serialized["low_base_candidate"] = bool(result.get("low_base_candidate"))
    return serialized


def _start_low_base_scan_job(store, config: AppConfig, target_date: str, name_lookup=None, snapshot_saver=None) -> dict:
    job_id = str(uuid.uuid4())[:8]
    with _low_base_scan_lock:
        _low_base_scan_jobs[job_id] = {
            "status": "running",
            "started_at": time.time(),
            "result": None,
            "target_date": target_date,
            "name_lookup": name_lookup,
            "snapshot_saver": snapshot_saver,
            "processed": 0,
            "total": 0,
            "candidates_total": 0,
        }
        _low_base_scan_jobs_by_date[target_date] = job_id
    thread = threading.Thread(target=_run_low_base_scan_async, args=(job_id, store, config, target_date), daemon=True)
    thread.start()
    return {"mode": "scan", "job_id": job_id, "status": "started", "count": 0, "target_date": target_date, "error": ""}


def _get_or_start_low_base_scan_job(
    store,
    config: AppConfig,
    target_date: str,
    force_refresh: bool = False,
    name_lookup=None,
    snapshot_loader=None,
    snapshot_saver=None,
) -> dict:
    with _low_base_scan_lock:
        if not force_refresh:
            cached = _low_base_scan_result_cache.get(target_date)
            if _is_current_low_base_scan_result(cached):
                _fill_names(cached.get("items", []), name_lookup=name_lookup)
                return cached
            if cached is not None:
                _low_base_scan_result_cache.pop(target_date, None)

            existing_job_id = _low_base_scan_jobs_by_date.get(target_date)
            if existing_job_id:
                existing_job = _low_base_scan_jobs.get(existing_job_id, {})
                if existing_job.get("status") in {"running", "started"}:
                    return {
                        "mode": "scan",
                        "job_id": existing_job_id,
                        "status": existing_job.get("status", "running"),
                        "count": 0,
                        "target_date": target_date,
                        "started_at": existing_job.get("started_at"),
                        "elapsed_seconds": round(max(0.0, time.time() - float(existing_job.get("started_at") or time.time())), 1),
                        "processed": int(existing_job.get("processed") or 0),
                        "total": int(existing_job.get("total") or 0),
                        "candidates_total": int(existing_job.get("candidates_total") or 0),
                        "error": "",
                    }
                if existing_job.get("status") == "done" and _is_current_low_base_scan_result(existing_job.get("result")):
                    _fill_names(existing_job["result"].get("items", []), name_lookup=name_lookup)
                    return existing_job["result"]

        if not force_refresh and callable(snapshot_loader):
            persisted = snapshot_loader(target_date)
            if _is_current_low_base_scan_result(persisted) and persisted.get("items"):
                _fill_names(persisted.get("items", []), name_lookup=name_lookup)
                _low_base_scan_result_cache[target_date] = persisted
                return persisted

    return _start_low_base_scan_job(
        store,
        config,
        target_date,
        name_lookup=name_lookup,
        snapshot_saver=snapshot_saver,
    )


def _run_low_base_scan_async(job_id: str, store, config: AppConfig, target_date: str) -> None:
    try:
        with _low_base_scan_lock:
            job = _low_base_scan_jobs.get(job_id, {})
            name_lookup = job.get("name_lookup")
            snapshot_saver = job.get("snapshot_saver")

        result = run_low_base_backtest_scan(
            store,
            config,
            target_date,
            name_lookup=name_lookup,
            job_id=job_id,
        )
        _fill_names(result.get("items", []), name_lookup=name_lookup)
        if callable(snapshot_saver):
            try:
                snapshot_saver(target_date, result)
            except Exception:
                pass
        with _low_base_scan_lock:
            _low_base_scan_jobs[job_id] = {"status": "done", "result": result, "elapsed": result.get("elapsed", 0)}
            _low_base_scan_jobs_by_date[target_date] = job_id
            _low_base_scan_result_cache[target_date] = result
    except Exception as exc:
        with _low_base_scan_lock:
            _low_base_scan_jobs[job_id] = {"status": "error", "error": str(exc)}
            if _low_base_scan_jobs_by_date.get(target_date) == job_id:
                _low_base_scan_jobs_by_date.pop(target_date, None)


def _update_low_base_scan_job_progress(
    job_id: str | None,
    *,
    processed: int | None = None,
    total: int | None = None,
    candidates_total: int | None = None,
) -> None:
    if not job_id:
        return
    with _low_base_scan_lock:
        job = _low_base_scan_jobs.get(job_id)
        if not job or job.get("status") == "done":
            return
        if processed is not None:
            job["processed"] = int(processed)
        if total is not None:
            job["total"] = int(total)
        if candidates_total is not None:
            job["candidates_total"] = int(candidates_total)
