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


def evaluate_resistance(
    recent: pd.DataFrame,
    latest: pd.Series,
    config: AppConfig,
    base_breakout_price: float | None = None,
) -> dict[str, object]:
    if recent.empty:
        return {"resistance_ok": False, "headroom_pct": None, "nearest_resistance": None}

    current_close = float(latest["close"])
    # entry_ref: use max(close, base_bp) so minor swing highs below
    # the base breakout price don't count as "overhead resistance"
    entry_ref = current_close
    if base_breakout_price is not None and base_breakout_price > entry_ref:
        entry_ref = base_breakout_price

    nearest = _find_nearest_swing_high(recent.tail(config.strategy.resistance_lookback_weeks), entry_ref)
    high_52w = latest.get("high_52w")

    resistance = nearest
    if resistance is None and pd.notna(high_52w) and float(high_52w) > entry_ref:
        resistance = float(high_52w)
    # Fallback: use breakout_level itself as the resistance reference
    # when no swing high or 52w high exists above entry_ref
    if resistance is None:
        breakout_level = latest.get("breakout_level")
        if pd.notna(breakout_level) and float(breakout_level) >= entry_ref:
            resistance = float(breakout_level)
    # Ultimate fallback: use base_breakout_price as the resistance
    # when it's above the current close (the fixed base top IS the
    # relevant overhead to clear)
    if resistance is None and base_breakout_price is not None and base_breakout_price > current_close:
        resistance = float(base_breakout_price)

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


def compute_overhead_supply(daily_bars: pd.DataFrame) -> dict[str, object]:
    """Compute overhead supply: % of past 250 trading days where close > current close.

    This measures trapped sellers above current price — the Weinstein idea that
    what matters is not "how far to resistance" but "are there sellers waiting above?"

    Returns dict with:
        overhead_supply_pct: 0-100, lower = cleaner overhead
        overhead_supply_days: number of days above current close (out of lookback)
        overhead_supply_lookback: actual lookback window used
        overhead_supply_ok: True if supply is acceptable (<=40%)
    """
    frame = daily_bars.copy()
    if frame.empty or "close" not in frame.columns or "trade_date" not in frame.columns:
        return {"overhead_supply_pct": 50.0, "overhead_supply_days": 0, "overhead_supply_lookback": 0, "overhead_supply_ok": False}
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    close_series = pd.to_numeric(frame["close"], errors="coerce").dropna()
    if len(close_series) < 10:
        return {"overhead_supply_pct": 50.0, "overhead_supply_days": 0, "overhead_supply_lookback": 0, "overhead_supply_ok": False}
    current_close = float(close_series.iloc[-1])
    lookback = min(250, len(close_series) - 1)
    if lookback < 1:
        return {"overhead_supply_pct": 0.0, "overhead_supply_days": 0, "overhead_supply_lookback": 0, "overhead_supply_ok": True}
    # Count days BEFORE today where close > current close (trapped sellers)
    overhead_days = int((close_series.iloc[-(lookback+1):-1] > current_close).sum())
    overhead_supply_pct = overhead_days / lookback * 100.0
    overhead_supply_ok = overhead_supply_pct <= 40.0  # ≤40% is acceptable
    return {
        "overhead_supply_pct": round(overhead_supply_pct, 2),
        "overhead_supply_days": overhead_days,
        "overhead_supply_lookback": lookback,
        "overhead_supply_ok": overhead_supply_ok,
    }
