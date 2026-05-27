from __future__ import annotations

from datetime import datetime
import time
from typing import Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import BaseDataAdapter
from winstan.config import normalize_date_like


HTTP_FALLBACK_SYMBOL_LIMIT = 20


class TickflowAdapter(BaseDataAdapter):
    source_name = "tickflow"

    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        free_base_url: str,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._free_base_url = free_base_url.rstrip("/")
        self._session = requests.Session()
        self._session.trust_env = False
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._client = self._build_sdk_client()

    def _build_sdk_client(self):
        try:
            from tickflow import TickFlow
        except ImportError:
            return None

        if self._api_key:
            return TickFlow(api_key=self._api_key)
        return TickFlow.free()

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key} if self._api_key else {}

    @property
    def _active_base_url(self) -> str:
        return self._base_url if self._api_key else self._free_base_url

    def fetch_stock_universe(self) -> pd.DataFrame:
        url = f"{self._active_base_url}/v1/universes/CN_Equity_A"
        payload = self._request_json(url)
        if not payload:
            return pd.DataFrame(columns=["symbol", "name", "market", "list_date", "is_st"])
        payload = payload.get("data", {})
        symbols = payload.get("symbols", [])
        frame = pd.DataFrame({"symbol": symbols})
        frame["name"] = None
        frame["market"] = "A"
        frame["list_date"] = pd.NaT
        frame["is_st"] = False
        return frame[["symbol", "name", "market", "list_date", "is_st"]]

    def fetch_daily_bars(
        self,
        symbols: Iterable[str],
        start_date: str,
        end_date: str,
        adjust_type: str = "forward",
    ) -> pd.DataFrame:
        symbol_list = list(symbols)
        if not symbol_list:
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)

        started_at = time.perf_counter()
        if self._client is not None:
            try:
                frame = self._fetch_via_sdk(symbol_list, start_date, end_date)
                print(
                    "[tickflow] fetch_daily_bars done "
                    f"mode=sdk symbols={len(symbol_list)} rows={len(frame)} elapsed={round(time.perf_counter() - started_at, 2)}s"
                )
                return frame
            except Exception:
                pass

        if len(symbol_list) > HTTP_FALLBACK_SYMBOL_LIMIT:
            print(
                "[tickflow] fetch_daily_bars skipped "
                f"mode=http symbols={len(symbol_list)} reason=batch_too_large limit={HTTP_FALLBACK_SYMBOL_LIMIT}"
            )
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)

        frames = [self._fetch_single_via_http(symbol, start_date, end_date) for symbol in symbol_list]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            print(
                "[tickflow] fetch_daily_bars done "
                f"mode=http symbols={len(symbol_list)} rows=0 elapsed={round(time.perf_counter() - started_at, 2)}s"
            )
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)
        combined = pd.concat(frames, ignore_index=True)
        normalized = self.ensure_standard_columns(combined, self.source_name)
        print(
            "[tickflow] fetch_daily_bars done "
            f"mode=http symbols={len(symbol_list)} rows={len(normalized)} elapsed={round(time.perf_counter() - started_at, 2)}s"
        )
        return normalized

    def fetch_index_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        frame = self.fetch_daily_bars([symbol], start_date, end_date)
        return frame[frame["symbol"] == symbol].reset_index(drop=True)

    def _fetch_via_sdk(self, symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        start_time = int(datetime.fromisoformat(normalize_date_like(start_date) or "").timestamp() * 1000)
        end_time = int(datetime.fromisoformat(normalize_date_like(end_date) or "").timestamp() * 1000)
        frames = self._client.klines.batch(
            symbols,
            period="1d",
            count=10000,
            start_time=start_time,
            end_time=end_time,
            as_dataframe=True,
            show_progress=False,
        )
        normalized: list[pd.DataFrame] = []
        for symbol, frame in frames.items():
            if frame is None or frame.empty:
                continue
            local = frame.copy()
            local["symbol"] = symbol
            local["trade_date"] = pd.to_datetime(local["trade_date"], errors="coerce")
            normalized.append(local)

        if not normalized:
            return pd.DataFrame(columns=BaseDataAdapter.ensure_standard_columns(pd.DataFrame(), self.source_name).columns)
        combined = pd.concat(normalized, ignore_index=True)
        return self.ensure_standard_columns(combined, self.source_name)

    def _fetch_single_via_http(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        url = f"{self._active_base_url}/v1/klines"
        normalized_start = normalize_date_like(start_date) or ""
        normalized_end = normalize_date_like(end_date) or ""
        params = {
            "symbol": symbol,
            "period": "1d",
            "count": 10000,
            "start_time": int(datetime.fromisoformat(normalized_start).timestamp() * 1000),
            "end_time": int(datetime.fromisoformat(normalized_end).timestamp() * 1000),
        }
        payload = self._request_json(url, params=params)
        if payload is None:
            return pd.DataFrame()
        data = payload.get("data", {})
        if not data:
            return pd.DataFrame()

        frame = pd.DataFrame(data)
        if frame.empty:
            return pd.DataFrame()
        frame["symbol"] = symbol
        if "trade_date" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        elif "timestamp" in frame.columns:
            frame["trade_date"] = pd.to_datetime(frame["timestamp"], unit="ms", errors="coerce")
        else:
            return pd.DataFrame()
        return frame

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        timeout: int = 30,
    ) -> dict[str, object] | None:
        for attempt in range(3):
            try:
                response = self._session.get(url, headers=self._headers, params=params, timeout=timeout)
                if response.status_code == 429:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
                return None
            except (requests.RequestException, ValueError):
                if attempt >= 2:
                    return None
                time.sleep(1.0 * (attempt + 1))
        return None
