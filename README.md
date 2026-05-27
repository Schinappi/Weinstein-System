# Winstan Phase 1

Phase 1 focuses on an offline A-share screener for the Weinstein strategy:

- market trend filter
- 30-week stage analysis
- volume confirmation
- relative strength
- resistance headroom
- optional breakout confirmation

Primary design goals:

- support both Tushare and TickFlow
- keep a local cache with DuckDB + Parquet
- produce candidates, Top N strong names, and a summary report

