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
        "cont_stage1_box_type": None,
        "cont_stage1_box_detail": None,
        "cont_score_stage1": 0.0,
    }

    if weekly.empty or "close" not in weekly.columns or len(weekly) < 30:
        return default

    working = weekly.copy()
    working["ma_30w"] = working["close"].rolling(min_periods=1, window=30).mean()

    ma_vals = working["ma_30w"].dropna()
    if len(ma_vals) < 10:
        return default

    slope_10w = (float(ma_vals.iloc[-1]) / float(ma_vals.iloc[-10]) - 1.0) * 100.0

    # 前趋斜率：>0% 即可（MA30w不下跌），不需要强制 >5%
    prior_trend_ok = slope_10w > 0.0

    if not prior_trend_ok:
        return {
            **default,
            "cont_quality_reason": f"MA30w斜率{slope_10w:.1f}%（需>0%）",
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
        "cont_volume_trend_ok": vol_ok,
        "cont_atr_rank_pct": round(atr_rank, 1) if atr_rank is not None else None,
        "cont_is_applicable": True,
        "cont_prior_trend_ok": True,
        "cont_box_top_slope": box_info.get("box_top_slope_monthly"),
        "cont_box_bottom_slope": box_info.get("box_bottom_slope_monthly"),
        "cont_stage1_box_type": box_info.get("stage1_box_type"),
        "cont_stage1_box_detail": box_info.get("stage1_box_detail"),
    }


# ════════════════════════════════════════════════════
#  维度1: 前期趋势强度（0-5%为理想缓升区间）
# ════════════════════════════════════════════════════

def _score_prior_trend(slope_10w: float) -> float:
    """斜率越高越好，但平坦箱体(0-3%)也是优质形态"""
    if slope_10w <= 0:
        return 0.0
    elif 0.0 < slope_10w <= 1.0:
        return 22.0
    elif 1.0 < slope_10w <= 3.0:
        return 23.0
    elif 3.0 < slope_10w <= 5.0:
        return 25.0
    elif 5.0 < slope_10w <= 8.0:
        return 25.0
    elif 8.0 < slope_10w <= 12.0:
        return 22.0
    elif 12.0 < slope_10w <= 15.0:
        return 18.0
    elif 15.0 < slope_10w <= 20.0:
        return 10.0
    elif 20.0 < slope_10w <= 30.0:
        return 5.0
    return 0.0


# ════════════════════════════════════════════════════
#  维度2: 回踩深度 (保持不变)
# ════════════════════════════════════════════════════

def _score_pullback_depth(df: pd.DataFrame) -> tuple[float, float | None]:
    if "close" not in df.columns or "ma_30w" not in df.columns:
        return 0.0, None
    close_val = float(df["close"].iloc[-1])
    ma_val = float(df["ma_30w"].iloc[-1])
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
    1. 扫描最近30周，找局部摆动高点和低点
    2. 对高点和低点分别做线性回归，形成上下轨
    3. 评估：触碰频率(10) + 越界控制(10) + 持续时间(5) = 25

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

    # ── 1. 找局部极值点 ──
    lookback = min(30, len(df))
    segment = df.iloc[-lookback:].reset_index(drop=True)

    highs = segment["high"].values
    lows = segment["low"].values
    n = len(segment)

    # 局部高点：比前后2根都高
    swing_high_idx = []
    swing_low_idx = []
    order = 2
    for i in range(order, n - order):
        if highs[i] == highs[i-order:i+order+1].max():
            # 确保是真正的局部最高
            if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
                swing_high_idx.append(i)
        if lows[i] == lows[i-order:i+order+1].min():
            if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
                swing_low_idx.append(i)

    if len(swing_high_idx) < 2 or len(swing_low_idx) < 2:
        return 0.0, info

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

    if len(swing_high_idx) < 2 or len(swing_low_idx) < 2:
        return 0.0, info

    # ── 2. 拟合上下轨（线性回归，共用斜率确保平行）──
    x_highs = np.array(swing_high_idx, dtype=float)
    y_highs = highs[swing_high_idx]
    x_lows = np.array(swing_low_idx, dtype=float)
    y_lows = lows[swing_low_idx]

    # 分别拟合上下轨（保存独立斜率，用于 Stage I 基底质量判断）
    a_h_orig, b_h_orig = np.polyfit(x_highs, y_highs, 1)
    a_l_orig, b_l_orig = np.polyfit(x_lows, y_lows, 1)
    a_h, b_h = a_h_orig, b_h_orig
    a_l, b_l = a_l_orig, b_l_orig

    # 独立箱顶/箱底斜率（%/月），用于 Stage I 基底质量
    top_slope_monthly = (a_h_orig * 4) / b_h_orig * 100 if b_h_orig != 0 else 0
    bot_slope_monthly = (a_l_orig * 4) / b_l_orig * 100 if b_l_orig != 0 else 0

    # 共用斜率 = 平均值，确保上下轨平行
    a_common = (a_h + a_l) / 2.0
    b_h = np.mean(y_highs - a_common * x_highs)
    b_l = np.mean(y_lows - a_common * x_lows)
    a_h = a_l = a_common

    # 计算平行斜率 (%/月, 按4周=1月)
    slope_h_monthly = (a_h * 4) / b_h * 100 if b_h != 0 else 0
    slope_l_monthly = (a_l * 4) / b_l * 100 if b_l != 0 else 0
    slope_max_abs = max(abs(slope_h_monthly), abs(slope_l_monthly))

    # ── 3. 箱体有效性检查 ──
    box_start = min(swing_high_idx[0], swing_low_idx[0])
    box_end = n - 1  # 到最新
    box_len = box_end - box_start
    box_weeks = int(box_len)

    if box_weeks < 4:
        return 0.0, info

    # 上轨必须在下轨之上（在箱体中间点检查）
    mid_x = (box_start + box_end) / 2.0
    upper_mid = a_h * mid_x + b_h
    lower_mid = a_l * mid_x + b_l
    if lower_mid <= 0 or upper_mid <= lower_mid:
        return 0.0, info

    # 通道宽度 = 上轨/下轨 - 1（任何时间点，独立于斜率漂移）
    box_range_pct = (upper_mid / lower_mid - 1.0) * 100.0

    # 通道宽度 ≤20%
    if box_range_pct > 20.0:
        return 0.0, info

    # 斜率约束：|斜率| ≤ 5%/月，防止把趋势当成箱体
    if slope_max_abs > 5.0:
        return 0.0, info

    # ── 越界预检：只有足够多 bar 在通道内才算有效箱体 ──
    inside_count = 0
    total_bars = box_end - box_start + 1
    margin = 0.04  # 4% 容差（平行箱体更紧致，适当放宽）
    for i in range(box_start, box_end + 1):
        upper_i = a_h * i + b_h
        lower_i = a_l * i + b_l
        if lower_i > 0 and lows[i] >= lower_i * (1 - margin) and highs[i] <= upper_i * (1 + margin):
            inside_count += 1
    inside_pct = inside_count / total_bars * 100.0
    if inside_pct < 75.0:
        return 0.0, info  # 通道太松，不够格

    info["box_valid"] = True
    info["box_range_pct"] = box_range_pct
    info["box_duration_weeks"] = box_weeks
    info["box_start_idx"] = box_start
    info["box_end_idx"] = box_end
    info["box_a_h"] = float(a_h)
    info["box_b_h"] = float(b_h)
    info["box_a_l"] = float(a_l)
    info["box_b_l"] = float(b_l)
    # ── Stage I 基底质量：根据独立箱顶/箱底斜率判断 ──
    info["box_top_slope_monthly"] = round(top_slope_monthly, 2)
    info["box_bottom_slope_monthly"] = round(bot_slope_monthly, 2)
    top_abs = abs(top_slope_monthly)
    bot_abs = abs(bot_slope_monthly)
    if top_abs <= 5.0 and bot_abs <= 5.0:
        stage1_type = "优秀"
        stage1_detail = (
            f"箱顶斜率{top_slope_monthly:+.1f}%/月，"
            f"箱底斜率{bot_slope_monthly:+.1f}%/月，几乎水平"
        )
    elif top_abs <= 10.0 and bot_abs <= 10.0:
        stage1_type = "可接受"
        stage1_detail = (
            f"箱顶斜率{top_slope_monthly:+.1f}%/月，"
            f"箱底斜率{bot_slope_monthly:+.1f}%/月，轻微倾斜"
        )
    elif top_slope_monthly > 15.0:
        stage1_type = "需警惕"
        stage1_detail = (
            f"箱顶斜率{top_slope_monthly:+.1f}%/月（>15%），"
            f"持续创新高，更像Stage II初期而非Stage I"
        )
    else:
        stage1_type = "一般"
        stage1_detail = (
            f"箱顶斜率{top_slope_monthly:+.1f}%/月，"
            f"箱底斜率{bot_slope_monthly:+.1f}%/月"
        )
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

    # 总分 = (触碰 + 越界 + 时长) × 品质因子
    total = (touch_score + penetration_score + duration_bonus) * pen_quality
    total = min(total, 25.0)

    return total, info


# ════════════════════════════════════════════════════
#  维度4: 量能趋势 (🆕 箱体内量能递减)
# ════════════════════════════════════════════════════

def _score_volume_trend(df: pd.DataFrame, box_start_idx: int = 0) -> tuple[float, bool]:
    """箱体内量能是否递减 (0-15)

    把箱体分前1/3和后1/3，对比量能。
    后段量能 < 前段 * 0.7 → 明显萎缩
    """
    if "volume" not in df.columns or len(df) < 8:
        return 0.0, False

    lookback = min(30, len(df))
    segment = df.iloc[-lookback:].reset_index(drop=True)
    n = len(segment)

    if box_start_idx > 0 and box_start_idx < n:
        start = box_start_idx
    else:
        start = max(0, n - 12)  # fallback: 最近12周

    box_segment = segment.iloc[start:]
    box_len = len(box_segment)
    if box_len < 4:
        return 0.0, False

    third = max(1, box_len // 3)
    vol_early = box_segment["volume"].iloc[:third].mean()
    vol_late = box_segment["volume"].iloc[-third:].mean()

    if vol_early <= 0:
        return 0.0, False

    ratio = float(vol_late / vol_early)
    contracted = ratio < 0.85

    if ratio <= 0.5:
        return 15.0, contracted
    elif ratio <= 0.65:
        return 12.0, contracted
    elif ratio <= 0.85:
        return 8.0, contracted
    elif ratio <= 1.0:
        return 3.0, contracted
    return 0.0, contracted


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
