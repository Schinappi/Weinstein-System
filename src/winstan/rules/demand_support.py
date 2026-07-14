"""Demand-zone support quality scoring.

The detector intentionally starts from support-zone evidence instead of box
geometry:

1. Find local low pivots in the recent weekly window.
2. Cluster nearby pivot prices into demand zones.
3. Count separated touches of the strongest zone.
4. Score rebound, penetration, box height, and time span.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from winstan.config import AppConfig
from winstan.rules.base_oscillation import compute_base_oscillation_quality


LOOKBACK_WEEKS = 30
PIVOT_RADIUS = 2
SUPPORT_TOLERANCE_PCT = 1.5
REBOUND_LOOKAHEAD_WEEKS = 5
MIN_REBOUND_PCT = 5.0
BOX_LOOKBACK_WEEKS = 20
VOLUME_LOOKBACK_WEEKS = 20
MIN_VALID_TOUCHES = 2
MIN_CANDIDATE_SCORE = 70.0


@dataclass(frozen=True)
class TouchEvent:
    index: int
    date: object
    low: float
    rebound_pct: float | None
    rebound_success: bool | None
    penetration_pct: float
    volume_ratio: float | None


def compute_demand_support_quality(
    recent: pd.DataFrame,
    config: AppConfig | None = None,
    daily: pd.DataFrame | None = None,
) -> dict[str, object]:
    """Compute large-base support quality from weekly OHLCV data.

    ``config`` and ``daily`` are accepted for API symmetry with other rule
    modules.  The implementation now delegates to the large-base oscillation
    model: support touches + full touch-to-touch swings + top stability.
    """
    return compute_base_oscillation_quality(recent, daily=daily)


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
    }


def _find_pivot_lows(window: pd.DataFrame) -> list[dict[str, float | int]]:
    lows = window["low"].to_numpy(dtype=float)
    pivots: list[dict[str, float | int]] = []
    for i in range(len(window)):
        left = max(0, i - PIVOT_RADIUS)
        right = min(len(window), i + PIVOT_RADIUS + 1)
        local = lows[left:right]
        low = lows[i]
        if not np.isfinite(low):
            continue
        if low <= float(np.nanmin(local)):
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


def _build_touch_events(
    weekly: pd.DataFrame,
    window_start: int,
    support: float,
    zone_lower: float,
    zone_upper: float,
) -> list[TouchEvent]:
    in_zone = weekly.iloc[window_start:][
        (weekly.iloc[window_start:]["low"] >= zone_lower)
        & (weekly.iloc[window_start:]["low"] <= zone_upper)
    ]
    if in_zone.empty:
        return []

    events: list[TouchEvent] = []
    current_group: list[int] = []
    previous_idx: int | None = None
    for idx in in_zone.index.tolist():
        if previous_idx is None or idx - previous_idx <= 1:
            current_group.append(idx)
        else:
            events.append(_event_from_group(weekly, current_group, support))
            current_group = [idx]
        previous_idx = idx
    if current_group:
        events.append(_event_from_group(weekly, current_group, support))
    return events


def _event_from_group(weekly: pd.DataFrame, group: list[int], support: float) -> TouchEvent:
    lows = weekly.loc[group, "low"]
    idx = int(lows.idxmin())
    low = float(weekly.loc[idx, "low"])
    end_idx = min(len(weekly) - 1, idx + REBOUND_LOOKAHEAD_WEEKS)
    future = weekly.iloc[idx : end_idx + 1]
    rebound_pct: float | None = None
    rebound_success: bool | None = None
    if len(future) >= 2 and low > 0:
        future_high = float(future["high"].max())
        rebound_pct = (future_high / low - 1.0) * 100.0
        rebound_success = rebound_pct >= MIN_REBOUND_PCT
    penetration_pct = max(0.0, (support - low) / support * 100.0) if support > 0 else 0.0
    volume_ratio = _touch_volume_ratio(weekly, idx)
    return TouchEvent(
        index=idx,
        date=weekly.loc[idx, "trade_date"],
        low=low,
        rebound_pct=rebound_pct,
        rebound_success=rebound_success,
        penetration_pct=penetration_pct,
        volume_ratio=volume_ratio,
    )


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


def _score_touch_count(touch_count: int) -> float:
    if touch_count >= 5:
        return 30.0
    if touch_count == 4:
        return 28.0
    if touch_count == 3:
        return 24.0
    if touch_count == 2:
        return 15.0
    if touch_count == 1:
        return 6.0
    return 0.0


def _score_rebounds(evaluable: list[TouchEvent], successful: list[TouchEvent]) -> float:
    if not evaluable:
        return 0.0
    success_ratio = len(successful) / len(evaluable)
    avg_rebound = _mean([touch.rebound_pct for touch in evaluable if touch.rebound_pct is not None]) or 0.0
    ratio_score = success_ratio * 20.0
    strength_score = min(max(avg_rebound, 0.0) / 10.0 * 5.0, 5.0)
    return min(25.0, ratio_score + strength_score)


def _score_penetration(avg_penetration_pct: float | None) -> float:
    if avg_penetration_pct is None:
        return 0.0
    if avg_penetration_pct <= 0.5:
        return 20.0
    if avg_penetration_pct <= 1.0:
        return 18.0
    if avg_penetration_pct <= 2.0:
        return 14.0
    if avg_penetration_pct <= 4.0:
        return 8.0
    return max(0.0, 8.0 - (avg_penetration_pct - 4.0) * 2.0)


def _compute_box_height_pct(weekly: pd.DataFrame) -> float | None:
    window = weekly.tail(min(BOX_LOOKBACK_WEEKS, len(weekly)))
    if window.empty:
        return None
    mean_price = float(window["close"].mean())
    if mean_price <= 0:
        return None
    return (float(window["high"].max()) - float(window["low"].min())) / mean_price * 100.0


def _score_box_height(box_height_pct: float | None) -> float:
    if box_height_pct is None:
        return 0.0
    if box_height_pct <= 12.0:
        return 15.0
    if box_height_pct <= 15.0:
        return 13.0
    if box_height_pct <= 20.0:
        return 10.0
    if box_height_pct <= 25.0:
        return 6.0
    if box_height_pct <= 35.0:
        return 3.0
    return 0.0


def _touch_span_weeks(touches: list[TouchEvent]) -> int:
    if len(touches) < 2:
        return 0
    return int(touches[-1].index - touches[0].index + 1)


def _score_duration(duration_weeks: int) -> float:
    if duration_weeks >= 30:
        return 10.0
    if duration_weeks >= 24:
        return 9.0
    if duration_weeks >= 20:
        return 8.0
    if duration_weeks >= 14:
        return 6.0
    if duration_weeks >= 10:
        return 5.0
    if duration_weeks >= 6:
        return 3.0
    return 0.0


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
    success_rate: float | None,
    avg_rebound: float | None,
    avg_penetration: float | None,
    box_height_pct: float | None,
    duration_weeks: int,
    pending_count: int,
) -> str:
    parts = [f"{grade} demand zone {total:.0f}", f"touches {touch_count}"]
    if success_rate is not None:
        parts.append(f"rebound {success_rate:.0f}%")
    if avg_rebound is not None:
        parts.append(f"avg rebound {avg_rebound:.1f}%")
    if avg_penetration is not None:
        parts.append(f"penetration {avg_penetration:.2f}%")
    if box_height_pct is not None:
        parts.append(f"box {box_height_pct:.1f}%")
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
