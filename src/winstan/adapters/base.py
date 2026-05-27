from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

import pandas as pd


STANDARD_COLUMNS = [
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


class BaseDataAdapter(ABC):
    source_name: str

    @abstractmethod
    def fetch_stock_universe(self) -> pd.DataFrame:
        """Return stock universe metadata."""

    @abstractmethod
    def fetch_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        adjust_type: str = "forward",
    ) -> pd.DataFrame:
        """Return stock daily bars."""

    @abstractmethod
    def fetch_index_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """Return benchmark daily bars."""

    @staticmethod
    def ensure_standard_columns(frame: pd.DataFrame, source_name: str) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        result = frame.copy()
        if "trade_date" not in result.columns:
            for candidate in ("date", "datetime", "timestamp", "time"):
                if candidate in result.columns:
                    result = result.rename(columns={candidate: "trade_date"})
                    break
        if "symbol" not in result.columns:
            for candidate in ("ts_code", "code"):
                if candidate in result.columns:
                    result = result.rename(columns={candidate: "symbol"})
                    break
        if "volume" not in result.columns and "vol" in result.columns:
            result = result.rename(columns={"vol": "volume"})

        if "trade_date" in result.columns:
            result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.normalize()
        else:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        if "close" not in result.columns:
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        if "adj_factor" not in result.columns:
            result["adj_factor"] = 1.0
        result["source"] = source_name

        for column in STANDARD_COLUMNS:
            if column not in result.columns:
                result[column] = None

        result = result[STANDARD_COLUMNS].sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        return result
