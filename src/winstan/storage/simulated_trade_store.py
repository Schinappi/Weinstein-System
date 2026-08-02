from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from winstan.storage.duckdb_store import DuckDBStore


SIMULATED_TRADE_COLUMNS = [
    "id",
    "monitor_id",
    "symbol",
    "name",
    "order_date",
    "order_price",
    "status",
    "entry_date",
    "entry_price",
    "entry_low",
    "stop_loss_price",
    "close_date",
    "close_price",
    "close_reason",
    "latest_trade_date",
    "latest_close",
    "current_return_pct",
    "realized_return_pct",
    "max_gain_pct",
    "max_drawdown_pct",
    "holding_days",
    "created_at",
    "updated_at",
]


class SimulatedTradeStore:
    def __init__(self, path: Path) -> None:
        self.duckdb_store = DuckDBStore(path)
        self.ensure_table()

    def ensure_table(self) -> None:
        with self.duckdb_store.connect() as conn:
            existing = set(conn.execute("SHOW TABLES").fetchdf()["name"].astype(str).tolist())
        if "simulated_trades" not in existing:
            self._write_table(self._empty_frame())

    def list_items(self, statuses: list[str] | None = None) -> pd.DataFrame:
        frame = self._read_table()
        if statuses:
            frame = frame[frame["status"].astype(str).isin(statuses)].copy()
        return frame.reset_index(drop=True)

    def upsert_for_monitor(self, monitor_id: str, payload: dict[str, object]) -> dict[str, object]:
        frame = self.list_items()
        mask = frame["monitor_id"].astype(str) == str(monitor_id)
        if mask.any():
            existing = frame.loc[mask].iloc[0].to_dict()
            existing.update({key: value for key, value in payload.items() if key in SIMULATED_TRADE_COLUMNS and key != "created_at"})
            normalized = self._normalize_item(existing, preserve_identity=True)
            updated = frame.loc[~mask].copy()
            updated = pd.concat([updated, pd.DataFrame([normalized])], ignore_index=True)
            self._write_table(updated)
            return normalized

        normalized = self._normalize_item(payload)
        frame = pd.concat([frame, pd.DataFrame([normalized])], ignore_index=True)
        self._write_table(frame)
        return normalized

    def _read_table(self) -> pd.DataFrame:
        try:
            with self.duckdb_store.connect() as conn:
                frame = conn.execute("SELECT * FROM simulated_trades").fetchdf()
        except Exception:
            return self._empty_frame()
        expected = self._empty_frame()
        for column in expected.columns:
            if column not in frame.columns:
                frame[column] = None
        return frame[expected.columns].copy()

    def _write_table(self, frame: pd.DataFrame) -> None:
        expected = self._empty_frame()
        working = expected.copy() if frame.empty else frame.copy()
        for column in expected.columns:
            if column not in working.columns:
                working[column] = None
        self.duckdb_store.write_results("simulated_trades", working[expected.columns].reset_index(drop=True))

    def _normalize_item(self, item: dict[str, object], preserve_identity: bool = False) -> dict[str, object]:
        now = _now_iso()
        payload = {column: item.get(column) for column in SIMULATED_TRADE_COLUMNS}
        payload["id"] = str(item.get("id") or (item.get("id") if preserve_identity else uuid4()))
        payload["monitor_id"] = str(item.get("monitor_id") or "")
        payload["symbol"] = str(item.get("symbol") or "").upper()
        payload["name"] = str(item.get("name") or "")
        payload["status"] = str(item.get("status") or "pending")
        payload["created_at"] = str(item.get("created_at") or now)
        payload["updated_at"] = now
        return payload

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=SIMULATED_TRADE_COLUMNS)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
