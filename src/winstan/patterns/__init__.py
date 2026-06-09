"""
Pattern-based recommendation engine for Weinstein System.

Detects chart patterns (W-bottom, Cup & Handle, etc.) on top of 
existing Weinstein Stage II / Stage I screening results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# ── Constants ──────────────────────────────────────────────────────────────

LOOKBACK_DAYS = 200          # How far back to look for patterns
TROUGH_MIN_DISTANCE = 10     # Minimum trading days between two troughs
TROUGH_MAX_DISTANCE = 80     # Maximum trading days between two troughs
VOLUME_SURGE_RATIO = 1.2     # Minimum volume ratio for breakout confirmation
NECKLINE_BREAKOUT_CHECK = 5  # Check breakout within this many recent days
MIN_WBOTTOM_SCORE = 60       # Minimum composite score to appear in recommendations

# ── Data Structures ────────────────────────────────────────────────────────

@dataclass
class WBottomPattern:
    """Detected W-bottom (double-bottom) pattern."""
    detected: bool = False
    left_trough_idx: int = -1       # Index in daily bars
    left_trough_price: float = 0.0
    neckline_idx: int = -1          # Peak between troughs
    neckline_price: float = 0.0
    right_trough_idx: int = -1
    right_trough_price: float = 0.0
    breakout_idx: int = -1          # Bar where price broke above neckline
    breakout_price: float = 0.0
    volume_surge: bool = False
    volume_ratio: float = 0.0
    pattern_score: float = 0.0      # How textbook-perfect the pattern is (0-100)
    description: str = ""


@dataclass
class Recommendation:
    """Final recommendation for a single stock."""
    symbol: str
    name: str
    stage_label: str
    stage2_candidate: bool
    close: float
    final_score: float          # Weinstein composite score
    
    # Pattern info
    w_bottom: WBottomPattern = field(default_factory=WBottomPattern)
    
    # Recommendation
    rec_level: str = ""          # S / A / B / C
    rec_reason: str = ""
    rec_score: float = 0.0       # Combined recommendation score
    
    # Risk control
    stop_loss_ref: float | None = None
    target_entry_price: float | None = None
    suggestion: str = ""


# ── W-Bottom Detection ─────────────────────────────────────────────────────

def detect_w_bottom(daily: pd.DataFrame) -> WBottomPattern:
    """
    Detect a W-bottom (double-bottom) pattern in daily price data.
    
    Criteria:
    1. Two distinct troughs separated by a peak (neckline)
    2. Right trough >= left trough (higher low)
    3. Price has broken above neckline recently
    4. Volume confirmation on/after breakout
    """
    result = WBottomPattern()
    if daily.empty or len(daily) < TROUGH_MAX_DISTANCE + 20:
        return result

    frame = daily.sort_values("trade_date").reset_index(drop=True)
    closes = frame["close"].values.astype(float)
    highs = frame["high"].values.astype(float)
    lows = frame["low"].values.astype(float)
    volumes = frame["volume"].values.astype(float)
    n = len(closes)

    # ── Step 1: Find significant troughs (local minima) ──
    troughs = []
    window = 5  # Look for local minima within ±5 bars
    for i in range(window, n - window):
        left = lows[i - window:i]
        right = lows[i + 1:i + window + 1]
        if len(left) == 0 or len(right) == 0:
            continue
        if lows[i] <= left.min() and lows[i] <= right.min():
            # Only consider significant troughs (not too shallow)
            # Check that price dropped enough from recent highs
            nearby_high = highs[max(0, i - 15):min(n, i + 15)].max()
            drop_pct = (nearby_high - lows[i]) / nearby_high * 100
            if drop_pct >= 8:
                troughs.append((i, lows[i], drop_pct))

    if len(troughs) < 2:
        return result

    # ── Step 2: Find best pair of troughs ──
    best_pair = None
    best_score = -1

    for a in range(len(troughs)):
        for b in range(a + 1, len(troughs)):
            idx_a, price_a, drop_a = troughs[a]
            idx_b, price_b, drop_b = troughs[b]
            gap = idx_b - idx_a

            if gap < TROUGH_MIN_DISTANCE or gap > TROUGH_MAX_DISTANCE:
                continue

            # Right trough must be >= left trough (higher low = bullish)
            if price_b < price_a * 0.95:
                continue  # Right trough too much lower, not a valid W

            # Find neckline = highest high between the two troughs
            neck_high = highs[idx_a:idx_b + 1].max()
            neck_idx = idx_a + highs[idx_a:idx_b + 1].argmax()

            # Check how far current price is from neckline
            current_close = closes[-1]
            breakout_pct = (current_close / neck_high - 1.0) * 100

            # Recent breakout check
            recent_bars = frame.tail(NECKLINE_BREAKOUT_CHECK)
            breakout_score = 0
            for _, rb in recent_bars.iterrows():
                if rb["close"] >= neck_high:
                    breakout_score += 1

            # Volume check on/after breakout
            breakout_region = frame.tail(max(NECKLINE_BREAKOUT_CHECK + 5, 15))
            avg_volume = volumes[:n - 15].mean() if n > 15 else volumes.mean()
            recent_avg_volume = breakout_region["volume"].mean()
            volume_ratio_val = recent_avg_volume / avg_volume if avg_volume > 0 else 1.0

            # ── Score this pair ──
            score = 0
            
            # Higher low bonus
            higher_low_pct = (price_b - price_a) / price_a * 100
            if higher_low_pct > 0:
                score += min(25, higher_low_pct * 2)  # Up to 25 pts
            
            # Breakout strength
            if breakout_pct > 0:
                score += min(30, breakout_pct * 3)
            score += min(20, breakout_score * 5)  # 5 pts per bar above neckline
            
            # Volume confirmation
            if volume_ratio_val >= VOLUME_SURGE_RATIO:
                score += min(15, (volume_ratio_val - 1.0) * 20)
            elif volume_ratio_val >= 1.0:
                score += 5
            
            # Drop depth: significant drop = more meaningful pattern
            avg_drop = (drop_a + drop_b) / 2
            if avg_drop >= 15:
                score += 10
            elif avg_drop >= 10:
                score += 5

            if score > best_score:
                best_score = score
                best_pair = (idx_a, price_a, idx_b, price_b, neck_idx, neck_high,
                             breakout_pct, volume_ratio_val, score)

    if best_pair is None or best_score < 20:
        return result

    idx_a, price_a, idx_b, price_b, neck_idx, neck_high, breakout_pct, vol_ratio, score = best_pair

    # Volume surge boolean
    volume_ok = vol_ratio >= VOLUME_SURGE_RATIO

    # Generate description
    desc_parts = []
    higher_low_pct = (price_b - price_a) / price_a * 100
    desc_parts.append(f"W底形态")
    if higher_low_pct > 0:
        desc_parts.append(f"右底高{higher_low_pct:.1f}%")
    if breakout_pct > 0:
        desc_parts.append(f"突破颈线{breakout_pct:.1f}%")
    else:
        desc_parts.append(f"距颈线{abs(breakout_pct):.1f}%")
    if volume_ok:
        desc_parts.append(f"放量{vol_ratio:.1f}倍")

    return WBottomPattern(
        detected=True,
        left_trough_idx=idx_a,
        left_trough_price=price_a,
        neckline_idx=neck_idx,
        neckline_price=neck_high,
        right_trough_idx=idx_b,
        right_trough_price=price_b,
        breakout_idx=len(closes) - 1 if breakout_pct > 0 else -1,
        breakout_price=closes[-1],
        volume_surge=volume_ok,
        volume_ratio=vol_ratio,
        pattern_score=min(100, score),
        description=" / ".join(desc_parts),
    )


# ── Recommendation Engine ──────────────────────────────────────────────────

def compute_recommendations(
    screening_df: pd.DataFrame,
    parquet_root: Path,
) -> list[dict[str, object]]:
    """
    Compute recommendations from screening results + pattern detection.
    
    Args:
        screening_df: Full screening_results DataFrame (from get_results())
        parquet_root: Path to parquet data directory
    
    Returns:
        List of serialized recommendation dicts, sorted by priority.
    """
    import pandas as pd

    from winstan.storage.parquet_store import ParquetStore
    from winstan.outputs.explanations import get_trend_stage_label, get_watch_rank_label
    from winstan.rules.stage_analysis import apply_stage2_scoring

    store = ParquetStore(parquet_root)

    # Focus on: Stage2 candidates + top Stage1 stocks (W-bottom candidates)
    candidates = screening_df[screening_df.get("stage2_candidate", False)].copy()
    stage1_watch = screening_df[
        (screening_df["stage_label"] == "I")
        & (screening_df.get("base_flatness_ok", False))
    ].head(50).copy()
    target = pd.concat([candidates, stage1_watch], ignore_index=True)
    target = target.drop_duplicates(subset=["symbol"])

    recs: list[dict[str, object]] = []

    for _, row in target.iterrows():
        symbol = str(row.get("symbol", ""))
        if not symbol:
            continue

        daily = store.read_symbol_frame("daily_bars", symbol)
        if daily.empty:
            continue

        # Detect patterns
        wb = detect_w_bottom(daily)

        weinstein_score = float(row.get("final_score", 0) or 0)
        close = float(row.get("close", 0) or 0)

        # ── Determine recommendation level ──
        stage2 = bool(row.get("stage2_candidate", False))
        breakout_ok = bool(row.get("breakout_ok", False))
        breakout_status = str(row.get("breakout_status", ""))
        volume_ratio = float(row.get("volume_ratio", 0) or 0)
        volume_ok = bool(row.get("volume_ok", False))

        # Priority logic:
        is_just_broke = breakout_status == "just_broke_out"
        is_near_breakout = breakout_status == "near_breakout"
        has_wb = wb.detected

        rec_level = "C"
        rec_reason = ""
        rec_score = 0.0

        # 🔴 S级: Stage II + W底 + 放量突破 + 高综合分（顶级）
        if stage2 and has_wb and is_just_broke and wb.volume_surge and weinstein_score >= MIN_WBOTTOM_SCORE:
            rec_level = "S"
            rec_reason = wb.description
            rec_score = max(weinstein_score, wb.pattern_score)
        # 🟠 S级(无放量): Stage II + W底 + 突破
        elif stage2 and has_wb and is_just_broke:
            rec_level = "S"
            rec_reason = wb.description
            rec_score = max(weinstein_score, wb.pattern_score * 0.8)
        # ⭐ A级: Stage I + W底 + 放量突破 + 高综合分
        elif has_wb and is_just_broke and wb.volume_surge and weinstein_score >= MIN_WBOTTOM_SCORE:
            rec_level = "A"
            rec_reason = f"W底突破 | {wb.description}"
            rec_score = max(weinstein_score * 0.9, wb.pattern_score)
        # ⭐ A级: Stage II + 刚突破 + 放量（即使没W底或已不在新手期）
        elif stage2 and is_just_broke and volume_ok:
            rec_level = "A"
            rec_reason = "刚突破放量"
            rec_score = weinstein_score
        # 🔶 B+级: W底形成 + 放量 + 距突破近（提前埋伏目标）
        elif has_wb and wb.volume_surge and (not is_just_broke):
            # 计算距突破距离
            _neck = wb.neckline_price
            _curr = float(row.get("close", 0) or 0)
            _dist_pct = (_curr / _neck - 1) * 100 if _neck > 0 else 999
            if _dist_pct < 10:
                rec_level = "B+"
                rec_reason = f"提前埋伏 | {wb.description}" if _dist_pct > 0 else f"W底待突破 | {wb.description}"
                rec_score = weinstein_score * 0.8 + wb.pattern_score * 0.2
        # 👀 B级: Stage2 临近突破 / Stage1 W底（未突破）
        elif (stage2 and is_near_breakout) or (has_wb and not is_just_broke):
            rec_level = "B"
            rec_reason = wb.description if has_wb else "临近突破关注"
            rec_score = weinstein_score * 0.9 + (wb.pattern_score * 0.1 if has_wb else 0)
        # 🟢 C级: 一般观察
        else:
            rec_level = "C"
            rec_reason = "Stage2候选" if stage2 else "Stage1观察"
            rec_score = weinstein_score

        # Build stop loss and target entry
        if stage2:
            stop_loss = float(row.get("breakout_level", 0)) if pd.notna(row.get("breakout_level")) else None
            target_entry = float(row.get("breakout_level", 0)) if pd.notna(row.get("breakout_level")) else None
        elif wb.detected:
            stop_loss = wb.neckline_price * 0.93  # 7% below neckline
            target_entry = wb.neckline_price
        else:
            stop_loss = None
            target_entry = None

        recs.append({
            "symbol": symbol,
            "name": str(row.get("name", "")),
            "stage": str(row.get("stage_label", "")),
            "close": f"{close:.2f}" if close else "--",
            "weinstein_score": f"{weinstein_score:.2f}" if weinstein_score else "--",
            "rec_level": rec_level,
            "rec_score": f"{rec_score:.2f}" if rec_score else "--",
            "rec_reason": rec_reason,
            "has_w_bottom": has_wb,
            "w_bottom": {
                "left_price": f"{wb.left_trough_price:.2f}",
                "right_price": f"{wb.right_trough_price:.2f}",
                "neckline": f"{wb.neckline_price:.2f}",
                "volume_ratio": f"{wb.volume_ratio:.2f}",
                "pattern_score": f"{wb.pattern_score:.1f}",
                "description": wb.description,
            } if has_wb else None,
            "target_entry": f"{target_entry:.2f}" if target_entry else "--",
            "stop_loss": f"{stop_loss:.2f}" if stop_loss else "--",
            "analysis": f"{rec_reason} | 温斯坦综合分{weinstein_score:.1f}",
        })

    # Sort: S > A > B+ > B > C, then by rec_score descending
    level_order = {"S": 0, "A": 1, "B+": 2, "B": 3, "C": 4}
    def _sort_key(r):
        lev = level_order.get(r["rec_level"], 99)
        try:
            sc = float(r.get("rec_score", 0) or 0)
        except (ValueError, TypeError):
            sc = 0.0
        return (lev, -sc)
    recs.sort(key=_sort_key)

    # Assign rank
    for i, rec in enumerate(recs):
        rec["rank"] = i + 1

    return recs
