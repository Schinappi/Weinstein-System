"""Crash-rebound scan/backtest endpoint using historical daily slices."""
from __future__ import annotations

import threading
import time
import uuid

import pandas as pd

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig
from winstan.rules.crash_rebound import compute_crash_rebound_quality


_crash_scan_jobs: dict[str, dict] = {}
_crash_scan_jobs_by_date: dict[str, str] = {}
_crash_scan_result_cache: dict[str, dict] = {}
_crash_scan_lock = threading.Lock()
CRASH_SCAN_TOP_N = 100
CRASH_SCAN_RESULT_VERSION = 3


def _is_current_crash_scan_result(payload: dict | None) -> bool:
    return isinstance(payload, dict) and int(payload.get("crash_scan_result_version") or 0) == CRASH_SCAN_RESULT_VERSION


def run_crash_rebound_for_symbols(
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
                return _get_or_start_crash_scan_job(
                    store,
                    config,
                    target_date,
                    force_refresh=force_refresh,
                    name_lookup=name_lookup,
                    snapshot_loader=snapshot_loader,
                    snapshot_saver=snapshot_saver,
                )
            return _start_crash_scan_job(
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
    items.sort(key=lambda item: item.get("crash_rebound_score") or 0, reverse=True)
    return {
        "items": items,
        "count": len(items),
        "target_date": resolved_target_date,
        "mode": "manual",
        "error": "",
        "crash_scan_result_version": CRASH_SCAN_RESULT_VERSION,
    }


def run_crash_rebound_scan(
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
    candidates: list[dict[str, object]] = []
    _update_crash_scan_job_progress(job_id, processed=0, total=len(all_symbols), candidates_total=0)
    started_at = time.perf_counter()

    for processed, symbol in enumerate(all_symbols, start=1):
        try:
            item = _evaluate_symbol(store, config, symbol, target_date, name_lookup=name_lookup, include_non_candidate=False)
            if item.get("crash_rebound_candidate"):
                candidates.append(item)
        except Exception:
            pass
        finally:
            _update_crash_scan_job_progress(
                job_id,
                processed=processed,
                total=len(all_symbols),
                candidates_total=len(candidates),
            )

    candidates.sort(
        key=lambda item: (
            float(item.get("crash_rebound_score") or 0),
            float(item.get("crash_rebound_rally_pct") or 0),
            float(item.get("crash_rebound_crash_pct") or 0),
        ),
        reverse=True,
    )
    top_candidates = candidates[:CRASH_SCAN_TOP_N]
    return {
        "items": top_candidates,
        "count": len(top_candidates),
        "target_date": target_date,
        "scanned": len(all_symbols),
        "mode": "scan",
        "elapsed": round(time.perf_counter() - started_at, 1),
        "candidates_total": len(candidates),
        "error": "",
        "crash_scan_result_version": CRASH_SCAN_RESULT_VERSION,
    }


def get_crash_scan_status(job_id: str) -> dict:
    with _crash_scan_lock:
        job = _crash_scan_jobs.get(job_id)
    if job is None:
        return {"status": "not_found"}
    if job.get("status") == "done":
        return job["result"]
    if job.get("status") == "error":
        return {
            "status": "error",
            "job_id": job_id,
            "target_date": job.get("target_date"),
            "error": str(job.get("error") or "crash rebound scan failed"),
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
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce")
    daily = daily[daily["trade_date"] <= cutoff].copy()
    if len(daily) < 80:
        return {
            "symbol": symbol,
            "name": _lookup_name(symbol, name_lookup=name_lookup),
            "error": f"仅{len(daily)}根日线",
            "available_days": len(daily),
        }

    result = compute_crash_rebound_quality(daily)
    candidate = bool(result.get("crash_rebound_candidate"))
    if not include_non_candidate and not candidate:
        return {"symbol": symbol, "crash_rebound_candidate": False}

    latest = daily.sort_values("trade_date").iloc[-1]
    return {
        "symbol": symbol,
        "name": _lookup_name(symbol, name_lookup=name_lookup),
        "latest_date": str(pd.Timestamp(latest.get("trade_date")).date()),
        "available_days": len(daily),
        "close": _optional_float(latest.get("close")),
        "error": "",
        **_serialize_crash_result(result),
    }


def _serialize_crash_result(result: dict[str, object]) -> dict[str, object]:
    return {
        "crash_rebound_candidate": bool(result.get("crash_rebound_candidate")),
        "crash_rebound_score": _optional_float(result.get("crash_rebound_score")) or 0.0,
        "crash_rebound_grade": str(result.get("crash_rebound_grade") or "C"),
        "crash_rebound_reason": str(result.get("crash_rebound_reason") or ""),
        "crash_rebound_rally_start_date": str(result.get("crash_rebound_rally_start_date") or ""),
        "crash_rebound_rally_start_price": _optional_float(result.get("crash_rebound_rally_start_price")),
        "crash_rebound_peak_date": str(result.get("crash_rebound_peak_date") or ""),
        "crash_rebound_peak_price": _optional_float(result.get("crash_rebound_peak_price")),
        "crash_rebound_crash_low_date": str(result.get("crash_rebound_crash_low_date") or ""),
        "crash_rebound_crash_low_price": _optional_float(result.get("crash_rebound_crash_low_price")),
        "crash_rebound_rally_pct": _optional_float(result.get("crash_rebound_rally_pct")),
        "crash_rebound_crash_pct": _optional_float(result.get("crash_rebound_crash_pct")),
        "crash_rebound_rally_days": int(result.get("crash_rebound_rally_days") or 0),
        "crash_rebound_crash_days": int(result.get("crash_rebound_crash_days") or 0),
        "crash_rebound_rally_launch_pct": _optional_float(result.get("crash_rebound_rally_launch_pct")),
        "crash_rebound_rally_speed_pct": _optional_float(result.get("crash_rebound_rally_speed_pct")),
        "crash_rebound_rally_smoothness_pct": _optional_float(result.get("crash_rebound_rally_smoothness_pct")),
        "crash_rebound_crash_smoothness_pct": _optional_float(result.get("crash_rebound_crash_smoothness_pct")),
        "crash_rebound_bottom_distance_pct": _optional_float(result.get("crash_rebound_bottom_distance_pct")),
        "crash_rebound_base_start_date": str(result.get("crash_rebound_base_start_date") or ""),
        "crash_rebound_base_end_date": str(result.get("crash_rebound_base_end_date") or ""),
        "crash_rebound_base_days": int(result.get("crash_rebound_base_days") or 0),
        "crash_rebound_base_high": _optional_float(result.get("crash_rebound_base_high")),
        "crash_rebound_base_low": _optional_float(result.get("crash_rebound_base_low")),
        "crash_rebound_base_height_pct": _optional_float(result.get("crash_rebound_base_height_pct")),
        "crash_rebound_limit_price": _optional_float(result.get("crash_rebound_limit_price")),
        "crash_rebound_score_rally": _optional_float(result.get("crash_rebound_score_rally")),
        "crash_rebound_score_crash": _optional_float(result.get("crash_rebound_score_crash")),
        "crash_rebound_score_rally_smoothness": _optional_float(result.get("crash_rebound_score_rally_smoothness")),
        "crash_rebound_score_crash_smoothness": _optional_float(result.get("crash_rebound_score_crash_smoothness")),
        "crash_rebound_score_base": _optional_float(result.get("crash_rebound_score_base")),
    }


def _start_crash_scan_job(store, config: AppConfig, target_date: str, name_lookup=None, snapshot_saver=None) -> dict:
    job_id = str(uuid.uuid4())[:8]
    with _crash_scan_lock:
        _crash_scan_jobs[job_id] = {
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
        _crash_scan_jobs_by_date[target_date] = job_id
    thread = threading.Thread(target=_run_crash_scan_async, args=(job_id, store, config, target_date), daemon=True)
    thread.start()
    return {"mode": "scan", "job_id": job_id, "status": "started", "count": 0, "target_date": target_date, "error": ""}


def _get_or_start_crash_scan_job(
    store,
    config: AppConfig,
    target_date: str,
    force_refresh: bool = False,
    name_lookup=None,
    snapshot_loader=None,
    snapshot_saver=None,
) -> dict:
    with _crash_scan_lock:
        if not force_refresh:
            cached = _crash_scan_result_cache.get(target_date)
            if _is_current_crash_scan_result(cached):
                _fill_names(cached.get("items", []), name_lookup=name_lookup)
                return cached
            existing_job_id = _crash_scan_jobs_by_date.get(target_date)
            if existing_job_id:
                existing_job = _crash_scan_jobs.get(existing_job_id, {})
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
                if existing_job.get("status") == "done" and _is_current_crash_scan_result(existing_job.get("result")):
                    _fill_names(existing_job["result"].get("items", []), name_lookup=name_lookup)
                    return existing_job["result"]
        if not force_refresh and callable(snapshot_loader):
            persisted = snapshot_loader(target_date)
            if _is_current_crash_scan_result(persisted) and persisted.get("items"):
                _fill_names(persisted.get("items", []), name_lookup=name_lookup)
                _crash_scan_result_cache[target_date] = persisted
                return persisted

    return _start_crash_scan_job(
        store,
        config,
        target_date,
        name_lookup=name_lookup,
        snapshot_saver=snapshot_saver,
    )


def _run_crash_scan_async(job_id: str, store, config: AppConfig, target_date: str) -> None:
    try:
        with _crash_scan_lock:
            job = _crash_scan_jobs.get(job_id, {})
            name_lookup = job.get("name_lookup")
            snapshot_saver = job.get("snapshot_saver")
        result = run_crash_rebound_scan(store, config, target_date, name_lookup=name_lookup, job_id=job_id)
        _fill_names(result.get("items", []), name_lookup=name_lookup)
        if callable(snapshot_saver):
            try:
                snapshot_saver(target_date, result)
            except Exception:
                pass
        with _crash_scan_lock:
            _crash_scan_jobs[job_id] = {"status": "done", "result": result, "elapsed": result.get("elapsed", 0)}
            _crash_scan_jobs_by_date[target_date] = job_id
            _crash_scan_result_cache[target_date] = result
    except Exception as exc:
        with _crash_scan_lock:
            _crash_scan_jobs[job_id] = {"status": "error", "error": str(exc)}
            if _crash_scan_jobs_by_date.get(target_date) == job_id:
                _crash_scan_jobs_by_date.pop(target_date, None)


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
    excluded = tuple(str(prefix) for prefix in getattr(config.universe, "excluded_symbol_prefixes", []) or ())
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
    dates = [str(item[1] or "").strip() for item in sym_date_pairs if str(item[1] or "").strip()]
    if not dates:
        return fallback
    unique_dates = list(dict.fromkeys(dates))
    return unique_dates[0] if len(unique_dates) == 1 else "多日期"


def _update_crash_scan_job_progress(
    job_id: str | None,
    *,
    processed: int | None = None,
    total: int | None = None,
    candidates_total: int | None = None,
) -> None:
    if not job_id:
        return
    with _crash_scan_lock:
        job = _crash_scan_jobs.get(job_id)
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
