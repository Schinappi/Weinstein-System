from __future__ import annotations

import numpy as np
import pandas as pd

from winstan.config import AppConfig


def compute_weekly_indicators(
    weekly_bars: pd.DataFrame,
    market_weekly_bars: pd.DataFrame,
    config: AppConfig,
) -> pd.DataFrame:
    if weekly_bars.empty:
        return weekly_bars.copy()
    required = {"symbol", "trade_date", "close", "high", "low", "volume"}
    if not required.issubset(set(weekly_bars.columns)):
        return pd.DataFrame()

    weekly = weekly_bars.copy().sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    if {"trade_date", "close"}.issubset(set(market_weekly_bars.columns)):
        market = market_weekly_bars[["trade_date", "close"]].rename(columns={"close": "market_close"}).copy()
        market["trade_date"] = pd.to_datetime(market["trade_date"])
    else:
        market = pd.DataFrame(columns=["trade_date", "market_close"])
    weekly["trade_date"] = pd.to_datetime(weekly["trade_date"])
    weekly = weekly.merge(market, on="trade_date", how="left")

    grouped = weekly.groupby("symbol", sort=False)
    ma_window = config.strategy.ma_window_weeks
    short_window = config.strategy.short_ma_window_weeks
    volume_window = config.strategy.volume_avg_weeks
    slope_lookback = config.strategy.ma_slope_lookback_weeks

    weekly["ma_30w"] = grouped["close"].transform(lambda s: s.rolling(ma_window, min_periods=ma_window).mean())
    weekly["ma_10w"] = grouped["close"].transform(lambda s: s.rolling(short_window, min_periods=short_window).mean())
    weekly["weekly_volume_ma_10"] = grouped["volume"].transform(
        lambda s: s.rolling(volume_window, min_periods=volume_window).mean()
    )
    weekly["high_52w"] = grouped["high"].transform(
        lambda s: s.rolling(config.strategy.resistance_lookback_weeks, min_periods=10).max()
    )
    weekly["low_10w"] = grouped["low"].transform(lambda s: s.rolling(short_window, min_periods=short_window).min())
    weekly["rs_line"] = weekly["close"] / weekly["market_close"]
    weekly["ma_30w_slope"] = grouped["ma_30w"].transform(lambda s: (s - s.shift(slope_lookback)) / slope_lookback)
    weekly["ma_spread_pct"] = np.where(
        weekly["ma_30w"].notna() & weekly["ma_10w"].notna() & (weekly["ma_30w"] != 0),
        np.abs(weekly["ma_10w"] / weekly["ma_30w"] - 1.0) * 100.0,
        np.nan,
    )
    weekly["price_vs_ma_pct"] = np.where(
        weekly["ma_30w"].notna() & (weekly["ma_30w"] != 0),
        (weekly["close"] / weekly["ma_30w"] - 1.0) * 100.0,
        np.nan,
    )
    base_window = max(4, config.strategy.watch_base_lookback_weeks)
    weekly["base_range_pct"] = grouped["high"].transform(
        lambda s: (s.rolling(base_window, min_periods=base_window).max() / s.rolling(base_window, min_periods=base_window).min() - 1.0) * 100.0
    )
    weekly["base_close_std_pct"] = grouped["close"].transform(
        lambda s: (s.rolling(base_window, min_periods=base_window).std() / s.rolling(base_window, min_periods=base_window).mean()) * 100.0
    )
    weekly["headroom_to_52w_high_pct"] = np.where(
        weekly["high_52w"].notna() & (weekly["close"] != 0),
        (weekly["high_52w"] / weekly["close"] - 1.0) * 100.0,
        np.nan,
    )
    weekly["rs_13w_return"] = grouped["rs_line"].transform(lambda s: s / s.shift(13) - 1.0)
    weekly["rs_26w_return"] = grouped["rs_line"].transform(lambda s: s / s.shift(26) - 1.0)
    weekly["rs_52w_return"] = grouped["rs_line"].transform(lambda s: s / s.shift(52) - 1.0)
    weekly["breakout_level"] = grouped["high"].transform(
        lambda s: s.shift(1).rolling(config.strategy.breakout_lookback_weeks, min_periods=4).max()
    )
    return weekly


def compute_rs_ranks(weekly_bars: pd.DataFrame) -> pd.DataFrame:
    if weekly_bars.empty:
        return pd.DataFrame(columns=["symbol", "rs_rank_pct", "rs_composite"])

    latest = weekly_bars.sort_values(["symbol", "trade_date"]).groupby("symbol", as_index=False).tail(1).copy()
    latest["rs_composite"] = (
        latest["rs_13w_return"].fillna(0.0) * 0.50
        + latest["rs_26w_return"].fillna(0.0) * 0.30
        + latest["rs_52w_return"].fillna(0.0) * 0.20
    )
    latest["rs_rank_pct"] = latest["rs_composite"].rank(method="dense", pct=True, ascending=False) * 100.0
    return latest[["symbol", "rs_rank_pct", "rs_composite"]]
