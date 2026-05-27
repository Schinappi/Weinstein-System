from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig


def _is_progressive_series(values: pd.Series) -> bool:
    window = pd.to_numeric(values, errors="coerce").dropna().reset_index(drop=True)
    if len(window) < 3:
        return False

    changes = window.diff().dropna()
    if changes.empty:
        return False

    allowed_pullbacks = 1 if len(changes) >= 4 else 0
    improving_steps = int((changes >= 0).sum())
    required_steps = len(changes) - allowed_pullbacks
    return improving_steps >= required_steps and float(window.iloc[-1]) > float(window.iloc[0])


def evaluate_stage(latest: pd.Series, recent: pd.DataFrame, config: AppConfig) -> dict[str, object]:
    if recent.empty or pd.isna(latest.get("ma_30w")):
        return {
            "stage_label": "UNKNOWN",
            "stage2_candidate": False,
            "trend_score": 0.0,
            "base_flatness_ok": False,
            "stage2_score": 0.0,
            "stage2_reason": "无数据",
        }

    stage_window = recent.tail(config.strategy.min_stage2_weeks)
    highs_rising = _is_progressive_series(stage_window["high"])
    lows_rising = _is_progressive_series(stage_window["low"])
    price_above = latest["close"] > latest["ma_30w"]
    slope_up = latest["ma_30w_slope"] > 0 if pd.notna(latest["ma_30w_slope"]) else False
    base_flatness_ok = _is_flat_base(latest, config)
    stage2_score, stage2_reason = _stage2_score(
        latest=latest,
        price_above=price_above,
        slope_up=slope_up,
        highs_rising=highs_rising,
        lows_rising=lows_rising,
        base_flatness_ok=base_flatness_ok,
        config=config,
    )

    if price_above and slope_up and highs_rising and lows_rising and base_flatness_ok:
        stage = "II"
        candidate = True
    elif (latest["close"] < latest["ma_30w"]) and (latest["ma_30w_slope"] < 0 if pd.notna(latest["ma_30w_slope"]) else False):
        stage = "IV"
        candidate = False
    elif slope_up:
        stage = "I"
        candidate = False
    else:
        stage = "III"
        candidate = False

    trend_score = 0.0
    trend_score += 40.0 if price_above else 0.0
    trend_score += 30.0 if slope_up else 0.0
    trend_score += 15.0 if highs_rising else 0.0
    trend_score += 15.0 if lows_rising else 0.0

    return {
        "stage_label": stage,
        "stage2_candidate": candidate,
        "trend_score": trend_score,
        "base_flatness_ok": base_flatness_ok,
        "stage2_score": stage2_score,
        "stage2_reason": stage2_reason,
    }


def _is_flat_base(latest: pd.Series, config: AppConfig) -> bool:
    range_pct = latest.get("base_range_pct")
    std_pct = latest.get("base_close_std_pct")
    ma_spread_pct = latest.get("ma_spread_pct")

    if pd.isna(range_pct) or pd.isna(std_pct) or pd.isna(ma_spread_pct):
        return False

    return (
        float(range_pct) <= config.strategy.watch_base_max_range_pct
        and float(std_pct) <= config.strategy.watch_base_max_close_std_pct
        and float(ma_spread_pct) <= config.strategy.watch_ma_spread_max_pct
    )


def _stage2_score(
    latest: pd.Series,
    price_above: bool,
    slope_up: bool,
    highs_rising: bool,
    lows_rising: bool,
    base_flatness_ok: bool,
    config: AppConfig,
) -> tuple[float, str]:
    price_vs_ma_pct = latest.get("price_vs_ma_pct")
    ma_spread_pct = latest.get("ma_spread_pct")
    breakout_level = latest.get("breakout_level")
    weekly_volume_ma_10 = latest.get("weekly_volume_ma_10")
    volume = latest.get("volume")
    rs_rank_pct = latest.get("rs_rank_pct")
    rs_composite = latest.get("rs_composite")

    breakout_pct = None
    if pd.notna(breakout_level) and float(breakout_level) != 0:
        breakout_pct = (float(latest["close"]) / float(breakout_level) - 1.0) * 100.0

    score = 0.0
    reason: list[str] = []

    proximity_score = _stage2_proximity_score(float(price_vs_ma_pct) if pd.notna(price_vs_ma_pct) else None)
    score += proximity_score
    if proximity_score > 0:
        reason.append("价格靠近30周线")

    if price_above:
        score += 15.0
        reason.append("站上30周线")
    elif pd.notna(price_vs_ma_pct) and float(price_vs_ma_pct) >= -5.0:
        score += 8.0
        reason.append("仍在30周线附近")

    if slope_up:
        score += 15.0
        reason.append("30周线向上")
    elif pd.notna(latest.get("ma_30w_slope")) and float(latest["ma_30w_slope"]) > -0.02:
        score += 8.0
        reason.append("30周线走平")

    if highs_rising:
        score += 10.0
        reason.append("高点抬升")
    if lows_rising:
        score += 10.0
        reason.append("低点抬升")

    if base_flatness_ok:
        score += 15.0
        reason.append("基底平整")
    else:
        score += 4.0
        reason.append("基底偏松")

    volume_ratio = None
    if pd.notna(weekly_volume_ma_10) and float(weekly_volume_ma_10) > 0 and pd.notna(volume):
        volume_ratio = float(volume) / float(weekly_volume_ma_10)
        if volume_ratio >= 1.8:
            score += 12.0
            reason.append("成交量明显放大")
        elif volume_ratio >= 1.2:
            score += 8.0
            reason.append("成交量温和放大")
        elif volume_ratio >= 0.9:
            score += 4.0
            reason.append("成交量正常")

    if pd.notna(rs_rank_pct):
        rs_rank_pct = float(rs_rank_pct)
        rs_score = max(0.0, 18.0 - (rs_rank_pct - 1.0) * 0.2)
        score += rs_score
        if rs_rank_pct <= 20:
            reason.append("相对强度较强")
        elif rs_rank_pct <= 40:
            reason.append("相对强度中上")

    if pd.notna(rs_composite):
        rs_boost = min(max(float(rs_composite) * 30.0, 0.0), 6.0)
        score += rs_boost

    if pd.notna(ma_spread_pct):
        spread = float(ma_spread_pct)
        if spread <= 2:
            score += 5.0
        elif spread <= 5:
            score += 2.5

    if breakout_pct is not None:
        if -3 <= breakout_pct <= 8:
            score += 10.0
            reason.append("接近突破位")
        elif breakout_pct > 8:
            score += 3.0

    return min(score, 100.0), " / ".join(reason) if reason else "普通趋势"


def _stage2_proximity_score(price_vs_ma_pct: float | None) -> float:
    if price_vs_ma_pct is None or pd.isna(price_vs_ma_pct):
        return 0.0
    if price_vs_ma_pct >= 0:
        return min(25.0, 10.0 + float(price_vs_ma_pct) * 1.5)
    if price_vs_ma_pct >= -8:
        return max(0.0, 25.0 + float(price_vs_ma_pct) * 2.5)
    return 0.0


def apply_stage2_scoring(results: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    scored = results.copy()
    score_columns = {
        "structure_score": 0.0,
        "timing_score": 0.0,
        "strength_score": 0.0,
        "risk_score": 0.0,
        "final_score": 0.0,
        "stage2_score": 0.0,
        "stage2_reason": "普通趋势",
        "stage2_watch_score": 0.0,
        "stage2_watch_reason": "普通趋势",
    }
    if scored.empty:
        for column, default in score_columns.items():
            scored[column] = pd.Series(dtype="float64" if isinstance(default, float) else "object")
        return scored

    profile = scored.apply(lambda row: pd.Series(_build_stage2_profile(row, config)), axis=1)
    for column in profile.columns:
        scored[column] = profile[column]
    return scored


def _build_stage2_profile(row: pd.Series, config: AppConfig) -> dict[str, object]:
    structure_score = _score_structure(row)
    timing_score = _score_timing(row, config)
    strength_score = _score_strength(row)
    risk_score = _score_risk(row, config)
    final_score = _clamp_score(
        structure_score * 0.45
        + timing_score * 0.30
        + strength_score * 0.20
        - risk_score * 0.15
    )
    reason = _build_stage2_reason(row, structure_score, timing_score, strength_score, risk_score)
    return {
        "structure_score": structure_score,
        "timing_score": timing_score,
        "strength_score": strength_score,
        "risk_score": risk_score,
        "final_score": final_score,
        "stage2_score": final_score,
        "stage2_reason": reason,
        "stage2_watch_score": final_score,
        "stage2_watch_reason": reason,
    }


def _score_structure(row: pd.Series) -> float:
    trend_score = _to_float(row.get("trend_score")) or 0.0
    stage_label = str(row.get("stage_label") or "UNKNOWN")
    base_flatness_ok = _to_bool(row.get("base_flatness_ok"))
    stage2_candidate = _to_bool(row.get("stage2_candidate"))
    ma_spread_pct = _to_float(row.get("ma_spread_pct"))

    score = trend_score * 0.45
    score += {"II": 18.0, "I": 12.0, "III": 5.0, "IV": 0.0, "UNKNOWN": 2.0}.get(stage_label, 2.0)
    score += 15.0 if base_flatness_ok else 4.0
    if ma_spread_pct is not None:
        if ma_spread_pct <= 2.0:
            score += 8.0
        elif ma_spread_pct <= 5.0:
            score += 4.0
        elif ma_spread_pct <= 8.0:
            score += 1.0
    if stage2_candidate:
        score += 8.0
    return _clamp_score(score)


def _score_timing(row: pd.Series, config: AppConfig) -> float:
    breakout_status = str(row.get("breakout_status") or "no_breakout_level")
    breakout_pct = _to_float(row.get("breakout_pct"))
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    volume_ratio = _to_float(row.get("volume_ratio"))
    breakout_ok = _to_bool(row.get("breakout_ok"))

    score = 0.0
    score += {
        "just_broke_out": 34.0,
        "near_breakout": 30.0,
        "below_breakout": 18.0,
        "extended_breakout": 4.0,
        "no_breakout_level": 12.0,
    }.get(breakout_status, 12.0)

    if breakout_pct is not None:
        if -2.0 <= breakout_pct <= 5.0:
            score += 22.0
        elif (-5.0 <= breakout_pct < -2.0) or (5.0 < breakout_pct <= config.strategy.watch_breakout_max_pct):
            score += 14.0
        elif config.strategy.watch_breakout_max_pct < breakout_pct <= config.strategy.watch_breakout_max_pct + 7.0:
            score += 6.0
        elif breakout_pct < -5.0:
            score += 8.0

    if price_vs_ma_pct is not None:
        if 0.0 <= price_vs_ma_pct <= 8.0:
            score += 22.0
        elif -5.0 <= price_vs_ma_pct < 0.0:
            score += 18.0
        elif 8.0 < price_vs_ma_pct <= config.strategy.watch_max_price_vs_ma_pct:
            score += 12.0
        elif -10.0 <= price_vs_ma_pct < -5.0:
            score += 8.0
        else:
            score += 4.0

    if volume_ratio is not None:
        if volume_ratio >= 1.8:
            score += 14.0
        elif volume_ratio >= 1.2:
            score += 11.0
        elif volume_ratio >= 0.9:
            score += 7.0
        else:
            score += 2.0

    if breakout_ok:
        score += 8.0
    elif breakout_status == "below_breakout":
        score += 2.0
    return _clamp_score(score)


def _score_strength(row: pd.Series) -> float:
    rs_rank_pct = _to_float(row.get("rs_rank_pct"))
    rs_composite = _to_float(row.get("rs_composite"))

    rank_score = 0.0 if rs_rank_pct is None else _clamp_score(100.0 - max(rs_rank_pct - 1.0, 0.0) * 1.4)
    composite_score = 0.0 if rs_composite is None else _clamp_score(50.0 + rs_composite * 250.0)
    score = rank_score * 0.80 + composite_score * 0.20
    if rs_rank_pct is not None and rs_rank_pct <= 10.0:
        score += 6.0
    elif rs_rank_pct is not None and rs_rank_pct <= 20.0:
        score += 3.0
    return _clamp_score(score)


def _score_risk(row: pd.Series, config: AppConfig) -> float:
    breakout_status = str(row.get("breakout_status") or "no_breakout_level")
    stage_label = str(row.get("stage_label") or "UNKNOWN")
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    breakout_pct = _to_float(row.get("breakout_pct"))
    headroom_pct = _to_float(row.get("headroom_pct"))

    score = 0.0
    if not _to_bool(row.get("market_ok")):
        score += 18.0
    if not _to_bool(row.get("base_flatness_ok")):
        score += 14.0
    if not _to_bool(row.get("volume_ok")):
        score += 10.0
    if not _to_bool(row.get("resistance_ok")):
        score += 14.0

    if breakout_status == "extended_breakout":
        score += 26.0
    elif breakout_pct is not None and breakout_pct > config.strategy.watch_breakout_max_pct:
        score += 18.0
    elif breakout_pct is not None and breakout_pct < -6.0:
        score += 10.0

    if price_vs_ma_pct is not None:
        if price_vs_ma_pct > config.strategy.watch_max_price_vs_ma_pct + 5.0:
            score += 28.0
        elif price_vs_ma_pct > config.strategy.watch_max_price_vs_ma_pct:
            score += 20.0
        elif price_vs_ma_pct > 10.0:
            score += 10.0
        elif price_vs_ma_pct < -8.0:
            score += 14.0

    if headroom_pct is not None:
        if headroom_pct < 5.0:
            score += 24.0
        elif headroom_pct < 8.0:
            score += 18.0
        elif headroom_pct < config.strategy.resistance_min_headroom_pct:
            score += 10.0

    score += {"III": 8.0, "IV": 16.0}.get(stage_label, 0.0)
    if not _to_bool(row.get("breakout_ok")) and breakout_status not in {"near_breakout", "just_broke_out"}:
        score += 8.0
    return _clamp_score(score)


def _build_stage2_reason(
    row: pd.Series,
    structure_score: float,
    timing_score: float,
    strength_score: float,
    risk_score: float,
) -> str:
    bits = [
        f"结构{_score_bucket(structure_score)}",
        f"时机{_score_bucket(timing_score)}",
        f"强度{_score_bucket(strength_score)}",
        f"风险{_risk_bucket(risk_score)}",
    ]

    breakout_status = str(row.get("breakout_status") or "no_breakout_level")
    if breakout_status == "just_broke_out":
        bits.append("刚突破")
    elif breakout_status == "near_breakout":
        bits.append("临近突破")
    elif breakout_status == "extended_breakout":
        bits.append("突破后偏扩展")

    if not _to_bool(row.get("market_ok")):
        bits.append("大盘过滤未通过")
    if not _to_bool(row.get("resistance_ok")):
        bits.append("上方空间偏小")
    return " / ".join(bits)


def _score_bucket(value: float) -> str:
    if value >= 80.0:
        return "优秀"
    if value >= 65.0:
        return "良好"
    if value >= 50.0:
        return "中等"
    return "偏弱"


def _risk_bucket(value: float) -> str:
    if value >= 70.0:
        return "较高"
    if value >= 45.0:
        return "可控偏高"
    if value >= 25.0:
        return "可控"
    return "较低"


def _clamp_score(value: float) -> float:
    return max(0.0, min(float(value), 100.0))


def _to_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _to_bool(value: object) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(value)
