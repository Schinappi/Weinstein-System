"""Box-oscillation backtest endpoint using historical daily slices."""
from __future__ import annotations

import threading
import time
import uuid

import pandas as pd

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.rules.demand_support import compute_demand_support_quality


_box_scan_jobs: dict[str, dict] = {}
_box_scan_jobs_by_date: dict[str, str] = {}
_box_scan_result_cache: dict[str, dict] = {}
_box_scan_lock = threading.Lock()
BOX_SCAN_TOP_N = 100
BOX_SCAN_RESULT_VERSION = 1


def _is_current_box_scan_result(payload: dict | None) -> bool:
    return isinstance(payload, dict) and int(payload.get("box_scan_result_version") or 0) == BOX_SCAN_RESULT_VERSION


def run_box_backtest_for_symbols(
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
    """Run box-oscillation backtest for explicit symbols, or scan all symbols."""
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
        if not symbol:
            continue
        sym_date_pairs.append((_normalize_symbol(symbol), dt))

    resolved_target_date = _resolve_display_target_date(sym_date_pairs, fallback=target_date)
    if not sym_date_pairs:
        if target_date:
            if reuse_scan:
                return _get_or_start_box_scan_job(
                    store,
                    config,
                    target_date,
                    force_refresh=force_refresh,
                    name_lookup=name_lookup,
                    snapshot_loader=snapshot_loader,
                    snapshot_saver=snapshot_saver,
                )
            return _start_box_scan_job(
                store,
                config,
                target_date,
                name_lookup=name_lookup,
                snapshot_saver=snapshot_saver,
            )
        return {"items": [], "error": "请输入代码和日期", "count": 0}

    items: list[dict[str, object]] = []
    for symbol, dt_str in sym_date_pairs:
        item = _evaluate_symbol(store, config, symbol, dt_str, name_lookup=name_lookup, include_non_candidate=True)
        items.append(item)

    items.sort(key=lambda item: item.get("demand_support_score") or 0, reverse=True)
    return {
        "items": items,
        "count": len(items),
        "target_date": resolved_target_date,
        "mode": "manual",
        "error": "",
        "box_scan_result_version": BOX_SCAN_RESULT_VERSION,
    }


def run_box_backtest_scan(
    store,
    config: AppConfig,
    target_date: str,
    name_lookup=None,
    job_id: str | None = None,
) -> dict:
    """Run a full-market box-oscillation scan using data up to target_date."""
    cutoff = pd.Timestamp(target_date)
    if pd.isna(cutoff):
        return {"items": [], "error": f"无效日期: {target_date}", "count": 0}

    all_symbols = [symbol for symbol in store.list_cached_symbols("daily_bars") if _is_scan_symbol_allowed(symbol, config)]
    candidates: list[dict[str, object]] = []
    progress = {"processed": 0, "candidates": 0, "total": len(all_symbols)}
    _update_box_scan_job_progress(job_id, processed=0, total=progress["total"], candidates_total=0)

    started_at = time.perf_counter()
    for symbol in all_symbols:
        try:
            item = _evaluate_symbol(store, config, symbol, target_date, name_lookup=name_lookup, include_non_candidate=False)
            if item.get("demand_support_candidate"):
                candidates.append(item)
                progress["candidates"] = len(candidates)
        except Exception:
            pass
        finally:
            progress["processed"] += 1
            _update_box_scan_job_progress(
                job_id,
                processed=progress["processed"],
                total=progress["total"],
                candidates_total=progress["candidates"],
            )

    candidates.sort(
        key=lambda item: (
            float(item.get("demand_support_score") or 0),
            float(item.get("demand_support_avg_swing_pct") or 0),
            int(item.get("demand_support_touch_count") or 0),
        ),
        reverse=True,
    )
    top_candidates = candidates[:BOX_SCAN_TOP_N]
    return {
        "items": top_candidates,
        "count": len(top_candidates),
        "target_date": target_date,
        "scanned": len(all_symbols),
        "mode": "scan",
        "elapsed": round(time.perf_counter() - started_at, 1),
        "candidates_total": len(candidates),
        "error": "",
        "box_scan_result_version": BOX_SCAN_RESULT_VERSION,
    }


def get_box_scan_status(job_id: str) -> dict:
    """Return async box scan status or finished payload."""
    with _box_scan_lock:
        job = _box_scan_jobs.get(job_id)
    if job is None:
        return {"status": "not_found"}
    if job.get("status") == "done":
        return job["result"]
    if job.get("status") == "error":
        return {
            "status": "error",
            "job_id": job_id,
            "target_date": job.get("target_date"),
            "error": str(job.get("error") or "box scan failed"),
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

    weekly_cut = build_weekly_bars(daily)
    if len(weekly_cut) < 16:
        return {
            "symbol": symbol,
            "name": _lookup_name(symbol, name_lookup=name_lookup),
            "error": f"仅{len(weekly_cut)}周",
            "available_weeks": len(weekly_cut),
            "available_days": len(daily),
        }

    result = compute_demand_support_quality(weekly_cut, config, daily=daily)
    candidate = bool(result.get("demand_support_candidate"))
    if not include_non_candidate and not candidate:
        return {"symbol": symbol, "demand_support_candidate": False}

    return {
        "symbol": symbol,
        "name": _lookup_name(symbol, name_lookup=name_lookup),
        "latest_date": str(daily["trade_date"].max().date()),
        "available_days": len(daily),
        "available_weeks": len(weekly_cut),
        "close": _optional_float(daily.sort_values("trade_date").iloc[-1].get("close")),
        "error": "",
        **_serialize_box_result(result),
    }


def _serialize_box_result(result: dict[str, object]) -> dict[str, object]:
    return {
        "demand_support_score": _optional_float(result.get("demand_support_score")) or 0.0,
        "demand_support_grade": str(result.get("demand_support_grade") or "C"),
        "demand_support_reason": str(result.get("demand_support_reason") or ""),
        "demand_support_candidate": bool(result.get("demand_support_candidate")),
        "demand_support_price": _optional_float(result.get("demand_support_price")),
        "demand_support_lower": _optional_float(result.get("demand_support_lower")),
        "demand_support_upper": _optional_float(result.get("demand_support_upper")),
        "demand_support_zone_width_pct": _optional_float(result.get("demand_support_zone_width_pct")),
        "demand_support_touch_count": int(result.get("demand_support_touch_count") or 0),
        "demand_support_success_count": int(result.get("demand_support_success_count") or 0),
        "demand_support_pending_count": int(result.get("demand_support_pending_count") or 0),
        "demand_support_success_rate": _optional_float(result.get("demand_support_success_rate")),
        "demand_support_avg_rebound_pct": _optional_float(result.get("demand_support_avg_rebound_pct")),
        "demand_support_avg_penetration_pct": _optional_float(result.get("demand_support_avg_penetration_pct")),
        "demand_support_max_penetration_pct": _optional_float(result.get("demand_support_max_penetration_pct")),
        "demand_support_box_height_pct": _optional_float(result.get("demand_support_box_height_pct")),
        "demand_support_duration_weeks": int(result.get("demand_support_duration_weeks") or 0),
        "demand_support_duration_bars": int(result.get("demand_support_duration_bars") or 0),
        "demand_support_duration_unit": str(result.get("demand_support_duration_unit") or ""),
        "demand_support_latest_touch_date": str(result.get("demand_support_latest_touch_date") or ""),
        "demand_support_avg_touch_volume_ratio": _optional_float(result.get("demand_support_avg_touch_volume_ratio")),
        "demand_support_score_touch": _optional_float(result.get("demand_support_score_touch")),
        "demand_support_score_rebound": _optional_float(result.get("demand_support_score_rebound")),
        "demand_support_score_penetration": _optional_float(result.get("demand_support_score_penetration")),
        "demand_support_score_box": _optional_float(result.get("demand_support_score_box")),
        "demand_support_score_duration": _optional_float(result.get("demand_support_score_duration")),
        "demand_support_score_cycle": _optional_float(result.get("demand_support_score_cycle")),
        "demand_support_score_volume": _optional_float(result.get("demand_support_score_volume")),
        "demand_support_data_source": str(result.get("demand_support_data_source") or ""),
        "demand_support_lookback_bars": int(result.get("demand_support_lookback_bars") or 0),
        "demand_support_avg_swing_pct": _optional_float(result.get("demand_support_avg_swing_pct")),
        "demand_support_swing_count": int(result.get("demand_support_swing_count") or 0),
        "demand_support_top_price": _optional_float(result.get("demand_support_top_price")),
        "demand_support_top_stability_pct": _optional_float(result.get("demand_support_top_stability_pct")),
        "demand_support_rebound_efficiency": _optional_float(result.get("demand_support_rebound_efficiency")),
        "demand_support_box_utilization_pct": _optional_float(result.get("demand_support_box_utilization_pct")),
        "demand_support_volume_contraction_ratio": _optional_float(result.get("demand_support_volume_contraction_ratio")),
        "demand_support_approach_gap_pct": _optional_float(result.get("demand_support_approach_gap_pct")),
        "demand_support_approach_decline_pct": _optional_float(result.get("demand_support_approach_decline_pct")),
        "demand_support_approach_energy_pct": _optional_float(result.get("demand_support_approach_energy_pct")),
        "demand_support_pullback_volume_ratio": _optional_float(result.get("demand_support_pullback_volume_ratio")),
        "demand_support_score_approach": _optional_float(result.get("demand_support_score_approach")),
        "demand_support_score_support_quality": _optional_float(result.get("demand_support_score_support_quality")),
        "demand_support_score_historical_rebound": _optional_float(result.get("demand_support_score_historical_rebound")),
        "demand_support_score_current_distance": _optional_float(result.get("demand_support_score_current_distance")),
        "demand_support_score_trend_filter": _optional_float(result.get("demand_support_score_trend_filter")),
        "demand_support_avg_5d_rebound_pct": _optional_float(result.get("demand_support_avg_5d_rebound_pct")),
        "demand_support_avg_10d_rebound_pct": _optional_float(result.get("demand_support_avg_10d_rebound_pct")),
        "demand_support_avg_20d_rebound_pct": _optional_float(result.get("demand_support_avg_20d_rebound_pct")),
        "demand_support_rebound_success_rate": _optional_float(result.get("demand_support_rebound_success_rate")),
        "demand_support_rebound_sample_count": int(result.get("demand_support_rebound_sample_count") or 0),
        "demand_support_active": bool(result.get("demand_support_active")),
        "demand_support_latest_break_pct": _optional_float(result.get("demand_support_latest_break_pct")),
    }


def _start_box_scan_job(store, config: AppConfig, target_date: str, name_lookup=None, snapshot_saver=None) -> dict:
    job_id = str(uuid.uuid4())[:8]
    with _box_scan_lock:
        _box_scan_jobs[job_id] = {
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
        _box_scan_jobs_by_date[target_date] = job_id
    thread = threading.Thread(target=_run_box_scan_async, args=(job_id, store, config, target_date), daemon=True)
    thread.start()
    return {"mode": "scan", "job_id": job_id, "status": "started", "count": 0, "target_date": target_date, "error": ""}


def _get_or_start_box_scan_job(
    store,
    config: AppConfig,
    target_date: str,
    force_refresh: bool = False,
    name_lookup=None,
    snapshot_loader=None,
    snapshot_saver=None,
) -> dict:
    with _box_scan_lock:
        if not force_refresh:
            cached = _box_scan_result_cache.get(target_date)
            if _is_current_box_scan_result(cached):
                _fill_names(cached.get("items", []), name_lookup=name_lookup)
                return cached
            if cached is not None:
                _box_scan_result_cache.pop(target_date, None)

            existing_job_id = _box_scan_jobs_by_date.get(target_date)
            if existing_job_id:
                existing_job = _box_scan_jobs.get(existing_job_id, {})
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
                if existing_job.get("status") == "done" and _is_current_box_scan_result(existing_job.get("result")):
                    _fill_names(existing_job["result"].get("items", []), name_lookup=name_lookup)
                    return existing_job["result"]

        if not force_refresh and callable(snapshot_loader):
            persisted = snapshot_loader(target_date)
            if _is_current_box_scan_result(persisted) and persisted.get("items"):
                _fill_names(persisted.get("items", []), name_lookup=name_lookup)
                _box_scan_result_cache[target_date] = persisted
                return persisted

    return _start_box_scan_job(
        store,
        config,
        target_date,
        name_lookup=name_lookup,
        snapshot_saver=snapshot_saver,
    )


def _run_box_scan_async(job_id: str, store, config: AppConfig, target_date: str) -> None:
    try:
        with _box_scan_lock:
            job = _box_scan_jobs.get(job_id, {})
            name_lookup = job.get("name_lookup")
            snapshot_saver = job.get("snapshot_saver")

        result = run_box_backtest_scan(
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
        with _box_scan_lock:
            _box_scan_jobs[job_id] = {"status": "done", "result": result, "elapsed": result.get("elapsed", 0)}
            _box_scan_jobs_by_date[target_date] = job_id
            _box_scan_result_cache[target_date] = result
    except Exception as exc:
        with _box_scan_lock:
            _box_scan_jobs[job_id] = {"status": "error", "error": str(exc)}
            if _box_scan_jobs_by_date.get(target_date) == job_id:
                _box_scan_jobs_by_date.pop(target_date, None)


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if "." in normalized:
        return normalized
    if normalized.startswith("6"):
        return f"{normalized}.SH"
    if normalized.startswith(("0", "3")):
        return f"{normalized}.SZ"
    return f"{normalized}.BJ"


def _is_scan_symbol_allowed(symbol: str, config: AppConfig) -> bool:
    code = str(symbol or "").split(".", 1)[0]
    excluded = tuple(str(prefix) for prefix in getattr(config.universe, "excluded_symbol_prefixes", []) or [])
    return not (excluded and code.startswith(excluded))


def _lookup_name(symbol: str, name_lookup=None) -> str:
    try:
        if callable(name_lookup):
            return str(name_lookup(symbol) or "")
    except Exception:
        pass
    return ""


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _resolve_display_target_date(sym_date_pairs: list[tuple[str, str]], fallback: str) -> str:
    if not sym_date_pairs:
        return fallback
    dates = [str(item[1] or "").strip() for item in sym_date_pairs if str(item[1] or "").strip()]
    if not dates:
        return fallback
    unique_dates = list(dict.fromkeys(dates))
    if len(unique_dates) == 1:
        return unique_dates[0]
    return "多日期"


def _update_box_scan_job_progress(
    job_id: str | None,
    *,
    processed: int | None = None,
    total: int | None = None,
    candidates_total: int | None = None,
) -> None:
    if not job_id:
        return
    with _box_scan_lock:
        job = _box_scan_jobs.get(job_id)
        if not job or job.get("status") == "done":
            return
        if processed is not None:
            job["processed"] = int(processed)
        if total is not None:
            job["total"] = int(total)
        if candidates_total is not None:
            job["candidates_total"] = int(candidates_total)


def _fill_names(items: list[dict[str, object]], name_lookup=None) -> None:
    for item in items:
        if not item.get("name"):
            item["name"] = _lookup_name(str(item.get("symbol") or ""), name_lookup=name_lookup)
