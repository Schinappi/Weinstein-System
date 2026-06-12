"""
Supplementary fundamental data fetching and scoring for Weinstein screening.

Integrates Tushare APIs (股东人数, 北向资金, 个股资金流) into the
Weinstein scoring pipeline.  Each function returns a dict of per-symbol
scores that get merged into the screening results DataFrame.

APIs used (all require 5000+ Tushare积分):
  - stk_holdernumber  → holder_score
  - hk_hold            → northbound_score
  - moneyflow          → moneyflow_confirm
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from winstan.adapters.tushare_client import build_tushare_pro

# ── helper ──────────────────────────────────────────────────────────


def _to_float(v) -> float | None:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _batch_symbols(symbols: list[str], size: int = 200):
    """Yield successive chunks of symbols."""
    for i in range(0, len(symbols), size):
        yield symbols[i : i + size]


# ═══════════════════════════════════════════════════════════════════
# 1. 股东人数 (stk_holdernumber)
# ═══════════════════════════════════════════════════════════════════

HOLDER_QUARTER_MAP = {
    1: "0331",
    2: "0630",
    3: "0930",
    4: "1231",
}


def _latest_holder_quarters(all_dates: list[str]) -> tuple[str, str]:
    """Given available end_dates from API, return (latest, previous) quarter ends."""
    sorted_dates = sorted(set(d for d in all_dates if d), reverse=True)
    if len(sorted_dates) < 2:
        return (sorted_dates[0] if sorted_dates else None, None)
    return (sorted_dates[0], sorted_dates[1])


def fetch_holder_data(
    pro: object,
    symbols: list[str],
) -> dict[str, dict]:
    """Fetch latest 2 quarters of shareholder count for each symbol.

    Returns {symbol: {"holder_num": ..., "prev_holder_num": ...,
                       "holder_change_pct": ...}}
    """
    from winstan.config import load_config
    from pathlib import Path
    config = load_config(Path("config/strategy.yaml"))

    today = date.today()
    result: dict[str, dict] = {}
    cached = _load_holder_cache(config)

    for batch in _batch_symbols(symbols):
        for sym in batch:
            # Try cache first
            sym_clean = sym.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
            ts_code = sym
            cached_val = cached.get(ts_code, {})

            # Get available dates from cache
            cached_dates = sorted(cached_val.keys(), reverse=True) if cached_val else []
            if len(cached_dates) >= 2:
                curr_q, prev_q = cached_dates[0], cached_dates[1]
                curr = cached_val.get(curr_q)
                prev = cached_val.get(prev_q)
                if curr is not None and prev is not None:
                    change_pct = ((curr - prev) / prev * 100) if prev != 0 else 0.0
                    result[sym] = {
                        "holder_num": curr,
                        "prev_holder_num": prev,
                        "holder_change_pct": round(change_pct, 2),
                    }
                    continue

            # Fetch from API
            try:
                df = pro.stk_holdernumber(ts_code=ts_code, limit=10)
            except Exception:
                continue
            if df is None or df.empty:
                continue

            # Map end_date → holder_num
            holder_map: dict[str, float] = {}
            for _, row in df.iterrows():
                ed = str(row.get("end_date", ""))
                hn = _to_float(row.get("holder_num"))
                if ed and hn is not None:
                    # Keep first occurrence (latest by API sort order)
                    if ed not in holder_map:
                        holder_map[ed] = hn

            # Save to cache
            cached[ts_code] = holder_map

            # Get latest and previous
            avail_dates = sorted(holder_map.keys(), reverse=True)
            if len(avail_dates) >= 2:
                curr_q, prev_q = avail_dates[0], avail_dates[1]
                curr = holder_map.get(curr_q)
                prev = holder_map.get(prev_q)
                if curr is not None and prev is not None:
                    change_pct = ((curr - prev) / prev * 100) if prev != 0 else 0.0
                    result[sym] = {
                        "holder_num": curr,
                        "prev_holder_num": prev,
                        "holder_change_pct": round(change_pct, 2),
                    }

    _save_holder_cache(config, cached)
    return result


def _load_holder_cache(config) -> dict[str, dict[str, float]]:
    """Load the shareholder number cache from parquet."""
    import json
    from pathlib import Path
    cache_path = Path(config.parquet_root) / "supplement" / "holder_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass
    return {}


def _save_holder_cache(config, cache: dict) -> None:
    import json
    from pathlib import Path
    cache_path = Path(config.parquet_root) / "supplement" / "holder_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, default=str))


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


# ═══════════════════════════════════════════════════════════════════
# 2. 北向资金 (hk_hold)
# ═══════════════════════════════════════════════════════════════════

def fetch_northbound_data(
    pro: object,
    symbols: list[str],
) -> dict[str, dict]:
    """Fetch latest northbound (HK Stock Connect) holdings for each A-share symbol.

    hk_hold data is monthly (month-end).  We pull the latest few records
    and check month-over-month ratio increases.

    Returns {symbol: {"nb_ratio": ..., "nb_consecutive_increases": ...,
                       "nb_score": ...}}
    """
    result: dict[str, dict] = {}

    for batch in _batch_symbols(symbols):
        for sym in batch:
            ts_code = sym
            exchange = "SZ" if ".SZ" in sym else "SH" if ".SH" in sym else ""

            try:
                df = pro.hk_hold(ts_code=ts_code, exchange=exchange, limit=15)
            except Exception:
                continue
            if df is None or df.empty:
                continue

            # Sort by trade_date descending
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)

            ratios = []
            for _, row in df.iterrows():
                r = _to_float(row.get("ratio"))
                if r is not None:
                    ratios.append(r)

            if len(ratios) < 2:
                result[sym] = {
                    "nb_ratio": ratios[0] if ratios else None,
                    "nb_consecutive_increases": 0,
                }
                continue

            # Count consecutive increases from most recent period backwards
            consecutive = 0
            for i in range(len(ratios) - 1):
                if ratios[i] > ratios[i + 1]:
                    consecutive += 1
                else:
                    break

            result[sym] = {
                "nb_ratio": ratios[0],
                "nb_consecutive_increases": consecutive,
            }

    return result


def compute_northbound_score(nb_ratio: float | None, consecutive_increases: int) -> float:
    """Score northbound holding trend.

    Rules (adapted for monthly data):
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


# ═══════════════════════════════════════════════════════════════════
# 3. 个股资金流 (moneyflow)
# ═══════════════════════════════════════════════════════════════════

def fetch_moneyflow_data(
    pro: object,
    symbols: list[str],
    lookback_days: int = 5,
) -> dict[str, dict]:
    """Fetch recent moneyflow data for each symbol.

    We look at net large+extra-large order money flow over the past N trading days.

    Returns {symbol: {"net_mf_amount": ..., "mf_consecutive_positive": ...,
                       "moneyflow_confirm": ...}}
    """
    end = date.today()
    start = end - timedelta(days=lookback_days * 2)  # buffer for non-trading days

    result: dict[str, dict] = {}

    for batch in _batch_symbols(symbols):
        for sym in batch:
            ts_code = sym

            try:
                df = pro.moneyflow(
                    ts_code=ts_code,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    limit=lookback_days * 2,
                )
            except Exception:
                continue
            if df is None or df.empty:
                continue

            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)

            # Compute net large+extra-large order amount for each day
            net_amounts = []
            for _, row in df.iterrows():
                buy_lg = _to_float(row.get("buy_lg_amount")) or 0.0
                sell_lg = _to_float(row.get("sell_lg_amount")) or 0.0
                buy_elg = _to_float(row.get("buy_elg_amount")) or 0.0
                sell_elg = _to_float(row.get("sell_elg_amount")) or 0.0
                net = (buy_lg + buy_elg) - (sell_lg + sell_elg)
                net_amounts.append(net)

            if not net_amounts:
                continue

            # Latest day net flow
            latest_net = net_amounts[0]

            # Count consecutive positive net flow days from latest
            consecutive = 0
            for amt in net_amounts:
                if amt > 0:
                    consecutive += 1
                else:
                    break

            result[sym] = {
                "net_mf_amount": round(latest_net, 2),
                "mf_consecutive_positive": consecutive,
            }

    return result


def compute_moneyflow_confirm(
    net_mf_amount: float | None,
    consecutive_positive: int,
) -> float:
    """Score moneyflow confirmation (bonus only, not a gate).

    Rules:
      Latest day positive net flow        → +5
      Consecutive 3+ days positive        → +10
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


# ═══════════════════════════════════════════════════════════════════
# 4. Main entry point
# ═══════════════════════════════════════════════════════════════════

def fetch_supplemental_data(
    results: pd.DataFrame,
) -> pd.DataFrame:
    """Fetch supplemental fundamental/moneyflow data and merge into results.

    This is the main entry point called from ``screener.run()`` after the
    basic evaluation is complete.

    Only fetches data for symbols where ``stage2_candidate`` is True (saves API quota).

    Returns a copy of ``results`` with new columns:
      holder_num, holder_change_pct, holder_score,
      nb_ratio, nb_consecutive_increases, nb_score,
      net_mf_amount, mf_consecutive_positive, moneyflow_confirm
    """
    if results.empty:
        return results

    scored = results.copy()

    # Only fetch for candidate symbols to save API quota
    candidate_symbols = scored[scored["stage2_candidate"] == True]["symbol"].dropna().unique().tolist()
    fetch_symbols = list(set(candidate_symbols))
    # Limit to prevent excessive API calls (Tushare rate limited)
    max_fetch = 300
    if len(fetch_symbols) > max_fetch:
        print(f"[fundamental] Limiting fetch from {len(fetch_symbols)} to {max_fetch} symbols")
        fetch_symbols = fetch_symbols[:max_fetch]

    if not fetch_symbols:
        print("[fundamental] No candidate symbols to fetch supplemental data for")
        _fill_fundamental_defaults(scored)
        return scored

    try:
        ts, pro = build_tushare_pro()
    except Exception as exc:
        print(f"[fundamental] Tushare connection failed: {exc}")
        # Fill with defaults and return
        _fill_fundamental_defaults(scored)
        return scored

    # 1. 股东人数
    print(f"[fundamental] Fetching holder data for {len(fetch_symbols)} symbols...")
    holder_data = fetch_holder_data(pro, fetch_symbols)
    scored["holder_num"] = scored["symbol"].map(lambda s: (holder_data.get(s) or {}).get("holder_num"))
    scored["holder_change_pct"] = scored["symbol"].map(
        lambda s: (holder_data.get(s) or {}).get("holder_change_pct")
    )
    scored["holder_score"] = scored["holder_change_pct"].apply(compute_holder_score)
    holder_filled = scored["holder_score"].ne(0).sum()
    holder_sourced = len(holder_data)
    print(f"  → holder data for {holder_sourced} symbols, {holder_filled} have non-zero scores")

    # 2. 北向资金
    print(f"[fundamental] Fetching northbound data...")
    nb_data = fetch_northbound_data(pro, fetch_symbols)
    scored["nb_ratio"] = scored["symbol"].map(lambda s: (nb_data.get(s) or {}).get("nb_ratio"))
    scored["nb_consecutive_increases"] = scored["symbol"].map(
        lambda s: (nb_data.get(s) or {}).get("nb_consecutive_increases", 0)
    )
    scored["nb_score"] = scored.apply(
        lambda row: compute_northbound_score(
            row.get("nb_ratio"),
            int(row.get("nb_consecutive_increases") or 0),
        ),
        axis=1,
    )
    nb_filled = (scored["nb_ratio"].notna()).sum()
    print(f"  → got northbound data for {len(nb_data)}/{len(fetch_symbols)} symbols, "
          f"{nb_filled} have ratios")

    # 3. 个股资金流
    print(f"[fundamental] Fetching moneyflow data...")
    mf_data = fetch_moneyflow_data(pro, fetch_symbols)
    scored["net_mf_amount"] = scored["symbol"].map(lambda s: (mf_data.get(s) or {}).get("net_mf_amount"))
    scored["mf_consecutive_positive"] = scored["symbol"].map(
        lambda s: (mf_data.get(s) or {}).get("mf_consecutive_positive", 0)
    )
    scored["moneyflow_confirm"] = scored.apply(
        lambda row: compute_moneyflow_confirm(
            row.get("net_mf_amount"),
            int(row.get("mf_consecutive_positive") or 0),
        ),
        axis=1,
    )
    mf_filled = (scored["net_mf_amount"].notna()).sum()
    print(f"  → got moneyflow data for {len(mf_data)}/{len(fetch_symbols)} symbols, "
          f"{mf_filled} have flows")

    # Fill any missing
    for col in ["holder_score", "nb_score", "moneyflow_confirm"]:
        scored[col] = scored[col].fillna(0.0)

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
