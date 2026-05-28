from __future__ import annotations

import json
from pathlib import Path
from threading import RLock

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
from winstan.rules.market_trend import evaluate_market_trend
from winstan.rules.relative_strength_rule import evaluate_relative_strength
from winstan.rules.resistance_rule import evaluate_resistance
from winstan.rules.stage_analysis import apply_stage2_scoring, evaluate_stage
from winstan.rules.volume_confirmation import evaluate_volume
from winstan.scoring.ranker import build_quasi_stage2_top_n, build_stage2_top_n, score_and_rank
from winstan.storage.duckdb_store import DuckDBStore
from winstan.storage.parquet_store import ParquetStore

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


class DashboardService:
    def __init__(self, config_path: str | Path) -> None:
        self.config = load_config(Path(config_path))
        self.parquet_store = ParquetStore(self.config.parquet_root)
        self.duckdb_store = DuckDBStore(self.config.duckdb_path)
        self._router: DataSourceRouter | None = None
        self._results: pd.DataFrame | None = None
        self._stage1: pd.DataFrame | None = None
        self._stage2: pd.DataFrame | None = None
        self._quasi_stage2: pd.DataFrame | None = None
        self._universe: pd.DataFrame | None = None
        self._detail_analysis_cache: dict[str, str] = {}
        self._lock = RLock()

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

    def get_dashboard_payload(self) -> dict[str, object]:
        results = self.get_results()
        stage1, stage2, quasi_stage2 = self.get_rankings()
        return {
            "summary": {
                "total_symbols": int(len(results)),
                "candidate_count": int(results["stage2_candidate"].sum()) if not results.empty else 0,
                "stage1_count": int(len(stage1)),
                "stage2_count": int(len(stage2)),
                "quasi_stage2_count": int(len(quasi_stage2)),
            },
            "update_status": self._get_update_status(),
            "stage1": self._serialize_stage1(stage1),
            "stage2": self._serialize_stage2(stage2),
            "quasi_stage2": self._serialize_quasi_stage2(quasi_stage2),
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

    def get_stock_detail(self, symbol: str) -> dict[str, object]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("股票代码不能为空")

        row = self._resolve_stock_row(normalized)

        name = _first_text(row.get("name"), self._lookup_stock_name(normalized))
        daily = self._ensure_daily_bars(normalized)
        if daily.empty:
            raise ValueError(f"未找到 {normalized} 的行情数据")

        stage1, stage2, _ = self.get_rankings()

        latest_trade_date = daily.sort_values("trade_date")["trade_date"].iloc[-1]

        return {
            "symbol": normalized,
            "name": name,
            "stage1_rank": self._lookup_rank(stage1, normalized, "top_n_rank"),
            "stage2_rank": self._lookup_rank(stage2, normalized, "stage2_top_n_rank"),
            "metrics": self._build_metrics(row, latest_trade_date=latest_trade_date),
            "chart": self._build_chart_payload(daily, row),
        }

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
                results = WeinsteinScreener(self.config).run()["results"]

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
        cached = clean_daily_bars(self.parquet_store.read_symbol_frame("daily_bars", symbol))
        if not cached.empty:
            return cached

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
        resistance_info = evaluate_resistance(recent, latest, self.config)
        breakout_info = evaluate_breakout(latest, self.config)

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
            {"label": "距突破位", "value": _format_percent(row.get("breakout_pct"))},
            {"label": "拒绝原因", "value": _first_text(row.get("reject_reason"), "无")},
        ]

    def _build_chart_payload(self, daily: pd.DataFrame, row: pd.Series) -> dict[str, object]:
        frame = daily.sort_values("trade_date").tail(240).copy()
        frame["ma144"] = frame["close"].rolling(144).mean()
        frame["ma169"] = frame["close"].rolling(169).mean()
        breakout_line = _to_float(row.get("breakout_level"))
        resistance_line = _to_float(row.get("nearest_resistance"))
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
                    "ma144": _to_float(row.get("ma144")),
                    "ma169": _to_float(row.get("ma169")),
                }
            )
        return {
            "candles": items,
            "breakout_line": breakout_line,
            "resistance_line": resistance_line,
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
                    "watch_score": _format_number(row.get("watch_score")),
                    "total_score": _format_number(row.get("total_score")),
                    "analysis": build_weinstein_analysis(row, self.config),
                }
            )
        return rows

    def _serialize_quasi_stage2(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "rank": _to_int(row.get("quasi_stage2_rank")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "stage": _stage_label_text(row.get("stage_label")),
                    "watch_reason": _first_text(row.get("stage2_watch_reason"), row.get("stage2_reason")),
                    "missing_gates": _format_quasi_missing_gates(row),
                    "final_score": _format_number(row.get("final_score")),
                    "structure_score": _format_number(row.get("structure_score")),
                    "timing_score": _format_number(row.get("timing_score")),
                    "strength_score": _format_number(row.get("strength_score")),
                    "risk_score": _format_number(row.get("risk_score")),
                    "analysis": build_weinstein_analysis(row, self.config),
                }
            )
        return rows

    def _serialize_stage2(self, frame: pd.DataFrame) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "rank": _to_int(row.get("stage2_top_n_rank")),
                    "symbol": _to_text(row.get("symbol")),
                    "name": _to_text(row.get("name")),
                    "stage": _stage_label_text(row.get("stage_label")),
                    "close": _format_number(row.get("close")),
                    "watch_reason": _first_text(row.get("stage2_watch_reason"), row.get("stage2_reason")),
                    "final_score": _format_number(row.get("final_score")),
                    "structure_score": _format_number(row.get("structure_score")),
                    "timing_score": _format_number(row.get("timing_score")),
                    "strength_score": _format_number(row.get("strength_score")),
                    "risk_score": _format_number(row.get("risk_score")),
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


def _format_number(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"{numeric:.2f}"


def _format_percent(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"{numeric:.2f}%"


def _format_percent_rank(value: object) -> str:
    numeric = _to_float(value)
    return "--" if numeric is None else f"前{numeric:.2f}%"


def _format_date(value: object) -> str:
    if value is None or pd.isna(value):
        return "--"
    return pd.to_datetime(value).strftime("%Y-%m-%d")
