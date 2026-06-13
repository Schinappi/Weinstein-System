"""
Industry Relative Strength — Shenwan Level-1 industry indices.

Fetches 28 Shenwan L1 industry index returns from sw_daily (2 API calls).
Maps to individual stocks via stock_basic.industry (same naming convention).

Pipeline:
  1. index_classify(level="L1") → 28 industry codes + names
  2. sw_daily(trade_date=latest) + sw_daily(trade_date=26w_ago)
  3. Compute 26w return per industry
  4. Rank → industry_rs_rank_pct
  5. Map stocks via stock_basic.industry → industry_breadth (% RS positive)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from winstan.config import AppConfig, load_config


def _load_industry_map(config: AppConfig) -> dict[str, str]:
    """stock ts_code/symbol → Shenwan L1 industry name.

    Uses cached stock_basic + a manual mapping from stock_basic industry
    names (110) to Shenwan L1 names (28).
    """
    import json
    mapping_path = Path(config.project_root) / "data" / "mapping" / "stock_industry_to_sw_l1.json"
    if not mapping_path.exists():
        # Fallback: try relative to CWD
        mapping_path = Path("data/mapping/stock_industry_to_sw_l1.json")
    if mapping_path.exists():
        basic_to_l1 = json.loads(mapping_path.read_text())
    else:
        print("[industry] WARNING: mapping file not found, using identity mapping")
        basic_to_l1 = {}

    cache_path = Path(config.parquet_root) / "supplement" / "industry_map.parquet"
    if cache_path.exists():
        df = pd.read_parquet(cache_path)
    else:
        from winstan.adapters.tushare_client import build_tushare_pro
        _, pro = build_tushare_pro()
        df = pro.stock_basic(fields="ts_code,symbol,name,industry,market")
        if df is not None and not df.empty:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path, index=False)
            print(f"[industry] Cached {len(df)} mappings")
    result: dict[str, str] = {}
    for _, r in df.iterrows():
        ind = str(r.get("industry") or "").strip()
        if ind:
            l1 = basic_to_l1.get(ind, ind)  # map to Shenwan L1 if available
            result[str(r["ts_code"]).strip()] = l1
            sym = str(r.get("symbol") or "").strip()
            if sym:
                result[sym] = l1
    result = {k: v for k, v in result.items() if v}
    return result


def _get_l1_industry_map(pro: object) -> dict[str, str]:
    """index_code → industry_name for Shenwan L1."""
    df = pro.index_classify(level="L1")
    return dict(zip(df["index_code"], df["industry_name"]))


def _fetch_industry_26w_returns(pro: object) -> pd.DataFrame:
    """Fetch 26-week returns for all 28 Shenwan L1 industries.

    2 API calls total. Returns DataFrame: industry, return_26w
    """
    from datetime import date, timedelta

    # Get L1 index codes
    l1 = pro.index_classify(level="L1")
    l1_codes = l1["index_code"].dropna().unique().tolist()
    name_map = dict(zip(l1["index_code"], l1["industry_name"]))

    # Find latest & past trading days
    today = date.today()

    def find_trade_date(base_offset: int, max_offset: int = 14) -> str | None:
        for off in range(max_offset):
            d = (today - timedelta(days=base_offset + off)).strftime("%Y%m%d")
            try:
                df = pro.sw_daily(trade_date=d)
                if df is not None and not df.empty:
                    return d
            except Exception:
                continue
        return None

    latest_date = find_trade_date(0)
    past_date = find_trade_date(182)

    if not latest_date or not past_date:
        print("[industry] WARNING: could not fetch sw_daily data")
        return pd.DataFrame(columns=["industry", "return_26w"])

    latest = pro.sw_daily(trade_date=latest_date)
    past = pro.sw_daily(trade_date=past_date)

    li = latest[latest["ts_code"].isin(l1_codes)][["ts_code", "close"]]
    pi = past[past["ts_code"].isin(l1_codes)][["ts_code", "close"]]
    merged = li.merge(pi, on="ts_code", how="inner", suffixes=("", "_past"))
    merged["return_26w"] = (merged["close"] / merged["close_past"] - 1.0) * 100.0
    merged["industry"] = merged["ts_code"].map(name_map)
    merged = merged.dropna(subset=["return_26w", "industry"])

    print(f"[industry] {len(merged)} Shenwan L1 industries ({latest_date} vs {past_date}):")
    for label, asc in [("Top 5", False), ("Bottom 5", True)]:
        top = merged.sort_values("return_26w", ascending=asc).head(5)
        for _, r in top.iterrows():
            print(f"  {r['industry']:16s}  {r['return_26w']:+.2f}%")

    return merged[["industry", "return_26w"]]


def compute_industry_data(
    results: pd.DataFrame,
    market_weekly: pd.DataFrame,
) -> pd.DataFrame:
    """Compute industry RS rank and breadth.

    Args:
        results: per-symbol DataFrame (symbol, rs_26w_return)
        market_weekly: unused (kept for API compatibility)

    Returns:
        DataFrame: symbol, industry_rs_rank_pct, industry_breadth
    """
    if results.empty:
        return pd.DataFrame(columns=["symbol", "industry_rs_rank_pct", "industry_breadth"])

    config = load_config(Path("config/strategy.yaml"))
    industry_map = _load_industry_map(config)

    try:
        from winstan.adapters.tushare_client import build_tushare_pro
        _, pro = build_tushare_pro()
        ind_ret = _fetch_industry_26w_returns(pro)
    except Exception as e:
        print(f"[industry] ERROR: {e}")
        ind_ret = pd.DataFrame(columns=["industry", "return_26w"])

    if ind_ret.empty:
        result = results[["symbol"]].copy()
        result["industry_rs_rank_pct"] = 50.0
        result["industry_breadth"] = 50.0
        return result

    # Rank industries (lower pct = stronger)
    ind_ret["industry_rs_rank_pct"] = ind_ret["return_26w"].rank(
        method="dense", pct=True, ascending=True
    ) * 100.0

    # Map stocks → industry
    df = results[["symbol", "rs_26w_return"]].copy()
    df["industry"] = df["symbol"].map(industry_map)
    matched = df.dropna(subset=["industry"]).copy()

    # Industry breadth
    breadth = matched.groupby("industry")["rs_26w_return"].apply(
        lambda x: (x > 0).sum() / max(len(x), 1) * 100.0
    ).reset_index()
    breadth.columns = ["industry", "industry_breadth"]

    merged = matched[["symbol", "industry"]].merge(ind_ret, on="industry", how="left")
    merged = merged.merge(breadth, on="industry", how="left")
    merged["industry_rs_rank_pct"] = merged["industry_rs_rank_pct"].fillna(50.0)
    merged["industry_breadth"] = merged["industry_breadth"].fillna(0.0)

    stats = merged.groupby("industry").agg(
        rank=("industry_rs_rank_pct", "first"),
        breadth=("industry_breadth", "first"),
        count=("symbol", "count"),
    ).reset_index()
    print(f"[industry] {stats['count'].sum():.0f} stocks → {len(stats)} industries")
    for _, r in stats.sort_values("rank").head(5).iterrows():
        print(f"  {r['industry']:16s}  rank={r['rank']:.0f}  breadth={r['breadth']:.0f}%  n={r['count']:.0f}")

    return merged[["symbol", "industry_rs_rank_pct", "industry_breadth"]]
