"""Run Weinstein screener in batches to avoid OOM on 1.8GB RAM server."""
from __future__ import annotations

import gc
import sys
from itertools import islice
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from itertools import islice

import pandas as pd

from winstan.adapters.factory import DataSourceRouter
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import load_config
from winstan.indicators.core import compute_rs_ranks, compute_weekly_indicators
from winstan.outputs.exporters import export_results
from winstan.pipeline.universe import build_universe
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.rules.breakout_rule import evaluate_breakout
from winstan.rules.market_trend import evaluate_market_trend
from winstan.rules.relative_strength_rule import evaluate_relative_strength
from winstan.rules.resistance_rule import evaluate_resistance, compute_overhead_supply
from winstan.rules.stage_analysis import apply_stage2_scoring, detect_transition, evaluate_stage
from winstan.rules.base_quality import compute_base_quality
from winstan.rules.base_oscillation import LOOKBACK_DAYS
from winstan.rules.demand_support import compute_demand_support_quality
from winstan.rules.stage2_continuation import compute_continuation_quality
from winstan.rules.volume_confirmation import evaluate_volume
from winstan.scoring.ranker import build_stage2_top_n, score_and_rank
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore


BATCH_SIZE = 500  # process 500 stocks at a time


def _report_progress(progress_callback, **payload) -> None:
    if not callable(progress_callback):
        return
    try:
        progress_callback(payload)
    except Exception:
        pass


def _chunked(items: list[str], size: int):
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch


def run_batched_screener(progress_callback=None):
    config = load_config("config/strategy.yaml")
    router = DataSourceRouter(config)
    parquet_store = ParquetStore(config.parquet_root)
    duckdb_store = DuckDBStore(config.duckdb_path)

    raw_universe = router.fetch_stock_universe()
    universe = build_universe(raw_universe, config)
    symbols = universe["symbol"].dropna().tolist()
    print(f"Universe: {len(symbols)} stocks")

    market_daily = clean_daily_bars(
        parquet_store.read_many("index_bars", [config.market.benchmark_symbol])
    )
    market_weekly = build_weekly_bars(market_daily)
    market_weekly = compute_weekly_indicators(market_weekly, market_weekly, config)
    market_state = evaluate_market_trend(market_weekly, config)
    print(f"Market state: {market_state}")
    del market_daily
    gc.collect()

    all_records = []
    # Collect rs_composite per stock for global RS ranking later
    rs_composite_map: dict[str, float] = {}
    total = len(symbols)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    processed_symbols = 0
    demand_support_total = 0
    _report_progress(
        progress_callback,
        status="running",
        phase="started",
        message=f"Demand筛选开始: {total}只股票",
        processed=0,
        total=total,
        candidates_total=0,
        current_batch=0,
        total_batches=total_batches,
    )

    for batch_idx, batch in enumerate(_chunked(symbols, BATCH_SIZE)):
        print(f"[batch {batch_idx + 1}] Loading {len(batch)} stocks...")
        _report_progress(
            progress_callback,
            status="running",
            phase="loading",
            message=f"正在加载第 {batch_idx + 1}/{total_batches} 批，共 {len(batch)} 只",
            processed=processed_symbols,
            total=total,
            candidates_total=demand_support_total,
            current_batch=batch_idx + 1,
            total_batches=total_batches,
        )
        weekly_bars = clean_daily_bars(parquet_store.read_many("weekly_bars", batch))
        if weekly_bars.empty:
            print(f"[batch {batch_idx + 1}] No weekly data, skipping")
            processed_symbols += len(batch)
            _report_progress(
                progress_callback,
                status="running",
                phase="skipped",
                message=f"第 {batch_idx + 1}/{total_batches} 批无周线数据，已跳过",
                processed=processed_symbols,
                total=total,
                candidates_total=demand_support_total,
                current_batch=batch_idx + 1,
                total_batches=total_batches,
            )
            continue
        weekly_bars = compute_weekly_indicators(weekly_bars, market_weekly, config)
        # Compute per-batch rs composite (raw value, not ranked)
        batch_rs = compute_rs_ranks(weekly_bars)
        for _, r in batch_rs.iterrows():
            sym = str(r["symbol"])
            comp = float(r["rs_composite"]) if pd.notna(r["rs_composite"]) else None
            if comp is not None:
                rs_composite_map[sym] = comp
        # Merge rs ranks (will be overridden by global ranks after all batches)
        weekly_bars = weekly_bars.merge(batch_rs, on="symbol", how="left")
        del batch_rs
        gc.collect()

        records: list[dict] = []
        for symbol, group in weekly_bars.groupby("symbol", sort=False):
            recent = group.sort_values("trade_date").reset_index(drop=True)
            latest = recent.iloc[-1]
            stage_info = evaluate_stage(latest, recent, config)
            volume_info = evaluate_volume(recent.tail(max(config.strategy.volume_avg_weeks, 3)))
            # Evaluate RS with batch-local rank (placeholder, will be fixed globally)
            rs_info = evaluate_relative_strength(latest, config)
            resistance_info = evaluate_resistance(recent, latest, config, base_breakout_price=stage_info.get("base_breakout_price"))
            breakout_info = evaluate_breakout(latest, config)
            # Overhead supply
            daily = parquet_store.read_symbol_frame("daily_bars", symbol)
            overhead_info = compute_overhead_supply(daily)

            # Stage I 基底质量评分
            base_quality_info = compute_base_quality(recent, config, base_info=stage_info, daily=daily)
            demand_daily = daily.sort_values("trade_date").tail(LOOKBACK_DAYS).copy()
            demand_support_info = compute_demand_support_quality(recent, config, daily=demand_daily)

            # Stage2 续涨形态评分
            continuation_info = compute_continuation_quality(recent, config, daily=daily)

            # Stage1→2 转换检测
            transition_info = detect_transition(
                latest, recent, config,
                base_info=stage_info,
                base_quality_score=float(base_quality_info.get("base_quality_score", 0.0)),
            )

            record = {
                "symbol": symbol,
                "trade_date": latest["trade_date"],
                "close": float(latest["close"]),
                "market_ok": bool(market_state["market_ok"]) if config.market.use_market_filter else True,
                **stage_info,
                **volume_info,
                **rs_info,
                **resistance_info,
                **breakout_info,
                **overhead_info,
                **transition_info,
                **base_quality_info,
                **demand_support_info,
                **continuation_info,
                "price_vs_ma_pct": float(latest["price_vs_ma_pct"]) if pd.notna(latest["price_vs_ma_pct"]) else None,
                "ma_30w": float(latest["ma_30w"]) if pd.notna(latest["ma_30w"]) else None,
                "ma_10w": float(latest["ma_10w"]) if pd.notna(latest["ma_10w"]) else None,
                "ma_spread_pct": float(latest["ma_spread_pct"]) if pd.notna(latest["ma_spread_pct"]) else None,
                "base_range_pct": float(latest["base_range_pct"]) if pd.notna(latest["base_range_pct"]) else None,
                "base_close_std_pct": float(latest["base_close_std_pct"]) if pd.notna(latest["base_close_std_pct"]) else None,
                "stage2_score": float(stage_info["stage2_score"]),
                "stage2_reason": stage_info["stage2_reason"],
            }
            reject_bits = []
            for key, label in [("market_ok", "market"), ("stage2_candidate", "stage"),
                               ("volume_ok", "volume"), ("rs_ok", "relative_strength"),
                               ("resistance_ok", "resistance"), ("breakout_ok", "breakout")]:
                if not record.get(key, False):
                    reject_bits.append(label)
            record["reject_reason"] = ",".join(reject_bits)
            records.append(record)

        all_records.extend(records)
        demand_support_batch_count = sum(1 for record in records if record.get("demand_support_candidate"))
        demand_support_total += int(demand_support_batch_count)
        processed_symbols += len(batch)
        print(f"[batch {batch_idx + 1}] Done: {len(records)} evaluated ({len(all_records)}/{total} total)")
        _report_progress(
            progress_callback,
            status="running",
            phase="evaluating",
            message=(
                f"第 {batch_idx + 1}/{total_batches} 批完成: "
                f"累计 {processed_symbols}/{total}，Demand命中 {demand_support_total} 只"
            ),
            processed=processed_symbols,
            total=total,
            candidates_total=demand_support_total,
            current_batch=batch_idx + 1,
            total_batches=total_batches,
        )
        del weekly_bars, records
        gc.collect()

    # Compute global RS ranking across all stocks
    print(f"\nComputing global RS ranking across {len(rs_composite_map)} stocks...")
    _report_progress(
        progress_callback,
        status="running",
        phase="ranking",
        message="正在计算全市场RS排名并写入结果",
        processed=processed_symbols,
        total=total,
        candidates_total=demand_support_total,
        current_batch=total_batches,
        total_batches=total_batches,
    )
    rs_df = pd.DataFrame(list(rs_composite_map.items()), columns=["symbol", "rs_composite"])
    rs_df["rs_rank_pct"] = rs_df["rs_composite"].rank(method="dense", pct=True, ascending=False) * 100.0
    rs_lookup = rs_df.set_index("symbol")["rs_rank_pct"].to_dict()
    del rs_df
    gc.collect()

    # Override per-stock RS values with global ranks
    threshold = config.strategy.rs_rank_threshold_pct
    for record in all_records:
        sym = str(record["symbol"])
        if sym in rs_lookup:
            global_pct = float(rs_lookup[sym])
            record["rs_rank_pct"] = global_pct
            record["rs_ok"] = global_pct <= threshold
            record["rs_score"] = max(0.0, 100.0 - (global_pct - 1.0))
            # Recalculate reject_reason with updated rs_ok
            reject_bits = []
            for key, label in [("market_ok", "market"), ("stage2_candidate", "stage"),
                               ("volume_ok", "volume"), ("rs_ok", "relative_strength"),
                               ("resistance_ok", "resistance"), ("breakout_ok", "breakout")]:
                if not record.get(key, False):
                    reject_bits.append(label)
            record["reject_reason"] = ",".join(reject_bits)

    print(f"Global RS ranking applied to {len(all_records)} records.")

    if not all_records:
        print("No results!")
        return

    results = pd.DataFrame(all_records)
    results = results.merge(universe[["symbol", "name"]], on="symbol", how="left")
    results = apply_stage2_scoring(results, config)
    print(f"Results: {len(results)} rows")

    candidates, top_n = score_and_rank(results, config)
    stage2_top_n = build_stage2_top_n(results, config)

    summary = {
        "total_symbols": len(symbols),
        "market_ok": market_state.get("market_ok"),
        "stage2_count": int(results["stage2_candidate"].sum()) if not results.empty else 0,
        "stage2_top_count": len(stage2_top_n),
        "volume_ok_count": int(results["volume_ok"].sum()) if not results.empty else 0,
        "rs_ok_count": int(results["rs_ok"].sum()) if not results.empty else 0,
        "resistance_ok_count": int(results["resistance_ok"].sum()) if not results.empty else 0,
        "demand_support_count": int(results["demand_support_candidate"].sum()) if not results.empty and "demand_support_candidate" in results.columns else 0,
        "candidate_count": len(candidates),
    }

    export_results(config, results, candidates, top_n, stage2_top_n, summary)
    duckdb_store.write_results("screening_results", results)
    duckdb_store.append_snapshot(results)

    print(f"\n=== Summary ===")
    print(f"Total: {summary['total_symbols']}")
    print(f"Market OK: {summary['market_ok']}")
    print(f"Stage II candidates: {summary['stage2_count']}")
    print(f"Stage II Top N: {summary['stage2_top_count']}")
    print(f"Candidates: {summary['candidate_count']}")
    _report_progress(
        progress_callback,
        status="completed",
        phase="completed",
        message=f"Demand筛选完成: 命中 {summary['demand_support_count']} 只",
        processed=total,
        total=total,
        candidates_total=summary["demand_support_count"],
        current_batch=total_batches,
        total_batches=total_batches,
    )


if __name__ == "__main__":
    run_batched_screener()
