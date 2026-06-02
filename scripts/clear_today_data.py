"""Remove 2026-05-29 rows from all daily_bars and index_bars parquet files."""
import os
import pandas as pd

TODAY = "2026-05-29"
TARGETS = [
    ("data/parquet/daily_bars", "daily_bars"),
    ("data/parquet/index_bars", "index_bars"),
]

for parquet_dir, label in TARGETS:
    if not os.path.isdir(parquet_dir):
        print(f"[{label}] Directory not found: {parquet_dir}")
        continue
    
    files = sorted(os.listdir(parquet_dir))
    total = len(files)
    modified = 0
    removed = 0
    errors = 0

    for fname in files:
        if not fname.endswith(".parquet"):
            continue
        fpath = os.path.join(parquet_dir, fname)
        try:
            df = pd.read_parquet(fpath)
            before = len(df)
            df = df[df["trade_date"] != TODAY]
            after = len(df)
            if after < before:
                df.to_parquet(fpath, index=False)
                modified += 1
                removed += before - after
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  Error {fname}: {e}")

    print(f"[{label}] Total: {total} files, modified: {modified}, rows removed: {removed}, errors: {errors}")

print("Done!")
