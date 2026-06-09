from __future__ import annotations

from pathlib import Path

import pandas as pd
import polars as pl


class ParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _dataset_dir(self, dataset: str) -> Path:
        path = self.root / dataset
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _symbol_path(self, dataset: str, symbol: str) -> Path:
        safe = symbol.replace("/", "_")
        return self._dataset_dir(dataset) / f"{safe}.parquet"

    def has_symbol(self, dataset: str, symbol: str) -> bool:
        return self._symbol_path(dataset, symbol).exists()

    def write_symbol_frame(self, dataset: str, symbol: str, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        path = self._symbol_path(dataset, symbol)
        pl.from_pandas(frame).write_parquet(path)

    def write_intraday_snapshot(self, date_str: str, frame: pd.DataFrame) -> None:
        """Write a snapshot of intraday real-time data for a given date.
        
        The frame must have at least columns: symbol, trade_date, open, high, low, close, volume.
        Stored as a single parquet file under data/intraday/YYYY-MM-DD.parquet
        so it can be merged with historical daily_bars without overwriting them.
        """
        if frame.empty:
            return
        dataset_dir = self.root / "intraday"
        dataset_dir.mkdir(parents=True, exist_ok=True)
        path = dataset_dir / f"{date_str}.parquet"
        pl.from_pandas(frame).write_parquet(path)

    def read_intraday_snapshot(self, date_str: str) -> pd.DataFrame:
        """Read intraday snapshot for a date, returns empty DataFrame if not found."""
        path = self.root / "intraday" / f"{date_str}.parquet"
        if not path.exists():
            return pd.DataFrame()
        return pl.read_parquet(path).to_pandas()

    def read_symbol_frame(self, dataset: str, symbol: str) -> pd.DataFrame:
        path = self._symbol_path(dataset, symbol)
        if not path.exists():
            return pd.DataFrame()
        return pl.read_parquet(path).to_pandas()

    def read_many(self, dataset: str, symbols: list[str]) -> pd.DataFrame:
        frames = [self.read_symbol_frame(dataset, symbol) for symbol in symbols]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def list_cached_symbols(self, dataset: str) -> list[str]:
        dataset_dir = self._dataset_dir(dataset)
        return sorted(path.stem for path in dataset_dir.glob("*.parquet"))

