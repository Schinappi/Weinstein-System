"""Backtest endpoint: compute continuation scores using only historical data."""
from __future__ import annotations

import pandas as pd
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig

# ── 异步扫描任务管理 ──
_scan_jobs: dict[str, dict] = {}
_scan_lock = threading.Lock()


def run_backtest_for_symbols(
    store,
    config: AppConfig,
    symbols_str: str,
    target_date: str,
) -> dict:
    """回测：只用 target_date 之前的数据计算续涨结构分"""
    from winstan.rules.stage2_continuation import compute_continuation_quality

    lines = [l.strip() for l in symbols_str.replace("\n", "\n").split("\n") if l.strip()]
    sym_date_pairs = []
    for line in lines:
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            sym = parts[0].strip().upper(); dt = parts[1].strip()
        elif target_date:
            sym = parts[0].strip().upper() if parts else ""; dt = target_date
        else:
            continue
        if sym:
            if "." not in sym:
                sym += ".SH" if sym.startswith("6") else ".SZ" if sym.startswith(("0", "3")) else ".BJ"
            sym_date_pairs.append((sym, dt))

    if not sym_date_pairs:
        if target_date:
            # 异步启动扫描，立即返回 job_id
            job_id = str(uuid.uuid4())[:8]
            with _scan_lock:
                _scan_jobs[job_id] = {"status": "running", "started_at": time.time(), "result": None}
            t = threading.Thread(target=_run_scan_async, args=(job_id, store, config, target_date), daemon=True)
            t.start()
            return {"mode": "scan", "job_id": job_id, "status": "started", "count": 0, "error": ""}
        return {"items": [], "error": "请输入代码和日期", "count": 0}

    items = []
    for sym, dt_str in sym_date_pairs:
        try:
            cutoff = pd.Timestamp(dt_str)
            if pd.isna(cutoff):
                items.append({"symbol": sym, "error": f"无效日期: {dt_str}"}); continue
            w = clean_daily_bars(store.read_symbol_frame("weekly_bars", sym))
            w["trade_date"] = pd.to_datetime(w["trade_date"])
            t = w[w["trade_date"] <= cutoff].copy()
            if len(t) < 30:
                d = clean_daily_bars(store.read_symbol_frame("daily_bars", sym))
                if not d.empty:
                    d["trade_date"] = pd.to_datetime(d["trade_date"])
                    from winstan.resample.weekly_builder import build_weekly_bars
                    dw = build_weekly_bars(d); t = dw[dw["trade_date"] <= cutoff].copy()
            if t.empty: items.append({"symbol": sym, "error": "无数据"}); continue
            if len(t) < 30: items.append({"symbol": sym, "error": f"仅{len(t)}周", "available_weeks": len(t)}); continue
            d = clean_daily_bars(store.read_symbol_frame("daily_bars", sym))
            if not d.empty: d["trade_date"] = pd.to_datetime(d["trade_date"]); d = d[d["trade_date"] <= cutoff].copy()
            r = compute_continuation_quality(t, config, daily=d if not d.empty else None)
            items.append({
                "symbol": sym, "name": _lookup_name(store, sym),
                "latest_date": str(t["trade_date"].max().date()), "available_weeks": len(t),
                "cont_score_box": float(r.get("cont_score_box") or 0),
                "cont_quality_score": float(r.get("cont_quality_score") or 0),
                "cont_quality_grade": str(r.get("cont_quality_grade") or "C"),
                "cont_is_applicable": bool(r.get("cont_is_applicable")),
                "cont_prior_trend_ok": bool(r.get("cont_prior_trend_ok")),
                "cont_pool_b": bool(r.get("cont_pool_b")),
                "cont_volume_trend_ok": bool(r.get("cont_volume_trend_ok")),
                "cont_box_range_pct": float(r.get("cont_box_range_pct") or 0) if r.get("cont_box_range_pct") is not None else None,
                "cont_box_duration_weeks": int(r.get("cont_box_duration_weeks") or 0),
                "cont_quality_reason": str(r.get("cont_quality_reason") or ""),
                "cont_ma30w_slope_10w": float(r.get("cont_ma30w_slope_10w") or 0) if r.get("cont_ma30w_slope_10w") is not None else None,
                "cont_box_flatness": float(r.get("cont_box_flatness") or 0) if r.get("cont_box_flatness") is not None else None,
                "cont_box_conv": float(r.get("cont_box_conv") or 0) if r.get("cont_box_conv") is not None else None,
                "cont_box_vol_low": float(r.get("cont_box_vol_low") or 0) if r.get("cont_box_vol_low") is not None else None,
                "cont_box_no_trend": float(r.get("cont_box_no_trend") or 0) if r.get("cont_box_no_trend") is not None else None,
                "error": "",
            })
        except Exception as exc:
            items.append({"symbol": sym, "error": str(exc)[:120]})
    items.sort(key=lambda x: x.get("cont_score_box") or 0, reverse=True)
    return {"items": items, "count": len(items), "target_date": target_date, "error": ""}


def run_backtest_scan(store, config: AppConfig, target_date: str) -> dict:
    """全市场扫描：8线程返回续涨TOP50"""
    from winstan.rules.stage2_continuation import compute_continuation_quality

    cutoff = pd.Timestamp(target_date)
    if pd.isna(cutoff):
        return {"items": [], "error": f"无效日期: {target_date}", "count": 0}
    all_syms = store.list_cached_symbols("weekly_bars")
    lock = threading.Lock()
    candidates = []

    def _check(sym):
        try:
            w = clean_daily_bars(store.read_symbol_frame("weekly_bars", sym))
            w["trade_date"] = pd.to_datetime(w["trade_date"]); t = w[w["trade_date"] <= cutoff].copy()
            if len(t) < 30: return
            r = compute_continuation_quality(t, config)  # 纯周线，跳过日线加速
            if not r["cont_is_applicable"] or r.get("cont_score_box", 0) <= 0: return
            fs = (float(r["cont_quality_score"]) * 0.4
                  + (100 - min(float(r.get("rs_rank_pct") or 50), 100)) * 0.3
                  + (float(r["cont_score_volume"]) / 15 * 100) * 0.2
                  + (float(r.get("headroom_pct") or 0)) * 0.1)
            with lock:
                candidates.append({
                    "symbol": sym, "name": _lookup_name(store, sym),
                    "cont_score_box": float(r.get("cont_score_box") or 0),
                    "cont_quality_score": float(r.get("cont_quality_score") or 0),
                    "cont_is_applicable": True,
                    "cont_prior_trend_ok": bool(r.get("cont_prior_trend_ok")),
                    "cont_pool_b": bool(r.get("cont_pool_b")),
                    "cont_volume_trend_ok": bool(r.get("cont_volume_trend_ok")),
                    "cont_box_range_pct": float(r.get("cont_box_range_pct") or 0) if r.get("cont_box_range_pct") else None,
                    "cont_box_duration_weeks": int(r.get("cont_box_duration_weeks") or 0),
                    "cont_quality_reason": str(r.get("cont_quality_reason") or ""),
                    "final_score": round(fs, 1), "error": "",
                    "latest_date": str(t["trade_date"].max().date()), "available_weeks": len(t),
                })
        except Exception:
            pass

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(_check, all_syms))
    candidates.sort(key=lambda x: x.get("cont_score_box", 0), reverse=True)
    return {"items": candidates[:50], "count": min(len(candidates), 50), "target_date": target_date,
            "scanned": len(all_syms), "mode": "scan",
            "elapsed": round(time.perf_counter() - t0, 1),
            "candidates_total": len(candidates), "error": ""}


def _run_scan_async(job_id: str, store, config, target_date: str) -> None:
    """后台运行全市场扫描，完成后存入 _scan_jobs"""
    try:
        result = run_backtest_scan(store, config, target_date)
        with _scan_lock:
            _scan_jobs[job_id] = {"status": "done", "result": result, "elapsed": result.get("elapsed", 0)}
    except Exception as e:
        with _scan_lock:
            _scan_jobs[job_id] = {"status": "error", "error": str(e)}


def get_scan_status(job_id: str) -> dict:
    """轮询扫描任务状态"""
    with _scan_lock:
        job = _scan_jobs.get(job_id)
    if job is None:
        return {"status": "not_found"}
    if job["status"] == "done":
        return job["result"]
    return {"status": job["status"], "job_id": job_id}


def _lookup_name(store, symbol: str) -> str:
    try:
        d = store.read_symbol_frame("weekly_bars", symbol)
        return ""
    except Exception:
        return ""
