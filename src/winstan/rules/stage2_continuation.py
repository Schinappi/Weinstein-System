"""Stage2 续涨形态识别 (Stage2 Continuation Pattern) — v2 箱体纪律版

识别「暴涨→回踩 MA30w→箱式震荡→续涨」形态。
核心升级：不再用固定窗口振幅，改为自动检测自然箱体并评估纪律。

5维评分：
1. 前期趋势强度 (25) — MA30w 斜率
2. 回踩深度 (25) — 价格距MA30w距离
3. 箱体纪律 (25) — 🆕 箱体检测+触碰频率+越界惩罚+持续加分
4. 量能趋势 (15) — 🆕 箱体内量能递减
5. 波动压缩 (10) — ATR 排位（加分项）
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from winstan.config import AppConfig

# 维度权重
WEIGHT_TREND = 25
WEIGHT_PULLBACK = 25
WEIGHT_BOX = 25      # 🆕 箱体纪律
WEIGHT_VOLUME = 15   # 🆕 箱体内量能趋势
WEIGHT_ATR = 10

GRADE_THRESHOLDS = [
    (85, "S", "极优续涨"),
    (70, "A", "优质续涨"),
    (50, "B", "合格续涨"),
    (0,  "C", "一般"),
]


def compute_continuation_quality(
    weekly: pd.DataFrame,
    config: AppConfig,
    daily: pd.DataFrame | None = None,
) -> dict[str, object]:
    default: dict[str, object] = {
        "cont_quality_score": 0.0,
        "cont_quality_grade": "C",
        "cont_quality_reason": "无数据",
        "cont_score_trend": 0.0,
        "cont_score_pullback": 0.0,
        "cont_score_box": 0.0,
        "cont_score_volume": 0.0,
        "cont_score_atr": 0.0,
        "cont_ma30w_slope_10w": None,
        "cont_pullback_pct": None,
        "cont_box_range_pct": None,
        "cont_box_duration_weeks": 0,
        "cont_box_touch_count": 0,
        "cont_box_penetration_pct": None,
        "cont_volume_trend_ok": False,
        "cont_atr_rank_pct": None,
        "cont_is_applicable": False,
        "cont_prior_trend_ok": False,
        "cont_box_top_slope": None,
        "cont_box_bottom_slope": None,
        "cont_box_top_displacement_pct": None,
        "cont_box_bottom_displacement_pct": None,
        "cont_box_drift_monthly": None,
        "cont_box_center_drift_pct": None,
        "cont_box_tilt_ratio": None,
        "cont_stage1_box_type": None,
        "cont_stage1_box_detail": None,
        "cont_score_stage1": 0.0,
        "cont_box_start_idx": 0,
    }

    if weekly.empty or "close" not in weekly.columns or len(weekly) < 30:
        return default

    working = weekly.copy()

    # ── EMA144（日线144日指数均线）替代 MA30w ──
    ema144_vals = None
    if daily is not None and not daily.empty and "close" in daily.columns:
        daily_sorted = daily.sort_values("trade_date").copy()
        daily_sorted["ema144"] = daily_sorted["close"].ewm(span=144, min_periods=1).mean()
        daily_sorted["trade_date"] = pd.to_datetime(daily_sorted["trade_date"])
        working["_d"] = pd.to_datetime(working["trade_date"])
        ema_map = {}
        for _, wr in working.iterrows():
            wd = wr["_d"]
            mask = daily_sorted["trade_date"] <= wd
            if mask.any():
                ema_map[wd] = float(daily_sorted.loc[mask, "ema144"].iloc[-1])
        working["ema144"] = working["_d"].map(ema_map)
        ema144_vals = working["ema144"].dropna()
        working.drop(columns=["_d"], inplace=True)

    if ema144_vals is None or len(ema144_vals) < 10:
        # 回退：周线 EMA30（指数加权，比 SMA 更敏感）
        working["ema_30w"] = working["close"].ewm(span=30, min_periods=1).mean()
        ema144_vals = working["ema_30w"].dropna()
        if len(ema144_vals) < 10:
            return default

    slope_10w = (float(ema144_vals.iloc[-1]) / float(ema144_vals.iloc[-10]) - 1.0) * 100.0
    # 5周斜率：捕捉近期走平（10周可能包含早期急跌）
    slope_5w = (float(ema144_vals.iloc[-1]) / float(ema144_vals.iloc[-5]) - 1.0) * 100.0 if len(ema144_vals) >= 5 else slope_10w
    # 取较好的（更接近0的那个），避免10周早期急跌掩盖近期走平
    slope_best = slope_5w if abs(slope_5w) < abs(slope_10w) else slope_10w

    # ── 前期下跌确认：Stage I = 下跌结束 + EMA走平 ──
    # 用全历史做峰→谷对比，不限制谷值距今时间
    # 改为：当前 EMA 是否仍在谷底附近（未大幅反弹）
    lookback = min(300, len(ema144_vals))
    if lookback >= 40:
        window = ema144_vals.iloc[-lookback:]
        ma_peak = float(window.max())
        ma_trough = float(window.min())
        ema_now = float(ema144_vals.iloc[-1])
        # 峰→谷跌幅达标
        if lookback >= 200:
            enough_decline = (ma_trough / ma_peak - 1.0) * 100.0 < -20.0
        elif lookback >= 120:
            enough_decline = (ma_trough / ma_peak - 1.0) * 100.0 < -15.0
        elif lookback >= 80:
            enough_decline = (ma_trough / ma_peak - 1.0) * 100.0 < -10.0
        else:
            enough_decline = (ma_trough / ma_peak - 1.0) * 100.0 < -6.0
        # 当前 EMA 仍在谷底附近（未涨超 20%）→ 说明下跌后一直在筑底
        # 当前必须在峰谷区间的下半段：离谷近，离峰远
        # (current - trough) / (peak - trough) < 0.4 → 更接近谷
        peak_trough_range = ma_peak - ma_trough
        if peak_trough_range > 0:
            position_in_range = (ema_now - ma_trough) / peak_trough_range
            in_lower_half = position_in_range < 0.4
        else:
            in_lower_half = True
        # 从谷反弹不超过 15%（在谷底附近）
        still_near_trough = ma_trough > 0 and (ema_now / ma_trough - 1.0) * 100.0 < 15.0
        long_term_decline = enough_decline and in_lower_half and still_near_trough
    else:
        long_term_decline = False
    ma_flattening = -2.0 <= slope_best <= 4.0
    prior_trend_ok = long_term_decline and ma_flattening

    if not prior_trend_ok:
        reason_parts = []
        if not long_term_decline:
            reason_parts.append("无前期下跌" if len(ema144_vals) >= 30 else "数据不足")
        if not ma_flattening:
            reason_parts.append(f"EMA斜率10w={slope_10w:.1f}% 5w={slope_5w:.1f}%（需-2%~4%）")
        return {
            **default,
            "cont_quality_reason": "；".join(reason_parts),
            "cont_ma30w_slope_10w": round(slope_10w, 2),
            "cont_prior_trend_ok": False,
        }

    trend_score = _score_prior_trend(slope_10w)
    pullback_score, pullback_pct = _score_pullback_depth(working)
    box_score, box_info = _score_box_discipline(working)
    volume_score, vol_ok = _score_volume_trend(working, box_info.get("box_start_idx", 0))
    atr_score, atr_rank = _score_atr_compression(working, daily)
    stage1_bonus = _score_stage1_quality(box_info)

    total = trend_score + pullback_score + box_score + volume_score + atr_score + stage1_bonus
    total = min(total, 100.0)

    grade, grade_label = "C", "一般"
    for threshold, g, label in GRADE_THRESHOLDS:
        if total >= threshold:
            grade, grade_label = g, label
            break

    reason_parts: list[str] = []
    if grade != "C":
        reason_parts.append(grade_label)
    if pullback_pct is not None:
        reason_parts.append(f"距MA30w+{pullback_pct:.0f}%")
    box_range = box_info.get("box_range_pct")
    if box_range is not None:
        reason_parts.append(f"箱幅{box_range:.0f}%")
    box_dur = box_info.get("box_duration_weeks", 0)
    if box_dur >= 8:
        reason_parts.append(f"{box_dur}周箱体")
    if vol_ok:
        reason_parts.append("量缩")
    if atr_score >= 5:
        reason_parts.append("波压")
    stage1_type = box_info.get("stage1_box_type")
    if stage1_type == "优秀":
        reason_parts.append("基底优良")
    elif stage1_type == "可接受":
        reason_parts.append("基底可接受")
    elif stage1_type == "需警惕":
        reason_parts.append("基底需警惕")

    return {
        "cont_quality_score": round(total, 1),
        "cont_quality_grade": grade,
        "cont_quality_reason": " / ".join(reason_parts) if reason_parts else "续涨待确认",
        "cont_score_trend": round(trend_score, 1),
        "cont_score_pullback": round(pullback_score, 1),
        "cont_score_box": round(box_score, 1),
        "cont_score_volume": round(volume_score, 1),
        "cont_score_atr": round(atr_score, 1),
        "cont_score_stage1": round(stage1_bonus, 1),
        "cont_ma30w_slope_10w": round(slope_10w, 2),
        "cont_pullback_pct": round(pullback_pct, 1) if pullback_pct is not None else None,
        "cont_box_range_pct": round(box_range, 1) if box_range is not None else None,
        "cont_box_duration_weeks": box_dur,
        "cont_box_touch_count": box_info.get("touch_count", 0),
        "cont_box_penetration_pct": round(box_info.get("penetration_pct", 100.0), 1),
        # 四维结构分明细
        "cont_box_flatness": box_info.get("box_flatness"),
        "cont_box_conv": box_info.get("box_conv"),
        "cont_box_vol_low": box_info.get("box_vol_low"),
        "cont_box_no_trend": box_info.get("box_no_trend"),
        "cont_volume_trend_ok": vol_ok,
        "cont_atr_rank_pct": round(atr_rank, 1) if atr_rank is not None else None,
        "cont_is_applicable": True,
        "cont_prior_trend_ok": prior_trend_ok,
        "cont_pool_b": False,
        "cont_box_top_slope": box_info.get("box_top_slope_monthly"),
        "cont_box_bottom_slope": box_info.get("box_bottom_slope_monthly"),
        "cont_box_top_displacement_pct": box_info.get("box_top_displacement_pct"),
        "cont_box_bottom_displacement_pct": box_info.get("box_bottom_displacement_pct"),
        "cont_box_drift_monthly": box_info.get("box_drift_monthly"),
        "cont_box_center_drift_pct": box_info.get("box_center_drift_pct"),
        "cont_box_tilt_ratio": box_info.get("box_tilt_ratio"),
        "cont_stage1_box_type": box_info.get("stage1_box_type"),
        "cont_stage1_box_detail": box_info.get("stage1_box_detail"),
        "cont_box_start_idx": box_info.get("box_start_idx", 0),
    }


# ════════════════════════════════════════════════════
#  维度1: 前期趋势强度（0-5%为理想缓升区间）
# ════════════════════════════════════════════════════

def _score_prior_trend(slope: float) -> float:
    """斜率评分：平/微跌也是 Stage 1 优质形态"""
    s = abs(slope)
    if s <= 1.0:
        return 22.0      # 几乎平
    elif s <= 2.0:
        return 18.0      # 微倾
    elif s <= 3.0:
        return 14.0
    elif s <= 4.0:
        return 10.0
    elif s <= 5.0:
        return 7.0       # 缓升
    elif s <= 8.0:
        return 5.0
    return 2.0


# ════════════════════════════════════════════════════
#  维度2: 回踩深度 (保持不变)
# ════════════════════════════════════════════════════

def _score_pullback_depth(df: pd.DataFrame) -> tuple[float, float | None]:
    if "close" not in df.columns:
        return 0.0, None
    close_val = float(df["close"].iloc[-1])
    # 优先使用 EMA144，回退到 MA30w
    if "ema144" in df.columns:
        ma_val = float(df["ema144"].iloc[-1])
    elif "ma_30w" in df.columns:
        ma_val = float(df["ma_30w"].iloc[-1])
    else:
        return 0.0, None
    if ma_val <= 0:
        return 0.0, None
    pct = (close_val / ma_val - 1.0) * 100.0

    if 0.0 <= pct <= 5.0:
        return 25.0, pct
    elif 5.0 < pct <= 10.0:
        return 20.0, pct
    elif 10.0 < pct <= 15.0:
        return 12.0, pct
    elif 15.0 < pct <= 20.0:
        return 5.0, pct
    elif -5.0 <= pct < 0.0:
        return 10.0, pct
    elif -10.0 <= pct < -5.0:
        return 3.0, pct
    return 0.0, pct


# ════════════════════════════════════════════════════
#  维度3: 箱体纪律 (🆕 核心升级)
# ════════════════════════════════════════════════════

def _score_box_discipline(df: pd.DataFrame) -> tuple[float, dict]:
    """箱体检测 + 纪律评分 (0-25)

    算法：
    1. 多窗口（30/24/18/12周）重试，确保早期箱体不被拉涨期淹没
    2. 找局部摆动高点和低点
    3. 对高点和低点分别做线性回归，形成上下轨
    4. 评估：触碰频率(10) + 越界控制(10) + 持续时间(5) = 25

    返回: (score, info_dict)
    """
    info: dict = {
        "box_range_pct": None,
        "box_duration_weeks": 0,
        "touch_count": 0,
        "penetration_pct": 100.0,
        "box_start_idx": 0,
        "box_valid": False,
    }

    if "high" not in df.columns or "low" not in df.columns or len(df) < 8:
        return 0.0, info

    # 多窗口重试：早期箱体（8-12周）在30周窗口里被拉涨期主导
    # 依次尝试更短的窗口，第一个成功就返回
    for lookback in [30, 24, 18, 12]:
        if lookback > len(df):
            continue
        segment = df.iloc[-lookback:].reset_index(drop=True)
        box_score, result_info = _detect_box_core(segment)
        if box_score > 0:
            offset = len(df) - lookback
            result_info["box_start_idx"] = result_info.get("box_start_idx", 0) + offset
            result_info["box_end_idx"] = result_info.get("box_end_idx", 0) + offset
            result_info["box_seg_offset"] = offset

            # ── 前期下跌确认已由 cont_prior_trend_ok 硬门檻处理 ──
            # 此处不再重复扣分，直接返回箱体结构分

            return box_score, result_info

    return 0.0, info


def _detect_box_core(segment: pd.DataFrame) -> tuple[float, dict]:
    """核心箱体检测：四维评分模型。

    box_score = 结构平整度(40%) + 均线收敛度(20%) + 波动收缩(20%) + 趋势缺失(20%)
    box_score > 0.75 → Stage1_Box = True

    segment 已经是截好的 [-lookback:] 切片，需包含足够历史用于MA计算。
    """
    info: dict = {
        "box_range_pct": None, "box_duration_weeks": 0, "touch_count": 0,
        "penetration_pct": 100.0, "box_start_idx": 0, "box_valid": False,
    }
    n = len(segment)
    if n < 8:
        return 0.0, info

    # 从后往前找最长的紧致区间作为箱体
    highs = segment["high"].values
    lows = segment["low"].values
    closes = segment["close"].values
    volumes = segment["volume"].values if "volume" in segment.columns else np.ones(n)

    best_score = 0.0
    best_start = 0
    best_top = 0.0
    best_bottom = 0.0
    best_detail = {"flatness": 0.0, "conv": 0.0, "vol_low": 0.0, "no_trend": 0.0}

    for box_start in range(0, n - 3):
        box_n = n - box_start
        if box_n < 3:
            continue
        h = highs[box_start:]
        l = lows[box_start:]
        c = closes[box_start:]
        v = volumes[box_start:]

        box_high = float(np.max(h))
        box_low = float(np.min(l))
        if box_low <= 0 or box_high <= box_low:
            continue
        box_range = (box_high / box_low - 1.0) * 100.0
        if box_range > 35.0 or box_range < 3.0:
            continue

        # 包含率：水平箱体必须兜住大部分 bar
        inside = 0
        for i in range(box_n):
            if l[i] >= box_low * 0.97 and h[i] <= box_high * 1.03:
                inside += 1
        inside_pct = inside / box_n * 100.0
        if inside_pct < 65.0:
            continue

        # 1. 结构平整度 (40%): 振幅≤15%优秀, ≤25%合格, ≤35%勉强
        x = np.arange(box_n, dtype=float)
        slope, _ = np.polyfit(x, c, 1)
        trend_pct = abs(slope * box_n) / np.mean(c) * 100.0
        flatness = max(0.0, 1.0 - box_range / 35.0) * 0.6 + max(0.0, 1.0 - trend_pct / 8.0) * 0.4

        # 2. 均线收敛度 (20%): spread ≤15% 都算合理
        conv = 0.5
        if "ma_10w" in segment.columns and "ma_30w" in segment.columns:
            ma10_end = float(segment["ma_10w"].iloc[-1])
            ma30_end = float(segment["ma_30w"].iloc[-1])
            if ma30_end > 0:
                spread_end = abs(ma10_end / ma30_end - 1.0) * 100.0
                conv = max(0.0, 1.0 - spread_end / 15.0)

        # 3. 波动收缩 (20%): 箱体内量能 vs 箱体前量能
        vol_low = 0.5
        if box_start > 0:
            pre_vol = np.mean(volumes[:box_start]) if box_start > 0 else np.mean(v)
            box_vol = np.mean(v)
            vol_ratio = box_vol / pre_vol if pre_vol > 0 else 1.0
            vol_low = max(0.0, min(1.0, 1.5 - vol_ratio))

        # 4. 趋势缺失 (20%): 箱体内涨幅≤15%都不大幅扣分
        close_chg = abs(c[-1] / c[0] - 1.0) * 100.0
        no_trend = max(0.0, 1.0 - close_chg / 15.0) * 0.5 + max(0.0, 1.0 - trend_pct / 8.0) * 0.5

        box_score = flatness * 0.4 + conv * 0.2 + vol_low * 0.2 + no_trend * 0.2

        if box_score > best_score:
            best_score = box_score
            best_start = box_start
            best_top = box_high
            best_bottom = box_low
            best_detail = {"flatness": flatness, "conv": conv, "vol_low": vol_low, "no_trend": no_trend}

    if best_score < 0.60:
        return 0.0, info

    # 用最佳起点构造水平箱体
    box_start = best_start
    box_weeks = n - box_start
    b_h = best_top
    b_l = best_bottom
    box_range_pct = (b_h / b_l - 1.0) * 100.0

    # 转换为 0-25 的 box_score 给续涨评分用
    # 注意：实际 box_score 会被 _score_box_discipline 的前期下跌因子调整
    normalized_box = best_score * 25.0

    info["box_valid"] = True
    info["box_range_pct"] = box_range_pct
    info["box_duration_weeks"] = box_weeks
    info["box_start_idx"] = box_start
    info["box_end_idx"] = n - 1
    # 四维评分明细
    info["box_flatness"] = round(best_detail["flatness"], 3)
    info["box_conv"] = round(best_detail["conv"], 3)
    info["box_vol_low"] = round(best_detail["vol_low"], 3)
    info["box_no_trend"] = round(best_detail["no_trend"], 3)
    info["box_a_h"] = 0.0
    info["box_b_h"] = float(b_h)
    info["box_a_l"] = 0.0
    info["box_b_l"] = float(b_l)
    info["box_top_displacement_pct"] = 0.0
    info["box_bottom_displacement_pct"] = 0.0
    info["box_drift_monthly"] = 0.0
    info["box_center_drift_pct"] = 0.0
    info["box_tilt_ratio"] = 0.0
    info["box_top_slope_monthly"] = 0.0
    info["box_bottom_slope_monthly"] = 0.0

    # 基底质量
    if best_score >= 0.80:
        info["stage1_box_type"] = "优秀"
        info["stage1_box_detail"] = f"紧致横盘，振幅{box_range_pct:.1f}%"
    elif best_score >= 0.70:
        info["stage1_box_type"] = "可接受"
        info["stage1_box_detail"] = f"横盘整理，振幅{box_range_pct:.1f}%"
    else:
        info["stage1_box_type"] = "一般"
        info["stage1_box_detail"] = f"振幅{box_range_pct:.1f}%"

    return normalized_box, info


# ════════════════════════════════════════════════════
#  维度4: 量能趋势
# ════════════════════════════════════════════════════

def _score_volume_trend_stub_placeholder(df, box_start_idx=0):
    return 0.0, False


# ════════════════════════════════════════════════════
#  维度4: 量能趋势 (旧函数，将被替换)
# ════════════════════════════════════════════════════
    swing_low_idx = []
    order = 1 if n <= 18 else 2
    for i in range(order, n - order):
        if highs[i] == highs[i-order:i+order+1].max():
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                swing_high_idx.append(i)
        if lows[i] == lows[i-order:i+order+1].min():
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                swing_low_idx.append(i)

    # 摆点质量分：2+2满分，1+1及格但扣分
    swing_quality = 1.0
    if len(swing_high_idx) < 2 or len(swing_low_idx) < 2:
        if len(swing_high_idx) < 1 or len(swing_low_idx) < 1:
            return 0.0, info  # 完全没有摆点，放弃
        swing_quality = 0.5  # 摆点不足，总分打对折

    # 只取最近的极值点（砍掉早期杂点，避免箱体起点被拖偏）
    MAX_SWING_POINTS = 6
    if len(swing_high_idx) > MAX_SWING_POINTS:
        swing_high_idx = swing_high_idx[-MAX_SWING_POINTS:]
    if len(swing_low_idx) > MAX_SWING_POINTS:
        swing_low_idx = swing_low_idx[-MAX_SWING_POINTS:]

    # ── 异常摆点剔除：回归残差法（拟合→剔除残差>10%的摆点→重拟合一次）──
    def _refit_remove_outliers(indices: list[int], values: np.ndarray) -> list[int]:
        if len(indices) <= 2:
            return indices
        x = np.array(indices, dtype=float)
        y = values[indices]
        a, b = np.polyfit(x, y, 1)
        pred = a * x + b
        residuals = np.abs(y - pred) / np.abs(pred)
        max_i = int(np.argmax(residuals))
        if residuals[max_i] > 0.10 and len(indices) > 2:
            return [indices[i] for i in range(len(indices)) if i != max_i]
        return indices

    if len(swing_high_idx) >= 3:
        swing_high_idx = _refit_remove_outliers(swing_high_idx, highs)
    if len(swing_low_idx) >= 3:
        swing_low_idx = _refit_remove_outliers(swing_low_idx, lows)

    # ── 2. 水平箱体：Stage1 box = 供需均衡带，不是趋势拟合线 ──
    # 箱顶 = 区间最高价（水平阻力位），箱底 = 区间最低价（水平支撑位）
    # 拒绝回归拟合——回归斜线是 Stage2 channel，不是 Stage1 box
    box_start = int(min(swing_high_idx[0], swing_low_idx[0]))
    box_end = n - 1
    box_weeks = box_end - box_start

    if box_weeks < 3:
        return 0.0, info

    # 水平阻力/支撑（减1%缓冲避免毛刺影响）
    box_top = float(np.max(highs[box_start:box_end+1])) * 0.99
    box_bottom = float(np.min(lows[box_start:box_end+1])) * 1.01

    if box_bottom <= 0 or box_top <= box_bottom:
        return 0.0, info

    a_h = a_l = 0.0
    b_h = box_top
    b_l = box_bottom
    box_range_pct = (box_top / box_bottom - 1.0) * 100.0

    if box_range_pct > 35.0 or box_range_pct < 5.0:
        return 0.0, info

    # 水平箱体内包含率：价格是否真的在箱体内运行
    inside_count = 0
    margin = 0.03
    for i in range(box_start, box_end + 1):
        if lows[i] >= box_bottom * (1 - margin) and highs[i] <= box_top * (1 + margin):
            inside_count += 1
    inside_pct_pre = inside_count / (box_end - box_start + 1) * 100.0

    if inside_pct_pre < 60.0:
        return 0.0, info  # 水平线都兜不住 → 不是横盘

    # 水平线：漂移/倾斜/中枢全为零
    top_displacement_pct = 0.0
    bottom_displacement_pct = 0.0
    drift_per_month = 0.0
    tilt_ratio = 0.0
    center_drift_pct = 0.0
    slope_h_monthly = 0.0
    slope_l_monthly = 0.0
    drift_penalty = 1.0

    # ── 滑动窗口优化起点：水平线无需重拟合，只需找最佳包含率 ──
    best_inside_pct = inside_pct_pre
    best_box_start = box_start
    best_box_top = box_top
    best_box_bottom = box_bottom

    for trim in range(1, box_weeks - 3):
        test_start = box_start + trim
        test_top = float(np.max(highs[test_start:box_end+1])) * 0.99
        test_bottom = float(np.min(lows[test_start:box_end+1])) * 1.01
        if test_bottom <= 0 or test_top <= test_bottom:
            continue
        inside = 0
        total_b = box_end - test_start + 1
        for i in range(test_start, box_end + 1):
            if lows[i] >= test_bottom * 0.97 and highs[i] <= test_top * 1.03:
                inside += 1
        pct = inside / total_b * 100.0
        if pct > best_inside_pct:
            best_inside_pct = pct
            best_box_start = test_start
            best_box_top = test_top
            best_box_bottom = test_bottom

    inside_pct = best_inside_pct
    if inside_pct < 60.0:
        return 0.0, info

    # 用最佳起点更新
    box_start = int(best_box_start)
    box_weeks = box_end - box_start
    b_h = best_box_top
    b_l = best_box_bottom
    box_range_pct = (b_h / b_l - 1.0) * 100.0

    # 水平线：所有位移/漂移指标为零
    a_h = a_l = 0.0
    top_displacement_pct = 0.0
    bottom_displacement_pct = 0.0
    drift_per_month = 0.0
    tilt_ratio = 0.0
    center_drift_pct = 0.0
    _drift_penalty = 1.0

    info["box_valid"] = True
    info["box_range_pct"] = box_range_pct
    info["box_duration_weeks"] = box_weeks
    info["box_start_idx"] = box_start
    info["box_end_idx"] = box_end
    info["box_a_h"] = 0.0
    info["box_b_h"] = float(b_h)
    info["box_a_l"] = 0.0
    info["box_b_l"] = float(b_l)
    info["box_top_displacement_pct"] = 0.0
    info["box_bottom_displacement_pct"] = 0.0
    info["box_drift_monthly"] = 0.0
    info["box_center_drift_pct"] = 0.0
    info["box_tilt_ratio"] = 0.0
    info["box_top_slope_monthly"] = 0.0
    info["box_bottom_slope_monthly"] = 0.0

    # ── 基底质量：根据包含率和振幅判断 ──
    if inside_pct >= 85.0 and box_range_pct <= 12.0:
        stage1_type = "优秀"
        stage1_detail = f"包含率{inside_pct:.0f}%，振幅{box_range_pct:.1f}%，紧致横盘"
    elif inside_pct >= 75.0 and box_range_pct <= 16.0:
        stage1_type = "可接受"
        stage1_detail = f"包含率{inside_pct:.0f}%，振幅{box_range_pct:.1f}%"
    elif inside_pct < 65.0:
        stage1_type = "需警惕"
        stage1_detail = f"包含率{inside_pct:.0f}%（偏低），可能不是有效横盘"
    else:
        stage1_type = "一般"
        stage1_detail = f"包含率{inside_pct:.0f}%，振幅{box_range_pct:.1f}%"
    info["stage1_box_type"] = stage1_type
    info["stage1_box_detail"] = stage1_detail
    # ── 箱体完整性检查：用前2/3箱体拟合，检查后1/3是否脱离 ──
    # 防止回归线自适应地把破位"兜"进去
    box_mid = int(box_start + (box_end - box_start) * 0.67)
    if box_mid > box_start + 4:
        # 只用前2/3的摆点重拟合
        early_highs = [i for i in swing_high_idx if i <= box_mid]
        early_lows = [i for i in swing_low_idx if i <= box_mid]
        if len(early_highs) >= 2 and len(early_lows) >= 2:
            xh_e = np.array(early_highs, dtype=float)
            yh_e = highs[early_highs]
            xl_e = np.array(early_lows, dtype=float)
            yl_e = lows[early_lows]
            a_h2, b_h2 = np.polyfit(xh_e, yh_e, 1)
            a_l2, b_l2 = np.polyfit(xl_e, yl_e, 1)
            a_c2 = (a_h2 + a_l2) / 2.0
            b_h2 = np.mean(yh_e - a_c2 * xh_e)
            b_l2 = np.mean(yl_e - a_c2 * xl_e)

            # 检查后1/3是否脱离此箱体
            late_start = box_mid + 1
            breakout_count = 0
            check_count = 0
            for i in range(late_start, box_end + 1):
                upper_i = a_c2 * i + b_h2
                lower_i = a_c2 * i + b_l2
                if lower_i <= 0 or upper_i <= lower_i:
                    continue
                check_count += 1
                if highs[i] > upper_i * 1.05:
                    breakout_count += 1
                if lows[i] < lower_i * 0.95:
                    breakout_count += 1
            if check_count >= 3 and breakout_count >= check_count * 0.5:
                return 0.0, info  # 后1/3严重脱离早期箱体

    # ── 4. 触碰频率评分 (0-10) ──
    touch_high = 0
    touch_low = 0
    tolerance = 0.02  # 2% 容差

    for i in range(box_start, box_end + 1):
        upper_at_i = a_h * i + b_h
        lower_at_i = a_l * i + b_l
        if upper_at_i > 0 and abs(highs[i] - upper_at_i) / upper_at_i <= tolerance:
            touch_high += 1
        if lower_at_i > 0 and abs(lows[i] - lower_at_i) / lower_at_i <= tolerance:
            touch_low += 1

    total_touches = touch_high + touch_low
    info["touch_count"] = total_touches

    if touch_high >= 3 and touch_low >= 3:
        touch_score = 10.0
    elif touch_high >= 2 and touch_low >= 2:
        touch_score = 7.0
    elif touch_high >= 1 and touch_low >= 1:
        touch_score = 4.0
    else:
        touch_score = 1.0

    # ── 5. 越界控制评分 (0-10) ──
    penetration_count = 0
    total_bars = box_end - box_start + 1

    for i in range(box_start, box_end + 1):
        upper_at_i = a_h * i + b_h
        lower_at_i = a_l * i + b_l
        # 越界超过3%才计入
        if upper_at_i > 0 and highs[i] > upper_at_i * 1.03:
            penetration_count += 1
        if lower_at_i > 0 and lows[i] < lower_at_i * 0.97:
            penetration_count += 1

    penetration_pct = (1.0 - penetration_count / total_bars) * 100.0
    info["penetration_pct"] = penetration_pct

    # ── 越界品质因子 (0.0-1.0)：直接乘到总分上 ──
    # 含义：箱体越漏，所有子分（触碰/时长）都跟着缩水
    if penetration_pct >= 95.0:
        pen_quality = 1.0       # 极紧致箱体，满分
    elif penetration_pct >= 90.0:
        pen_quality = 0.85      # 良好
    elif penetration_pct >= 85.0:
        pen_quality = 0.65      # 可接受
    elif penetration_pct >= 80.0:
        pen_quality = 0.45      # 偏漏
    elif penetration_pct >= 75.0:
        pen_quality = 0.25      # 很漏
    elif penetration_pct >= 70.0:
        pen_quality = 0.10      # 严重漏
    else:
        pen_quality = 0.0       # 不是有效箱体

    # 越界子分（保留独立维度，但收紧阈值）
    if penetration_pct >= 95.0:
        penetration_score = 10.0
    elif penetration_pct >= 90.0:
        penetration_score = 7.0
    elif penetration_pct >= 85.0:
        penetration_score = 4.0
    elif penetration_pct >= 80.0:
        penetration_score = 1.0
    else:
        penetration_score = 0.0

    # ── 6. 持续时间加分 (0-5)：漏箱不给时长分 ──
    if penetration_pct >= 80.0:
        if box_weeks >= 24:
            duration_bonus = 5.0
        elif box_weeks >= 16:
            duration_bonus = 3.0
        elif box_weeks >= 8:
            duration_bonus = 1.0
        else:
            duration_bonus = 0.0
    else:
        duration_bonus = 0.0

    # 总分 = (触碰 + 越界 + 时长) × 品质因子 × 摆点质量 × 综合惩罚
    total = (touch_score + penetration_score + duration_bonus) * pen_quality * swing_quality * _drift_penalty
    total = min(total, 25.0)

    return total, info


# ════════════════════════════════════════════════════
#  维度4: 量能趋势 (🆕 箱体内量能递减)
# ════════════════════════════════════════════════════

def _score_volume_trend(df: pd.DataFrame, box_start_idx: int = 0) -> tuple[float, bool]:
    """箱体内量能是否递减 (0-15)

    三段式量能分析：前段（回踩期）→ 中段（缩量期）→ 后段（蓄力期）
    真正的缩量发生在中段。后段放量是突破前兆，不应惩罚。
    """
    if "volume" not in df.columns or len(df) < 8:
        return 0.0, False

    lookback = min(30, len(df))
    segment = df.iloc[-lookback:].reset_index(drop=True)
    n = len(segment)

    if box_start_idx > 0 and box_start_idx < n:
        start = box_start_idx
    else:
        start = max(0, n - 12)

    box_segment = segment.iloc[start:]
    box_len = len(box_segment)
    if box_len < 6:
        return 0.0, False

    third = max(1, box_len // 3)
    vol_early = box_segment["volume"].iloc[:third].mean()
    vol_mid = box_segment["volume"].iloc[third:2*third].mean()
    vol_late = box_segment["volume"].iloc[-third:].mean()

    if vol_early <= 0:
        return 0.0, False

    # 核心判断：中段 vs 前段 — 真正的缩量发生在这里
    mid_ratio = float(vol_mid / vol_early) if vol_early > 0 else 999
    late_ratio = float(vol_late / vol_early) if vol_early > 0 else 999
    contracted = mid_ratio < 0.85  # 中段明显萎缩（用于展示，不作硬门檻）

    # 中段缩量得分：放宽分级
    if mid_ratio <= 0.4:
        mid_score = 12.0       # 极度缩量
    elif mid_ratio <= 0.55:
        mid_score = 10.0
    elif mid_ratio <= 0.7:
        mid_score = 8.0
    elif mid_ratio <= 0.85:
        mid_score = 6.0        # 明显缩量
    elif mid_ratio <= 1.0:
        mid_score = 3.0        # 轻微缩量/持平
    elif mid_ratio <= 1.2:
        mid_score = 1.0        # 量能偏高但可接受
    else:
        mid_score = 0.0

    # 后段蓄力加分：后段放量（>前段50%）说明资金进场
    if late_ratio > 1.5 and mid_ratio < 0.85:
        late_bonus = 3.0
    elif late_ratio > 1.0 and mid_ratio < 1.0:
        late_bonus = 2.0
    else:
        late_bonus = 0.0

    total = mid_score + late_bonus
    return min(total, 15.0), contracted


# ════════════════════════════════════════════════════
#  维度5: 波动压缩 (保持不变)
# ════════════════════════════════════════════════════

def _score_atr_compression(
    df: pd.DataFrame,
    daily: pd.DataFrame | None,
) -> tuple[float, float | None]:
    if len(df) < 10:
        return 0.0, None
    if "high" in df.columns and "low" in df.columns and "close" in df.columns:
        prev_close = df["close"].shift(1)
        weekly_range = (df["high"] - df["low"]) / prev_close
        weekly_range = weekly_range.dropna()
    else:
        return 0.0, None
    if len(weekly_range) < 30:
        return 0.0, None

    current_atr = float(weekly_range.iloc[-4:].mean())
    all_ranges = weekly_range.values
    atr_rank = (all_ranges < current_atr).sum() / len(all_ranges) * 100.0

    if atr_rank <= 15.0:
        return 10.0, round(atr_rank, 1)
    elif atr_rank <= 25.0:
        return 8.0, round(atr_rank, 1)
    elif atr_rank <= 35.0:
        return 6.0, round(atr_rank, 1)
    elif atr_rank <= 50.0:
        return 3.0, round(atr_rank, 1)
    return 0.0, round(atr_rank, 1)


# ════════════════════════════════════════════════════
#  维度6: Stage I 基底质量奖惩（箱顶/箱底独立斜率）
#  优秀=+5, 可接受=+2, 一般=0, 需警惕=-5
# ════════════════════════════════════════════════════

def _score_stage1_quality(box_info: dict) -> float:
    """根据箱体独立斜率判定 Stage I 基底质量，返回奖惩分。

    奖惩分说明：
      +5  优秀   → 箱顶斜率±5%且箱底斜率±5%，几乎水平
      +2  可接受 → 箱顶斜率±10%且箱底斜率±10%，轻微倾斜
       0  一般   → 介于可接受和需警惕之间
      -5  需警惕 → 箱顶斜率>+15%，更像Stage II初期
       0  无数据 → 箱体无效或无分类
    """
    stage1_type = box_info.get("stage1_box_type")
    if stage1_type == "优秀":
        return 5.0
    elif stage1_type == "可接受":
        return 2.0
    elif stage1_type == "需警惕":
        return -5.0
    return 0.0
