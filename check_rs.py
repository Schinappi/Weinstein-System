import sys; sys.path.insert(0,'src')
from winstan.storage.duckdb_store import DuckDBStore
from winstan.config import load_config
cfg = load_config('config/strategy.yaml')
store = DuckDBStore(cfg.duckdb_path)
with store.connect() as conn:
    df = conn.execute('SELECT symbol, rs_rank_pct, rs_composite, rs_line FROM screening_results LIMIT 20').fetchdf()
print('rs_rank_pct sample:')
print(df.to_string())
print()
print(f'rs_rank_pct not null: {df["rs_rank_pct"].notna().sum()}/{len(df)}')
print(f'rs_rank_pct values >0: {(df["rs_rank_pct"] > 0).sum() if df["rs_rank_pct"].notna().any() else 0}')
