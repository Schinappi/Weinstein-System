"""Daily-bar detector for sharp-rally followed by sharp-crash rebound setups."""
from __future__ import annotations

import math

import pandas as pd


LOOKBACK_BARS = 320
MIN_RALLY_BARS = 10
MAX_RALLY_BARS = 130
MIN_CRASH_BARS = 10
MAX_CRASH_BARS = 150
MIN_RALLY_PCT = 80.0
MIN_CRASH_PCT = 40.0
MAX_DISTANCE_FROM_CRASH_LOW_PCT = 12.0
RALLY_PIVOT_LOOKBACK = 3
RALLY_PIVOT_LOOKFORWARD = 2
RALLY_LAUNCH_BARS = 5
MIN_RALLY_LAUNCH_PCT = 8.0
RALLY_SPEED_FULL_SCORE_PCT = 5.0
PRE_RALLY_BASE_BARS = 22
MIN_PRE_RALLY_BASE_BARS = 15
MIN_PRE_RALLY_BASE_HEIGHT_PCT = 5.0
MAX_PRE_RALLY_BASE_HEIGHT_PCT = 35.0


def compute_crash_rebound_quality(daily_bars: pd.DataFrame) -> dict[str, object]:
    """Score a rapid low-to-peak rally followed by a smooth severe selloff.

    The target day is the final daily bar.  A signal remains actionable only
    while its close is still close to the selloff low, rather than after a
    material rebound has already happened.
    """
    frame = _prepare_daily_bars(daily_bars)
    if len(frame) < MIN_RALLY_BARS + MIN_CRASH_BARS + 1:
        return _empty_result("日线样本不足")

    window = frame.tail(LOOKBACK_BARS).reset_index(drop=True)
    pattern = _find_best_pattern(window)
    if pattern is None:
        return _empty_result("未识别到先急涨后急跌结构")

    rally_pct = pattern["rally_pct"]
    crash_pct = pattern["crash_pct"]
    rally_smoothness = pattern["rally_smoothness"]
    crash_smoothness = pattern["crash_smoothness"]
    bottom_distance_pct = pattern["bottom_distance_pct"]
    base_score = pattern["base_score"]

    rally_score = min(35.0, rally_pct / 150.0 * 35.0)
    crash_score = min(35.0, crash_pct / 80.0 * 35.0)
    rally_smooth_score = rally_smoothness * 15.0
    crash_smooth_score = crash_smoothness * 15.0
    total_score = min(100.0, rally_score + crash_score + rally_smooth_score + crash_smooth_score + base_score)

    candidate = (
        rally_pct >= MIN_RALLY_PCT
        and crash_pct >= MIN_CRASH_PCT
        and bottom_distance_pct <= MAX_DISTANCE_FROM_CRASH_LOW_PCT
    )
    reason = (
        f"上涨 {rally_pct:.1f}%/{pattern['rally_days']}日，流畅度 {rally_smoothness * 100:.0f}%；"
        f"下跌 {crash_pct:.1f}%/{pattern['crash_days']}日，流畅度 {crash_smoothness * 100:.0f}%；"
        f"现价距跌后低点 {bottom_distance_pct:.1f}%"
    )
    if not candidate:
        failed = []
        if rally_pct < MIN_RALLY_PCT:
            failed.append(f"上涨不足{MIN_RALLY_PCT:.0f}%")
        if crash_pct < MIN_CRASH_PCT:
            failed.append(f"下跌不足{MIN_CRASH_PCT:.0f}%")
        if bottom_distance_pct > MAX_DISTANCE_FROM_CRASH_LOW_PCT:
            failed.append(f"已离跌后低点{bottom_distance_pct:.1f}%")
        reason = f"{reason}；未命中：{'、'.join(failed)}"

    return {
        "crash_rebound_candidate": candidate,
        "crash_rebound_score": round(total_score, 1),
        "crash_rebound_grade": _score_grade(total_score),
        "crash_rebound_reason": reason,
        "crash_rebound_rally_start_date": _format_date(pattern["rally_start_date"]),
        "crash_rebound_rally_start_price": pattern["rally_start_price"],
        "crash_rebound_peak_date": _format_date(pattern["peak_date"]),
        "crash_rebound_peak_price": pattern["peak_price"],
        "crash_rebound_crash_low_date": _format_date(pattern["crash_low_date"]),
        "crash_rebound_crash_low_price": pattern["crash_low_price"],
        "crash_rebound_rally_pct": round(rally_pct, 2),
        "crash_rebound_crash_pct": round(crash_pct, 2),
        "crash_rebound_rally_days": int(pattern["rally_days"]),
        "crash_rebound_crash_days": int(pattern["crash_days"]),
        "crash_rebound_rally_launch_pct": round(pattern["rally_launch_pct"], 2),
        "crash_rebound_rally_speed_pct": round(pattern["rally_speed_pct"], 2),
        "crash_rebound_rally_smoothness_pct": round(rally_smoothness * 100.0, 2),
        "crash_rebound_crash_smoothness_pct": round(crash_smoothness * 100.0, 2),
        "crash_rebound_bottom_distance_pct": round(bottom_distance_pct, 2),
        "crash_rebound_base_start_date": _format_date(pattern["base_start_date"]),
        "crash_rebound_base_end_date": _format_date(pattern["base_end_date"]),
        "crash_rebound_base_days": int(pattern["base_days"]),
        "crash_rebound_base_high": pattern["base_high"],
        "crash_rebound_base_low": pattern["base_low"],
        "crash_rebound_base_height_pct": round(pattern["base_height_pct"], 2),
        "crash_rebound_limit_price": pattern["base_high"],
        "crash_rebound_score_rally": round(rally_score, 1),
        "crash_rebound_score_crash": round(crash_score, 1),
        "crash_rebound_score_rally_smoothness": round(rally_smooth_score, 1),
        "crash_rebound_score_crash_smoothness": round(crash_smooth_score, 1),
        "crash_rebound_score_base": round(base_score, 1),
    }


def _find_best_pattern(frame: pd.DataFrame) -> dict[str, object] | None:
    best: dict[str, object] | None = None
    bar_count = len(frame)
    for peak_index in range(MIN_RALLY_BARS, bar_count - MIN_CRASH_BARS):
        prior_start = max(0, peak_index - MAX_RALLY_BARS)
        prior_end = peak_index - MIN_RALLY_BARS
        local_peak = frame.iloc[max(0, peak_index - 5) : min(bar_count, peak_index + 6)]["high"].max()
        peak_price = float(frame.at[peak_index, "high"])
        if peak_price < float(local_peak):
            continue

        crash_start = peak_index + MIN_CRASH_BARS
        crash_end = min(bar_count - 1, peak_index + MAX_CRASH_BARS)
        crash_window = frame.iloc[crash_start : crash_end + 1]
        if crash_window.empty:
            continue
        crash_low_index = int(crash_window["low"].idxmin())
        crash_days = crash_low_index - peak_index
        if crash_days < MIN_CRASH_BARS or crash_days > MAX_CRASH_BARS:
            continue
        crash_low_price = float(frame.at[crash_low_index, "low"])
        if crash_low_price <= 0:
            continue
        crash_pct = (1.0 - crash_low_price / peak_price) * 100.0
        if crash_pct < MIN_CRASH_PCT:
            continue

        latest_close = float(frame.iloc[-1]["close"])
        bottom_distance_pct = (latest_close / crash_low_price - 1.0) * 100.0
        if bottom_distance_pct < -0.01:
            continue

        rally = _find_main_rally_leg(
            frame,
            peak_index=peak_index,
            start_index=prior_start,
            end_index=prior_end,
            peak_price=peak_price,
        )
        if rally is None:
            continue

        rally_start_index = int(rally["start_index"])
        rally_start_price = float(rally["start_price"])
        rally_pct = float(rally["rally_pct"])
        rally_days = int(rally["rally_days"])
        rally_smoothness = float(rally["rally_smoothness"])
        crash_smoothness = _leg_smoothness(frame.iloc[peak_index : crash_low_index + 1]["close"], direction=-1)
        base = _find_pre_rally_base(frame, rally_start_index)
        base_score = float(base["score"]) if base is not None else 0.0
        selection_score = (
            min(35.0, rally_pct / 150.0 * 35.0)
            + min(35.0, crash_pct / 80.0 * 35.0)
            + rally_smoothness * 15.0
            + crash_smoothness * 15.0
            + base_score
        )
        candidate = {
            "selection_score": selection_score,
            "rally_start_date": frame.at[rally_start_index, "trade_date"],
            "rally_start_price": rally_start_price,
            "peak_date": frame.at[peak_index, "trade_date"],
            "peak_price": peak_price,
            "crash_low_date": frame.at[crash_low_index, "trade_date"],
            "crash_low_price": crash_low_price,
            "rally_pct": rally_pct,
            "crash_pct": crash_pct,
            "rally_days": rally_days,
            "crash_days": crash_days,
            "rally_launch_pct": float(rally["rally_launch_pct"]),
            "rally_speed_pct": float(rally["rally_speed_pct"]),
            "rally_smoothness": rally_smoothness,
            "crash_smoothness": crash_smoothness,
            "bottom_distance_pct": bottom_distance_pct,
            "base_start_date": base["start_date"] if base else None,
            "base_end_date": base["end_date"] if base else None,
            "base_days": int(base["days"]) if base else 0,
            "base_high": float(base["high"]) if base else None,
            "base_low": float(base["low"]) if base else None,
            "base_height_pct": float(base["height_pct"]) if base else 0.0,
            "base_score": base_score,
        }
        if best is None or candidate["selection_score"] > best["selection_score"]:
            best = candidate
    return best


def _find_main_rally_leg(
    frame: pd.DataFrame,
    *,
    peak_index: int,
    start_index: int,
    end_index: int,
    peak_price: float,
) -> dict[str, float | int] | None:
    """Choose the actual launch leg instead of joining a distant low to the peak."""
    best: dict[str, float | int] | None = None
    for candidate_index in range(start_index, end_index + 1):
        rally_days = peak_index - candidate_index
        if rally_days < MIN_RALLY_BARS or rally_days > MAX_RALLY_BARS:
            continue
        if candidate_index + RALLY_LAUNCH_BARS > peak_index:
            continue
        if not _is_local_low(frame, candidate_index):
            continue

        start_price = float(frame.at[candidate_index, "low"])
        start_close = float(frame.at[candidate_index, "close"])
        launch_close = float(frame.at[candidate_index + RALLY_LAUNCH_BARS, "close"])
        if start_price <= 0 or start_close <= 0 or launch_close <= 0:
            continue

        rally_pct = (peak_price / start_price - 1.0) * 100.0
        if rally_pct < MIN_RALLY_PCT:
            continue
        launch_pct = (launch_close / start_close - 1.0) * 100.0
        if launch_pct < MIN_RALLY_LAUNCH_PCT:
            continue

        rally_smoothness = _leg_smoothness(frame.iloc[candidate_index : peak_index + 1]["close"], direction=1)
        rally_speed_pct = math.log(peak_price / start_price) / rally_days * 100.0
        candidate = {
            "selection_score": _main_rally_selection_score(
                rally_pct=rally_pct,
                rally_smoothness=rally_smoothness,
                rally_speed_pct=rally_speed_pct,
            ),
            "start_index": candidate_index,
            "start_price": start_price,
            "rally_pct": rally_pct,
            "rally_days": rally_days,
            "rally_launch_pct": launch_pct,
            "rally_speed_pct": rally_speed_pct,
            "rally_smoothness": rally_smoothness,
        }
        if best is None or candidate["selection_score"] > best["selection_score"]:
            best = candidate
    return best


def _is_local_low(frame: pd.DataFrame, index: int) -> bool:
    left = max(0, index - RALLY_PIVOT_LOOKBACK)
    right = min(len(frame) - 1, index + RALLY_PIVOT_LOOKFORWARD)
    current_low = float(frame.at[index, "low"])
    nearby_low = float(frame.iloc[left : right + 1]["low"].min())
    return current_low <= nearby_low


def _main_rally_selection_score(*, rally_pct: float, rally_smoothness: float, rally_speed_pct: float) -> float:
    """Balance minimum size with speed and continuity to identify a main surge."""
    amplitude_score = min(1.0, max(0.0, (rally_pct - MIN_RALLY_PCT) / 170.0))
    speed_score = min(1.0, max(0.0, rally_speed_pct / RALLY_SPEED_FULL_SCORE_PCT))
    return 0.20 * amplitude_score + 0.45 * rally_smoothness + 0.35 * speed_score


def _find_pre_rally_base(frame: pd.DataFrame, rally_start_index: int) -> dict[str, float | int | object] | None:
    """Describe the short consolidation immediately preceding a selected main rally."""
    base_start_index = max(0, rally_start_index - (PRE_RALLY_BASE_BARS - 1))
    base = frame.iloc[base_start_index : rally_start_index + 1]
    if len(base) < MIN_PRE_RALLY_BASE_BARS:
        return None

    base_high = float(base["high"].max())
    base_low = float(base["low"].min())
    if base_high <= 0 or base_low <= 0:
        return None
    height_pct = (base_high / base_low - 1.0) * 100.0
    if not MIN_PRE_RALLY_BASE_HEIGHT_PCT <= height_pct <= MAX_PRE_RALLY_BASE_HEIGHT_PCT:
        return None

    duration_score = min(4.0, len(base) / PRE_RALLY_BASE_BARS * 4.0)
    range_score = 4.0
    breakout_score = 2.0
    return {
        "start_date": frame.at[base_start_index, "trade_date"],
        "end_date": frame.at[rally_start_index, "trade_date"],
        "days": len(base),
        "high": base_high,
        "low": base_low,
        "height_pct": height_pct,
        "score": duration_score + range_score + breakout_score,
    }


def _leg_smoothness(closes: pd.Series, direction: int) -> float:
    values = pd.to_numeric(closes, errors="coerce").dropna().astype(float)
    if len(values) < 2 or (values <= 0).any():
        return 0.0
    log_returns = values.map(math.log).diff().dropna()
    if log_returns.empty:
        return 0.0
    directional_ratio = float((log_returns * direction > 0).mean())
    net_move = abs(float(math.log(values.iloc[-1] / values.iloc[0])))
    path_move = float(log_returns.abs().sum())
    efficiency = min(1.0, net_move / path_move) if path_move > 0 else 0.0
    return max(0.0, min(1.0, 0.65 * efficiency + 0.35 * directional_ratio))


def _prepare_daily_bars(daily_bars: pd.DataFrame) -> pd.DataFrame:
    if daily_bars.empty:
        return pd.DataFrame(columns=["trade_date", "high", "low", "close"])
    required = {"trade_date", "high", "low", "close"}
    if not required.issubset(daily_bars.columns):
        return pd.DataFrame(columns=["trade_date", "high", "low", "close"])
    frame = daily_bars[["trade_date", "high", "low", "close"]].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    for column in ["high", "low", "close"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["trade_date", "high", "low", "close"]).sort_values("trade_date").reset_index(drop=True)


def _empty_result(reason: str) -> dict[str, object]:
    return {
        "crash_rebound_candidate": False,
        "crash_rebound_score": 0.0,
        "crash_rebound_grade": "C",
        "crash_rebound_reason": reason,
    }


def _score_grade(score: float) -> str:
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    return "C"


def _format_date(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")
