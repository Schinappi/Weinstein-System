from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from winstan.dashboard.box_chart import _compute_box_daily_boundaries
from threading import RLock, Thread

import pandas as pd

from winstan.adapters.factory import DataSourceRouter
from winstan.calendar.trading_calendar import clean_daily_bars
from winstan.config import AppConfig, load_config
from winstan.indicators.core import compute_weekly_indicators
from winstan.llm.deepseek import DeepSeekError, build_detail_analysis
from winstan.outputs.explanations import build_weinstein_analysis, get_trend_stage_label, get_watch_rank_label
from winstan.pipeline.screener import WeinsteinScreener
from winstan.pipeline.universe import build_universe
from winstan.resample.weekly_builder import build_weekly_bars
from winstan.rules.breakout_rule import evaluate_breakout
from winstan.rules.demand_support import compute_demand_support_quality
from winstan.rules.market_trend import evaluate_market_trend
from winstan.rules.relative_strength_rule import evaluate_relative_strength
from winstan.rules.resistance_rule import evaluate_resistance, compute_overhead_supply
from winstan.rules.stage_analysis import apply_stage2_scoring, evaluate_stage
from winstan.rules.volume_confirmation import evaluate_volume
from winstan.scoring.ranker import build_quasi_stage2_top_n, build_stage2_top_n, score_and_rank
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore
from winstan.storage.price_monitor_store import PriceMonitorStore
from winstan.storage.watchlist_store import WatchlistStore
from winstan.signals.trade_watch import (
    build_trade_watch_signal,
    _resolve_target_entry_price,
    _resolve_stop_loss_reference,
)
from winstan.scoring.fundamental import get_fundamental_for_symbol
from winstan.dashboard.overview_store import OverviewStore

STAGE_LABELS = {
    "I": "阶段I",
    "II": "阶段II",
    "III": "阶段III",
    "IV": "阶段IV",
    "UNKNOWN": "未知",
}

QUASI_GATE_LABELS = {
    "stage2_candidate": "阶段结构",
    "volume_ok": "量能确认",
    "rs_ok": "相对强弱",
    "resistance_ok": "上方空间",
    "breakout_ok": "突破位置",
}


OVERVIEW_SNAPSHOT_VERSION = 7


class DashboardService:
    def __init__(self, config_path: str | Path) -> None:
        self.config = load_config(Path(config_path))
        self.parquet_store = ParquetStore(self.config.parquet_root)
        self.duckdb_store = DuckDBStore(self.config.duckdb_path)
        self.watchlist_store = WatchlistStore(self.config.duckdb_path)
        self.price_monitor_store = PriceMonitorStore(self.config.duckdb_path)
        self.overview_store = OverviewStore(self.config.logs_dir / "overview_rankings")
        self._router: DataSourceRouter | None = None
        self._results: pd.DataFrame | None = None
        self._stage1: pd.DataFrame | None = None
        self._stage2: pd.DataFrame | None = None
        self._quasi_stage2: pd.DataFrame | None = None
        self._universe: pd.DataFrame | None = None
        self._detail_analysis_cache: dict[str, str] = {}
        self._recommendations: list[dict[str, object]] | None = None
        self._lock = RLock()
        self._refresh_lock = RLock()
        self._refresh_running: bool = False
        self._refresh_result: dict[str, object] = {"status": "idle"}

    @property
    def router(self) -> DataSourceRouter:
        if self._router is None:
            self._router = DataSourceRouter(self.config)
        return self._router

    def _get_update_status(self) -> dict[str, object]:
        status_path = self.config.logs_dir / "incremental_update_status.json"
        if not status_path.exists() or not status_path.is_file():
            return {
                "exists": False,
                "success": False,
                "stale": True,
                "level": "warn",
                "title": "尚未执行增量更新",
                "message": "请先运行 scripts/update_daily_bars.py 生成最新缓存。",
                "finished_at": "",
                "latest_trade_date": "",
                "total_runtime_seconds": None,
                "stock_update_runtime_seconds": None,
                "phase1_runtime_seconds": None,
                "error": "",
                "phase1_requested": False,
                "phase1_ran": False,
                "phase1_results_count": None,
                "phase1_candidate_count": None,
                "phase1_stage1_count": None,
                "phase1_stage2_count": None,
                "stock_symbols_updated": None,
                "stock_rows_added": None,
                "index_updated": None,
                "index_rows_added": None,
                "skipped_non_trading_day": False,
                "raw_payload_text": "",
            }

        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "exists": True,
                "success": False,
                "stale": True,
                "level": "warn",
                "title": "更新状态读取失败",
                "message": str(exc),
                "finished_at": "",
                "latest_trade_date": "",
                "total_runtime_seconds": None,
                "stock_update_runtime_seconds": None,
                "phase1_runtime_seconds": None,
                "error": str(exc),
                "phase1_requested": False,
                "phase1_ran": False,
                "phase1_results_count": None,
                "phase1_candidate_count": None,
                "phase1_stage1_count": None,
                "phase1_stage2_count": None,
                "stock_symbols_updated": None,
                "stock_rows_added": None,
                "index_updated": None,
                "index_rows_added": None,
                "skipped_non_trading_day": False,
                "raw_payload_text": "",
            }

        success = _to_bool(payload.get("success"))
        finished_at = _to_text(payload.get("finished_at"))
        latest_trade_date = _first_text(payload.get("phase1_latest_trade_date"), payload.get("end_date"))
        stale = self._is_update_status_stale(success, finished_at)
        if not success:
            title = "最近一次更新失败"
            message = _first_text(payload.get("error"), "请检查 logs/incremental_update_status.json")
        elif stale:
            title = "缓存可能已过期"
            message = "最近一次更新距今超过 36 小时，建议重新执行增量更新。"
        else:
            title = "缓存状态正常"
            message = "最近一次增量更新执行成功。"

        return {
            "exists": True,
            "success": success,
            "stale": stale,
            "level": "warn" if stale or not success else "ok",
            "title": title,
            "message": message,
            "finished_at": finished_at,
            "latest_trade_date": latest_trade_date,
            "total_runtime_seconds": _to_float(payload.get("total_runtime_seconds")),
            "stock_update_runtime_seconds": _to_float(payload.get("stock_update_runtime_seconds")),
            "phase1_runtime_seconds": _to_float(payload.get("phase1_runtime_seconds")),
            "error": _to_text(payload.get("error")),
            "phase1_requested": _to_bool(payload.get("phase1_requested")),
            "phase1_ran": _to_bool(payload.get("phase1_ran")),
            "phase1_results_count": _to_int(payload.get("phase1_results_count")),
            "phase1_candidate_count": _to_int(payload.get("phase1_candidate_count")),
            "phase1_stage1_count": _to_int(payload.get("phase1_stage1_count")),
            "phase1_stage2_count": _to_int(payload.get("phase1_stage2_count")),
            "stock_symbols_updated": _to_int(payload.get("stock_symbols_updated")),
            "stock_rows_added": _to_int(payload.get("stock_rows_added")),
            "index_updated": payload.get("index_updated"),
            "index_rows_added": _to_int(payload.get("index_rows_added")),
            "phase1_skipped_reason": _to_text(payload.get("phase1_skipped_reason")),
            "skipped_non_trading_day": _to_bool(payload.get("skipped_non_trading_day")),
            "raw_payload_text": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        }

    @staticmethod
    def _is_update_status_stale(success: bool, finished_at: str) -> bool:
        if not success or not finished_at:
            return True
        finished = pd.to_datetime(finished_at, errors="coerce", utc=True)
        if pd.isna(finished):
            return True
        return (pd.Timestamp.now(tz="UTC") - finished) > pd.Timedelta(hours=36)

    def refresh_ranking_cache(self) -> dict[str, object]:
        """Clear in-memory ranking caches so the next read re-computes from DuckDB."""
        with self._lock:
            self._stage1 = None
            self._stage2 = None
            self._quasi_stage2 = None
            self._results = None
            self._universe = None
            self._recommendations = None
        return {"refreshed": True}

    def get_dashboard_payload(self) -> dict[str, object]:
        results = self.get_results()
        stage1, stage2, quasi_stage2 = self.get_rankings()
        stage2_tracking = self._build_stage2_tracking_summary()
        return {
            "summary": {
                "total_symbols": int(len(results)),
                "candidate_count": int(results["stage2_candidate"].sum()) if not results.empty else 0,
                "stage1_count": int(len(stage1)),
                "stage2_count": int(len(stage2)),
                "quasi_stage2_count": int(len(quasi_stage2)),
                "watching_count": int(stage2_tracking["watching_count"]),
                "holding_count": int(stage2_tracking["holding_count"]),
            },
            "update_status": self._get_update_status(),
            "snapshot_dates": self.get_available_dates(),
            "stage1": self._serialize_stage1(stage1),
            "stage2": self._serialize_stage2(stage2),
            "quasi_stage2": self._serialize_quasi_stage2(quasi_stage2),
            "stage2_tracking": stage2_tracking,
        }

    def get_navigation_payload(self) -> dict[str, object]:
        return {
            "items": [
                {"key": "dashboard", "label": "总览"},
                {"key": "watchlist", "label": "Stage2 监控"},
                {"key": "holdings", "label": "持有看板"},
            ]
        }

    def get_shareholder_ranking_payload(self) -> dict[str, object]:
        """读取股东人数排行榜（从 DuckDB shareholder_ranking 表）"""
        store = DuckDBStore(self.config.duckdb_path)
        rows: list[dict[str, object]] = []
        try:
            with store.connect() as conn:
                df = conn.execute("""
                    SELECT symbol, name, rank, holder_num_latest, holder_num_prev,
                           holder_change_pct, holder_change_score,
                           latest_quarter, prev_quarter,
                           final_score, stage_label, stage2_candidate,
                           combined_score, weinstein_available, scan_date
                    FROM shareholder_ranking
                    ORDER BY combined_score DESC
                    LIMIT 50
                """).fetchdf()
            for _, r in df.iterrows():
                rows.append({
                    "symbol": _to_text(r.get("symbol")),
                    "name": _to_text(r.get("name")),
                    "rank": _to_int(r.get("rank")),
                    "holder_num_latest": _to_int(r.get("holder_num_latest")),
                    "holder_num_prev": _to_int(r.get("holder_num_prev")),
                    "holder_change_pct": _to_float(r.get("holder_change_pct")),
                    "holder_change_score": _to_float(r.get("holder_change_score")),
                    "latest_quarter": _to_text(r.get("latest_quarter")),
                    "prev_quarter": _to_text(r.get("prev_quarter")),
                    "final_score": _to_float(r.get("final_score")),
                    "stage_label": _to_text(r.get("stage_label")),
                    "stage2_candidate": _to_bool(r.get("stage2_candidate")),
                    "combined_score": _to_float(r.get("combined_score")),
                    "weinstein_available": _to_bool(r.get("weinstein_available")),
                    "scan_date": _to_text(r.get("scan_date")),
                })
        except Exception as exc:
            return {"items": rows, "error": str(exc), "count": 0}
        return {"items": rows, "count": len(rows), "error": ""}

    def get_transition_ranking_payload(self) -> dict[str, object]:
        """读取 Stage1→2 转换候选排行榜（transition_candidate=True, 按 transition_score 降序）"""
        store = DuckDBStore(self.config.duckdb_path)
        rows: list[dict[str, object]] = []
        try:
            with store.connect() as conn:
                df = conn.execute("""
                    SELECT symbol, name, close, stage_label,
                           transition_score, transition_reason,
                           transition_base_weeks, transition_base_high,
                           transition_distance_pct, transition_volume_ratio,
                           final_score, headroom_pct, overhead_supply_pct,
                           rs_rank_pct,
                           base_quality_score, base_quality_grade, base_quality_reason
                    FROM screening_results
                    WHERE transition_candidate = TRUE
                    ORDER BY transition_score DESC
                    LIMIT 50
                """).fetchdf()
            for _, r in df.iterrows():
                rows.append({
                    "symbol": _to_text(r.get("symbol")),
                    "name": _to_text(r.get("name")),
                    "close": _to_float(r.get("close")),
                    "stage_label": _to_text(r.get("stage_label")),
                    "transition_score": _to_float(r.get("transition_score")),
                    "transition_reason": _to_text(r.get("transition_reason")),
                    "transition_base_weeks": _to_int(r.get("transition_base_weeks")),
                    "transition_base_high": _to_float(r.get("transition_base_high")),
                    "transition_distance_pct": _to_float(r.get("transition_distance_pct")),
                    "transition_volume_ratio": _to_float(r.get("transition_volume_ratio")),
                    "final_score": _to_float(r.get("final_score")),
                    "headroom_pct": _to_float(r.get("headroom_pct")),
                    "overhead_supply_pct": _to_float(r.get("overhead_supply_pct")),
                    "rs_rank_pct": _to_float(r.get("rs_rank_pct")),
                    "base_quality_score": _to_float(r.get("base_quality_score")),
                    "base_quality_grade": _to_text(r.get("base_quality_grade")),
                    "base_quality_reason": _to_text(r.get("base_quality_reason")),
                })
        except Exception as exc:
            return {"items": rows, "error": str(exc), "count": 0}
        return {"items": rows, "count": len(rows), "error": ""}

    def get_continuation_ranking_payload(self) -> dict[str, object]:
        """读取 Stage2 续涨候选排行榜（cont_is_applicable=True, 按 cont_quality_score 降序）"""
        store = DuckDBStore(self.config.duckdb_path)
        rows: list[dict[str, object]] = []
        try:
            with store.connect() as conn:
                df = conn.execute("""
                    SELECT symbol, name, close, stage_label,
                           cont_quality_score, cont_quality_grade, cont_quality_reason,
                           cont_ma30w_slope_10w, cont_pullback_pct,
                           cont_box_range_pct, cont_box_duration_weeks,
                           cont_box_touch_count, cont_box_penetration_pct,
                           cont_volume_trend_ok, cont_atr_rank_pct,
                           base_quality_score, base_quality_grade,
                           final_score, rs_rank_pct, headroom_pct
                    FROM screening_results
                    WHERE cont_is_applicable = TRUE
                      AND cont_score_box > 0
                      AND cont_prior_trend_ok = TRUE
                    ORDER BY cont_quality_score DESC
                    LIMIT 50
                """).fetchdf()
            for _, r in df.iterrows():
                rows.append({
                    "symbol": _to_text(r.get("symbol")),
                    "name": _to_text(r.get("name")),
                    "close": _to_float(r.get("close")),
                    "stage_label": _to_text(r.get("stage_label")),
                    "cont_quality_score": _to_float(r.get("cont_quality_score")),
                    "cont_quality_grade": _to_text(r.get("cont_quality_grade")),
                    "cont_quality_reason": _to_text(r.get("cont_quality_reason")),
                    "cont_ma30w_slope_10w": _to_float(r.get("cont_ma30w_slope_10w")),
                    "cont_pullback_pct": _to_float(r.get("cont_pullback_pct")),
                    "cont_box_range_pct": _to_float(r.get("cont_box_range_pct")),
                    "cont_box_duration_weeks": _to_int(r.get("cont_box_duration_weeks")),
                    "cont_box_touch_count": _to_int(r.get("cont_box_touch_count")),
                    "cont_box_penetration_pct": _to_float(r.get("cont_box_penetration_pct")),
                    "cont_volume_trend_ok": _to_bool(r.get("cont_volume_trend_ok")),
                    "cont_atr_rank_pct": _to_float(r.get("cont_atr_rank_pct")),
                    "base_quality_score": _to_float(r.get("base_quality_score")),
                    "base_quality_grade": _to_text(r.get("base_quality_grade")),
                    "final_score": _to_float(r.get("final_score")),
                    "rs_rank_pct": _to_float(r.get("rs_rank_pct")),
                    "headroom_pct": _to_float(r.get("headroom_pct")),
                })
        except Exception as exc:
            return {"items": rows, "error": str(exc), "count": 0}
        return {"items": rows, "count": len(rows), "error": ""}

    def get_demand_support_ranking_payload(self) -> dict[str, object]:
        """Read stocks with strong repeated demand-zone support."""
        store = DuckDBStore(self.config.duckdb_path)
        rows: list[dict[str, object]] = []
        try:
            with store.connect() as conn:
                df = conn.execute("""
                    SELECT symbol, name, close, stage_label,
                           demand_support_score, demand_support_grade,
                           demand_support_reason, demand_support_price,
                           demand_support_lower, demand_support_upper,
                           demand_support_touch_count,
                           demand_support_success_count,
                           demand_support_success_rate,
                           demand_support_avg_rebound_pct,
                           demand_support_avg_penetration_pct,
                           demand_support_max_penetration_pct,
                           demand_support_box_height_pct,
                           demand_support_duration_weeks,
                           demand_support_latest_touch_date,
                           demand_support_avg_touch_volume_ratio,
                           base_quality_score, base_quality_grade,
                           final_score, rs_rank_pct, headroom_pct
                    FROM screening_results
                    WHERE demand_support_candidate = TRUE
                    ORDER BY demand_support_score DESC,
                             demand_support_touch_count DESC,
                             demand_support_duration_weeks DESC
                    LIMIT 50
                """).fetchdf()
            for _, r in df.iterrows():
                rows.append({
                    "symbol": _to_text(r.get("symbol")),
                    "name": _to_text(r.get("name")),
                    "close": _to_float(r.get("close")),
                    "stage_label": _to_text(r.get("stage_label")),
                    "demand_support_score": _to_float(r.get("demand_support_score")),
                    "demand_support_grade": _to_text(r.get("demand_support_grade")),
                    "demand_support_reason": _to_text(r.get("demand_support_reason")),
                    "demand_support_price": _to_float(r.get("demand_support_price")),
                    "demand_support_lower": _to_float(r.get("demand_support_lower")),
                    "demand_support_upper": _to_float(r.get("demand_support_upper")),
                    "demand_support_touch_count": _to_int(r.get("demand_support_touch_count")),
                    "demand_support_success_count": _to_int(r.get("demand_support_success_count")),
                    "demand_support_success_rate": _to_float(r.get("demand_support_success_rate")),
                    "demand_support_avg_rebound_pct": _to_float(r.get("demand_support_avg_rebound_pct")),
                    "demand_support_avg_penetration_pct": _to_float(r.get("demand_support_avg_penetration_pct")),
                    "demand_support_max_penetration_pct": _to_float(r.get("demand_support_max_penetration_pct")),
                    "demand_support_box_height_pct": _to_float(r.get("demand_support_box_height_pct")),
                    "demand_support_duration_weeks": _to_int(r.get("demand_support_duration_weeks")),
                    "demand_support_latest_touch_date": _to_text(r.get("demand_support_latest_touch_date")),
                    "demand_support_avg_touch_volume_ratio": _to_float(r.get("demand_support_avg_touch_volume_ratio")),
                    "base_quality_score": _to_float(r.get("base_quality_score")),
                    "base_quality_grade": _to_text(r.get("base_quality_grade")),
                    "final_score": _to_float(r.get("final_score")),
                    "rs_rank_pct": _to_float(r.get("rs_rank_pct")),
                    "headroom_pct": _to_float(r.get("headroom_pct")),
                })
        except Exception as exc:
            return {"items": rows, "error": str(exc), "count": 0}
        return {"items": rows, "count": len(rows), "error": ""}

    def get_stage2_watchlist_payload(self) -> dict[str, object]:
        self.refresh_stage2_tracking()
        watchlist = self.watchlist_store.list_watchlist(["watching", "triggered", "expired", "cancelled"])
        active = watchlist[watchlist["status"].astype(str).isin(["watching", "triggered"])].copy() if not watchlist.empty else pd.DataFrame()
        if not active.empty:
            active = active.sort_values(["days_waited", "distance_to_entry_pct"], ascending=[True, True], na_position="last")
        return {
            "summary": self._build_stage2_tracking_summary(watchlist=watchlist),
            "items": self._serialize_watchlist(active),
        }

    def get_stage2_holdings_payload(self) -> dict[str, object]:
        self.refresh_stage2_tracking()
        holdings = self.watchlist_store.list_holdings(["holding"])
        if not holdings.empty:
            holdings = holdings.sort_values(["current_return_pct", "mfe_pct"], ascending=[False, False], na_position="last")
        return {
            "summary": self._build_holdings_summary(holdings),
            "items": self._serialize_holdings(holdings),
        }

    def delete_watch_item(self, watch_id: str) -> dict[str, object]:
        ok = self.watchlist_store.cancel_watch_item(watch_id)
        if not ok:
            raise ValueError(f"未找到监控记录: {watch_id}")
        return {"deleted": True, "id": watch_id}

    def delete_holding_item(self, holding_id: str) -> dict[str, object]:
        ok = self.watchlist_store.close_holding_item(holding_id)
        if not ok:
            raise ValueError(f"未找到持有记录: {holding_id}")
        return {"deleted": True, "id": holding_id}

    def add_stage2_watch(self, symbol: str) -> dict[str, object]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("股票代码不能为空")
        row = self._resolve_stock_row(normalized)
        if not _to_text(row.get("name")):
            row = row.copy()
            row["name"] = self._lookup_stock_name(normalized)
        signal = build_trade_watch_signal(row, self.config, watch_source="manual")
        created = self.watchlist_store.add_watch_item(self._prepare_watch_item(signal))
        return {"item": self._serialize_watchlist(pd.DataFrame([created]))[0]}

    def refresh_stage2_tracking(self) -> dict[str, object]:
        with self._lock:
            self._sync_auto_watch_candidates()
            self._refresh_watchlist_states()
            self._refresh_holding_states()
        watchlist = self.watchlist_store.list_watchlist(["watching", "triggered", "expired", "cancelled"])
        holdings = self.watchlist_store.list_holdings(["holding", "closed"])
        return {
            "watching_count": int((watchlist["status"].astype(str) == "watching").sum()) if not watchlist.empty else 0,
            "holding_count": int((holdings["status"].astype(str) == "holding").sum()) if not holdings.empty else 0,
        }

    def search_stocks(self, query: str, limit: int = 30) -> list[dict[str, object]]:
        universe = self.get_universe()
        if universe.empty:
            return []

        local = universe.copy()
        local["symbol"] = local["symbol"].fillna("").astype(str)
        local["name"] = local["name"].fillna("").astype(str)
        text = query.strip().lower()
        if text:
            mask = local["symbol"].str.lower().str.contains(text) | local["name"].str.lower().str.contains(text)
            local = local[mask]
        local = local.head(limit)

        results = self.get_results()
        result_symbols = set(results["symbol"].astype(str)) if not results.empty else set()
        stage1, stage2, _ = self.get_rankings()
        stage1_symbols = set(stage1["symbol"].astype(str)) if not stage1.empty else set()
        stage2_symbols = set(stage2["symbol"].astype(str)) if not stage2.empty else set()

        items: list[dict[str, object]] = []
        for _, row in local.iterrows():
            symbol = _to_text(row.get("symbol"))
            items.append(
                {
                    "symbol": symbol,
                    "name": _to_text(row.get("name")),
                    "in_results": symbol in result_symbols,
                    "in_stage1": symbol in stage1_symbols,
                    "in_stage2": symbol in stage2_symbols,
                }
            )
        return items

    def get_stock_detail(self, symbol: str, chart_type: str = "weekly") -> dict[str, object]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("股票代码不能为空")

        row = self._resolve_stock_row(normalized)
        name = _first_text(row.get("name"), self._lookup_stock_name(normalized))

        daily = self._ensure_daily_bars(normalized)
        if daily.empty:
            raise ValueError(f"未找到 {normalized} 的行情数据")

        # Re-evaluate resistance and breakout_level with fresh weekly data
        # to avoid stale cache. Also restores the original rolling breakout_level
        # which was incorrectly overwritten by base_breakout_price.
        try:
            weekly = compute_weekly_indicators(build_weekly_bars(daily), pd.DataFrame(), self.config)
            if not weekly.empty:
                recent = weekly.sort_values("trade_date").reset_index(drop=True)
                latest = recent.iloc[-1]
                base_bp = _to_float(row.get("base_breakout_price"))
                fresh_resistance = evaluate_resistance(recent, latest, self.config, base_breakout_price=base_bp)
                row = row.copy()
                # Sync close to fresh data (intraday snapshot may be newer than cache)
                row["close"] = float(latest["close"])
                row["headroom_pct"] = fresh_resistance.get("headroom_pct")
                row["nearest_resistance"] = fresh_resistance.get("nearest_resistance")
                row["resistance_ok"] = fresh_resistance.get("resistance_ok")
                # Restore original rolling breakout_level (dynamic pressure)
                # and store effective reference separately
                raw_bl = latest.get("breakout_level")
                if pd.notna(raw_bl) and float(raw_bl) != 0:
                    row["breakout_level"] = float(raw_bl)
                row["breakout_ref_price"] = base_bp if base_bp else (float(raw_bl) if pd.notna(raw_bl) else None)
                # Compute overhead supply from daily bars
                overhead_info = compute_overhead_supply(daily)
                row["overhead_supply_pct"] = overhead_info.get("overhead_supply_pct")
                row["overhead_supply_ok"] = overhead_info.get("overhead_supply_ok")
        except Exception:
            pass  # fall back to cached values

        stage1, stage2, _ = self.get_rankings()

        latest_trade_date = daily.sort_values("trade_date")["trade_date"].iloc[-1]

        return {
            "symbol": normalized,
            "name": name,
            "latest_close": _to_float(daily.sort_values("trade_date").iloc[-1].get("close")),
            "stage1_rank": self._lookup_rank(stage1, normalized, "top_n_rank"),
            "stage2_rank": self._lookup_rank(stage2, normalized, "stage2_top_n_rank"),
            "metrics": self._build_metrics(row, latest_trade_date=latest_trade_date),
            "chart": self._build_chart_payload(daily, row, chart_type),
            "fundamental": self._get_fundamental_data(row, normalized),
        }

    def load_overview_snapshot(self, target_date: str) -> dict[str, object] | None:
        payload = self.overview_store.load(target_date)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("snapshot_version") or 0) != OVERVIEW_SNAPSHOT_VERSION:
            return None
        items = payload.get("items")
        if not isinstance(items, list):
            return None
        return payload

    def save_overview_snapshot(self, target_date: str, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return
        self.overview_store.save(target_date, {**payload, "snapshot_version": OVERVIEW_SNAPSHOT_VERSION})

    def get_price_monitor_payload(self) -> dict[str, object]:
        frame = self.price_monitor_store.list_items()
        if frame.empty:
            return {"items": [], "count": 0}

        refreshed_rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            item = row.to_dict()
            symbol = _to_text(item.get("symbol")).upper()
            if symbol:
                daily = self._ensure_daily_bars(symbol)
                if not daily.empty:
                    latest = daily.sort_values("trade_date").iloc[-1]
                    latest_close = _to_float(latest.get("close"))
                    target_price = _to_float(item.get("target_price"))
                    item["latest_trade_date"] = latest.get("trade_date")
                    item["latest_close"] = latest_close
                    if latest_close is not None and target_price is not None:
                        item["distance_amount"] = target_price - latest_close
                        item["distance_pct"] = (target_price / latest_close - 1.0) * 100.0 if latest_close else None
                    self.price_monitor_store.update_item(_to_text(item.get("id")), item)
            refreshed_rows.append(item)

        refreshed = pd.DataFrame(refreshed_rows)
        return {
            "count": int(len(refreshed)),
            "items": self._serialize_price_monitors(refreshed),
        }

    def add_price_monitor(self, symbol: str, target_price: float) -> dict[str, object]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("股票代码不能为空")
        if target_price <= 0:
            raise ValueError("目标价格必须大于 0")

        daily = self._ensure_daily_bars(normalized)
        if daily.empty:
            raise ValueError(f"未找到 {normalized} 的行情数据")

        latest = daily.sort_values("trade_date").iloc[-1]
        latest_close = _to_float(latest.get("close"))
        payload = {
            "symbol": normalized,
            "name": self._lookup_stock_name(normalized),
            "target_price": float(target_price),
            "latest_trade_date": latest.get("trade_date"),
            "latest_close": latest_close,
            "distance_amount": (float(target_price) - latest_close) if latest_close is not None else None,
            "distance_pct": ((float(target_price) / latest_close) - 1.0) * 100.0 if latest_close not in {None, 0.0} else None,
        }
        item = self.price_monitor_store.add_item(payload)
        return {"item": self._serialize_price_monitors(pd.DataFrame([item]))[0]}

    def delete_price_monitor(self, item_id: str) -> dict[str, object]:
        ok = self.price_monitor_store.delete_item(item_id)
        if not ok:
            raise ValueError(f"未找到监控记录: {item_id}")
        return {"deleted": True, "id": item_id}

    def _get_fundamental_data(self, row: pd.Series, symbol: str) -> dict[str, object]:
        """Return fundamental data from row cache, or fetch live from Tushare if missing.

        Checks detail fields (holder_change_pct, nb_ratio, net_mf_amount) to
        distinguish \"real 0 score after API call\" from \"no API was ever called
        for this stock\" (e.g. non-candidate stocks where default 0.0 was filled).
        """
        holder_chg = _to_float(row.get("holder_change_pct"))
        nb_ratio = _to_float(row.get("nb_ratio"))
        mf_amt = _to_float(row.get("net_mf_amount"))

        # If any detail field has real data, use the cached row
        if (holder_chg is not None
                or nb_ratio is not None
                or mf_amt is not None):
            return {
                "holder_score": _to_float(row.get("holder_score")),
                "holder_change_pct": holder_chg,
                "holder_num": _to_float(row.get("holder_num")),
                "nb_score": _to_float(row.get("nb_score")),
                "nb_ratio": nb_ratio,
                "nb_vol_chg_5d": _to_float(row.get("nb_vol_chg_5d")),
                "nb_vol_chg_10d": _to_float(row.get("nb_vol_chg_10d")),
                "nb_vol_chg_20d": _to_float(row.get("nb_vol_chg_20d")),
                "moneyflow_confirm": _to_float(row.get("moneyflow_confirm")),
                "net_mf_amount": mf_amt,
            }

        # Otherwise fetch on-demand (for searched stocks not in screening results)
        return self._fetch_live_fundamental(symbol)

    def _fetch_live_fundamental(self, symbol: str) -> dict[str, object]:
        """Read fundamental data from DuckDB batch cache (no API calls).

        The batch cache is populated by ``fetch_supplemental_data()`` during
        Weinstein screening — 3 API calls total for ALL A-shares.
        """
        return get_fundamental_for_symbol(symbol)

    def get_stock_analysis(self, symbol: str) -> dict[str, object]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("股票代码不能为空")

        row = self._resolve_stock_row(normalized)
        daily = self._ensure_daily_bars(normalized)
        if not daily.empty:
            row = row.copy()
            row["trade_date"] = daily.sort_values("trade_date")["trade_date"].iloc[-1]
        if not _to_text(row.get("name")):
            row = row.copy()
            row["name"] = self._lookup_stock_name(normalized)
        return {
            "symbol": normalized,
            "analysis": self._build_detail_analysis(normalized, row),
        }

    def get_results(self) -> pd.DataFrame:
        with self._lock:
            if self._results is not None:
                return self._results.copy()

            results = self._read_results_from_duckdb()
            if results.empty:
                # 禁止自动触发完整筛选 (内存密集, 易 OOM)
                # 完整筛选应由外部脚本手动触发
                pass

            if "trade_date" in results.columns:
                results["trade_date"] = pd.to_datetime(results["trade_date"], errors="coerce")
            results = apply_stage2_scoring(results, self.config)
            self._results = results.reset_index(drop=True)
            return self._results.copy()

    def get_rankings(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        with self._lock:
            if self._stage1 is not None and self._stage2 is not None and self._quasi_stage2 is not None:
                return self._stage1.copy(), self._stage2.copy(), self._quasi_stage2.copy()

            results = self.get_results()
            _, stage1 = score_and_rank(results, self.config)
            stage2 = build_stage2_top_n(results, self.config)
            quasi_stage2 = build_quasi_stage2_top_n(results, self.config)
            self._stage1 = stage1.reset_index(drop=True)
            self._stage2 = stage2.reset_index(drop=True)
            self._quasi_stage2 = quasi_stage2.reset_index(drop=True)
            return self._stage1.copy(), self._stage2.copy(), self._quasi_stage2.copy()

    def get_available_dates(self) -> list[str]:
        return self.duckdb_store.list_snapshot_dates()

    def get_stage1_payload(self) -> dict[str, object]:
        results = self.get_results()
        _, stage1 = score_and_rank(results, self.config)
        return {"stage1": self._serialize_stage1(stage1.reset_index(drop=True))}

    def get_recommendations_payload(self) -> dict[str, object]:
        from winstan.patterns import compute_recommendations
        if hasattr(self, '_recommendations') and self._recommendations is not None:
            return {"recommendations": self._recommendations}
        # Compute without holding the main lock (recommendations is read-only once set)
        results = self.get_results()
        recs = compute_recommendations(results, self.config.parquet_root)
        self._recommendations = recs
        return {"recommendations": recs}

    def run_preclose(self) -> dict[str, object]:
        """手动触发：拉腾讯实时行情 → 注入今日日K → 跑完整 Weinstein 筛选 → 刷新排行缓存"""
        import subprocess
        import sys
        import time
        import tempfile
        import os

        t0 = time.time()
        script = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "preclose_and_phase1.py"
        # 用临时文件避免管道缓冲区死锁
        tmp = tempfile.NamedTemporaryFile(mode='w+', suffix='.txt', delete=False)
        tmp_path = tmp.name
        tmp.close()
        stdout = ""
        exit_code = -1
        try:
            with open(tmp_path, 'w') as f:
                proc = subprocess.run(
                    [sys.executable, str(script)],
                    stdout=f, stderr=subprocess.STDOUT, timeout=600,
                    cwd=script.parent.parent,
                )
                exit_code = proc.returncode
            with open(tmp_path) as f:
                stdout = f.read()
        except subprocess.TimeoutExpired:
            os.unlink(tmp_path)
            return {"success": False, "message": "预收盘+筛选超时（>600秒）", "elapsed_seconds": 600}
        except Exception as e:
            os.unlink(tmp_path)
            return {"success": False, "message": f"预收盘执行失败: {e}", "elapsed_seconds": time.time() - t0}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        elapsed = time.time() - t0
        exit_ok = exit_code == 0
        if not exit_ok:
            self.refresh_ranking_cache()
            self._recommendations = None
            return {
                "success": False,
                "message": f"预收盘脚本异常退出(code={exit_code})",
                "elapsed_seconds": round(elapsed, 1),
                "stderr": stdout[:500] if stdout else "",
            }

        # 解析 JSON 摘要行 `[preclose-summary] {...}`
        details = {}
        for line in stdout.split("\n"):
            line = line.strip()
            if line.startswith("[preclose-summary] "):
                try:
                    details = json.loads(line[len("[preclose-summary] "):])
                except Exception:
                    pass
                break

        # 刷新内存缓存
        self.refresh_ranking_cache()
        self._recommendations = None

        candidate_count = details.get("candidate_count", 0)
        stage2_count = details.get("stage2_count", 0)
        stage2_top = details.get("stage2_top_count", 0)
        total_symbols = details.get("total_symbols", 0)
        details_elapsed = details.get("elapsed_seconds", round(elapsed, 1))

        return {
            "success": True,
            "message": f"预收盘完成: {total_symbols}只股票, {candidate_count}候选, Stage II={stage2_count}(前{stage2_top})",
            "elapsed_seconds": round(elapsed, 1),
            "details": {
                "total_symbols": total_symbols,
                "candidate_count": candidate_count,
                "stage2_count": stage2_count,
                "stage2_top_count": stage2_top,
                "script_elapsed": details_elapsed,
            },
        }

    def _refresh_bg_worker(self, script: Path) -> None:
        """后台线程执行：拉最新K线 → 重跑续涨评分"""
        import subprocess, sys, time
        with self._refresh_lock:
            self._refresh_result = {"status": "running", "started_at": time.time()}
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=600,
                cwd=script.parent.parent,
            )
            exit_ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            with self._refresh_lock:
                self._refresh_running = False
                self._refresh_result = {"status": "failed", "message": "续涨刷新超时（>600秒）", "elapsed_seconds": 600}
            return
        except Exception as e:
            with self._refresh_lock:
                self._refresh_running = False
                self._refresh_result = {"status": "failed", "message": f"刷新异常: {e}", "elapsed_seconds": round(time.time() - t0, 1)}
            return

        elapsed = time.time() - t0
        self.refresh_ranking_cache()
        self._recommendations = None

        # 统计续涨结果
        results = self.get_results()
        cont_count = int((results.get("cont_is_applicable", pd.Series(dtype=bool)) & (results.get("cont_score_box", 0) > 0)).sum()) if not results.empty else 0

        with self._refresh_lock:
            self._refresh_running = False
            self._refresh_result = {
                "status": "completed" if exit_ok else "failed",
                "success": exit_ok,
                "message": f"续涨刷新完成: {cont_count}只有效箱体候选" if exit_ok else f"筛选异常退出(code={proc.returncode})",
                "elapsed_seconds": round(elapsed, 1),
                "continuation_count": cont_count,
                "exit_code": proc.returncode,
            }

    def refresh_continuation(self) -> dict[str, object]:
        """手动触发：拉最新K线 → 重跑续涨评分 → 刷新排行（后台异步）"""
        import time
        with self._refresh_lock:
            if self._refresh_running:
                return {"success": False, "message": "已有刷新任务在运行中，请稍后重试", "status": "busy"}

        script = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "run_batched_phase1.py"
        self._refresh_running = True
        self._refresh_result = {"status": "starting"}

        Thread(target=self._refresh_bg_worker, args=(script,), daemon=True).start()
        return {"success": True, "status": "started", "message": "续涨刷新已在后台启动，预计3-5分钟完成"}

    def get_refresh_status(self) -> dict[str, object]:
        """查询续涨刷新状态"""
        import time
        with self._refresh_lock:
            result = dict(self._refresh_result)
        result["running"] = self._refresh_running
        return result

    def get_rankings_by_date(self, dt: str) -> dict[str, object]:
        results = self.duckdb_store.read_snapshot(dt)
        if results.empty:
            return {}
        results = apply_stage2_scoring(results, self.config)
        _, stage1 = score_and_rank(results, self.config)
        stage2 = build_stage2_top_n(results, self.config)
        quasi_stage2 = build_quasi_stage2_top_n(results, self.config)
        return {
            "snapshot_date": dt,
            "summary": {
                "total_symbols": int(len(results)),
                "candidate_count": int(results["stage2_candidate"].sum()) if not results.empty else 0,
                "stage1_count": int(len(stage1)),
                "stage2_count": int(len(stage2)),
                "quasi_stage2_count": int(len(quasi_stage2)),
            },
            "stage1": self._serialize_stage1(stage1),
            "stage2": self._serialize_stage2(stage2),
            "quasi_stage2": self._serialize_quasi_stage2(quasi_stage2),
        }

    def get_universe(self) -> pd.DataFrame:
        with self._lock:
            if self._universe is not None:
                return self._universe.copy()

            try:
                raw_universe = self.router.fetch_stock_universe()
                universe = build_universe(raw_universe, self.config)
            except Exception:
                results = self.get_results()
                if results.empty:
                    universe = pd.DataFrame(columns=["symbol", "name"])
                else:
                    universe = results[["symbol", "name"]].drop_duplicates(subset=["symbol"]).reset_index(drop=True)

            self._universe = universe.reset_index(drop=True)
            return self._universe.copy()

    def _build_detail_analysis(self, symbol: str, row: pd.Series) -> str:
        cache_key = f"{symbol}:{_format_date(row.get('trade_date'))}"
        cached = self._detail_analysis_cache.get(cache_key)
        if cached:
            return cached
        try:
            analysis = build_detail_analysis(row, self.config)
        except DeepSeekError:
            analysis = build_weinstein_analysis(row, self.config)
        except Exception:
            analysis = build_weinstein_analysis(row, self.config)
        self._detail_analysis_cache[cache_key] = analysis
        return analysis

    def _resolve_stock_row(self, symbol: str) -> pd.Series:
        results = self.get_results()
        row_frame = results[results["symbol"].astype(str).str.upper() == symbol] if not results.empty else pd.DataFrame()
        if row_frame.empty:
            return self._build_single_symbol_result(symbol)
        return row_frame.iloc[0].copy()

    def _read_results_from_duckdb(self) -> pd.DataFrame:
        try:
            with self.duckdb_store.connect() as conn:
                return conn.execute("SELECT * FROM screening_results").fetchdf()
        except Exception:
            return pd.DataFrame()

    def _lookup_stock_name(self, symbol: str) -> str:
        universe = self.get_universe()
        matched = universe[universe["symbol"].astype(str).str.upper() == symbol]
        if matched.empty:
            return ""
        return _to_text(matched.iloc[0].get("name"))

    def _ensure_daily_bars(self, symbol: str) -> pd.DataFrame:
        # 1. 读历史日K
        cached = clean_daily_bars(self.parquet_store.read_symbol_frame("daily_bars", symbol))
        
        # 2. 如果有今日 intraday 快照，合并进去
        today_str = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
        intraday = self.parquet_store.read_intraday_snapshot(today_str)
        if not intraday.empty:
            today_rows = intraday[intraday["symbol"] == symbol].copy()
            if not today_rows.empty:
                today_rows["trade_date"] = pd.to_datetime(today_rows["trade_date"])
                # 如果历史已有今天的数据，先剔除
                if not cached.empty:
                    cached = cached[cached["trade_date"].dt.strftime("%Y-%m-%d") != today_str]
                merged = clean_daily_bars(pd.concat([cached, today_rows], ignore_index=True))
                if not merged.empty:
                    return merged
        
        if not cached.empty:
            return cached

        # 3. 缓存没有则从数据源拉取
        try:
            fetched = self.router.fetch_daily_bars(
                [symbol],
                start_date=self.config.data.effective_start_date,
                end_date=self.config.data.effective_end_date,
            )
        except Exception:
            fetched = pd.DataFrame()
        fetched = clean_daily_bars(fetched)
        if not fetched.empty:
            self.parquet_store.write_symbol_frame("daily_bars", symbol, fetched)
        return fetched

    def _ensure_market_daily_bars(self) -> pd.DataFrame:
        symbol = self.config.market.benchmark_symbol
        cached = clean_daily_bars(self.parquet_store.read_symbol_frame("index_bars", symbol))
        if not cached.empty:
            return cached

        try:
            fetched = self.router.fetch_index_daily_bars(
                symbol,
                start_date=self.config.data.effective_start_date,
                end_date=self.config.data.effective_end_date,
            )
        except Exception:
            fetched = pd.DataFrame()
        fetched = clean_daily_bars(fetched)
        if not fetched.empty:
            self.parquet_store.write_symbol_frame("index_bars", symbol, fetched)
        return fetched

    def _build_single_symbol_result(self, symbol: str) -> pd.Series:
        daily = self._ensure_daily_bars(symbol)
        if daily.empty:
            raise ValueError(f"未找到 {symbol} 的行情数据")

        market_daily = self._ensure_market_daily_bars()
        market_weekly = compute_weekly_indicators(build_weekly_bars(market_daily), build_weekly_bars(market_daily), self.config)
        market_state = evaluate_market_trend(market_weekly, self.config)

        weekly = compute_weekly_indicators(build_weekly_bars(daily), market_weekly, self.config)
        if weekly.empty:
            raise ValueError(f"{symbol} 的周线数据不足，无法分析")

        recent = weekly.sort_values("trade_date").reset_index(drop=True)
        latest = recent.iloc[-1].copy()
        stage_info = evaluate_stage(latest, recent, self.config)
        volume_info = evaluate_volume(recent.tail(max(self.config.strategy.volume_avg_weeks, 3)))
        rs_info = evaluate_relative_strength(pd.Series({"rs_rank_pct": None, "rs_line": latest.get("rs_line")}), self.config)
        resistance_info = evaluate_resistance(recent, latest, self.config, base_breakout_price=stage_info.get("base_breakout_price"))
        breakout_info = evaluate_breakout(
            latest, self.config,
            base_breakout_price=stage_info.get("base_breakout_price"),
        )
        demand_support_info = compute_demand_support_quality(recent, self.config, daily=daily)

        record = {
            "symbol": symbol,
            "name": self._lookup_stock_name(symbol),
            "trade_date": latest["trade_date"],
            "close": float(latest["close"]),
            "market_ok": bool(market_state.get("market_ok", False)) if self.config.market.use_market_filter else True,
            **stage_info,
            **volume_info,
            **rs_info,
            **resistance_info,
            **breakout_info,
            **demand_support_info,
            "price_vs_ma_pct": _to_float(latest.get("price_vs_ma_pct")),
            "ma_30w": _to_float(latest.get("ma_30w")),
            "ma_10w": _to_float(latest.get("ma_10w")),
            "ma_spread_pct": _to_float(latest.get("ma_spread_pct")),
            "base_range_pct": _to_float(latest.get("base_range_pct")),
            "base_close_std_pct": _to_float(latest.get("base_close_std_pct")),
            "stage2_score": float(stage_info["stage2_score"]),
            "stage2_reason": stage_info["stage2_reason"],
        }
        record["reject_reason"] = self._build_reject_reason(record)
        scored = apply_stage2_scoring(pd.DataFrame([record]), self.config)
        return scored.iloc[0].copy()

    def _build_metrics(self, row: pd.Series, latest_trade_date=None) -> list[dict[str, str]]:
        trade_date_value = latest_trade_date if latest_trade_date is not None else row.get("trade_date")
        return [
            {"label": "趋势阶段", "value": get_trend_stage_label(row, self.config)},
            {"label": "候选等级", "value": get_watch_rank_label(row)},
            {"label": "收盘价", "value": _format_number(row.get("close"))},
            {"label": "交易日期", "value": _format_date(trade_date_value)},
            {"label": "综合分", "value": _format_number(row.get("final_score"))},
            {"label": "结构分", "value": _format_number(row.get("structure_score"))},
            {"label": "时机分", "value": _format_number(row.get("timing_score"))},
            {"label": "强度分", "value": _format_number(row.get("strength_score"))},
            {"label": "风险分", "value": _format_number(row.get("risk_score"))},
            {"label": "量能比", "value": _format_number(row.get("volume_ratio"))},
            {"label": "RS排名", "value": _format_percent_rank(row.get("rs_rank_pct"))},
            {"label": "上方空间", "value": _format_percent(row.get("headroom_pct"))},
            {"label": "套牢盘", "value": _format_percent(row.get("overhead_supply_pct"))},
            {"label": "距突破位", "value": _format_percent(row.get("breakout_pct"))},
            {"label": "基底突破位", "value": _format_number(row.get("base_breakout_price"), "#0.00", "无基底")},
            {"label": "距基底突破", "value": _format_base_extension(row)},
            {"label": "基底质量", "value": _format_base_quality(row)},
            {"label": "行业RS", "value": _format_industry_rs(row.get("industry_rs_rank_pct"))},
            {"label": "行业广度", "value": _format_percent(row.get("industry_breadth"))},
            {"label": "拒绝原因", "value": _first_text(row.get("reject_reason"), "无")},
        ]

    def _build_chart_payload(self, daily: pd.DataFrame, row: pd.Series, chart_type: str = "weekly") -> dict[str, object]:
        if chart_type == "daily":
            frame = daily.sort_values("trade_date").tail(240).copy()
            frame["ema144"] = frame["close"].ewm(span=144, min_periods=1).mean()
            frame["ema169"] = frame["close"].ewm(span=169, min_periods=1).mean()
        else:
            from winstan.resample.weekly_builder import build_weekly_bars
            weekly = build_weekly_bars(daily.sort_values("trade_date"))
            frame = weekly.tail(260).copy()
            frame["ema30w"] = frame["close"].ewm(span=30, min_periods=1).mean()
        breakout_line = _to_float(row.get("breakout_level"))
        resistance_line = _to_float(row.get("nearest_resistance"))
        base_breakout_line = _to_float(row.get("base_breakout_price"))
        base_stop_line = _to_float(row.get("base_low"))

        # ── 续涨箱体边界 ──
        box_upper = box_lower = box_start_idx = box_end_idx = None
        cont_box_score = _to_float(row.get("cont_score_box"))
        if cont_box_score and cont_box_score > 0:
            try:
                from winstan.resample.weekly_builder import build_weekly_bars
                from winstan.rules.stage2_continuation import _score_box_discipline
                daily_sorted = daily.sort_values("trade_date")
                weekly = build_weekly_bars(daily_sorted)
                if not weekly.empty and len(weekly) >= 8:
                    _, box_info = _score_box_discipline(weekly)
                    if box_info.get("box_valid"):
                        a_h = box_info.get("box_a_h")
                        b_h = box_info.get("box_b_h")
                        a_l = box_info.get("box_a_l")
                        b_l = box_info.get("box_b_l")
                        bs_abs = box_info.get("box_start_idx", 0)
                        be_abs = box_info.get("box_end_idx", 0)
                        seg_off = box_info.get("box_seg_offset", 0)
                        if be_abs <= bs_abs:
                            be_abs = len(weekly) - 1
                        if a_h is not None and a_l is not None:
                            box_upper, box_lower, box_start_idx, box_end_idx = (
                                _compute_box_daily_boundaries(
                                    daily_sorted, weekly, a_h, b_h, a_l, b_l,
                                    bs_abs, be_abs, seg_off, len(frame)
                                )
                            )
            except Exception:
                pass  # box viz is best-effort

        items: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            items.append(
                {
                    "date": _format_date(row.get("trade_date")),
                    "open": _to_float(row.get("open")),
                    "high": _to_float(row.get("high")),
                    "low": _to_float(row.get("low")),
                    "close": _to_float(row.get("close")),
                    "volume": _to_float(row.get("volume")),
                    "ema30w": _to_float(row.get("ema30w")),
                    "ma144": _to_float(row.get("ma144")),
                    "ma169": _to_float(row.get("ma169")),
                    "ema144": _to_float(row.get("ema144")),
                    "ema169": _to_float(row.get("ema169")),
                }
            )
        return {
            "chart_type": chart_type,
            "candles": items,
            "breakout_line": breakout_line,
            "resistance_line": resistance_line,
            "base_breakout_line": base_breakout_line,
            "base_stop_line": base_stop_line,
            "box_upper": box_upper,
            "box_lower": box_lower,
            "box_start_idx": box_start_idx,
            "box_end_idx": box_end_idx,
        }

    def _serialize_stage1(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "rank": _to_int(row.get("top_n_rank")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "stage": _stage_label_text(row.get("stage_label")),
                    "close": _format_number(row.get("close")),
                    "watch_reason": _to_text(row.get("watch_reason")),
                    # 基底质量
                    "base_quality_score": _format_number(row.get("base_quality_score"), ".0f"),
                    "base_quality_grade": _to_text(row.get("base_quality_grade")),
                    "base_quality_reason": _to_text(row.get("base_quality_reason")),
                    "base_score_ma": _format_number(row.get("base_score_ma"), ".0f"),
                    "base_score_range": _format_number(row.get("base_score_range"), ".0f"),
                    "base_score_length": _format_number(row.get("base_score_length"), ".0f"),
                    "base_score_volume": _format_number(row.get("base_score_volume"), ".0f"),
                    "base_score_atr": _format_number(row.get("base_score_atr"), ".0f"),
                    "base_duration_weeks": _to_int(row.get("base_duration_weeks")),
                    "base_volume_contraction_ok": _to_bool(row.get("base_volume_contraction_ok")),
                    "base_atr_rank_pct": _to_int(row.get("base_atr_rank_pct")),
                    "demand_support_score": _format_number(row.get("demand_support_score"), ".0f"),
                    "demand_support_grade": _to_text(row.get("demand_support_grade")),
                    "demand_support_reason": _to_text(row.get("demand_support_reason")),
                    "demand_support_price": _format_number(row.get("demand_support_price")),
                    "demand_support_lower": _format_number(row.get("demand_support_lower")),
                    "demand_support_upper": _format_number(row.get("demand_support_upper")),
                    "demand_support_touch_count": _to_int(row.get("demand_support_touch_count")),
                    "demand_support_success_rate": _format_percent(row.get("demand_support_success_rate")),
                    "demand_support_avg_rebound_pct": _format_percent(row.get("demand_support_avg_rebound_pct")),
                    "demand_support_avg_penetration_pct": _format_percent(row.get("demand_support_avg_penetration_pct")),
                    "demand_support_box_height_pct": _format_percent(row.get("demand_support_box_height_pct")),
                    "demand_support_duration_weeks": _to_int(row.get("demand_support_duration_weeks")),
                    # 旧字段保留
                    "watch_score": _format_number(row.get("watch_score")),
                    "total_score": _format_number(row.get("total_score")),
                    "analysis": build_weinstein_analysis(row, self.config),
                }
            )
        return rows

    def _build_stage2_tracking_summary(self, watchlist: pd.DataFrame | None = None) -> dict[str, object]:
        local_watchlist = watchlist if watchlist is not None else self.watchlist_store.list_watchlist(["watching", "triggered", "expired", "cancelled"])
        holdings = self.watchlist_store.list_holdings(["holding", "closed"])
        watching_count = int((local_watchlist["status"].astype(str) == "watching").sum()) if not local_watchlist.empty else 0
        triggered_count = int((local_watchlist["status"].astype(str) == "triggered").sum()) if not local_watchlist.empty else 0
        expired_count = int((local_watchlist["status"].astype(str) == "expired").sum()) if not local_watchlist.empty else 0
        holding_count = int((holdings["status"].astype(str) == "holding").sum()) if not holdings.empty else 0
        return {
            "watching_count": watching_count,
            "triggered_count": triggered_count,
            "expired_count": expired_count,
            "holding_count": holding_count,
        }

    def _build_holdings_summary(self, holdings: pd.DataFrame) -> dict[str, object]:
        if holdings.empty:
            return {
                "holding_count": 0,
                "avg_return_pct": None,
                "median_mfe_pct": None,
                "median_mae_pct": None,
            }
        return {
            "holding_count": int(len(holdings)),
            "avg_return_pct": _series_mean(holdings.get("current_return_pct")),
            "median_mfe_pct": _series_median(holdings.get("mfe_pct")),
            "median_mae_pct": _series_median(holdings.get("mae_pct")),
        }

    def _serialize_watchlist(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        if frame.empty:
            return []
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "id": _to_text(row.get("id")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "watch_date": _format_date(row.get("watch_date")),
                    "expire_date": _format_date(row.get("expire_date")),
                    "status": _to_text(row.get("status")),
                    "days_waited": _to_int(row.get("days_waited")),
                    "target_entry_price": _format_number(row.get("target_entry_price")),
                    "latest_close": _format_number(row.get("latest_close")),
                    "distance_to_entry_pct": _format_percent(row.get("distance_to_entry_pct")),
                    "trigger_date": _format_date(row.get("trigger_date")),
                    "trigger_mode": _to_text(row.get("trigger_mode")),
                    "stage_label": _to_text(row.get("stage_label")),
                    "watch_rank_label": _to_text(row.get("watch_rank_label")),
                    "rs_rank_pct": _format_percent_rank(row.get("rs_rank_pct")),
                    "headroom_pct": _format_percent(row.get("headroom_pct")),
                    "volume_label": _to_text(row.get("volume_label")),
                }
            )
        return rows

    def _serialize_holdings(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        if frame.empty:
            return []
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "id": _to_text(row.get("id")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "entry_date": _format_date(row.get("entry_date")),
                    "entry_price": _format_number(row.get("entry_price")),
                    "latest_close": _format_number(row.get("latest_close")),
                    "current_return_pct": _format_percent(row.get("current_return_pct")),
                    "mfe_pct": _format_percent(row.get("mfe_pct")),
                    "mae_pct": _format_percent(row.get("mae_pct")),
                    "holding_days": _to_int(row.get("holding_days")),
                    "stage_label_latest": _to_text(row.get("stage_label_latest")),
                    "watch_rank_latest": _to_text(row.get("watch_rank_latest")),
                    "risk_flag": _to_text(row.get("risk_flag")),
                    "entry_mode": _to_text(row.get("entry_mode")),
                    "volume_confirmed_on_trigger": _to_bool(row.get("volume_confirmed_on_trigger")),
                }
            )
        return rows

    def _serialize_price_monitors(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        if frame.empty:
            return []
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "id": _to_text(row.get("id")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "target_price": _format_number(row.get("target_price")),
                    "latest_trade_date": _format_date(row.get("latest_trade_date")),
                    "latest_close": _format_number(row.get("latest_close")),
                    "distance_amount": _format_signed_number(row.get("distance_amount")),
                    "distance_pct": _format_signed_percent(row.get("distance_pct")),
                }
            )
        return rows

    def _sync_auto_watch_candidates(self) -> None:
        candidates = self._default_stage2_watch_pool()
        if candidates.empty:
            return
        active_watchlist = self.watchlist_store.list_watchlist(["watching", "triggered"])
        active_watch_map = {
            _to_text(row.get("symbol")).upper(): row.to_dict()
            for _, row in active_watchlist.iterrows()
            if _to_text(row.get("symbol"))
        }
        active_symbols = self.watchlist_store.list_active_symbols()
        for _, row in candidates.iterrows():
            symbol = _to_text(row.get("symbol")).upper()
            if not symbol:
                continue
            signal = build_trade_watch_signal(row, self.config, watch_source="stage2_auto")
            if symbol in active_watch_map:
                merged = self._merge_signal_into_watch_item(active_watch_map[symbol], signal)
                self.watchlist_store.update_watch_item(_to_text(active_watch_map[symbol].get("id")), self._prepare_watch_item(merged))
                continue
            if symbol in active_symbols:
                continue
            self.watchlist_store.add_watch_item(self._prepare_watch_item(signal))
            active_symbols.add(symbol)

    def _refresh_watchlist_states(self) -> None:
        watchlist = self.watchlist_store.list_watchlist(["watching"])
        if watchlist.empty:
            return
        for _, watch_row in watchlist.iterrows():
            item = watch_row.to_dict()
            daily = self._ensure_daily_bars(_to_text(item.get("symbol")))
            if daily.empty:
                continue
            updated = self._update_watch_item_from_daily(item, daily)
            if updated.get("status") == "triggered":
                self.watchlist_store.update_watch_item(_to_text(item.get("id")), updated)
                self.watchlist_store.add_holding_item(self._build_holding_from_watch(updated))
                continue
            self.watchlist_store.update_watch_item(_to_text(item.get("id")), updated)

    def _refresh_holding_states(self) -> None:
        holdings = self.watchlist_store.list_holdings(["holding"])
        if holdings.empty:
            return
        for _, holding_row in holdings.iterrows():
            item = holding_row.to_dict()
            daily = self._ensure_daily_bars(_to_text(item.get("symbol")))
            if daily.empty:
                continue
            updated = self._update_holding_item_from_daily(item, daily)
            self.watchlist_store.update_holding_item(_to_text(item.get("id")), updated)

    def _default_stage2_watch_pool(self) -> pd.DataFrame:
        results = self.get_results()
        if results.empty:
            return pd.DataFrame()
        _, stage2, _ = self.get_rankings()
        watch_rows = results.copy()
        watch_rows["watch_rank_label"] = watch_rows.apply(get_watch_rank_label, axis=1)
        watch_rows["trend_stage_label"] = watch_rows.apply(lambda row: get_trend_stage_label(row, self.config), axis=1)
        strong_pool = watch_rows[
            watch_rows["watch_rank_label"].isin(["核心候选", "强观察"])
            & watch_rows["trend_stage_label"].astype(str).str.contains("Stage II", case=False, na=False)
        ]
        frames = [frame for frame in [stage2, strong_pool] if not frame.empty]
        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["symbol"], keep="first")
        return merged.reset_index(drop=True)

    def _prepare_watch_item(self, signal: dict[str, object]) -> dict[str, object]:
        prepared = dict(signal)
        prepared["expire_date"] = self._compute_watch_expire_date(prepared)
        return prepared

    def _merge_signal_into_watch_item(self, existing: dict[str, object], signal: dict[str, object]) -> dict[str, object]:
        """Merge latest screening signal into an existing watch item.

        Cross-day protection: fields that define the entry contract
        (target_entry_price, breakout_level, stop_loss_reference) are
        locked once the watch_date is before today.  This prevents a
        data re-fetch + screener re-run from silently mutating a
        yesterday entry.

        Same-day correction: when watch_date equals today a re-run is
        likely fixing incomplete data, so the contract fields ARE
        refreshed from the latest signal.
        """
        merged = dict(existing)
        today = date.today().isoformat()
        watch_date = _to_text(existing.get("watch_date"))
        is_same_day = bool(watch_date) and watch_date == today

        always_locked = {
            "id", "created_at",
            "watch_date", "source_trade_date",
            "status", "trigger_date", "trigger_price_observed", "trigger_mode",
        }
        cross_day_locked = {
            "target_entry_price", "breakout_level", "stop_loss_reference",
            "watch_window_days",
        }

        for key, value in signal.items():
            if key in always_locked:
                continue
            if not is_same_day and key in cross_day_locked:
                continue
            merged[key] = value
        return merged

    def _update_watch_item_from_daily(self, item: dict[str, object], daily: pd.DataFrame) -> dict[str, object]:
        updated = dict(item)
        watch_date = pd.to_datetime(item.get("watch_date"), errors="coerce")
        current_status = _to_text(item.get("status"))
        updated["expire_date"] = self._compute_watch_expire_date(updated)
        frame = daily.sort_values("trade_date").copy()
        latest = frame.iloc[-1]
        updated["latest_trade_date"] = _format_date(latest.get("trade_date"))
        updated["latest_close"] = _to_float(latest.get("close"))
        target_entry_price = _to_float(item.get("target_entry_price"))
        if target_entry_price is not None and _to_float(latest.get("close")) not in {None, 0.0}:
            updated["distance_to_entry_pct"] = (target_entry_price / float(latest.get("close")) - 1.0) * 100.0
        watch_window_days = max(_to_int(item.get("watch_window_days")), 1)
        if pd.isna(watch_date):
            updated["days_waited"] = 0
            return updated

        # Already triggered items stay triggered (shown in green), no state change back
        if current_status == "triggered":
            updated["days_waited"] = int(max(_to_int(item.get("days_waited")), 0))
            return updated

        future = frame[frame["trade_date"] > watch_date].copy()
        updated["days_waited"] = int(min(len(future), watch_window_days))
        if future.empty:
            return updated
        window = future.head(watch_window_days).copy()
        breakout_trigger = self._find_breakout_trigger_row(window, target_entry_price)
        if breakout_trigger is not None:
            updated["status"] = "triggered"
            updated["trigger_date"] = _format_date(breakout_trigger.get("trade_date"))
            updated["trigger_price_observed"] = target_entry_price
            updated["trigger_mode"] = "breakout"
            updated["volume_confirmed_on_trigger"] = self._daily_volume_confirmed(frame, breakout_trigger.get("trade_date"))
            return updated
        if len(window) >= watch_window_days:
            updated["status"] = "expired"
        return updated

    def _build_holding_from_watch(self, item: dict[str, object]) -> dict[str, object]:
        entry_date = _to_text(item.get("trigger_date")) or _to_text(item.get("watch_date"))
        entry_mode = _to_text(item.get("trigger_mode")) or "breakout"
        entry_price = _to_float(item.get("trigger_price_observed"))
        if entry_price is None:
            entry_price = _to_float(item.get("target_entry_price"))
        return {
            "watchlist_id": _to_text(item.get("id")),
            "symbol": _to_text(item.get("symbol")),
            "name": _to_text(item.get("name")),
            "from_watch_date": _to_text(item.get("watch_date")),
            "trigger_date": _to_text(item.get("trigger_date")),
            "entry_date": entry_date,
            "entry_price": entry_price,
            "entry_mode": entry_mode,
            "latest_trade_date": _to_text(item.get("latest_trade_date")),
            "latest_close": _to_float(item.get("latest_close")),
            "holding_days": 0,
            "current_return_pct": 0.0 if entry_price is not None else None,
            "highest_price_since_entry": entry_price,
            "lowest_price_since_entry": entry_price,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "stage_label_latest": _to_text(item.get("stage_label")),
            "watch_rank_latest": _to_text(item.get("watch_rank_label")),
            "volume_confirmed_on_trigger": _to_bool(item.get("volume_confirmed_on_trigger")),
            "breakout_level": _to_float(item.get("breakout_level")),
            "stop_loss_reference": _to_float(item.get("stop_loss_reference")),
            "risk_flag": "",
            "status": "holding",
            "close_date": "",
            "close_reason": "",
        }

    def _update_holding_item_from_daily(self, item: dict[str, object], daily: pd.DataFrame) -> dict[str, object]:
        updated = dict(item)
        entry_date = pd.to_datetime(item.get("entry_date"), errors="coerce")
        entry_price = _to_float(item.get("entry_price"))
        frame = daily.sort_values("trade_date").copy()
        if pd.isna(entry_date) or entry_price is None:
            return updated
        holding_frame = frame[frame["trade_date"] >= entry_date].copy()
        if holding_frame.empty:
            return updated
        latest = holding_frame.iloc[-1]
        highest_price = float(holding_frame["high"].max())
        lowest_price = float(holding_frame["low"].min())
        current_return_pct = (float(latest.get("close")) / entry_price - 1.0) * 100.0 if entry_price else None
        mfe_pct = (highest_price / entry_price - 1.0) * 100.0 if entry_price else None
        mae_pct = (lowest_price / entry_price - 1.0) * 100.0 if entry_price else None
        updated["latest_trade_date"] = _format_date(latest.get("trade_date"))
        updated["latest_close"] = _to_float(latest.get("close"))
        updated["holding_days"] = max(len(holding_frame) - 1, 0)
        updated["current_return_pct"] = current_return_pct
        updated["highest_price_since_entry"] = highest_price
        updated["lowest_price_since_entry"] = lowest_price
        updated["mfe_pct"] = mfe_pct
        updated["mae_pct"] = mae_pct
        latest_row = self._resolve_stock_row(_to_text(item.get("symbol")))
        updated["stage_label_latest"] = get_trend_stage_label(latest_row, self.config)
        updated["watch_rank_latest"] = get_watch_rank_label(latest_row)
        updated["risk_flag"] = self._build_holding_risk_flag(updated)
        return updated

    def _build_holding_risk_flag(self, item: dict[str, object]) -> str:
        latest_close = _to_float(item.get("latest_close"))
        stop_loss_reference = _to_float(item.get("stop_loss_reference"))
        breakout_ref = _to_float(item.get("breakout_ref_price")) or _to_float(item.get("breakout_level"))
        stage_label_latest = _to_text(item.get("stage_label_latest"))
        if latest_close is not None and stop_loss_reference is not None and latest_close < stop_loss_reference:
            return "跌破止损参考"
        if latest_close is not None and breakout_ref is not None and latest_close < breakout_ref:
            return "跌回突破位下方"
        if "Stage II" not in stage_label_latest:
            return "趋势阶段转弱"
        return "正常"

    def _daily_volume_confirmed(self, frame: pd.DataFrame, trade_date: object) -> bool:
        marker = pd.to_datetime(trade_date, errors="coerce")
        if pd.isna(marker):
            return False
        ordered = frame.sort_values("trade_date").copy().reset_index(drop=True)
        ordered["volume"] = pd.to_numeric(ordered["volume"], errors="coerce")
        matched = ordered[ordered["trade_date"] == marker]
        if matched.empty:
            return False
        idx = int(matched.index[0])
        start_idx = max(0, idx - max(self.config.strategy.daily_volume_avg_days, 5))
        baseline = ordered.iloc[start_idx:idx]["volume"].dropna()
        if baseline.empty:
            return False
        return float(ordered.iloc[idx]["volume"]) >= float(baseline.mean())

    def _find_breakout_trigger_row(self, window: pd.DataFrame, target_entry_price: float | None) -> pd.Series | None:
        if target_entry_price is None or window.empty:
            return None
        trigger_rows = window[pd.to_numeric(window["high"], errors="coerce") >= target_entry_price]
        if trigger_rows.empty:
            return None
        return trigger_rows.iloc[0]

    def _compute_watch_expire_date(self, item: dict[str, object]) -> str:
        watch_date = pd.to_datetime(item.get("watch_date"), errors="coerce")
        if pd.isna(watch_date):
            return ""
        watch_window_days = max(_to_int(item.get("watch_window_days")), 1)
        future_business_days = pd.bdate_range(watch_date + pd.offsets.BDay(1), periods=watch_window_days)
        if len(future_business_days) == 0:
            return _format_date(watch_date)
        return future_business_days[-1].strftime("%Y-%m-%d")

    def _serialize_quasi_stage2(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "rank": _to_int(row.get("quasi_stage2_rank")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "stage": _stage_label_text(row.get("stage_label")),
                    "close": _format_number(row.get("close")),
                    "watch_reason": _first_text(row.get("stage2_watch_reason"), row.get("stage2_reason")),
                    "missing_gates": _format_quasi_missing_gates(row),
                    "final_score": _format_number(row.get("final_score")),
                    "analysis": build_weinstein_analysis(row, self.config),
                }
            )
        return rows

    def _serialize_stage2(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            target_entry_price = _resolve_target_entry_price(row, self.config)
            stop_loss_reference = _resolve_stop_loss_reference(row)
            rows.append(
                {
                    "rank": _to_int(row.get("stage2_top_n_rank")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "stage": _stage_label_text(row.get("stage_label")),
                    "close": _format_number(row.get("close")),
                    "target_entry_price": _format_number(target_entry_price),
                    "stop_loss_reference": _format_number(stop_loss_reference),
                    "watch_reason": _first_text(row.get("stage2_watch_reason"), row.get("stage2_reason")),
                    "final_score": _format_number(row.get("final_score")),
                    "analysis": build_weinstein_analysis(row, self.config),
                }
            )
        return rows

    @staticmethod
    def _lookup_rank(frame: pd.DataFrame, symbol: str, rank_column: str) -> int | None:
        if frame.empty:
            return None
        matched = frame[frame["symbol"].astype(str).str.upper() == symbol]
        if matched.empty:
            return None
        rank = matched.iloc[0].get(rank_column)
        if rank is None or pd.isna(rank):
            return None
        return int(rank)

    def _build_reject_reason(self, record: dict[str, object]) -> str:
        cfg = self.config.strategy
        reasons: list[str] = []
        for key, label in [
            ("market_ok", "大盘(价>30周线+均线上倾+10周>30周)"),
            ("stage2_candidate", "阶段II(站上30周线+均线上倾+高低点抬升+基底平整)"),
            ("volume_ok", "量能(周量比≥1.0)"),
            ("rs_ok", f"RS排名(前{cfg.rs_rank_threshold_pct}%)"),
            ("resistance_ok", f"上方空间(≥{cfg.resistance_min_headroom_pct}%)"),
        ]:
            if not record.get(key, False):
                reasons.append(label)
        if cfg.enable_breakout_filter and not record.get("breakout_ok", False):
            reasons.append(f"突破确认(涨幅≥{cfg.breakout_min_pct}%)")
        return "；".join(reasons) if reasons else ""


def _to_float(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _to_bool(value: object) -> bool:
    if value is None or pd.isna(value):
        return False
    return bool(value)


def _to_int(value: object) -> int:
    if value is None or pd.isna(value):
        return 0
    return int(value)


def _to_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def _first_text(*values: object) -> str:
    for value in values:
        text = _to_text(value)
        if text:
            return text
    return ""


def _stage_label_text(value: object) -> str:
    key = _to_text(value) or "UNKNOWN"
    return STAGE_LABELS.get(key, key if key != "UNKNOWN" else "未知")


def _format_quasi_missing_gates(row: pd.Series) -> str:
    missing = [label for key, label in QUASI_GATE_LABELS.items() if not _to_bool(row.get(key))]
    return "、".join(missing) if missing else "--"


def _format_number(value: object, format_spec: str = ".2f", fallback: str = "--") -> str:
    numeric = _to_float(value)
    if numeric is None:
        return fallback
    if format_spec.startswith("#"):
        return format(numeric, format_spec.replace("#", ""))
    return format(numeric, format_spec)


def _format_percent(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"{numeric:.2f}%"


def _format_signed_number(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"{numeric:+.2f}"


def _format_signed_percent(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"{numeric:+.2f}%"


def _format_percent_rank(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"前 {numeric:.0f}%"


def _format_base_extension(row: dict[str, object]) -> str:
    """Format distance from base breakout price as percentage."""
    base_bp = _to_float(row.get("base_breakout_price"))
    close_p = _to_float(row.get("close"))
    if base_bp is None or base_bp == 0 or close_p is None:
        return "--"
    ext = (close_p / base_bp - 1.0) * 100.0
    fixed = row.get("base_breakout_fixed", False)
    tag = " ✓锁定" if fixed else ""
    return f"{ext:+.1f}%{tag}"


def _format_base_quality(row: dict[str, object]) -> str:
    """Format base quality score with grade."""
    score = _to_float(row.get("base_quality_score"))
    grade = _to_text(row.get("base_quality_grade"))
    if score is None or grade is None or grade == "":
        return "--"
    icons = {"S": "🏆", "A": "⭐", "B": "✓", "C": "—"}
    icon = icons.get(grade, "")
    return f"{icon} {score:.0f}分 ({grade})"


def _format_industry_rs(value: object) -> str:
    """industry_rs_rank_pct: 行业强度百分位, 100=最强, 0=最弱."""
    numeric = _to_float(value)
    if numeric is None:
        return "--"
    label = "强" if numeric >= 70 else "中" if numeric >= 30 else "弱"
    return f"前 {numeric:.0f}% ({label})"


def _format_date(value: object) -> str:
    if value is None or pd.isna(value):
        return "--"
    text = str(value).strip()
    if not text:
        return "--"
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return "--"
    return parsed.strftime("%Y-%m-%d")


def _series_mean(series: object) -> float | None:
    if series is None:
        return None
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.dropna().empty:
        return None
    return float(numeric.mean())


def _series_median(series: object) -> float | None:
    if series is None:
        return None
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.dropna().empty:
        return None
    return float(numeric.median())
