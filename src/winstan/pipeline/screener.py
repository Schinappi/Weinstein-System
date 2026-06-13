from __future__ import annotations

from itertools import islice

import pandas as pd

from winstan.adapters.factory import DataSourceRouter
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig
from winstan.indicators.core import compute_rs_ranks, compute_weekly_indicators
from winstan.outputs.exporters import export_results
from winstan.pipeline.universe import build_universe
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.rules.breakout_rule import evaluate_breakout
from winstan.rules.market_trend import evaluate_market_trend
from winstan.rules.relative_strength_rule import evaluate_relative_strength
from winstan.rules.resistance_rule import evaluate_resistance
from winstan.rules.stage_analysis import apply_stage2_scoring, evaluate_stage
from winstan.rules.volume_confirmation import evaluate_volume
from winstan.scoring.fundamental import fetch_supplemental_data
from winstan.scoring.ranker import build_stage2_top_n, score_and_rank
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore


MIN_MARKET_HISTORY_ROWS = 150


def _chunked(items: list[str], size: int):
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch


class WeinsteinScreener:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.router = DataSourceRouter(config)
        self.parquet_store = ParquetStore(config.parquet_root)
        self.duckdb_store = DuckDBStore(config.duckdb_path)

    def run(self) -> dict[str, object]:
        raw_universe = self._load_raw_universe()
        universe = build_universe(raw_universe, self.config)
        symbols = universe["symbol"].dropna().tolist()

        self._ensure_stock_cache(symbols)
        self._ensure_index_cache(self.config.market.benchmark_symbol)

        # Build market weekly once (small)
        with self.duckdb_store.connect() as conn:
            market_daily = clean_daily_bars(
                conn.execute("SELECT * FROM index_bars").fetchdf()
            )
        market_weekly = build_weekly_bars(market_daily)
        market_weekly = compute_weekly_indicators(market_weekly, market_weekly, self.config)
        market_state = evaluate_market_trend(market_weekly, self.config)
        del market_daily

        # Process stocks in batches to avoid OOM.
        # RS ranks need ALL stocks, so collect rs_composite per batch and rank at the end.
        batch_size = 500
        all_weekly: list[pd.DataFrame] = []
        all_rs_composite: list[pd.DataFrame] = []
        for batch_idx in range(0, len(symbols), batch_size):
            batch_symbols = symbols[batch_idx:batch_idx + batch_size]
            with self.duckdb_store.connect() as conn:
                placeholders = ",".join(f"'{s}'" for s in batch_symbols)
                batch_daily = clean_daily_bars(
                    conn.execute(f"SELECT * FROM daily_bars WHERE symbol IN ({placeholders})").fetchdf()
                )
            batch_weekly = build_weekly_bars(batch_daily)
            batch_weekly = compute_weekly_indicators(batch_weekly, market_weekly, self.config)

            # Collect rs_composite for ALL-STOCK ranking later
            latest = batch_weekly.sort_values(["symbol", "trade_date"]).groupby("symbol", as_index=False).tail(1).copy()
            rs_comp = latest["rs_13w_return"].fillna(0.0) * 0.50 \
                      + latest["rs_26w_return"].fillna(0.0) * 0.30 \
                      + latest["rs_52w_return"].fillna(0.0) * 0.20
            all_rs_composite.append(pd.DataFrame({"symbol": latest["symbol"], "rs_composite": rs_comp}))

            all_weekly.append(batch_weekly)
            del batch_daily, batch_weekly, latest

            print(f"[phase1] batch {batch_idx // batch_size + 1}/{(len(symbols) + batch_size - 1) // batch_size} done ({len(batch_symbols)} symbols)")

        # Combine weekly, compute RS ranks across ALL stocks, merge back
        weekly_bars = pd.concat(all_weekly, ignore_index=True) if all_weekly else pd.DataFrame()
        del all_weekly

        rs_ranks = pd.concat(all_rs_composite, ignore_index=True) if all_rs_composite else pd.DataFrame()
        del all_rs_composite
        if not rs_ranks.empty:
            rs_ranks["rs_rank_pct"] = rs_ranks["rs_composite"].rank(method="dense", pct=True, ascending=False) * 100.0
            weekly_bars = weekly_bars.merge(rs_ranks[["symbol", "rs_rank_pct", "rs_composite"]], on="symbol", how="left")
        del rs_ranks

        results = self._evaluate_symbols(weekly_bars, market_state)
        del weekly_bars
        if not results.empty:
            results = results.merge(universe[["symbol", "name"]], on="symbol", how="left")

            # ── Industry RS (memory-light: uses results, not full weekly bars) ──
            from winstan.rules.industry_rs import compute_industry_data
            industry_data = compute_industry_data(results, market_weekly)
            if not industry_data.empty:
                results = results.merge(industry_data, on="symbol", how="left")

            # 获取补充数据（股东人数/北向资金/资金流）并合并到结果
            results = fetch_supplemental_data(results)
            results = apply_stage2_scoring(results, self.config)

        candidates, top_n = score_and_rank(results, self.config)
        stage2_top_n = build_stage2_top_n(results, self.config)
        summary = self._build_summary(symbols, results, candidates, market_state, stage2_top_n)

        export_results(self.config, results, candidates, top_n, stage2_top_n, summary)
        self.duckdb_store.write_results("screening_results", results)

        return {
            "results": results,
            "candidates": candidates,
            "top_n": top_n,
            "stage2_top_n": stage2_top_n,
            "summary": summary,
        }

    def _load_raw_universe(self) -> pd.DataFrame:
        if self.config.universe.mode == "custom_list":
            return pd.DataFrame(
                {
                    "symbol": self.config.universe.custom_symbols,
                    "name": [None] * len(self.config.universe.custom_symbols),
                    "market": [None] * len(self.config.universe.custom_symbols),
                    "list_date": [pd.NaT] * len(self.config.universe.custom_symbols),
                    "is_st": [False] * len(self.config.universe.custom_symbols),
                }
            )
        return self.router.fetch_stock_universe()

    def _ensure_stock_cache(self, symbols: list[str]) -> None:
        missing = symbols if self.config.data.force_refresh else [
            symbol for symbol in symbols if not self.parquet_store.has_symbol("daily_bars", symbol)
        ]
        for batch in _chunked(missing, self.config.data.batch_size):
            try:
                frame = self.router.fetch_daily_bars(
                    batch,
                    start_date=self.config.data.effective_start_date,
                    end_date=self.config.data.effective_end_date,
                )
            except Exception as exc:
                print(f"stock batch skipped: {batch[:3]}... ({len(batch)} symbols), reason={exc}")
                continue
            frame = clean_daily_bars(frame)
            for symbol, group in frame.groupby("symbol", sort=False):
                self.parquet_store.write_symbol_frame("daily_bars", symbol, group)

        self.duckdb_store.refresh_parquet_view("daily_bars", str(self.config.parquet_root / "daily_bars" / "*.parquet"))

    def _ensure_index_cache(self, symbol: str) -> None:
        cached = clean_daily_bars(self.parquet_store.read_symbol_frame("index_bars", symbol))
        needs_refresh = (
            self.config.data.force_refresh
            or cached.empty
            or len(cached) < MIN_MARKET_HISTORY_ROWS
        )
        if needs_refresh:
            try:
                frame = self.router.fetch_index_daily_bars(
                    symbol,
                    start_date=self.config.data.effective_start_date,
                    end_date=self.config.data.effective_end_date,
                )
            except Exception as exc:
                print(f"index fetch skipped: {symbol}, reason={exc}")
                frame = pd.DataFrame()
            frame = clean_daily_bars(frame)
            self.parquet_store.write_symbol_frame("index_bars", symbol, frame)

        self.duckdb_store.refresh_parquet_view("index_bars", str(self.config.parquet_root / "index_bars" / "*.parquet"))

    def _evaluate_symbols(self, weekly_bars: pd.DataFrame, market_state: dict[str, object]) -> pd.DataFrame:
        records: list[dict[str, object]] = []
        for symbol, group in weekly_bars.groupby("symbol", sort=False):
            recent = group.sort_values("trade_date").reset_index(drop=True)
            latest = recent.iloc[-1]

            stage_info = evaluate_stage(latest, recent, self.config)
            volume_info = evaluate_volume(recent.tail(max(self.config.strategy.volume_avg_weeks, 3)))
            rs_info = evaluate_relative_strength(latest, self.config)
            resistance_info = evaluate_resistance(recent, latest, self.config)
            breakout_info = evaluate_breakout(latest, self.config)

            record = {
                "symbol": symbol,
                "trade_date": latest["trade_date"],
                "close": float(latest["close"]),
                "market_ok": bool(market_state["market_ok"]) if self.config.market.use_market_filter else True,
                **stage_info,
                **volume_info,
                **rs_info,
                **resistance_info,
                **breakout_info,
                "price_vs_ma_pct": float(latest["price_vs_ma_pct"]) if pd.notna(latest["price_vs_ma_pct"]) else None,
                "ma_30w": float(latest["ma_30w"]) if pd.notna(latest["ma_30w"]) else None,
                "ma_10w": float(latest["ma_10w"]) if pd.notna(latest["ma_10w"]) else None,
                "ma_spread_pct": float(latest["ma_spread_pct"]) if pd.notna(latest["ma_spread_pct"]) else None,
                "base_range_pct": float(latest["base_range_pct"]) if pd.notna(latest["base_range_pct"]) else None,
                "base_close_std_pct": float(latest["base_close_std_pct"]) if pd.notna(latest["base_close_std_pct"]) else None,
                "stage2_score": float(stage_info["stage2_score"]),
                "stage2_reason": stage_info["stage2_reason"],
                # RS components needed for industry-level computation
                "rs_26w_return": float(latest["rs_26w_return"]) if "rs_26w_return" in latest and pd.notna(latest["rs_26w_return"]) else None,
            }
            record["reject_reason"] = self._build_reject_reason(record)
            records.append(record)

        return pd.DataFrame(records)

    @staticmethod
    def _build_reject_reason(record: dict[str, object]) -> str:
        reasons: list[str] = []
        for key, label in [
            ("market_ok", "market"),
            ("stage2_candidate", "stage"),
            ("volume_ok", "volume"),
            ("rs_ok", "relative_strength"),
            ("resistance_ok", "resistance"),
            ("breakout_ok", "breakout"),
        ]:
            if not record.get(key, False):
                reasons.append(label)
        return ",".join(reasons) if reasons else ""

    def _build_summary(
        self,
        symbols: list[str],
        results: pd.DataFrame,
        candidates: pd.DataFrame,
        market_state: dict[str, object],
        stage2_top_n: pd.DataFrame,
    ) -> dict[str, object]:
        summary = {
            "total_symbols": len(symbols),
            "market_ok": market_state.get("market_ok"),
            "stage2_count": int(results["stage2_candidate"].sum()) if not results.empty else 0,
            "stage2_top_count": len(stage2_top_n),
            "volume_ok_count": int(results["volume_ok"].sum()) if not results.empty else 0,
            "rs_ok_count": int(results["rs_ok"].sum()) if not results.empty else 0,
            "resistance_ok_count": int(results["resistance_ok"].sum()) if not results.empty else 0,
            "candidate_count": len(candidates),
            "config_snapshot": {
                "benchmark_symbol": self.config.market.benchmark_symbol,
                "top_n": self.config.ranking.top_n,
                "resistance_min_headroom_pct": self.config.strategy.resistance_min_headroom_pct,
                "enable_breakout_filter": self.config.strategy.enable_breakout_filter,
            },
        }
        return summary
