"""Stage I 基底质量评分 (Base Quality Score)

基于专家建议的5维独立评估体系：
1. 长期均线平坦度 — MA30w 10周斜率
2. 箱体振幅质量 — 基底振幅收敛度
3. 基底持续时间 — 连续满足基底条件的天数
4. 成交量萎缩 — 近期量能 vs 远期量能
5. 波动率压缩 — ATR 相对历史水平 (加分项)
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from winstan.config import AppConfig


# ── 维度权重 ──
WEIGHT_MA = 25       # 均线平坦度
WEIGHT_RANGE = 25    # 箱体振幅
WEIGHT_LENGTH = 25   # 持续时间
WEIGHT_VOLUME = 15   # 量能萎缩
WEIGHT_ATR = 10      # 波动率压缩（加分项）

# ── 等级阈值 ──
GRADE_THRESHOLDS = [
    (85, "S", "极优质基底"),
    (70, "A", "优质基底"),
    (50, "B", "合格基底"),
    (0,  "C", "一般基底"),
]


def compute_base_quality(
    recent: pd.DataFrame,
    config: AppConfig,
    base_info: dict[str, object] | None = None,
    daily: pd.DataFrame | None = None,
) -> dict[str, object]:
    """计算 Stage I 基底质量评分。

    Args:
        recent: 周线 DataFrame（至少60行，按 trade_date 升序）
        config: 系统配置
        base_info: 可选的已检测基底信息 (from _detect_bases)
        daily: 可选的日线 DataFrame（用于更精细的成交量/ATR计算）

    Returns:
        base_quality_score: float (0-100)
        base_quality_grade: str (S/A/B/C)
        base_quality_reason: str
        base_score_ma, base_score_range, base_score_length,
        base_score_volume, base_score_atr: 各维度得分
        base_flatness_slope_10w: float | None — MA30w 10周斜率(%)
        base_range_pct_detected: float | None — 检测到的基底振幅(%)
        base_duration_weeks: int — 连续基底周数
        base_volume_contraction_ok: bool — 量能萎缩是否达标
        base_atr_rank_pct: float | None — ATR 在历史中的百分比排名
    """
    default = {
        "base_quality_score": 0.0,
        "base_quality_grade": "C",
        "base_quality_reason": "无数据",
        "base_score_ma": 0.0,
        "base_score_range": 0.0,
        "base_score_length": 0.0,
        "base_score_volume": 0.0,
        "base_score_atr": 0.0,
        "base_flatness_slope_10w": None,
        "base_range_pct_detected": None,
        "base_duration_weeks": 0,
        "base_volume_contraction_ok": False,
        "base_atr_rank_pct": None,
    }

    if recent.empty or "close" not in recent.columns:
        return default

    # ── 计算辅助指标 ──
    working = recent.copy()
    working["ma_30w"] = working["close"].rolling(min_periods=1, window=30).mean()

    # ── 维度1: 均线平坦度 (0-25) ──
    ma_score, slope_10w = _score_ma_flatness(working)

    # ── 维度2: 箱体振幅 (0-25) ──
    range_score, base_range_pct = _score_box_range(working)

    # ── 维度3: 持续时间 (0-25) ──
    length_score, duration_weeks = _score_base_duration(working, config)

    # ── 维度4: 量能萎缩 (0-15) ──
    volume_score, vol_contraction_ok = _score_volume_contraction(working)

    # ── 维度5: 波动率压缩 (0-10, 加分项) ──
    atr_score, atr_rank = _score_atr_compression(working, daily)

    # ── 汇总 ──
    total = ma_score + range_score + length_score + volume_score + atr_score
    total = min(total, 100.0)

    grade, grade_label = "C", "一般基底"
    for threshold, g, label in GRADE_THRESHOLDS:
        if total >= threshold:
            grade, grade_label = g, label
            break

    # 构建原因文本
    reason_parts: list[str] = []
    if grade != "C":
        reason_parts.append(grade_label)
    if slope_10w is not None:
        reason_parts.append(f"均线偏{slope_10w:+.1f}%")
    if base_range_pct is not None:
        reason_parts.append(f"振幅{base_range_pct:.0f}%")
    if duration_weeks > 0:
        reason_parts.append(f"持续{duration_weeks}周")
    if vol_contraction_ok:
        reason_parts.append("量缩")
    if atr_score >= 5:
        reason_parts.append("波压")

    return {
        "base_quality_score": round(total, 1),
        "base_quality_grade": grade,
        "base_quality_reason": " / ".join(reason_parts) if reason_parts else "未达标",
        "base_score_ma": round(ma_score, 1),
        "base_score_range": round(range_score, 1),
        "base_score_length": round(length_score, 1),
        "base_score_volume": round(volume_score, 1),
        "base_score_atr": round(atr_score, 1),
        "base_flatness_slope_10w": round(slope_10w, 2) if slope_10w is not None else None,
        "base_range_pct_detected": round(base_range_pct, 1) if base_range_pct is not None else None,
        "base_duration_weeks": duration_weeks,
        "base_volume_contraction_ok": vol_contraction_ok,
        "base_atr_rank_pct": round(atr_rank, 1) if atr_rank is not None else None,
    }


# ══════════════════════════════════════════════════════════════
#  维度实现
# ══════════════════════════════════════════════════════════════

def _score_ma_flatness(df: pd.DataFrame) -> tuple[float, float | None]:
    """维度1: MA30w 10周斜率 (0-25分)
    
    理想: -3% < slope_10w < +5%（均线基本走平）
    最佳: slope 接近 0%（完全走平）
    slope > 10%: 上升太快，不是基底（0分）
    """
    if "ma_30w" not in df.columns or len(df) < 10:
        return 0.0, None

    ma30_values = df["ma_30w"].dropna()
    if len(ma30_values) < 10:
        return 0.0, None

    current_ma = float(ma30_values.iloc[-1])
    past_ma = float(ma30_values.iloc[-10])

    if past_ma <= 0:
        return 0.0, None

    slope_pct = (current_ma / past_ma - 1.0) * 100.0

    # 评分曲线
    if -3.0 <= slope_pct <= 5.0:
        # 在这个范围内，越接近0越高
        deviation = abs(slope_pct)
        if deviation <= 1.0:
            score = 25.0  # 几乎完全走平
        elif deviation <= 2.0:
            score = 22.0
        elif deviation <= 3.0:
            score = 18.0
        elif deviation <= 4.0:
            score = 12.0
        else:
            score = 8.0
    elif -5.0 <= slope_pct < -3.0:
        # 轻微下降，给部分分
        score = 10.0 - abs(slope_pct + 3.0) * 2.0
        score = max(score, 3.0)
    elif 5.0 < slope_pct <= 10.0:
        # 轻微上升偏多，给部分分
        score = 10.0 - (slope_pct - 5.0) * 0.8
        score = max(score, 1.0)
    else:
        score = 0.0

    return score, round(slope_pct, 2)


def _score_box_range(df: pd.DataFrame) -> tuple[float, float | None]:
    """维度2: 箱体振幅 (0-25分)
    
    用60日滚动窗口计算基底振幅。
    理想: 10%-40%（有明显箱体，但不是大区间震荡）
    最佳: 15%-30%
    >50%: 振幅过大不稳定
    <8%: 可能数据问题
    """
    if "high" not in df.columns or "low" not in df.columns or len(df) < 12:
        return 0.0, None

    window = min(60, len(df))
    high_max = float(df["high"].iloc[-window:].max())
    low_min = float(df["low"].iloc[-window:].min())

    if low_min <= 0:
        return 0.0, None

    range_pct = (high_max / low_min - 1.0) * 100.0

    if 15.0 <= range_pct <= 30.0:
        # 理想振幅区间
        center = 22.5
        deviation = abs(range_pct - center)
        score = 25.0 - deviation * 0.8
        score = max(score, 20.0)
    elif 10.0 <= range_pct < 15.0:
        score = 15.0 + (range_pct - 10.0) * 2.0
    elif 30.0 < range_pct <= 40.0:
        score = 25.0 - (range_pct - 30.0) * 0.6
        score = max(score, 12.0)
    elif 8.0 <= range_pct < 10.0:
        score = 8.0
    elif 40.0 < range_pct <= 50.0:
        score = 5.0
    else:
        score = 0.0

    return score, round(range_pct, 1)


def _score_base_duration(
    df: pd.DataFrame,
    config: AppConfig,
) -> tuple[float, int]:
    """维度3: 基底持续时间 (0-25分)
    
    统计 MA30w 走平（斜率在 ±3%）的连续周数。
    ≥60周（15个月）= 满分25
    ≥40周 = 20分
    ≥20周 = 12分
    ≥8周  = 5分
    """
    if "ma_30w" not in df.columns or len(df) < 5:
        return 0.0, 0

    # 从最近开始向前数，找到 MA30w 斜率在 ±3% 的连续段
    # 用4周滚动斜率来平滑
    ma_flat: list[bool] = []
    for i in range(4, len(df)):
        past = float(df["ma_30w"].iloc[i - 4])
        curr = float(df["ma_30w"].iloc[i])
        if past > 0 and curr > 0:
            slope_4w = (curr / past - 1.0) * 100.0
            ma_flat.append(-3.0 <= slope_4w <= 3.0)
        else:
            ma_flat.append(False)

    # 从末尾往前数连续 True
    duration = 0
    for v in reversed(ma_flat):
        if v:
            duration += 1
        else:
            break

    # 评分
    if duration >= 60:
        score = 25.0
    elif duration >= 40:
        score = 20.0 + (duration - 40) * 0.25
        score = min(score, 24.5)
    elif duration >= 20:
        score = 12.0 + (duration - 20) * 0.4
    elif duration >= 8:
        score = 5.0 + (duration - 8) * 0.58
    elif duration >= 4:
        score = 2.0
    else:
        score = 0.0

    return score, duration


def _score_volume_contraction(df: pd.DataFrame) -> tuple[float, bool]:
    """维度4: 成交量萎缩 (0-15分)
    
    vol_20 / vol_60 < 0.8 → 量能萎缩明显
    越低越好，表示浮筹减少。
    """
    if "volume" not in df.columns or len(df) < 60:
        if len(df) >= 20:
            # 数据不够60周，用20周
            vol_recent = df["volume"].iloc[-20:].mean()
            vol_all = df["volume"].iloc[-40:].mean() if len(df) >= 40 else df["volume"].mean()
            if vol_all > 0:
                ratio = vol_recent / vol_all
            else:
                return 0.0, False
        else:
            return 0.0, False
    else:
        vol_recent = df["volume"].iloc[-20:].mean()
        vol_far = df["volume"].iloc[-60:-20].mean()

        if vol_far <= 0:
            return 0.0, False

        ratio = vol_recent / vol_far

    contracted = ratio < 0.8

    if ratio <= 0.5:
        score = 15.0  # 极度萎缩
    elif ratio <= 0.65:
        score = 12.0
    elif ratio <= 0.8:
        score = 9.0
    elif ratio <= 0.95:
        score = 4.0
    elif ratio <= 1.1:
        score = 1.0
    else:
        score = 0.0

    return score, contracted


def _score_atr_compression(
    df: pd.DataFrame,
    daily: pd.DataFrame | None,
) -> tuple[float, float | None]:
    """维度5: 波动率压缩 — 加分项 (0-10分)
    
    用周线估算 ATR。如果提供日线则更精确。
    比较当前 ATR 在历史中的排名。
    ATR rank < 20%: 极度压缩 → 10分
    ATR rank < 30%: 明显压缩 → 8分
    ATR rank < 50%: 一般   → 4分
    """
    # 用周线估算波动率 (high-low 幅度)
    if len(df) < 10:
        return 0.0, None

    # 计算每周的振幅
    if "high" in df.columns and "low" in df.columns:
        weekly_range = (df["high"] - df["low"]) / df["close"].shift(1)
        weekly_range = weekly_range.dropna()
    else:
        return 0.0, None

    if len(weekly_range) < 50:
        return 0.0, None

    current_range = float(weekly_range.iloc[-1])
    # 用近期均值作为当前波动率代表
    current_atr = float(weekly_range.iloc[-5:].mean())

    all_ranges = weekly_range.values
    atr_rank = (all_ranges < current_atr).sum() / len(all_ranges) * 100.0

    if atr_rank <= 15.0:
        score = 10.0
    elif atr_rank <= 25.0:
        score = 8.0
    elif atr_rank <= 35.0:
        score = 6.0
    elif atr_rank <= 50.0:
        score = 3.0
    else:
        score = 0.0

    return score, round(atr_rank, 1)
