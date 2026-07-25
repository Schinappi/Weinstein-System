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
        """Append a daily snapshot of screening results with a snapshot_date column.
        Automatically adds missing columns to the target table to handle schema evolution.
        """
        snap = frame.copy()
        snapshot_value = (snapshot_date or date.today()).isoformat()
        snap["_snapshot_date"] = snapshot_value
        with self.connect() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {SNAPSHOT_TABLE} AS "
                f"SELECT * FROM snap WHERE FALSE"
            )
            # Auto-add any columns that exist in the DataFrame but not in the table
            table_cols = set(
                row[0] for row in conn.execute(
                    f"SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name='{SNAPSHOT_TABLE}'"
                ).fetchall()
            )
            for col in snap.columns:
                if col not in table_cols:
                    dtype = "VARCHAR"
                    if snap[col].dtype in ("int64", "Int64", "int32"):
                        dtype = "BIGINT"
                    elif snap[col].dtype in ("float64", "Float64", "float32"):
                        dtype = "DOUBLE"
                    elif snap[col].dtype == "bool":
                        dtype = "BOOLEAN"
                    conn.execute(f'ALTER TABLE {SNAPSHOT_TABLE} ADD COLUMN "{col}" {dtype}')
                    print(f"[duckdb] Added column to {SNAPSHOT_TABLE}: {col} ({dtype})")
            conn.register("snap_frame", snap)
            cols = ", ".join(f'"{c}"' for c in snap.columns)
            conn.execute(f"DELETE FROM {SNAPSHOT_TABLE} WHERE _snapshot_date = ?", [snapshot_value])
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

    # ── Fundamental Data Tables ─────────────────────────────────────

    FUNDAMENTAL_TABLES = {
        "holder": "fundamental_holder",
        "northbound": "fundamental_northbound",
        "moneyflow": "fundamental_moneyflow",
    }

    def _ensure_fundamental_tables(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fundamental_holder (
                ts_code VARCHAR,
                end_date VARCHAR,
                holder_num DOUBLE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fundamental_northbound (
                ts_code VARCHAR,
                trade_date VARCHAR,
                exchange VARCHAR,
                vol DOUBLE,
                ratio DOUBLE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fundamental_moneyflow (
                ts_code VARCHAR,
                trade_date VARCHAR,
                buy_lg_amount DOUBLE,
                sell_lg_amount DOUBLE,
                buy_elg_amount DOUBLE,
                sell_elg_amount DOUBLE,
                net_mf_vol DOUBLE,
                net_mf_amount DOUBLE
            )
        """)

    def write_fundamental_table(self, table_key: str, frame: pd.DataFrame) -> None:
        """Replace all data in a fundamental table with new batch data."""
        table_name = self.FUNDAMENTAL_TABLES.get(table_key)
        if not table_name:
            raise ValueError(f"Unknown fundamental table key: {table_key}, use: {list(self.FUNDAMENTAL_TABLES.keys())}")
        with self.connect() as conn:
            self._ensure_fundamental_tables(conn)
            conn.execute(f"DELETE FROM {table_name}")
            conn.register("fund_frame", frame)
            cols = ", ".join(f'"{c}"' for c in frame.columns)
            conn.execute(f"INSERT INTO {table_name} ({cols}) SELECT * FROM fund_frame")

    def append_fundamental_table(self, table_key: str, frame: pd.DataFrame) -> None:
        """Append data, replacing rows with duplicate (ts_code, date) keys.

        Used for northbound data so we accumulate multiple months of
        holdings (needed to compute consecutive increases).
        """
        table_name = self.FUNDAMENTAL_TABLES.get(table_key)
        if not table_name:
            return
        with self.connect() as conn:
            self._ensure_fundamental_tables(conn)
            # Delete existing rows for the same (ts_code, trade_date) combo
            date_col = "trade_date"
            if table_key == "holder":
                date_col = "end_date"
            dates = frame[date_col].dropna().unique().tolist()
            for d in dates:
                conn.execute(f"DELETE FROM {table_name} WHERE \"{date_col}\" = ?", [d])
            conn.register("fund_frame", frame)
            cols = ", ".join(f'"{c}"' for c in frame.columns)
            conn.execute(f"INSERT INTO {table_name} ({cols}) SELECT * FROM fund_frame")

    def read_fundamental_table(self, table_key: str) -> pd.DataFrame:
        """Read all data from a fundamental table."""
        table_name = self.FUNDAMENTAL_TABLES.get(table_key)
        if not table_name:
            return pd.DataFrame()
        with self.connect() as conn:
            try:
                return conn.execute(f"SELECT * FROM {table_name}").fetchdf()
            except Exception:
                return pd.DataFrame()

    def get_latest_holder_data(self) -> pd.DataFrame:
        """Get the latest 2 quarters of holder data for all symbols."""
        with self.connect() as conn:
            self._ensure_fundamental_tables(conn)
            return conn.execute("""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY end_date DESC) AS rn
                    FROM fundamental_holder
                )
                SELECT ts_code, end_date, holder_num, rn
                FROM ranked
                WHERE rn <= 2
                ORDER BY ts_code, rn
            """).fetchdf()

    def get_latest_northbound_data(self) -> pd.DataFrame:
        """Get latest northbound data for all symbols (most recent trade_date per symbol)."""
        with self.connect() as conn:
            self._ensure_fundamental_tables(conn)
            return conn.execute("""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                    FROM fundamental_northbound
                )
                SELECT ts_code, trade_date, ratio, exchange, rn
                FROM ranked
                WHERE rn <= 2
                ORDER BY ts_code, rn
            """).fetchdf()

    def get_latest_moneyflow_data(self, lookback_days: int = 5) -> pd.DataFrame:
        """Get recent moneyflow data (up to lookback_days per symbol)."""
        with self.connect() as conn:
            self._ensure_fundamental_tables(conn)
            return conn.execute("""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn
                    FROM fundamental_moneyflow
                )
                SELECT ts_code, trade_date, buy_lg_amount, sell_lg_amount,
                       buy_elg_amount, sell_elg_amount, net_mf_amount, rn
                FROM ranked
                WHERE rn <= ?
                ORDER BY ts_code, rn
            """, [lookback_days]).fetchdf()
