import sys; sys.path.insert(0,'src')
import pandas as pd
from winstan.storage.parquet_store import ParquetStore
from winstan.config import load_config
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.calendar.trading_calendar import clean_daily_bars
cfg = load_config('config/strategy.yaml')
store = ParquetStore(cfg.parquet_root)
# Stock daily bars
stock = clean_daily_bars(store.read_symbol_frame('daily_bars', '000001.SZ'))
stock_weekly = build_weekly_bars(stock)
print(f'Stock weekly dates: {stock_weekly["trade_date"].min()} ~ {stock_weekly["trade_date"].max()}')
print(f'Stock weekly rows: {len(stock_weekly)}')
# Index daily bars
index = clean_daily_bars(store.read_symbol_frame('index_bars', '000906.SH'))
index_weekly = build_weekly_bars(index)
print(f'Index weekly dates: {index_weekly["trade_date"].min()} ~ {index_weekly["trade_date"].max()}')
print(f'Index weekly rows: {len(index_weekly)}')
# Check merge
merged = stock_weekly.merge(index_weekly[['trade_date','close']].rename(columns={'close':'market_close'}), on='trade_date', how='left')
print(f'Merged rows: {len(merged)}')
print(f'market_close NaN: {merged["market_close"].isna().sum()}')
if merged['market_close'].isna().any():
    missing = merged[merged['market_close'].isna()][['trade_date','close']].head(10)
    print('Missing market dates (first 10):')
    print(missing.to_string())
