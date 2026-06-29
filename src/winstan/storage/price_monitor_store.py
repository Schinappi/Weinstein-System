from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

from winstan.storage.duckdb_store import DuckDBStore

PRICE_MONITOR_COLUMNS = [
    "id",
    "symbol",
    "name",
    "target_price",
    "latest_trade_date",
    "latest_close",
    "distance_amount",
    "distance_pct",
    "created_at",
    "updated_at",
]


class PriceMonitorStore:
    def __init__(self, path: Path) -> None:
        self.duckdb_store = DuckDBStore(path)
        self.ensure_table()

    def ensure_table(self) -> None:
        with self.duckdb_store.connect() as conn:
            existing = set(conn.execute("SHOW TABLES").fetchdf()["name"].astype(str).tolist())
        if "price_monitors" not in existing:
            self._write_table(self._empty_frame())

    def list_items(self) -> pd.DataFrame:
        return self._read_table().reset_index(drop=True)

    def add_item(self, item: dict[str, object]) -> dict[str, object]:
        frame = self.list_items()
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            mask = frame["symbol"].astype(str).str.upper().eq(symbol)
            if mask.any():
                existing = frame.loc[mask].iloc[0].to_dict()
                existing.update({key: value for key, value in item.items() if key not in {"id", "created_at"}})
                return self.update_item(str(existing["id"]), existing) or self._normalize_item(existing, preserve_identity=True)

        payload = self._normalize_item(item)
        frame = pd.concat([frame, pd.DataFrame([payload])], ignore_index=True)
        self._write_table(frame)
        return payload

    def update_item(self, item_id: str, payload: dict[str, object]) -> dict[str, object] | None:
        frame = self.list_items()
        mask = frame["id"].astype(str) == str(item_id)
        if not mask.any():
            return None
        existing = frame.loc[mask].iloc[0].to_dict()
        existing.update({key: value for key, value in payload.items() if key in PRICE_MONITOR_COLUMNS and key != "created_at"})
        normalized = self._normalize_item(existing, preserve_identity=True)
        updated = frame.loc[~mask].copy()
        updated = pd.concat([updated, pd.DataFrame([normalized])], ignore_index=True)
        self._write_table(updated)
        return normalized

    def delete_item(self, item_id: str) -> bool:
        frame = self.list_items()
        mask = frame["id"].astype(str) == str(item_id)
        if not mask.any():
            return False
        self._write_table(frame.loc[~mask].copy())
        return True

    def _read_table(self) -> pd.DataFrame:
        try:
            with self.duckdb_store.connect() as conn:
                frame = conn.execute("SELECT * FROM price_monitors").fetchdf()
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
        self.duckdb_store.write_results("price_monitors", working[expected.columns].reset_index(drop=True))

    def _normalize_item(self, item: dict[str, object], preserve_identity: bool = False) -> dict[str, object]:
        now = _now_iso()
        payload = {column: item.get(column) for column in PRICE_MONITOR_COLUMNS}
        payload["id"] = str(item.get("id") or (item.get("id") if preserve_identity else uuid4()))
        payload["symbol"] = str(item.get("symbol") or "").upper()
        payload["name"] = str(item.get("name") or "")
        payload["created_at"] = str(item.get("created_at") or now)
        payload["updated_at"] = str(item.get("updated_at") or now)
        return payload

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=PRICE_MONITOR_COLUMNS)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
