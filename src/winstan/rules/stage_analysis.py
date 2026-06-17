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
            "extension_pct": None,
            "stage2_age_weeks": 0,
            "base_breakout_price": None,
            "base_low": None,
            "base_id": None,
            "base_breakout_fixed": False,
            "base_weeks": 0,
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

    # Extension pct: (close - ma_30w) / ma_30w as percentage
    extension_pct = (
        float((latest["close"] - latest["ma_30w"]) / latest["ma_30w"]) * 100.0
        if pd.notna(latest.get("ma_30w")) and float(latest["ma_30w"]) > 0
        else None
    )

    # Stage II age: count consecutive weeks with close > ma_30w (proxy for Stage II duration)
    stage2_age_weeks = _count_consecutive_above_ma(recent, config)

    # 基底检测：识别最近的平坦基底区间，计算固定突破位
    base_info = _detect_bases(recent, config)

    return {
        "stage_label": stage,
        "stage2_candidate": candidate,
        "trend_score": trend_score,
        "base_flatness_ok": base_flatness_ok,
        "stage2_score": stage2_score,
        "stage2_reason": stage2_reason,
        "extension_pct": extension_pct,
        "stage2_age_weeks": stage2_age_weeks,
        "base_breakout_price": base_info["base_breakout_price"],
        "base_low": base_info["base_low"],
        "base_id": base_info["base_id"],
        "base_breakout_fixed": base_info["base_breakout_fixed"],
        "base_weeks": base_info["base_weeks"],
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


def _count_consecutive_above_ma(recent: pd.DataFrame, config: AppConfig) -> int:
    """Count consecutive weeks where close > ma_30w (proxy for Stage II age)."""
    if recent.empty or "ma_30w" not in recent.columns or "close" not in recent.columns:
        return 0
    above = recent["close"] > recent["ma_30w"]
    # Walk backwards from the latest week, counting consecutive True
    count = 0
    for val in above[::-1]:
        if val:
            count += 1
        else:
            break
    return count


MIN_BASE_WEEKS = 3


def _detect_bases(recent: pd.DataFrame, config: AppConfig) -> dict[str, object]:
    """Identify flat base consolidation zones in weekly history.

    Walks the weekly DataFrame chronologically, finds consecutive weeks
    where _is_flat_base() is True, groups them into bases, and returns
    the most recent base's high/low/breakout status.

    Once price closes above base_high, the breakout price is locked
    and never updated — matching Weinstein's "fixed buy point" concept.
    """
    result: dict[str, object] = {
        "base_breakout_price": None,
        "base_low": None,
        "base_id": None,
        "base_breakout_fixed": False,
        "base_weeks": 0,
    }
    if recent.empty or "high" not in recent.columns or "low" not in recent.columns:
        return result

    # Build flatness mask
    flat_mask: list[bool] = []
    for i in range(len(recent)):
        row = recent.iloc[i]
        flat_mask.append(_is_flat_base(row, config))

    # Segment into consecutive flat regions (bases)
    bases: list[dict[str, object]] = []
    in_base = False
    base_start = 0
    for i, is_flat in enumerate(flat_mask):
        if is_flat and not in_base:
            in_base = True
            base_start = i
        elif not is_flat and in_base:
            in_base = False
            length = i - base_start
            if length >= MIN_BASE_WEEKS:
                base_high = float(recent["high"].iloc[base_start:i].max())
                base_low = float(recent["low"].iloc[base_start:i].min())
                bases.append({
                    "start_idx": base_start,
                    "end_idx": i - 1,
                    "high": base_high,
                    "low": base_low,
                    "weeks": length,
                })
    # Trailing base (still ongoing at latest row)
    if in_base:
        length = len(flat_mask) - base_start
        if length >= MIN_BASE_WEEKS:
            base_high = float(recent["high"].iloc[base_start:].max())
            base_low = float(recent["low"].iloc[base_start:].min())
            bases.append({
                "start_idx": base_start,
                "end_idx": len(recent) - 1,
                "high": base_high,
                "low": base_low,
                "weeks": length,
            })

    if not bases:
        return result

    last_base = bases[-1]
    current_close = float(recent["close"].iloc[-1])
    broken_out = current_close > float(last_base["high"])

    result["base_breakout_price"] = float(last_base["high"])
    result["base_low"] = float(last_base["low"])
    result["base_id"] = len(bases)
    result["base_breakout_fixed"] = broken_out
    result["base_weeks"] = int(last_base["weeks"])
    return result


# ── Stage1→2 转换检测 ────────────────────────────────────────
TRANSITION_MIN_BASE_WEEKS = 8
TRANSITION_MAX_BASE_RANGE_PCT = 15.0
TRANSITION_MAX_EXTENSION_PCT = 5.0   # 距基底顶 ≤5% 才算"附近"
TRANSITION_MIN_VOLUME_RATIO = 1.2    # 突破周量能 ≥ 均量1.2倍


def detect_transition(
    latest: pd.Series,
    recent: pd.DataFrame,
    config: AppConfig,
    base_info: dict[str, object] | None = None,
    base_quality_score: float = 0.0,
) -> dict[str, object]:
    """检测 Stage1→2 转换候选。

    核心条件：
    1. 存在有效基底（≥8周，振幅≤15%）
    2. MA30w 刚刚转升（近4周内从≤0变为>0，或持续上升<4周）
    3. 价格接近基底顶（距突破≤5%）
    4. 突破周放量 ≥ 1.2x 均量
    5. 上方空间 ≥5%，套牢盘 ≤40%

    Returns:
        transition_candidate: bool
        transition_score: float (0-100)
        transition_reason: str
        transition_base_weeks: int
        transition_base_high: float | None
        transition_distance_pct: float | None (距突破位距离)
        transition_volume_ratio: float | None
        transition_ma_slope_change: bool
    """
    default = {
        "transition_candidate": False,
        "transition_score": 0.0,
        "transition_reason": "",
        "transition_base_weeks": 0,
        "transition_base_high": None,
        "transition_distance_pct": None,
        "transition_volume_ratio": None,
        "transition_ma_slope_change": False,
    }

    if recent.empty or "close" not in recent.columns:
        default["transition_reason"] = "无数据"
        return default

    # ── 1. 基底检测 ──
    # 用更严格标准重新检测基底
    bases = _detect_bases_strict(recent, config,
                                  min_weeks=TRANSITION_MIN_BASE_WEEKS,
                                  max_range_pct=TRANSITION_MAX_BASE_RANGE_PCT)
    if not bases:
        default["transition_reason"] = "无≥8周紧凑基底"
        return default

    last_base = bases[-1]
    base_high = float(last_base["high"])
    base_low = float(last_base["low"])
    base_weeks = int(last_base["weeks"])
    current_close = float(latest["close"])

    # ── 2. 价格距基底顶 ──
    distance_pct = (current_close / base_high - 1.0) * 100.0
    if distance_pct > TRANSITION_MAX_EXTENSION_PCT:
        default["transition_reason"] = f"距基底顶+{distance_pct:.0f}%（已跑远）"
        return default
    if distance_pct < -8.0:
        default["transition_reason"] = f"距基底顶{distance_pct:.0f}%（尚未接近）"
        return default

    # ── 3. MA30w 斜率转升 ──
    slope_change = _detect_slope_turn(recent)
    if not slope_change:
        default["transition_reason"] = "MA30w未确认转升"
        return default

    # ── 4. 量能确认 ──
    vol_ratio = _compute_weekly_volume_ratio(latest)
    if vol_ratio is None or vol_ratio < TRANSITION_MIN_VOLUME_RATIO:
        default["transition_reason"] = f"量能{vol_ratio:.1f}x（需≥{TRANSITION_MIN_VOLUME_RATIO}x）"
        return default

    # ── 5. 上方空间 ──
    headroom_pct = latest.get("headroom_pct")
    if pd.notna(headroom_pct) and float(headroom_pct) < 5.0:
        default["transition_reason"] = f"上方空间{float(headroom_pct):.1f}%（需≥5%）"
        return default

    # ── 评分 ──
    score = 0.0
    reason_parts: list[str] = []

    # 基底质量 (35分) — 用独立 Base Quality 评分加权
    if base_quality_score >= 85:
        score += 35.0
        reason_parts.append("极优质基底")
    elif base_quality_score >= 70:
        score += 28.0
        reason_parts.append("优质基底")
    elif base_quality_score >= 50:
        score += 18.0
        reason_parts.append("合格基底")
    elif base_quality_score > 0:
        score += 8.0
        reason_parts.append(f"基底{base_quality_score:.0f}分")
    else:
        # Fallback to weeks-based
        if base_weeks >= 20:
            score += 30.0
            reason_parts.append("大型基底")
        elif base_weeks >= 12:
            score += 24.0
            reason_parts.append("中型基底")
        else:
            score += 12.0
            reason_parts.append(f"基底{base_weeks}周")

    # 突破距离 (25分) — 越近越高
    if distance_pct >= -1.0:
        score += 25.0
        reason_parts.append("紧贴突破位")
    elif distance_pct >= -3.0:
        score += 18.0
        reason_parts.append("接近突破位")
    else:
        score += 8.0
        reason_parts.append("逼近基底顶")

    # RS 强度 (20分)
    rs_rank = latest.get("rs_rank_pct")
    if pd.notna(rs_rank):
        rs_val = float(rs_rank)
        if rs_val >= 85:
            score += 20.0; reason_parts.append("RS优秀")
        elif rs_val >= 70:
            score += 12.0; reason_parts.append("RS良好")
        elif rs_val >= 50:
            score += 6.0; reason_parts.append("RS中等")
        else:
            score += 0.0; reason_parts.append("RS偏弱")

    # 量能 (15分)
    if vol_ratio >= 2.0:
        score += 15.0; reason_parts.append("倍量突破")
    elif vol_ratio >= 1.5:
        score += 10.0; reason_parts.append("放量突破")
    else:
        score += 5.0; reason_parts.append("温和放量")

    # 上方空间 (10分)
    if pd.notna(headroom_pct):
        hp = float(headroom_pct)
        if hp >= 15:
            score += 10.0; reason_parts.append("空间充足")
        elif hp >= 8:
            score += 6.0; reason_parts.append("空间良好")
        else:
            score += 3.0; reason_parts.append("空间偏紧")

    return {
        "transition_candidate": True,
        "transition_score": min(score, 100.0),
        "transition_reason": " / ".join(reason_parts),
        "transition_base_weeks": base_weeks,
        "transition_base_high": base_high,
        "transition_distance_pct": round(distance_pct, 1),
        "transition_volume_ratio": round(vol_ratio, 2),
        "transition_ma_slope_change": True,
    }


def _detect_bases_strict(
    recent: pd.DataFrame,
    config: AppConfig,
    min_weeks: int = 8,
    max_range_pct: float = 15.0,
) -> list[dict[str, object]]:
    """比 _detect_bases 更严格的基底检测：自定义最小周数和最大振幅。"""
    if recent.empty:
        return []

    flat_mask: list[bool] = []
    for i in range(len(recent)):
        row = recent.iloc[i]
        ok = _is_flat_base(row, config)
        if ok:
            # 额外检查振幅
            base_range = row.get("base_range_pct")
            if pd.notna(base_range) and float(base_range) <= max_range_pct:
                flat_mask.append(True)
            else:
                flat_mask.append(False)
        else:
            flat_mask.append(False)

    bases: list[dict[str, object]] = []
    in_base = False
    base_start = 0
    for i, is_flat in enumerate(flat_mask):
        if is_flat and not in_base:
            in_base = True
            base_start = i
        elif not is_flat and in_base:
            in_base = False
            length = i - base_start
            if length >= min_weeks:
                bases.append({
                    "start_idx": base_start,
                    "end_idx": i - 1,
                    "high": float(recent["high"].iloc[base_start:i].max()),
                    "low": float(recent["low"].iloc[base_start:i].min()),
                    "weeks": length,
                })
    if in_base:
        length = len(flat_mask) - base_start
        if length >= min_weeks:
            bases.append({
                "start_idx": base_start,
                "end_idx": len(recent) - 1,
                "high": float(recent["high"].iloc[base_start:].max()),
                "low": float(recent["low"].iloc[base_start:].min()),
                "weeks": length,
            })
    return bases


def _detect_slope_turn(recent: pd.DataFrame) -> bool:
    """检测 MA30w 斜率是否刚转升（近4周内从≤0变为>0）。"""
    if "ma_30w_slope" not in recent.columns:
        return False

    slopes = recent["ma_30w_slope"].dropna().values
    if len(slopes) < 5:
        return recent["ma_30w_slope"].iloc[-1] > 0 if pd.notna(recent["ma_30w_slope"].iloc[-1]) else False

    # 最新斜率必须 > 0
    if slopes[-1] <= 0:
        return False

    # 近4周内必须出现过 ≤0（说明是"转升"不是"一直升"）
    lookback = min(4, len(slopes) - 1)
    recent_slopes = slopes[-(lookback + 1):-1]
    return any(s <= 0 for s in recent_slopes)


def _compute_weekly_volume_ratio(latest: pd.Series) -> float | None:
    """计算周量比 = 本周成交量 / 10周均量。"""
    vol = latest.get("volume")
    vol_ma10 = latest.get("weekly_volume_ma_10")
    if pd.isna(vol) or pd.isna(vol_ma10) or float(vol_ma10) <= 0:
        return None
    return round(float(vol) / float(vol_ma10), 2)


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
    base_breakout_price = latest.get("base_breakout_price")
    weekly_volume_ma_10 = latest.get("weekly_volume_ma_10")
    volume = latest.get("volume")
    rs_rank_pct = latest.get("rs_rank_pct")
    rs_composite = latest.get("rs_composite")

    base_extension_pct = None
    if pd.notna(base_breakout_price) and float(base_breakout_price) > 0:
        base_extension_pct = (float(latest["close"]) / float(base_breakout_price) - 1.0) * 100.0

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

    if base_extension_pct is not None:
        if -3 <= base_extension_pct <= 3:
            score += 15.0
            reason.append("基底最佳买点")
        elif 3 < base_extension_pct <= 8:
            score += 10.0
            reason.append("靠近基底突破位")
        elif 8 < base_extension_pct <= 15:
            score += 5.0
            reason.append("基底突破后小幅扩展")
        elif base_extension_pct > 15:
            score += 1.0

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
        "breakout_quality_score": 0.0,
        "safety_score": 0.0,
        "final_score": 0.0,
        "stage2_score": 0.0,
        "stage2_reason": "普通趋势",
        "stage2_watch_score": 0.0,
        "stage2_watch_reason": "普通趋势",
        "industry_rs_rank_pct": 50.0,
        "industry_breadth": 0.0,
    }
    if scored.empty:
        for column, default in score_columns.items():
            scored[column] = pd.Series(dtype="float64" if isinstance(default, float) else "object")
        return scored

    profile = scored.apply(lambda row: pd.Series(_build_stage2_profile(row, config)), axis=1)
    for column in profile.columns:
        scored[column] = profile[column]
    return scored


# Risk gate: stocks with risk_score above this threshold are eliminated.
RISK_GATE_THRESHOLD = 70


def _build_stage2_profile(row: pd.Series, config: AppConfig) -> dict[str, object]:
    structure_score = _score_structure(row)
    timing_score = _score_timing(row, config)
    strength_score = _score_strength(row)
    breakout_quality_score = _score_breakout_quality(row, config)
    risk_score = _score_risk(row, config)
    safety_score = _clamp_score(100.0 - risk_score)
    extension_score = _score_extension(row)
    stage2_age_bonus = _score_stage2_age(row)

    # ── 补充基本面评分（股东人数/北向资金/资金流） ──
    holder_score = _to_float(row.get("holder_score")) or 0.0
    nb_score = _to_float(row.get("nb_score")) or 0.0
    moneyflow_confirm = _to_float(row.get("moneyflow_confirm")) or 0.0

    # Risk gate: stocks that are too risky are eliminated outright.
    if risk_score > RISK_GATE_THRESHOLD:
        reason = "风险过高，被过滤"
        return {
            "structure_score": structure_score,
            "timing_score": timing_score,
            "strength_score": strength_score,
            "risk_score": risk_score,
            "breakout_quality_score": breakout_quality_score,
            "safety_score": safety_score,
            "extension_score": extension_score,
            "stage2_age_bonus": stage2_age_bonus,
            "holder_score": holder_score,
            "nb_score": nb_score,
            "moneyflow_confirm": moneyflow_confirm,
            "final_score": 0.0,
            "stage2_score": 0.0,
            "stage2_reason": reason,
            "stage2_watch_score": 0.0,
            "stage2_watch_reason": reason,
        }

    # Final weighted score: buy-point oriented.
    # Timing (entry position) and strength (RS) are the primary drivers,
    # with extension (distance from MA) preventing chase-on-extended setups.
    final_score = _clamp_score(
        structure_score * 0.20          # 趋势结构完整性
        + timing_score * 0.30           # 买点时机 (权重最高, 最接近突破位)
        + strength_score * 0.20         # RS强度
        + breakout_quality_score * 0.15 # 突破质量
        + safety_score * 0.05           # 基础安全
        + extension_score * 0.10        # 扩展度惩罚 (离30周线越近越好)
        + stage2_age_bonus              # 新鲜度奖励 (刚转Stage II加分)
        + holder_score * 0.05           # 股东人数（筹码集中度加分）
        + nb_score * 0.05               # 北向资金（机构资金确认）
        + moneyflow_confirm * 0.05      # 资金流确认（大单净流入加分）
    )
    reason = _build_stage2_reason(row, structure_score, timing_score, strength_score, risk_score)
    return {
        "structure_score": structure_score,
        "timing_score": timing_score,
        "strength_score": strength_score,
        "risk_score": risk_score,
        "breakout_quality_score": breakout_quality_score,
        "safety_score": safety_score,
        "extension_score": extension_score,
        "stage2_age_bonus": stage2_age_bonus,
        "holder_score": holder_score,
        "nb_score": nb_score,
        "moneyflow_confirm": moneyflow_confirm,
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
    """How favourable is the current price position for entry.

    Uses base_breakout_price (fixed base top, not rolling max) to compute
    true extension from the Weinstein buy point.  Falls back to breakout_pct
    (rolling resistance) when no base is detected.
    """
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    base_breakout_price = _to_float(row.get("base_breakout_price"))
    close_price = _to_float(row.get("close"))

    score = 0.0

    # Price relative to the 30-week MA — the core Weinstein entry metric.
    if price_vs_ma_pct is not None:
        if 0.0 <= price_vs_ma_pct <= 8.0:
            score += 50.0
        elif -5.0 <= price_vs_ma_pct < 0.0:
            score += 40.0
        elif 8.0 < price_vs_ma_pct <= config.strategy.watch_max_price_vs_ma_pct:
            score += 30.0
        elif -10.0 <= price_vs_ma_pct < -5.0:
            score += 20.0
        else:
            score += 10.0

    # Distance from base breakout price (fixed buy point).
    # This replaces the old rolling-max breakout_pct which drifted upward
    # as price rallied, hiding true extension.
    if base_breakout_price is not None and base_breakout_price > 0 and close_price is not None:
        base_ext_pct = (close_price / base_breakout_price - 1.0) * 100.0
        if -2.0 <= base_ext_pct <= 3.0:
            score += 45.0      # 最佳买点：刚突破或即将突破
        elif 3.0 < base_ext_pct <= 8.0:
            score += 35.0      # 可追但略贵
        elif 8.0 < base_ext_pct <= 15.0:
            score += 20.0      # 偏扩展
        elif 15.0 < base_ext_pct <= 20.0:
            score += 8.0       # 明显扩展
        elif base_ext_pct > 20.0:
            score += 0.0       # 严重扩展，不追
        else:
            score += 15.0      # 仍在基底下方，等待突破
    else:
        # Fallback: no base detected, use legacy breakout_pct
        breakout_pct = _to_float(row.get("breakout_pct"))
        if breakout_pct is not None:
            if -2.0 <= breakout_pct <= 2.0:
                score += 40.0
            elif (-5.0 <= breakout_pct < -2.0) or (2.0 < breakout_pct <= config.strategy.watch_breakout_max_pct):
                score += 25.0
            elif breakout_pct > config.strategy.watch_breakout_max_pct:
                score += 8.0
            else:
                score += 15.0

    return _clamp_score(score)


def _score_breakout_quality(row: pd.Series, config: AppConfig) -> float:
    """Rate the quality of the breakout event itself — status + volume.

    When a base is detected, uses the true base-breakout distance (fixed
    base top) instead of the rolling-max breakout_pct which drifts.
    """
    breakout_status = str(row.get("breakout_status") or "no_breakout_level")
    volume_ratio = _to_float(row.get("volume_ratio"))
    breakout_ok = _to_bool(row.get("breakout_ok"))
    base_breakout_price = _to_float(row.get("base_breakout_price"))
    close_price = _to_float(row.get("close"))
    base_breakout_fixed = _to_bool(row.get("base_breakout_fixed"))

    score = 0.0

    # Base-based extension first (more accurate than rolling breakout_status)
    if base_breakout_price is not None and base_breakout_price > 0 and close_price is not None:
        base_ext_pct = (close_price / base_breakout_price - 1.0) * 100.0
        if base_ext_pct <= 0:
            score += 22.0       # 仍在基底内，等待突破
        elif base_ext_pct <= 3.0:
            score += 50.0       # 刚突破基底，最佳
        elif base_ext_pct <= 8.0:
            score += 35.0       # 小幅扩展，尚可
        elif base_ext_pct <= 15.0:
            score += 15.0       # 明显扩展
        elif base_ext_pct <= 20.0:
            score += 5.0        # 严重扩展
        else:
            score += 0.0        # 远超基底，不追
    else:
        # Fallback: no base detected, use old breakout_status logic
        score += {
            "just_broke_out": 42.0,
            "near_breakout": 38.0,
            "below_breakout": 18.0,
            "extended_breakout": 5.0,
            "no_breakout_level": 14.0,
        }.get(breakout_status, 14.0)

    # Volume confirmation — stronger volume = higher conviction.
    if volume_ratio is not None:
        if volume_ratio >= 3.0:
            score += 32.0
        elif volume_ratio >= 1.8:
            score += 26.0
        elif volume_ratio >= 1.2:
            score += 18.0
        elif volume_ratio >= 0.9:
            score += 10.0
        else:
            score += 3.0

    if breakout_ok:
        score += 12.0

    return _clamp_score(score)


def _score_strength(row: pd.Series) -> float:
    rs_rank_pct = _to_float(row.get("rs_rank_pct"))
    rs_composite = _to_float(row.get("rs_composite"))
    industry_rs_rank_pct = _to_float(row.get("industry_rs_rank_pct"))
    industry_breadth = _to_float(row.get("industry_breadth"))

    #── 个股相对强度（60%）──
    rank_score = 0.0 if rs_rank_pct is None else _clamp_score(100.0 - max(rs_rank_pct - 1.0, 0.0) * 1.4)
    composite_score = 0.0 if rs_composite is None else _clamp_score(50.0 + rs_composite * 250.0)
    stock_score = rank_score * 0.80 + composite_score * 0.20

    #── 行业相对强度（25%）：industry_rs_rank_pct 越高越好（100=最强行业）──
    industry_rs_score = 0.0
    if industry_rs_rank_pct is not None:
        # Percentile directly as score: 100 = top industry
        industry_rs_score = _clamp_score(industry_rs_rank_pct * 0.95 + 5.0)
        # Bonus for top-quartile industries
        if industry_rs_rank_pct >= 90.0:
            industry_rs_score += 6.0
        elif industry_rs_rank_pct >= 75.0:
            industry_rs_score += 3.0

    #── 行业广度（15%）：行业内RS为正的股票占比 ──
    breadth_score = 0.0
    if industry_breadth is not None:
        breadth_score = _clamp_score(industry_breadth * 0.85 + 15.0)

    score = stock_score * 0.60 + industry_rs_score * 0.25 + breadth_score * 0.15
    if rs_rank_pct is not None and rs_rank_pct <= 10.0:
        score += 6.0
    elif rs_rank_pct is not None and rs_rank_pct <= 20.0:
        score += 3.0
    return _clamp_score(score)


def _score_extension(row: pd.Series) -> float:
    """Score how close the stock is to its 30-week MA AND base breakout price.

    Lower extension = better entry point.  Weights: 60% MA distance, 40% base distance.
    """
    extension_pct = _to_float(row.get("extension_pct"))
    base_breakout_price = _to_float(row.get("base_breakout_price"))
    close_price = _to_float(row.get("close"))

    ma_score = 0.0
    if extension_pct is not None:
        if extension_pct <= 5.0:
            ma_score = 100.0
        elif extension_pct <= 10.0:
            ma_score = 90.0
        elif extension_pct <= 15.0:
            ma_score = 70.0
        elif extension_pct <= 20.0:
            ma_score = 40.0
        else:
            ma_score = 10.0

    base_score = 50.0  # neutral: no base detected
    if base_breakout_price is not None and base_breakout_price > 0 and close_price is not None:
        base_ext_pct = (close_price / base_breakout_price - 1.0) * 100.0
        if base_ext_pct <= 3.0:
            base_score = 100.0
        elif base_ext_pct <= 8.0:
            base_score = 85.0
        elif base_ext_pct <= 15.0:
            base_score = 50.0
        elif base_ext_pct <= 20.0:
            base_score = 20.0
        else:
            base_score = 0.0

    return ma_score * 0.60 + base_score * 0.40


def _score_stage2_age(row: pd.Series) -> float:
    """Bonus for stocks that recently entered Stage II (freshness matters)."""
    age = row.get("stage2_age_weeks")
    if age is None or pd.isna(age):
        stage_label = str(row.get("stage_label") or "")
        if stage_label != "II":
            return 0.0
        age = 0
    else:
        age = int(age)
    if age <= 8:
        return 20.0
    if age <= 16:
        return 15.0
    if age <= 24:
        return 10.0
    return 0.0


def _score_risk(row: pd.Series, config: AppConfig) -> float:
    breakout_status = str(row.get("breakout_status") or "no_breakout_level")
    stage_label = str(row.get("stage_label") or "UNKNOWN")
    price_vs_ma_pct = _to_float(row.get("price_vs_ma_pct"))
    breakout_pct = _to_float(row.get("breakout_pct"))
    headroom_pct = _to_float(row.get("headroom_pct"))
    base_breakout_price = _to_float(row.get("base_breakout_price"))
    close_price = _to_float(row.get("close"))

    score = 0.0
    if not _to_bool(row.get("market_ok")):
        score += 18.0
    if not _to_bool(row.get("base_flatness_ok")):
        score += 14.0
    if not _to_bool(row.get("volume_ok")):
        score += 10.0
    # 突破期/临近突破的股票天然靠近阻力位，头寸惩罚已由 headroom_pct 单独处理
    if not _to_bool(row.get("resistance_ok")):
        if breakout_status not in {"near_breakout", "just_broke_out", "below_breakout"}:
            score += 14.0

    # Extension penalty: use base_breakout_price when available
    # (fixed base top — accurate), fall back to legacy breakout_pct
    if base_breakout_price is not None and base_breakout_price > 0 and close_price is not None:
        base_ext_pct = (close_price / base_breakout_price - 1.0) * 100.0
        if base_ext_pct > 20.0:
            score += 28.0      # 远离基底突破位，严重扩展
        elif base_ext_pct > 15.0:
            score += 20.0      # 明显扩展
        elif base_ext_pct > 8.0:
            score += 10.0      # 小幅扩展
    else:
        # Legacy: rolling breakout_pct (less accurate, drifts with price)
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
        elif price_vs_ma_pct > 8.0:
            score += 6.0
        elif price_vs_ma_pct < -8.0:
            score += 14.0

    if headroom_pct is not None:
        if headroom_pct < 5.0:
            score += 24.0
        elif headroom_pct < 8.0:
            score += 18.0
        elif headroom_pct < config.strategy.resistance_min_headroom_pct:
            score += 10.0

    # Overhead supply penalty: heavy trapped sellers = risk
    overhead_supply_pct = _to_float(row.get("overhead_supply_pct"))
    if overhead_supply_pct is not None:
        if overhead_supply_pct > 60.0:
            score += 18.0       # very heavy overhead supply
        elif overhead_supply_pct > 40.0:
            score += 10.0       # moderate overhead supply
        elif overhead_supply_pct > 20.0:
            score += 4.0        # light overhead supply

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

    # Base breakout extension (fixed base top, accurate)
    base_breakout_price = _to_float(row.get("base_breakout_price"))
    close_price = _to_float(row.get("close"))
    if base_breakout_price is not None and base_breakout_price > 0 and close_price is not None:
        base_ext_pct = (close_price / base_breakout_price - 1.0) * 100.0
        if base_ext_pct <= 3.0:
            bits.append("基底突破买点")
        elif base_ext_pct <= 8.0:
            bits.append("靠近基底突破位")
        elif base_ext_pct <= 15.0:
            bits.append("基底突破后扩展")
        elif base_ext_pct > 15.0:
            bits.append("远离基底(追高风险)")
    else:
        # Fallback to old breakout_status labels
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

    # Extension info
    extension_pct = _to_float(row.get("extension_pct"))
    if extension_pct is not None:
        if extension_pct > 15.0:
            bits.append("远离30周线")
        elif extension_pct > 10.0:
            bits.append("偏扩展")
        elif extension_pct <= 5.0:
            bits.append("紧贴均线")
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
