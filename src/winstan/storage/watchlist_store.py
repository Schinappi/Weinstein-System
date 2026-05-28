from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from winstan.storage.duckdb_store import DuckDBStore


WATCHLIST_COLUMNS = [
    "id",
    "symbol",
    "name",
    "source_trade_date",
    "watch_date",
    "expire_date",
    "status",
    "watch_source",
    "watch_window_days",
    "target_entry_price",
    "breakout_level",
    "stop_loss_reference",
    "volume_confirmation_needed",
    "volume_ratio_at_signal",
    "volume_label",
    "volume_ok_at_signal",
    "stage_label",
    "watch_rank_label",
    "stage2_score",
    "final_score",
    "structure_score",
    "timing_score",
    "strength_score",
    "risk_score",
    "rs_rank_pct",
    "headroom_pct",
    "market_ok",
    "breakout_status",
    "latest_trade_date",
    "latest_close",
    "distance_to_entry_pct",
    "days_waited",
    "trigger_date",
    "trigger_price_observed",
    "volume_confirmed_on_trigger",
    "created_at",
    "updated_at",
]

HOLDING_COLUMNS = [
    "id",
    "watchlist_id",
    "symbol",
    "name",
    "from_watch_date",
    "trigger_date",
    "entry_date",
    "entry_price",
    "entry_mode",
    "latest_trade_date",
    "latest_close",
    "holding_days",
    "current_return_pct",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "mfe_pct",
    "mae_pct",
    "stage_label_latest",
    "watch_rank_latest",
    "volume_confirmed_on_trigger",
    "breakout_level",
    "stop_loss_reference",
    "risk_flag",
    "status",
    "close_date",
    "close_reason",
    "created_at",
    "updated_at",
]


class WatchlistStore:
    def __init__(self, path: Path) -> None:
        self.duckdb_store = DuckDBStore(path)
        self.ensure_tables()

    def ensure_tables(self) -> None:
        with self.duckdb_store.connect() as conn:
            existing = set(conn.execute("SHOW TABLES").fetchdf()["name"].astype(str).tolist())
        if "stage2_watchlist" not in existing:
            self._write_table("stage2_watchlist", self._empty_watchlist_frame())
        if "stage2_holdings" not in existing:
            self._write_table("stage2_holdings", self._empty_holdings_frame())

    def list_watchlist(self, statuses: list[str] | None = None) -> pd.DataFrame:
        frame = self._read_table("stage2_watchlist", self._empty_watchlist_frame())
        if statuses:
            frame = frame[frame["status"].astype(str).isin(statuses)].copy()
        return frame.reset_index(drop=True)

    def list_holdings(self, statuses: list[str] | None = None) -> pd.DataFrame:
        frame = self._read_table("stage2_holdings", self._empty_holdings_frame())
        if statuses:
            frame = frame[frame["status"].astype(str).isin(statuses)].copy()
        return frame.reset_index(drop=True)

    def list_active_symbols(self) -> set[str]:
        watchlist = self.list_watchlist(["watching", "triggered"])
        holdings = self.list_holdings(["holding"])
        symbols = set(watchlist["symbol"].dropna().astype(str).str.upper())
        symbols.update(holdings["symbol"].dropna().astype(str).str.upper())
        return symbols

    def add_watch_item(self, item: dict[str, object]) -> dict[str, object]:
        frame = self.list_watchlist()
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            active = frame[
                frame["symbol"].astype(str).str.upper().eq(symbol)
                & frame["status"].astype(str).isin(["watching", "triggered"])
            ]
            if not active.empty:
                existing = active.iloc[0].to_dict()
                existing.update({key: value for key, value in item.items() if key not in {"id", "created_at"}})
                self.update_watch_item(str(existing["id"]), existing)
                return existing

        payload = self._normalize_watch_item(item)
        frame = pd.concat([frame, pd.DataFrame([payload])], ignore_index=True)
        self._write_table("stage2_watchlist", frame)
        return payload

    def update_watch_item(self, watch_id: str, payload: dict[str, object]) -> dict[str, object] | None:
        frame = self.list_watchlist()
        mask = frame["id"].astype(str) == str(watch_id)
        if not mask.any():
            return None
        existing = frame.loc[mask].iloc[0].to_dict()
        existing.update({key: value for key, value in payload.items() if key in WATCHLIST_COLUMNS and key != "created_at"})
        existing["updated_at"] = _now_iso()
        normalized = self._normalize_watch_item(existing, preserve_identity=True)
        for column in WATCHLIST_COLUMNS:
            frame.loc[mask, column] = normalized.get(column)
        self._write_table("stage2_watchlist", frame)
        return normalized

    def add_holding_item(self, item: dict[str, object]) -> dict[str, object]:
        frame = self.list_holdings()
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            active = frame[
                frame["symbol"].astype(str).str.upper().eq(symbol)
                & frame["status"].astype(str).eq("holding")
            ]
            if not active.empty:
                existing = active.iloc[0].to_dict()
                existing.update({key: value for key, value in item.items() if key not in {"id", "created_at"}})
                self.update_holding_item(str(existing["id"]), existing)
                return existing

        payload = self._normalize_holding_item(item)
        frame = pd.concat([frame, pd.DataFrame([payload])], ignore_index=True)
        self._write_table("stage2_holdings", frame)
        return payload

    def update_holding_item(self, holding_id: str, payload: dict[str, object]) -> dict[str, object] | None:
        frame = self.list_holdings()
        mask = frame["id"].astype(str) == str(holding_id)
        if not mask.any():
            return None
        existing = frame.loc[mask].iloc[0].to_dict()
        existing.update({key: value for key, value in payload.items() if key in HOLDING_COLUMNS and key != "created_at"})
        existing["updated_at"] = _now_iso()
        normalized = self._normalize_holding_item(existing, preserve_identity=True)
        for column in HOLDING_COLUMNS:
            frame.loc[mask, column] = normalized.get(column)
        self._write_table("stage2_holdings", frame)
        return normalized

    def _read_table(self, table_name: str, empty_frame: pd.DataFrame) -> pd.DataFrame:
        try:
            with self.duckdb_store.connect() as conn:
                frame = conn.execute(f"SELECT * FROM {table_name}").fetchdf()
        except Exception:
            return empty_frame.copy()
        for column in empty_frame.columns:
            if column not in frame.columns:
                frame[column] = None
        return frame[empty_frame.columns].copy()

    def _write_table(self, table_name: str, frame: pd.DataFrame) -> None:
        expected = self._empty_watchlist_frame() if table_name == "stage2_watchlist" else self._empty_holdings_frame()
        working = expected.copy() if frame.empty else frame.copy()
        for column in expected.columns:
            if column not in working.columns:
                working[column] = None
        working = working[expected.columns].reset_index(drop=True)
        self.duckdb_store.write_results(table_name, working)

    def _normalize_watch_item(self, item: dict[str, object], preserve_identity: bool = False) -> dict[str, object]:
        now = _now_iso()
        payload = {column: item.get(column) for column in WATCHLIST_COLUMNS}
        payload["id"] = str(item.get("id") or (item.get("id") if preserve_identity else uuid4()))
        payload["symbol"] = str(item.get("symbol") or "").upper()
        payload["name"] = str(item.get("name") or "")
        payload["status"] = str(item.get("status") or "watching")
        payload["watch_source"] = str(item.get("watch_source") or "stage2_auto")
        payload["watch_window_days"] = int(item.get("watch_window_days") or 3)
        payload["created_at"] = str(item.get("created_at") or now)
        payload["updated_at"] = str(item.get("updated_at") or now)
        return payload

    def _normalize_holding_item(self, item: dict[str, object], preserve_identity: bool = False) -> dict[str, object]:
        now = _now_iso()
        payload = {column: item.get(column) for column in HOLDING_COLUMNS}
        payload["id"] = str(item.get("id") or (item.get("id") if preserve_identity else uuid4()))
        payload["watchlist_id"] = str(item.get("watchlist_id") or "")
        payload["symbol"] = str(item.get("symbol") or "").upper()
        payload["name"] = str(item.get("name") or "")
        payload["entry_mode"] = str(item.get("entry_mode") or "target_price")
        payload["status"] = str(item.get("status") or "holding")
        payload["created_at"] = str(item.get("created_at") or now)
        payload["updated_at"] = str(item.get("updated_at") or now)
        return payload

    @staticmethod
    def _empty_watchlist_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    @staticmethod
    def _empty_holdings_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=HOLDING_COLUMNS)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
