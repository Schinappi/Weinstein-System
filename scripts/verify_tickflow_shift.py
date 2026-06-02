"""Verify tickflow date shift pattern."""
import os
import pandas as pd

parquet_dir = "data/parquet/daily_bars"
files = sorted(os.listdir(parquet_dir))

# Deep check a mixed stock
sym = "600519.SH"
df = pd.read_parquet(f"{parquet_dir}/{sym}.parquet")
tickflow = df[df["source"] == "tickflow"].copy()
tushare = df[df["source"] == "tushare"].copy()

tickflow["trade_date_dt"] = pd.to_datetime(tickflow["trade_date"])
tickflow["dow"] = tickflow["trade_date_dt"].dt.dayofweek  # Mon=0, Sun=6
weekend_in_tickflow = tickflow[tickflow["dow"].isin([5, 6])]
print(f"{sym}: tickflow weekend rows = {len(weekend_in_tickflow)}")
print(f"  Weekend dates: {weekend_in_tickflow['trade_date'].tolist()[:20]}")

# Check the actual date shift
print("\nLast 10 tickflow rows:")
print(tickflow[["trade_date", "close", "volume"]].tail(10).to_string())
print("\nLast 10 tushare rows:")
print(tushare[["trade_date", "close", "volume"]].tail(10).to_string())

# Now check: if we shift tickflow dates +1 day, do they match tushare?
print("\n=== Verifying shift pattern ===")
tickflow["trade_date_shifted"] = (tickflow["trade_date_dt"] + pd.Timedelta(days=1)).dt.date.astype(str)

# Compare last 10 shifted tickflow with tushare
tickflow_shifted = tickflow.rename(columns={"trade_date_shifted": "trade_date"})
# Check if shifted tickflow 5/26 would match tushare 5/27
tickflow_526 = tickflow_shifted[tickflow_shifted["trade_date"] == "2026-05-27"]
tushare_527 = tushare[tushare["trade_date"] == "2026-05-27"]
if not tickflow_526.empty and not tushare_527.empty:
    print(f"Tickflow shifted 5/27: close={tickflow_526['close'].values[0]}, vol={tickflow_526['volume'].values[0]}")
    print(f"Tushare 5/27:          close={tushare_527['close'].values[0]}, vol={tushare_527['volume'].values[0]}")
    print(f"Close match: {abs(tickflow_526['close'].values[0] - tushare_527['close'].values[0]) < 0.01}")
else:
    print("Cannot compare - one is empty")
    print("tickflow_526:", len(tickflow_526), "tushare_527:", len(tushare_527))

# Check if original tickflow 5/26 matches tushare 5/25 (one day BEFORE)
# This would mean tickflow is 1 day AHEAD of tushare
print("\n=== Check if tickflow is 1 day AHEAD ===")
tushare_525 = tushare[tushare["trade_date"] == "2026-05-25"]
tickflow_late526 = tickflow[tickflow["trade_date"] == "2026-05-26"]
if not tushare_525.empty and not tickflow_late526.empty:
    print(f"Tushare 5/25: close={tushare_525['close'].values[0]}")
    print(f"Tickflow 5/26: close={tickflow_late526['close'].values[0]}")

# Check a few more stocks
print("\n\n=== Checking more stocks ===")
for sym in ["000333.SZ", "600036.SH", "002415.SZ", "601318.SH"]:
    try:
        df = pd.read_parquet(f"{parquet_dir}/{sym}.parquet")
        tushare = df[df["source"] == "tushare"].copy()
        tickflow = df[df["source"] == "tickflow"].copy()
        if tickflow.empty or tushare.empty:
            print(f"{sym}: skip - one empty")
            continue
        
        # Shift tickflow +1 day and compare with tushare for the overlapping period
        tushare.set_index("trade_date", inplace=True)
        tickflow["shifted_date"] = (pd.to_datetime(tickflow["trade_date"]) + pd.Timedelta(days=1)).dt.date.astype(str)
        
        match_count = 0
        total_check = 0
        for _, row in tickflow.iterrows():
            shifted_date = row["shifted_date"]
            if shifted_date in tushare.index:
                total_check += 1
                if abs(row["close"] - tushare.loc[shifted_date, "close"]) < 0.01:
                    match_count += 1
        
        tushare.reset_index(inplace=True)
        
        if total_check > 0:
            print(f"{sym}: shifted+1day match rate = {match_count}/{total_check} ({100*match_count/total_check:.1f}%)")
        else:
            print(f"{sym}: no overlapping dates with +1 shift")
    except Exception as e:
        print(f"{sym}: error {e}")

print("\n=== Also check: is tickflow's data from correct dates but with CLOSE values swapped? ===")
# Maybe tickflow dates are correct but close values look wrong because of adj_factor?
sym = "600519.SH"
df = pd.read_parquet(f"{parquet_dir}/{sym}.parquet")
tushare = df[df["source"] == "tushare"].copy()
tickflow = df[df["source"] == "tickflow"].copy()

# Get the raw unadjusted close from tickflow
tickflow_raw = tickflow.copy()
tickflow_raw["unadj_close"] = tickflow_raw["close"] * tickflow_raw["adj_factor"]

print("\nTickflow last 5 rows (with unadjusted close):")
print(tickflow_raw[["trade_date", "close", "adj_factor", "unadj_close"]].tail().to_string())
print("\nTushare last 5 rows (close is already adjusted):")
print(tushare[["trade_date", "close", "adj_factor"]].tail().to_string())

# The actual price should be close * adj_factor for tushare... or close * adj_factor for tickflow?
# For tickflow: close * 1.0 = close itself
# For tushare: close * 8.4464 = ~11000 for 茅台 - seems right as unadjusted price

# Check: if we take tickflow's unadjusted close (close * 1.0 = close itself) 
# and compare to tushare's adjusted close * adj_factor:
print("\n=== Compare actual prices ===")
print("Tushare 5/27 actual price (close*adj_factor):", 
      tushare[tushare["trade_date"]=="2026-05-27"]["close"].values[0] * 
      tushare[tushare["trade_date"]=="2026-05-27"]["adj_factor"].values[0])
print("Tickflow 5/26 actual price (close*1.0):", 
      tickflow[tickflow["trade_date"]=="2026-05-26"]["close"].values[0])
print("Tushare 5/26 actual price (close*adj_factor):", 
      tushare[tushare["trade_date"]=="2026-05-26"]["close"].values[0] * 
      tushare[tushare["trade_date"]=="2026-05-26"]["adj_factor"].values[0] if not tushare[tushare["trade_date"]=="2026-05-26"].empty else "N/A")
