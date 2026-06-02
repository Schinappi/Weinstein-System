"""Check stock count discrepancy."""
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, "src")
from winstan.config import load_config
from winstan.adapters.factory import DataSourceRouter
from winstan.adapters.tushare_client import build_tushare_pro
from winstan.pipeline.universe import build_universe

config = load_config(Path("config/strategy.yaml"))
_, pro = build_tushare_pro(config.data.tushare_token)

# 1. Count parquet files
import os
parquet_files = [f.replace(".parquet", "") for f in os.listdir("data/parquet/daily_bars") if f.endswith(".parquet")]
print(f"parquet 文件数: {len(parquet_files)}")

# 2. Tushare universe (how many stocks does Tushare know about?)
print("\n=== 获取 Tushare 全量股票列表 ===")
# Get all listed stocks
df_all = pro.stock_basic(exchange='', list_status='L', fields='ts_code,symbol,name,area,industry,list_date')
print(f"Tushare 正常上市股票: {len(df_all)}")

df_all_delisted = pro.stock_basic(exchange='', list_status='D', fields='ts_code,symbol,name,area,industry,list_date')
print(f"Tushare 已退市股票: {len(df_all_delisted)}")

# Check the actual universe built by the system
print("\n=== 系统构建的 Universe ===")
router = DataSourceRouter(config)
raw_universe = router.fetch_stock_universe()
print(f"router.fetch_stock_universe() 返回: {len(raw_universe)}")

universe = build_universe(raw_universe, config)
print(f"build_universe() 后: {len(universe)}")
print(f"universe symbols 样例: {universe['symbol'].head(10).tolist()}")
print(f"symbol 去重: {universe['symbol'].nunique()}")

# 3. Compare parquet vs universe
parquet_set = set(parquet_files)
universe_set = set(universe['symbol'].tolist())

in_parquet_not_universe = parquet_set - universe_set
in_universe_not_parquet = universe_set - parquet_set

print(f"\n=== 差异分析 ===")
print(f"parquet 有但 universe 没有: {len(in_parquet_not_universe)}")
if in_parquet_not_universe:
    print(f"  样例 (前20): {sorted(in_parquet_not_universe)[:20]}")
    # Check why - are they delisted?
    sample_symbols = sorted(in_parquet_not_universe)[:10]
    for sym in sample_symbols:
        try:
            # Tushare format: remove .SH/.SZ suffix
            ts_code = sym
            info = pro.stock_basic(ts_code=ts_code, fields='ts_code,name,list_status,list_date,delist_date')
            if not info.empty:
                row = info.iloc[0]
                print(f"  {sym}: list_status={row['list_status']}, list_date={row['list_date']}, delist_date={row.get('delist_date','N/A')}")
        except:
            pass

print(f"\nuniverse 有但 parquet 没有: {len(in_universe_not_parquet)}")
if in_universe_not_parquet:
    print(f"  样例: {sorted(in_universe_not_parquet)[:10]}")
