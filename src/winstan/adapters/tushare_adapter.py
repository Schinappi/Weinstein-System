from __future__ import annotations

import contextlib
import io
import time
from collections import deque
from typing import Iterable

import pandas as pd

from .base import BaseDataAdapter
from .tushare_client import build_tushare_pro
from winstan.config import DataConfig, normalize_date_like


class TushareAdapter(BaseDataAdapter):
    source_name = "tushare"

    def __init__(self, token: str | None, data_config: DataConfig | None = None) -> None:
        self._ts, self._pro = build_tushare_pro(token)
        self._config = data_config or DataConfig()
        self._call_timestamps: deque[float] = deque()
        self._trade_cal_cache: dict[tuple[str, str], list[str]] = {}
        self._trade_cal_remote_unavailable = False
        self._stock_daily_remote_unavailable = False
        try:
            self._pro._DataApi__timeout = self._config.tushare_timeout_seconds
        except Exception:
            pass

    def _throttle(self) -> None:
        calls_per_minute = max(1, min(self._config.tushare_calls_per_minute, 400))
        now = time.monotonic()
        while self._call_timestamps and now - self._call_timestamps[0] >= 60.0:
            self._call_timestamps.popleft()

        if len(self._call_timestamps) >= calls_per_minute:
            sleep_for = 60.0 - (now - self._call_timestamps[0]) + 0.2
            if sleep_for > 0:
                time.sleep(sleep_for)

        now = time.monotonic()
        while self._call_timestamps and now - self._call_timestamps[0] >= 60.0:
            self._call_timestamps.popleft()
        self._call_timestamps.append(now)

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "connection aborted" in message
            or "remote end closed connection without response" in message
            or "remotedisconnected" in message
            or "connection reset" in message
            or "connection broken" in message
        )

    def _call_api(self, func, **kwargs) -> tuple[pd.DataFrame, str | None]:
        for attempt in range(self._config.tushare_retry_times):
            try:
                self._throttle()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    frame = func(**kwargs)
                if frame is None:
                    return pd.DataFrame(), None
                if isinstance(frame, pd.DataFrame):
                    return frame, None
                if isinstance(frame, str):
                    message = frame.strip().lower()
                    if "token无效" in frame or "token invalid" in message or "expired" in message or "超期" in frame:
                        return pd.DataFrame(), "auth_error"
                    return pd.DataFrame(), "other_error"
                return pd.DataFrame(), "other_error"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                message = str(exc).lower()
                if self._is_connection_error(exc):
                    return pd.DataFrame(), "connection_error"
                if "timeout" in message or "too many requests" in message or "每分钟最多调用400次" in str(exc):
                    sleep_seconds = self._config.tushare_retry_sleep_seconds * (attempt + 1)
                    time.sleep(sleep_seconds)
                    continue
                return pd.DataFrame(), "other_error"
        return pd.DataFrame(), "retry_exhausted"

    @staticmethod
    def _business_day_trade_dates(start_date: str, end_date: str) -> list[str]:
        start = pd.to_datetime(start_date, format="%Y%m%d", errors="coerce")
        end = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce")
        if pd.isna(start) or pd.isna(end) or start > end:
            return []
        return [value.strftime("%Y%m%d") for value in pd.bdate_range(start=start, end=end)]

    def _fetch_open_trade_dates(self, start_date: str, end_date: str) -> list[str]:
        cache_key = (start_date, end_date)
        if cache_key in self._trade_cal_cache:
            cached = self._trade_cal_cache[cache_key]
            print(
                "[tushare] trade_cal cache hit "
                f"start_date={start_date} end_date={end_date} open_days={len(cached)}"
            )
            return list(cached)

        if self._trade_cal_remote_unavailable:
            fallback_dates = self._business_day_trade_dates(start_date, end_date)
            self._trade_cal_cache[cache_key] = fallback_dates
            print(
                "[tushare] trade_cal fallback "
                f"start_date={start_date} end_date={end_date} reason=remote_unavailable open_days={len(fallback_dates)}"
            )
            return list(fallback_dates)

        calendar_frame, failure_reason = self._call_api(
            self._pro.trade_cal,
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
            is_open="1",
            fields="cal_date",
        )
        if failure_reason is not None:
            print(
                "[tushare] trade_cal failed "
                f"start_date={start_date} end_date={end_date} reason={failure_reason}"
            )
            if failure_reason == "connection_error":
                self._trade_cal_remote_unavailable = True
                fallback_dates = self._business_day_trade_dates(start_date, end_date)
                self._trade_cal_cache[cache_key] = fallback_dates
                print(
                    "[tushare] trade_cal fallback "
                    f"start_date={start_date} end_date={end_date} reason=connection_error open_days={len(fallback_dates)}"
                )
                return list(fallback_dates)
            return []

        if calendar_frame is None or calendar_frame.empty or "cal_date" not in calendar_frame.columns:
            row_count = 0 if calendar_frame is None else len(calendar_frame)
            columns = [] if calendar_frame is None else calendar_frame.columns.tolist()
            print(
                "[tushare] trade_cal empty "
                f"start_date={start_date} end_date={end_date} rows={row_count} columns={columns}"
            )
            return []

        trade_dates = sorted(str(value) for value in calendar_frame["cal_date"].dropna().astype(str).unique())
        self._trade_cal_cache[cache_key] = trade_dates
        return list(trade_dates)

    @staticmethod
    def _apply_forward_adjustment(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty or "adj_factor" not in frame.columns:
            return frame

        adjusted = frame.copy()
        adjusted["trade_date"] = pd.to_datetime(adjusted["trade_date"], format="%Y%m%d", errors="coerce")
        adjusted["adj_factor"] = pd.to_numeric(adjusted["adj_factor"], errors="coerce")
        latest_adj_factor = adjusted.sort_values(["ts_code", "trade_date"]).groupby("ts_code")["adj_factor"].transform("last")
        valid_mask = adjusted["adj_factor"].notna() & latest_adj_factor.notna() & (latest_adj_factor != 0)
        ratio = pd.Series(1.0, index=adjusted.index, dtype=float)
        ratio.loc[valid_mask] = adjusted.loc[valid_mask, "adj_factor"] / latest_adj_factor.loc[valid_mask]

        for column in ("open", "high", "low", "close"):
            adjusted[column] = pd.to_numeric(adjusted[column], errors="coerce") * ratio
        return adjusted

    @staticmethod
    def _symbol_sample_text(symbols: list[str], limit: int = 5) -> str:
        if not symbols:
            return ""
        sample = symbols[:limit]
        suffix = "" if len(symbols) <= limit else f" ... +{len(symbols) - limit}"
        return ",".join(sample) + suffix

    def fetch_stock_universe(self) -> pd.DataFrame:
        fields = "ts_code,symbol,name,market,list_date"
        frame, failure_reason = self._call_api(self._pro.stock_basic, exchange="", list_status="L", fields=fields)
        if failure_reason is not None or frame is None or frame.empty:
            return pd.DataFrame(columns=["symbol", "name", "market", "list_date", "is_st"])

        frame = frame.rename(columns={"ts_code": "symbol", "symbol": "ticker"})
        frame["list_date"] = pd.to_datetime(frame["list_date"], format="%Y%m%d", errors="coerce")
        frame["is_st"] = frame["name"].fillna("").str.upper().str.contains("ST")
        return frame[["symbol", "name", "market", "list_date", "is_st"]]

    def fetch_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        adjust_type: str = "forward",
    ) -> pd.DataFrame:
        symbol_list = [str(symbol) for symbol in symbols if str(symbol)]
        if not symbol_list:
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)

        if self._stock_daily_remote_unavailable:
            print(
                "[tushare] fetch_daily_bars skipped "
                f"symbols={len(symbol_list)} start_date={start_date} end_date={end_date} reason=remote_unavailable"
            )
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)

        total_started_at = time.perf_counter()
        frames: list[pd.DataFrame] = []
        daily_api_runtime_seconds = 0.0
        adj_api_runtime_seconds = 0.0
        filter_runtime_seconds = 0.0
        daily_empty_symbols = 0
        failed_symbols = 0
        start = (normalize_date_like(start_date) or "").replace("-", "")
        end = (normalize_date_like(end_date) or "").replace("-", "")
        print(
            "[tushare] fetch_daily_bars start "
            f"symbols={len(symbol_list)} start_date={start} end_date={end} sample={self._symbol_sample_text(symbol_list)}"
        )
        progress_every = 50 if len(symbol_list) >= 200 else 20 if len(symbol_list) >= 50 else 10
        for index, symbol in enumerate(symbol_list, start=1):
            symbol_started_at = time.perf_counter()
            if index == 1 or index % progress_every == 0 or index == len(symbol_list):
                print(
                    "[tushare] fetch_daily_bars progress "
                    f"current={index}/{len(symbol_list)} symbol={symbol} rows={sum(len(frame) for frame in frames)} empty={daily_empty_symbols} failed={failed_symbols}"
                )
            daily_started_at = time.perf_counter()
            daily_frame, daily_failure_reason = self._call_api(
                self._pro.daily,
                ts_code=symbol,
                start_date=start,
                end_date=end,
            )
            daily_api_runtime_seconds += time.perf_counter() - daily_started_at
            if daily_failure_reason is not None:
                failed_symbols += 1
                if daily_failure_reason in {"connection_error", "auth_error"}:
                    self._stock_daily_remote_unavailable = True
                print(
                    "[tushare] daily failed "
                    f"current={index}/{len(symbol_list)} symbol={symbol} start_date={start} end_date={end} reason={daily_failure_reason}"
                )
                if self._stock_daily_remote_unavailable:
                    break
                continue
            if daily_frame is None or daily_frame.empty:
                daily_empty_symbols += 1
                symbol_elapsed_seconds = time.perf_counter() - symbol_started_at
                if symbol_elapsed_seconds >= 5:
                    print(
                        "[tushare] daily empty slow "
                        f"current={index}/{len(symbol_list)} symbol={symbol} elapsed={round(symbol_elapsed_seconds, 2)}s"
                    )
                continue

            filtered = daily_frame.copy()

            if adjust_type == "forward":
                adj_started_at = time.perf_counter()
                adj_factor_frame, adj_failure_reason = self._call_api(
                    self._pro.adj_factor,
                    ts_code=symbol,
                    start_date=start,
                    end_date=end,
                    fields="ts_code,trade_date,adj_factor",
                )
                adj_api_runtime_seconds += time.perf_counter() - adj_started_at
                if adj_failure_reason is not None:
                    if adj_failure_reason in {"connection_error", "auth_error"}:
                        self._stock_daily_remote_unavailable = True
                    print(
                        "[tushare] adj_factor failed "
                        f"symbol={symbol} start_date={start} end_date={end} reason={adj_failure_reason}"
                    )
                    if self._stock_daily_remote_unavailable:
                        break
                    frames.append(filtered)
                    symbol_elapsed_seconds = time.perf_counter() - symbol_started_at
                    if symbol_elapsed_seconds >= 5:
                        print(
                            "[tushare] symbol slow "
                            f"current={index}/{len(symbol_list)} symbol={symbol} rows={len(filtered)} elapsed={round(symbol_elapsed_seconds, 2)}s mode=daily_only"
                        )
                    continue
                if adj_factor_frame is not None and not adj_factor_frame.empty:
                    merge_started_at = time.perf_counter()
                    filtered = filtered.merge(
                        adj_factor_frame[["ts_code", "trade_date", "adj_factor"]],
                        on=["ts_code", "trade_date"],
                        how="left",
                    )
                    filter_runtime_seconds += time.perf_counter() - merge_started_at
            frames.append(filtered)
            symbol_elapsed_seconds = time.perf_counter() - symbol_started_at
            if symbol_elapsed_seconds >= 5:
                print(
                    "[tushare] symbol slow "
                    f"current={index}/{len(symbol_list)} symbol={symbol} rows={len(filtered)} elapsed={round(symbol_elapsed_seconds, 2)}s"
                )

        if not frames:
            print(
                "[tushare] fetch_daily_bars done "
                f"symbols={len(symbol_list)} rows=0 elapsed={round(time.perf_counter() - total_started_at, 2)}s "
                f"daily_api={round(daily_api_runtime_seconds, 2)}s "
                f"adj_api={round(adj_api_runtime_seconds, 2)}s filter_merge={round(filter_runtime_seconds, 2)}s "
                f"daily_empty_symbols={daily_empty_symbols} failed_symbols={failed_symbols}"
            )
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)

        concat_started_at = time.perf_counter()
        combined = pd.concat(frames, ignore_index=True)
        concat_runtime_seconds = time.perf_counter() - concat_started_at
        adjust_runtime_seconds = 0.0
        if adjust_type == "forward":
            adjust_started_at = time.perf_counter()
            combined = self._apply_forward_adjustment(combined)
            adjust_runtime_seconds = time.perf_counter() - adjust_started_at

        normalize_started_at = time.perf_counter()
        normalized = self.ensure_standard_columns(
            combined.rename(columns={"ts_code": "symbol", "vol": "volume"}),
            self.source_name,
        )
        normalize_runtime_seconds = time.perf_counter() - normalize_started_at
        print(
            "[tushare] fetch_daily_bars done "
            f"symbols={len(symbol_list)} rows={len(normalized)} elapsed={round(time.perf_counter() - total_started_at, 2)}s "
            f"daily_api={round(daily_api_runtime_seconds, 2)}s "
            f"adj_api={round(adj_api_runtime_seconds, 2)}s filter_merge={round(filter_runtime_seconds, 2)}s "
            f"concat={round(concat_runtime_seconds, 2)}s adjust={round(adjust_runtime_seconds, 2)}s normalize={round(normalize_runtime_seconds, 2)}s"
        )
        if normalized.empty:
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)
        return normalized

    def fetch_index_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        frame, failure_reason = self._call_api(
            self._pro.index_daily,
            ts_code=symbol,
            start_date=(normalize_date_like(start_date) or "").replace("-", ""),
            end_date=(normalize_date_like(end_date) or "").replace("-", ""),
        )
        if failure_reason is not None or frame is None or frame.empty:
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)

        return self.ensure_standard_columns(
            frame.rename(columns={"ts_code": "symbol", "vol": "volume"}),
            self.source_name,
        )
