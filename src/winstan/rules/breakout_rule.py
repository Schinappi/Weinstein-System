from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig


def evaluate_breakout(latest: pd.Series, config: AppConfig) -> dict[str, object]:
    breakout_level = latest.get("breakout_level")
    if pd.isna(breakout_level) or breakout_level == 0:
        return {
            "breakout_ok": not config.strategy.enable_breakout_filter,
            "breakout_strength": 0.0,
            "breakout_level": None,
            "breakout_pct": None,
            "breakout_status": "no_breakout_level",
            "breakout_reason": "无突破位",
        }

    breakout_pct = (latest["close"] / breakout_level - 1.0) * 100.0
    if breakout_pct >= 0 and breakout_pct <= config.strategy.watch_breakout_max_pct:
        breakout_status = "just_broke_out"
    elif breakout_pct < 0 and breakout_pct >= -config.strategy.watch_near_breakout_pct:
        breakout_status = "near_breakout"
    elif breakout_pct > config.strategy.watch_breakout_max_pct:
        breakout_status = "extended_breakout"
    else:
        breakout_status = "below_breakout"

    breakout_ok = True if not config.strategy.enable_breakout_filter else breakout_pct >= config.strategy.breakout_min_pct
    return {
        "breakout_ok": breakout_ok,
        "breakout_strength": max(breakout_pct, 0.0),
        "breakout_pct": breakout_pct,
        "breakout_level": float(breakout_level),
        "breakout_status": breakout_status,
        "breakout_reason": "突破有效" if breakout_pct >= config.strategy.breakout_min_pct else "未确认突破",
    }
