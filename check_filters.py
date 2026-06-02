import sys; sys.path.insert(0,'src')
from winstan.storage.duckdb_store import DuckDBStore
from winstan.config import load_config
from winstan.rules.stage_analysis import apply_stage2_scoring
cfg = load_config('config/strategy.yaml')
store = DuckDBStore(cfg.duckdb_path)
with store.connect() as conn:
    df = conn.execute('SELECT * FROM screening_results').fetchdf()
weighted = apply_stage2_scoring(df, cfg)
filters = ['market_ok', 'stage2_candidate', 'volume_ok', 'rs_ok', 'breakout_ok']
for f in filters:
    print(f'{f}: {weighted[f].fillna(False).astype(bool).sum()} / {len(weighted)}')
mask = (weighted['market_ok'].fillna(False).astype(bool)
        & weighted['stage2_candidate'].fillna(False).astype(bool)
        & weighted['volume_ok'].fillna(False).astype(bool)
        & weighted['rs_ok'].fillna(False).astype(bool)
        & weighted['breakout_ok'].fillna(False).astype(bool))
print(f'\nPass ALL 5: {mask.sum()} stocks')
if mask.sum() == 0:
    weighted['pc'] = (weighted['market_ok'].fillna(False).astype(bool).astype(int)
                      + weighted['stage2_candidate'].fillna(False).astype(bool).astype(int)
                      + weighted['volume_ok'].fillna(False).astype(bool).astype(int)
                      + weighted['rs_ok'].fillna(False).astype(bool).astype(int)
                      + weighted['breakout_ok'].fillna(False).astype(bool).astype(int))
    top = weighted.sort_values(['pc','final_score'], ascending=[False,False]).head(15)
    print('Top by passes:')
    print(top[['symbol','name','pc','breakout_ok','breakout_status','final_score']].to_string())
