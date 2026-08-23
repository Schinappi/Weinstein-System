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
LOOKBACK_DAYS = 1500
PIVOT_RADIUS = 2
SUPPORT_TOLERANCE_PCT = 1.5
MAX_TOUCH_PENETRATION_PCT = 4.0
VOLUME_LOOKBACK_WEEKS = 20
DAILY_TOUCH_MERGE_GAP_BARS = 10
WEEKLY_TOUCH_MERGE_GAP_BARS = 1
MIN_SEPARATE_TOUCH_SWING_PCT = 12.0
MIN_PRE_TOUCH_RALLY_PCT = 20.0
DAILY_PRE_TOUCH_LOOKBACK_BARS = 45
WEEKLY_PRE_TOUCH_LOOKBACK_BARS = 10
MIN_VALID_TOUCHES = 3
MIN_VALID_SWING_CYCLES = 1
MIN_CANDIDATE_SCORE = 70.0
MIN_CANDIDATE_AVG_SWING_PCT = 15.0
MAX_CANDIDATE_TOP_STABILITY_PCT = 25.0
MIN_CANDIDATE_REBOUND_EFFICIENCY = 0.65
SUPPORT_QUALITY_RAW_MAX = 30.0
HISTORICAL_REBOUND_RAW_MAX = 25.0
SUPPORT_QUALITY_WEIGHT = 45.0
HISTORICAL_REBOUND_WEIGHT = 30.0
CURRENT_DISTANCE_WEIGHT = 25.0
MAJOR_SUPPORT_MAX_SUPPORT_QUANTILE = 0.35
MAJOR_SUPPORT_FLOOR_BAND_PCT = 10.0
FAR_SUPPORT_PENALTY_START_PCT = 10.0
FAR_SUPPORT_PENALTY_PER_PCT = 1.6
FAR_SUPPORT_PENALTY_MAX = 45.0
REBOUND_WINDOWS = (5, 10, 20)
RECENT_APPROACH_LOOKBACK_BARS = 20
RECENT_SUPPORT_BREAK_LOOKBACK_BARS = 20
PULLBACK_VOLUME_RECENT_BARS = 5
PULLBACK_VOLUME_BASELINE_BARS = 20


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


@dataclass(frozen=True)
class SupportZoneEvaluation:
    cluster: list[dict[str, float | int]]
    support: float
    zone_lower: float
    zone_upper: float
    touches: list[SupportTouch]
    preliminary_cycles: list[SwingCycle]
    box_top: float | None
    cycles: list[SwingCycle]
    avg_swing: float | None
    avg_penetration: float | None
    max_penetration: float | None
    duration_bars: int
    duration_weeks: int
    box_height_pct: float | None
    top_stability_pct: float | None
    avg_efficiency: float | None
    utilization_pct: float | None
    volume_contraction_ratio: float | None
    approach_gap_pct: float | None
    approach_decline_pct: float | None
    approach_energy_pct: float | None
    pullback_volume_ratio: float | None
    avg_rebound_5d_pct: float | None
    avg_rebound_10d_pct: float | None
    avg_rebound_20d_pct: float | None
    rebound_success_rate_pct: float | None
    rebound_sample_count: int
    support_quality_score: float
    historical_rebound_score: float
    current_distance_score: float
    support_quality_raw_score: float
    historical_rebound_raw_score: float
    current_distance_raw_score: float
    support_quality_norm_score: float
    historical_rebound_norm_score: float
    current_distance_norm_score: float
    approach_score: float
    trend_filter_score: float
    touch_score: float
    penetration_score: float
    swing_score: float
    cycle_score: float
    top_score: float
    duration_score: float
    volume_score: float
    total: float
    active: bool
    latest_break_pct: float
    recent_close_break_pct: float


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

    merge_gap = DAILY_TOUCH_MERGE_GAP_BARS if source_label == "daily" else WEEKLY_TOUCH_MERGE_GAP_BARS
    evaluations = [
        evaluation
        for cluster in clusters
        if (
            evaluation := _evaluate_support_zone(
                bars=bars,
                cluster=cluster,
                window_start=window_start,
                merge_gap=merge_gap,
                source_label=source_label,
            )
        )
        is not None
    ]
    if not evaluations:
        return _default_result("no support touches")

    selected = _select_best_support_zone(evaluations)
    support = selected.support
    zone_lower = selected.zone_lower
    zone_upper = selected.zone_upper
    touches = selected.touches
    touch_count = len(touches)
    if touch_count == 0:
        return _default_result("no support touches")

    preliminary_cycles = selected.preliminary_cycles
    peak_prices = [cycle.peak_price for cycle in preliminary_cycles]
    box_top = selected.box_top
    cycles = selected.cycles
    swing_values = [cycle.swing_pct for cycle in cycles]
    avg_swing = selected.avg_swing
    swing_count = len(cycles)
    avg_penetration = selected.avg_penetration
    max_penetration = selected.max_penetration
    duration_weeks = selected.duration_weeks
    box_height_pct = selected.box_height_pct
    top_stability_pct = selected.top_stability_pct
    avg_efficiency = selected.avg_efficiency
    utilization_pct = selected.utilization_pct
    volume_contraction_ratio = selected.volume_contraction_ratio
    approach_gap_pct = selected.approach_gap_pct
    approach_decline_pct = selected.approach_decline_pct
    approach_energy_pct = selected.approach_energy_pct
    pullback_volume_ratio = selected.pullback_volume_ratio
    approach_score = selected.approach_score
    support_quality_score = selected.support_quality_score
    historical_rebound_score = selected.historical_rebound_score
    current_distance_score = selected.current_distance_score
    support_quality_raw_score = selected.support_quality_raw_score
    historical_rebound_raw_score = selected.historical_rebound_raw_score
    current_distance_raw_score = selected.current_distance_raw_score
    support_quality_norm_score = selected.support_quality_norm_score
    historical_rebound_norm_score = selected.historical_rebound_norm_score
    current_distance_norm_score = selected.current_distance_norm_score
    trend_filter_score = selected.trend_filter_score
    touch_score = selected.touch_score
    penetration_score = selected.penetration_score
    swing_score = selected.swing_score
    cycle_score = selected.cycle_score
    top_score = selected.top_score
    duration_score = selected.duration_score
    volume_score = selected.volume_score
    total = selected.total
    grade = _grade(total)
    latest_touch = touches[-1]
    pending_count = max(touch_count - swing_count, 0)
    volume_ratios = [touch.volume_ratio for touch in touches if touch.volume_ratio is not None]
    volume_confirm_count = sum(1 for ratio in volume_ratios if ratio >= 1.0)
    success_count = sum(1 for value in swing_values if value >= MIN_CANDIDATE_AVG_SWING_PCT)
    success_rate = success_count / swing_count * 100.0 if swing_count else None

    approach_ok = approach_gap_pct is not None and approach_gap_pct <= 10.0 and current_distance_score >= 5.0
    candidate = (
        selected.active
        and touch_count >= 2
        and support_quality_score >= 28.0
        and historical_rebound_score >= 12.0
        and approach_ok
        and total >= 60.0
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
        latest_break_pct=selected.latest_break_pct,
        recent_close_break_pct=selected.recent_close_break_pct,
        approach_gap_pct=approach_gap_pct,
        approach_decline_pct=approach_decline_pct,
        approach_energy_pct=approach_energy_pct,
        support_quality_score=support_quality_score,
        historical_rebound_score=historical_rebound_score,
        current_distance_score=current_distance_score,
        support_quality_norm_score=support_quality_norm_score,
        historical_rebound_norm_score=historical_rebound_norm_score,
        current_distance_norm_score=current_distance_norm_score,
        volume_score=volume_score,
        trend_filter_score=trend_filter_score,
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
        "demand_support_duration_bars": int(selected.duration_bars),
        "demand_support_duration_unit": source_label,
        "demand_support_latest_touch_date": _format_date(latest_touch.date),
        "demand_support_volume_confirm_count": int(volume_confirm_count),
        "demand_support_volume_confirm_rate": (
            round(volume_confirm_count / len(volume_ratios) * 100.0, 1) if volume_ratios else None
        ),
        "demand_support_avg_touch_volume_ratio": round(_mean(volume_ratios), 2) if volume_ratios else None,
        "demand_support_score_touch": round(support_quality_score, 1),
        "demand_support_score_rebound": round(historical_rebound_score, 1),
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
        "demand_support_approach_gap_pct": round(approach_gap_pct, 2) if approach_gap_pct is not None else None,
        "demand_support_approach_decline_pct": round(approach_decline_pct, 2) if approach_decline_pct is not None else None,
        "demand_support_approach_energy_pct": round(approach_energy_pct, 2) if approach_energy_pct is not None else None,
        "demand_support_pullback_volume_ratio": round(pullback_volume_ratio, 2) if pullback_volume_ratio is not None else None,
        "demand_support_score_approach": round(approach_score, 1),
        "demand_support_score_support_quality": round(support_quality_score, 1),
        "demand_support_score_historical_rebound": round(historical_rebound_score, 1),
        "demand_support_score_current_distance": round(current_distance_score, 1),
        "demand_support_score_support_quality_raw": round(support_quality_raw_score, 1),
        "demand_support_score_historical_rebound_raw": round(historical_rebound_raw_score, 1),
        "demand_support_score_current_distance_raw": round(current_distance_raw_score, 1),
        "demand_support_score_support_quality_norm": round(support_quality_norm_score, 1),
        "demand_support_score_historical_rebound_norm": round(historical_rebound_norm_score, 1),
        "demand_support_score_current_distance_norm": round(current_distance_norm_score, 1),
        "demand_support_score_trend_filter": round(trend_filter_score, 1),
        "demand_support_avg_5d_rebound_pct": (
            round(selected.avg_rebound_5d_pct, 2) if selected.avg_rebound_5d_pct is not None else None
        ),
        "demand_support_avg_10d_rebound_pct": (
            round(selected.avg_rebound_10d_pct, 2) if selected.avg_rebound_10d_pct is not None else None
        ),
        "demand_support_avg_20d_rebound_pct": (
            round(selected.avg_rebound_20d_pct, 2) if selected.avg_rebound_20d_pct is not None else None
        ),
        "demand_support_rebound_success_rate": (
            round(selected.rebound_success_rate_pct, 1) if selected.rebound_success_rate_pct is not None else None
        ),
        "demand_support_rebound_sample_count": int(selected.rebound_sample_count),
        "demand_support_active": bool(selected.active),
        "demand_support_latest_break_pct": round(selected.latest_break_pct, 2),
        "demand_support_recent_close_break_pct": round(selected.recent_close_break_pct, 2),
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
        "demand_support_duration_bars": 0,
        "demand_support_duration_unit": "",
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
        "demand_support_approach_gap_pct": None,
        "demand_support_approach_decline_pct": None,
        "demand_support_approach_energy_pct": None,
        "demand_support_pullback_volume_ratio": None,
        "demand_support_score_approach": 0.0,
        "demand_support_score_support_quality": 0.0,
        "demand_support_score_historical_rebound": 0.0,
        "demand_support_score_current_distance": 0.0,
        "demand_support_score_support_quality_raw": 0.0,
        "demand_support_score_historical_rebound_raw": 0.0,
        "demand_support_score_current_distance_raw": 0.0,
        "demand_support_score_support_quality_norm": 0.0,
        "demand_support_score_historical_rebound_norm": 0.0,
        "demand_support_score_current_distance_norm": 0.0,
        "demand_support_score_trend_filter": 0.0,
        "demand_support_avg_5d_rebound_pct": None,
        "demand_support_avg_10d_rebound_pct": None,
        "demand_support_avg_20d_rebound_pct": None,
        "demand_support_rebound_success_rate": None,
        "demand_support_rebound_sample_count": 0,
        "demand_support_active": False,
        "demand_support_latest_break_pct": None,
        "demand_support_recent_close_break_pct": None,
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


def _evaluate_support_zone(
    *,
    bars: pd.DataFrame,
    cluster: list[dict[str, float | int]],
    window_start: int,
    merge_gap: int,
    source_label: str,
) -> SupportZoneEvaluation | None:
    support = float(np.median([item["price"] for item in cluster]))
    if support <= 0:
        return None

    zone_lower = support * (1.0 - SUPPORT_TOLERANCE_PCT / 100.0)
    zone_upper = support * (1.0 + SUPPORT_TOLERANCE_PCT / 100.0)
    raw_touches = _build_support_touches(bars, window_start, support, zone_lower, zone_upper, merge_gap)
    raw_touches = _filter_retest_touches(bars, raw_touches, support, source_label)
    touches = _merge_weak_retest_touches(bars, raw_touches)
    if not touches:
        return None

    preliminary_cycles = _build_swing_cycles(bars, touches, box_top=None)
    peak_prices = [cycle.peak_price for cycle in preliminary_cycles]
    box_top = _estimate_box_top(bars, peak_prices, support)
    cycles = _build_swing_cycles(bars, touches, box_top=box_top)
    swing_values = [cycle.swing_pct for cycle in cycles]
    avg_swing = _mean(swing_values)

    penetration_values = [touch.penetration_pct for touch in touches]
    avg_penetration = _mean(penetration_values)
    max_penetration = max(penetration_values) if penetration_values else None
    duration_bars = _touch_span_bars(touches)
    duration_weeks = _duration_in_weeks(duration_bars, source_label)

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
    latest_low = float(bars.iloc[-1]["low"])
    recent_low_window = bars.tail(min(5, len(bars)))
    recent_low_max = float(recent_low_window["low"].max()) if not recent_low_window.empty else None
    approach_gap_pct = None
    approach_decline_pct = None
    if support > 0 and np.isfinite(latest_low):
        approach_gap_pct = max(0.0, (latest_low / support - 1.0) * 100.0)
    if recent_low_max is not None and np.isfinite(latest_low) and latest_low > 0:
        approach_decline_pct = max(0.0, (recent_low_max / latest_low - 1.0) * 100.0)
    approach_energy_pct = _approach_energy_pct(bars, latest_low)
    pullback_volume_ratio = _pullback_volume_ratio(bars)
    rebound_stats = _historical_rebound_stats(bars, touches)

    support_quality_raw_score = _score_support_quality(touches, avg_penetration, duration_bars, source_label)
    historical_rebound_raw_score = _score_historical_rebound(rebound_stats)
    current_distance_raw_score = _score_current_distance(approach_gap_pct)
    support_quality_norm_score = _normalize_score(support_quality_raw_score, SUPPORT_QUALITY_RAW_MAX)
    historical_rebound_norm_score = _normalize_score(historical_rebound_raw_score, HISTORICAL_REBOUND_RAW_MAX)
    current_distance_norm_score = _normalize_score(current_distance_raw_score, CURRENT_DISTANCE_WEIGHT)
    support_quality_score = _score_weighted_support_quality(support_quality_norm_score)
    historical_rebound_score = _score_weighted_historical_rebound(historical_rebound_norm_score)
    current_distance_score = _score_weighted_current_distance(current_distance_norm_score)
    far_distance_penalty = _score_far_distance_penalty(approach_gap_pct)
    volume_score = _score_pullback_volume(pullback_volume_ratio, volume_contraction_ratio)
    trend_filter_score = _score_trend_filter(bars)
    approach_score = current_distance_score

    touch_score = _score_touch_count(len(touches))
    penetration_score = _score_penetration(avg_penetration)
    swing_score = _score_avg_swing(avg_swing)
    cycle_score = _score_cycle_count(len(cycles))
    top_score = _score_top_stability(top_stability_pct)
    duration_score = _score_duration(duration_weeks)
    total = min(
        100.0,
        max(
            0.0,
            support_quality_score
            + historical_rebound_score
            + current_distance_score
            - far_distance_penalty,
        ),
    )
    latest_break_pct = _latest_break_pct(bars, zone_lower, support)
    recent_close_break_pct = _recent_close_break_pct(bars, zone_lower, support)

    return SupportZoneEvaluation(
        cluster=cluster,
        support=support,
        zone_lower=zone_lower,
        zone_upper=zone_upper,
        touches=touches,
        preliminary_cycles=preliminary_cycles,
        box_top=box_top,
        cycles=cycles,
        avg_swing=avg_swing,
        avg_penetration=avg_penetration,
        max_penetration=max_penetration,
        duration_bars=duration_bars,
        duration_weeks=duration_weeks,
        box_height_pct=box_height_pct,
        top_stability_pct=top_stability_pct,
        avg_efficiency=avg_efficiency,
        utilization_pct=utilization_pct,
        volume_contraction_ratio=volume_contraction_ratio,
        approach_gap_pct=approach_gap_pct,
        approach_decline_pct=approach_decline_pct,
        approach_energy_pct=approach_energy_pct,
        pullback_volume_ratio=pullback_volume_ratio,
        avg_rebound_5d_pct=rebound_stats.get("avg_5d"),
        avg_rebound_10d_pct=rebound_stats.get("avg_10d"),
        avg_rebound_20d_pct=rebound_stats.get("avg_20d"),
        rebound_success_rate_pct=rebound_stats.get("success_rate"),
        rebound_sample_count=int(rebound_stats.get("sample_count") or 0),
        support_quality_score=support_quality_score,
        historical_rebound_score=historical_rebound_score,
        current_distance_score=current_distance_score,
        support_quality_raw_score=support_quality_raw_score,
        historical_rebound_raw_score=historical_rebound_raw_score,
        current_distance_raw_score=current_distance_raw_score,
        support_quality_norm_score=support_quality_norm_score,
        historical_rebound_norm_score=historical_rebound_norm_score,
        current_distance_norm_score=current_distance_norm_score,
        approach_score=approach_score,
        trend_filter_score=trend_filter_score,
        touch_score=touch_score,
        penetration_score=penetration_score,
        swing_score=swing_score,
        cycle_score=cycle_score,
        top_score=top_score,
        duration_score=duration_score,
        volume_score=volume_score,
        total=total,
        active=latest_break_pct <= 0.0 and recent_close_break_pct <= 0.0,
        latest_break_pct=latest_break_pct,
        recent_close_break_pct=recent_close_break_pct,
    )


def _select_best_support_zone(evaluations: list[SupportZoneEvaluation]) -> SupportZoneEvaluation:
    if not evaluations:
        raise ValueError("No support zone evaluations available")

    active = [evaluation for evaluation in evaluations if evaluation.active]
    pool = active or evaluations
    strong = [evaluation for evaluation in pool if len(evaluation.touches) >= 2]
    candidates = strong or pool

    major_pool = _major_support_pool(candidates)
    # A nearby minor shelf must not hide a lower, repeatedly tested floor.
    # Current distance remains part of the score, but it is not a selector gate.
    pool = major_pool or candidates

    def key(evaluation: SupportZoneEvaluation) -> tuple[float, float, float, float, float, float, float]:
        avg_swing = evaluation.avg_swing or 0.0
        structural_strength = evaluation.support_quality_score + evaluation.historical_rebound_score
        return (
            structural_strength,
            float(evaluation.support_quality_score),
            float(evaluation.historical_rebound_score),
            float(len(evaluation.touches)),
            float(evaluation.duration_bars),
            avg_swing,
            float(evaluation.current_distance_score),
        )

    return max(pool, key=key)


def _major_support_pool(evaluations: list[SupportZoneEvaluation]) -> list[SupportZoneEvaluation]:
    if not evaluations:
        return []

    supports = np.array([evaluation.support for evaluation in evaluations], dtype=float)
    supports = supports[np.isfinite(supports)]
    if supports.size == 0:
        return []

    low_ceiling = float(np.quantile(supports, MAJOR_SUPPORT_MAX_SUPPORT_QUANTILE))
    floor = float(np.min(supports))
    floor_band = floor * (1.0 + MAJOR_SUPPORT_FLOOR_BAND_PCT / 100.0)
    major_ceiling = min(low_ceiling, floor_band)
    return [
        evaluation
        for evaluation in evaluations
        if (
            len(evaluation.touches) >= 2
            and evaluation.support <= major_ceiling
            and evaluation.support_quality_score >= 34.0
            and evaluation.historical_rebound_score >= 18.0
        )
    ]


def _build_support_touches(
    weekly: pd.DataFrame,
    window_start: int,
    support: float,
    zone_lower: float,
    zone_upper: float,
    merge_gap_bars: int,
) -> list[SupportTouch]:
    touch_lower = support * (1.0 - MAX_TOUCH_PENETRATION_PCT / 100.0)
    in_zone = weekly.iloc[window_start:][
        (weekly.iloc[window_start:]["low"] >= touch_lower)
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


def _merge_weak_retest_touches(weekly: pd.DataFrame, touches: list[SupportTouch]) -> list[SupportTouch]:
    if len(touches) <= 1:
        return touches

    merged: list[SupportTouch] = [touches[0]]
    for touch in touches[1:]:
        previous = merged[-1]
        if _touch_to_touch_swing_pct(weekly, previous, touch) < MIN_SEPARATE_TOUCH_SWING_PCT:
            merged[-1] = _merge_touch_pair(previous, touch)
        else:
            merged.append(touch)
    return merged


def _filter_retest_touches(
    weekly: pd.DataFrame,
    touches: list[SupportTouch],
    support: float,
    source_label: str,
) -> list[SupportTouch]:
    if len(touches) <= 1 or support <= 0:
        return touches

    lookback = DAILY_PRE_TOUCH_LOOKBACK_BARS if source_label == "daily" else WEEKLY_PRE_TOUCH_LOOKBACK_BARS
    valid: list[SupportTouch] = []
    for touch in touches:
        start = max(0, touch.index - lookback)
        previous = weekly.iloc[start:touch.index]
        if previous.empty:
            continue
        pre_high = float(previous["high"].max())
        if not np.isfinite(pre_high):
            continue
        pre_rally_pct = (pre_high / support - 1.0) * 100.0
        if pre_rally_pct >= MIN_PRE_TOUCH_RALLY_PCT:
            valid.append(touch)
    return valid


def _touch_to_touch_swing_pct(weekly: pd.DataFrame, first: SupportTouch, second: SupportTouch) -> float:
    if second.index <= first.index or first.low <= 0:
        return 0.0
    segment = weekly.iloc[first.index : second.index + 1]
    if segment.empty:
        return 0.0
    peak_price = float(segment["high"].max())
    return max(0.0, (peak_price / first.low - 1.0) * 100.0)


def _merge_touch_pair(first: SupportTouch, second: SupportTouch) -> SupportTouch:
    if second.low < first.low:
        low_touch = second
    else:
        low_touch = first

    volume_ratio = _mean([first.volume_ratio, second.volume_ratio])
    return SupportTouch(
        index=low_touch.index,
        date=low_touch.date,
        low=low_touch.low,
        penetration_pct=max(first.penetration_pct, second.penetration_pct),
        volume_ratio=volume_ratio,
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


def _historical_rebound_stats(weekly: pd.DataFrame, touches: list[SupportTouch]) -> dict[str, float | int | None]:
    rebounds_by_window: dict[int, list[float]] = {window: [] for window in REBOUND_WINDOWS}
    best_rebounds: list[float] = []
    for touch in touches:
        future_bars = len(weekly) - touch.index - 1
        if touch.low <= 0 or future_bars < min(REBOUND_WINDOWS):
            continue
        per_touch: list[float] = []
        for window in REBOUND_WINDOWS:
            if future_bars < window:
                continue
            segment = weekly.iloc[touch.index : touch.index + window + 1]
            if segment.empty:
                continue
            high = float(segment["high"].max())
            if not np.isfinite(high) or high <= touch.low:
                continue
            rebound = (high / touch.low - 1.0) * 100.0
            rebounds_by_window[window].append(rebound)
            per_touch.append(rebound)
        if per_touch:
            best_rebounds.append(max(per_touch))

    success_count = sum(1 for value in best_rebounds if value >= MIN_CANDIDATE_AVG_SWING_PCT)
    return {
        "avg_5d": _mean(rebounds_by_window[5]),
        "avg_10d": _mean(rebounds_by_window[10]),
        "avg_20d": _mean(rebounds_by_window[20]),
        "success_rate": (success_count / len(best_rebounds) * 100.0 if best_rebounds else None),
        "sample_count": len(best_rebounds),
    }


def _approach_energy_pct(weekly: pd.DataFrame, latest_low: float) -> float | None:
    if weekly.empty or latest_low <= 0:
        return None
    recent = weekly.tail(min(RECENT_APPROACH_LOOKBACK_BARS, len(weekly)))
    if recent.empty:
        return None
    recent_high = float(recent["high"].max())
    if not np.isfinite(recent_high):
        return None
    return max(0.0, (recent_high / latest_low - 1.0) * 100.0)


def _pullback_volume_ratio(weekly: pd.DataFrame) -> float | None:
    if "volume" not in weekly.columns or len(weekly) < PULLBACK_VOLUME_RECENT_BARS + 5:
        return None
    recent = weekly.tail(PULLBACK_VOLUME_RECENT_BARS)["volume"].dropna()
    baseline_start = max(0, len(weekly) - PULLBACK_VOLUME_RECENT_BARS - PULLBACK_VOLUME_BASELINE_BARS)
    baseline = weekly.iloc[baseline_start : len(weekly) - PULLBACK_VOLUME_RECENT_BARS]["volume"].dropna()
    if recent.empty or baseline.empty:
        return None
    baseline_mean = float(baseline.mean())
    if baseline_mean <= 0:
        return None
    return float(recent.mean()) / baseline_mean


def _score_support_quality(
    touches: list[SupportTouch],
    avg_penetration_pct: float | None,
    duration_bars: int,
    source_label: str,
) -> float:
    touch_count = len(touches)
    if touch_count >= 4:
        touch_component = 16.0
    elif touch_count == 3:
        touch_component = 14.0
    elif touch_count == 2:
        touch_component = 11.0
    elif touch_count == 1:
        touch_component = 3.0
    else:
        touch_component = 0.0

    penetration_component = min(_score_penetration(avg_penetration_pct) / 15.0 * 8.0, 8.0)

    interval_component = 0.0
    if touch_count >= 2:
        interval_bars = duration_bars / max(touch_count - 1, 1)
        interval_weeks = _duration_in_weeks(int(round(interval_bars)), source_label)
        if interval_weeks >= 24:
            interval_component = 6.0
        elif interval_weeks >= 12:
            interval_component = 5.0
        elif interval_weeks >= 6:
            interval_component = 3.0
        elif interval_weeks >= 3:
            interval_component = 1.5
        else:
            interval_component = 0.5

    return min(30.0, touch_component + penetration_component + interval_component)


def _normalize_score(score: float | None, raw_max: float) -> float:
    if score is None or raw_max <= 0:
        return 0.0
    return min(max(float(score) / raw_max * 100.0, 0.0), 100.0)


def _score_weighted_support_quality(norm_score: float) -> float:
    return _piecewise_linear(
        norm_score,
        [
            (0.0, 0.0),
            (40.0, 18.0),
            (60.0, 28.0),
            (75.0, 37.0),
            (90.0, 43.0),
            (100.0, SUPPORT_QUALITY_WEIGHT),
        ],
    )


def _score_weighted_historical_rebound(norm_score: float) -> float:
    return _piecewise_linear(
        norm_score,
        [
            (0.0, 0.0),
            (20.0, 6.0),
            (40.0, 12.0),
            (60.0, 18.0),
            (80.0, 23.0),
            (100.0, HISTORICAL_REBOUND_WEIGHT),
        ],
    )


def _score_weighted_current_distance(norm_score: float) -> float:
    return min(max(norm_score / 100.0 * CURRENT_DISTANCE_WEIGHT, 0.0), CURRENT_DISTANCE_WEIGHT)


def _score_far_distance_penalty(gap_pct: float | None) -> float:
    if gap_pct is None or gap_pct <= FAR_SUPPORT_PENALTY_START_PCT:
        return 0.0
    penalty = (gap_pct - FAR_SUPPORT_PENALTY_START_PCT) * FAR_SUPPORT_PENALTY_PER_PCT
    return min(max(penalty, 0.0), FAR_SUPPORT_PENALTY_MAX)


def _piecewise_linear(value: float, points: list[tuple[float, float]]) -> float:
    clean_value = min(max(float(value), points[0][0]), points[-1][0])
    for idx in range(1, len(points)):
        left_x, left_y = points[idx - 1]
        right_x, right_y = points[idx]
        if clean_value <= right_x:
            if right_x == left_x:
                return right_y
            ratio = (clean_value - left_x) / (right_x - left_x)
            return left_y + (right_y - left_y) * ratio
    return points[-1][1]


def _score_historical_rebound(stats: dict[str, float | int | None]) -> float:
    avg_candidates = [
        stats.get("avg_10d"),
        stats.get("avg_20d"),
        stats.get("avg_5d"),
    ]
    avg_rebound = next((float(value) for value in avg_candidates if value is not None), None)
    if avg_rebound is None:
        return 0.0

    if avg_rebound >= 25.0:
        strength_component = 17.0
    elif avg_rebound >= 18.0:
        strength_component = 15.0
    elif avg_rebound >= 12.0:
        strength_component = 11.0
    elif avg_rebound >= 8.0:
        strength_component = 7.0
    elif avg_rebound >= 5.0:
        strength_component = 4.0
    else:
        strength_component = max(0.0, avg_rebound / 5.0 * 4.0)

    success_rate = stats.get("success_rate")
    if success_rate is None:
        reliability_component = 0.0
    elif float(success_rate) >= 80.0:
        reliability_component = 6.0
    elif float(success_rate) >= 60.0:
        reliability_component = 4.5
    elif float(success_rate) >= 40.0:
        reliability_component = 2.5
    elif float(success_rate) > 0.0:
        reliability_component = 1.0
    else:
        reliability_component = 0.0

    sample_count = int(stats.get("sample_count") or 0)
    if sample_count >= 3:
        sample_component = 2.0
    elif sample_count == 2:
        sample_component = 1.5
    elif sample_count == 1:
        sample_component = 0.75
    else:
        sample_component = 0.0

    return min(25.0, strength_component + reliability_component + sample_component)


def _score_current_distance(gap_pct: float | None) -> float:
    if gap_pct is None:
        return 0.0

    if gap_pct <= 0.0:
        return 25.0
    elif gap_pct <= 1.0:
        return 25.0
    elif gap_pct <= 2.0:
        return 24.0
    elif gap_pct <= 4.0:
        return 21.0
    elif gap_pct <= 6.0:
        return 16.0
    elif gap_pct <= 8.0:
        return 10.0
    elif gap_pct <= 10.0:
        return 5.0
    return 0.0


def _score_pullback_volume(
    pullback_ratio: float | None,
    base_contraction_ratio: float | None,
) -> float:
    ratio = pullback_ratio if pullback_ratio is not None else base_contraction_ratio
    if ratio is None:
        return 0.0
    if ratio <= 0.65:
        return 10.0
    if ratio <= 0.8:
        return 8.0
    if ratio <= 0.95:
        return 5.5
    if ratio <= 1.1:
        return 2.5
    return 0.0


def _score_trend_filter(weekly: pd.DataFrame) -> float:
    closes = weekly["close"].dropna().astype(float)
    if len(closes) < 60:
        return 2.0
    span = min(150, max(30, len(closes) // 2))
    ema = closes.ewm(span=span, adjust=False).mean()
    latest_close = float(closes.iloc[-1])
    latest_ema = float(ema.iloc[-1])
    if latest_ema <= 0:
        return 0.0
    lookback = min(20, len(ema) - 1)
    ema_slope_pct = (latest_ema / float(ema.iloc[-lookback - 1]) - 1.0) * 100.0 if lookback > 0 else 0.0
    if latest_close >= latest_ema and ema_slope_pct >= 0.0:
        return 5.0
    if latest_close >= latest_ema * 0.95 and ema_slope_pct >= -3.0:
        return 3.5
    if latest_close >= latest_ema * 0.9:
        return 2.0
    if latest_close >= latest_ema * 0.85:
        return 1.0
    return 0.0


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
    if avg_penetration_pct <= 1.5:
        return 14.0
    if avg_penetration_pct <= 3.0:
        return 12.5
    if avg_penetration_pct <= 4.0:
        return 10.0
    if avg_penetration_pct <= 6.0:
        return 5.0
    return max(0.0, 5.0 - (avg_penetration_pct - 6.0) * 1.5)


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
        return 6.0
    if stability_pct <= 18.0:
        return 4.0
    if stability_pct <= MAX_CANDIDATE_TOP_STABILITY_PCT:
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


def _touch_span_bars(touches: list[SupportTouch]) -> int:
    if len(touches) < 2:
        return 0
    return int(touches[-1].index - touches[0].index + 1)


def _duration_in_weeks(duration_bars: int, source_label: str) -> int:
    if source_label == "daily":
        return int(round(duration_bars / 5.0))
    return duration_bars


def _latest_break_pct(weekly: pd.DataFrame, zone_lower: float, support: float) -> float:
    if weekly.empty or support <= 0:
        return 0.0
    latest_close = weekly.iloc[-1].get("close")
    if latest_close is None or pd.isna(latest_close):
        return 0.0
    return max(0.0, (zone_lower - float(latest_close)) / support * 100.0)


def _recent_close_break_pct(bars: pd.DataFrame, zone_lower: float, support: float) -> float:
    if bars.empty or support <= 0 or "close" not in bars.columns:
        return 0.0
    recent = bars.tail(min(RECENT_SUPPORT_BREAK_LOOKBACK_BARS, len(bars)))
    closes = pd.to_numeric(recent["close"], errors="coerce").dropna()
    if closes.empty:
        return 0.0
    break_pcts = (zone_lower - closes) / support * 100.0
    return max(0.0, float(break_pcts.max()))


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
    latest_break_pct: float,
    recent_close_break_pct: float,
    approach_gap_pct: float | None,
    approach_decline_pct: float | None,
    approach_energy_pct: float | None,
    support_quality_score: float,
    historical_rebound_score: float,
    current_distance_score: float,
    support_quality_norm_score: float,
    historical_rebound_norm_score: float,
    current_distance_norm_score: float,
    volume_score: float,
    trend_filter_score: float,
) -> str:
    parts = [
        f"{grade} demand {total:.0f}",
        f"support {support_quality_score:.1f}/45({support_quality_norm_score:.0f})",
        f"rebound {historical_rebound_score:.1f}/30({historical_rebound_norm_score:.0f})",
        f"distance {current_distance_score:.1f}/25({current_distance_norm_score:.0f})",
    ]
    parts.append(f"touches {touch_count}")
    if avg_swing is not None:
        parts.append(f"aux swing {avg_swing:.1f}%")
    if rebound_efficiency is not None:
        parts.append(f"eff {rebound_efficiency:.2f}")
    if avg_penetration is not None:
        parts.append(f"penetration {avg_penetration:.2f}%")
    if top_stability_pct is not None:
        parts.append(f"top stable {top_stability_pct:.1f}%")
    if duration_weeks:
        parts.append(f"{duration_weeks}w")
    if approach_gap_pct is not None:
        parts.append(f"approach {approach_gap_pct:.1f}%")
    if approach_decline_pct is not None:
        parts.append(f"5d decline {approach_decline_pct:.1f}%")
    if approach_energy_pct is not None:
        parts.append(f"20d energy {approach_energy_pct:.1f}%")
    parts.append(f"vol {volume_score:.1f}")
    parts.append(f"trend {trend_filter_score:.1f}")
    if latest_break_pct > 0:
        parts.append(f"broken {latest_break_pct:.1f}%")
    if recent_close_break_pct > 0:
        parts.append(f"recent close broken {recent_close_break_pct:.1f}%")
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
