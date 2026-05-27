from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig


def evaluate_market_trend(market_weekly: pd.DataFrame, config: AppConfig) -> dict[str, object]:
    if market_weekly.empty:
        return {
            "market_ok": False,
            "reason": "benchmark data unavailable",
        }

    latest = market_weekly.sort_values("trade_date").iloc[-1]
    price_above_ma = bool(latest["close"] > latest["ma_30w"]) if pd.notna(latest["ma_30w"]) else False
    short_above_long = bool(latest["ma_10w"] > latest["ma_30w"]) if pd.notna(latest["ma_10w"]) else False
    slope_up = bool(latest["ma_30w_slope"] > 0) if pd.notna(latest["ma_30w_slope"]) else False
    market_ok = price_above_ma and slope_up and short_above_long
    return {
        "market_ok": market_ok,
        "reason": "price_above_ma_30w and ma_30w_up and ma_10w_above_ma_30w" if market_ok else "market trend filter failed",
        "market_close": float(latest["close"]),
        "market_ma_30w": float(latest["ma_30w"]) if pd.notna(latest["ma_30w"]) else None,
    }

