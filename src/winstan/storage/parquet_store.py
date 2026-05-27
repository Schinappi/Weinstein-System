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

