"""Backtest endpoint: compute continuation scores using only historical data."""
from __future__ import annotations

import threading
import time
import uuid

import pandas as pd

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig

_scan_jobs: dict[str, dict] = {}
_scan_jobs_by_date: dict[str, str] = {}
_scan_result_cache: dict[str, dict] = {}
_scan_lock = threading.Lock()
SCAN_TOP_N = 100
SCAN_RESULT_VERSION = 4


def _is_current_scan_result(payload: dict | None) -> bool:
    return isinstance(payload, dict) and int(payload.get("scan_result_version") or 0) == SCAN_RESULT_VERSION


def run_backtest_for_symbols(
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
    """Run backtest for explicit symbols, or start/reuse a scan for a target date."""
    from winstan.rules.stage2_continuation import compute_continuation_quality

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
        if "." not in symbol:
            if symbol.startswith("6"):
                symbol += ".SH"
            elif symbol.startswith(("0", "3")):
                symbol += ".SZ"
            else:
                symbol += ".BJ"
        sym_date_pairs.append((symbol, dt))

    resolved_target_date = _resolve_display_target_date(sym_date_pairs, fallback=target_date)

    if not sym_date_pairs:
        if target_date:
            if reuse_scan:
                return _get_or_start_scan_job(
                    store,
                    config,
                    target_date,
                    force_refresh=force_refresh,
                    name_lookup=name_lookup,
                    snapshot_loader=snapshot_loader,
                    snapshot_saver=snapshot_saver,
                )
            return _start_scan_job(
                store,
                config,
                target_date,
                name_lookup=name_lookup,
                snapshot_saver=snapshot_saver,
            )
        return {"items": [], "error": "请输入代码和日期", "count": 0}

    items: list[dict[str, object]] = []
    for symbol, dt_str in sym_date_pairs:
        try:
            cutoff = pd.Timestamp(dt_str)
            if pd.isna(cutoff):
                items.append({"symbol": symbol, "error": f"无效日期: {dt_str}"})
                continue

            daily = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
            weekly_cut = pd.DataFrame()
            if not daily.empty:
                daily["trade_date"] = pd.to_datetime(daily["trade_date"])
                daily = daily[daily["trade_date"] <= cutoff].copy()
                if not daily.empty:
                    from winstan.resample.weekly_builder import build_weekly_bars

                    weekly_cut = build_weekly_bars(daily)

            if weekly_cut.empty:
                weekly = clean_daily_bars(store.read_symbol_frame("weekly_bars", symbol))
                if weekly.empty:
                    items.append({"symbol": symbol, "error": "无数据"})
                    continue
                weekly["trade_date"] = pd.to_datetime(weekly["trade_date"])
                weekly_cut = weekly[weekly["trade_date"] <= cutoff].copy()

            if weekly_cut.empty:
                items.append({"symbol": symbol, "error": "无数据"})
                continue
            if len(weekly_cut) < 30:
                items.append({"symbol": symbol, "error": f"仅{len(weekly_cut)}周", "available_weeks": len(weekly_cut)})
                continue

            result = compute_continuation_quality(weekly_cut, config, daily=daily if not daily.empty else None)
            items.append(
                {
                    "symbol": symbol,
                    "name": _lookup_name(symbol, name_lookup=name_lookup),
                    "latest_date": str(weekly_cut["trade_date"].max().date()),
                    "available_weeks": len(weekly_cut),
                    "cont_score_box": float(result.get("cont_score_box") or 0),
                    "cont_quality_score": float(result.get("cont_quality_score") or 0),
                    "cont_quality_grade": str(result.get("cont_quality_grade") or "C"),
                    "cont_is_applicable": bool(result.get("cont_is_applicable")),
                    "cont_prior_trend_ok": bool(result.get("cont_prior_trend_ok")),
                    "cont_pool_b": bool(result.get("cont_pool_b")),
                    "cont_volume_trend_ok": bool(result.get("cont_volume_trend_ok")),
                    "cont_box_range_pct": (
                        float(result.get("cont_box_range_pct") or 0)
                        if result.get("cont_box_range_pct") is not None
                        else None
                    ),
                    "cont_box_duration_weeks": int(result.get("cont_box_duration_weeks") or 0),
                    "cont_quality_reason": str(result.get("cont_quality_reason") or ""),
                    "cont_ma30w_slope_10w": _optional_float(result.get("cont_ma30w_slope_10w")),
                    "cont_box_flatness": _optional_float(result.get("cont_box_flatness")),
                    "cont_box_conv": _optional_float(result.get("cont_box_conv")),
                    "cont_box_vol_low": _optional_float(result.get("cont_box_vol_low")),
                    "cont_box_no_trend": _optional_float(result.get("cont_box_no_trend")),
                    "cont_flatten_duration_weeks": int(result.get("cont_flatten_duration_weeks") or 0),
                    "cont_flatten_score": _optional_float(result.get("cont_flatten_score")),
                    "cont_decline_pct": _optional_float(result.get("cont_decline_pct")),
                    "cont_platform_width_pct": _optional_float(result.get("cont_platform_width_pct")),
                    "cont_platform_width_threshold_pct": _optional_float(result.get("cont_platform_width_threshold_pct")),
                    "cont_lifecycle_phase": str(result.get("cont_lifecycle_phase") or ""),
                    "cont_ema_weekly_change_pct": _optional_float(result.get("cont_ema_weekly_change_pct")),
                    "cont_ema_weekly_change_4w_avg": _optional_float(result.get("cont_ema_weekly_change_4w_avg")),
                    "cont_ema_weekly_change_8w_avg": _optional_float(result.get("cont_ema_weekly_change_8w_avg")),
                    "cont_flatten_shrink_ratio": _optional_float(result.get("cont_flatten_shrink_ratio")),
                    "cont_base_duration_weeks": int(result.get("cont_base_duration_weeks") or 0),
                    "cont_base_maturity_score": _optional_float(result.get("cont_base_maturity_score")),
                    "cont_base_range_stability_score": _optional_float(result.get("cont_base_range_stability_score")),
                    "cont_base_center_drift_pct": _optional_float(result.get("cont_base_center_drift_pct")),
                    "cont_near_trough_weeks": int(result.get("cont_near_trough_weeks") or 0),
                    "cont_long_low_base_ok": bool(result.get("cont_long_low_base_ok")),
                    "error": "",
                }
            )
        except Exception as exc:
            items.append({"symbol": symbol, "error": str(exc)[:120]})

    items.sort(key=lambda item: item.get("cont_score_box") or 0, reverse=True)
    return {
        "items": items,
        "count": len(items),
        "target_date": resolved_target_date,
        "error": "",
        "scan_result_version": SCAN_RESULT_VERSION,
    }


def run_backtest_scan(
    store,
    config: AppConfig,
    target_date: str,
    name_lookup=None,
    job_id: str | None = None,
) -> dict:
    """Run a full-market continuation scan and return the top continuation candidates."""
    from winstan.resample.weekly_builder import build_weekly_bars
    from winstan.rules.stage2_continuation import compute_continuation_quality

    cutoff = pd.Timestamp(target_date)
    if pd.isna(cutoff):
        return {"items": [], "error": f"无效日期: {target_date}", "count": 0}

    all_symbols = store.list_cached_symbols("daily_bars")
    lock = threading.Lock()
    candidates: list[dict[str, object]] = []
    progress = {"processed": 0, "candidates": 0, "total": len(all_symbols)}
    _update_scan_job_progress(job_id, processed=0, total=progress["total"], candidates_total=0)

    def _check(symbol: str) -> None:
        try:
            daily = clean_daily_bars(store.read_symbol_frame("daily_bars", symbol))
            if daily.empty:
                return
            daily["trade_date"] = pd.to_datetime(daily["trade_date"])
            daily = daily[daily["trade_date"] <= cutoff].copy()
            if len(daily) < 200:
                return

            weekly_cut = build_weekly_bars(daily)
            if len(weekly_cut) < 30:
                return

            result = compute_continuation_quality(weekly_cut, config, daily=daily)
            box_score = float(result.get("cont_score_box") or 0)
            if not result.get("cont_is_applicable"):
                return

            final_score = (
                float(result.get("cont_quality_score") or 0) * 0.4
                + (100 - min(float(result.get("rs_rank_pct") or 50), 100)) * 0.3
                + (float(result.get("cont_score_volume") or 0) / 15.0 * 100.0) * 0.2
                + float(result.get("headroom_pct") or 0) * 0.1
            )

            with lock:
                candidates.append(
                    {
                        "symbol": symbol,
                        "name": _lookup_name(symbol, name_lookup=name_lookup),
                        "cont_score_box": box_score,
                        "cont_quality_score": float(result.get("cont_quality_score") or 0),
                        "cont_quality_grade": str(result.get("cont_quality_grade") or "C"),
                        "cont_is_applicable": True,
                        "cont_prior_trend_ok": bool(result.get("cont_prior_trend_ok")),
                        "cont_pool_b": bool(result.get("cont_pool_b")),
                        "cont_volume_trend_ok": bool(result.get("cont_volume_trend_ok")),
                        "cont_box_range_pct": (
                            float(result.get("cont_box_range_pct") or 0)
                            if result.get("cont_box_range_pct") is not None
                            else None
                        ),
                        "cont_box_duration_weeks": int(result.get("cont_box_duration_weeks") or 0),
                        "cont_quality_reason": str(result.get("cont_quality_reason") or ""),
                        "cont_box_flatness": _optional_float(result.get("cont_box_flatness")),
                        "cont_flatten_duration_weeks": int(result.get("cont_flatten_duration_weeks") or 0),
                        "cont_flatten_score": _optional_float(result.get("cont_flatten_score")),
                        "cont_decline_pct": _optional_float(result.get("cont_decline_pct")),
                        "cont_platform_width_pct": _optional_float(result.get("cont_platform_width_pct")),
                        "cont_platform_width_threshold_pct": _optional_float(result.get("cont_platform_width_threshold_pct")),
                        "cont_lifecycle_phase": str(result.get("cont_lifecycle_phase") or ""),
                        "cont_ema_weekly_change_pct": _optional_float(result.get("cont_ema_weekly_change_pct")),
                        "cont_ema_weekly_change_4w_avg": _optional_float(result.get("cont_ema_weekly_change_4w_avg")),
                        "cont_ema_weekly_change_8w_avg": _optional_float(result.get("cont_ema_weekly_change_8w_avg")),
                        "cont_flatten_shrink_ratio": _optional_float(result.get("cont_flatten_shrink_ratio")),
                        "cont_base_duration_weeks": int(result.get("cont_base_duration_weeks") or 0),
                        "cont_base_maturity_score": _optional_float(result.get("cont_base_maturity_score")),
                        "cont_near_trough_weeks": int(result.get("cont_near_trough_weeks") or 0),
                        "cont_long_low_base_ok": bool(result.get("cont_long_low_base_ok")),
                        "final_score": round(final_score, 1),
                        "latest_date": str(weekly_cut["trade_date"].max().date()),
                        "available_weeks": len(weekly_cut),
                        "error": "",
                    }
                )
                progress["candidates"] = len(candidates)
                progress_snapshot = progress.copy()
            _update_scan_job_progress(
                job_id,
                processed=progress_snapshot["processed"],
                total=progress_snapshot["total"],
                candidates_total=progress_snapshot["candidates"],
            )
        except Exception:
            return
        finally:
            with lock:
                progress["processed"] += 1
                progress_snapshot = progress.copy()
            _update_scan_job_progress(
                job_id,
                processed=progress_snapshot["processed"],
                total=progress_snapshot["total"],
                candidates_total=progress_snapshot["candidates"],
            )

    started_at = time.perf_counter()
    for symbol in all_symbols:
        _check(symbol)

    candidates.sort(key=lambda item: item.get("cont_score_box", 0), reverse=True)
    top_candidates = candidates[:SCAN_TOP_N]
    return {
        "items": top_candidates,
        "count": len(top_candidates),
        "target_date": target_date,
        "scanned": len(all_symbols),
        "mode": "scan",
        "elapsed": round(time.perf_counter() - started_at, 1),
        "candidates_total": len(candidates),
        "error": "",
        "scan_result_version": SCAN_RESULT_VERSION,
    }


def run_backtest_scan_with_names(store, config: AppConfig, target_date: str, name_lookup=None) -> dict:
    result = run_backtest_scan(store, config, target_date, name_lookup=name_lookup)
    _fill_names(result.get("items", []), name_lookup=name_lookup)
    return result


def get_scan_status(job_id: str) -> dict:
    """Return async scan status or finished payload."""
    with _scan_lock:
        job = _scan_jobs.get(job_id)
    if job is None:
        return {"status": "not_found"}
    if job.get("status") == "done":
        return job["result"]
    if job.get("status") == "error":
        return {
            "status": "error",
            "job_id": job_id,
            "target_date": job.get("target_date"),
            "error": str(job.get("error") or "scan failed"),
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


def _start_scan_job(store, config: AppConfig, target_date: str, name_lookup=None, snapshot_saver=None) -> dict:
    job_id = str(uuid.uuid4())[:8]
    with _scan_lock:
        _scan_jobs[job_id] = {
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
    thread = threading.Thread(target=_run_scan_async, args=(job_id, store, config, target_date), daemon=True)
    thread.start()
    return {"mode": "scan", "job_id": job_id, "status": "started", "count": 0, "target_date": target_date, "error": ""}


def _get_or_start_scan_job(
    store,
    config: AppConfig,
    target_date: str,
    force_refresh: bool = False,
    name_lookup=None,
    snapshot_loader=None,
    snapshot_saver=None,
) -> dict:
    with _scan_lock:
        if not force_refresh:
            cached = _scan_result_cache.get(target_date)
            if _is_current_scan_result(cached):
                _fill_names(cached.get("items", []), name_lookup=name_lookup)
                return cached
            if cached is not None:
                _scan_result_cache.pop(target_date, None)

            existing_job_id = _scan_jobs_by_date.get(target_date)
            if existing_job_id:
                existing_job = _scan_jobs.get(existing_job_id, {})
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
                if existing_job.get("status") == "done" and _is_current_scan_result(existing_job.get("result")):
                    _fill_names(existing_job["result"].get("items", []), name_lookup=name_lookup)
                    return existing_job["result"]

        if not force_refresh and callable(snapshot_loader):
            persisted = snapshot_loader(target_date)
            if _is_current_scan_result(persisted) and persisted.get("items"):
                _fill_names(persisted.get("items", []), name_lookup=name_lookup)
                _scan_result_cache[target_date] = persisted
                return persisted

        job_id = str(uuid.uuid4())[:8]
        _scan_jobs[job_id] = {
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
        _scan_jobs_by_date[target_date] = job_id

    thread = threading.Thread(target=_run_scan_async, args=(job_id, store, config, target_date), daemon=True)
    thread.start()
    return {"mode": "scan", "job_id": job_id, "status": "started", "count": 0, "target_date": target_date, "error": ""}


def _run_scan_async(job_id: str, store, config: AppConfig, target_date: str) -> None:
    try:
        name_lookup = None
        snapshot_saver = None
        with _scan_lock:
            job = _scan_jobs.get(job_id, {})
            name_lookup = job.get("name_lookup")
            snapshot_saver = job.get("snapshot_saver")

        result = run_backtest_scan(
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
        with _scan_lock:
            _scan_jobs[job_id] = {"status": "done", "result": result, "elapsed": result.get("elapsed", 0)}
            _scan_jobs_by_date[target_date] = job_id
            _scan_result_cache[target_date] = result
    except Exception as exc:
        with _scan_lock:
            _scan_jobs[job_id] = {"status": "error", "error": str(exc)}
            if _scan_jobs_by_date.get(target_date) == job_id:
                _scan_jobs_by_date.pop(target_date, None)


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


def _update_scan_job_progress(
    job_id: str | None,
    *,
    processed: int | None = None,
    total: int | None = None,
    candidates_total: int | None = None,
) -> None:
    if not job_id:
        return
    with _scan_lock:
        job = _scan_jobs.get(job_id)
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
