"""Rebuild weekly parquet bars from the local daily cache."""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from winstan.config import load_config
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.storage.parquet_store import ParquetStore


def main() -> None:
    config = load_config(project_root / "config/strategy.yaml")
    store = ParquetStore(config.parquet_root)
    symbols = store.list_cached_symbols("daily_bars")
    updated = 0
    for index, symbol in enumerate(symbols, start=1):
        weekly = build_weekly_bars(store.read_symbol_frame("daily_bars", symbol))
        if not weekly.empty:
            store.write_symbol_frame("weekly_bars", symbol, weekly)
            updated += 1
        if index % 500 == 0:
            print(f"rebuilt {index}/{len(symbols)} symbols", flush=True)
    print(f"done weekly_symbols={updated}", flush=True)


if __name__ == "__main__":
    main()
