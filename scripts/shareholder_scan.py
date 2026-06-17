#!/usr/bin/env python3
"""股东人数排行榜 — Tushare批量拉取 + Weinstein交叉筛选

用法:
    python scripts/shareholder_scan.py              # 标准运行
    python scripts/shareholder_scan.py --dry-run    # 仅拉取，不写库
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

# ── 路径设置 ──────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

from winstan.adapters.tushare_client import build_tushare_pro
from winstan.config import load_config
from winstan.storage.duckdb_store import DuckDBStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("shareholder_scan")

# ── 常量 ─────────────────────────────────────────────────────
SHAREHOLDER_TABLE = "shareholder_ranking"
QUARTER_ENDS = [
    "20260331", "20251231", "20250930", "20250630",
    "20250331", "20241231", "20240930",
]  # 最新优先，自动选最新的2个有数据的季度


# ══════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════

def fetch_shareholder_data(pro: Any) -> pd.DataFrame:
    """批量拉取最近2个季度的股东人数。
    
    Returns:
        DataFrame with columns: ts_code, end_date, holder_num, ann_date
    """
    frames: list[pd.DataFrame] = []
    found_quarters: list[str] = []

    for end_date in QUARTER_ENDS:
        if len(found_quarters) >= 2:
            break
        try:
            logger.info(f"拉取 {end_date} ...")
            df = pro.stk_holdernumber(end_date=end_date)
            if df is not None and not df.empty:
                df = df[["ts_code", "ann_date", "end_date", "holder_num"]].copy()
                df["holder_num"] = pd.to_numeric(df["holder_num"], errors="coerce")
                frames.append(df)
                found_quarters.append(end_date)
                logger.info(f"  → {len(df)} 只股票")
        except Exception as exc:
            logger.warning(f"  {end_date} 拉取失败: {exc}")

    if not frames:
        raise RuntimeError("未能获取任何季度的股东人数数据")

    combined = pd.concat(frames, ignore_index=True)
    logger.info(f"合计: {len(combined)} 行, 季度: {found_quarters}")
    return combined


# ══════════════════════════════════════════════════════════════
#  计算变化
# ══════════════════════════════════════════════════════════════

def compute_shareholder_changes(raw: pd.DataFrame) -> pd.DataFrame:
    """计算每只股票股东人数变化（最新季度 vs 上季度）。
    
    Args:
        raw: fetch_shareholder_data 返回的合并数据
    
    Returns:
        DataFrame: symbol, name, holder_num_latest, holder_num_prev, 
                   holder_change_pct, holder_change_abs, holder_change_score
    """
    # 找出每个symbol的最新和上一季度
    raw = raw.copy()
    raw["end_date"] = pd.to_datetime(raw["end_date"])
    raw = raw.sort_values(["ts_code", "end_date"], ascending=[True, False])
    
    latest = raw.groupby("ts_code").first().reset_index()
    prev = raw.groupby("ts_code").nth(1).reset_index()
    
    merged = latest[["ts_code", "end_date", "holder_num", "ann_date"]].rename(
        columns={
            "end_date": "latest_quarter",
            "holder_num": "holder_num_latest",
            "ann_date": "ann_date_latest",
        }
    )
    
    prev_renamed = prev[["ts_code", "end_date", "holder_num"]].rename(
        columns={
            "end_date": "prev_quarter",
            "holder_num": "holder_num_prev",
        }
    )
    
    result = merged.merge(prev_renamed, on="ts_code", how="left")
    
    # 计算变化
    result["holder_change_abs"] = result["holder_num_latest"] - result["holder_num_prev"]
    result["holder_change_pct"] = result.apply(
        lambda r: round(
            (r["holder_change_abs"] / r["holder_num_prev"] * 100), 2
        )
        if pd.notna(r["holder_num_prev"]) and r["holder_num_prev"] > 0
        else None,
        axis=1,
    )
    
    # 评分：减少越多分越高
    def _score(pct: float | None) -> float:
        if pct is None or pd.isna(pct):
            return 0.0
        if pct <= -20:
            return 100.0  # 筹码大幅集中
        if pct <= -15:
            return 90.0
        if pct <= -10:
            return 80.0  # 明显集中
        if pct <= -5:
            return 60.0   # 小幅集中
        if pct <= -3:
            return 40.0
        if pct <= 0:
            return 20.0   # 基本持平
        if pct <= 5:
            return 5.0    # 微增
        if pct <= 10:
            return -10.0  # 分散
        return -30.0      # 大幅分散
    
    result["holder_change_score"] = result["holder_change_pct"].apply(_score)
    
    # 重命名 ts_code → symbol
    result = result.rename(columns={"ts_code": "symbol"})
    
    logger.info(
        f"股东人数变化: 减少={len(result[result['holder_change_pct'] < 0])}只, "
        f"持平={len(result[result['holder_change_pct'] == 0])}只, "
        f"增加={len(result[result['holder_change_pct'] > 0])}只"
        if not result.empty else "无数据"
    )
    
    return result


# ══════════════════════════════════════════════════════════════
#  Weinstein 交叉
# ══════════════════════════════════════════════════════════════

def cross_with_weinstein(
    shareholder: pd.DataFrame,
    store: DuckDBStore,
) -> pd.DataFrame:
    """将股东数据与 Weinstein screening_results 交叉合并。
    
    Args:
        shareholder: compute_shareholder_changes 的输出
        store: DuckDB 连接
    
    Returns:
        合并后的 DataFrame，包含 Weinstein 评分和股东数据
    """
    # 读取 screening_results
    try:
        with store.connect() as conn:
            screening = conn.execute(
                "SELECT symbol, name, close, final_score, stage_label, "
                "stage2_score, stage2_candidate, headroom_pct, "
                "overhead_supply_pct "
                "FROM screening_results"
            ).fetchdf()
    except Exception:
        logger.warning("无法读取 screening_results（可能尚未运行Phase1）")
        screening = pd.DataFrame()
    
    if screening.empty:
        # 无Weinstein数据，纯股东排行
        result = shareholder.copy()
        result["name"] = result["symbol"]  # fallback
        result["final_score"] = 0.0
        result["stage_label"] = ""
        result["stage2_candidate"] = False
        result["weinstein_available"] = False
    else:
        result = shareholder.merge(
            screening, on="symbol", how="left", suffixes=("", "_ws")
        )
        result["weinstein_available"] = result["final_score"].notna()
        result["final_score"] = result["final_score"].fillna(0.0)
        result["name"] = result["name"].fillna(result["symbol"])
        result["stage_label"] = result["stage_label"].fillna("")
        result["stage2_candidate"] = result["stage2_candidate"].fillna(False)
    
    # 综合分 = 股东变化评分 × 50% + Weinstein综合分 × 50%
    # 对于无Weinstein数据的股票，综合分 = 股东变化评分
    result["combined_score"] = result.apply(
        lambda r: round(
            r["holder_change_score"] * 0.5
            + r["final_score"] * 0.5
            if r["weinstein_available"]
            else r["holder_change_score"],
            1,
        ),
        axis=1,
    )
    
    # 排名
    result = result.sort_values("combined_score", ascending=False).reset_index(drop=True)
    result["rank"] = range(1, len(result) + 1)
    
    return result


# ══════════════════════════════════════════════════════════════
#  数据写入
# ══════════════════════════════════════════════════════════════

def save_to_duckdb(store: DuckDBStore, df: pd.DataFrame) -> None:
    """将排行榜写入 DuckDB（replace模式）。
    
    Args:
        store: DuckDB 连接
        df: cross_with_weinstein 的输出
    """
    with store.connect() as conn:
        # 确保表存在
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {SHAREHOLDER_TABLE} (
                symbol VARCHAR PRIMARY KEY,
                name VARCHAR,
                rank INTEGER,
                holder_num_latest DOUBLE,
                holder_num_prev DOUBLE,
                holder_change_pct DOUBLE,
                holder_change_score DOUBLE,
                latest_quarter VARCHAR,
                prev_quarter VARCHAR,
                final_score DOUBLE,
                stage_label VARCHAR,
                stage2_candidate BOOLEAN,
                combined_score DOUBLE,
                weinstein_available BOOLEAN,
                scan_date VARCHAR
            )
        """)
        
        # 写入
        today = date.today().strftime("%Y-%m-%d")
        write_df = df[[
            "symbol", "name", "rank", "holder_num_latest", "holder_num_prev",
            "holder_change_pct", "holder_change_score", "latest_quarter",
            "prev_quarter", "final_score", "stage_label", "stage2_candidate",
            "combined_score", "weinstein_available",
        ]].copy()
        write_df["scan_date"] = today
        write_df["latest_quarter"] = write_df["latest_quarter"].astype(str)
        write_df["prev_quarter"] = write_df["prev_quarter"].astype(str)
        
        # Replace
        conn.execute(f"DELETE FROM {SHAREHOLDER_TABLE}")
        conn.register("_tmp_shareholder", write_df)
        cols = ", ".join(f'"{c}"' for c in write_df.columns)
        conn.execute(
            f"INSERT INTO {SHAREHOLDER_TABLE} ({cols}) SELECT * FROM _tmp_shareholder"
        )
        conn.unregister("_tmp_shareholder")
        
        count = conn.execute(
            f"SELECT COUNT(*) FROM {SHAREHOLDER_TABLE}"
        ).fetchone()[0]
        logger.info(f"写入 {count} 条记录到 {SHAREHOLDER_TABLE}")


# ══════════════════════════════════════════════════════════════
#  Top N 摘要
# ══════════════════════════════════════════════════════════════

def print_top_summary(df: pd.DataFrame, top_n: int = 20) -> None:
    """打印排名摘要。"""
    top = df.head(top_n)
    print(f"\n{'='*80}")
    print(f"  股东人数排行榜 TOP {top_n}")
    print(f"{'='*80}")
    print(f"{'排名':<5} {'代码':<12} {'名称':<10} {'股东变化%':>8} {'股东评分':>6} {'Weinstein':>8} {'综合分':>6} {'阶段':<8}")
    print(f"{'-'*80}")
    for _, r in top.iterrows():
        chg = f"{r['holder_change_pct']:+.1f}%" if pd.notna(r['holder_change_pct']) else "N/A"
        ws = f"{r['final_score']:.0f}" if r['weinstein_available'] else "N/A"
        combined = f"{r['combined_score']:.0f}"
        print(
            f"{r['rank']:<5} "
            f"{r['symbol']:<12} "
            f"{str(r.get('name','?')):<10} "
            f"{chg:>8} "
            f"{r['holder_change_score']:>6.0f} "
            f"{ws:>8} "
            f"{combined:>6} "
            f"{str(r.get('stage_label','')):<8}"
        )


# ══════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="股东人数排行榜")
    parser.add_argument("--dry-run", action="store_true", help="仅拉取分析，不写库")
    parser.add_argument("--top", type=int, default=20, help="显示前N名")
    args = parser.parse_args()

    logger.info("===== 股东人数扫描开始 =====")

    # 1. 拉取数据
    _, pro = build_tushare_pro()
    raw = fetch_shareholder_data(pro)
    
    # 2. 计算变化
    shareholder = compute_shareholder_changes(raw)
    
    # 3. 交叉Weinstein
    config = load_config(PROJECT_DIR / "config" / "strategy.yaml")
    store = DuckDBStore(config.duckdb_path)
    combined = cross_with_weinstein(shareholder, store)
    
    # 4. 保存
    if not args.dry_run:
        save_to_duckdb(store, combined)
        logger.info("数据已写入 DuckDB")
    else:
        logger.info("DRY RUN — 未写入数据库")
    
    # 5. 摘要
    print_top_summary(combined, args.top)
    
    # 统计
    reducing = len(combined[combined["holder_change_pct"] < 0])
    with_weinstein = len(combined[combined["weinstein_available"]])
    stage2 = len(combined[combined["stage2_candidate"] == True])
    print(f"\n统计: 共{len(combined)}只 | 筹码集中={reducing}只 | "
          f"有Weinstein评分={with_weinstein}只 | Stage2候选={stage2}只")
    
    logger.info("===== 扫描完成 =====")


if __name__ == "__main__":
    main()
