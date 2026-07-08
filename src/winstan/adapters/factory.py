from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from winstan.config import AppConfig

from .akshare_adapter import AkshareAdapter
from .base import BaseDataAdapter
from .tickflow_adapter import TickflowAdapter
from .tushare_adapter import ChinadataAdapter, TushareAdapter


def build_adapter(name: str, config: AppConfig) -> BaseDataAdapter:
    normalized = name.lower()
    if normalized in {"", "none", "null"}:
        raise ValueError("No adapter configured.")
    if normalized == "akshare":
        return AkshareAdapter(config.data)
    if normalized == "baostock":
        from .baostock_adapter import BaostockAdapter

        return BaostockAdapter(config.data)
    if normalized == "tushare":
        return TushareAdapter(config.data.tushare_token, config.data)
    if normalized == "chinadata":
        if not config.data.chinadata_token:
            raise ValueError("CHINADATA_TOKEN is not configured.")
        return ChinadataAdapter(config.data.chinadata_token, config.data)
    if normalized == "tickflow":
        return TickflowAdapter(
            api_key=config.data.tickflow_api_key,
            base_url=config.data.tickflow_base_url,
            free_base_url=config.data.tickflow_free_base_url,
        )
    raise ValueError(f"Unsupported data source: {name}")


class DataSourceRouter:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.primary = self._safe_build(config.data.primary_source)
        self.fallback = self._safe_build(config.data.fallback_source)
        if self.primary is None and self.fallback is None:
            raise RuntimeError("No usable data source is available. Check Tushare/Baostock/TickFlow configuration.")

    def _safe_build(self, source_name: str) -> BaseDataAdapter | None:
        try:
            return build_adapter(source_name, self.config)
        except Exception:
            return None

    def fetch_stock_universe(self) -> pd.DataFrame:
        # ── 从 parquet 缓存构建 universe（绕过 stock_basic 限速）──
        daily_dir = self.config.parquet_root / 'daily_bars'
        if daily_dir.exists():
            files = sorted(daily_dir.glob('*.parquet'))
            symbols = [f.stem for f in files if f.stem != '__today__']
            import pandas as pd
            frame = pd.DataFrame({
                'symbol': symbols,
                'name': '',
                'market': '',
                'list_date': pd.NaT,
                'is_st': False,
            })
            if not frame.empty:
                print(f"[router] fetch_stock_universe from parquet cache: {len(frame)} symbols")
                return frame
        # 回退到 API
        for adapter in (self.primary, self.fallback):
            if adapter is None:
                continue
            try:
                frame = adapter.fetch_stock_universe()
                if not frame.empty:
                    return frame
            except Exception:
                continue
        raise RuntimeError("Failed to fetch stock universe from all configured data sources.")

    def fetch_daily_bars(self, symbols: Iterable[str], start_date: str, end_date: str) -> pd.DataFrame:
        symbol_list = list(symbols)
        if not symbol_list:
            return pd.DataFrame()

        primary_frame = pd.DataFrame()
        primary_name = getattr(self.primary, "source_name", "none") if self.primary is not None else "none"
        fallback_name = getattr(self.fallback, "source_name", "none") if self.fallback is not None else "none"
        if self.primary is not None:
            try:
                primary_frame = self.primary.fetch_daily_bars(
                    symbol_list,
                    start_date=start_date,
                    end_date=end_date,
                    adjust_type=self.config.data.adjust_type,
                )
            except Exception:
                primary_frame = pd.DataFrame()
            print(
                "[router] fetch_daily_bars primary "
                f"source={primary_name} symbols={len(symbol_list)} rows={len(primary_frame)}"
            )

        if primary_frame.empty:
            if self.fallback is None:
                return primary_frame
            try:
                fallback_frame = self.fallback.fetch_daily_bars(
                    symbol_list,
                    start_date=start_date,
                    end_date=end_date,
                    adjust_type=self.config.data.adjust_type,
                )
                print(
                    "[router] fetch_daily_bars fallback "
                    f"source={fallback_name} symbols={len(symbol_list)} rows={len(fallback_frame)}"
                )
                return fallback_frame
            except Exception:
                print(
                    "[router] fetch_daily_bars fallback failed "
                    f"source={fallback_name} symbols={len(symbol_list)}"
                )
                return pd.DataFrame()

        fetched_symbols = set(primary_frame["symbol"].unique())
        missing = [symbol for symbol in symbol_list if symbol not in fetched_symbols]
        if not missing or self.fallback is None:
            return primary_frame

        try:
            fallback_frame = self.fallback.fetch_daily_bars(
                missing,
                start_date=start_date,
                end_date=end_date,
                adjust_type=self.config.data.adjust_type,
            )
        except Exception:
            fallback_frame = pd.DataFrame()
        print(
            "[router] fetch_daily_bars supplement "
            f"source={fallback_name} missing={len(missing)} rows={len(fallback_frame)}"
        )
        if fallback_frame.empty:
            return primary_frame
        return pd.concat([primary_frame, fallback_frame], ignore_index=True)

    def fetch_index_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        for adapter in (self.primary, self.fallback):
            if adapter is None:
                continue
            try:
                frame = adapter.fetch_index_daily_bars(symbol, start_date, end_date)
                if not frame.empty:
                    return frame
            except Exception:
                continue
        return pd.DataFrame()
