from __future__ import annotations

import pandas as pd

from winstan.config import AppConfig


def _find_nearest_swing_high(recent: pd.DataFrame, current_close: float) -> float | None:
    highs = recent["high"].tolist()
    swing_highs: list[float] = []
    for index in range(1, len(highs) - 1):
        if highs[index] >= highs[index - 1] and highs[index] >= highs[index + 1]:
            if highs[index] > current_close:
                swing_highs.append(float(highs[index]))
    if not swing_highs:
        return None
    return min(swing_highs)


def evaluate_resistance(recent: pd.DataFrame, latest: pd.Series, config: AppConfig) -> dict[str, object]:
    if recent.empty:
        return {"resistance_ok": False, "headroom_pct": None, "nearest_resistance": None}

    current_close = float(latest["close"])
    nearest = _find_nearest_swing_high(recent.tail(config.strategy.resistance_lookback_weeks), current_close)
    high_52w = latest.get("high_52w")

    resistance = nearest
    if resistance is None and pd.notna(high_52w) and float(high_52w) > current_close:
        resistance = float(high_52w)

    if resistance is None:
        return {
            "resistance_ok": True,
            "headroom_pct": None,
            "nearest_resistance": None,
            "resistance_reason": "no clear overhead resistance",
        }

    headroom_pct = (resistance / current_close - 1.0) * 100.0
    resistance_ok = headroom_pct >= config.strategy.resistance_min_headroom_pct
    return {
        "resistance_ok": resistance_ok,
        "headroom_pct": headroom_pct,
        "nearest_resistance": resistance,
        "resistance_reason": "sufficient headroom" if resistance_ok else "overhead resistance too close",
    }

