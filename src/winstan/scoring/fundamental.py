"""
Supplementary fundamental data fetching and scoring for Weinstein screening.

Uses batch Tushare API queries (3 calls for ALL A-shares) instead of
per-stock calls.  Raw data is cached in DuckDB for fast dashboard reads.

APIs used (all require 5000+ Tushare积分):
  - stk_holdernumber  → holder_score  (batch by quarter-end date)
  - hk_hold            → nb_score      (batch by month-end trade_date)
  - moneyflow          → moneyflow_confirm (batch by trade_date)
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from winstan.adapters.tushare_client import build_tushare_pro
from winstan.config import load_config
from winstan.storage.duckdb_store import DuckDBStore


# ── helpers ──────────────────────────────────────────────────────────


def _to_float(v) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════
#  Batch fetch functions  (1 API call each → ALL A-shares)
# ══════════════════════════════════════════════════════════════════════


def batch_fetch_holder(pro: object) -> pd.DataFrame:
    """Fetch shareholder counts for ALL A-shares (latest 2 quarters).

    1 API call per quarter → ~5500 rows each.  Stores in DuckDB table ``fundamental_holder``.
    """
    frames = []
    for end_date in ["20260331", "20251231", "20250930", "20250630", "20250331"]:
        if len(frames) >= 2:
            break
        try:
            df = pro.stk_holdernumber(end_date=end_date)
            if df is not None and not df.empty:
                result = df[["ts_code", "end_date", "holder_num"]].copy()
                print(f"[fundamental/batch] holder: {len(result)} rows @ {end_date}")
                frames.append(result)
        except Exception:
            continue
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        print(f"[fundamental/batch] holder total: {len(combined)} rows ({len(frames)} quarters)")
        return combined
    print("[fundamental/batch] holder: no data found")
    return pd.DataFrame(columns=["ts_code", "end_date", "holder_num"])


def batch_fetch_northbound(pro: object) -> pd.DataFrame:
    """Fetch northbound A-share holdings (latest 2 quarter-end dates).

    Tushare ``hk_hold`` only provides A-share northbound data at
    quarter-end dates (20260331, 20251231, etc).  Fetches 2 most
    recent quarters so we can compute quarter-over-quarter vol change.

    Returns combined DataFrame with ~8000 rows (2 dates x ~4000 stocks).
    """
    # Try recent quarter-end dates
    candidate_dates = ["20260331", "20251231", "20250930", "20250630"]
    found_dates = []

    for td in candidate_dates:
        if len(found_dates) >= 2:
            break
        for exchange in ["SH"]:
            try:
                df = pro.hk_hold(trade_date=td, exchange=exchange)
                if df is not None and not df.empty:
                    found_dates.append(td)
                    break
            except Exception:
                continue

    if len(found_dates) < 2:
        print(f"[fundamental/batch] northbound: only found {len(found_dates)} dates, need 2")
        return pd.DataFrame(columns=["ts_code", "trade_date", "exchange", "vol", "ratio"])

    print(f"[fundamental/batch] northbound dates: {found_dates}")

    frames = []
    for td in found_dates:
        for exchange in ["SH", "SZ"]:
            try:
                df = pro.hk_hold(trade_date=td, exchange=exchange)
                if df is not None and not df.empty:
                    result = df[["ts_code", "trade_date", "exchange", "vol", "ratio"]].copy()
                    frames.append(result)
                    print(f"[fundamental/batch] northbound {exchange}@{td}: {len(result)} rows")
                else:
                    print(f"[fundamental/batch] northbound {exchange}@{td}: empty")
            except Exception as e:
                print(f"[fundamental/batch] northbound {exchange}@{td}: error={e}")

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        print(f"[fundamental/batch] northbound total: {len(combined)} rows")
        return combined
    print("[fundamental/batch] northbound: no data found")
    return pd.DataFrame(columns=["ts_code", "trade_date", "exchange", "vol", "ratio"])


def batch_fetch_moneyflow(pro: object) -> pd.DataFrame:
    """Fetch moneyflow for ALL A-shares (latest trading day).

    1 API call → ~5200 rows.  Stores in DuckDB table ``fundamental_moneyflow``.
    """
    today = date.today()
    # Try recent trading days (walk back up to 14 days)
    for offset in range(0, 14):
        td = (today - timedelta(days=offset)).strftime("%Y%m%d")
        try:
            df = pro.moneyflow(trade_date=td)
            if df is not None and not df.empty:
                cols = [
                    "ts_code", "trade_date",
                    "buy_lg_amount", "sell_lg_amount",
                    "buy_elg_amount", "sell_elg_amount",
                    "net_mf_vol", "net_mf_amount",
                ]
                result = df[[c for c in cols if c in df.columns]].copy()
                print(f"[fundamental/batch] moneyflow: {len(result)} rows @ {td}")
                return result
        except Exception:
            continue
    print("[fundamental/batch] moneyflow: no data found")
    return pd.DataFrame(columns=[
        "ts_code", "trade_date",
        "buy_lg_amount", "sell_lg_amount",
        "buy_elg_amount", "sell_elg_amount",
        "net_mf_vol", "net_mf_amount",
    ])


# ══════════════════════════════════════════════════════════════════════
#  Score computation  (unchanged — logic-only, no API calls)
# ══════════════════════════════════════════════════════════════════════


def compute_holder_score(holder_change_pct: float | None) -> float:
    """Score shareholder count reduction (concentration = bullish).

    Rules:
      <-20%  → +15   (strong concentration)
      <-10%  → +10   (moderate concentration)
      <-5%   → +5    (slight concentration)
      >+10%  → -10   (significant dispersion)
      else   → 0
    """
    if holder_change_pct is None:
        return 0.0
    if holder_change_pct < -20:
        return 15.0
    if holder_change_pct < -10:
        return 10.0
    if holder_change_pct < -5:
        return 5.0
    if holder_change_pct > 10:
        return -10.0
    return 0.0


def compute_northbound_score(
    nb_ratio: float | None,
    vol_chg_5d: float | None,
    vol_chg_10d: float | None,
    vol_chg_20d: float | None,
) -> float:
    """Score northbound institutional accumulation momentum.

    Rules (quarterly holding volume change):
      20d change > 20%  -> +20  (机构大幅加仓)
      20d change > 10%  -> +14  (机构持续加仓)
      20d change > 5%   -> +8   (机构小幅加仓)
      20d change > 0%   -> +4   (机构持平/微增)
      10d acceleration   -> +3   (近期加速买入)
      5d acceleration    -> +2   (短期加速买入)
      20d change < -10%  -> -10  (机构明显减仓)
    """
    if nb_ratio is None or nb_ratio <= 0:
        return 0.0

    score = 0.0

    if vol_chg_20d is not None:
        if vol_chg_20d > 20:
            score += 20.0
        elif vol_chg_20d > 10:
            score += 14.0
        elif vol_chg_20d > 5:
            score += 8.0
        elif vol_chg_20d > 0:
            score += 4.0
        elif vol_chg_20d < -10:
            score -= 10.0

    if vol_chg_10d is not None and vol_chg_20d is not None:
        if vol_chg_10d > 0 and vol_chg_10d * 2 > vol_chg_20d + 5:
            score += 3.0

    if vol_chg_5d is not None and vol_chg_10d is not None:
        if vol_chg_5d > 0 and vol_chg_5d * 2 > vol_chg_10d + 2:
            score += 2.0

    return score


def compute_moneyflow_confirm(
    net_mf_amount: float | None,
    consecutive_positive: int,
) -> float:
    """Score moneyflow confirmation (bonus only, not a gate).

    Rules:
      Latest day positive net flow        → +5
      Consecutive 3+ days positive        → +10 (cumulative)
      Latest day negative (< -1M)         → -5
    """
    if net_mf_amount is None:
        return 0.0
    score = 0.0
    if net_mf_amount > 0:
        score += 5.0
        if consecutive_positive >= 3:
            score += 10.0
    elif net_mf_amount < -100:  # 万元级别负向
        score -= 5.0
    return score


# ══════════════════════════════════════════════════════════════════════
#  Batch fetch + cache + merge
# ══════════════════════════════════════════════════════════════════════


def _compute_holder_scores(store: DuckDBStore) -> pd.DataFrame:
    """Read holder data from DuckDB, compute change_pct and score per symbol."""
    df = store.get_latest_holder_data()
    if df.empty:
        return pd.DataFrame(columns=["symbol", "holder_num", "holder_change_pct", "holder_score"])

    # Pivot: latest (rn=1) and previous (rn=2)
    latest = df[df["rn"] == 1][["ts_code", "holder_num"]].rename(columns={"holder_num": "curr_num"})
    prev = df[df["rn"] == 2][["ts_code", "holder_num"]].rename(columns={"holder_num": "prev_num"})
    merged = latest.merge(prev, on="ts_code", how="left")

    merged["holder_change_pct"] = merged.apply(
        lambda r: round(((r["curr_num"] - r["prev_num"]) / r["prev_num"] * 100), 2)
        if r["prev_num"] is not None and r["prev_num"] != 0 and pd.notna(r["prev_num"])
        else None,
        axis=1,
    )
    merged["holder_score"] = merged["holder_change_pct"].apply(compute_holder_score)
    merged["holder_num"] = merged["curr_num"]
    merged = merged.rename(columns={"ts_code": "symbol"})
    return merged[["symbol", "holder_num", "holder_change_pct", "holder_score"]]


def _compute_northbound_scores(store: DuckDBStore) -> pd.DataFrame:
    """Read northbound data from DuckDB, compute vol changes and scores.

    Uses 2 most recent quarter-end dates to compute vol change
    (hk_hold only provides quarterly A-share data).
    """
    df = store.read_fundamental_table("northbound")
    if df.empty:
        return pd.DataFrame(columns=["symbol", "nb_ratio",
                                      "nb_vol_chg_5d", "nb_vol_chg_10d", "nb_vol_chg_20d",
                                      "nb_score"])

    # Sum vol across SH+SZ for each stock+date
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
    df_agg = df.groupby(["ts_code", "trade_date"], as_index=False)["vol"].sum()

    # Get latest ratio per stock (from most recent date)
    latest_per_stock = df.groupby("ts_code", as_index=False).apply(
        lambda g: g.loc[g["trade_date"].idxmax()],
        include_groups=False,
    ).reset_index()
    if "ratio" not in latest_per_stock.columns:
        latest_per_stock["ratio"] = None
    ratio_map = dict(zip(latest_per_stock["ts_code"], latest_per_stock["ratio"]))

    # Pivot: latest quarter vs previous quarter
    dates = sorted(df_agg["trade_date"].unique())
    if len(dates) < 2:
        return pd.DataFrame(columns=["symbol", "nb_ratio",
                                      "nb_vol_chg_5d", "nb_vol_chg_10d", "nb_vol_chg_20d",
                                      "nb_score"])

    latest_date = max(dates)
    prev_date = dates[-2]

    latest_vol = df_agg[df_agg["trade_date"] == latest_date][["ts_code", "vol"]].rename(columns={"vol": "vol_latest"})
    prev_vol = df_agg[df_agg["trade_date"] == prev_date][["ts_code", "vol"]].rename(columns={"vol": "vol_prev"})

    results = latest_vol.merge(prev_vol, on="ts_code", how="outer")

    # Calculate quarterly vol change %; map to 5d/10d/20d (same value for all)
    def _qoq_pct(row):
        if row["vol_prev"] and row["vol_prev"] > 0 and row["vol_latest"] and row["vol_latest"] > 0:
            return round((row["vol_latest"] - row["vol_prev"]) / row["vol_prev"] * 100, 1)
        return None

    results["nb_vol_chg_5d"] = results.apply(_qoq_pct, axis=1)
    results["nb_vol_chg_10d"] = results.apply(_qoq_pct, axis=1)
    results["nb_vol_chg_20d"] = results.apply(_qoq_pct, axis=1)

    results = results.rename(columns={"ts_code": "symbol"})
    results["nb_ratio"] = results["symbol"].map(ratio_map)
    results["nb_score"] = results.apply(
        lambda r: compute_northbound_score(
            r.get("nb_ratio"), r.get("nb_vol_chg_5d"),
            r.get("nb_vol_chg_10d"), r.get("nb_vol_chg_20d"),
        ), axis=1)

    return results[["symbol", "nb_ratio",
                     "nb_vol_chg_5d", "nb_vol_chg_10d", "nb_vol_chg_20d",
                     "nb_score"]]

def _compute_moneyflow_scores(store: DuckDBStore, lookback_days: int = 5) -> pd.DataFrame:
    """Read moneyflow data from DuckDB, compute scores per symbol."""
    df = store.get_latest_moneyflow_data(lookback_days)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "net_mf_amount", "moneyflow_confirm"])

    # Compute net amount per row
    df["net_amount"] = df.apply(
        lambda r: (
            (r["buy_lg_amount"] or 0)
            + (r["buy_elg_amount"] or 0)
            - (r["sell_lg_amount"] or 0)
            - (r["sell_elg_amount"] or 0)
        ),
        axis=1,
    )

    # Latest day per symbol
    latest = df[df["rn"] == 1].copy()
    latest["net_mf_amount"] = latest["net_amount"]

    # Count consecutive positive days
    def count_consecutive_positive(grp: pd.DataFrame) -> int:
        sorted_grp = grp.sort_values("rn")
        count = 0
        for _, row in sorted_grp.iterrows():
            if row["net_amount"] > 0:
                count += 1
            else:
                break
        return count

    consec = df.groupby("ts_code").apply(count_consecutive_positive).reset_index()
    consec.columns = ["ts_code", "mf_consecutive_positive"]

    merged = latest.merge(consec, on="ts_code", how="left")
    merged["mf_consecutive_positive"] = merged["mf_consecutive_positive"].fillna(0).astype(int)
    merged["moneyflow_confirm"] = merged.apply(
        lambda r: compute_moneyflow_confirm(r["net_mf_amount"], r["mf_consecutive_positive"]),
        axis=1,
    )
    merged = merged.rename(columns={"ts_code": "symbol"})
    return merged[["symbol", "net_mf_amount", "moneyflow_confirm"]]


# ══════════════════════════════════════════════════════════════════════
#  Main entry point — called from screener.run()
# ══════════════════════════════════════════════════════════════════════


def fetch_supplemental_data(results: pd.DataFrame) -> pd.DataFrame:
    """Fetch fundamental data via batch API queries, cache in DuckDB, merge scores.

    Makes only 3 API calls total (one per dimension) to cover ALL A-shares,
    then merges scores into the screening results.

    Returns a copy of ``results`` with new columns:
      holder_num, holder_change_pct, holder_score,
      nb_ratio, nb_vol_chg_5d, nb_vol_chg_10d, nb_vol_chg_20d, nb_score,
      net_mf_amount, moneyflow_confirm
    """
    if results.empty:
        return results

    scored = results.copy()
    config = load_config(Path("config/strategy.yaml"))
    store = DuckDBStore(config.duckdb_path)

    try:
        ts, pro = build_tushare_pro()
    except Exception as exc:
        print(f"[fundamental] Tushare connection failed: {exc}")
        _fill_fundamental_defaults(scored)
        return scored

    # ── 1. Batch fetch all three dimensions ──
    print("[fundamental] Batch fetching holder data...")
    holder_df = batch_fetch_holder(pro)
    if not holder_df.empty:
        store.write_fundamental_table("holder", holder_df)

    print("[fundamental] Batch fetching northbound data...")
    nb_df = batch_fetch_northbound(pro)
    if not nb_df.empty:
        store.write_fundamental_table("northbound", nb_df)

    print("[fundamental] Batch fetching moneyflow data...")
    mf_df = batch_fetch_moneyflow(pro)
    if not mf_df.empty:
        store.write_fundamental_table("moneyflow", mf_df)

    # ── 2. Compute scores from DuckDB cache ──
    holder_scores = _compute_holder_scores(store)
    nb_scores = _compute_northbound_scores(store)
    mf_scores = _compute_moneyflow_scores(store)

    # ── 3. Merge into results ──
    # Holder
    if not holder_scores.empty:
        scored = scored.merge(holder_scores, on="symbol", how="left")
    else:
        scored["holder_num"] = None
        scored["holder_change_pct"] = None
        scored["holder_score"] = 0.0

    # Northbound
    if not nb_scores.empty:
        scored = scored.merge(nb_scores, on="symbol", how="left")
    else:
        scored["nb_ratio"] = None
        scored["nb_vol_chg_5d"] = None
        scored["nb_vol_chg_10d"] = None
        scored["nb_vol_chg_20d"] = None
        scored["nb_score"] = 0.0

    # Moneyflow
    if not mf_scores.empty:
        scored = scored.merge(mf_scores, on="symbol", how="left")
    else:
        scored["net_mf_amount"] = None
        scored["moneyflow_confirm"] = 0.0

    # Fill NaN for stocks that had no match in batch data
    for col in ["holder_score", "nb_score", "moneyflow_confirm", "nb_vol_chg_5d", "nb_vol_chg_10d", "nb_vol_chg_20d"]:
        if col in scored.columns:
            scored[col] = scored[col].fillna(0.0)

    print(f"[fundamental] Done — holder non-zero: {(scored.get('holder_score', pd.Series([0])) != 0).sum()}, "
          f"nb non-zero: {(scored.get('nb_score', pd.Series([0])) != 0).sum()}, "
          f"mf non-zero: {(scored.get('moneyflow_confirm', pd.Series([0])) != 0).sum()}")
    return scored


def _fill_fundamental_defaults(df: pd.DataFrame) -> None:
    """Fill fundamental columns with defaults when Tushare is unavailable."""
    for col in [
        "holder_num", "holder_change_pct",
        "nb_ratio", "nb_consecutive_increases",
        "net_mf_amount", "mf_consecutive_positive",
    ]:
        if col not in df.columns:
            df[col] = None
    for col in ["holder_score", "nb_score", "moneyflow_confirm", "nb_vol_chg_5d", "nb_vol_chg_10d", "nb_vol_chg_20d"]:
        if col not in df.columns:
            df[col] = 0.0


# ══════════════════════════════════════════════════════════════════════
#  Dashboard-friendly helpers  (read from DuckDB cache, no API calls)
# ══════════════════════════════════════════════════════════════════════


def get_fundamental_for_symbol(symbol: str, config_path: str = "config/strategy.yaml") -> dict:
    """Read fundamental data for a single symbol from DuckDB cache.

    Returns same dict structure as dashboard's ``_get_fundamental_data()``.
    No API calls — pure cache read.
    """
    config = load_config(Path(config_path))
    store = DuckDBStore(config.duckdb_path)

    result: dict = {
        "holder_score": None,
        "holder_change_pct": None,
        "holder_num": None,
        "nb_score": None,
        "nb_ratio": None,
        "nb_vol_chg_5d": None,
        "nb_vol_chg_10d": None,
        "nb_vol_chg_20d": None,
        "moneyflow_confirm": None,
        "net_mf_amount": None,
    }

    # Holder
    holder_df = store.get_latest_holder_data()
    if not holder_df.empty:
        sym_data = holder_df[holder_df["ts_code"] == symbol]
        if not sym_data.empty:
            curr = sym_data[sym_data["rn"] == 1]
            prev = sym_data[sym_data["rn"] == 2]
            if not curr.empty:
                curr_num = _to_float(curr.iloc[0].get("holder_num"))
                result["holder_num"] = curr_num
                if not prev.empty:
                    prev_num = _to_float(prev.iloc[0].get("holder_num"))
                    if prev_num and prev_num != 0:
                        chg = round((curr_num - prev_num) / prev_num * 100, 2) if curr_num else None
                        result["holder_change_pct"] = chg
                        result["holder_score"] = compute_holder_score(chg)
                    else:
                        result["holder_score"] = compute_holder_score(None)
                else:
                    result["holder_score"] = compute_holder_score(None)

    # Northbound — read from DuckDB batch cache
    nb_df = store.get_latest_northbound_data()
    if not nb_df.empty:
        sym_data = nb_df[nb_df["ts_code"] == symbol]
        if not sym_data.empty:
            curr = sym_data[sym_data["rn"] == 1]
            prev = sym_data[sym_data["rn"] == 2]
            if not curr.empty:
                ratio = _to_float(curr.iloc[0].get("ratio"))
                result["nb_ratio"] = ratio
                consec = 0
                if not prev.empty:
                    prev_ratio = _to_float(prev.iloc[0].get("ratio"))
                    if prev_ratio and ratio and ratio > prev_ratio:
                        consec = 1
                result["nb_consecutive_increases"] = consec
                result["nb_score"] = compute_northbound_score(ratio, None, None, None)

    # Moneyflow
    mf_df = store.get_latest_moneyflow_data(lookback_days=5)
    if not mf_df.empty:
        sym_data = mf_df[mf_df["ts_code"] == symbol]
        if not sym_data.empty:
            # Latest day
            latest = sym_data[sym_data["rn"] == 1]
            if not latest.empty:
                row = latest.iloc[0]
                buy_lg = _to_float(row.get("buy_lg_amount")) or 0
                sell_lg = _to_float(row.get("sell_lg_amount")) or 0
                buy_elg = _to_float(row.get("buy_elg_amount")) or 0
                sell_elg = _to_float(row.get("sell_elg_amount")) or 0
                net_amt = round(buy_lg + buy_elg - sell_lg - sell_elg, 2)
                result["net_mf_amount"] = net_amt

                # Count consecutive positive
                consec = 0
                sorted_sym = sym_data.sort_values("rn")
                for _, r in sorted_sym.iterrows():
                    bl = _to_float(r.get("buy_lg_amount")) or 0
                    sl = _to_float(r.get("sell_lg_amount")) or 0
                    be = _to_float(r.get("buy_elg_amount")) or 0
                    se = _to_float(r.get("sell_elg_amount")) or 0
                    na = bl + be - sl - se
                    if na > 0:
                        consec += 1
                    else:
                        break
                result["moneyflow_confirm"] = compute_moneyflow_confirm(net_amt, consec)

    return result
