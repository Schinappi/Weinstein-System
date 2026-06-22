#!/usr/bin/env python3
"""
收盘前一键跑：拉腾讯实时行情 → 注入今日日K到 daily_bars parquet → 跑完整 Phase1 筛选
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ── 项目路径 ──────────────────────────────────
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from winstan.adapters.tushare_client import build_tushare_pro
from winstan.config import AppConfig, load_config
from winstan.data.daily_updater import bulk_update_recent
from winstan.pipeline.screener import WeinsteinScreener
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore

CONFIG_PATH = PROJECT_ROOT / "config" / "strategy.yaml"

# ── 腾讯行情字段索引 ──────────────────────────
F_CODE = 2     # 市场代码 (e.g., "600519")
F_PRICE = 3    # 当前价
F_PRE_CLOSE = 4  # 昨收
F_OPEN = 5     # 今开
F_VOLUME = 6   # 成交量(手)
F_HIGH = 33    # 最高
F_LOW = 34     # 最低
F_AMOUNT = 37  # 成交额
F_PRE_VOLUME = 22  # 昨成交量
F_PERCENT = 43 # 涨跌幅


def _safe_float(val) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def fetch_all_realtime() -> dict[str, dict]:
    """拉全A实时行情，返回 {symbol -> {...}}"""
    code_ranges = [
        *(f"sh{str(i).zfill(6)}" for i in range(600000, 606000)),
        *(f"sh{str(i).zfill(6)}" for i in range(603000, 605000)),
        *(f"sh{str(i).zfill(6)}" for i in range(605000, 606000)),
        *(f"sh{str(i).zfill(6)}" for i in range(688000, 690000)),
        *(f"sz{str(i).zfill(6)}" for i in range(0, 1000)),
        *(f"sz{str(i).zfill(6)}" for i in range(1000, 2000)),
        *(f"sz{str(i).zfill(6)}" for i in range(2000, 3000)),
        *(f"sz{str(i).zfill(6)}" for i in range(3000, 3100)),
        *(f"sz{str(i).zfill(6)}" for i in range(300000, 302000)),
        *(f"bj{str(i).zfill(6)}" for i in range(920000, 921000)),
        *(f"bj{str(i).zfill(6)}" for i in range(430000, 431000)),
        *(f"bj{str(i).zfill(6)}" for i in range(830000, 831000)),
    ]
    result: dict[str, dict] = {}
    batch_size = 80
    session = requests.Session()
    total_batches = (len(code_ranges) + batch_size - 1) // batch_size

    for i in range(0, len(code_ranges), batch_size):
        batch = code_ranges[i : i + batch_size]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = session.get(url, timeout=15)
            resp.encoding = "gbk"
            for line in resp.text.strip().split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                try:
                    raw = line.split("=", 1)[1].strip('"')
                    parts = raw.split("~")
                    if len(parts) < 40:
                        continue
                    code = parts[F_CODE]
                    price = _safe_float(parts[F_PRICE])
                    if price <= 0:
                        continue
                    if code.startswith("6"):
                        symbol = f"{code}.SH"
                    elif code.startswith(("0", "3")):
                        symbol = f"{code}.SZ"
                    elif code.startswith(("4", "8", "9")):
                        symbol = f"{code}.BJ"
                    else:
                        continue
                    result[symbol] = {
                        "price": price,
                        "pre_close": _safe_float(parts[F_PRE_CLOSE]),
                        "open": _safe_float(parts[F_OPEN]),
                        "high": _safe_float(parts[F_HIGH]),
                        "low": _safe_float(parts[F_LOW]),
                        "volume": int(float(parts[F_VOLUME]) if parts[F_VOLUME] else 0),
                        "amount": _safe_float(parts[F_AMOUNT]),
                        "change_pct": _safe_float(parts[F_PERCENT]),
                    }
                except (IndexError, ValueError):
                    continue
        except requests.RequestException as e:
            print(f"[preclose] Batch fetch error: {e}", file=sys.stderr)
    return result


def realtime_to_daily_bars(
    realtime: dict[str, dict],
    today_date: date,
) -> pd.DataFrame:
    """将实时行情转换为 daily_bars 格式的 DataFrame"""
    rows = []
    for symbol, rt in realtime.items():
        rows.append({
            "symbol": symbol,
            "trade_date": today_date,
            "open": rt.get("open", 0.0),
            "high": rt.get("high", 0.0),
            "low": rt.get("low", 0.0),
            "close": rt["price"],
            "volume": rt.get("volume", 0),
            "amount": rt.get("amount", 0.0),
            "adj_factor": 1.0,     # 未复权
            "source": "realtime",
        })
    df = pd.DataFrame(rows)
    # 类型统一
    for col in ["open", "high", "low", "close", "amount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(np.int64)
    return df


def inject_realtime_into_daily_bars(
    config: AppConfig,
    realtime: dict[str, dict],
    today: date,
) -> None:
    """保存实时行情为 daily_bars/__today__.parquet + intraday/YYYY-MM-DD.parquet"""
    today_df = realtime_to_daily_bars(realtime, today)
    if today_df.empty:
        print("[inject] No realtime data to inject")
        return

    parquet_root = Path(config.parquet_root)
    # 1. Inject into daily_bars (for Phase1 DuckDB view)
    today_path = parquet_root / "daily_bars" / "__today__.parquet"
    today_df.to_parquet(today_path, index=False)
    print(f"[inject] Saved {len(today_df)} bars → {today_path}")

    # 2. Save intraday snapshot (for Dashboard K-line view via _ensure_daily_bars)
    from winstan.storage.parquet_store import ParquetStore
    store = ParquetStore(config.parquet_root)
    today_str = today.strftime("%Y-%m-%d")
    store.write_intraday_snapshot(today_str, today_df)
    print(f"[inject] Saved intraday snapshot → {today_str}")


def main() -> None:
    t0 = time.time()
    config = load_config(CONFIG_PATH)
    today = date.today()
    print(f"[preclose] Date: {today}")

    # ── Step 0: 补全近期缺失的日K线 ──
    print("[preclose] Step 0: Completing missing recent daily bars...")
    parquet_store = ParquetStore(config.parquet_root)
    try:
        _, pro = build_tushare_pro(config.data.tushare_token)
        update_result = bulk_update_recent(pro, parquet_store, days_back=5, end_date=today)
        print(f"[preclose] K-line补全: updated={update_result['updated']} "
              f"rows_added={update_result['rows_added']} "
              f"api={update_result['api_seconds']:.1f}s merge={update_result['merge_seconds']:.0f}s")
    except Exception as e:
        print(f"[preclose] K-line补全失败 (继续): {e}")

    # ── Step 1: 拉实时行情 ──
    print("[preclose] Fetching all A-share real-time data...")
    realtime = fetch_all_realtime()
    print(f"[preclose] Got {len(realtime)} real-time quotes")

    # ── Step 2: 注入今日日K到 daily_bars parquet ──
    print("[preclose] Injecting real-time bars into daily_bars parquet...")
    inject_realtime_into_daily_bars(config, realtime, today)

    # ── Step 3: 跑 Phase1 完整筛选 ──
    print("[preclose] Running Phase1 screening...")
    screener = WeinsteinScreener(config)
    results = screener.run()
    summary = results.get("summary", {})
    elapsed = time.time() - t0

    print(f"\n{'═' * 50}")
    print(f"✅ Phase1 complete in {elapsed:.1f}s")
    print(f"   Total symbols:     {summary.get('total_symbols', '?')}")
    print(f"   Candidates:        {summary.get('candidate_count', '?')}")
    print(f"   Stage II top N:    {summary.get('stage2_top_count', '?')}")
    print(f"   Stage2 candidates: {summary.get('stage2_count', '?')}")
    print(f"   Market OK:         {summary.get('market_ok', '?')}")
    print(f"{'═' * 50}")

    # 输出 JSON 摘要供 service.py 解析
    print(f"\n[preclose-summary] {{\"success\":true,\"elapsed_seconds\":{elapsed:.1f},\"candidate_count\":{summary.get('candidate_count',0)},\"stage2_count\":{summary.get('stage2_count',0)},\"stage2_top_count\":{summary.get('stage2_top_count',0)},\"total_symbols\":{summary.get('total_symbols',0)}}}")


if __name__ == "__main__":
    main()
