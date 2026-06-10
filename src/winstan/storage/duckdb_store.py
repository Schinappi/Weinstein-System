from __future__ import annotations

from datetime import date
from pathlib import Path
from glob import glob

import duckdb
import pandas as pd


SNAPSHOT_TABLE = "screening_history"


class DuckDBStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

    def refresh_parquet_view(self, view_name: str, parquet_glob: str) -> None:
        if not glob(parquet_glob):
            return
        escaped_glob = parquet_glob.replace("'", "''\"")
        with self.connect() as conn:
            conn.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet('{escaped_glob}', union_by_name=true);"
            )

    def write_results(self, table_name: str, frame: pd.DataFrame) -> None:
        with self.connect() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.register("results_frame", frame)
            conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM results_frame")

    def append_snapshot(self, frame: pd.DataFrame, snapshot_date: date | None = None) -> None:
        """Append a daily snapshot of screening results with a snapshot_date column."""
        snap = frame.copy()
        snap["_snapshot_date"] = (snapshot_date or date.today()).isoformat()
        with self.connect() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} AS "
                f"SELECT * FROM snap WHERE FALSE"
            )
            conn.register("snap_frame", snap)
            cols = ", ".join(f'"{c}"' for c in snap.columns)
            conn.execute(f"INSERT INTO {SNAPSHOT_TABLE} ({cols}) SELECT * FROM snap_frame")

    def read_snapshot(self, snapshot_date: str) -> pd.DataFrame:
        """Read screening results for a given snapshot date (ISO format)."""
        with self.connect() as conn:
            try:
                return conn.execute(
                    f"SELECT * FROM {SNAPSHOT_TABLE} WHERE _snapshot_date = ?",
                    [snapshot_date],
                ).fetchdf()
            except Exception:
                return pd.DataFrame()

    def list_snapshot_dates(self) -> list[str]:
        """Return sorted list of available snapshot dates (most recent first)."""
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT _snapshot_date FROM {SNAPSHOT_TABLE} ORDER BY _snapshot_date DESC"
                ).fetchall()
                return [str(r[0]) for r in rows]
            except Exception:
                return []
