"""Post-decline quiet-base / low-volatility accumulation-base scoring.

The model does not claim to observe accumulation directly. It scores the
observable trace described by the expert: a prior decline followed by a quiet,
low-level, low-volume base.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from winstan.config import AppConfig


PRIOR_DECLINE_LOOKBACK_DAILY = 300
PRIOR_DECLINE_LOOKBACK_WEEKLY = 60
VOLUME_RECENT_BARS = 10
VOLUME_BASELINE_BARS = 10
LIQUIDITY_LOOKBACK_BARS = 20
RECENT_BREAK_LOOKBACK_BARS = 20
RECENT_STABILITY_BARS = 10
MIN_BASE_DURATION_WEEKS = 6
MAX_BASE_DURATION_WEEKS = 40
MAX_BASE_CLOSE_RANGE_PCT = 8.0
BOTTOM_BONUS_CLOSE_RANGE_PCT = 5.0
SUPPORT_ZONE_LOWER_QUANTILE = 0.10
SUPPORT_PRICE_QUANTILE = 0.20
SUPPORT_ZONE_UPPER_QUANTILE = 0.35
BOX_TOP_QUANTILE = 0.90
MAX_CANDIDATE_VOLUME_RATIO = 0.85


@dataclass(frozen=True)
class QuietBaseWindow:
    start_idx: int
    end_idx: int
    start_date: object | None
    end_date: object | None
    duration_bars: int
    duration_weeks: int
    low: float | None
    high: float | None
    support_price: float | None
    zone_lower: float | None
    zone_upper: float | None
    close_top: float | None
    close_range_pct: float | None
    avg_abs_change_pct: float | None
    recent_close_range_pct: float | None
    drift_pct: float | None


def compute_low_base_quality(
    recent: pd.DataFrame,
    config: AppConfig | None = None,
    daily: pd.DataFrame | None = None,
) -> dict[str, object]:
    del config
    source = daily if daily is not None and not daily.empty else recent
    source_label = "daily" if daily is not None and not daily.empty else "weekly"
    bars = _prepare_bars(source)
    if bars.empty:
        return _default_result("insufficient data")

    base = _find_quiet_base_window(bars, source_label)
    support = base.support_price
    zone_lower = base.zone_lower
    zone_upper = base.zone_upper
    box_top = base.close_top
    if (
        base.duration_bars <= 0
        or support is None
        or zone_lower is None
        or zone_upper is None
        or box_top is None
        or support <= 0
    ):
        return _default_result("no quiet base window")

    prior_decline_pct, prior_start, prior_end = _prior_decline_metrics(
        bars,
        base.start_idx,
        support,
        source_label,
    )
    quietness_value = base.close_range_pct
    volume_ratio, volume_recent, volume_baseline = _volume_decay_metrics(bars, base.start_idx)
    recent_amount_avg = _recent_amount_avg(bars)
    touches = _support_touches(bars, base.start_idx, zone_lower, zone_upper, source_label)
    touch_count = len(touches)
    touch_low_progress_pct = _touch_low_progress_pct(touches)
    recent_intraday_break_pct = _recent_break_pct(bars, zone_lower, support, field="low")
    recent_close_break_pct = _recent_break_pct(bars, zone_lower, support, field="close") or 0.0
    false_break_count = _false_break_count(bars, base.start_idx, zone_lower)
    latest_close = _to_float(bars.iloc[-1].get("close"))
    distance_to_top_pct = _distance_to_top_pct(latest_close, box_top)
    ema_flat_score, ema_slope_20_pct = _ema_flat_score(bars, source_label)
    direction_volume_ratio, direction_latest_volume, direction_base_avg_volume = _direction_volume_metrics(
        bars,
        base.start_idx,
    )
    direction_baseline_end_date = bars.iloc[-2].get("trade_date") if len(bars) >= 2 else None
    direction_volume_score = _score_direction_volume(direction_volume_ratio)
    approach_gap_pct = _approach_gap_pct(latest_close, support)
    avg_penetration_pct = _avg_close_penetration_pct(bars, base.start_idx, zone_lower, support)
    avg_swing_pct = _base_swing_pct(support, box_top)

    prior_score = _score_prior_decline(prior_decline_pct)
    duration_score = _score_base_duration(base.duration_weeks)
    volatility_score = _score_quiet_base(
        close_range_pct=base.close_range_pct,
        avg_abs_change_pct=base.avg_abs_change_pct,
        source_label=source_label,
    )
    volume_score = 0.0
    bottom_score = _score_bottom_stability(
        close_range_pct=base.close_range_pct,
        recent_close_range_pct=base.recent_close_range_pct,
        recent_close_break_pct=recent_close_break_pct,
    )
    breakout_score = _score_breakout_readiness(
        distance_to_top_pct=distance_to_top_pct,
        ema_flat_score=ema_flat_score,
        direction_volume_score=direction_volume_score,
        recent_intraday_break_pct=recent_intraday_break_pct,
        recent_close_break_pct=recent_close_break_pct,
    )
    total = round(
        min(
            100.0,
            max(0.0, prior_score + duration_score + volatility_score + bottom_score + breakout_score),
        ),
        1,
    )

    support_active = bool(
        base.close_range_pct is not None
        and base.close_range_pct <= MAX_BASE_CLOSE_RANGE_PCT
        and base.drift_pct is not None
        and base.drift_pct <= _max_base_drift_pct(source_label)
    )
    candidate = (
        prior_decline_pct is not None
        and prior_decline_pct >= 25.0
        and base.duration_weeks >= MIN_BASE_DURATION_WEEKS
        and support_active
        and volume_ratio is not None
        and volume_ratio <= MAX_CANDIDATE_VOLUME_RATIO
    )

    grade = _grade(total)
    reason = _build_reason(
        grade=grade,
        total=total,
        prior_score=prior_score,
        duration_score=duration_score,
        volatility_score=volatility_score,
        bottom_score=bottom_score,
        breakout_score=breakout_score,
        prior_decline_pct=prior_decline_pct,
        prior_range=_format_range(prior_start, prior_end),
        base_range=_format_range(base.start_date, base.end_date),
        close_range_pct=base.close_range_pct,
        avg_abs_change_pct=base.avg_abs_change_pct,
        recent_close_range_pct=base.recent_close_range_pct,
        quietness_value=quietness_value,
        volume_ratio=volume_ratio,
        volume_range=_volume_decay_range(base.start_date, source_label),
        recent_amount_avg=recent_amount_avg,
        direction_volume_ratio=direction_volume_ratio,
        direction_volume_score=direction_volume_score,
        touch_count=touch_count,
        touch_low_progress_pct=touch_low_progress_pct,
    )
    support_reason = _support_reason(
        close_range_pct=base.close_range_pct,
        recent_close_range_pct=base.recent_close_range_pct,
        approach_gap_pct=approach_gap_pct,
        drift_pct=base.drift_pct,
    )

    return {
        "low_base_score": total,
        "low_base_grade": grade,
        "low_base_reason": reason,
        "low_base_candidate": bool(candidate),
        "low_base_support_price": round(support, 4),
        "low_base_lower": round(zone_lower, 4),
        "low_base_upper": round(zone_upper, 4),
        "low_base_top_price": round(box_top, 4),
        "low_base_base_start_date": _format_date(base.start_date),
        "low_base_base_end_date": _format_date(base.end_date),
        "low_base_base_range": _format_range(base.start_date, base.end_date),
        "low_base_duration_bars": int(base.duration_bars),
        "low_base_duration_weeks": int(base.duration_weeks),
        "low_base_duration_unit": source_label,
        "low_base_score_prior_decline": round(prior_score, 1),
        "low_base_score_duration": round(duration_score, 1),
        "low_base_score_volatility": round(volatility_score, 1),
        "low_base_score_volume": round(volume_score, 1),
        "low_base_score_bottom_stability": round(bottom_score, 1),
        "low_base_score_breakout_readiness": round(breakout_score, 1),
        "low_base_prior_decline_pct": round(prior_decline_pct, 2) if prior_decline_pct is not None else None,
        "low_base_prior_decline_start_date": _format_date(prior_start),
        "low_base_prior_decline_end_date": _format_date(prior_end),
        "low_base_prior_decline_range": _format_range(prior_start, prior_end),
        "low_base_volatility_contraction_ratio": round(quietness_value, 2) if quietness_value is not None else None,
        "low_base_volatility_recent_pct": (
            round(base.recent_close_range_pct, 2) if base.recent_close_range_pct is not None else None
        ),
        "low_base_volatility_baseline_pct": (
            round(base.avg_abs_change_pct, 2) if base.avg_abs_change_pct is not None else None
        ),
        "low_base_volatility_range": _quiet_base_range(source_label),
        "low_base_base_close_range_pct": round(base.close_range_pct, 2) if base.close_range_pct is not None else None,
        "low_base_base_avg_abs_change_pct": (
            round(base.avg_abs_change_pct, 2) if base.avg_abs_change_pct is not None else None
        ),
        "low_base_recent_close_range_pct": (
            round(base.recent_close_range_pct, 2) if base.recent_close_range_pct is not None else None
        ),
        "low_base_base_drift_pct": round(base.drift_pct, 2) if base.drift_pct is not None else None,
        "low_base_volume_decay_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "low_base_volume_recent_avg": round(volume_recent, 2) if volume_recent is not None else None,
        "low_base_volume_baseline_avg": round(volume_baseline, 2) if volume_baseline is not None else None,
        "low_base_volume_range": _volume_decay_range(base.start_date, source_label),
        "low_base_recent_amount_avg": round(recent_amount_avg, 2) if recent_amount_avg is not None else None,
        "low_base_liquidity_threshold": None,
        "low_base_liquidity_ok": None,
        "low_base_liquidity_range": f"近{LIQUIDITY_LOOKBACK_BARS}{_unit_label(source_label)}均成交额，仅展示不否决",
        "low_base_direction_volume_ratio": (
            round(direction_volume_ratio, 2) if direction_volume_ratio is not None else None
        ),
        "low_base_direction_latest_volume": (
            round(direction_latest_volume, 2) if direction_latest_volume is not None else None
        ),
        "low_base_direction_base_avg_volume": (
            round(direction_base_avg_volume, 2) if direction_base_avg_volume is not None else None
        ),
        "low_base_score_direction_volume": round(direction_volume_score, 1),
        "low_base_direction_volume_range": _direction_volume_range(
            base.start_date,
            direction_baseline_end_date,
            base.end_date,
            source_label,
        ),
        "low_base_touch_count": int(touch_count),
        "low_base_touch_low_progress_pct": (
            round(touch_low_progress_pct, 2) if touch_low_progress_pct is not None else None
        ),
        "low_base_recent_intraday_break_pct": (
            round(recent_intraday_break_pct, 2) if recent_intraday_break_pct is not None else None
        ),
        "low_base_recent_close_break_pct": round(recent_close_break_pct, 2),
        "low_base_false_break_count": int(false_break_count),
        "low_base_bottom_stability_range": _bottom_range(
            close_range_pct=base.close_range_pct,
            recent_close_range_pct=base.recent_close_range_pct,
            recent_close_break_pct=recent_close_break_pct,
            latest_close=latest_close,
            support=support,
        ),
        "low_base_distance_to_top_pct": round(distance_to_top_pct, 2) if distance_to_top_pct is not None else None,
        "low_base_ema_slope_20_pct": round(ema_slope_20_pct, 2) if ema_slope_20_pct is not None else None,
        "low_base_abnormal_volume_ratio": (
            round(direction_volume_ratio, 2) if direction_volume_ratio is not None else None
        ),
        "low_base_breakout_readiness_range": _breakout_range(
            distance_to_top_pct,
            ema_slope_20_pct,
            direction_volume_ratio,
            recent_intraday_break_pct,
        ),
        "low_base_support_score": round(volatility_score + bottom_score, 1),
        "low_base_support_grade": "A" if support_active else "C",
        "low_base_support_reason": support_reason,
        "low_base_approach_gap_pct": round(approach_gap_pct, 2) if approach_gap_pct is not None else None,
        "low_base_avg_penetration_pct": round(avg_penetration_pct, 2) if avg_penetration_pct is not None else None,
        "low_base_avg_swing_pct": round(avg_swing_pct, 2) if avg_swing_pct is not None else None,
        "low_base_support_active": support_active,
    }


def _default_result(reason: str) -> dict[str, object]:
    return {
        "low_base_score": 0.0,
        "low_base_grade": "C",
        "low_base_reason": reason,
        "low_base_candidate": False,
        "low_base_support_price": None,
        "low_base_lower": None,
        "low_base_upper": None,
        "low_base_top_price": None,
        "low_base_base_start_date": "",
        "low_base_base_end_date": "",
        "low_base_base_range": "",
        "low_base_duration_bars": 0,
        "low_base_duration_weeks": 0,
        "low_base_duration_unit": "",
        "low_base_score_prior_decline": 0.0,
        "low_base_score_duration": 0.0,
        "low_base_score_volatility": 0.0,
        "low_base_score_volume": 0.0,
        "low_base_score_bottom_stability": 0.0,
        "low_base_score_breakout_readiness": 0.0,
        "low_base_prior_decline_pct": None,
        "low_base_prior_decline_start_date": "",
        "low_base_prior_decline_end_date": "",
        "low_base_prior_decline_range": "",
        "low_base_volatility_contraction_ratio": None,
        "low_base_volatility_recent_pct": None,
        "low_base_volatility_baseline_pct": None,
        "low_base_volatility_range": "",
        "low_base_base_close_range_pct": None,
        "low_base_base_avg_abs_change_pct": None,
        "low_base_recent_close_range_pct": None,
        "low_base_base_drift_pct": None,
        "low_base_volume_decay_ratio": None,
        "low_base_volume_recent_avg": None,
        "low_base_volume_baseline_avg": None,
        "low_base_volume_range": "",
        "low_base_recent_amount_avg": None,
        "low_base_liquidity_threshold": None,
        "low_base_liquidity_ok": None,
        "low_base_liquidity_range": "",
        "low_base_direction_volume_ratio": None,
        "low_base_direction_latest_volume": None,
        "low_base_direction_base_avg_volume": None,
        "low_base_score_direction_volume": 0.0,
        "low_base_direction_volume_range": "",
        "low_base_touch_count": 0,
        "low_base_touch_low_progress_pct": None,
        "low_base_recent_intraday_break_pct": None,
        "low_base_recent_close_break_pct": None,
        "low_base_false_break_count": 0,
        "low_base_bottom_stability_range": "",
        "low_base_distance_to_top_pct": None,
        "low_base_ema_slope_20_pct": None,
        "low_base_abnormal_volume_ratio": None,
        "low_base_breakout_readiness_range": "",
        "low_base_support_score": 0.0,
        "low_base_support_grade": "C",
        "low_base_support_reason": "",
        "low_base_approach_gap_pct": None,
        "low_base_avg_penetration_pct": None,
        "low_base_avg_swing_pct": None,
        "low_base_support_active": False,
    }


def _prepare_bars(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "high", "low", "close"}
    if frame.empty or not required.issubset(frame.columns):
        return pd.DataFrame()
    bars = frame.copy().sort_values("trade_date").reset_index(drop=True)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume", "amount"]:
        if column in bars.columns:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
    return bars.dropna(subset=["trade_date", "high", "low", "close"]).reset_index(drop=True)


def _find_quiet_base_window(
    bars: pd.DataFrame,
    source_label: str,
) -> QuietBaseWindow:
    bars_per_week = 5 if source_label == "daily" else 1
    min_base_bars = MIN_BASE_DURATION_WEEKS * bars_per_week
    max_base_bars = min(len(bars), MAX_BASE_DURATION_WEEKS * bars_per_week)
    if len(bars) < min_base_bars:
        return _empty_base_window()

    end_idx = len(bars) - 1
    best_window: QuietBaseWindow | None = None
    best_key: tuple[float, ...] | None = None

    for duration_bars in range(min_base_bars, max_base_bars + 1):
        start_idx = end_idx - duration_bars + 1
        if start_idx < 0:
            continue
        segment = bars.iloc[start_idx : end_idx + 1]
        candidate = _build_quiet_base_window(segment, start_idx, end_idx, source_label)
        if candidate is None:
            continue
        quiet_ok = (
            candidate.close_range_pct is not None
            and candidate.close_range_pct <= MAX_BASE_CLOSE_RANGE_PCT
            and candidate.drift_pct is not None
            and candidate.drift_pct <= _max_base_drift_pct(source_label)
        )
        key = (
            1.0 if quiet_ok else 0.0,
            -(candidate.close_range_pct or 999.0),
            -(candidate.recent_close_range_pct or 999.0),
            float(candidate.duration_bars),
            -(candidate.avg_abs_change_pct or 999.0),
            -(candidate.drift_pct or 999.0),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_window = candidate
    return best_window or _empty_base_window()


def _build_quiet_base_window(
    segment: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    source_label: str,
) -> QuietBaseWindow | None:
    closes = pd.to_numeric(segment["close"], errors="coerce").dropna()
    if len(closes) < 2:
        return None
    close_low = _to_float(closes.min())
    close_high = _to_float(closes.max())
    if close_low is None or close_high is None or close_low <= 0:
        return None

    close_range_pct = _series_range_pct(closes)
    avg_abs_change_pct = _average_abs_change_pct(closes)
    recent_close_range_pct = _series_range_pct(closes.tail(min(RECENT_STABILITY_BARS, len(closes))))
    first_close = _to_float(closes.iloc[0])
    last_close = _to_float(closes.iloc[-1])
    drift_pct = None
    if first_close is not None and first_close > 0 and last_close is not None:
        drift_pct = abs(last_close / first_close - 1.0) * 100.0

    support_price = _series_quantile(closes, SUPPORT_PRICE_QUANTILE) or close_low
    zone_lower = _series_quantile(closes, SUPPORT_ZONE_LOWER_QUANTILE) or close_low
    zone_upper = _series_quantile(closes, SUPPORT_ZONE_UPPER_QUANTILE) or support_price
    close_top = _series_quantile(closes, BOX_TOP_QUANTILE) or close_high

    support_price = max(support_price, zone_lower)
    zone_upper = max(zone_upper, support_price)
    close_top = max(close_top, zone_upper)
    if zone_upper <= zone_lower:
        zone_upper = zone_lower * 1.003

    duration_bars = int(len(segment))
    duration_weeks = max(1, int(round(duration_bars / 5.0))) if source_label == "daily" else duration_bars
    lows = pd.to_numeric(segment["low"], errors="coerce")
    highs = pd.to_numeric(segment["high"], errors="coerce")
    return QuietBaseWindow(
        start_idx=start_idx,
        end_idx=end_idx,
        start_date=segment.iloc[0].get("trade_date") if not segment.empty else None,
        end_date=segment.iloc[-1].get("trade_date") if not segment.empty else None,
        duration_bars=duration_bars,
        duration_weeks=duration_weeks,
        low=_to_float(lows.min()),
        high=_to_float(highs.max()),
        support_price=support_price,
        zone_lower=zone_lower,
        zone_upper=zone_upper,
        close_top=close_top,
        close_range_pct=close_range_pct,
        avg_abs_change_pct=avg_abs_change_pct,
        recent_close_range_pct=recent_close_range_pct,
        drift_pct=drift_pct,
    )


def _empty_base_window() -> QuietBaseWindow:
    return QuietBaseWindow(
        start_idx=0,
        end_idx=-1,
        start_date=None,
        end_date=None,
        duration_bars=0,
        duration_weeks=0,
        low=None,
        high=None,
        support_price=None,
        zone_lower=None,
        zone_upper=None,
        close_top=None,
        close_range_pct=None,
        avg_abs_change_pct=None,
        recent_close_range_pct=None,
        drift_pct=None,
    )


def _prior_decline_metrics(
    bars: pd.DataFrame,
    base_start_idx: int,
    support: float,
    source_label: str,
) -> tuple[float | None, object | None, object | None]:
    if base_start_idx <= 0 or support <= 0:
        return None, None, None
    lookback = PRIOR_DECLINE_LOOKBACK_DAILY if source_label == "daily" else PRIOR_DECLINE_LOOKBACK_WEEKLY
    start = max(0, base_start_idx - lookback)
    prior = bars.iloc[start : base_start_idx + 1].copy()
    if prior.empty:
        return None, None, None
    highs = pd.to_numeric(prior["high"], errors="coerce")
    if highs.dropna().empty:
        return None, None, None
    peak_idx = highs.idxmax()
    peak = float(highs.loc[peak_idx])
    if not np.isfinite(peak) or peak <= support:
        return None, None, None
    return (peak / support - 1.0) * 100.0, bars.at[peak_idx, "trade_date"], bars.at[base_start_idx, "trade_date"]


def _volume_decay_metrics(
    bars: pd.DataFrame,
    base_start_idx: int,
) -> tuple[float | None, float | None, float | None]:
    if "volume" not in bars.columns:
        return None, None, None
    values = pd.to_numeric(bars["volume"], errors="coerce")
    recent = values.tail(min(VOLUME_RECENT_BARS, len(values))).dropna()
    if recent.empty:
        return None, None, None
    baseline_start = max(0, int(base_start_idx) - VOLUME_BASELINE_BARS)
    baseline_end = min(len(values), int(base_start_idx) + 1)
    baseline = values.iloc[baseline_start:baseline_end].dropna()
    recent_mean = float(recent.mean())
    baseline_mean = float(baseline.mean()) if not baseline.empty else None
    if baseline_mean is None or baseline_mean <= 0:
        return None, recent_mean, baseline_mean
    return recent_mean / baseline_mean, recent_mean, baseline_mean


def _recent_amount_avg(bars: pd.DataFrame) -> float | None:
    if "amount" not in bars.columns:
        return None
    values = pd.to_numeric(bars["amount"], errors="coerce").dropna()
    if values.empty:
        return None
    recent = values.tail(min(LIQUIDITY_LOOKBACK_BARS, len(values)))
    return float(recent.mean())


def _direction_volume_metrics(
    bars: pd.DataFrame,
    base_start_idx: int,
) -> tuple[float | None, float | None, float | None]:
    if "volume" not in bars.columns or len(bars) < 2:
        return None, None, None
    values = pd.to_numeric(bars["volume"], errors="coerce")
    latest = _to_float(values.iloc[-1])
    if latest is None:
        return None, None, None
    baseline_start = max(0, min(int(base_start_idx), len(values) - 2))
    baseline = values.iloc[baseline_start:-1].dropna()
    if baseline.empty:
        return None, latest, None
    baseline_mean = float(baseline.mean())
    if baseline_mean <= 0:
        return None, latest, baseline_mean
    return latest / baseline_mean, latest, baseline_mean


def _support_touches(
    bars: pd.DataFrame,
    base_start_idx: int,
    zone_lower: float,
    zone_upper: float,
    source_label: str,
) -> list[tuple[int, float]]:
    segment = bars.iloc[base_start_idx:].copy()
    closes = pd.to_numeric(segment["close"], errors="coerce")
    mask = (closes <= zone_upper) & (closes >= zone_lower * 0.985)
    raw_indices = segment.index[mask.fillna(False)].tolist()
    if not raw_indices:
        return []
    merge_gap = 5 if source_label == "daily" else 1
    groups: list[list[int]] = []
    current: list[int] = []
    previous: int | None = None
    for idx in raw_indices:
        if previous is None or idx - previous <= merge_gap:
            current.append(idx)
        else:
            groups.append(current)
            current = [idx]
        previous = idx
    if current:
        groups.append(current)
    touches: list[tuple[int, float]] = []
    for group in groups:
        closes_in_group = pd.to_numeric(bars.loc[group, "close"], errors="coerce")
        min_idx = int(closes_in_group.idxmin())
        touches.append((min_idx, float(bars.at[min_idx, "close"])))
    return touches


def _touch_low_progress_pct(touches: list[tuple[int, float]]) -> float | None:
    if len(touches) < 2:
        return None
    first = touches[0][1]
    last = touches[-1][1]
    if first <= 0:
        return None
    return (last / first - 1.0) * 100.0


def _recent_break_pct(bars: pd.DataFrame, zone_lower: float, support: float, *, field: str) -> float | None:
    if field not in bars.columns or support <= 0:
        return None
    recent = bars.tail(min(RECENT_BREAK_LOOKBACK_BARS, len(bars)))
    values = pd.to_numeric(recent[field], errors="coerce").dropna()
    if values.empty:
        return None
    return max(0.0, float(((zone_lower - values) / support * 100.0).max()))


def _false_break_count(bars: pd.DataFrame, base_start_idx: int, zone_lower: float) -> int:
    segment = bars.iloc[base_start_idx:].copy()
    if segment.empty:
        return 0
    lows = pd.to_numeric(segment["low"], errors="coerce")
    closes = pd.to_numeric(segment["close"], errors="coerce")
    return int(((lows < zone_lower) & (closes >= zone_lower)).sum())


def _distance_to_top_pct(latest_close: float | None, top_price: float | None) -> float | None:
    if latest_close is None or top_price is None or latest_close <= 0 or top_price <= 0:
        return None
    return (top_price / latest_close - 1.0) * 100.0


def _ema_flat_score(bars: pd.DataFrame, source_label: str) -> tuple[float, float | None]:
    closes = pd.to_numeric(bars["close"], errors="coerce").dropna()
    if len(closes) < 30:
        return 0.0, None
    span = 144 if source_label == "daily" else 30
    ema = closes.ewm(span=span, min_periods=1).mean()
    if len(ema) < 21 or float(ema.iloc[-21]) <= 0:
        return 0.0, None
    slope = (float(ema.iloc[-1]) / float(ema.iloc[-21]) - 1.0) * 100.0
    if slope >= 1.5:
        score = 5.0
    elif slope >= 0.0:
        score = 4.0
    elif slope >= -1.5:
        score = 3.0
    elif slope >= -4.0:
        score = 1.5
    else:
        score = 0.0
    return score, slope


def _score_prior_decline(value: float | None) -> float:
    if value is None:
        return 0.0
    if value > 60.0:
        return 15.0
    if value >= 40.0:
        return 12.75
    if value >= 25.0:
        return 9.0
    if value >= 15.0:
        return 4.5
    return 0.0


def _score_base_duration(weeks: int) -> float:
    if weeks < MIN_BASE_DURATION_WEEKS:
        return 0.0
    if weeks < 8:
        return 6.0
    if weeks < 16:
        return 12.0
    if weeks <= 30:
        return 20.0
    if weeks <= 40:
        return 16.0
    return 10.0


def _score_quiet_base(
    *,
    close_range_pct: float | None,
    avg_abs_change_pct: float | None,
    source_label: str,
) -> float:
    if close_range_pct is None:
        return 0.0
    scale = 1.0 if source_label == "daily" else 1.8
    if close_range_pct <= 3.5 * scale:
        range_score = 15.0
    elif close_range_pct <= BOTTOM_BONUS_CLOSE_RANGE_PCT * scale:
        range_score = 13.0
    elif close_range_pct <= 6.5 * scale:
        range_score = 10.0
    elif close_range_pct <= MAX_BASE_CLOSE_RANGE_PCT * scale:
        range_score = 7.0
    elif close_range_pct <= 10.0 * scale:
        range_score = 3.0
    else:
        range_score = 0.0

    if avg_abs_change_pct is None:
        noise_score = 0.0
    elif avg_abs_change_pct <= 0.9 * scale:
        noise_score = 10.0
    elif avg_abs_change_pct <= 1.2 * scale:
        noise_score = 8.0
    elif avg_abs_change_pct <= 1.6 * scale:
        noise_score = 6.0
    elif avg_abs_change_pct <= 2.0 * scale:
        noise_score = 4.0
    elif avg_abs_change_pct <= 2.4 * scale:
        noise_score = 2.0
    else:
        noise_score = 0.0
    return min(25.0, range_score + noise_score)


def _score_direction_volume(ratio: float | None) -> float:
    if ratio is None:
        return 0.0
    if ratio >= 3.0:
        return 5.0
    if ratio >= 2.0:
        return 4.0
    if ratio >= 1.5:
        return 3.0
    if ratio >= 1.2:
        return 2.0
    if ratio >= 1.0:
        return 1.0
    return 0.0


def _score_bottom_stability(
    *,
    close_range_pct: float | None,
    recent_close_range_pct: float | None,
    recent_close_break_pct: float,
) -> float:
    if close_range_pct is None:
        return 0.0
    if close_range_pct <= 6.0:
        range_bonus = 16.0
    elif close_range_pct <= 8.0:
        range_bonus = 14.0
    elif close_range_pct <= 10.0:
        range_bonus = 11.0
    elif close_range_pct <= 12.0:
        range_bonus = 6.0
    else:
        range_bonus = 0.0

    if recent_close_range_pct is None:
        recent_bonus = 0.0
    elif recent_close_range_pct <= 1.5:
        recent_bonus = 5.0
    elif recent_close_range_pct <= 2.5:
        recent_bonus = 4.0
    elif recent_close_range_pct <= 3.5:
        recent_bonus = 3.0
    elif recent_close_range_pct <= 5.0:
        recent_bonus = 1.0
    else:
        recent_bonus = 0.0

    if recent_close_break_pct <= 0.0:
        support_hold_bonus = 4.0
    elif recent_close_break_pct <= 1.0:
        support_hold_bonus = 2.0
    else:
        support_hold_bonus = 0.0
    return min(25.0, range_bonus + recent_bonus + support_hold_bonus)


def _score_breakout_readiness(
    *,
    distance_to_top_pct: float | None,
    ema_flat_score: float,
    direction_volume_score: float,
    recent_intraday_break_pct: float | None,
    recent_close_break_pct: float,
) -> float:
    if distance_to_top_pct is None:
        distance_score = 0.0
    elif distance_to_top_pct <= 0.0:
        distance_score = 1.0
    elif distance_to_top_pct <= 5.0:
        distance_score = 5.0
    elif distance_to_top_pct <= 12.0:
        distance_score = 4.0
    elif distance_to_top_pct <= 25.0:
        distance_score = 2.0
    else:
        distance_score = 0.0

    if recent_intraday_break_pct is None:
        recovery_score = 0.5
    else:
        recovered = max(0.0, recent_intraday_break_pct - max(recent_close_break_pct, 0.0))
        recovery_score = 2.0 if recovered >= 1.0 else 1.0 if recovered > 0.0 else 0.5

    return min(
        15.0,
        distance_score
        + min(max(ema_flat_score, 0.0), 5.0)
        + min(max(direction_volume_score, 0.0), 5.0)
        + recovery_score,
    )


def _grade(score: float) -> str:
    if score >= 85.0:
        return "S"
    if score >= 75.0:
        return "A"
    if score >= 60.0:
        return "B"
    return "C"


def _build_reason(**kwargs: object) -> str:
    parts = [
        f"{kwargs['grade']} 静默基底 {float(kwargs['total']):.0f}",
        f"前期下跌 {float(kwargs['prior_score']):.1f}/15",
        f"横盘持续 {float(kwargs['duration_score']):.1f}/20",
        f"横盘静默 {float(kwargs['volatility_score']):.1f}/25",
        f"底部稳定加分 {float(kwargs['bottom_score']):.1f}/25",
        f"突破准备 {float(kwargs['breakout_score']):.1f}/15",
    ]
    prior_decline_pct = kwargs.get("prior_decline_pct")
    if prior_decline_pct is not None:
        parts.append(f"前期区间 {kwargs.get('prior_range') or '--'} 跌幅 {float(prior_decline_pct):.1f}%")
    parts.append(f"横盘区间 {kwargs.get('base_range') or '--'}")
    close_range_pct = kwargs.get("close_range_pct")
    avg_abs_change_pct = kwargs.get("avg_abs_change_pct")
    recent_close_range_pct = kwargs.get("recent_close_range_pct")
    if close_range_pct is not None:
        recent_text = "--" if recent_close_range_pct is None else f"{float(recent_close_range_pct):.2f}%"
        avg_text = "--" if avg_abs_change_pct is None else f"{float(avg_abs_change_pct):.2f}%"
        parts.append(
            f"收盘振幅 {float(close_range_pct):.2f}% / 近{RECENT_STABILITY_BARS}振幅 {recent_text} / 单根均变动 {avg_text}"
        )
    quietness_value = kwargs.get("quietness_value")
    if quietness_value is not None:
        parts.append(f"整体振幅 {float(quietness_value):.2f}%")
    volume_ratio = kwargs.get("volume_ratio")
    if volume_ratio is not None:
        parts.append(f"量能参考 {kwargs.get('volume_range') or '--'} {float(volume_ratio):.2f}x（不计分）")
    recent_amount_avg = kwargs.get("recent_amount_avg")
    if recent_amount_avg is not None:
        parts.append(f"近20均成交额 {float(recent_amount_avg) / 100000000.0:.2f}亿")
    direction_volume_ratio = kwargs.get("direction_volume_ratio")
    if direction_volume_ratio is not None:
        parts.append(
            f"方向量能 {float(direction_volume_ratio):.2f}x / 加分 {float(kwargs.get('direction_volume_score') or 0):.1f}"
        )
    touch_progress = kwargs.get("touch_low_progress_pct")
    progress_text = "--" if touch_progress is None else f"{float(touch_progress):.1f}%"
    parts.append(f"触底{int(kwargs.get('touch_count') or 0)}次 / 低点进展 {progress_text}")
    return " / ".join(parts)


def _bottom_range(
    *,
    close_range_pct: float | None,
    recent_close_range_pct: float | None,
    recent_close_break_pct: float,
    latest_close: float | None,
    support: float | None,
) -> str:
    whole = "--" if close_range_pct is None else f"{close_range_pct:.2f}%"
    recent = "--" if recent_close_range_pct is None else f"{recent_close_range_pct:.2f}%"
    approach = _approach_gap_pct(latest_close, support)
    approach_text = "--" if approach is None else f"{approach:.2f}%"
    return (
        f"横盘收盘振幅{whole} / 近{RECENT_STABILITY_BARS}收盘振幅{recent} / "
        f"近20收盘跌破{recent_close_break_pct:.2f}% / 距底部{approach_text}"
    )


def _breakout_range(
    distance_to_top_pct: float | None,
    ema_slope_20_pct: float | None,
    direction_volume_ratio: float | None,
    recent_intraday_break_pct: float | None,
) -> str:
    top = "--" if distance_to_top_pct is None else f"距箱顶{distance_to_top_pct:.2f}%"
    ema = "--" if ema_slope_20_pct is None else f"EMA斜率{ema_slope_20_pct:.2f}%"
    volume = "--" if direction_volume_ratio is None else f"方向量{direction_volume_ratio:.2f}x"
    probe = "--" if recent_intraday_break_pct is None else f"盘中下探{recent_intraday_break_pct:.2f}%"
    return f"{top} / {ema} / {volume} / {probe}"


def _direction_volume_range(
    start: object | None,
    baseline_end: object | None,
    current: object | None,
    source_label: str,
) -> str:
    unit = _unit_label(source_label)
    return (
        f"{_format_date(current) or '当前'}{unit}量 / "
        f"横盘首日至前一{unit}均量（{_format_date(start) or '--'} -> {_format_date(baseline_end) or '--'}）"
    )


def _quiet_base_range(source_label: str) -> str:
    unit = _unit_label(source_label)
    return f"横盘全区收盘振幅 / 近{RECENT_STABILITY_BARS}{unit}收盘振幅 / 单根均变动"


def _volume_decay_range(start: object | None, source_label: str) -> str:
    unit = _unit_label(source_label)
    if start is None:
        return f"近{VOLUME_RECENT_BARS}{unit}均量 / 横盘首日前{VOLUME_BASELINE_BARS}{unit}至首{unit}均量"
    return (
        f"近{VOLUME_RECENT_BARS}{unit}均量 / "
        f"横盘首日前{VOLUME_BASELINE_BARS}{unit}至首{unit}均量（到 {_format_date(start) or '--'}）"
    )


def _unit_label(source_label: str) -> str:
    return "日" if source_label == "daily" else "周"


def _format_range(start: object | None, end: object | None) -> str:
    if start is None and end is None:
        return ""
    return f"{_format_date(start)} -> {_format_date(end)}"


def _format_date(value: object | None) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return pd.Timestamp(value).date().isoformat()
    except Exception:
        return str(value)


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        result = float(value)
    except Exception:
        return None
    return result if np.isfinite(result) else None


def _series_quantile(values: pd.Series, q: float) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    return _to_float(numeric.quantile(q))


def _series_range_pct(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return None
    low = _to_float(numeric.min())
    high = _to_float(numeric.max())
    if low is None or high is None or low <= 0:
        return None
    return (high / low - 1.0) * 100.0


def _average_abs_change_pct(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if len(numeric) < 2:
        return None
    changes = numeric.pct_change().abs().dropna()
    if changes.empty:
        return None
    return float(changes.mean() * 100.0)


def _approach_gap_pct(latest_close: float | None, support: float | None) -> float | None:
    if latest_close is None or support is None or latest_close <= 0 or support <= 0:
        return None
    return (latest_close / support - 1.0) * 100.0


def _avg_close_penetration_pct(
    bars: pd.DataFrame,
    base_start_idx: int,
    zone_lower: float,
    support: float,
) -> float | None:
    if support <= 0:
        return None
    segment = bars.iloc[base_start_idx:].copy()
    closes = pd.to_numeric(segment["close"], errors="coerce").dropna()
    if closes.empty:
        return None
    penetration = ((zone_lower - closes) / support * 100.0).clip(lower=0.0)
    if penetration.empty:
        return None
    positive = penetration[penetration > 0]
    if positive.empty:
        return 0.0
    return float(positive.mean())


def _base_swing_pct(support: float | None, top_price: float | None) -> float | None:
    if support is None or top_price is None or support <= 0 or top_price <= 0:
        return None
    return (top_price / support - 1.0) * 100.0


def _support_reason(
    *,
    close_range_pct: float | None,
    recent_close_range_pct: float | None,
    approach_gap_pct: float | None,
    drift_pct: float | None,
) -> str:
    close_text = "--" if close_range_pct is None else f"{close_range_pct:.2f}%"
    recent_text = "--" if recent_close_range_pct is None else f"{recent_close_range_pct:.2f}%"
    approach_text = "--" if approach_gap_pct is None else f"{approach_gap_pct:.2f}%"
    drift_text = "--" if drift_pct is None else f"{drift_pct:.2f}%"
    return f"底部横盘收盘振幅 {close_text} / 近10振幅 {recent_text} / 漂移 {drift_text} / 距底部 {approach_text}"


def _max_base_drift_pct(source_label: str) -> float:
    return 6.0 if source_label == "daily" else 10.0
