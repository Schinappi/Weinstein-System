from __future__ import annotations

import atexit
import contextlib
import io
import time
from collections.abc import Iterable

import pandas as pd

from winstan.config import DataConfig, normalize_date_like

from .base import BaseDataAdapter, STANDARD_COLUMNS
from .tushare_adapter import TushareAdapter


class BaostockAdapter(BaseDataAdapter):
    source_name = "baostock"

    def __init__(self, data_config: DataConfig | None = None) -> None:
        import baostock as bs

        self._bs = bs
        self._config = data_config or DataConfig()
        self._logged_in = False
        self._login()
        atexit.register(self.close)

    def _login(self) -> None:
        if self._logged_in:
            return
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = self._bs.login()
        if getattr(result, "error_code", "") != "0":
            raise RuntimeError(f"Baostock login failed: {getattr(result, 'error_msg', 'unknown error')}")
        self._logged_in = True

    def close(self) -> None:
        if not self._logged_in:
            return
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self._bs.logout()
        finally:
            self._logged_in = False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    @staticmethod
    def _normalize_date(value: str | None, fallback: str | None = None) -> str | None:
        return normalize_date_like(value, fallback)

    @staticmethod
    def _symbol_sample_text(symbols: list[str], limit: int = 5) -> str:
        if not symbols:
            return ""
        sample = symbols[:limit]
        suffix = "" if len(symbols) <= limit else f" ... +{len(symbols) - limit}"
        return ",".join(sample) + suffix

    @staticmethod
    def _to_bs_symbol(symbol: str) -> str:
        cleaned = str(symbol).strip()
        lowered = cleaned.lower()
        if lowered.startswith(("sh.", "sz.")):
            return lowered
        if "." in cleaned:
            code, market = cleaned.split(".", 1)
            market = market.upper()
            if market in {"SH", "SZ"}:
                return f"{market.lower()}.{code}"
        market_prefix = "sh" if cleaned.startswith(("5", "6", "9")) else "sz"
        return f"{market_prefix}.{cleaned}"

    @staticmethod
    def _to_app_symbol(symbol: str) -> str:
        cleaned = str(symbol).strip().lower()
        if cleaned.startswith(("sh.", "sz.")):
            market, code = cleaned.split(".", 1)
            return f"{code}.{market.upper()}"
        if cleaned.isdigit():
            return BaostockAdapter._to_app_symbol(BaostockAdapter._to_bs_symbol(cleaned))
        return cleaned.upper()

    @classmethod
    def _is_a_share_symbol(cls, symbol: str) -> bool:
        normalized = cls._to_bs_symbol(symbol)
        if not normalized.startswith(("sh.", "sz.")):
            return False
        market, code = normalized.split(".", 1)
        if market == "sh":
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        if market == "sz":
            return code.startswith(("000", "001", "002", "003", "300", "301"))
        return False

    def _query_frame(self, func, **kwargs) -> pd.DataFrame:
        self._login()
        last_error = "unknown error"
        for attempt in range(2):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = func(**kwargs)
            error_code = getattr(result, "error_code", "")
            error_msg = str(getattr(result, "error_msg", "unknown error") or "unknown error")
            if error_code == "0":
                break
            last_error = error_msg
            if "未登录" in error_msg and attempt == 0:
                self._logged_in = False
                self._login()
                continue
            raise RuntimeError(error_msg)
        else:
            raise RuntimeError(last_error)

        fields = list(getattr(result, "fields", []) or [])
        rows: list[list[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        if not fields:
            return pd.DataFrame(rows)
        return pd.DataFrame(rows, columns=fields)

    def _fetch_open_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        frame = self._query_frame(
            self._bs.query_trade_dates,
            start_date=start_date,
            end_date=end_date,
        )
        if frame.empty:
            return []
        return (
            frame.loc[frame["is_trading_day"].astype(str) == "1", "calendar_date"]
            .dropna()
            .astype(str)
            .sort_values()
            .tolist()
        )

    def fetch_stock_universe(self) -> pd.DataFrame:
        frame = self._query_frame(self._bs.query_stock_basic)
        if frame.empty:
            return pd.DataFrame(columns=["symbol", "name", "market", "list_date", "is_st"])

        frame = frame[
            (frame["type"].astype(str) == "1")
            & (frame["status"].astype(str) == "1")
            & frame["code"].map(self._is_a_share_symbol)
        ].copy()
        if frame.empty:
            return pd.DataFrame(columns=["symbol", "name", "market", "list_date", "is_st"])

        frame["symbol"] = frame["code"].map(self._to_app_symbol)
        frame["name"] = frame["code_name"].astype(str).str.strip()
        frame["market"] = frame["symbol"].str.split(".").str[-1]
        frame["list_date"] = pd.to_datetime(frame["ipoDate"], errors="coerce")
        frame["is_st"] = frame["name"].str.contains("ST", case=False, na=False)
        return frame[["symbol", "name", "market", "list_date", "is_st"]].reset_index(drop=True)

    def _attach_adjust_factors(self, frame: pd.DataFrame, bs_symbol: str, end_date: str) -> pd.DataFrame:
        adj_frame = self._query_frame(
            self._bs.query_adjust_factor,
            code=bs_symbol,
            end_date=end_date,
        )
        if adj_frame.empty:
            result = frame.copy()
            result["adj_factor"] = 1.0
            return result

        factors = adj_frame.rename(
            columns={
                "dividOperateDate": "trade_date",
                "adjustFactor": "adj_factor",
            }
        )[["trade_date", "adj_factor"]].copy()
        factors["trade_date"] = pd.to_datetime(factors["trade_date"], errors="coerce")
        factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
        factors = factors.dropna(subset=["trade_date", "adj_factor"]).sort_values("trade_date").reset_index(drop=True)
        if factors.empty:
            result = frame.copy()
            result["adj_factor"] = 1.0
            return result

        result = frame.copy().sort_values("trade_date").reset_index(drop=True)
        result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        result = pd.merge_asof(result, factors, on="trade_date", direction="backward")
        result["adj_factor"] = pd.to_numeric(result["adj_factor"], errors="coerce").fillna(float(factors["adj_factor"].iloc[0]))
        return result

    def fetch_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        adjust_type: str = "forward",
    ) -> pd.DataFrame:
        symbol_list = [self._to_app_symbol(str(symbol)) for symbol in symbols if str(symbol).strip()]
        if not symbol_list:
            return self._empty_frame()

        start = self._normalize_date(start_date, "2018-01-01") or "2018-01-01"
        end = self._normalize_date(end_date, start) or start
        frames: list[pd.DataFrame] = []
        failed_symbols = 0
        empty_symbols = 0
        progress_every = 50 if len(symbol_list) >= 200 else 20 if len(symbol_list) >= 50 else 10
        started_at = time.perf_counter()
        print(
            "[baostock] fetch_daily_bars start "
            f"symbols={len(symbol_list)} start_date={start} end_date={end} sample={self._symbol_sample_text(symbol_list)}"
        )

        for index, symbol in enumerate(symbol_list, start=1):
            if index == 1 or index % progress_every == 0 or index == len(symbol_list):
                print(
                    "[baostock] fetch_daily_bars progress "
                    f"current={index}/{len(symbol_list)} rows={sum(len(frame) for frame in frames)} "
                    f"empty={empty_symbols} failed={failed_symbols} symbol={symbol}"
                )

            bs_symbol = self._to_bs_symbol(symbol)
            try:
                history = self._query_frame(
                    self._bs.query_history_k_data_plus,
                    code=bs_symbol,
                    fields="date,code,open,high,low,close,volume,amount",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="3",
                )
            except Exception as exc:
                failed_symbols += 1
                print(f"[baostock] fetch failed symbol={symbol} reason={exc}")
                continue

            if history.empty:
                empty_symbols += 1
                continue

            history = history.rename(columns={"date": "trade_date"}).copy()
            history["trade_date"] = pd.to_datetime(history["trade_date"], errors="coerce")
            history["symbol"] = symbol

            if adjust_type == "forward":
                try:
                    history = self._attach_adjust_factors(history, bs_symbol, end)
                    history["ts_code"] = history["symbol"]
                    history = TushareAdapter._apply_forward_adjustment(history)
                    history = history.drop(columns=["ts_code"], errors="ignore")
                except Exception as exc:
                    print(f"[baostock] adj_factor fallback symbol={symbol} reason={exc}")
                    history["adj_factor"] = 1.0
            else:
                history["adj_factor"] = 1.0

            frames.append(
                history[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor"]]
            )

        if not frames:
            print(
                "[baostock] fetch_daily_bars done "
                f"symbols={len(symbol_list)} rows=0 elapsed={round(time.perf_counter() - started_at, 2)}s "
                f"empty_symbols={empty_symbols} failed_symbols={failed_symbols}"
            )
            return self._empty_frame()

        normalized = self.ensure_standard_columns(pd.concat(frames, ignore_index=True), self.source_name)
        print(
            "[baostock] fetch_daily_bars done "
            f"symbols={len(symbol_list)} rows={len(normalized)} elapsed={round(time.perf_counter() - started_at, 2)}s "
            f"empty_symbols={empty_symbols} failed_symbols={failed_symbols}"
        )
        return normalized

    def fetch_index_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        start = self._normalize_date(start_date, "2018-01-01") or "2018-01-01"
        end = self._normalize_date(end_date, start) or start
        bs_symbol = self._to_bs_symbol(symbol)
        try:
            frame = self._query_frame(
                self._bs.query_history_k_data_plus,
                code=bs_symbol,
                fields="date,code,open,high,low,close,volume,amount",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="3",
            )
        except Exception as exc:
            print(f"[baostock] index fetch failed symbol={symbol} reason={exc}")
            return self._empty_frame()

        if frame.empty:
            return self._empty_frame()

        frame = frame.rename(columns={"date": "trade_date"}).copy()
        frame["symbol"] = self._to_app_symbol(bs_symbol)
        frame["adj_factor"] = 1.0
        return self.ensure_standard_columns(
            frame[["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount", "adj_factor"]],
            self.source_name,
        )
