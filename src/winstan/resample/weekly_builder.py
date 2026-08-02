from __future__ import annotations

import pandas as pd

WEEKLY_COLUMNS = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "source"]


def build_weekly_bars(daily_bars: pd.DataFrame) -> pd.DataFrame:
    if daily_bars.empty:
        return pd.DataFrame(columns=WEEKLY_COLUMNS)

    required = {"symbol", "trade_date", "open", "high", "low", "close", "volume"}
    if not required.issubset(set(daily_bars.columns)):
        return pd.DataFrame(columns=WEEKLY_COLUMNS)

    frame = daily_bars.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="mixed", errors="coerce")
    frame = frame.dropna(subset=["trade_date"])
    frame = frame.sort_values(["symbol", "trade_date"])
    unique_symbols = frame["symbol"].dropna().unique()

    agg_map: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if "amount" in frame.columns:
        agg_map["amount"] = "sum"
    if "adj_factor" in frame.columns:
        agg_map["adj_factor"] = "last"
    if "source" in frame.columns:
        agg_map["source"] = "last"

    if len(unique_symbols) == 1:
        indexed = frame.set_index("trade_date")
        weekly = (
            indexed.resample("W-FRI")
            .agg(agg_map)
            .dropna(subset=["open", "high", "low", "close"], how="any")
            .reset_index()
        )
        weekly["symbol"] = unique_symbols[0]
        result = weekly
        for column in WEEKLY_COLUMNS:
            if column not in result.columns:
                result[column] = None
        return result[WEEKLY_COLUMNS]

    weekly_frames: list[pd.DataFrame] = []
    for symbol, group in frame.groupby("symbol", sort=False):
        indexed = group.set_index("trade_date")
        weekly = (
            indexed.resample("W-FRI")
            .agg(agg_map)
            .dropna(subset=["open", "high", "low", "close"], how="any")
            .reset_index()
        )
        weekly["symbol"] = symbol
        weekly_frames.append(weekly)

    if not weekly_frames:
        return pd.DataFrame(columns=WEEKLY_COLUMNS)
    result = pd.concat(weekly_frames, ignore_index=True)
    for column in WEEKLY_COLUMNS:
        if column not in result.columns:
            result[column] = None
    return result[WEEKLY_COLUMNS]
