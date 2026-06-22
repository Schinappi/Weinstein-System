from __future__ import annotations

import time
from typing import Iterable

import pandas as pd

from .base import BaseDataAdapter
from winstan.config import DataConfig, normalize_date_like


class AkshareAdapter(BaseDataAdapter):
    """A-share data via akshare (free, no token required).

    Fetches *qfq* (前复权) daily bars directly — no separate adj_factor
    step needed.
    """

    source_name = "akshare"

    def __init__(self, data_config: DataConfig | None = None) -> None:
        self._config = data_config or DataConfig()

    # ------------------------------------------------------------------
    def fetch_stock_universe(self) -> pd.DataFrame:
        import akshare as ak

        frame = ak.stock_zh_a_spot_em()
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["symbol", "name", "market", "list_date", "is_st"])

        out = pd.DataFrame()
        out["symbol"] = frame["代码"].astype(str).str.strip()
        out["name"] = frame["名称"].astype(str).str.strip()
        out["market"] = out["symbol"].apply(lambda s: "SH" if s.startswith(("6", "9")) else "SZ")
        out["list_date"] = pd.NaT
        out["is_st"] = out["name"].str.contains("ST", case=False, na=False)
        return out

    # ------------------------------------------------------------------
    def fetch_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        adjust_type: str = "forward",
    ) -> pd.DataFrame:
        import akshare as ak

        symbol_list = [str(s) for s in symbols if str(s)]
        if not symbol_list:
            return self._empty_frame()

        frames: list[pd.DataFrame] = []
        start = normalize_date_like(start_date) or "20180101"
        end = normalize_date_like(end_date) or ""
        start_fmt = start.replace("-", "")
        end_fmt = end.replace("-", "")

        print(
            f"[akshare] fetch_daily_bars start "
            f"symbols={len(symbol_list)} start_date={start_fmt} end_date={end_fmt} "
            f"sample={', '.join(symbol_list[:5])}"
        )

        for symbol in symbol_list:
            try:
                # akshare uses plain numeric codes: "603213" not "603213.SH"
                code = symbol.split(".")[0]
                raw = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_fmt,
                    end_date=end_fmt,
                    adjust="qfq",
                )
            except Exception as exc:
                print(f"[akshare] fetch failed symbol={symbol}: {exc}")
                continue

            if raw is None or raw.empty:
                continue

            raw = raw.rename(
                columns={
                    "日期": "trade_date",
                    "开盘": "open",
                    "最高": "high",
                    "最低": "low",
                    "收盘": "close",
                    "成交量": "volume",
                    "成交额": "amount",
                }
            )
            raw["symbol"] = symbol
            raw["source"] = "akshare"
            # akshare returns qfq prices; no adj_factor needed.
            raw["adj_factor"] = 1.0

            keep_cols = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "source"]
            raw = raw[[c for c in keep_cols if c in raw.columns]]
            frames.append(raw)

            if len(frames) % 200 == 0:
                print(f"[akshare] fetch_daily_bars progress {len(frames)}/{len(symbol_list)}")

        if not frames:
            print(f"[akshare] fetch_daily_bars done 0 rows")
            return self._empty_frame()

        combined = pd.concat(frames, ignore_index=True)
        normalized = self.ensure_standard_columns(combined, self.source_name)
        print(f"[akshare] fetch_daily_bars done symbols={len(symbol_list)} rows={len(normalized)}")
        return normalized

    # ------------------------------------------------------------------
    def fetch_index_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        import akshare as ak

        start_fmt = (normalize_date_like(start_date) or "20180101").replace("-", "")
        end_fmt = (normalize_date_like(end_date) or "").replace("-", "")

        # Map benchmark symbol to akshare index code
        index_map = {
            "000906.SH": "000906",  # 中证1000
            "000300.SH": "000300",  # 沪深300
            "000001.SH": "000001",  # 上证指数
            "399001.SZ": "399001",  # 深证成指
        }
        code = index_map.get(symbol, symbol.split(".")[0])

        try:
            raw = ak.stock_zh_index_daily_em(symbol=f"sh{code}" if symbol.endswith(".SH") else f"sz{code}")
        except Exception:
            try:
                raw = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_fmt, end_date=end_fmt)
            except Exception as exc:
                print(f"[akshare] index fetch failed symbol={symbol}: {exc}")
                return self._empty_index_frame()

        if raw is None or raw.empty:
            return self._empty_index_frame()

        raw = raw.rename(
            columns={
                "date": "trade_date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "amount": "amount",
            }
        )
        raw["symbol"] = symbol
        raw["source"] = "akshare"
        raw["adj_factor"] = 1.0

        keep = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "source"]
        raw = raw[[c for c in keep if c in raw.columns]]
        return self.ensure_standard_columns(raw, self.source_name)

    # ------------------------------------------------------------------
    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "source"])

    @staticmethod
    def _empty_index_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor", "source"])
