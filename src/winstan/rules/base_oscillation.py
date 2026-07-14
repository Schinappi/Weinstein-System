"""Large-base oscillation quality scoring.

This module scores the pattern the user is actually looking for:

* a long, visible base;
* repeated support at the bottom;
* meaningful swings from bottom toward the top;
* reasonably stable top resistance;
* enough completed bottom-to-top-to-bottom cycles.

It deliberately avoids the old "future 5 weeks rebound" shortcut.  For each
support touch, swing is measured until the next support touch, which better
captures large boxes that need 10-20 weeks to move from bottom to top.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


LOOKBACK_WEEKS = 52
LOOKBACK_DAYS = 252
PIVOT_RADIUS = 2
SUPPORT_TOLERANCE_PCT = 1.5
VOLUME_LOOKBACK_WEEKS = 20
DAILY_TOUCH_MERGE_GAP_BARS = 10
WEEKLY_TOUCH_MERGE_GAP_BARS = 1
MIN_VALID_TOUCHES = 3
MIN_VALID_SWING_CYCLES = 1
MIN_CANDIDATE_SCORE = 70.0
MIN_CANDIDATE_AVG_SWING_PCT = 15.0
MAX_CANDIDATE_TOP_STABILITY_PCT = 25.0
MIN_CANDIDATE_REBOUND_EFFICIENCY = 0.65


@dataclass(frozen=True)
class SupportTouch:
    index: int
    date: object
    low: float
    penetration_pct: float
    volume_ratio: float | None


@dataclass(frozen=True)
class SwingCycle:
    touch_index: int
    next_touch_index: int
    peak_index: int
    touch_low: float
    peak_price: float
    swing_pct: float
    rebound_efficiency: float | None


def compute_base_oscillation_quality(
    recent: pd.DataFrame,
    daily: pd.DataFrame | None = None,
) -> dict[str, object]:
    default = _default_result("insufficient data")
    source = daily if daily is not None and not daily.empty else recent
    if source.empty or not {"low", "high", "close"}.issubset(source.columns):
        return default

    bars = source.sort_values("trade_date").reset_index(drop=True).copy()
    for col in ["low", "high", "close", "volume"]:
        if col in bars.columns:
            bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["low", "high", "close"]).reset_index(drop=True)
    if len(bars) < max(16, PIVOT_RADIUS * 2 + 3):
        return default

    lookback = LOOKBACK_DAYS if daily is not None and not daily.empty else LOOKBACK_WEEKS
    source_label = "daily" if daily is not None and not daily.empty else "weekly"
    window_start = max(0, len(bars) - lookback)
    window = bars.iloc[window_start:].reset_index(drop=False).rename(columns={"index": "_abs_idx"})
    pivots = _find_pivot_lows(window)
    if len(pivots) < 2:
        pivots = _fallback_low_candidates(window)
    if len(pivots) < 2:
        return _default_result("not enough bottom pivots")

    clusters = _cluster_prices(pivots, SUPPORT_TOLERANCE_PCT)
    if not clusters:
        return _default_result("no bottom cluster")

    support_cluster = _select_best_cluster(clusters, len(bars))
    support = float(np.median([item["price"] for item in support_cluster]))
    if support <= 0:
        return _default_result("invalid support")

    zone_lower = support * (1.0 - SUPPORT_TOLERANCE_PCT / 100.0)
    zone_upper = support * (1.0 + SUPPORT_TOLERANCE_PCT / 100.0)
    merge_gap = DAILY_TOUCH_MERGE_GAP_BARS if source_label == "daily" else WEEKLY_TOUCH_MERGE_GAP_BARS
    touches = _build_support_touches(bars, window_start, support, zone_lower, zone_upper, merge_gap)
    touch_count = len(touches)
    if touch_count == 0:
        return _default_result("no support touches")

    preliminary_cycles = _build_swing_cycles(bars, touches, box_top=None)
    peak_prices = [cycle.peak_price for cycle in preliminary_cycles]
    box_top = _estimate_box_top(bars, peak_prices, support)
    cycles = _build_swing_cycles(bars, touches, box_top=box_top)

    swing_values = [cycle.swing_pct for cycle in cycles]
    avg_swing = _mean(swing_values)
    swing_count = len(cycles)
    penetration_values = [touch.penetration_pct for touch in touches]
    avg_penetration = _mean(penetration_values)
    max_penetration = max(penetration_values) if penetration_values else None
    duration_weeks = _touch_span_weeks(touches)

    box_height_pct = None
    if box_top is not None and box_top > support:
        box_height_pct = (box_top / support - 1.0) * 100.0

    top_stability_pct = _top_stability_pct(peak_prices)
    rebound_efficiencies = [cycle.rebound_efficiency for cycle in cycles if cycle.rebound_efficiency is not None]
    avg_efficiency = _mean(rebound_efficiencies)
    utilization_pct = None
    if avg_swing is not None and box_height_pct and box_height_pct > 0:
        utilization_pct = min(avg_swing / box_height_pct * 100.0, 140.0)

    volume_contraction_ratio = _volume_contraction_ratio(bars, touches)

    touch_score = _score_touch_count(touch_count)
    penetration_score = _score_penetration(avg_penetration)
    swing_score = _score_avg_swing(avg_swing)
    cycle_score = _score_cycle_count(swing_count)
    top_score = _score_top_stability(top_stability_pct)
    duration_score = _score_duration(duration_weeks)
    volume_score = _score_volume_contraction(volume_contraction_ratio)

    total = min(
        100.0,
        touch_score
        + penetration_score
        + swing_score
        + cycle_score
        + top_score
        + duration_score
        + volume_score,
    )
    grade = _grade(total)
    latest_touch = touches[-1]
    pending_count = max(touch_count - swing_count, 0)
    volume_ratios = [touch.volume_ratio for touch in touches if touch.volume_ratio is not None]
    volume_confirm_count = sum(1 for ratio in volume_ratios if ratio >= 1.0)
    success_count = sum(1 for value in swing_values if value >= MIN_CANDIDATE_AVG_SWING_PCT)
    success_rate = success_count / swing_count * 100.0 if swing_count else None

    candidate = (
        total >= MIN_CANDIDATE_SCORE
        and touch_count >= MIN_VALID_TOUCHES
        and swing_count >= MIN_VALID_SWING_CYCLES
        and (avg_swing or 0.0) >= MIN_CANDIDATE_AVG_SWING_PCT
        and (avg_efficiency is None or avg_efficiency >= MIN_CANDIDATE_REBOUND_EFFICIENCY)
        and (top_stability_pct is None or top_stability_pct <= MAX_CANDIDATE_TOP_STABILITY_PCT)
    )

    reason = _build_reason(
        grade=grade,
        total=total,
        touch_count=touch_count,
        swing_count=swing_count,
        avg_swing=avg_swing,
        avg_penetration=avg_penetration,
        top_stability_pct=top_stability_pct,
        rebound_efficiency=avg_efficiency,
        duration_weeks=duration_weeks,
        pending_count=pending_count,
    )

    return {
        "demand_support_score": round(total, 1),
        "demand_support_grade": grade,
        "demand_support_reason": reason,
        "demand_support_candidate": bool(candidate),
        "demand_support_price": round(support, 4),
        "demand_support_lower": round(zone_lower, 4),
        "demand_support_upper": round(zone_upper, 4),
        "demand_support_zone_width_pct": round((zone_upper / zone_lower - 1.0) * 100.0, 2),
        "demand_support_touch_count": int(touch_count),
        "demand_support_success_count": int(success_count),
        "demand_support_pending_count": int(pending_count),
        "demand_support_success_rate": round(success_rate, 1) if success_rate is not None else None,
        "demand_support_avg_rebound_pct": round(avg_swing, 2) if avg_swing is not None else None,
        "demand_support_avg_penetration_pct": round(avg_penetration or 0.0, 2),
        "demand_support_max_penetration_pct": round(max_penetration or 0.0, 2),
        "demand_support_box_height_pct": round(box_height_pct, 2) if box_height_pct is not None else None,
        "demand_support_duration_weeks": int(duration_weeks),
        "demand_support_latest_touch_date": _format_date(latest_touch.date),
        "demand_support_volume_confirm_count": int(volume_confirm_count),
        "demand_support_volume_confirm_rate": (
            round(volume_confirm_count / len(volume_ratios) * 100.0, 1) if volume_ratios else None
        ),
        "demand_support_avg_touch_volume_ratio": round(_mean(volume_ratios), 2) if volume_ratios else None,
        "demand_support_score_touch": round(touch_score, 1),
        "demand_support_score_rebound": round(swing_score, 1),
        "demand_support_score_penetration": round(penetration_score, 1),
        "demand_support_score_box": round(top_score, 1),
        "demand_support_score_duration": round(duration_score, 1),
        "demand_support_score_cycle": round(cycle_score, 1),
        "demand_support_score_volume": round(volume_score, 1),
        "demand_support_data_source": source_label,
        "demand_support_lookback_bars": int(lookback),
        "demand_support_avg_swing_pct": round(avg_swing, 2) if avg_swing is not None else None,
        "demand_support_swing_count": int(swing_count),
        "demand_support_top_price": round(box_top, 4) if box_top is not None else None,
        "demand_support_top_stability_pct": round(top_stability_pct, 2) if top_stability_pct is not None else None,
        "demand_support_rebound_efficiency": round(avg_efficiency, 3) if avg_efficiency is not None else None,
        "demand_support_box_utilization_pct": round(utilization_pct, 1) if utilization_pct is not None else None,
        "demand_support_volume_contraction_ratio": (
            round(volume_contraction_ratio, 2) if volume_contraction_ratio is not None else None
        ),
    }


def _default_result(reason: str) -> dict[str, object]:
    return {
        "demand_support_score": 0.0,
        "demand_support_grade": "C",
        "demand_support_reason": reason,
        "demand_support_candidate": False,
        "demand_support_price": None,
        "demand_support_lower": None,
        "demand_support_upper": None,
        "demand_support_zone_width_pct": None,
        "demand_support_touch_count": 0,
        "demand_support_success_count": 0,
        "demand_support_pending_count": 0,
        "demand_support_success_rate": None,
        "demand_support_avg_rebound_pct": None,
        "demand_support_avg_penetration_pct": None,
        "demand_support_max_penetration_pct": None,
        "demand_support_box_height_pct": None,
        "demand_support_duration_weeks": 0,
        "demand_support_latest_touch_date": "",
        "demand_support_volume_confirm_count": 0,
        "demand_support_volume_confirm_rate": None,
        "demand_support_avg_touch_volume_ratio": None,
        "demand_support_score_touch": 0.0,
        "demand_support_score_rebound": 0.0,
        "demand_support_score_penetration": 0.0,
        "demand_support_score_box": 0.0,
        "demand_support_score_duration": 0.0,
        "demand_support_score_cycle": 0.0,
        "demand_support_score_volume": 0.0,
        "demand_support_data_source": "",
        "demand_support_lookback_bars": 0,
        "demand_support_avg_swing_pct": None,
        "demand_support_swing_count": 0,
        "demand_support_top_price": None,
        "demand_support_top_stability_pct": None,
        "demand_support_rebound_efficiency": None,
        "demand_support_box_utilization_pct": None,
        "demand_support_volume_contraction_ratio": None,
    }


def _find_pivot_lows(window: pd.DataFrame) -> list[dict[str, float | int]]:
    lows = window["low"].to_numpy(dtype=float)
    pivots: list[dict[str, float | int]] = []
    for i in range(len(window)):
        left = max(0, i - PIVOT_RADIUS)
        right = min(len(window), i + PIVOT_RADIUS + 1)
        local = lows[left:right]
        low = lows[i]
        if np.isfinite(low) and low <= float(np.nanmin(local)):
            pivots.append({"idx": int(window.iloc[i]["_abs_idx"]), "price": float(low)})
    return pivots


def _fallback_low_candidates(window: pd.DataFrame) -> list[dict[str, float | int]]:
    cutoff = float(window["low"].quantile(0.35))
    candidates = window[window["low"] <= cutoff]
    return [
        {"idx": int(row["_abs_idx"]), "price": float(row["low"])}
        for _, row in candidates.iterrows()
        if pd.notna(row.get("low"))
    ]


def _cluster_prices(
    points: list[dict[str, float | int]],
    tolerance_pct: float,
) -> list[list[dict[str, float | int]]]:
    ordered = sorted(points, key=lambda item: float(item["price"]))
    clusters: list[list[dict[str, float | int]]] = []
    for point in ordered:
        price = float(point["price"])
        placed = False
        for cluster in clusters:
            center = float(np.median([float(item["price"]) for item in cluster]))
            if center > 0 and abs(price / center - 1.0) * 100.0 <= tolerance_pct:
                cluster.append(point)
                placed = True
                break
        if not placed:
            clusters.append([point])
    return clusters


def _select_best_cluster(
    clusters: list[list[dict[str, float | int]]],
    total_weeks: int,
) -> list[dict[str, float | int]]:
    def key(cluster: list[dict[str, float | int]]) -> tuple[float, float, float]:
        idxs = [int(item["idx"]) for item in cluster]
        span = max(idxs) - min(idxs) if idxs else 0
        recency = max(idxs) / max(total_weeks - 1, 1) if idxs else 0.0
        return (float(len(cluster)), float(span), float(recency))

    return max(clusters, key=key)


def _build_support_touches(
    weekly: pd.DataFrame,
    window_start: int,
    support: float,
    zone_lower: float,
    zone_upper: float,
    merge_gap_bars: int,
) -> list[SupportTouch]:
    in_zone = weekly.iloc[window_start:][
        (weekly.iloc[window_start:]["low"] >= zone_lower)
        & (weekly.iloc[window_start:]["low"] <= zone_upper)
    ]
    if in_zone.empty:
        return []

    touches: list[SupportTouch] = []
    current_group: list[int] = []
    previous_idx: int | None = None
    for idx in in_zone.index.tolist():
        if previous_idx is None or idx - previous_idx <= merge_gap_bars:
            current_group.append(idx)
        else:
            touches.append(_touch_from_group(weekly, current_group, support))
            current_group = [idx]
        previous_idx = idx
    if current_group:
        touches.append(_touch_from_group(weekly, current_group, support))
    return touches


def _touch_from_group(weekly: pd.DataFrame, group: list[int], support: float) -> SupportTouch:
    lows = weekly.loc[group, "low"]
    idx = int(lows.idxmin())
    low = float(weekly.loc[idx, "low"])
    penetration_pct = max(0.0, (support - low) / support * 100.0) if support > 0 else 0.0
    return SupportTouch(
        index=idx,
        date=weekly.loc[idx, "trade_date"],
        low=low,
        penetration_pct=penetration_pct,
        volume_ratio=_touch_volume_ratio(weekly, idx),
    )


def _build_swing_cycles(
    weekly: pd.DataFrame,
    touches: list[SupportTouch],
    box_top: float | None,
) -> list[SwingCycle]:
    cycles: list[SwingCycle] = []
    for idx, touch in enumerate(touches[:-1]):
        next_touch = touches[idx + 1]
        if next_touch.index <= touch.index:
            continue
        segment = weekly.iloc[touch.index : next_touch.index + 1]
        if segment.empty:
            continue
        peak_index = int(segment["high"].idxmax())
        peak_price = float(weekly.loc[peak_index, "high"])
        if touch.low <= 0 or peak_price <= touch.low:
            continue
        swing_pct = (peak_price / touch.low - 1.0) * 100.0
        efficiency = None
        if box_top is not None and box_top > touch.low:
            efficiency = max(0.0, min((peak_price - touch.low) / (box_top - touch.low), 1.4))
        cycles.append(
            SwingCycle(
                touch_index=touch.index,
                next_touch_index=next_touch.index,
                peak_index=peak_index,
                touch_low=touch.low,
                peak_price=peak_price,
                swing_pct=swing_pct,
                rebound_efficiency=efficiency,
            )
        )
    return cycles


def _estimate_box_top(weekly: pd.DataFrame, peak_prices: list[float], support: float) -> float | None:
    clean_peaks = [float(value) for value in peak_prices if np.isfinite(value) and value > support]
    if clean_peaks:
        return float(np.median(clean_peaks))

    recent = weekly.tail(min(LOOKBACK_WEEKS, len(weekly)))
    high_candidates = recent[recent["high"] >= recent["high"].quantile(0.75)]["high"].dropna()
    if high_candidates.empty:
        return None
    top = float(high_candidates.median())
    return top if top > support else None


def _touch_volume_ratio(weekly: pd.DataFrame, idx: int) -> float | None:
    if "volume" not in weekly.columns or idx <= 0:
        return None
    start = max(0, idx - VOLUME_LOOKBACK_WEEKS)
    baseline = weekly.iloc[start:idx]["volume"].dropna()
    current = weekly.iloc[idx].get("volume")
    if baseline.empty or current is None or pd.isna(current):
        return None
    baseline_mean = float(baseline.mean())
    if baseline_mean <= 0:
        return None
    return float(current) / baseline_mean


def _volume_contraction_ratio(weekly: pd.DataFrame, touches: list[SupportTouch]) -> float | None:
    if "volume" not in weekly.columns or len(weekly) < 12:
        return None
    start_idx = touches[0].index if touches else max(0, len(weekly) - LOOKBACK_WEEKS)
    base = weekly.iloc[start_idx:].copy()
    if len(base) < 12:
        return None
    half = max(len(base) // 2, 1)
    early = base.iloc[:half]["volume"].dropna()
    late = base.iloc[half:]["volume"].dropna()
    if early.empty or late.empty:
        return None
    early_mean = float(early.mean())
    if early_mean <= 0:
        return None
    return float(late.mean()) / early_mean


def _score_touch_count(touch_count: int) -> float:
    if touch_count >= 5:
        return 20.0
    if touch_count == 4:
        return 18.0
    if touch_count == 3:
        return 15.0
    if touch_count == 2:
        return 10.0
    if touch_count == 1:
        return 3.0
    return 0.0


def _score_penetration(avg_penetration_pct: float | None) -> float:
    if avg_penetration_pct is None:
        return 0.0
    if avg_penetration_pct <= 0.5:
        return 15.0
    if avg_penetration_pct <= 1.0:
        return 13.5
    if avg_penetration_pct <= 2.0:
        return 10.5
    if avg_penetration_pct <= 4.0:
        return 6.0
    return max(0.0, 6.0 - (avg_penetration_pct - 4.0) * 1.5)


def _score_avg_swing(avg_swing_pct: float | None) -> float:
    if avg_swing_pct is None:
        return 0.0
    if avg_swing_pct >= 40.0:
        return 25.0
    if avg_swing_pct >= 30.0:
        return 22.0 + (avg_swing_pct - 30.0) / 10.0 * 3.0
    if avg_swing_pct >= 20.0:
        return 17.0 + (avg_swing_pct - 20.0) / 10.0 * 5.0
    if avg_swing_pct >= 12.0:
        return 10.0 + (avg_swing_pct - 12.0) / 8.0 * 7.0
    if avg_swing_pct >= 8.0:
        return 5.0 + (avg_swing_pct - 8.0) / 4.0 * 5.0
    return max(0.0, avg_swing_pct / 8.0 * 5.0)


def _score_cycle_count(cycle_count: int) -> float:
    if cycle_count >= 4:
        return 15.0
    if cycle_count == 3:
        return 12.0
    if cycle_count == 2:
        return 9.0
    if cycle_count == 1:
        return 5.0
    return 0.0


def _top_stability_pct(peak_prices: list[float]) -> float | None:
    clean = [float(value) for value in peak_prices if np.isfinite(value)]
    if len(clean) < 2:
        return None
    mean_value = float(np.mean(clean))
    if mean_value <= 0:
        return None
    return (max(clean) - min(clean)) / mean_value * 100.0


def _score_top_stability(stability_pct: float | None) -> float:
    if stability_pct is None:
        return 2.0
    if stability_pct <= 3.0:
        return 10.0
    if stability_pct <= 6.0:
        return 8.0
    if stability_pct <= 10.0:
        return 5.0
    if stability_pct <= 16.0:
        return 2.0
    return 0.0


def _score_duration(duration_weeks: int) -> float:
    if duration_weeks >= 40:
        return 10.0
    if duration_weeks >= 30:
        return 9.0
    if duration_weeks >= 24:
        return 8.0
    if duration_weeks >= 16:
        return 6.0
    if duration_weeks >= 10:
        return 4.0
    if duration_weeks >= 6:
        return 2.0
    return 0.0


def _score_volume_contraction(ratio: float | None) -> float:
    if ratio is None:
        return 0.0
    if ratio <= 0.65:
        return 5.0
    if ratio <= 0.8:
        return 4.0
    if ratio <= 0.95:
        return 2.5
    if ratio <= 1.1:
        return 1.0
    return 0.0


def _touch_span_weeks(touches: list[SupportTouch]) -> int:
    if len(touches) < 2:
        return 0
    return int(touches[-1].index - touches[0].index + 1)


def _grade(score: float) -> str:
    if score >= 90:
        return "S"
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    return "C"


def _build_reason(
    *,
    grade: str,
    total: float,
    touch_count: int,
    swing_count: int,
    avg_swing: float | None,
    avg_penetration: float | None,
    top_stability_pct: float | None,
    rebound_efficiency: float | None,
    duration_weeks: int,
    pending_count: int,
) -> str:
    parts = [f"{grade} base {total:.0f}", f"touches {touch_count}", f"cycles {swing_count}"]
    if avg_swing is not None:
        parts.append(f"avg swing {avg_swing:.1f}%")
    if rebound_efficiency is not None:
        parts.append(f"eff {rebound_efficiency:.2f}")
    if avg_penetration is not None:
        parts.append(f"penetration {avg_penetration:.2f}%")
    if top_stability_pct is not None:
        parts.append(f"top stable {top_stability_pct:.1f}%")
    if duration_weeks:
        parts.append(f"{duration_weeks}w")
    if pending_count:
        parts.append(f"{pending_count} pending")
    return " / ".join(parts)


def _mean(values: list[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and np.isfinite(float(value))]
    if not clean:
        return None
    return float(np.mean(clean))


def _format_date(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return str(value)
