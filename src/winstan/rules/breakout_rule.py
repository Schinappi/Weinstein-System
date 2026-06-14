from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig


def evaluate_breakout(
    latest: pd.Series,
    config: AppConfig,
    base_breakout_price: float | None = None,
) -> dict[str, object]:
    """Evaluate breakout status — uses base_breakout_price when available.

    When base_breakout_price (fixed base top) is provided, it overrides the
    rolling breakout_level for computing breakout_pct and status.  This gives
    an accurate "distance from the Weinstein buy point" instead of a drifting
    rolling-max that rises with the stock price.

    Args:
        latest: Weekly indicators row (must include 'close' and 'breakout_level')
        config: App configuration
        base_breakout_price: Fixed base top from _detect_bases(), or None
    """
    # Choose reference price: base top > rolling breakout level
    ref_price: float | None = None
    use_base = False
    if base_breakout_price is not None and pd.notna(base_breakout_price) and base_breakout_price > 0:
        ref_price = float(base_breakout_price)
        use_base = True
    else:
        breakout_level = latest.get("breakout_level")
        if pd.notna(breakout_level) and float(breakout_level) != 0:
            ref_price = float(breakout_level)

    if ref_price is None:
        return {
            "breakout_ok": True,  # always True when filter disabled
            "breakout_strength": 0.0,
            "breakout_level": None,
            "breakout_pct": None,
            "breakout_status": "no_breakout_level",
            "breakout_reason": "无突破位",
        }

    close = float(latest["close"])
    breakout_pct = (close / ref_price - 1.0) * 100.0

    # Status classification
    max_pct = config.strategy.watch_breakout_max_pct
    near_pct = config.strategy.watch_near_breakout_pct
    min_pct = config.strategy.breakout_min_pct

    if breakout_pct >= 0 and breakout_pct <= max_pct:
        breakout_status = "just_broke_out"
    elif breakout_pct < 0 and breakout_pct >= -near_pct:
        breakout_status = "near_breakout"
    elif breakout_pct < -near_pct:
        breakout_status = "below_breakout"
    else:
        breakout_status = "extended_breakout"

    breakout_ok = breakout_pct >= min_pct if config.strategy.enable_breakout_filter else True

    reason = (
        f"基底突破({base_breakout_price:.2f})" if use_base else "动态压力突破"
    )

    return {
        "breakout_ok": breakout_ok,
        "breakout_strength": max(breakout_pct, 0.0),
        "breakout_pct": breakout_pct,
        "breakout_level": float(ref_price),
        "breakout_status": breakout_status,
        "breakout_reason": reason,
    }
