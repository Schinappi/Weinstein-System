"""Verify tickflow date shift pattern - v2."""
import os
import pandas as pd
import numpy as np

parquet_dir = "data/parquet/daily_bars"

# Check 600519.SH in detail
sym = "600519.SH"
df = pd.read_parquet(f"{parquet_dir}/{sym}.parquet")
tickflow = df[df["source"] == "tickflow"].copy()
tushare = df[df["source"] == "tushare"].copy()

# Compare shifted tickflow vs tushare
tickflow["shifted_date"] = pd.to_datetime(tickflow["trade_date"]) + pd.Timedelta(days=1)

# Merge shifted tickflow with tushare dates
merged = pd.merge(
    tickflow[["shifted_date", "close", "volume"]].rename(columns={"close": "tf_close", "volume": "tf_vol", "shifted_date": "trade_date"}),
    tushare[["trade_date", "close", "volume"]].rename(columns={"close": "ts_close", "volume": "ts_vol"}),
    on="trade_date",
    how="inner"
)
matching = np.isclose(merged["tf_close"], merged["ts_close"], rtol=0.001)
print(f"{sym}: shifted+1 matching = {matching.sum()}/{len(merged)} ({100*matching.sum()/len(merged):.1f}%)")
if len(merged) > 0:
    print(merged.head(10).to_string())

# Also check: how many dates have both tickflow (original) AND tushare?
tushare_dates = set(tushare["trade_date"].astype(str))
tickflow_dates = set(tickflow["trade_date"].astype(str))
overlap = tushare_dates & tickflow_dates
print(f"Overlap (same date, both sources): {sorted(overlap)}")

# Check shifted tickflow dates overlap with tushare
shifted_tf_dates = set(tickflow["shifted_date"])
shifted_overlap = tushare_dates & shifted_tf_dates
print(f"Overlap (shifted+1): {sorted(shifted_overlap)}")

# Broad sample check
print("\n=== Broad sample across 200 stocks ===")
results = []
for fname in sorted(os.listdir(parquet_dir))[:200]:
    sym = fname.replace(".parquet", "")
    try:
        df = pd.read_parquet(f"{parquet_dir}/{fname}")
        if "source" not in df.columns:
            continue
        tushare = df[df["source"] == "tushare"].copy()
        tickflow = df[df["source"] == "tickflow"].copy()
        if tickflow.empty or tushare.empty:
            continue
        
        # Shift tickflow +1
        tickflow["shifted_date"] = pd.to_datetime(tickflow["trade_date"]) + pd.Timedelta(days=1)
        
        merged = pd.merge(
            tickflow[["shifted_date", "close"]].rename(columns={"close": "tf_close", "shifted_date": "trade_date"}),
            tushare[["trade_date", "close"]].rename(columns={"close": "ts_close"}),
            on="trade_date",
            how="inner"
        )
        if len(merged) > 0:
            matching = np.isclose(merged["tf_close"], merged["ts_close"], rtol=0.001)
            match_pct = 100 * matching.sum() / len(merged)
            results.append((sym, len(merged), matching.sum(), match_pct))
    except:
        pass

if results:
    results.sort(key=lambda x: x[3])
    print(f"Checked {len(results)} stocks with tickflow+tushare overlap")
    print(f"Match rate range: {min(r[3] for r in results):.1f}% ~ {max(r[3] for r in results):.1f}%")
    print(f"Lowest 5: {results[:5]}")
    print(f"Highest 5: {results[-5:]}")
    
    avg_match = np.mean([r[3] for r in results])
    print(f"Average match rate (shift+1): {avg_match:.1f}%")
    
    below90 = sum(1 for r in results if r[3] < 90)
    print(f"Stocks with < 90% match: {below90}/{len(results)}")
