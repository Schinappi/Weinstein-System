from __future__ import annotations

from pathlib import Path
from glob import glob

import duckdb
import pandas as pd


class DuckDBStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

    def refresh_parquet_view(self, view_name: str, parquet_glob: str) -> None:
        if not glob(parquet_glob):
            return
        escaped_glob = parquet_glob.replace("'", "''")
        with self.connect() as conn:
            conn.execute(
                f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet('{escaped_glob}');"
            )

    def write_results(self, table_name: str, frame: pd.DataFrame) -> None:
        with self.connect() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.register("results_frame", frame)
            conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM results_frame")
