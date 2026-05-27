from __future__ import annotations

import pandas as pd

STANDARD_DAILY_COLUMNS = [
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adj_factor",
    "source",
]


def clean_daily_bars(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS)

    cleaned = frame.copy()
    required = {"symbol", "trade_date", "close"}
    if not required.issubset(set(cleaned.columns)):
        return pd.DataFrame(columns=STANDARD_DAILY_COLUMNS)

    if "adj_factor" not in cleaned.columns:
        cleaned["adj_factor"] = 1.0
    cleaned["trade_date"] = pd.to_datetime(cleaned["trade_date"]).dt.normalize()
    cleaned = cleaned.dropna(subset=["symbol", "trade_date", "close"])
    cleaned = cleaned.drop_duplicates(subset=["symbol", "trade_date"], keep="last")
    cleaned = cleaned.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    numeric_columns = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]
    for column in numeric_columns:
        if column in cleaned.columns:
            cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")

    for column in STANDARD_DAILY_COLUMNS:
        if column not in cleaned.columns:
            cleaned[column] = None
    return cleaned[STANDARD_DAILY_COLUMNS]
