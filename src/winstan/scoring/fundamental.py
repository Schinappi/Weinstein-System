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


def _fetch_northbound_candidates(pro: object, scored: pd.DataFrame) -> pd.DataFrame:
    """Fetch northbound data per-stock for candidate symbols only.

    ``hk_hold`` does NOT support batch queries for A-shares (returns
    only HK stocks when queried without ``ts_code``).  We fetch per
    candidate symbol instead — at most ~30 API calls, not 5000+.
    """
    candidates = scored[scored["stage2_candidate"] == True]["symbol"].dropna().unique().tolist()
    if not candidates:
        return pd.DataFrame(columns=["symbol", "nb_ratio", "nb_consecutive_increases", "nb_score"])

    result_rows = []
    for sym in candidates:
        try:
            df = pro.hk_hold(ts_code=sym, limit=5)
            if df is None or df.empty:
                continue
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            ratios = [_to_float(r) for r in df["ratio"] if _to_float(r) is not None]
            if not ratios:
                continue
            nb_ratio = ratios[0]
            consec = 0
            for i in range(len(ratios) - 1):
                if ratios[i] > ratios[i + 1]:
                    consec += 1
                else:
                    break
            result_rows.append({
                "symbol": sym,
                "nb_ratio": nb_ratio,
                "nb_consecutive_increases": consec,
                "nb_score": compute_northbound_score(nb_ratio, consec),
            })
        except Exception:
            continue

    print(f"  → northbound: {len(result_rows)}/{len(candidates)} candidates have data")
    return pd.DataFrame(result_rows) if result_rows else pd.DataFrame(
        columns=["symbol", "nb_ratio", "nb_consecutive_increases", "nb_score"]
    )


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


def compute_northbound_score(nb_ratio: float | None, consecutive_increases: int) -> float:
    """Score northbound holding trend.

    Rules (monthly data):
      1 consecutive increase  → +5
      2 consecutive increases → +10
      3+ consecutive increase → +15
    """
    if nb_ratio is None or nb_ratio <= 0:
        return 0.0
    if consecutive_increases >= 3:
        return 15.0
    if consecutive_increases >= 2:
        return 10.0
    if consecutive_increases >= 1:
        return 5.0
    return 0.0


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
    """Read northbound data from DuckDB, compute consecutive increases and score."""
    df = store.get_latest_northbound_data()
    if df.empty:
        return pd.DataFrame(columns=["symbol", "nb_ratio", "nb_consecutive_increases", "nb_score"])

    latest = df[df["rn"] == 1][["ts_code", "ratio"]].rename(columns={"ratio": "nb_ratio"})
    prev = df[df["rn"] == 2][["ts_code", "ratio"]].rename(columns={"ratio": "prev_ratio"})
    merged = latest.merge(prev, on="ts_code", how="left")

    # Count consecutive increases
    merged["nb_consecutive_increases"] = merged.apply(
        lambda r: 0
        if r["prev_ratio"] is None or pd.isna(r["prev_ratio"])
        else (1 if r["nb_ratio"] > r["prev_ratio"] else 0),
        axis=1,
    )
    merged["nb_score"] = merged.apply(
        lambda r: compute_northbound_score(r["nb_ratio"], r["nb_consecutive_increases"]),
        axis=1,
    )
    merged = merged.rename(columns={"ts_code": "symbol"})
    return merged[["symbol", "nb_ratio", "nb_consecutive_increases", "nb_score"]]


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
      nb_ratio, nb_consecutive_increases, nb_score,
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

    # ── 1. Batch fetch holder + moneyflow; per-stock northbound ──
    print("[fundamental] Batch fetching holder data...")
    holder_df = batch_fetch_holder(pro)
    if not holder_df.empty:
        store.write_fundamental_table("holder", holder_df)

    print("[fundamental] Batch fetching moneyflow data...")
    mf_df = batch_fetch_moneyflow(pro)
    if not mf_df.empty:
        store.write_fundamental_table("moneyflow", mf_df)

    # ── 2. Compute scores ──
    holder_scores = _compute_holder_scores(store)
    mf_scores = _compute_moneyflow_scores(store)

    # Northbound: per-stock for candidate symbols (hk_hold doesn't support batch)
    nb_scores = _fetch_northbound_candidates(pro, scored)

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
        scored["nb_consecutive_increases"] = 0
        scored["nb_score"] = 0.0

    # Moneyflow
    if not mf_scores.empty:
        scored = scored.merge(mf_scores, on="symbol", how="left")
    else:
        scored["net_mf_amount"] = None
        scored["moneyflow_confirm"] = 0.0

    # Fill NaN for stocks that had no match in batch data
    for col in ["holder_score", "nb_score", "moneyflow_confirm"]:
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
    for col in ["holder_score", "nb_score", "moneyflow_confirm"]:
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
        "nb_consecutive_increases": 0,
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

    # Northbound — hk_hold doesn't support batch, fetch per-stock on demand
    try:
        from winstan.adapters.tushare_client import build_tushare_pro
        _, nb_pro = build_tushare_pro()
        nb_df = nb_pro.hk_hold(ts_code=symbol, limit=5)
        if nb_df is not None and not nb_df.empty:
            nb_df = nb_df.copy()
            nb_df["trade_date"] = pd.to_datetime(nb_df["trade_date"], errors="coerce")
            nb_df = nb_df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            ratios = [_to_float(r) for r in nb_df["ratio"] if _to_float(r) is not None]
            if ratios:
                result["nb_ratio"] = ratios[0]
                consec = 0
                for i in range(len(ratios) - 1):
                    if ratios[i] > ratios[i + 1]:
                        consec += 1
                    else:
                        break
                result["nb_consecutive_increases"] = consec
                result["nb_score"] = compute_northbound_score(ratios[0], consec)
    except Exception:
        pass

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
