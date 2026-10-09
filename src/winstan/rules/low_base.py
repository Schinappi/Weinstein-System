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
RECENT_LOW_LOOKBACK_DAILY = 20
RECENT_LOW_LOOKBACK_WEEKLY = 8
# A low-base candidate must still be close to the recent stopping low.  This
# prevents a post-rally consolidation from being presented as a low-level base.
MAX_BASE_REBOUND_FROM_RECENT_LOW_PCT = 10.0
SUPPORT_ZONE_LOWER_QUANTILE = 0.10
SUPPORT_PRICE_QUANTILE = 0.20
SUPPORT_ZONE_UPPER_QUANTILE = 0.35
BOX_TOP_QUANTILE = 0.90
MAX_CANDIDATE_VOLUME_RATIO = 0.85
# 趋势斜率"走平"硬门槛：每天平均涨跌幅度超过此值即视为单调趋势（非震荡）
TREND_SLOPE_MAX_DAILY_PCT = 0.10
# 触底点搜索范围（交易日）：横盘窗口起点从"最近这波下跌的触底点"开始算。
# 太宽会找到更早的旧底（如半年前的低点），太窄会漏掉当前这波下跌的底。
TROUGH_LOOKBACK_BARS = 60
# 触底点识别（趋势转走平）：摆动低点之前 N 天的斜率低于此值 = 在下跌；
# 之后到最新的斜率高于此值 = 已走平。两者同时满足才是"下跌结束触底"。
TROUGH_DECLINE_LOOKBACK_BARS = 20
TROUGH_DECLINE_SLOPE_PCT = -0.20
TROUGH_FLAT_SLOPE_PCT = -0.05
# 暴跌底斜率阈值：前 N 天斜率低于此值（更陡）视为"单边暴跌"的底，
# 优先于普通阴跌/震荡回落。用于区分"暴跌首次触底"与"暴跌后的震荡二次下探"。
TROUGH_CRASH_SLOPE_PCT = -1.0
# 暴跌底搜索范围（交易日）：只在此范围内找暴跌底，避免抓到太早的历史暴跌。
TROUGH_CRASH_LOOKBACK_BARS = 120


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
    trend_slope_pct: float | None
    reason: str = ""


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
        return _default_result("无有效K线数据（缺少开高低收字段或数据为空）")

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
        return _default_result(base.reason or "未识别到横盘窗口")

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
    recent_low_rebound_pct, recent_low_date = _recent_low_rebound_metrics(
        bars,
        base.start_idx,
        support,
        source_label,
    )

    prior_score = _score_prior_decline(prior_decline_pct)
    duration_score = _score_base_duration(base.duration_weeks)
    volatility_score = _score_quiet_base(
        close_range_pct=base.close_range_pct,
        avg_abs_change_pct=base.avg_abs_change_pct,
        trend_slope_pct=base.trend_slope_pct,
        source_label=source_label,
    )
    volatility_range_score, volatility_noise_score, volatility_trend_score = _score_quiet_base_components(
        close_range_pct=base.close_range_pct,
        avg_abs_change_pct=base.avg_abs_change_pct,
        trend_slope_pct=base.trend_slope_pct,
        source_label=source_label,
    )
    volume_score = 0.0
    bottom_score = _score_bottom_stability(
        touch_count=touch_count,
        recent_close_break_pct=recent_close_break_pct,
        false_break_count=false_break_count,
    )
    total = round(
        min(
            100.0,
            max(0.0, prior_score + duration_score + volatility_score + bottom_score),
        ),
        1,
    )

    support_active = bool(
        base.close_range_pct is not None
        and base.close_range_pct <= MAX_BASE_CLOSE_RANGE_PCT
        and base.trend_slope_pct is not None
        and abs(base.trend_slope_pct) <= TREND_SLOPE_MAX_DAILY_PCT * (1.8 if source_label == "weekly" else 1.0)
        and (
            recent_low_rebound_pct is None
            or recent_low_rebound_pct <= MAX_BASE_REBOUND_FROM_RECENT_LOW_PCT
        )
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
    if (
        recent_low_rebound_pct is not None
        and recent_low_rebound_pct > MAX_BASE_REBOUND_FROM_RECENT_LOW_PCT
    ):
        reason += (
            f" / 不列入候选：相对近期低点{recent_low_rebound_pct:.1f}%"
            f"，超过{MAX_BASE_REBOUND_FROM_RECENT_LOW_PCT:.0f}%上限"
        )
    support_reason = _support_reason(
        close_range_pct=base.close_range_pct,
        recent_close_range_pct=base.recent_close_range_pct,
        approach_gap_pct=approach_gap_pct,
        trend_slope_pct=base.trend_slope_pct,
        recent_low_rebound_pct=recent_low_rebound_pct,
        recent_low_date=recent_low_date,
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
        "low_base_score_volatility_range": round(volatility_range_score, 1),
        "low_base_score_volatility_noise": round(volatility_noise_score, 1),
        "low_base_score_volatility_trend": round(volatility_trend_score, 1),
        "low_base_score_volume": round(volume_score, 1),
        "low_base_score_bottom_stability": round(bottom_score, 1),
        "low_base_score_bottom_touch": round(_bottom_touch_score(touch_count), 1),
        "low_base_score_bottom_hold": round(_bottom_hold_score(recent_close_break_pct), 1),
        "low_base_score_bottom_false_break": round(_bottom_false_break_score(false_break_count), 1),
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
        "low_base_trend_slope_pct": round(base.trend_slope_pct, 4) if base.trend_slope_pct is not None else None,
        "low_base_recent_low_rebound_pct": (
            round(recent_low_rebound_pct, 2) if recent_low_rebound_pct is not None else None
        ),
        "low_base_recent_low_date": _format_date(recent_low_date),
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
        "low_base_score_volatility_range": 0.0,
        "low_base_score_volatility_noise": 0.0,
        "low_base_score_volatility_trend": 0.0,
        "low_base_score_volume": 0.0,
        "low_base_score_bottom_stability": 0.0,
        "low_base_score_bottom_touch": 0.0,
        "low_base_score_bottom_hold": 0.0,
        "low_base_score_bottom_false_break": 0.0,
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
        "low_base_trend_slope_pct": None,
        "low_base_recent_low_rebound_pct": None,
        "low_base_recent_low_date": "",
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


def _find_trough_idx(bars: pd.DataFrame, end_idx: int) -> int:
    """找触底点。

    分两级：
    1. 暴跌底：前 N 天斜率低于 TROUGH_CRASH_SLOPE_PCT（单边暴跌），优先采用——
       这能锁定"暴跌首次触底"，忽略之后的震荡二次下探。
    2. 趋势转走平底：前 N 天斜率显著负（在下跌）+ 之后到最新已走平（斜率接近 0）。
       用于阴跌（无单边暴跌）的"最近回落底"。
    """
    closes = pd.to_numeric(bars["close"], errors="coerce")
    order = 2

    # 摆动低点（收盘价局部最小）
    swing_lows = []
    for i in range(order, end_idx - order + 1):
        if (
            closes.iloc[i] < closes.iloc[i - order : i].min()
            and closes.iloc[i] < closes.iloc[i + 1 : i + 1 + order].min()
        ):
            swing_lows.append(i)

    # 第一优先：暴跌底。最近 TROUGH_CRASH_LOOKBACK_BARS 内、前 N 天斜率 < -1.0 的
    # 摆动低点中，取收盘价最低的那个（即暴跌段的最低点，锁定首次触底，
    # 忽略之后的反弹回落与震荡二次下探）。
    crash_lows = []
    for low_idx in swing_lows:
        if low_idx < end_idx - TROUGH_CRASH_LOOKBACK_BARS:
            continue
        left_seg = closes.iloc[max(0, low_idx - TROUGH_DECLINE_LOOKBACK_BARS) : low_idx + 1]
        left_slope = _trend_slope_pct(left_seg)
        if left_slope is not None and left_slope < TROUGH_CRASH_SLOPE_PCT:
            crash_lows.append(low_idx)
    if crash_lows:
        return min(crash_lows, key=lambda i: float(closes.iloc[i]))

    # 第二优先：趋势转走平底（之前下跌、之后走平）
    for low_idx in reversed(swing_lows):
        left_seg = closes.iloc[max(0, low_idx - TROUGH_DECLINE_LOOKBACK_BARS) : low_idx + 1]
        left_slope = _trend_slope_pct(left_seg)
        right_seg = closes.iloc[low_idx : end_idx + 1]
        right_slope = _trend_slope_pct(right_seg)
        if left_slope is not None and right_slope is not None:
            if left_slope < TROUGH_DECLINE_SLOPE_PCT and right_slope > TROUGH_FLAT_SLOPE_PCT:
                return low_idx

    # 回退：最近 TROUGH_LOOKBACK_BARS 内最低收盘
    search_start = max(0, end_idx - TROUGH_LOOKBACK_BARS)
    return int(closes.iloc[search_start : end_idx + 1].idxmin())


def _find_quiet_base_window(
    bars: pd.DataFrame,
    source_label: str,
) -> QuietBaseWindow:
    bars_per_week = 5 if source_label == "daily" else 1
    min_base_bars = MIN_BASE_DURATION_WEEKS * bars_per_week
    if len(bars) < min_base_bars:
        return _empty_base_window(
            f"历史数据不足：识别横盘需≥{MIN_BASE_DURATION_WEEKS}周（{min_base_bars}根K线），"
            f"当前仅 {len(bars)} 根"
        )

    end_idx = len(bars) - 1

    # 触底点 = 下跌趋势转为走平的转折点。横盘窗口 = 触底点 -> 最新一天，
    # 从"下跌完触底"才开始算，而不是把下跌段误算进横盘。
    trough_idx = _find_trough_idx(bars, end_idx)

    # 触底后横盘时间必须满足最小持续（否则尚未形成充分基底）
    hold_bars = end_idx - trough_idx + 1
    if hold_bars < min_base_bars:
        hold_days = hold_bars - 1
        hold_weeks = hold_days / bars_per_week
        trough_date = _format_date(bars.iloc[trough_idx].get("trade_date"))
        return _empty_base_window(
            f"触底后横盘时间不足：最近触底于 {trough_date}，"
            f"触底后仅 {hold_days} 个交易日（约{hold_weeks:.1f}周），"
            f"未达 {MIN_BASE_DURATION_WEEKS} 周最低蓄势期"
        )

    segment = bars.iloc[trough_idx : end_idx + 1]
    candidate = _build_quiet_base_window(segment, trough_idx, end_idx, source_label)
    if candidate is None:
        return _empty_base_window("横盘窗口构建失败（数据异常）")
    return candidate


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
    trend_slope_pct = _trend_slope_pct(closes)

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
        trend_slope_pct=trend_slope_pct,
    )


def _empty_base_window(reason: str = "") -> QuietBaseWindow:
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
        trend_slope_pct=None,
        reason=reason,
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


def _recent_low_rebound_metrics(
    bars: pd.DataFrame,
    base_start_idx: int,
    support: float,
    source_label: str,
) -> tuple[float | None, object | None]:
    """Measure how far the proposed base sits above the recent stopping low.

    The low-base screen is intended to find an accumulation area near the
    stopping low, not a quiet box formed after a sizeable rally.  Use the
    intraday low because it captures the actual rejection/stopping point more
    faithfully than a close-only minimum.
    """
    if support <= 0 or "low" not in bars.columns or base_start_idx < 0:
        return None, None
    lookback = RECENT_LOW_LOOKBACK_DAILY if source_label == "daily" else RECENT_LOW_LOOKBACK_WEEKLY
    start = max(0, int(base_start_idx) - lookback)
    recent = pd.to_numeric(bars.iloc[start : int(base_start_idx) + 1]["low"], errors="coerce").dropna()
    if recent.empty:
        return None, None
    low_idx = recent.idxmin()
    recent_low = float(recent.loc[low_idx])
    if not np.isfinite(recent_low) or recent_low <= 0:
        return None, None
    return (support / recent_low - 1.0) * 100.0, bars.at[low_idx, "trade_date"]


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
    if value >= 50.0:
        return 14.0
    if value >= 40.0:
        return 12.0
    if value >= 30.0:
        return 10.0
    if value >= 25.0:
        return 8.0
    if value >= 20.0:
        return 6.0
    if value >= 15.0:
        return 4.0
    return 0.0


def _score_base_duration(weeks: int) -> float:
    if weeks < MIN_BASE_DURATION_WEEKS:
        return 0.0
    if weeks < 8:
        return 8.0
    if weeks < 12:
        return 15.0
    if weeks < 16:
        return 20.0
    if weeks <= 30:
        return 25.0
    if weeks <= 40:
        return 20.0
    return 12.0


def _score_trend(trend_slope_pct: float | None, source_label: str) -> float:
    """趋势斜率分 (0-5)：|斜率|越小越走平，越大越像单调趋势。"""
    if trend_slope_pct is None:
        return 0.0
    abs_slope = abs(trend_slope_pct)
    max_slope = TREND_SLOPE_MAX_DAILY_PCT * (1.8 if source_label == "weekly" else 1.0)
    if max_slope <= 0:
        return 0.0
    ratio = abs_slope / max_slope
    if ratio <= 0.25:
        return 5.0
    if ratio <= 0.5:
        return 4.0
    if ratio <= 0.75:
        return 3.0
    if ratio <= 1.0:
        return 2.0
    return 0.0


def _score_quiet_base(
    *,
    close_range_pct: float | None,
    avg_abs_change_pct: float | None,
    trend_slope_pct: float | None,
    source_label: str,
) -> float:
    range_score, noise_score, trend_score = _score_quiet_base_components(
        close_range_pct=close_range_pct,
        avg_abs_change_pct=avg_abs_change_pct,
        trend_slope_pct=trend_slope_pct,
        source_label=source_label,
    )
    return min(30.0, range_score + noise_score + trend_score)


def _score_quiet_base_components(
    *,
    close_range_pct: float | None,
    avg_abs_change_pct: float | None,
    trend_slope_pct: float | None,
    source_label: str,
) -> tuple[float, float, float]:
    if close_range_pct is None:
        return 0.0, 0.0, 0.0
    scale = 1.0 if source_label == "daily" else 1.8
    # 区间振幅得分：0~20% 均有分（越小越安静），>20% 才归零。
    if close_range_pct <= 3.5 * scale:
        range_score = 16.0
    elif close_range_pct <= 5.0 * scale:
        range_score = 14.0
    elif close_range_pct <= 6.5 * scale:
        range_score = 12.0
    elif close_range_pct <= MAX_BASE_CLOSE_RANGE_PCT * scale:
        range_score = 10.0
    elif close_range_pct <= 10.0 * scale:
        range_score = 8.0
    elif close_range_pct <= 12.0 * scale:
        range_score = 6.0
    elif close_range_pct <= 14.0 * scale:
        range_score = 4.0
    elif close_range_pct <= 16.0 * scale:
        range_score = 3.0
    elif close_range_pct <= 18.0 * scale:
        range_score = 2.0
    elif close_range_pct <= 20.0 * scale:
        range_score = 1.0
    else:
        range_score = 0.0

    if avg_abs_change_pct is None:
        noise_score = 0.0
    elif avg_abs_change_pct <= 0.9 * scale:
        noise_score = 9.0
    elif avg_abs_change_pct <= 1.2 * scale:
        noise_score = 7.0
    elif avg_abs_change_pct <= 1.6 * scale:
        noise_score = 5.0
    elif avg_abs_change_pct <= 2.0 * scale:
        noise_score = 3.0
    elif avg_abs_change_pct <= 2.4 * scale:
        noise_score = 1.0
    else:
        noise_score = 0.0

    trend_score = _score_trend(trend_slope_pct, source_label)
    return range_score, noise_score, trend_score


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


def _bottom_touch_score(touch_count: int) -> float:
    """触底次数 (0-10)：价格反复回到支撑区，说明下方承接有效。"""
    if touch_count >= 5:
        return 10.0
    if touch_count >= 4:
        return 9.0
    if touch_count >= 3:
        return 7.0
    if touch_count >= 2:
        return 5.0
    if touch_count >= 1:
        return 3.0
    return 0.0


def _bottom_hold_score(recent_close_break_pct: float) -> float:
    """支撑保持 (0-12)：近20根收盘价跌破支撑区的最大幅度，越小越稳。"""
    if recent_close_break_pct <= 0.0:
        return 12.0
    if recent_close_break_pct <= 0.5:
        return 10.0
    if recent_close_break_pct <= 1.0:
        return 8.0
    if recent_close_break_pct <= 1.5:
        return 6.0
    if recent_close_break_pct <= 2.0:
        return 4.0
    if recent_close_break_pct <= 3.0:
        return 2.0
    return 0.0


def _bottom_false_break_score(false_break_count: int) -> float:
    """假跌破控制 (0-8)：盘中砸破支撑但收盘收回的次数越少，支撑越真实（负向）。"""
    if false_break_count == 0:
        return 8.0
    if false_break_count == 1:
        return 6.0
    if false_break_count == 2:
        return 4.0
    if false_break_count == 3:
        return 2.0
    return 0.0


def _score_bottom_stability(
    *,
    touch_count: int,
    recent_close_break_pct: float,
    false_break_count: int,
) -> float:
    return min(
        30.0,
        _bottom_touch_score(touch_count)
        + _bottom_hold_score(recent_close_break_pct)
        + _bottom_false_break_score(false_break_count),
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
        f"横盘持续 {float(kwargs['duration_score']):.1f}/25",
        f"横盘静默 {float(kwargs['volatility_score']):.1f}/30",
        f"底部稳定 {float(kwargs['bottom_score']):.1f}/30",
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
            f"区间振幅 {float(close_range_pct):.2f}%（>10%静默不足） / 近{RECENT_STABILITY_BARS}振幅 {recent_text} / 单日波动 {avg_text}"
        )
    volume_ratio = kwargs.get("volume_ratio")
    if volume_ratio is not None:
        parts.append(f"量能参考 {kwargs.get('volume_range') or '--'} {float(volume_ratio):.2f}x（不计分）")
    recent_amount_avg = kwargs.get("recent_amount_avg")
    if recent_amount_avg is not None:
        parts.append(f"近20均成交额 {float(recent_amount_avg) / 100000000.0:.2f}亿")
    direction_volume_ratio = kwargs.get("direction_volume_ratio")
    if direction_volume_ratio is not None:
        parts.append(f"方向量能 {float(direction_volume_ratio):.2f}x（仅展示不计分）")
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
    return f"横盘全区收盘振幅 / 近{RECENT_STABILITY_BARS}{unit}收盘振幅 / 单日波动"


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
    trend_slope_pct: float | None,
    recent_low_rebound_pct: float | None,
    recent_low_date: object | None,
) -> str:
    close_text = "--" if close_range_pct is None else f"{close_range_pct:.2f}%"
    recent_text = "--" if recent_close_range_pct is None else f"{recent_close_range_pct:.2f}%"
    approach_text = "--" if approach_gap_pct is None else f"{approach_gap_pct:.2f}%"
    trend_text = "--" if trend_slope_pct is None else f"{trend_slope_pct:+.3f}%/天"
    rebound_text = "--" if recent_low_rebound_pct is None else f"{recent_low_rebound_pct:.2f}%"
    low_date_text = _format_date(recent_low_date) or "--"
    return (
        f"底部横盘收盘振幅 {close_text} / 近10振幅 {recent_text} / 趋势斜率 {trend_text} "
        f"/ 距底部 {approach_text} / 相对近期低点反弹 {rebound_text}（低点{low_date_text}，上限{MAX_BASE_REBOUND_FROM_RECENT_LOW_PCT:.0f}%）"
    )


def _max_base_drift_pct(source_label: str) -> float:
    return 6.0 if source_label == "daily" else 10.0


def _trend_slope_pct(closes: pd.Series) -> float | None:
    """线性回归斜率，标准化为每根 bar 的百分比变动。

    正=单调上升，负=单调下降，0=走平。用于区分"震荡走平"与"单调趋势"，
    替代首末漂移（drift_pct）——后者无法区分"开始即最低、结束即最高"的震荡。
    """
    numeric = pd.to_numeric(closes, errors="coerce").dropna()
    if len(numeric) < 2:
        return None
    midpoint = float(numeric.mean())
    if midpoint <= 0:
        return None
    x = np.arange(len(numeric), dtype=float)
    slope, _ = np.polyfit(x, numeric.to_numpy(dtype=float), 1)
    return float(slope / midpoint * 100.0)
